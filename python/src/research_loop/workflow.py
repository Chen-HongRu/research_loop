"""Research policy in ordinary asyncio and native Agents SDK calls."""

import asyncio
import json
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agents import Agent, ItemHelpers, RunConfig, Runner, custom_span, trace
from pydantic import BaseModel, ConfigDict, Field

from .task_pool import PoolChange, TaskPool, TaskRequest, TaskState
from .tracing import trace_model_input


class Plan(BaseModel):
    model_config = ConfigDict(frozen=True)
    question: str
    change: str
    rationale: str
    based_on: tuple[str, ...]
    execution_kind: Literal["measurement", "analysis"] = "analysis"
    required_artifacts: tuple[str, ...] = ()
    numeric_fields: tuple[str, ...] = ()


class Evidence(BaseModel):
    """Host-collected development feedback, independent of Execute's answer."""

    model_config = ConfigDict(frozen=True)
    execution_complete: bool
    valid: bool
    feedback: Literal["development"] = "development"
    observations: dict[str, Any]


class CompletedExecution(BaseModel):
    """Application-authorized inputs for a fresh Review without re-execution."""

    model_config = ConfigDict(frozen=True)
    plan: Plan
    execute_summary: str
    evidence: Evidence


class Review(BaseModel):
    model_config = ConfigDict(frozen=True)
    interpretation: str
    limitations: tuple[str, ...]
    knowledge_changed: bool
    goal: Literal["achieved", "not-achieved", "unknown"]
    continue_research: bool
    next_question: str
    delivery_complete: bool = True


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


class FatalResearchError(RuntimeError):
    """Application signals that further dispatch is unsafe (e.g. unknown effects)."""


class Failure(BaseModel):
    model_config = ConfigDict(frozen=True)
    phase: str
    task_id: str | None = None
    record_id: str | None = None
    publication: int | None = None
    error_type: str
    error_message: str


@dataclass
class StageFailure:
    phase: str
    task: TaskState | None
    directory: Path
    error: Exception
    input: Any
    output: Any = None
    run_data: Any = None


@dataclass
class RecoveryDecision:
    action: Literal["retry", "blocked"]
    reason: str
    progress_key: str = ""
    retry_after: float = 0
    wakeup: Callable[[], Awaitable[None]] | None = None
    # For reconciled tool errors only; application must retain confirmed outputs.
    continuation_input: list[dict[str, Any]] | None = None
    restart_from: Literal["plan", "execute"] | None = None


class StageBlocked(RuntimeError):
    pass


@dataclass
class DeliveryRetry(Exception):
    setup: Any
    plan: Plan
    execute: str
    record: Record
    decision: RecoveryDecision


def is_fatal_error(error: BaseException) -> bool:
    """SDK tool errors may wrap an application's fatal resource error."""
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if not isinstance(current, Exception) or isinstance(
            current, FatalResearchError
        ):
            return True
        pending.extend(
            linked
            for linked in (current.__cause__, current.__context__)
            if linked is not None
        )
    return False


class ResearchResult(BaseModel):
    trace_id: str
    records: list[Record]
    scout_reports: list[ScoutReport]
    pool: list[dict[str, Any]]
    notes: str
    goal: Literal["achieved", "not-achieved", "unknown"]
    stopped: str
    failures: list[Failure] = Field(default_factory=list)
    pending_knowledge: list[dict[str, Any]] = Field(default_factory=list)


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
    completed_execution: CompletedExecution | None = None


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
    max_rounds: int | None = 3,
    investigation_concurrency: int = 2,
    experiment_concurrency: int = 1,
    max_planning_rounds: int | None = 4,
    max_scout_runs: int | None = 2,
    minimum_rounds: int = 1,
    max_turns: int = 12,
    prepare_input: Callable[[Agent, dict[str, Any]], dict[str, Any]] | None = None,
    knowledge_path: Path | None = None,
    publish_knowledge: Callable[[str], Any] | None = None,
    recover_stage: Callable[[StageFailure], Awaitable[RecoveryDecision]] | None = None,
    validate_evidence_refs: Callable[[tuple[str, ...], tuple[Record, ...]], Any]
    | None = None,
    assess_completion: Callable[[Record], Any] | None = None,
    confirm_stop: Callable[[dict, dict], Awaitable[bool]] | None = None,
    should_drain: Callable[[dict], Any] | None = None,
    on_quiescent: Callable[[dict], Any] | None = None,
    idle_wait: Callable[[dict], Awaitable[bool]] | None = None,
    initial_records: tuple[Record, ...] = (),
    initial_scout_reports: tuple[ScoutReport, ...] = (),
    initial_stage_inputs: dict[str, dict[str, Any]] | None = None,
) -> ResearchResult:
    """Run a process-local Study; optional limits keep offline demos bounded.

    Files are evidence. Only an explicit quiescent handoff can be restored.
    Application callbacks own diagnosis, exact evidence identity and Study judgment.
    """

    def within(count, limit):
        return limit is None or count < limit

    if (
        minimum_rounds < 1
        or (max_rounds is not None and minimum_rounds > max_rounds)
        or min(investigation_concurrency, experiment_concurrency, max_turns) < 1
        or any(
            v is not None and v < 1
            for v in (max_rounds, max_planning_rounds, max_scout_runs)
        )
    ):
        raise ValueError("Require positive limits or None and valid minimum_rounds")
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
    records: list[Record] = list(initial_records)
    record_offset = max(
        (
            int(r.id[1:])
            for r in initial_records
            if r.id.startswith("r") and r.id[1:].isdigit()
        ),
        default=0,
    )
    failures: list[Failure] = []
    reports: list[ScoutReport] = list(initial_scout_reports)
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
    draining = False
    stopped = "pool-empty"
    stop_candidates = []
    recovery_signatures = set()
    knowledge_pending = []
    stage_inputs = dict(initial_stage_inputs or {})
    if any(
        value.get("phase") not in {"plan", "execute", "review"}
        for value in stage_inputs.values()
    ):
        raise ValueError(
            "Only explicit Plan/Execute/Review stage inputs can be resumed"
        )
    knowledge_path = (
        Path(knowledge_path)
        if knowledge_path is not None
        else output_dir / "research.md"
    )
    if publish_knowledge is None:
        knowledge_path.parent.mkdir(parents=True, exist_ok=True)
    fatal_error: BaseException | None = None

    def remember_fatal(exc):
        nonlocal fatal_error
        if is_fatal_error(exc):
            if fatal_error is None:
                fatal_error = exc
                events.put_nowait(("fatal", None, None))
            return True
        return False

    def notes():
        return knowledge_path.read_text() if knowledge_path.exists() else ""

    def context():
        return {
            "goal": goal,
            "pool": pool.snapshot(),
            "reviewed_records": [r.model_dump(mode="json") for r in records],
            "failures": [f.model_dump(mode="json") for f in failures],
            "scout_reports": [r.model_dump(mode="json") for r in reports],
            "notes": notes(),
            "pending_knowledge": list(knowledge_pending),
            "draining": draining,
        }

    def start(kind, key, coroutine):
        task = asyncio.create_task(coroutine, name=f"{kind}:{key}")
        owned.add(task)
        task.add_done_callback(lambda done: events.put_nowait((kind, key, done)))
        return task

    async def resolve(value):
        return await value if inspect.isawaitable(value) else value

    async def recover(failure):
        if failure.task is not None:
            pool.stage(failure.task.request.id, failure.phase, correcting=True)
            failure.task = pool.state(failure.task.request.id)
        if recover_stage is None:
            if failure.phase == "delivery":
                raise StageBlocked(str(failure.error))
            raise failure.error
        decision = await recover_stage(failure)
        if decision.restart_from is not None and failure.phase != "delivery":
            raise ValueError(
                "Stage restart is allowed only for confirmed incomplete delivery"
            )
        if not decision.reason or decision.retry_after < 0:
            raise ValueError("Recovery requires a condition and nonnegative backoff")
        state = failure.task
        if decision.action == "blocked":
            if state is not None:
                pool.block(
                    state.request.id,
                    decision.reason,
                    "application callback"
                    if decision.wakeup
                    else "external action required",
                )
            if decision.wakeup is None:
                raise StageBlocked(decision.reason)
            await decision.wakeup()
            # A wakeup observes a real condition; diagnosis must still approve retry.
            return await recover(failure)
        if decision.action != "retry" or not decision.progress_key:
            raise StageBlocked("Retry requires a diagnosed action or changed condition")
        signature = (
            failure.phase,
            type(failure.error).__name__,
            str(failure.error),
            decision.progress_key,
        )
        if signature in recovery_signatures:
            raise StageBlocked(
                "Repeated failure without a new verified action or condition: "
                + decision.reason
            )
        recovery_signatures.add(signature)
        if decision.retry_after:
            await asyncio.sleep(decision.retry_after)
        if state is not None:
            pool.stage(state.request.id, failure.phase, correcting=True)
        return decision

    async def ask(
        agent, data, *, phase=None, state=None, directory=None, validate=None
    ):
        phase = phase or agent.name
        directory = directory or output_dir
        original = data
        prepared = False
        if state is not None:
            resumed = stage_inputs.get(state.request.id)
            if resumed is not None and resumed["phase"] == phase:
                data = resumed["input"]
                if not isinstance(data, (str, list)):
                    raise ValueError(
                        "Stage continuation input must be SDK string or history"
                    )
                del stage_inputs[state.request.id]
                prepared = True
        result = None
        output = None
        dispatched = False
        while True:
            captured_error = None

            def capture_error(info):
                nonlocal captured_error
                captured_error = (type(info.error), str(info.error), info.run_data)
                # Native error handlers expose the snapshot before SDK redaction.
                # None preserves failure; no schema-shaped scientific output is invented.
                return None

            if state is not None:
                pool.stage(state.request.id, phase)
            with custom_span("role_call", data={"role": agent.name}) as span:
                try:
                    if not prepared:
                        if prepare_input is not None:
                            data = prepare_input(agent, original)
                            span.span_data.data["input_reference"] = data
                        data = json.dumps(data)
                        prepared = True
                    dispatched = True
                    result = await Runner.run(
                        agent,
                        data,
                        max_turns=max_turns,
                        run_config=config,
                        error_handlers={
                            "invalid_final_output": capture_error,
                            "max_turns": capture_error,
                        },
                    )
                    output = result.final_output
                    if validate is not None:
                        await resolve(validate(output))
                    span.span_data.data["output"] = (
                        output.model_dump(mode="json")
                        if isinstance(output, BaseModel)
                        else output
                    )
                    return output
                except BaseException as exc:
                    if captured_error is not None and isinstance(exc, Exception):
                        error_type, error_message, snapshot = captured_error
                        exc = error_type(error_message)
                        exc.run_data = snapshot
                        output = snapshot.output
                    if remember_fatal(exc):
                        raise
                    span.set_error({"message": type(exc).__name__, "data": {}})
                    run_data = getattr(exc, "run_data", None)
                    failure = StageFailure(
                        phase, state, directory, exc, data, output, run_data
                    )
                    decision = await recover(failure)
                    if decision.continuation_input is not None:
                        history = decision.continuation_input
                    elif result is not None:
                        history = result.to_input_list()
                    elif run_data is not None:
                        if hasattr(run_data, "history"):
                            history = list(run_data.history)
                        else:
                            history = ItemHelpers.input_to_new_input_list(
                                run_data.input
                            )
                            history += [
                                item.to_input_item() for item in run_data.new_items
                            ]
                        calls = {
                            i.get("call_id")
                            for i in history
                            if i.get("type") == "function_call"
                        }
                        outputs = {
                            i.get("call_id")
                            for i in history
                            if i.get("type") == "function_call_output"
                        }
                        if calls - outputs:
                            raise StageBlocked(
                                "Unresolved tool effects require reconciled continuation_input"
                            )
                    else:
                        if dispatched and agent.tools:
                            raise StageBlocked(
                                "Tool-enabled failure without SDK history requires reconciled continuation_input"
                            )
                        history = (
                            ItemHelpers.input_to_new_input_list(data)
                            if prepared
                            else None
                        )
                    if history is not None:
                        history.append(
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {
                                        "stage_error": str(exc),
                                        "original_output": output.model_dump(
                                            mode="json"
                                        )
                                        if isinstance(output, BaseModel)
                                        else output,
                                        "correction": decision.reason,
                                    }
                                ),
                            }
                        )
                        data = history
                        prepared = True
                    result = output = None

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
        for value in proposals:
            if isinstance(value, BaseException) and is_fatal_error(value):
                raise value
        if all(isinstance(value, BaseException) for value in proposals):
            raise RuntimeError("Both Advocates failed; existing tasks are retained")
        decision = await ask(agents.judge, {**frozen, "advocates": outcomes})
        return decision, frozen["pool"]

    async def investigate_once(state, number, delivery=None):
        record_id = f"r{number:04d}" if isinstance(number, int) else f"r{number}"
        directory = output_dir / record_id
        directory.mkdir()
        frozen_records = tuple(records)
        phase = "prepare"
        with custom_span(
            "investigation", data={"record_id": record_id, "task_id": state.request.id}
        ) as span:
            try:
                setup = (
                    delivery.setup
                    if delivery and delivery.decision.restart_from is None
                    else prepare(state, frozen_records, directory, experiments)
                )
                if (
                    setup.completed_execution is None
                    and setup.planner.output_type is not Plan
                ) or setup.reviewer.output_type is not Review:
                    raise ValueError(
                        "Use Plan and Review output_type for investigation Agents"
                    )
                shared = {
                    "goal": goal,
                    "task": state.model_dump(mode="json"),
                    "reviewed_records": [
                        r.model_dump(mode="json") for r in frozen_records
                    ],
                    "failures": [f.model_dump(mode="json") for f in failures],
                    "scout_reports": [r.model_dump(mode="json") for r in reports],
                }

                def validate_plan(plan):
                    if validate_evidence_refs is not None:
                        return validate_evidence_refs(plan.based_on, frozen_records)
                    if not set(plan.based_on) <= {r.id for r in frozen_records}:
                        raise ValueError(
                            "Plan references evidence that has not been reviewed"
                        )

                if delivery is not None:
                    shared["delivery_correction"] = {
                        "previous_record": delivery.record.model_dump(mode="json"),
                        "reason": delivery.decision.reason,
                        "instruction": "Correct the incomplete delivery, retain confirmed measurements and effects, and record revisions.",
                    }
                retained = setup.completed_execution
                resume_entry = stage_inputs.get(state.request.id)
                if delivery is None and resume_entry is not None:
                    if resume_entry["phase"] == "review" and retained is None:
                        raise ValueError(
                            "Review continuation requires completed_execution"
                        )
                    if resume_entry["phase"] == "execute" and retained is not None:
                        raise ValueError("Completed execution cannot resume Execute")
                if delivery is not None and delivery.decision.restart_from is None:
                    phase = "collect_evidence"
                    while True:
                        try:
                            async with experiments:
                                corrected = await setup.collect_evidence()
                            break
                        except Exception as exc:
                            if remember_fatal(exc):
                                raise
                            await recover(
                                StageFailure(phase, state, directory, exc, shared)
                            )
                    retained = CompletedExecution(
                        plan=delivery.plan,
                        execute_summary=delivery.execute,
                        evidence=corrected,
                    )
                resumed = stage_inputs.get(state.request.id)
                if delivery is not None and delivery.decision.restart_from == "execute":
                    plan = delivery.plan
                    retained = None
                elif (
                    delivery is None
                    and resumed is not None
                    and resumed["phase"] == "execute"
                ):
                    plan = Plan.model_validate(resumed["plan"])
                    retained = None
                elif retained is None:
                    phase = "plan"
                    plan = await ask(
                        setup.planner,
                        {**shared, "notes": notes()},
                        phase=phase,
                        state=state,
                        directory=directory,
                        validate=validate_plan,
                    )
                else:
                    phase = "completed_execution"
                    if not retained.evidence.execution_complete and delivery is None:
                        raise ValueError("Retained execution must be complete")
                    plan = retained.plan
                await resolve(validate_plan(plan))
                save_json(directory / "plan.json", plan)
                if retained is None:
                    phase = "execute"
                    execute = str(
                        await ask(
                            setup.executor,
                            {
                                **shared,
                                "frozen_plan": plan.model_dump(),
                                "notes": notes(),
                            },
                            phase=phase,
                            state=state,
                            directory=directory,
                        )
                    )
                    phase = "collect_evidence"
                    with custom_span("collect_evidence") as evidence_span:
                        while True:
                            pool.stage(state.request.id, phase)
                            try:
                                async with experiments:
                                    evidence = await setup.collect_evidence()
                                break
                            except Exception as exc:
                                if remember_fatal(exc):
                                    raise
                                await recover(
                                    StageFailure(phase, state, directory, exc, shared)
                                )
                        evidence_span.span_data.data = evidence.model_dump(mode="json")
                else:
                    execute = retained.execute_summary
                    evidence = retained.evidence
                save_json(
                    directory / "completed-execution.json",
                    CompletedExecution(
                        plan=plan,
                        execute_summary=execute,
                        evidence=evidence,
                    ),
                )
                save_json(directory / "evidence.json", evidence)
                # Review receives neither Execute's hidden history nor its write tools.
                phase = "review"

                def validate_review(review):
                    if (
                        review.goal == "achieved"
                        and not review.continue_research
                        and (
                            not evidence.execution_complete
                            or not evidence.valid
                            or len(records) + 1 < minimum_rounds
                        )
                    ):
                        raise ValueError(
                            "Goal achieved requires valid complete evidence and minimum rounds"
                        )

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
                    phase=phase,
                    state=state,
                    directory=directory,
                    validate=validate_review,
                )
                record = Record(
                    id=record_id,
                    task=state.request,
                    source=state.source,
                    plan=plan,
                    execute=execute,
                    evidence=evidence,
                    review=review,
                )
                complete = (
                    evidence.execution_complete
                    and evidence.valid
                    and review.delivery_complete
                    and (
                        assess_completion is None
                        or await resolve(assess_completion(record))
                    )
                )
                if not complete:
                    # Publish truthful missing evidence, but never unlock dependents.
                    save_json(directory / "incomplete-record.json", record)
                    knowledge_queue.put_nowait(
                        {
                            "kind": "incomplete-review",
                            "record": record.model_dump(mode="json"),
                        }
                    )
                    phase = "delivery"
                    decision = await recover(
                        StageFailure(
                            "delivery",
                            state,
                            directory,
                            ValueError("Frozen task delivery is incomplete"),
                            shared,
                            record,
                        )
                    )
                    # The repair callback can arrange missing execution; re-collect host
                    # evidence and review it without replaying prior confirmed effects.
                    phase = "delivery"
                    raise DeliveryRetry(setup, plan, execute, record, decision)
                return record
            except DeliveryRetry:
                raise
            except BaseException as exc:
                span.set_error({"message": type(exc).__name__, "data": {}})
                if remember_fatal(exc):
                    raise
                failure = Failure(
                    phase=phase,
                    task_id=state.request.id,
                    record_id=record_id,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                save_json(directory / "error.json", failure)
                return failure

    async def investigate(state, number):
        delivery = None
        revision = 0
        while True:
            try:
                return await investigate_once(
                    state,
                    number if not revision else f"{number}-repair-{revision}",
                    delivery,
                )
            except DeliveryRetry as retry:
                delivery = retry
                revision += 1

    async def maintain_knowledge():
        publication = 0
        while True:
            item = await knowledge_queue.get()
            if item is None:
                knowledge_queue.task_done()
                return
            manager_idle.clear()
            batch = [item]
            while not knowledge_queue.empty():
                batch.append(knowledge_queue.get_nowait())
            knowledge_pending.extend(batch)
            publication += 1
            frozen_batch = list(knowledge_pending)
            try:
                save_json(
                    output_dir / f"knowledge-{publication:04d}-input.json", frozen_batch
                )

                async def publish(content):
                    content = str(content)
                    if publish_knowledge is not None:
                        await resolve(publish_knowledge(content))
                    else:
                        draft = knowledge_path.with_name(knowledge_path.name + ".tmp")
                        draft.write_text(content + "\n")
                        draft.replace(knowledge_path)

                content = str(
                    await ask(
                        agents.knowledge_manager,
                        {
                            "goal": goal,
                            "previous_notes": notes(),
                            "evidence": frozen_batch,
                        },
                        phase="knowledge",
                        validate=publish,
                    )
                )
                (output_dir / f"knowledge-{publication:04d}-output.md").write_text(
                    content + "\n"
                )
                del knowledge_pending[: len(frozen_batch)]
                events.put_nowait(("knowledge", publication, None))
            except Exception as exc:
                if remember_fatal(exc):
                    raise
                failure = Failure(
                    phase="knowledge",
                    publication=publication,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                save_json(
                    output_dir / f"knowledge-{publication:04d}-error.json",
                    {
                        "failure": failure.model_dump(mode="json"),
                        "failed_batch": frozen_batch,
                    },
                )
                failures.append(failure)
                # Evidence stays pending. A diagnosed retry above, or new input,
                # can retry it; failure alone never causes a tight planning loop.
            finally:
                for _ in batch:
                    knowledge_queue.task_done()
                manager_idle.set()
                events.put_nowait(("manager_idle", None, None))

    with trace("research-loop-sdk") as study_trace:
        manager = start("manager", None, maintain_knowledge())
        try:
            while True:
                if fatal_error is not None:
                    raise fatal_error
                if should_drain is not None and not draining:
                    draining = bool(await resolve(should_drain(context())))
                if (
                    on_quiescent is not None
                    and not active
                    and planning is None
                    and scout is None
                    and manager_idle.is_set()
                    and knowledge_queue.empty()
                    and not knowledge_pending
                    and events.empty()
                    and await resolve(on_quiescent(context()))
                ):
                    admission_closed = True
                    stopped = "handoff"
                    break
                # Human-ready tasks precede autonomous work, without preemption.
                while (
                    not admission_closed
                    and not draining
                    and within(started, max_rounds)
                    and len(active) < investigation_concurrency
                    and pool.ready()
                ):
                    state = pool.claim(pool.ready()[0].request.id)
                    started += 1
                    active[state.request.id] = start(
                        "investigation",
                        state.request.id,
                        investigate(state, started + record_offset),
                    )
                can_plan = (
                    not admission_closed
                    and not draining
                    and within(started, max_rounds)
                )
                if (
                    can_plan
                    and planning_needed
                    and planning is None
                    and within(planning_count, max_planning_rounds)
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
                    and within(scout_count, max_scout_runs)
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
                    current = context()
                    if (
                        on_quiescent is not None
                        and not knowledge_pending
                        and await resolve(on_quiescent(current))
                    ):
                        admission_closed = True
                        stopped = "handoff"
                        break
                    if (
                        confirm_stop is not None
                        and stop_candidates
                        and not knowledge_pending
                        and not draining
                    ):
                        candidate = stop_candidates[-1]
                        confirmed = await confirm_stop(candidate, current)
                        # Callback may wait while external evidence/conditions change;
                        # compare the exact process-owned snapshot after it returns.
                        if confirmed and current == context():
                            admission_closed = True
                            stopped = "goal-achieved"
                    if (
                        not admission_closed
                        and max_rounds is None
                        and idle_wait is not None
                    ):
                        if await idle_wait(context()):
                            planning_needed = scout_needed = True
                            continue
                    break
                kind, key, done = await events.get()
                if kind == "fatal":
                    raise fatal_error
                if kind == "manager_idle":
                    continue
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
                    except BaseException:
                        pool.fail(key)
                        raise
                    if isinstance(record, Failure):
                        failures.append(record)
                        if record.error_type == "StageBlocked":
                            pool.block(
                                key, record.error_message, "external action required"
                            )
                        else:
                            pool.fail(key)
                        planning_needed = scout_needed = True
                        continue
                    save_json(output_dir / record.id / "record.json", record)
                    records.append(record)
                    pool.complete(key, record.id)
                    knowledge_queue.put_nowait(
                        {"kind": "review", "record": record.model_dump(mode="json")}
                    )
                    planning_needed = scout_needed = True
                    if not record.review.continue_research:
                        stop_candidates.append(
                            {
                                "source": "review",
                                "record": record.model_dump(mode="json"),
                                "context": context(),
                            }
                        )
                elif kind == "planning":
                    planning = None
                    try:
                        decision, frozen_pool = done.result()
                    except Exception as exc:
                        if remember_fatal(exc):
                            raise
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
                    if decision.stop:
                        stop_candidates.append(
                            {
                                "source": "judge",
                                "decision": decision.model_dump(mode="json"),
                                "context": context(),
                            }
                        )
                elif kind == "scout":
                    scout = None
                    try:
                        report = done.result()
                    except Exception as exc:
                        if remember_fatal(exc):
                            raise
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
                if stopped == "goal-achieved"
                else (
                    records[-1].review.goal
                    if records and records[-1].review.goal != "achieved"
                    else "unknown"
                ),
                stopped=stopped
                if admission_closed
                else (
                    "round-limit"
                    if not within(started, max_rounds)
                    else "pool-blocked"
                    if knowledge_pending
                    or any(
                        t["status"] in {"pending", "blocked", "failed"}
                        for t in pool.snapshot()
                    )
                    else "pool-empty"
                ),
                failures=failures,
                pending_knowledge=knowledge_pending,
            )
            save_json(output_dir / "result.json", result)
            return result
        finally:
            for task in owned:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*owned, return_exceptions=True)
            save_json(output_dir / "pool.json", pool.snapshot())
