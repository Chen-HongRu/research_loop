"""Same completion gate for reuse-only Router and controlled Lead publication."""

import asyncio
from copy import deepcopy
import json

import pytest

from research_loop import group_router, human_input, loop
from research_loop.codex import CodexConfig
from test_exp_recovery import settled_exp as _settled_exp

settled_exp = _settled_exp


def candidate(sources):
    request = sources["study_context"]["input"].get("human_request")
    result = dict(
        summary="Recovered the existing independent assessment.",
        goal="achieved",
        limitations=["Synthetic only; no new computation."],
        next_action="",
        study_action="stop",
        stop_reason="The bounded recovery request is complete.",
    )
    if request:
        result["human_request"] = dict(
            id=request["id"],
            status="completed",
            reason="Existing execution and independent assessment are published.",
        )
    return dict(
        summary=result["summary"],
        help="",
        study_result=result,
        knowledge_files=[
            dict(
                source="knowledge/index.md",
                target="index.md",
                content="# Recovery\n\n[Finding](finding.md)\n",
            ),
            dict(
                source="knowledge/finding.md",
                target="finding.md",
                content="Existing result retained; synthetic only.",
            ),
        ],
        stop_basis="goal-satisfied",
    )


@pytest.mark.parametrize("mode", ["router", "lead"])
@pytest.mark.parametrize("with_request", [False, True])
def test_recovery_publication_uses_same_completion_gate_without_experiment(
    settled_exp,
    tmp_path,
    monkeypatch,
    mode,
    with_request,
):
    source, _, _ = settled_exp
    project = tmp_path / "project"
    project.mkdir()
    goal = tmp_path / "goal.md"
    goal.write_text(
        "Recover and publish already settled evidence; never execute the experiment."
    )
    root = tmp_path / mode
    loop.initialize(root, project, goal, coordinator=mode, recover_exp=source)
    if with_request:
        human_input.submit(root, goal.read_text())
    assert {g["id"] for g in loop.read_config(root)["groups"]} == {"synthesis"}
    calls = []
    thread_ids = []

    class Fake:
        def __init__(self, config, journal, tools=()):
            self.tools = {tool.name: tool for tool in tools}

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
            thread = f"thread-{len(thread_ids)}"
            thread_ids.append(thread)
            if kwargs.get("on_started"):
                kwargs["on_started"](thread)
            if "action" in schema["properties"]:
                raise AssertionError("The fixed recovery profile needs no Router model")
            if "review_candidate" in self.tools:
                calls.append("lead")
                assert kwargs["allow_builtin_tools"] is False
                value = candidate(payload)
                reviewed = await self.tools["review_candidate"].call(deepcopy(value))
                assert reviewed["review"]["verdict"] == "accept"
                return reviewed["accepted_candidate"]
            assert kwargs["allow_tools"] is False
            if "verdict" in schema["properties"]:
                calls.append("critic")
                assert (
                    payload["assignment"]["analysis_record"]["study_result"]["goal"]
                    == "achieved"
                )
                return dict(
                    summary="Independent review complete",
                    verdict="accept",
                    reason="The candidate preserves existing evidence and limitations.",
                    artifacts=[],
                )
            calls.append("analyst")
            return candidate(payload["assignment"])

    def config(*args, **kwargs):
        return CodexConfig(cwd=str(project)), {}

    monkeypatch.setattr(group_router, "configuration", config)
    monkeypatch.setattr("research_loop.group_agents.configuration", config)
    monkeypatch.setattr("research_loop.recovery.configuration", config)

    async def no_execution(*args, **kwargs):
        raise AssertionError("Recovery must not execute an experiment")

    monkeypatch.setattr(
        "research_loop.group_agents.NativeGroupServices.execute_prepared", no_execution
    )
    state = asyncio.run(loop.run(root, native_factory=Fake))
    assert state["status"] == "completed"
    if with_request:
        assert state["result"]["human_request"]["status"] == "completed"
        assert (root / "inputs/human.md").read_text() == ""
    else:
        assert "human_request" not in state["result"]
    assert (root / "knowledge/current/finding.md").is_file()
    assert (root / "knowledge/current/recovery-evidence/evaluation.json").is_file()
    assert calls == (["analyst", "critic"] if mode == "router" else ["lead", "critic"])
    assert state["thread_id"] == thread_ids[0]
    assert len(set(thread_ids)) == len(thread_ids)
    assert len(list((root / "group-runs").glob("*/run.json"))) == 1
    source_record = json.loads((source / "run.json").read_text())
    assert source_record["status"] == "closed" and source_record["round"] == 2
