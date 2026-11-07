"""Research policy in ordinary asyncio and native Agents SDK calls."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agents import Agent, RunConfig, Runner, custom_span, trace
from pydantic import BaseModel, ConfigDict

from .task_pool import PoolChange, TaskPool, TaskRequest, TaskState
from .tracing import trace_model_input


class Plan(BaseModel):
    model_config = ConfigDict(frozen=True)
    question: str
    change: str
    rationale: str
    based_on: tuple[str, ...]


class Evidence(BaseModel):
    """Host-collected development feedback, independent of Execute's answer."""

    model_config = ConfigDict(frozen=True)
    execution_complete: bool
    valid: bool
    feedback: Literal["development"] = "development"
    observations: dict[str, Any]


class Review(BaseModel):
    model_config = ConfigDict(frozen=True)
    interpretation: str
    limitations: tuple[str, ...]
    knowledge_changed: bool
    goal: Literal["achieved", "not-achieved", "unknown"]
    continue_research: bool
    next_question: str


class Record(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    task: TaskRequest
    source: Literal["human", "autonomous"]
    plan: Plan
    execute: str
    evidence: Evidence
    review: Review


class Proposal(BaseModel):
    rationale: str
    tasks: tuple[TaskRequest, ...]


class JudgeDecision(BaseModel):
    changes: tuple[PoolChange, ...]
    stop: bool
    reason: str


class ScoutReport(BaseModel):
    summary: str
    sources: tuple[str, ...]
    limitations: tuple[str, ...]


class ResearchResult(BaseModel):
    trace_id: str
    records: list[Record]
    scout_reports: list[ScoutReport]
    pool: list[dict[str, Any]]
    notes: str
    goal: Literal["achieved", "not-achieved", "unknown"]
    stopped: str


@dataclass
class ResearchAgents:
    advocates: tuple[Agent, Agent]
    judge: Agent
    scout: Agent
    knowledge_manager: Agent


@dataclass
class Investigation:
    planner: Agent
    executor: Agent
    reviewer: Agent
    collect_evidence: Callable[[], Awaitable[Evidence]]


# The application assembles native Agents and scoped tools per claimed task.
Prepare = Callable[
    [TaskState, tuple[Record, ...], Path, asyncio.Semaphore], Investigation
]


def save_json(path: Path, value: BaseModel | dict | list) -> None:
    data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    with path.open("x") as stream:
        stream.write(json.dumps(data, indent=2) + "\n")


async def run_research(
    *,
    goal: str,
    pool: TaskPool,
    agents: ResearchAgents,
    prepare: Prepare,
    output_dir: Path,
    max_rounds: int = 3,
    investigation_concurrency: int = 2,
    experiment_concurrency: int = 1,
    max_planning_rounds: int = 4,
    max_scout_runs: int = 2,
    minimum_rounds: int = 1,
    max_turns: int = 12,
) -> ResearchResult:
    """Run a new bounded study. Files are evidence, never recovery state.

    Pool changes run synchronously on this event loop. Every SDK call receives
    explicit inputs without a Session or previous response ID. Judge and Review
    stop decisions close task admission; already claimed investigations finish.
    """
    if (
        not 1 <= minimum_rounds <= max_rounds
        or min(investigation_concurrency, experiment_concurrency, max_turns) < 1
        or max_planning_rounds < 1
        or max_scout_runs < 1
    ):
        raise ValueError(
            "Require positive limits and 1 <= minimum_rounds <= max_rounds"
        )
    if (
        len(agents.advocates) != 2
        or any(a.output_type is not Proposal for a in agents.advocates)
        or agents.judge.output_type is not JudgeDecision
        or agents.scout.output_type is not ScoutReport
    ):
        raise ValueError(
            "Use two Proposal Advocates, a JudgeDecision Judge and ScoutReport Scout"
        )
    output_dir.mkdir(parents=True, exist_ok=False)
    config = RunConfig(
        trace_include_sensitive_data=True, call_model_input_filter=trace_model_input
    )
    records: list[Record] = []
    reports: list[ScoutReport] = []
    experiments = asyncio.Semaphore(experiment_concurrency)
    events = asyncio.Queue()
    knowledge_queue = asyncio.Queue()
    manager_idle = asyncio.Event()
    manager_idle.set()
    owned: set[asyncio.Task] = set()
    active: dict[str, asyncio.Task] = {}
    planning = scout = None
    planning_count = scout_count = started = 0
    planning_needed = scout_needed = True
    admission_closed = False
    stopped = "pool-empty"
    knowledge_path = output_dir / "research.md"

    def notes():
        return knowledge_path.read_text() if knowledge_path.exists() else ""

    def context():
        return {
            "goal": goal,
            "pool": pool.snapshot(),
            "reviewed_records": [r.model_dump(mode="json") for r in records],
            "scout_reports": [r.model_dump(mode="json") for r in reports],
            "notes": notes(),
        }

    def start(kind, key, coroutine):
        task = asyncio.create_task(coroutine, name=f"{kind}:{key}")
        owned.add(task)
        task.add_done_callback(lambda done: events.put_nowait((kind, key, done)))
        return task

    async def ask(agent, data):
        with custom_span("role_call", data={"role": agent.name}) as span:
            try:
                result = await Runner.run(
                    agent, json.dumps(data), max_turns=max_turns, run_config=config
                )
                output = result.final_output
                span.span_data.data["output"] = (
                    output.model_dump(mode="json")
                    if isinstance(output, BaseModel)
                    else output
                )
                return output
            except BaseException as exc:
                span.set_error({"message": type(exc).__name__, "data": {}})
                raise

    async def plan_pool(number, frozen):
        # Wait for both independent outcomes, including an individual failure.
        proposals = await asyncio.gather(
            *(ask(agent, frozen) for agent in agents.advocates), return_exceptions=True
        )
        outcomes = [
            {"error": type(value).__name__}
            if isinstance(value, BaseException)
            else {"proposal": value.model_dump(mode="json")}
            for value in proposals
        ]
        save_json(output_dir / f"planning-{number:04d}-advocates.json", outcomes)
        if all(isinstance(value, BaseException) for value in proposals):
            raise RuntimeError("Both Advocates failed; existing tasks are retained")
        decision = await ask(agents.judge, {**frozen, "advocates": outcomes})
        return decision, frozen["pool"]

    async def investigate(state, number):
        record_id = f"r{number:04d}"
        directory = output_dir / record_id
        directory.mkdir()
        frozen_records = tuple(records)
        with custom_span(
            "investigation", data={"record_id": record_id, "task_id": state.request.id}
        ) as span:
            try:
                setup = prepare(state, frozen_records, directory, experiments)
                if (
                    setup.planner.output_type is not Plan
                    or setup.reviewer.output_type is not Review
                ):
                    raise ValueError(
                        "Use Plan and Review output_type for investigation Agents"
                    )
                shared = {
                    "goal": goal,
                    "task": state.model_dump(mode="json"),
                    "reviewed_records": [
                        r.model_dump(mode="json") for r in frozen_records
                    ],
                    "scout_reports": [r.model_dump(mode="json") for r in reports],
                }
                plan = await ask(setup.planner, {**shared, "notes": notes()})
                if not set(plan.based_on) <= {r.id for r in frozen_records}:
                    raise ValueError(
                        "Plan references evidence that has not been reviewed"
                    )
                save_json(directory / "plan.json", plan)
                execute = str(
                    await ask(
                        setup.executor,
                        {**shared, "frozen_plan": plan.model_dump(), "notes": notes()},
                    )
                )
                with custom_span("collect_evidence") as evidence_span:
                    async with experiments:
                        evidence = await setup.collect_evidence()
                    evidence_span.span_data.data = evidence.model_dump(mode="json")
                save_json(directory / "evidence.json", evidence)
                # Review receives neither Execute's hidden history nor its write tools.
                review = await ask(
                    setup.reviewer,
                    {
                        **shared,
                        "frozen_plan": plan.model_dump(),
                        "execute_summary": execute,
                        "trusted_evidence": evidence.model_dump(),
                        "completed_reviews": len(records),
                        "minimum_rounds": minimum_rounds,
                        "notes": notes(),
                    },
                )
                return Record(
                    id=record_id,
                    task=state.request,
                    source=state.source,
                    plan=plan,
                    execute=execute,
                    evidence=evidence,
                    review=review,
                )
            except BaseException as exc:
                span.set_error({"message": type(exc).__name__, "data": {}})
                raise

    async def maintain_knowledge():
        publication = 0
        while True:
            item = await knowledge_queue.get()
            if item is None:
                knowledge_queue.task_done()
                return
            manager_idle.clear()
            batch = [item]
            # Freeze current pending inputs; later arrivals remain for the next call.
            while not knowledge_queue.empty():
                batch.append(knowledge_queue.get_nowait())
            publication += 1
            save_json(output_dir / f"knowledge-{publication:04d}-input.json", batch)
            try:
                content = str(
                    await ask(
                        agents.knowledge_manager,
                        {"goal": goal, "previous_notes": notes(), "evidence": batch},
                    )
                )
                draft = output_dir / "research.md.tmp"
                draft.write_text(content + "\n")
                draft.replace(knowledge_path)
                events.put_nowait(("knowledge", publication, None))
            finally:
                for _ in batch:
                    knowledge_queue.task_done()
                manager_idle.set()

    with trace("research-loop-sdk") as study_trace:
        manager = start("manager", None, maintain_knowledge())
        try:
            while True:
                # Human-ready tasks precede autonomous work, without preemption.
                while (
                    not admission_closed
                    and started < max_rounds
                    and len(active) < investigation_concurrency
                    and pool.ready()
                ):
                    state = pool.claim(pool.ready()[0].request.id)
                    started += 1
                    active[state.request.id] = start(
                        "investigation", state.request.id, investigate(state, started)
                    )
                can_plan = not admission_closed and started < max_rounds
                if (
                    can_plan
                    and planning_needed
                    and planning is None
                    and planning_count < max_planning_rounds
                ):
                    planning_count += 1
                    planning_needed = False
                    planning = start(
                        "planning", planning_count, plan_pool(planning_count, context())
                    )
                if (
                    can_plan
                    and scout_needed
                    and scout is None
                    and scout_count < max_scout_runs
                ):
                    scout_count += 1
                    scout_needed = False
                    scout = start("scout", scout_count, ask(agents.scout, context()))
                if (
                    not active
                    and planning is None
                    and scout is None
                    and manager_idle.is_set()
                    and knowledge_queue.empty()
                    and events.empty()
                ):
                    break
                kind, key, done = await events.get()
                if kind == "knowledge":
                    planning_needed = True
                    continue
                if kind == "manager":
                    done.result()
                    raise RuntimeError("Knowledge Manager stopped unexpectedly")
                if kind == "investigation":
                    del active[key]
                    try:
                        record = done.result()
                        if record.review.goal == "achieved" and (
                            not record.evidence.execution_complete
                            or not record.evidence.valid
                            or len(records) + 1 < minimum_rounds
                        ):
                            raise ValueError(
                                "Goal achieved requires valid complete evidence and minimum rounds"
                            )
                    except BaseException as exc:
                        with custom_span(
                            "accept_review", data={"task_id": key}
                        ) as failure:
                            failure.set_error(
                                {"message": type(exc).__name__, "data": {}}
                            )
                        pool.fail(key)
                        raise
                    save_json(output_dir / record.id / "record.json", record)
                    records.append(record)
                    pool.complete(key, record.id)
                    knowledge_queue.put_nowait(
                        {"kind": "review", "record": record.model_dump(mode="json")}
                    )
                    planning_needed = scout_needed = True
                    if not admission_closed and (
                        record.review.goal == "achieved"
                        or not record.review.continue_research
                    ):
                        admission_closed = True
                        stopped = (
                            "goal-achieved"
                            if record.review.goal == "achieved"
                            else "review-stop"
                        )
                elif kind == "planning":
                    planning = None
                    try:
                        decision, frozen_pool = done.result()
                    except Exception as exc:
                        save_json(
                            output_dir / f"planning-{key:04d}-error.json",
                            {"error": type(exc).__name__},
                        )
                        continue
                    receipt = pool.apply(decision.changes, frozen_pool)
                    save_json(
                        output_dir / f"planning-{key:04d}-judge.json",
                        {
                            "decision": decision.model_dump(mode="json"),
                            "applied": receipt,
                        },
                    )
                    if decision.stop and not admission_closed:
                        admission_closed = True
                        stopped = "judge-stop"
                elif kind == "scout":
                    scout = None
                    try:
                        report = done.result()
                    except Exception as exc:
                        save_json(
                            output_dir / f"scout-{key:04d}-error.json",
                            {"error": type(exc).__name__},
                        )
                        continue
                    save_json(output_dir / f"scout-{key:04d}.json", report)
                    reports.append(report)
                    knowledge_queue.put_nowait(
                        {"kind": "scout", "report": report.model_dump(mode="json")}
                    )
                    planning_needed = True

            knowledge_queue.put_nowait(None)
            await manager
            result = ResearchResult(
                trace_id=study_trace.trace_id,
                records=records,
                scout_reports=reports,
                pool=pool.snapshot(),
                notes=notes(),
                goal="achieved"
                if any(r.review.goal == "achieved" for r in records)
                else (records[-1].review.goal if records else "unknown"),
                stopped=stopped
                if admission_closed
                else (
                    "round-limit"
                    if started >= max_rounds
                    else "pool-blocked"
                    if any(t["status"] == "pending" for t in pool.snapshot())
                    else "pool-empty"
                ),
            )
            save_json(output_dir / "result.json", result)
            return result
        finally:
            for task in owned:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*owned, return_exceptions=True)
            save_json(output_dir / "pool.json", pool.snapshot())
