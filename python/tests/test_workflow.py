import asyncio
import json
from collections import Counter

import pytest
from agents import (
    Agent,
    MaxTurnsExceeded,
    ModelBehaviorError,
    function_tool,
    set_trace_processors,
)
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from research_loop.task_pool import CreateTask, TaskPool, TaskRequest
from research_loop.tracing import configure_local_tracing
from research_loop.workflow import (
    CompletedExecution,
    Evidence,
    FatalResearchError,
    Investigation,
    JudgeDecision,
    Plan,
    Proposal,
    ResearchAgents,
    RecoveryDecision,
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
    review_decisions=None,
    plan_reference=None,
    failure_phase=None,
    failure_error=None,
    hold_independent=False,
    completed_executions=None,
    failure_once=False,
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
        "planning_contexts": [],
        "manager_inputs": [],
        "cancelled": set(),
        "review_inputs": {},
        "collected": Counter(),
    }
    advocates_ready = asyncio.Event()
    plans_ready = asyncio.Event()
    followup_started = asyncio.Event()
    independent_collecting = asyncio.Event()

    failure_counts = Counter()

    def fail(phase, task_id):
        failure_counts[phase, task_id] += 1
        if failure_once and failure_counts[phase, task_id] > 1:
            return
        if task_id == "a" and phase == failure_phase:
            raise failure_error or RuntimeError("synthetic investigation failure")

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
            stats["planning_contexts"].append(payload(call))
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
            data = payload(call)
            stats["manager_inputs"].append(data)
            if manager_failure and stats["calls"]["Manager"] == int(manager_failure):
                raise RuntimeError("synthetic manager failure")
            if overlap:
                # A third investigation must progress while Manager is still active.
                await asyncio.wait_for(followup_started.wait(), 2)
            await asyncio.sleep(0)
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
            fail("plan", task_id)
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
        async def probe() -> str:
            """Return task-private tool feedback."""
            if hold_independent and task_id == "a":
                await independent_collecting.wait()
            fail("execute", task_id)
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
            stats["review_inputs"][task_id] = payload(call)
            fail("review", task_id)
            encoded = json.dumps(call.input)
            assert "PRIVATE-" not in encoded
            assert "function_call_output" not in encoded
            assert call.previous_response_id is None and call.conversation_id is None
            assert len(call.input) >= 1
            review_goal, continue_research = (review_decisions or {}).get(
                task_id, ("achieved" if achieved else "not-achieved", not achieved)
            )
            return message(
                Review(
                    interpretation="reviewed",
                    limitations=("synthetic",),
                    knowledge_changed=True,
                    goal=review_goal,
                    continue_research=continue_research,
                    next_question="next",
                )
            )

        async def collect():
            stats["collected"][task_id] += 1
            fail("collect_evidence", task_id)
            stats["experiments"] += 1
            stats["peak_experiments"] = max(
                stats["peak_experiments"], stats["experiments"]
            )
            try:
                if hold_independent and task_id == "b":
                    independent_collecting.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        stats["cancelled"].add(task_id)
                        raise
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
            completed_execution=(completed_executions or {}).get(task_id),
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
                **{
                    "max_rounds": 3,
                    "max_planning_rounds": 3,
                    "max_scout_runs": 1,
                    **(options or {}),
                },
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


def test_manager_failure_preserves_records_and_publishes_later_batch(tmp_path, sink):
    result, stats = run(tmp_path, manager_failure=True, scout_failure=True)
    assert len(result.records) == 3
    assert len(result.failures) == 1 and result.failures[0].phase == "knowledge"
    assert stats["calls"]["Manager"] >= 2
    assert result.notes
    receipt = json.loads((tmp_path / "study/knowledge-0001-error.json").read_text())
    assert receipt["failed_batch"]
    assert receipt["failure"]["error_type"] == "RuntimeError"
    assert list((tmp_path / "study").glob("r*/record.json"))
    assert (tmp_path / "study/result.json").exists()
    assert any(
        json.loads(line)["data"].get("error") for line in sink.read_text().splitlines()
    )


def test_manager_failed_batch_keeps_previous_notes_and_retries_pending_evidence(
    tmp_path, sink
):
    result, stats = run(
        tmp_path,
        manager_failure=2,
        scout_failure=True,
        options={"investigation_concurrency": 1},
    )
    assert len(stats["manager_inputs"]) == 3
    second, third = stats["manager_inputs"][1:]
    assert second["previous_notes"]
    assert third["previous_notes"] == second["previous_notes"]
    assert second["evidence"][0]["record"]["task"]["id"] == "b"
    assert [e["record"]["task"]["id"] for e in third["evidence"]] == ["b", "auto"]
    assert not result.pending_knowledge
    assert len(result.failures) == 1 and result.failures[0].publication == 2
    assert result.notes.count("Published") == 2


@pytest.mark.parametrize("latest_goal", ["achieved", "not-achieved"])
def test_scoped_achievement_continues_dependent_work(tmp_path, sink, latest_goal):
    result, stats = run(
        tmp_path,
        review_decisions={
            "a": ("achieved", True),
            "auto": (latest_goal, True),
        },
        options={"investigation_concurrency": 1},
    )
    assert [r.task.id for r in result.records] == ["a", "b", "auto"]
    assert result.records[0].review.goal == "achieved"
    assert result.records[0].review.continue_research
    assert stats["calls"]["Plan:auto"] == 1
    assert result.stopped == "round-limit"
    assert result.goal == ("unknown" if latest_goal == "achieved" else "not-achieved")
    saved = json.loads((tmp_path / "study/r0001/record.json").read_text())
    assert saved["review"]["goal"] == "achieved"
    assert saved["review"]["continue_research"]


@pytest.mark.parametrize("goal", ["achieved", "not-achieved"])
def test_task_review_stop_does_not_close_study_admission(tmp_path, sink, goal):
    result, stats = run(
        tmp_path,
        review_decisions={"a": (goal, False)},
        options={"investigation_concurrency": 1},
    )
    assert [r.task.id for r in result.records] == ["a", "b", "auto"]
    assert stats["calls"]["Plan:b"] == stats["calls"]["Plan:auto"] == 1
    assert result.stopped == "round-limit"


def test_stop_confirmation_receives_fresh_whole_study_context(tmp_path, sink):
    seen = []

    async def confirm(candidate, current):
        seen.append((candidate, current))
        return all(t["status"] == "reviewed" for t in current["pool"])

    result, _ = run(
        tmp_path,
        review_decisions={"a": ("achieved", False)},
        options={"confirm_stop": confirm},
    )
    assert [r.task.id for r in result.records] == ["a", "b", "auto"]
    assert result.goal == "achieved" and result.stopped == "goal-achieved"
    assert len(seen) == 1
    assert len(seen[0][0]["context"]["reviewed_records"]) < len(
        seen[0][1]["reviewed_records"]
    )


def test_invalid_evidence_cannot_establish_goal(tmp_path, sink):
    result, _ = run(tmp_path, invalid=True, achieved=True)
    assert result.goal == "unknown" and not result.records
    assert len(result.failures) == 2
    assert all(f.phase == "review" for f in result.failures)
    pool = json.loads((tmp_path / "study/pool.json").read_text())
    assert any(t["status"] == "failed" for t in pool)


def test_unreviewed_plan_reference_rejected_before_execute(tmp_path, sink):
    result, stats = run(tmp_path, plan_reference="invented-record")
    assert not result.records and len(result.failures) == 2
    assert all(f.phase == "plan" for f in result.failures)
    assert stats["calls"]["Exec:a"] == stats["calls"]["Exec:b"] == 0
    assert not list((tmp_path / "study").glob("r*/evidence.json"))


def test_final_only_feedback_is_not_a_valid_evidence_role():
    with pytest.raises(ValueError):
        Evidence(
            execution_complete=True, valid=True, feedback="final-only", observations={}
        )


def test_minimum_reviewed_rounds_enforced(tmp_path, sink):
    result, _ = run(tmp_path, achieved=True, options={"minimum_rounds": 3})
    assert not result.records and result.goal == "unknown"
    assert all("minimum rounds" in f.error_message for f in result.failures)


@pytest.mark.parametrize("phase", ["plan", "execute", "collect_evidence", "review"])
def test_investigation_failure_isolated_without_replay_or_dependency_release(
    tmp_path, sink, phase
):
    result, stats = run(
        tmp_path, failure_phase=phase, options={"max_planning_rounds": 8}
    )
    assert [r.task.id for r in result.records] == ["b"]
    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.phase == phase and failure.task_id == "a"
    assert failure.record_id == "r0001" and failure.error_message
    assert json.loads((tmp_path / "study/r0001/error.json").read_text()) == (
        failure.model_dump(mode="json")
    )
    states = {t["request"]["id"]: t for t in result.pool}
    assert states["a"]["status"] == "failed" and states["a"]["record_id"] is None
    assert states["auto"]["status"] == "pending"
    assert result.stopped == "pool-blocked"
    assert stats["calls"]["Plan:a"] == 1
    if phase == "execute":
        assert stats["calls"]["Exec:a"] == 1
    assert stats["calls"]["Plan:auto"] == 0
    assert any(
        any(f["task_id"] == "a" for f in context["failures"])
        for context in stats["planning_contexts"]
    )


def test_wrapped_fatal_tool_error_cancels_other_work(tmp_path, sink):
    async def scenario():
        agents, prepare, stats = program(
            failure_phase="execute",
            failure_error=FatalResearchError("synthetic unknown resource state"),
            hold_independent=True,
        )
        with pytest.raises(Exception) as caught:
            await asyncio.wait_for(
                run_research(
                    goal="test",
                    pool=TaskPool(
                        [
                            TaskRequest(id="a", question="first"),
                            TaskRequest(id="b", question="second"),
                        ]
                    ),
                    agents=agents,
                    prepare=prepare,
                    output_dir=tmp_path / "study",
                ),
                3,
            )
        from research_loop.workflow import is_fatal_error

        assert is_fatal_error(caught.value)
        assert stats["cancelled"] == {"b"}

    asyncio.run(scenario())
    assert not (tmp_path / "study/result.json").exists()


@pytest.mark.parametrize("error_type", [ModelBehaviorError, MaxTurnsExceeded])
def test_ordinary_sdk_error_is_isolated(tmp_path, sink, error_type):
    result, _ = run(
        tmp_path,
        failure_phase="plan",
        failure_error=error_type("synthetic bounded SDK failure"),
    )
    assert [r.task.id for r in result.records] == ["b"]
    assert result.failures[0].error_type == error_type.__name__


def test_cancellation_propagates_instead_of_becoming_task_failure(tmp_path, sink):
    with pytest.raises(asyncio.CancelledError):
        run(
            tmp_path,
            failure_phase="plan",
            failure_error=asyncio.CancelledError("synthetic operator stop"),
        )
    assert not (tmp_path / "study/result.json").exists()


def test_fatal_advocate_wakes_scheduler_before_blocked_peer_finishes(tmp_path, sink):
    async def scenario():
        agents, prepare, _ = program(scout_failure=True)
        peer_started = asyncio.Event()
        peer_cancelled = asyncio.Event()

        async def fatal(call):
            await peer_started.wait()
            raise FatalResearchError("synthetic unsafe global state")

        async def blocked(call):
            peer_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                peer_cancelled.set()

        agents.advocates = tuple(
            Agent(
                name=name,
                instructions=name,
                output_type=Proposal,
                model=ScriptedModel([ModelStep.respond(responder)]),
            )
            for name, responder in (("Fatal", fatal), ("Blocked", blocked))
        )
        with pytest.raises(FatalResearchError, match="unsafe global"):
            await asyncio.wait_for(
                run_research(
                    goal="test",
                    pool=TaskPool([]),
                    agents=agents,
                    prepare=prepare,
                    output_dir=tmp_path / "study",
                ),
                2,
            )
        assert peer_cancelled.is_set()

    asyncio.run(scenario())


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


def retained_execution(*, complete=True, valid=False, based_on=()):
    return CompletedExecution(
        plan=Plan(
            question="retained question",
            change="retained change",
            rationale="retained rationale",
            based_on=based_on,
        ),
        execute_summary="Retained public execution summary",
        evidence=Evidence(
            execution_complete=complete,
            valid=valid,
            observations={"measurement": [3, 7], "origin": "synthetic host"},
        ),
    )


def test_retained_execution_runs_only_fresh_review_and_copies_exact_inputs(
    tmp_path, sink
):
    retained = retained_execution(valid=True)
    result, stats = run(tmp_path, completed_executions={"a": retained})
    record = next(r for r in result.records if r.task.id == "a")
    assert stats["calls"]["Plan:a"] == stats["calls"]["Exec:a"] == 0
    assert stats["collected"]["a"] == 0
    assert stats["calls"]["Review:a"] == 1
    assert record.plan == retained.plan
    assert record.execute == retained.execute_summary
    assert record.evidence == retained.evidence
    data = stats["review_inputs"]["a"]
    assert data["frozen_plan"] == retained.plan.model_dump(mode="json")
    assert data["execute_summary"] == retained.execute_summary
    assert data["trusted_evidence"] == retained.evidence.model_dump(mode="json")
    assert data["goal"] == "test" and data["task"]["request"]["id"] == "a"
    directory = tmp_path / "study" / record.id
    assert json.loads((directory / "plan.json").read_text()) == data["frozen_plan"]
    assert (
        json.loads((directory / "evidence.json").read_text())
        == data["trusted_evidence"]
    )
    assert json.loads((directory / "completed-execution.json").read_text()) == (
        retained.model_dump(mode="json")
    )
    assert retained.evidence.valid is True


@pytest.mark.parametrize(
    "retained,error",
    [
        (retained_execution(complete=False), "must be complete"),
        (retained_execution(based_on=("old-unreviewed",)), "has not been reviewed"),
    ],
)
def test_retained_execution_rejects_incomplete_or_unreviewed_inputs(
    tmp_path, sink, retained, error
):
    result, stats = run(tmp_path, completed_executions={"a": retained})
    assert [r.task.id for r in result.records] == ["b"]
    assert result.failures[0].phase == "completed_execution"
    assert error in result.failures[0].error_message
    assert stats["calls"]["Plan:a"] == stats["calls"]["Exec:a"] == 0
    assert stats["calls"]["Review:a"] == stats["collected"]["a"] == 0


def test_retained_review_failure_leaves_independent_work_running(tmp_path, sink):
    retained = retained_execution(valid=True)
    result, stats = run(
        tmp_path, completed_executions={"a": retained}, failure_phase="review"
    )
    assert [r.task.id for r in result.records] == ["b"]
    assert result.failures[0].phase == "review"
    assert stats["calls"]["Review:a"] == 1
    assert stats["calls"]["Plan:a"] == stats["calls"]["Exec:a"] == 0
    assert stats["collected"]["a"] == 0
    states = {t["request"]["id"]: t["status"] for t in result.pool}
    assert states["a"] == "failed" and states["auto"] == "pending"
    assert json.loads(
        (tmp_path / "study/r0001/completed-execution.json").read_text()
    ) == (retained.model_dump(mode="json"))


def test_application_input_boundary_runs_once_per_role_not_tool_turn(tmp_path, sink):
    seen = Counter()

    def prepare_input(agent, data):
        seen[agent.name] += 1
        return {
            **data,
            "goal": "application-owned reference",
            "input_reference": f"knowledge:{agent.name}",
        }

    result, stats = run(tmp_path, options={"prepare_input": prepare_input})
    assert len(result.records) == 3 and not result.failures
    assert {
        "A",
        "B",
        "Judge",
        "Scout",
        "Manager",
        "Plan:a",
        "Exec:a",
        "Review:a",
    } <= seen.keys()
    assert seen["Exec:a"] == 1
    assert stats["calls"]["Exec:a"] == 2
    assert all(
        x["input_reference"].startswith("knowledge:")
        for x in stats["planning_contexts"]
    )
    assert stats["review_inputs"]["a"]["goal"] == "application-owned reference"


def test_input_preparation_failure_isolated_before_model_dispatch(tmp_path, sink):
    def prepare_input(agent, data):
        if agent.name == "Plan:a":
            raise ValueError("knowledge publication failed")
        return data

    result, stats = run(tmp_path, options={"prepare_input": prepare_input})
    assert stats["calls"]["Plan:a"] == stats["calls"]["Exec:a"] == 0
    assert any(f.task_id == "a" and f.phase == "plan" for f in result.failures)
    assert any(r.task.id == "b" for r in result.records)


def test_application_publisher_exclusively_owns_global_knowledge(tmp_path, sink):
    target = tmp_path / "global-index.md"
    target.write_text("initial global index")
    outputs = []

    def publish(content):
        outputs.append(content)
        target.write_text("application whole-tree publication")

    result, stats = run(
        tmp_path, options={"knowledge_path": target, "publish_knowledge": publish}
    )
    assert len(result.records) == 3 and not result.failures
    assert (
        outputs
        and stats["manager_inputs"][0]["previous_notes"] == "initial global index"
    )
    assert target.read_text() == "application whole-tree publication"
    assert result.notes == "application whole-tree publication"
    assert not (tmp_path / "study/research.md").exists()
    assert list((tmp_path / "study").glob("knowledge-*-output.md"))


def test_failed_application_publication_preserves_prior_global_notes(tmp_path, sink):
    target = tmp_path / "global-index.md"
    target.write_text("old published tree")

    def fail_publication(content):
        raise ValueError("activation failed; evidence pending")

    result, stats = run(
        tmp_path,
        options={"knowledge_path": target, "publish_knowledge": fail_publication},
    )
    assert len(result.records) == 3
    assert any(f.phase == "knowledge" for f in result.failures)
    assert target.read_text() == "old published tree"
    assert all(
        x["previous_notes"] == "old published tree" for x in stats["manager_inputs"]
    )


def test_unlimited_limits_run_four_tasks_without_demo_stop(tmp_path, sink):
    async def scenario():
        agents, prepare, stats = program()
        result = await run_research(
            goal="test",
            pool=TaskPool(
                [TaskRequest(id=x, question=x) for x in ("a", "b", "c", "d")]
            ),
            agents=agents,
            prepare=prepare,
            output_dir=tmp_path / "study",
            max_rounds=None,
            max_planning_rounds=None,
            max_scout_runs=1,
        )
        assert {r.task.id for r in result.records} == {"a", "b", "c", "d", "auto"}
        assert result.stopped == "pool-empty"
        return stats

    asyncio.run(scenario())


def test_review_recovery_does_not_repeat_execution_or_subprocess(tmp_path, sink):
    async def scenario():
        agents, prepare, stats = program(failure_phase="review", failure_once=True)
        calls = []

        def prepare_real(*args):
            setup = prepare(*args)
            original = setup.collect_evidence

            async def collect():
                import sys

                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-c", "print(3 + 4)", stdout=asyncio.subprocess.PIPE
                )
                stdout, _ = await process.communicate()
                assert process.returncode == 0 and stdout.strip() == b"7"
                calls.append(args[0].request.id)
                return await original()

            setup.collect_evidence = collect
            return setup

        async def recover(failure):
            assert failure.phase == "review"
            assert failure.task.request.id == "a"
            return RecoveryDecision(
                "retry",
                "Fresh transport boundary after transient failure",
                "transport-observed-recovered",
            )

        result = await run_research(
            goal="test",
            pool=TaskPool([TaskRequest(id="a", question="a")]),
            agents=agents,
            prepare=prepare_real,
            output_dir=tmp_path / "study",
            recover_stage=recover,
            max_rounds=2,
            max_scout_runs=1,
        )
        assert not result.failures
        assert calls.count("a") == stats["collected"]["a"] == 1
        assert stats["calls"]["Review:a"] == 2
        assert stats["calls"]["Exec:a"] == 2  # One tool call + final answer.

    asyncio.run(scenario())


def test_repeated_identical_diagnosis_blocks_without_busy_loop(tmp_path, sink):
    diagnoses = []

    async def recover(failure):
        diagnoses.append(failure)
        return RecoveryDecision("retry", "Test repaired environment", "same-condition")

    result, stats = run(
        tmp_path, failure_phase="plan", options={"recover_stage": recover}
    )
    assert len(diagnoses) == 2
    assert stats["calls"]["Plan:a"] == 2
    state = next(t for t in result.pool if t["request"]["id"] == "a")
    assert state["status"] == "blocked" and state["wakeup"]
    assert any(r.task.id == "b" for r in result.records)


def test_exact_reference_resolver_accepts_alias_and_rejects_false_source(
    tmp_path, sink
):
    seen = []

    def validate(refs, records):
        seen.append(refs)
        if refs != ("study:test/record:reviewed",):
            raise ValueError("Exact source is not reviewed")

    result, _ = run(
        tmp_path,
        plan_reference="study:test/record:reviewed",
        options={"validate_evidence_refs": validate},
    )
    assert len(result.records) == 3 and seen
    result2, _ = run(
        tmp_path / "other",
        plan_reference="fake:reviewed",
        options={"validate_evidence_refs": validate},
    )
    assert not result2.records and all(f.phase == "plan" for f in result2.failures)


def test_missing_delivery_stays_blocked_and_does_not_unlock_dependency(tmp_path, sink):
    result, stats = run(
        tmp_path, options={"assess_completion": lambda r: r.task.id != "a"}
    )
    assert [r.task.id for r in result.records] == ["b"]
    state = next(t for t in result.pool if t["request"]["id"] == "a")
    assert state["status"] == "blocked"
    assert stats["calls"]["Plan:auto"] == 0
    assert (tmp_path / "study/r0001/incomplete-record.json").exists()
    assert result.failures[0].phase == "delivery"


def test_delivery_correction_reuses_task_and_frozen_plan(tmp_path, sink):
    ready = set()

    async def recover(failure):
        assert failure.phase == "delivery"
        assert failure.output.task.id == "a"
        ready.add("a")
        return RecoveryDecision(
            "retry",
            "Implement the missing numeric output",
            "new-measurement-implementation",
            restart_from="execute",
        )

    result, stats = run(
        tmp_path,
        options={
            "assess_completion": lambda r: r.task.id != "a" or "a" in ready,
            "recover_stage": recover,
        },
    )
    assert len(result.records) == 3 and not result.failures
    assert stats["calls"]["Plan:a"] == 1
    assert stats["calls"]["Exec:a"] == 4
    assert stats["collected"]["a"] == 2
    assert (tmp_path / "study/r1-repair-1/record.json").exists()


def test_safe_handoff_retains_pending_work_without_dispatch(tmp_path, sink):
    result, stats = run(tmp_path, options={"on_quiescent": lambda context: True})
    assert result.stopped == "handoff" and not result.records
    assert all(t["status"] == "pending" for t in result.pool)
    assert not stats["calls"]
    restored = TaskPool.from_snapshot(result.pool)
    assert [t.request.id for t in restored.ready()] == ["a", "b"]


def test_sdk_max_turns_continues_confirmed_tool_history_without_replay(tmp_path, sink):
    histories = []

    async def recover(failure):
        assert failure.phase == "execute"
        assert isinstance(failure.error, MaxTurnsExceeded)
        assert failure.run_data is not None
        items = [item.to_input_item() for item in failure.run_data.new_items]
        assert any(i.get("type") == "function_call_output" for i in items)
        histories.append(items)
        return RecoveryDecision(
            "retry",
            "Continue after the newly confirmed tool result",
            "confirmed-output:" + failure.task.request.id,
        )

    result, stats = run(tmp_path, options={"max_turns": 1, "recover_stage": recover})
    assert len(result.records) == 3 and not result.failures
    assert len(histories) == 3
    assert all(stats["calls"]["Exec:" + task] == 2 for task in ("a", "b", "auto"))
    assert all(stats["collected"][task] == 1 for task in ("a", "b", "auto"))


def test_explicit_stage_handoff_resumes_execute_after_confirmed_tool(tmp_path, sink):
    plan = retained_execution().plan
    history = [
        {"role": "user", "content": "Continue the exact frozen task"},
        {
            "type": "function_call",
            "call_id": "confirmed-a",
            "name": "probe",
            "arguments": "{}",
        },
        {
            "type": "function_call_output",
            "call_id": "confirmed-a",
            "output": "PRIVATE-a",
        },
    ]
    result, stats = run(
        tmp_path,
        options={
            "initial_stage_inputs": {
                "a": {
                    "phase": "execute",
                    "input": history,
                    "plan": plan.model_dump(mode="json"),
                },
            }
        },
    )
    assert len(result.records) == 3 and not result.failures
    assert stats["calls"]["Plan:a"] == 0
    assert stats["calls"]["Exec:a"] == 1
    assert stats["collected"]["a"] == 1
    assert next(r for r in result.records if r.task.id == "a").plan == plan


def test_manager_diagnosed_retry_publishes_pending_batch_once(tmp_path, sink):
    repairs = []

    async def recover(failure):
        assert failure.phase == "knowledge"
        repairs.append(failure)
        return RecoveryDecision(
            "retry", "Repair publication environment", "verified-publication-repair"
        )

    result, _ = run(tmp_path, manager_failure=True, options={"recover_stage": recover})
    assert len(repairs) == 1
    assert len(result.records) == 3 and not result.failures
    assert not result.pending_knowledge


def test_unlimited_idle_wait_reopens_planning_only_after_event(tmp_path, sink):
    async def scenario():
        agents, prepare, _ = program()
        pool = TaskPool([TaskRequest(id="a", question="first")])
        observations = []

        async def idle(context):
            observations.append(len(context["reviewed_records"]))
            if len(observations) == 1:
                # The real application awaits an inbox/resource event here.
                await asyncio.sleep(0)
                pool.add_human(TaskRequest(id="b", question="new event"))
                return True
            return False

        result = await run_research(
            goal="test",
            pool=pool,
            agents=agents,
            prepare=prepare,
            output_dir=tmp_path / "study",
            max_rounds=None,
            max_planning_rounds=None,
            max_scout_runs=1,
            idle_wait=idle,
        )
        assert [r.task.id for r in result.records] == ["a", "auto", "b"]
        assert observations == [2, 3]

    asyncio.run(scenario())


def test_host_invalid_evidence_does_not_complete_negative_review(tmp_path, sink):
    result, stats = run(tmp_path, invalid=True)
    assert not result.records
    assert all(f.phase == "delivery" for f in result.failures)
    assert stats["calls"]["Plan:auto"] == 0
    assert all(t["status"] != "reviewed" for t in result.pool)


def test_invalid_retained_execution_is_reviewed_but_remains_incomplete(tmp_path, sink):
    retained = retained_execution(valid=False)
    result, stats = run(tmp_path, completed_executions={"a": retained})
    assert [r.task.id for r in result.records] == ["b"]
    assert stats["calls"]["Plan:a"] == stats["calls"]["Exec:a"] == 0
    assert stats["calls"]["Review:a"] == 1
    assert stats["calls"]["Plan:auto"] == 0
    assert (
        next(t for t in result.pool if t["request"]["id"] == "a")["status"] == "blocked"
    )


def test_drain_latches_before_new_admissions_and_seals_after_manager(tmp_path, sink):
    requested = False
    quiescent = []
    late_admissions = []

    def observe(agent, data):
        if requested and agent.name in {"A", "B", "Scout"}:
            late_admissions.append(agent.name)
        return data

    def completion(record):
        nonlocal requested
        if record.task.id == "a":
            requested = True
        return True

    def seal(context):
        quiescent.append(context)
        return context["draining"]

    result, stats = run(
        tmp_path,
        options={
            "investigation_concurrency": 1,
            "assess_completion": completion,
            "should_drain": lambda context: requested,
            "on_quiescent": seal,
            "prepare_input": observe,
            "max_rounds": None,
            "max_planning_rounds": None,
            "max_scout_runs": None,
        },
    )
    assert result.stopped == "handoff"
    assert [r.task.id for r in result.records] == ["a"]
    assert stats["calls"]["Plan:b"] == stats["calls"]["Plan:auto"] == 0
    assert not late_admissions
    assert not result.pending_knowledge
    assert quiescent[-1]["draining"]
    assert quiescent[-1]["pending_knowledge"] == []


def test_pending_knowledge_prevents_study_stop_confirmation(tmp_path, sink):
    confirmations = []

    def fail_publication(content):
        raise ValueError("Publication pending external repair")

    async def confirm(candidate, context):
        confirmations.append(context)
        return True

    result, _ = run(
        tmp_path,
        review_decisions={"a": ("achieved", False)},
        options={
            "publish_knowledge": fail_publication,
            "confirm_stop": confirm,
        },
    )
    assert result.pending_knowledge
    assert not confirmations
    assert result.stopped != "goal-achieved"


def test_plan_freezes_generic_delivery_contract():
    plan = Plan(
        question="question",
        change="measure",
        rationale="test",
        based_on=(),
        execution_kind="measurement",
        required_artifacts=("metrics.json",),
    )
    assert plan.execution_kind == "measurement"
    assert plan.required_artifacts == ("metrics.json",)
    assert Plan.model_validate(plan.model_dump()).required_artifacts == (
        "metrics.json",
    )


def test_native_invalid_plan_json_preserves_history_and_corrects_same_stage(
    tmp_path, sink
):
    async def scenario():
        agents, prepare, stats = program()
        reads = []
        corrections = []
        invalid_output = "THIS IS NOT PLAN JSON"

        def prepare_native(state, records, directory, experiments):
            setup = prepare(state, records, directory, experiments)
            if state.request.id != "a":
                return setup

            @function_tool
            async def read_contract() -> str:
                """Read the frozen synthetic contract."""
                reads.append("confirmed-contract-read")
                return "Preserve the frozen synthetic question"

            def corrected(call):
                encoded = json.dumps(call.input)
                assert invalid_output in encoded
                assert "stage_error" in encoded
                assert [
                    i["output"]
                    for i in call.input
                    if i.get("type") == "function_call_output"
                ] == ["Preserve the frozen synthetic question"]
                return message(
                    Plan(
                        question=state.request.question,
                        change="corrected change",
                        rationale="correct invalid JSON without repeating effects",
                        based_on=tuple(r.id for r in records),
                    )
                )

            setup.planner.tools = [read_contract]
            setup.planner.model = ScriptedModel(
                [
                    ModelStep.respond(
                        lambda call: [
                            function_call("read_contract", {}, call_id="contract-a")
                        ]
                    ),
                    ModelStep.respond(lambda call: [assistant_message(invalid_output)]),
                    ModelStep.respond(corrected),
                ],
                emit_traces=True,
            )
            return setup

        async def recover(failure):
            assert failure.phase == "plan"
            assert isinstance(failure.error, ModelBehaviorError)
            assert failure.run_data is not None
            assert invalid_output in json.dumps(failure.run_data.history)
            assert invalid_output in json.dumps(failure.output)
            corrections.append(failure)
            return RecoveryDecision(
                "retry",
                "Correct JSON using the exact malformed output",
                "invalid-json-feedback",
            )

        result = await run_research(
            goal="test",
            pool=TaskPool([TaskRequest(id="a", question="a")]),
            agents=agents,
            prepare=prepare_native,
            output_dir=tmp_path / "study",
            recover_stage=recover,
            max_rounds=2,
            max_scout_runs=1,
        )
        assert len(result.records) == 2 and not result.failures
        assert len(corrections) == len(reads) == 1
        assert stats["calls"]["Exec:a"] == 2
        assert stats["collected"]["a"] == stats["calls"]["Review:a"] == 1

    asyncio.run(scenario())
