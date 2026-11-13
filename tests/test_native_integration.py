"""Explicit opt-in checks of current declarative Agents and the native harness."""

import asyncio
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from research_loop.codex import Native, configuration
from research_loop.configuration import prepare_configuration
from research_loop.group_agents import NativeGroupServices
from research_loop.groups import GroupRuntime
from research_loop.state import Journal, write_json

MODEL = os.environ.get("RESEARCH_MODEL", "gpt-6.1-sol")
EFFORT = "medium"


def context(tmp_path, config, bindings):
    work = tmp_path / "work"
    work.mkdir()
    ctx = SimpleNamespace(
        root=tmp_path,
        work=work,
        study_root=tmp_path,
        run_id="native-check",
        round=1,
        config_revision=config["revision"],
        definition=SimpleNamespace(agents=bindings, settings={}),
        state={},
    )
    ctx.save_state = lambda: write_json(tmp_path / "state.json", ctx.state)
    return ctx


@pytest.mark.skipif(
    os.environ.get("RESEARCH_NATIVE_TEST") != "1",
    reason="Opt-in native account/model metadata handshake; no model turn",
)
def test_pinned_native_handshake_without_model(tmp_path):
    config, _ = configuration(
        tmp_path, tmp_path, [tmp_path / "work"], MODEL, EFFORT, allow_tools=False
    )

    async def run():
        journal = Journal(tmp_path / "events.jsonl")
        native = Native(config, journal)
        try:
            async with asyncio.timeout(40):
                await native.open()
                await native.verify(MODEL, EFFORT)
                await native.settle()
        finally:
            await native.close()
            journal.close()

    asyncio.run(run())


@pytest.mark.skipif(
    os.environ.get("RESEARCH_MODEL_TEST") != "1",
    reason="Opt-in actual OpenAI Plan/Prepare/local command/independent Evaluate smoke",
)
def test_current_research_group_shared_author_and_independent_evaluation(tmp_path):
    source = Path(__file__).resolve().parents[1] / "examples/research.yaml"
    declaration = yaml.safe_load(source.read_text())
    declaration["workflow"]["program"] = str(
        source.parent / declaration["workflow"]["program"]
    )
    for group in declaration["groups"].values():
        group["program"] = str(source.parent / group["program"])
    for agent in declaration["agents"].values():
        agent["instructions"] = str(source.parent / agent["instructions"])
    for profile in declaration["profiles"].values():
        profile.update(model=MODEL, effort=EFFORT)
    marker = "SYNTHETIC_AUTHOR_ONLY_" + uuid.uuid4().hex
    instructions = tmp_path / "author.md"
    instructions.write_text(
        Path(declaration["agents"]["experiment"]["instructions"]).read_text()
        + "\nConversation isolation witness: "
        + marker
        + ". Never write this witness into artifacts or pass it to Evaluate.\n"
    )
    declaration["agents"]["experiment"]["instructions"] = str(instructions)
    configured = tmp_path / "native.yaml"
    configured.write_text(yaml.safe_dump(declaration))
    config = prepare_configuration(
        configured, tmp_path / "configuration", revision="native-fixed"
    )
    config["project"] = str(tmp_path)
    task = tmp_path / "task.md"
    task.write_text("""One bounded synthetic CPU smoke; no scientific generalization.
Design and prepare a standard-library Python script computing count, sum and sum
of squares from [1,2,3,4]. Exact expected output is {"count":4,"sum":10,"sum_squares":30}.
Write the actual output to result.json. On each script invocation, append exactly
'once' with no newline to the file named dispatch-count (no extension), without
replacing any existing content. The host alone executes the prepared command once.
Plan writes a scientific
plan with the control and toy-only scope. Prepare may syntax-check only. Independent
Evaluate reads the actual plan, host execution receipt and result.json, checks the
three exact values without rerunning, and reports limitations. Do not spawn agents,
install packages, use web, remote resources or GPUs. Stop after this bounded Group.
""")
    journal = Journal(tmp_path / "events.jsonl")
    runtime = GroupRuntime(
        tmp_path,
        config["groups"],
        revision=config["revision"],
        service_factory=lambda ctx: NativeGroupServices(ctx, config, journal),
    )

    async def run():
        try:
            async with asyncio.timeout(480):
                identity = await runtime.start(
                    "exp",
                    str(task),
                    [],
                    context={"input": {"project_root": str(tmp_path)}},
                )
                await runtime.wait()
                result = runtime.observe(identity)
                assert result["status"] == "returned", result
                assert result["output"]["completion"] == "completed", result["output"]
                return identity
        finally:
            await runtime.close_runtime()
            journal.close()

    identity = asyncio.run(run())
    root = tmp_path / "group-runs" / identity
    state = json.loads((root / "state.json").read_text())
    calls = state["native_calls"]
    assert [call["binding"] for call in calls] == ["plan", "prepare", "evaluate"]
    assert calls[0]["thread_id"] == calls[1]["thread_id"] != calls[2]["thread_id"]
    assert calls[0]["session_id"] == calls[1]["session_id"] != calls[2]["session_id"]
    assert all(call["profile"] == {"model": MODEL, "effort": EFFORT} for call in calls)
    assert marker in calls[0]["instructions"] and marker not in json.dumps(calls[2])
    assert json.loads((root / "work/result.json").read_text()) == {
        "count": 4,
        "sum": 10,
        "sum_squares": 30,
    }
    assert (root / "work/dispatch-count").read_text() == "once"
    assert len(state["executions"]) == 1
    assert state["executions"]["prepared-experiment"]["receipt"]["exit_code"] == 0
    assert state["experiment_phases"]["evaluate"]["limitations"]
    assert all(
        state["experiment_phases"][name]["status"] == "ready"
        for name in ("plan", "prepare", "evaluate")
    )


@pytest.mark.skipif(
    os.environ.get("RESEARCH_MODEL_TEST") != "1",
    reason="Opt-in actual declarative OpenAI tools-none Agent",
)
def test_current_declarative_agent_has_no_tools_and_exact_profile(tmp_path):
    schema = {
        "type": "object",
        "properties": {"token": {"type": "string"}},
        "required": ["token"],
        "additionalProperties": False,
    }
    config = dict(
        revision="native-fixed",
        project=str(tmp_path),
        profiles={"text": {"model": MODEL, "effort": EFFORT}},
        agents={
            "echo": {
                "instructions": "Return the supplied token exactly as structured JSON. Use no tools or delegation.",
                "profile": "text",
                "tools": "none",
                "output_schema": schema,
            }
        },
    )
    ctx = context(tmp_path, config, {"reply": "echo"})
    journal = Journal(tmp_path / "events.jsonl")
    services = NativeGroupServices(ctx, config, journal)
    token = "SYNTHETIC_TOKEN_" + uuid.uuid4().hex

    async def run():
        try:
            async with asyncio.timeout(90):
                return await services.call(
                    "reply", "echo", "Echo the token.", {"token": token}, session="new"
                )
        finally:
            await services.close()
            journal.close()

    assert asyncio.run(run()) == {"token": token}
    receipt = ctx.state["native_calls"][0]
    assert receipt["agent_id"] == "echo" and receipt["profile"] == {
        "model": MODEL,
        "effort": EFFORT,
    }
    assert receipt["effective"]["allow_tools"] is False
    assert receipt["effective"]["allow_builtin_tools"] is False
    assert receipt["status"] == "completed"
