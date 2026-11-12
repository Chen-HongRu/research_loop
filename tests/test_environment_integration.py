"""Opt-in current declarative Agent environment propagation, without remote work."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from research_loop import environment
from research_loop.group_agents import NativeGroupServices
from research_loop.state import Journal
from test_native_integration import EFFORT, MODEL, context


@pytest.mark.skipif(
    os.environ.get("RESEARCH_ENVIRONMENT_MODEL_TEST") != "1",
    reason="Opt-in authorized OpenAI environment smoke using an existing interpreter",
)
def test_real_fresh_agents_use_fixed_declared_environment(tmp_path):
    selected = os.environ.get("RESEARCH_ENVIRONMENT_PYTHON")
    if not selected:
        pytest.fail(
            "Set RESEARCH_ENVIRONMENT_PYTHON to an existing authorized interpreter; this test does not create or install an environment"
        )
    declared = tmp_path / "environment.json"
    declared.write_text(
        json.dumps(
            {
                "python": selected,
                "resource": {
                    "python": "/opt/synthetic-device-env/bin/python",
                    "packages": {"synthetic-device-package": "7.8.9"},
                },
            }
        )
    )
    snapshot = environment.observe(environment.read_config(declared))
    schema = {
        "type": "object",
        "properties": {"artifact": {"type": "string"}},
        "required": ["artifact"],
        "additionalProperties": False,
    }
    config = dict(
        revision="fixed",
        project=str(tmp_path),
        execution_environment=snapshot,
        profiles={"selected": {"model": MODEL, "effort": EFFORT}},
        agents={
            "observer": {
                "instructions": "You are one fresh environment observer. Use only the injected environment snapshot and your own local shell. No delegation, web, remote/GPU work, package installation or environment changes.",
                "profile": "selected",
                "tools": "workspace",
                "output_schema": schema,
            }
        },
    )
    ctx = context(tmp_path, config, {"observe": "observer"})
    journal = Journal(tmp_path / "events.jsonl")
    service = NativeGroupServices(ctx, config, journal)

    async def run():
        try:
            async with asyncio.timeout(240):
                for name in ("first", "independent"):
                    result = await service.call(
                        "observe",
                        name,
                        "Run one local shell command with unqualified python. Write the supplied output file as JSON with actual sys.executable (python), sys.prefix (prefix), and resource_python, resource_package_version, resource_status from the injected environment snapshot. Resource values are declared and unverified. Return only its artifact path.",
                        {"output_file": f"{name}.json"},
                        session="new",
                        cwd=ctx.work,
                    )
                    assert Path(result["artifact"]).name == f"{name}.json"
        finally:
            await service.close()
            journal.close()

    asyncio.run(run())
    for name in ("first", "independent"):
        observed = json.loads((ctx.work / f"{name}.json").read_text())
        assert observed == {
            "python": snapshot["local"]["executable"],
            "prefix": snapshot["local"]["prefix"],
            "resource_python": "/opt/synthetic-device-env/bin/python",
            "resource_package_version": "7.8.9",
            "resource_status": "declared-unverified",
        }
    calls = ctx.state["native_calls"]
    assert len(calls) == 2 and calls[0]["thread_id"] != calls[1]["thread_id"]
    assert calls[0]["session_id"] != calls[1]["session_id"]
    assert all(call["profile"] == {"model": MODEL, "effort": EFFORT} for call in calls)
    assert all(call["input"]["execution_environment"] == snapshot for call in calls)
