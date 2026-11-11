"""Explicit opt-in pinned-runtime checks; default tests do not call a model."""

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest

from research_loop.codex import Native, Tool, configuration, plain
from research_loop.instructions import INSTRUCTIONS, RESULT_SCHEMA
from research_loop.loop import initialize, inspect
from research_loop.state import Journal


def workspace(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    goal = tmp_path / "goal.md"
    goal.write_text("Synthetic runtime verification only; no remote or GPU.")
    root = tmp_path / "run"
    initialize(root, project, goal, coordinator="lead")
    config, _ = configuration(
        root,
        project,
        [root / "work", root / "knowledge/draft"],
        "gpt-6.1-sol",
        "medium",
    )
    return root, config


def assess_role_smoke(root, result, receipts, children, thread_id, marker):
    """Check retained smoke evidence without admitting another model turn."""
    work = root / "work"
    expected = {"count": 4, "sum": 10, "sum_squares": 30}
    assert result["goal"] == "unknown"
    assert result["study_action"] == "stop"
    assert result["stop_reason"].strip()
    assert receipts == [{"marker": marker}]
    plan = json.loads((work / "plan.json").read_text())
    assert plan["numbers"] == [1, 2, 3, 4]
    assert plan["expected"] == expected
    assert plan["scope"].strip()
    assert (work / "experiment.py").stat().st_size
    output = json.loads((work / "result.json").read_text())
    assert output.get("actual", output) == expected
    if "all_checks_passed" in output:
        assert output["all_checks_passed"] is True
    assert (work / "execution.log").stat().st_size
    critique = json.loads((work / "critique.json").read_text())
    assert critique["verdict"] == "accept"
    assert critique["observed"] == expected
    assert critique["scope"] == "synthetic-only"
    assert critique["limitations"].strip()
    assert (work / "coordination.md").stat().st_size
    assert (root / "knowledge/draft/index.md").stat().st_size
    assert result["evidence"]
    assert len(children) >= 3
    rows = [
        json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()
    ]
    references = {
        row["thread_id"]: Path(row["path"])
        for row in rows
        if row["event"] == "codex.history.reference"
    }
    assert children <= references.keys()
    for child in children:
        assert marker not in references[child].read_text()
    # Inspect native tool records rather than matching policy text.
    calls = []
    for line in references[thread_id].read_text().splitlines():
        item = json.loads(line).get("payload", {})
        if item.get("type") == "function_call":
            calls.append((item["name"].split(".")[-1], json.loads(item["arguments"])))
    spawns = [args for name, args in calls if name == "spawn_agent"]
    assert len(spawns) >= 3
    assert all(args.get("fork_turns") == "none" for args in spawns)
    assert not any(name in {"resume_agent", "followup_task"} for name, _ in calls)
    # Native v2 reports child starts as subAgentActivity rather than spawnAgent.
    items = [
        row["data"]["item"]
        for row in rows
        if row["event"] == "codex.event"
        and row["method"] == "item/completed"
        and "item" in row["data"]
    ]
    spawned = set()
    for item in items:
        if (
            item.get("type") == "collabAgentToolCall"
            and item.get("tool") == "spawnAgent"
        ):
            spawned.update(item.get("receiverThreadIds", []))
        elif item.get("type") == "subAgentActivity" and item.get("kind") == "started":
            spawned.add(item["agentThreadId"])
    assert len(spawned) >= 3
    assert spawned <= children


@pytest.mark.skipif(
    os.environ.get("RESEARCH_NATIVE_TEST") != "1",
    reason="Opt-in native SDK metadata check",
)
def test_pinned_native_handshake_without_model(tmp_path):
    async def check():
        root, config = workspace(tmp_path)
        journal = Journal(root / "events.jsonl")
        native = Native(config, journal)
        try:
            async with asyncio.timeout(40):
                await native.open()
                await native.verify("gpt-6.1-sol", "medium")
                started = plain(
                    await asyncio.to_thread(
                        native.client.thread_start,
                        {
                            "model": "gpt-6.1-sol",
                            "allowProviderModelFallback": False,
                            "permissions": "research",
                            "approvalPolicy": "never",
                            "dynamicTools": [],
                        },
                    )
                )
                assert (
                    started["model"] == "gpt-6.1-sol"
                    and started["reasoningEffort"] == "medium"
                )
                native.thread_id = started["thread"]["id"]
                await native.settle()
        finally:
            await native.close()
            journal.close()

    asyncio.run(check())


@pytest.mark.skipif(
    os.environ.get("RESEARCH_MODEL_TEST") != "1",
    reason="Opt-in subscription/native-delegation smoke",
)
def test_native_fresh_research_roles_and_local_command(tmp_path):
    async def check():
        root, config = workspace(tmp_path)
        marker = f"SYNTHETIC_PARENT_ONLY_{uuid.uuid4().hex}"
        receipts = []

        async def record_probe(arguments):
            receipts.append(arguments)
            return {"recorded": True}

        tool = Tool(
            "record_probe",
            "Record the synthetic smoke marker on the host. Lead only.",
            {
                "type": "object",
                "properties": {"marker": {"type": "string"}},
                "required": ["marker"],
                "additionalProperties": False,
            },
            record_probe,
        )
        payload = inspect(root)["input"]
        payload["parent_only_marker"] = marker
        work = root / "work"
        expected = {"count": 4, "sum": 10, "sum_squares": 30}
        payload["verification"] = f"""One bounded synthetic integration smoke only.
Call record_probe once with parent_only_marker. Never pass that marker to children.
Use exactly three fresh native spawn_agent calls with fork_turns='none', sequentially:
Proposer, Experimenter, Critic. Copy the common contract and selected supplied role
into each self-contained task-specific handoff. Pin children to gpt-6.1-sol / medium,
no fallback. Include goal/project paths, evidence, ownership and completion check.
No nested delegation, resume/reuse/followup, web, GPU, SSH, or runtime edits.
This dependency sequence is only this smoke, not a mandatory general workflow.

1. Proposer owns only {work / "plan.json"}. Design a deterministic arithmetic check
on [1,2,3,4] with exact count, sum and sum of squares. Write a JSON plan with
numbers=[1,2,3,4], expected={json.dumps(expected)}, and a nonempty scope string
explaining the toy-only limitation. Compare the sum with an independent direct
addition control. No scientific/generalization claim or computation job.
Wait for completion; use its actual plan as evidence in the next fresh handoff.

2. NEW Experimenter reads the plan and owns only {work / "experiment.py"}. Prepare
a small standard-library Python script reading plan.json, computing those values
from numbers and writing {work / "result.json"}. It may syntax-check but must leave
the actual experiment invocation to you. Require exact command and expected output.
Do not write the script yourself. After it finishes, inspect the script and execute
its prepared command once yourself using native shell. Record command, stdout/stderr
and exit status in {work / "execution.log"}. No remote/model computation.

3. Only after result.json exists, NEW read-only Critic receives goal/project paths,
plan.json, experiment.py, result.json and execution.log, never executor chats.
Challenge the bounded claim: this arithmetic implementation matches this exact toy
plan. Critic must not edit files or rerun computation. Require its final reply as
JSON with verdict='accept' if supported, observed equal to the actual three values,
scope='synthetic-only', and a nonempty limitations string explaining why this does
not validate real scientific benefit. Persist that JSON yourself to
{work / "critique.json"}.

Maintain {work / "coordination.md"} with role, task, child identity, dependencies,
status and artifacts. Wait for all children to finish and close them with native
tools. Integrate an artifact-citing synthetic note into knowledge_draft/index.md.
Return the requested schema with goal='unknown', study_action='stop', and explicit
stop_reason that this authorized synthetic smoke is complete. Evidence must cite
actual artifact paths. Do not propose further runs.
"""
        journal = Journal(root / "events.jsonl")
        native = Native(config, journal, [tool])
        try:
            async with asyncio.timeout(480):
                await native.open()
                await native.verify("gpt-6.1-sol", "medium")
                result = await native.run(
                    INSTRUCTIONS,
                    payload,
                    RESULT_SCHEMA,
                    model="gpt-6.1-sol",
                    effort="medium",
                )
                await native.settle()
                assess_role_smoke(
                    root, result, receipts, native.children, native.thread_id, marker
                )
        finally:
            await native.stop()
            await native.close()
            journal.close()

    asyncio.run(check())
