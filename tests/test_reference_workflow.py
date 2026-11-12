"""The reference research plugin on real pinned Runs and local command services."""

import asyncio
import json
import shlex
import sys
from pathlib import Path

import pytest

from research_loop.codex import CodexConfig
from research_loop.configuration import prepare_configuration
from research_loop.group_agents import NativeGroupServices
from research_loop.groups import GroupRuntime
from research_loop.state import Journal, PublicationStore
from research_workflows.research import create


SOURCE = Path(__file__).resolve().parents[1] / "examples/research.yaml"


def test_research_plugin_executes_once_and_proposes_reviewed_publication(
    tmp_path, monkeypatch
):
    config = prepare_configuration(SOURCE, tmp_path / "declarations", revision="test")
    config["project"] = str(tmp_path)
    (tmp_path / "goal.md").write_text("One bounded synthetic CPU check: 6 * 7.")
    PublicationStore(tmp_path).prepare()
    (tmp_path / "outputs/current/group-artifacts.md").write_text(
        "- [Previous negative evidence](groups/previous/report.json)\n"
    )
    request = {
        "id": "synthetic-priority",
        "text": "Compute 6 * 7 once, analyze it and stop.",
    }
    workflow = create(config["workflow"]["settings"])
    context = workflow.context(tmp_path, config, 1, None, request)
    calls = []

    class Native:
        def __init__(self, config, journal):
            pass

        async def open(self):
            pass

        async def verify(self, *args, **kwargs):
            pass

        async def stop(self):
            pass

        async def settle(self):
            pass

        async def close(self):
            pass

        async def run(self, instructions, payload, schema, **kwargs):
            work, assignment = Path(payload["work"]), payload["assignment"]
            thread = kwargs["thread_id"] or f"thread-{len(calls)}"
            kwargs["on_started"](thread)
            calls.append((thread, payload, instructions))
            base = {
                "summary": "Authored bounded assessment",
                "status": "ready",
                "help": "No blocker; retain the stated limitations.",
                "artifacts": [],
            }
            fields = schema["properties"]
            if "experiment_task_ref" in fields:
                task = dict(
                    question="6 * 7?",
                    hypothesis="42",
                    controls=["Exact integers"],
                    permitted_changes=["Synthetic script"],
                    data_roles={"synthetic": "test"},
                    evaluation={"expected": 42},
                    inputs=[],
                    constraints=["CPU once"],
                    completion_criteria=["Independent evaluation"],
                )
                frontier = {
                    "hypotheses": [
                        dict(
                            id="integer-1",
                            evidence=[],
                            cost="One CPU command",
                            status="ready",
                            disposition="selected",
                        )
                    ]
                }
                (work / "task.json").write_text(json.dumps(task))
                (work / "frontier-source.json").write_text(json.dumps(frontier))
                return dict(
                    base,
                    disposition="experiment",
                    experiment_task_ref="task.json",
                    frontier_ref="frontier-source.json",
                    consequential_claim=False,
                )
            if "plan_artifact" in fields:
                (work / "scientific-plan.md").write_text(
                    "Compute once, preserve exact integer arithmetic, independently evaluate the observed result."
                )
                return dict(
                    base,
                    command="",
                    plan_artifact="scientific-plan.md",
                    artifacts=["scientific-plan.md"],
                )
            if "result_file" in fields:
                (work / "experiment.py").write_text(
                    'from pathlib import Path\nPath("result.json").write_text(str(6 * 7))\nPath("dispatch-count").write_text("once")\n'
                )
                return dict(
                    base,
                    command=f"{shlex.quote(sys.executable)} experiment.py",
                    result_file="result.json",
                    artifacts=["experiment.py"],
                )
            if "claim" in fields:
                assert (work / "result.json").read_text() == "42"
                return dict(
                    base,
                    command="",
                    claim="Observed 42",
                    limitations=["Synthetic"],
                    evidence=["result.json"],
                )
            if "study_result" in fields:
                receipts = [
                    r
                    for r in assignment["input_refs"]
                    if Path(r).name == "execution.json"
                ]
                evaluations = [
                    r
                    for r in assignment["input_refs"]
                    if Path(r).name == "evaluate.json"
                ]
                result = dict(
                    summary="Observed 42",
                    goal="achieved",
                    evidence=receipts + evaluations,
                    limitations=["Synthetic CPU only"],
                    next_action="",
                    study_action="stop",
                    stop_reason="Bounded goal satisfied",
                    human_request=dict(
                        id=request["id"],
                        status="completed",
                        evidence=receipts + evaluations,
                        reason="Actual execution and independent evaluation completed",
                    ),
                )
                return dict(
                    base,
                    study_result=result,
                    knowledge_files=[
                        dict(
                            source="knowledge/index.md",
                            target="index.md",
                            content="# Findings\n\n[Result](finding.md)\n",
                        ),
                        dict(
                            source="knowledge/finding.md",
                            target="finding.md",
                            content="Observed 42 in actual synthetic evidence.\n",
                        ),
                    ],
                    execution_refs=receipts,
                    analysis_refs=evaluations,
                    direction_refs=[],
                    stop_basis="goal-satisfied",
                )
            assert "verdict" in fields
            return dict(
                summary="Reviewed the candidate",
                verdict="accept",
                reason="The cited execution and independent evaluation support the bounded claim",
                artifacts=[],
            )

    monkeypatch.setattr(
        "research_loop.group_agents.configuration",
        lambda *args, **kwargs: (CodexConfig(cwd=str(tmp_path)), {}),
    )
    journal = Journal(tmp_path / "events.jsonl")
    runtime = GroupRuntime(
        tmp_path,
        config["groups"],
        service_factory=lambda ctx: NativeGroupServices(
            ctx, config, journal, native_factory=Native
        ),
        revision=config["revision"],
    )
    admitted = []

    def view():
        return dict(
            root=tmp_path,
            config=config,
            iteration=1,
            runs=runtime.observe()["runs"],
            admitted=admitted,
            input=context,
        )

    async def execute(group, task, refs):
        identity = await runtime.start(group, task, refs, context=context)
        admitted.append(identity)
        assert workflow.advance(view()) == {"action": "wait"}
        await runtime.wait()
        row = runtime.observe(identity)
        assert row["status"] == "returned", row
        assert workflow.advance(view()) == {"action": "close", "run_id": identity}
        runtime.close(identity)
        return row["output"]["artifacts"]

    async def scenario():
        direction = await execute("direction", str(tmp_path / "goal.md"), [])
        task = next(
            ref for ref in direction if Path(ref).name == "experiment-task.json"
        )
        experiment = await execute("exp", task, direction)
        report = next(ref for ref in experiment if Path(ref).name == "report.json")
        await execute("synthesis", report, direction + experiment)
        directive = workflow.advance(view())
        assert directive["action"] == "finish_round"
        proposal = workflow.complete(view(), directive["result_ref"])
        await runtime.close_runtime()
        return proposal

    proposal = asyncio.run(scenario())
    journal.close()
    assert proposal["action"] == "stop"
    assert proposal["input_outcome"] == {"id": request["id"], "status": "completed"}
    assert proposal["result"]["goal"] == "achieved"
    assert len(calls) == 6
    assert calls[1][0] == calls[2][0] != calls[3][0]
    assert calls[4][0] != calls[5][0]
    exp_run = next(row for row in runtime.observe()["runs"] if row["group_id"] == "exp")
    assert (
        tmp_path / "group-runs" / exp_run["run_id"] / "work/dispatch-count"
    ).read_text() == "once"
    records = tmp_path / "group-runs" / exp_run["run_id"] / "records"
    steps = {
        row["label"]: row["id"]
        for path in (records / "step").glob("*.json")
        if (row := json.loads(path.read_text()))["status"] == "completed"
    }
    handoffs = [
        json.loads(path.read_text()) for path in (records / "handoff").glob("*.json")
    ]
    pairs = {(row["sender"], row["receiver"]) for row in handoffs}
    assert {
        (steps["exp.plan"], steps["exp.prepare"]),
        (steps["exp.prepare"], steps["exp.execute"]),
        (steps["exp.execute"], steps["exp.evaluate"]),
    } <= pairs
    assert all(row["inputs"] for row in handoffs)
    agent_calls = [
        json.loads(path.read_text()) for path in (records / "call").glob("*.json")
    ]
    assert {row["parent_step_id"] for row in agent_calls} == {
        steps["exp.plan"],
        steps["exp.prepare"],
        steps["exp.evaluate"],
    }

    phases = json.loads(
        (tmp_path / "group-runs" / exp_run["run_id"] / "state.json").read_text()
    )["experiment_phases"]
    assert all(
        phases[name]["status"] == "ready" and phases[name]["help"]
        for name in ("plan", "prepare", "evaluate")
    )
    assert not (tmp_path / "outputs/current/finding.md").exists()
    proposed = {entry["destination"]: entry for entry in proposal["artifacts"]}
    assert (
        "[Group artifact index](group-artifacts.md)" in proposed["index.md"]["content"]
    )
    assert Path(proposed["finding.md"]["source"]).read_text().startswith("Observed 42")


def test_research_completion_rejects_blocked_and_unreviewed_candidates(tmp_path):
    directory = tmp_path / "group-runs/r/rounds/000001"
    directory.mkdir(parents=True)
    final, candidate = tmp_path / "final.json", tmp_path / "candidate.json"
    final.write_text("{}")
    candidate.write_text("{}")
    (directory / "artifacts.json").write_text(
        json.dumps(
            {
                "study-result.json": str(final),
                "reports/study-result-candidate.json": str(candidate),
            }
        )
    )
    workflow = create({})
    row = dict(
        run_id="r",
        group_id="synthesis",
        round=1,
        status="closed",
        output=dict(
            completion="blocked",
            help="Revise",
            unfinished=["review"],
            artifacts=[str(final), str(candidate)],
        ),
    )
    view = dict(root=tmp_path, runs=[row], admitted=["r"], input={"input": {}})
    with pytest.raises(ValueError, match="Blocked synthesis"):
        workflow.complete(view, str(candidate))
    row["output"].update(completion="completed", help="", unfinished=[])
    with pytest.raises(ValueError, match="reviewed final"):
        workflow.complete(view, str(candidate))
