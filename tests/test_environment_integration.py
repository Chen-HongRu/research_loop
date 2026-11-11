"""Opt-in isolated environment propagation; no cluster, GPU or real research."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from research_loop.codex import Native, configuration
from research_loop.instructions import RESULT_SCHEMA
from research_loop.loop import initialize, inspect
from research_loop.state import Journal


@pytest.mark.skipif(
    os.environ.get("RESEARCH_ENVIRONMENT_MODEL_TEST") != "1",
    reason="Opt-in authorized subscription environment smoke",
)
@pytest.mark.parametrize("mode", ["host", "native"])
def test_real_model_sees_fixed_environment_in_fresh_role(tmp_path, mode):
    selected = tmp_path / "selected environment"
    subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(selected)],
        check=True,
        capture_output=True,
        text=True,
    )
    python = selected / "bin/python"
    site = Path(
        subprocess.check_output(
            [
                str(python),
                "-I",
                "-c",
                "import sysconfig;print(sysconfig.get_path('purelib'))",
            ],
            text=True,
        ).strip()
    )
    distribution = site / "loop_env_witness-0.0.1.dist-info"
    distribution.mkdir()
    (distribution / "METADATA").write_text("Name: loop-env-witness\nVersion: 0.0.1\n")
    resource_python = "/opt/synthetic-device-env/bin/python"
    env_file = tmp_path / "environment.json"
    env_file.write_text(
        json.dumps(
            {
                "python": str(python),
                "resource": {
                    "python": resource_python,
                    "packages": {"synthetic-device-package": "7.8.9"},
                },
            }
        )
    )
    project = tmp_path / "project"
    project.mkdir()
    goal = tmp_path / "goal.md"
    goal.write_text(
        "Synthetic environment disclosure only; no research or compute grant."
    )
    root = tmp_path / "study"
    model = os.environ.get("RESEARCH_ENVIRONMENT_MODEL", "gpt-6.1-terra")
    initialize(
        root,
        project,
        goal,
        model=model,
        delegation=mode,
        environment_file=env_file,
        coordinator="lead",
    )
    packet = inspect(root)
    environment = packet["input"]["execution_environment"]
    work = root / "work"
    cfg, _ = configuration(
        root,
        project,
        [work, root / "knowledge/draft"],
        model,
        "medium",
        delegation=mode,
        execution_environment=environment,
    )
    verification = f"""This is one bounded synthetic environment smoke, not research.
This run's delegation mode is {mode}. In host mode you MUST use delegate_task
and wait_tasks, never native spawn/collaboration tools, even if they are visible.
In native mode you MUST use native spawn_agent with fork_turns='none'.
The runtime has supplied execution-environment context to you and fresh children.
1. Run one local shell command using the unqualified `python` from PATH. Write
{work / "lead.json"} with fields python (actual sys.executable) and
witness_version (installed loop-env-witness version from importlib.metadata).
2. Start exactly ONE NEW Analyst with the same exact model {model} / medium.
Use delegate_task in host mode, or native spawn_agent with fork_turns='none' in
native mode. Do not copy interpreter paths, package versions, environment JSON
or resource declarations into the child assignment: it must use the environment
instructions injected by the runtime, not your handoff or parent history.
Give only this assignment and output path: run one local shell command with
unqualified python; write {work / "child.json"} with fields python (actual
sys.executable), witness_version (installed loop-env-witness version),
resource_python, resource_package_version (synthetic-device-package), and
resource_status ('declared-unverified'), using the resource information visible
in its injected execution-environment context. Reading Study environment/config
files or parent histories to obtain those values is forbidden in this smoke.
No further agents, web, remote hosts, GPU, package installation or environment
changes. Wait for the child; host wait_tasks is nonblocking, so do not spin.
Return the required result schema with goal='unknown', study_action='stop', and
an explicit stop_reason that this synthetic smoke is complete. Cite both files.
"""

    async def run():
        journal = Journal(root / "events.jsonl")
        native = Native(cfg, journal)
        try:
            async with asyncio.timeout(240):
                await native.open()
                await native.verify(model, "medium")
                result = await native.run(
                    verification,
                    packet["input"],
                    RESULT_SCHEMA,
                    model=model,
                    effort="medium",
                    delegation=mode,
                    roles_path=root / "roles.json",
                    environment=environment,
                )
                await native.settle()
            assert result["study_action"] == "stop"
            assert len(native.children) == 1
            return set(native.children)
        finally:
            await native.stop()
            await native.close()
            journal.close()

    children = asyncio.run(run())
    lead = json.loads((work / "lead.json").read_text())
    child = json.loads((work / "child.json").read_text())
    assert lead["python"] == child["python"] == str(python)
    assert lead["witness_version"] == child["witness_version"] == "0.0.1"
    assert child["resource_python"] == resource_python
    assert child["resource_package_version"] == "7.8.9"
    assert child["resource_status"] == "declared-unverified"
    events = [
        json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()
    ]
    starts = [row for row in events if row["event"] == "role.task.started"]
    finishes = [row for row in events if row["event"] == "role.task.finished"]
    if mode == "host":
        assert len(starts) == len(finishes) == 1
        assert starts[0]["role"] == "analyst"
        assert finishes[0]["status"] == "completed"
        assert starts[0]["thread_id"] in children
        tools = {
            row["tool"]
            for row in events
            if row["event"] == "resource.tool" and row["success"]
        }
        assert {"delegate_task", "wait_tasks"} <= tools
        mapping = json.loads((root / "roles.json").read_text())["threads"]
        assert mapping[starts[0]["thread_id"]]["status"] == "completed"
    else:
        assert not starts and not finishes and not (root / "roles.json").exists()
    histories = {
        row["thread_id"]: Path(row["path"])
        for row in events
        if row["event"] == "codex.history.reference"
    }
    for identity in children:
        records = [
            json.loads(line) for line in histories[identity].read_text().splitlines()
        ]
        models = {
            row["payload"].get("model")
            for row in records
            if row.get("type") == "turn_context"
        }
        assert models == {model}
        messages = [
            row["payload"]
            for row in records
            if row.get("type") == "response_item"
            and row.get("payload", {}).get("type") == "message"
        ]
        users = [json.dumps(m) for m in messages if m.get("role") == "user"]
        developers = [json.dumps(m) for m in messages if m.get("role") == "developer"]
        assert users and not any(resource_python in text for text in users)
        assert any(resource_python in text for text in developers)
