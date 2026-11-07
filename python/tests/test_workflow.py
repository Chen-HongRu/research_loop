import asyncio
import json
from collections import Counter

import pytest
from agents import Agent, function_tool, set_trace_processors
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call

from research_loop.task_pool import CreateTask, TaskPool, TaskRequest
from research_loop.tracing import configure_local_tracing
from research_loop.workflow import (
    Evidence,
    Investigation,
    JudgeDecision,
    Plan,
    Proposal,
    ResearchAgents,
    Review,
    ScoutReport,
    run_research,
)


def payload(call):
    return json.loads(
        call.input if isinstance(call.input, str) else call.input[0]["content"]
    )


def message(value):
    return [
        assistant_message(
            value.model_dump_json() if hasattr(value, "model_dump_json") else value
        )
    ]


@pytest.fixture
def sink(tmp_path):
    processor = configure_local_tracing(tmp_path / "trace.jsonl")
    yield tmp_path / "trace.jsonl"
    set_trace_processors([])
    processor.shutdown()


def program(
    *,
    overlap=False,
    manager_failure=False,
    scout_failure=False,
    advocate_failures=(),
    invalid=False,
    achieved=False,
    plan_reference=None,
):
    stats = {
        "calls": Counter(),
        "models": {},
        "experiments": 0,
        "peak_experiments": 0,
        "manager_active": 0,
        "peak_manager": 0,
        "advocates": set(),
        "plans": set(),
    }
    advocates_ready = asyncio.Event()
    plans_ready = asyncio.Event()
    followup_started = asyncio.Event()

    def agent(role, responder, output_type=None, tools=()):
        async def respond(call):
            stats["calls"][role] += 1
            result = responder(call)
            return await result if hasattr(result, "__await__") else result

        model = ScriptedModel([ModelStep.respond(respond)] * 40, emit_traces=True)
        stats["models"][role] = model
        return Agent(
            name=role,
            instructions=role,
            model=model,
            output_type=output_type,
            tools=list(tools),
        )

    def advocate(label):
        async def propose(call):
            stats["advocates"].add(label)
            if len(stats["advocates"]) == 2:
                advocates_ready.set()
            if overlap:
                await asyncio.wait_for(advocates_ready.wait(), 2)
            if label in advocate_failures:
                raise RuntimeError("synthetic advocate failure")
            return message(
                Proposal(
                    rationale=label,
                    tasks=(
                        TaskRequest(
                            id="auto", question="Follow reviewed a", dependencies=("a",)
                        ),
                    ),
                )
            )

        return agent(label, propose, Proposal)

    def judge(call):
        data = payload(call)
        assert len(data["advocates"]) == 2
        if not any("proposal" in item for item in data["advocates"]):
            raise AssertionError("Judge must have at least one successful Advocate")
        changes = ()
        if "auto" not in {t["request"]["id"] for t in data["pool"]}:
            changes = (
                CreateTask(
                    action="create",
                    task=TaskRequest(
                        id="auto", question="Follow reviewed a", dependencies=("a",)
                    ),
                ),
            )
        return message(
            JudgeDecision(changes=changes, stop=False, reason="preserve humans")
        )

    async def maintain(call):
        stats["manager_active"] += 1
        stats["peak_manager"] = max(stats["peak_manager"], stats["manager_active"])
        try:
            if manager_failure:
                raise RuntimeError("synthetic manager failure")
            if overlap:
                # A third investigation must progress while Manager is still active.
                await asyncio.wait_for(followup_started.wait(), 2)
            await asyncio.sleep(0)
            data = payload(call)
            return message(
                data["previous_notes"]
                + "\nPublished "
                + ",".join(e["kind"] for e in data["evidence"])
            )
        finally:
            stats["manager_active"] -= 1

    def scout(call):
        if scout_failure:
            raise RuntimeError("synthetic scout failure")
        return message(
            ScoutReport(
                summary="Advisory only",
                sources=("synthetic:reference",),
                limitations=("Unverified source claim",),
            )
        )

    agents = ResearchAgents(
        advocates=(advocate("A"), advocate("B")),
        judge=agent("Judge", judge, JudgeDecision),
        scout=agent("Scout", scout, ScoutReport),
        knowledge_manager=agent("Manager", maintain),
    )

    def prepare(state, records, directory, experiments):
        task_id = state.request.id
        if task_id == "auto":
            assert any(r.task.id == "a" for r in records), (
                "Dependency admitted before Review"
            )
            followup_started.set()

        async def plan(call):
            stats["plans"].add(task_id)
            if {"a", "b"} <= stats["plans"]:
                plans_ready.set()
            if overlap and task_id in {"a", "b"}:
                await asyncio.wait_for(plans_ready.wait(), 2)
            return message(
                Plan(
                    question=state.request.question,
                    change="change",
                    rationale="test",
                    based_on=(plan_reference,)
                    if plan_reference
                    else tuple(r.id for r in records),
                )
            )

        @function_tool(failure_error_function=None)
        def probe() -> str:
            """Return task-private tool feedback."""
            return "PRIVATE-" + task_id

        def execute(call):
            outputs = [
                i["output"]
                for i in call.input
                if i.get("type") == "function_call_output"
            ]
            if not outputs:
                return [function_call("probe", {}, call_id="probe-" + task_id)]
            assert outputs == ["PRIVATE-" + task_id]
            return message("Public Execute summary")

        def review(call):
            encoded = json.dumps(call.input)
            assert "PRIVATE-" not in encoded
            assert "function_call_output" not in encoded
            assert call.previous_response_id is None and call.conversation_id is None
            assert len(call.input) == 1
            return message(
                Review(
                    interpretation="reviewed",
                    limitations=("synthetic",),
                    knowledge_changed=True,
                    goal="achieved" if achieved else "not-achieved",
                    continue_research=True,
                    next_question="next",
                )
            )

        async def collect():
            stats["experiments"] += 1
            stats["peak_experiments"] = max(
                stats["peak_experiments"], stats["experiments"]
            )
            try:
                await asyncio.sleep(0.01)
                return Evidence(
                    execution_complete=True,
                    valid=not invalid,
                    observations={"task": task_id},
                )
            finally:
                stats["experiments"] -= 1

        return Investigation(
            planner=agent("Plan:" + task_id, plan, Plan),
            executor=agent("Exec:" + task_id, execute, tools=[probe]),
            reviewer=agent("Review:" + task_id, review, Review),
            collect_evidence=collect,
        )

    return agents, prepare, stats


def run(tmp_path, *, options=None, **kwargs):
    async def scenario():
        agents, prepare, stats = program(**kwargs)
        pool = TaskPool(
            [
                TaskRequest(id="a", question="first"),
                TaskRequest(id="b", question="independent"),
            ]
        )
        result = await asyncio.wait_for(
            run_research(
                goal="test",
                pool=pool,
                agents=agents,
                prepare=prepare,
                output_dir=tmp_path / "study",
                max_rounds=3,
                max_planning_rounds=3,
                max_scout_runs=1,
                **(options or {}),
            ),
            8,
        )
        return result, stats

    return asyncio.run(scenario())


def test_full_parallel_topology_and_local_trace(tmp_path, sink, monkeypatch):
    # A configured key must not cause the replaced default exporter to be used.
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-not-a-credential")
    from agents.tracing.processors import BackendSpanExporter

    def forbidden(*args, **kwargs):
        pytest.fail("Default cloud exporter was called")

    monkeypatch.setattr(BackendSpanExporter, "export", forbidden)
    result, stats = run(tmp_path, overlap=True)
    assert len(result.records) == 3
    assert {r.task.id for r in result.records} == {"a", "b", "auto"}
    assert result.goal == "not-achieved" and result.stopped == "round-limit"
    assert stats["advocates"] == {"A", "B"}
    assert stats["peak_experiments"] == 1
    assert stats["peak_manager"] == 1
    assert result.scout_reports and result.notes
    assert all(t["status"] == "reviewed" for t in result.pool)
    auto = next(r for r in result.records if r.task.id == "auto")
    assert next(r.id for r in result.records if r.task.id == "a") in auto.plan.based_on
    rows = [json.loads(line) for line in sink.read_text().splitlines()]
    assert rows[0]["event"] == "trace.start" and rows[-1]["event"] == "trace.end"
    ended = [r["data"] for r in rows if r["event"] == "span.end"]
    role_calls = [
        s["span_data"]["data"]["role"]
        for s in ended
        if s["span_data"].get("name") == "role_call"
    ]
    assert {"A", "B", "Judge", "Scout", "Manager", "Exec:auto"} <= set(role_calls)
    inputs = [
        s["span_data"]["data"]
        for s in ended
        if s["span_data"].get("name") == "model_input"
    ]
    assert any(
        i["agent"] == "Exec:a" and i["tools"][0]["name"] == "probe" for i in inputs
    )
    assert any(s["span_data"]["type"] == "function" for s in ended)
    assert all(s["trace_id"] == result.trace_id for s in ended)


@pytest.mark.parametrize("failed", [("A",), ("A", "B")])
def test_advocate_failure_does_not_drop_human_tasks(tmp_path, sink, failed):
    result, stats = run(tmp_path, advocate_failures=failed)
    assert {"a", "b"} <= {r.task.id for r in result.records}
    if len(failed) == 2:
        assert stats["calls"]["Judge"] == 0
        assert len(result.records) == 2
        assert list((tmp_path / "study").glob("planning-*-error.json"))
    else:
        assert len(result.records) == 3


def test_scout_failure_is_advisory(tmp_path, sink):
    result, _ = run(tmp_path, scout_failure=True)
    assert len(result.records) == 3 and not result.scout_reports
    assert (tmp_path / "study/scout-0001-error.json").exists()


def test_manager_failure_is_terminal_and_preserves_accepted_records(tmp_path, sink):
    # No Scout input, so fail Manager only once it receives a published Review.
    with pytest.raises(RuntimeError, match="manager failure"):
        run(tmp_path, manager_failure=True, scout_failure=True)
    assert list((tmp_path / "study").glob("r*/record.json"))
    assert (tmp_path / "study/pool.json").exists()
    assert not (tmp_path / "study/result.json").exists()
    assert any(
        json.loads(line)["data"].get("error") for line in sink.read_text().splitlines()
    )


def test_invalid_evidence_cannot_establish_goal(tmp_path, sink):
    with pytest.raises(ValueError, match="Goal achieved requires"):
        run(tmp_path, invalid=True, achieved=True)
    assert not (tmp_path / "study/result.json").exists()
    pool = json.loads((tmp_path / "study/pool.json").read_text())
    assert any(t["status"] == "failed" for t in pool)


def test_unreviewed_plan_reference_rejected_before_execute(tmp_path, sink):
    with pytest.raises(ValueError, match="not been reviewed"):
        run(tmp_path, plan_reference="invented-record")
    assert not list((tmp_path / "study").glob("r*/evidence.json"))


def test_final_only_feedback_is_not_a_valid_evidence_role():
    with pytest.raises(ValueError):
        Evidence(
            execution_complete=True, valid=True, feedback="final-only", observations={}
        )


def test_minimum_reviewed_rounds_enforced(tmp_path, sink):
    with pytest.raises(ValueError, match="minimum rounds"):
        run(tmp_path, achieved=True, options={"minimum_rounds": 3})
    assert not (tmp_path / "study/result.json").exists()


def test_local_sink_never_constructs_default_cloud_exporter(tmp_path, monkeypatch):
    from agents.tracing.processors import BackendSpanExporter

    def forbidden(*args, **kwargs):
        pytest.fail("Cloud exporter must not be constructed for local tracing")

    monkeypatch.setattr(BackendSpanExporter, "__init__", forbidden)
    processor = configure_local_tracing(tmp_path / "local.jsonl")
    set_trace_processors([])
    processor.shutdown()


def test_queued_knowledge_publication_triggers_planning_before_quiescence(
    tmp_path, sink
):
    async def scenario():
        manager_started = asyncio.Event()
        judge_finished = asyncio.Event()

        def agent(name, responder, output_type=None):
            return Agent(
                name=name,
                instructions=name,
                output_type=output_type,
                model=ScriptedModel(
                    [ModelStep.respond(responder)] * 12, emit_traces=True
                ),
            )

        def advocate(call):
            return message(Proposal(rationale="wait for notes", tasks=()))

        async def judge(call):
            data = payload(call)
            if not data["notes"]:
                await manager_started.wait()
                judge_finished.set()
                return message(
                    JudgeDecision(changes=(), stop=False, reason="no notes yet")
                )
            return message(
                JudgeDecision(
                    changes=(
                        CreateTask(
                            action="create",
                            task=TaskRequest(id="from-notes", question="new notes"),
                        ),
                    ),
                    stop=False,
                    reason="published knowledge",
                )
            )

        async def manager(call):
            manager_started.set()
            await judge_finished.wait()
            # Let the initial planning completion queue ahead of publication,
            # with both events waiting when the coordinator resumes.
            await asyncio.sleep(0)
            return message("published notes")

        def scout(call):
            return message(ScoutReport(summary="advice", sources=(), limitations=()))

        def prepare(state, records, directory, experiments):
            async def collect():
                return Evidence(execution_complete=True, valid=True, observations={})

            return Investigation(
                planner=agent(
                    "Plan",
                    lambda call: message(
                        Plan(
                            question="new notes",
                            change="check",
                            rationale="notes",
                            based_on=(),
                        )
                    ),
                    Plan,
                ),
                executor=agent("Exec", lambda call: message("executed")),
                reviewer=agent(
                    "Review",
                    lambda call: message(
                        Review(
                            interpretation="observed",
                            limitations=(),
                            knowledge_changed=False,
                            goal="not-achieved",
                            continue_research=True,
                            next_question="",
                        )
                    ),
                    Review,
                ),
                collect_evidence=collect,
            )

        return await asyncio.wait_for(
            run_research(
                goal="use latest publication",
                pool=TaskPool([]),
                agents=ResearchAgents(
                    advocates=(
                        agent("A", advocate, Proposal),
                        agent("B", advocate, Proposal),
                    ),
                    judge=agent("Judge", judge, JudgeDecision),
                    scout=agent("Scout", scout, ScoutReport),
                    knowledge_manager=agent("Manager", manager),
                ),
                prepare=prepare,
                output_dir=tmp_path / "study",
                max_rounds=2,
                max_planning_rounds=2,
                max_scout_runs=1,
            ),
            3,
        )

    result = asyncio.run(scenario())
    assert [r.task.id for r in result.records] == ["from-notes"]
    assert (tmp_path / "study/planning-0002-judge.json").exists()


def test_external_client_can_initialize_with_standard_socks_proxy(monkeypatch):
    from openai import AsyncOpenAI

    monkeypatch.setenv("ALL_PROXY", "socks5://proxy.example.invalid:1080")
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)

    async def initialize_only():
        client = AsyncOpenAI(
            api_key="synthetic-test-not-a-credential", max_retries=0, timeout=30
        )
        await client.close()  # No request or network connection is made.

    asyncio.run(initialize_only())
