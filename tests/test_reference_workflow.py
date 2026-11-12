"""Complete reference workflow through the real controller, with no model calls."""

import asyncio
import json
import re
import shlex
import sys
from pathlib import Path

import pytest

from research_loop import group_router, human_input, loop
from research_loop.group_programs import exp
from research_loop.codex import CodexConfig
from test_group_router import decision


def test_default_research_workflow_completes_and_installs_scientific_knowledge(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    goal = tmp_path / "goal.md"
    goal.write_text("One bounded synthetic CPU experiment only: compute 6 * 7.")
    root = tmp_path / "study"
    loop.initialize(root, project, goal)
    config = loop.read_config(root)
    assert config["coordinator"] == "router"
    assert config["study_result_group"] == "synthesis"
    assert next(g for g in config["groups"] if g["id"] == "direction")["version"] == "2"
    assert {g["id"] for g in config["groups"]} == {
        "direction",
        "exp",
        "synthesis",
        "diagnosis",
    }
    human_input.submit(root, "Compute 6 * 7 once, analyze the actual result and stop.")
    calls = []
    threads = 0

    class Native:
        def __init__(self, config, journal, tools=None):
            self.router = tools is not None

        async def open(self):
            pass

        async def verify(self, *a, **kw):
            pass

        async def stop(self):
            pass

        async def settle(self):
            pass

        async def close(self):
            pass

        async def run(self, instructions, payload, schema, **kwargs):
            nonlocal threads
            threads += 1
            thread = kwargs.get("thread_id") or f"thread-{threads}"
            if kwargs.get("on_started"):
                kwargs["on_started"](thread)
            if self.router:
                rows = {r["group_id"]: r for r in payload["runs"]}
                if any(r["status"] == "running" for r in rows.values()):
                    return decision("wait")
                for row in rows.values():
                    if row["status"] == "returned":
                        return decision("close", run_id=row["id"])
                if not rows:
                    return decision(
                        "start", group_id="direction", task_ref=payload["goal_ref"]
                    )

                def ref(group, basename):
                    return next(
                        r
                        for r in rows[group]["output"]["artifacts"]
                        if Path(payload["references"][r]).name == basename
                    )

                if "exp" not in rows:
                    return decision(
                        "start",
                        group_id="exp",
                        task_ref=ref("direction", "experiment-task.json"),
                        input_refs=rows["direction"]["output"]["artifacts"],
                    )
                if "synthesis" not in rows:
                    return decision(
                        "start",
                        group_id="synthesis",
                        task_ref=ref("exp", "report.json"),
                        input_refs=[
                            r
                            for row in rows.values()
                            for r in row["output"]["artifacts"]
                        ],
                    )
                return decision(
                    "finish_round", result_ref=ref("synthesis", "study-result.json")
                )
            role = payload["role"]
            assignment = payload["assignment"]
            work = Path(payload["work"])
            calls.append((role, thread, instructions, payload))
            base = dict(summary="Authored bounded assessment", help="", artifacts=[])
            if payload["instructions"] == exp.PLAN_INSTRUCTIONS:
                (work / "scientific-plan.md").write_text(
                    "Exact integer arithmetic, one execution, independent evaluation."
                )
                return dict(
                    base,
                    command="",
                    plan_artifact="scientific-plan.md",
                    artifacts=["scientific-plan.md"],
                )
            if payload["instructions"] == exp.PREPARE_INSTRUCTIONS:
                (work / "experiment.py").write_text(
                    'from pathlib import Path\nPath("result.json").write_text(str(6*7))\n'
                )
                return dict(
                    base,
                    command=f"{shlex.quote(sys.executable)} experiment.py",
                    result_file="result.json",
                    artifacts=["experiment.py"],
                )
            if payload["instructions"] == exp.EVALUATE_INSTRUCTIONS:
                assert json.loads((work / "result.json").read_text()) == 42
                return dict(
                    base,
                    command="",
                    claim="Observed 42",
                    limitations=["Synthetic"],
                    evidence=["result.json"],
                )
            if role == "proposer":
                published = Path(
                    payload["study_context"]["input"]["published_knowledge_root"]
                )
                if (published / "group-artifacts.md").exists():
                    assert (
                        "[Group artifact index](group-artifacts.md)"
                        in (published / "index.md").read_text()
                    )
                    links = re.findall(
                        r"\]\(([^)]+)\)", (published / "group-artifacts.md").read_text()
                    )
                    for name in ("execution.json", "evaluate.json", "result.json"):
                        evidence = next(
                            published / ref for ref in links if Path(ref).name == name
                        )
                        assert evidence.is_file()
                        if name == "evaluate.json":
                            for ref in json.loads(evidence.read_text())["evidence"]:
                                assert (evidence.parent / ref).read_text() == "42"
                task = dict(
                    question="Compute 6 * 7?",
                    hypothesis="42",
                    controls=["Exact arithmetic"],
                    permitted_changes=["Create script"],
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
                (work / "authored-task.json").write_text(json.dumps(task))
                (work / "authored-frontier.json").write_text(json.dumps(frontier))
                return dict(
                    base,
                    disposition="experiment",
                    experiment_task_ref="authored-task.json",
                    frontier_ref="authored-frontier.json",
                    consequential_claim=False,
                )
            if role == "analyst":
                references = assignment["input_refs"]
                receipts = [r for r in references if Path(r).name == "execution.json"]
                evaluations = [r for r in references if Path(r).name == "evaluate.json"]
                # The evaluated raw file is available next to its citation, not private work.
                evaluation = json.loads(Path(evaluations[0]).read_text())
                for evidence in evaluation["evidence"]:
                    assert (Path(evaluations[0]).parent / evidence).read_text() == "42"
                request = payload["study_context"]["input"]["human_request"]
                result = dict(
                    summary="Observed 42; requested experiment complete",
                    goal="achieved",
                    evidence=receipts + evaluations,
                    limitations=["Synthetic CPU only"],
                    next_action="",
                    study_action="stop",
                    stop_reason="User requested one bounded task",
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
                            content="# Scientific knowledge\n\n[Finding](finding.md)\n",
                        ),
                        dict(
                            source="knowledge/finding.md",
                            target="finding.md",
                            content="Observed 42 in the actual synthetic CPU result.\n",
                        ),
                    ],
                    execution_refs=receipts,
                    analysis_refs=evaluations,
                    direction_refs=[],
                    stop_basis="goal-satisfied",
                )
            assert role == "critic"
            if "verdict" in schema["properties"]:
                return {
                    "summary": "Review complete",
                    "verdict": "accept",
                    "reason": "Evidence supports the bounded result",
                    "artifacts": [],
                }
            return base

    monkeypatch.setattr(group_router, "configuration", lambda *a, **k: (None, {}))
    monkeypatch.setattr(
        "research_loop.group_agents.configuration",
        lambda *a, **k: (CodexConfig(cwd=project), {}),
    )
    state = asyncio.run(loop.run(root, native_factory=Native))
    assert state["status"] == "completed"
    assert (root / "inputs/human.md").read_text() == ""
    assert (
        (root / "knowledge/current/index.md").read_text()
        == "# Scientific knowledge\n\n[Finding](finding.md)\n\n\n[Group artifact index](group-artifacts.md)\n"
    )
    assert (root / "knowledge/current/finding.md").read_text().startswith("Observed 42")
    assert [r for r, _, _, _ in calls] == [
        "proposer",
        "proposer",
        "experimenter",
        "critic",
        "analyst",
        "critic",
    ]
    assert calls[1][1] == calls[2][1] != calls[3][1]
    assert len(list((root / "group-runs").glob("*/run.json"))) == 3
    roles = json.loads((root / "roles.json").read_text())["threads"]
    assert sum(row["role"] == "router" for row in roles.values()) > 1
    # A later task gets its own Runs and follows durable evidence in the prior
    # published knowledge tree, without any earlier expert conversation.
    human_input.submit(
        root, "One explicitly authorized second CPU check of 6 * 7; stop afterwards."
    )
    second = asyncio.run(loop.run(root, continue_run=True, native_factory=Native))
    assert second["status"] == "completed"
    assert len(list((root / "group-runs").glob("*/run.json"))) == 6


def test_reference_workflow_rejects_blocked_or_candidate_study_result(tmp_path):
    class Runtime:
        def observe(self, run_id):
            return {"round": 1}

    root = tmp_path / "study"
    directory = root / "group-runs/r/rounds/000001"
    directory.mkdir(parents=True)
    final = root / "final.json"
    candidate = root / "candidate.json"
    candidate.write_text("{}")
    final.write_text("{}")
    (directory / "artifacts.json").write_text(
        json.dumps(
            {
                "study-result.json": str(final),
                "reports/study-result-candidate.json": str(candidate),
            }
        )
    )
    coordinator = group_router.GroupCoordinator(
        root,
        {"groups": [], "workflow": "research-v1", "study_result_group": "synthesis"},
        None,
    )
    coordinator.runtime = Runtime()
    coordinator.admitted = {"r"}
    row = {
        "id": "r",
        "group_id": "synthesis",
        "status": "closed",
        "output": dict(
            summary="",
            completion="blocked",
            unfinished=["review"],
            help="Revise unsupported claim",
            artifacts=[str(final), str(candidate)],
        ),
    }
    packet = {"runs": [row]}
    with pytest.raises(ValueError, match="Blocked synthesis"):
        coordinator.study_result(str(candidate), packet)
    row["output"].update(completion="completed", unfinished=[], help="")
    with pytest.raises(ValueError, match="reviewed final"):
        coordinator.study_result(str(candidate), packet)
