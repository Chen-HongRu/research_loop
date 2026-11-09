"""Opt-in real-model lifecycle parity; scheduler replies alone are synthetic."""

import asyncio
import json
import os
import re
import sys
from pathlib import Path

import pytest

from research_loop import human_input, loop
from research_loop.codex import Native
from research_loop.slurm import Slurm
from research_loop.state import now, write_json

MEASUREMENT_SCRIPT = (
    "import json, sys\nfrom pathlib import Path\n"
    "n = int(sys.argv[1])\nassert n in (1, 2)\n"
    "p = Path(__file__).parent / f'measurement-{n}.json'\n"
    "with p.open('x') as f:\n"
    "    json.dump({'value': (1 - 2) if n == 1 else (2 - 1)}, f)\n"
    "print(p.read_text())\n"
)


def assess_recorded_lifecycle(root, model, delegation, script):
    """Check actual dispatches and child replies, not lead-authored claims alone."""
    assert (root / "work/measure.py").read_text() == script
    events = [
        json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()
    ]
    boundaries = [i for i, row in enumerate(events) if row["event"] == "lead.input"]
    assert len(boundaries) == 2
    references = {
        row["thread_id"]: Path(row["path"])
        for row in events
        if row["event"] == "codex.history.reference"
    }
    lead = events[boundaries[0]]["thread_id"]
    histories = {
        tid: [json.loads(line) for line in path.read_text().splitlines()]
        for tid, path in references.items()
    }
    calls = [
        row["payload"]
        for row in histories[lead]
        if row.get("payload", {}).get("type") == "function_call"
    ]
    assert not any(
        call["name"].split(".")[-1] in {"resume_agent", "followup_task"}
        for call in calls
    )
    spawns = [
        json.loads(call["arguments"])
        for call in calls
        if call["name"].split(".")[-1] == "spawn_agent"
    ]
    if delegation == "native":
        assert len(spawns) == 2
        assert all(
            call.get("fork_turns") == "none"
            and call.get("model") == model
            and call.get("reasoning_effort") == "medium"
            for call in spawns
        )
    else:
        assert not spawns
    seen = set()
    for n, start in enumerate(boundaries, 1):
        block = events[start : boundaries[n] if n < len(boundaries) else len(events)]
        completed = [
            row["data"]["item"]
            for row in block
            if row["event"] == "codex.event"
            and row.get("method") == "item/completed"
            and row.get("data", {}).get("threadId") == lead
            and "item" in row.get("data", {})
        ]
        expression = (
            re.escape(str(root / "work/measure.py")) + r"['\"]?\s+([12])(?:\s|['\";]|$)"
        )
        executions = [
            item
            for item in completed
            if item.get("type") == "commandExecution"
            and re.search(expression, item.get("command", ""))
        ]
        assert len(executions) == 1
        execution = executions[0]
        assert execution["exitCode"] == 0
        assert int(re.search(expression, execution["command"])[1]) == n
        # A lead may append a labeled readback of the same artifact. Compare the
        # JSON measurement records, not provider-specific presentation text.
        outputs = []
        for line in execution["aggregatedOutput"].splitlines():
            try:
                output = json.loads(line)
            except ValueError:
                continue
            if isinstance(output, dict) and "value" in output:
                outputs.append(output)
        assert outputs and all(
            output["value"] == (-1 if n == 1 else 1) for output in outputs
        )
        if delegation == "host":
            started = [row for row in block if row["event"] == "role.task.started"]
            assert len(started) == 1
            assert (
                started[0]["role"] == "critic"
                and started[0]["model"] == model
                and started[0]["effort"] == "medium"
            )
            child = started[0]["thread_id"]
        else:
            children = {
                item["agentThreadId"]
                for item in completed
                if item.get("type") == "subAgentActivity"
                and item.get("kind") == "started"
            }
            children.update(
                tid
                for item in completed
                if item.get("type") == "collabAgentToolCall"
                and item.get("tool") == "spawnAgent"
                for tid in item.get("receiverThreadIds", [])
            )
            assert len(children) == 1
            child = children.pop()
        assert child not in seen and child != lead
        seen.add(child)
        finals = [
            row["payload"]
            for row in histories[child]
            if row.get("payload", {}).get("type") == "message"
            and row["payload"].get("role") == "assistant"
            and row["payload"].get("phase") == "final_answer"
        ]
        assert len(finals) == 1
        reply = json.loads(
            "".join(content.get("text", "") for content in finals[0]["content"])
        )
        assert json.loads((root / f"work/critique-{n}.json").read_text()) == reply


@pytest.mark.skipif(
    os.environ.get("RESEARCH_PROVIDER_PARITY_TEST") != "1",
    reason="Opt-in subscription and configured-provider model calls",
)
@pytest.mark.parametrize(
    "backend", ["subscription-native", "subscription-host", "provider-host"]
)
def test_real_model_lifecycle_parity(tmp_path, monkeypatch, backend):
    provider = None
    model = "gpt-6.1-sol"
    if backend == "provider-host":
        required = {
            name: os.environ.get("RESEARCH_PARITY_" + name)
            for name in ("PROVIDER", "BASE_URL", "KEY_ENV", "MODEL")
        }
        assert all(required.values()), "Set all RESEARCH_PARITY_* provider settings"
        assert os.environ.get(required["KEY_ENV"]), "Configured provider key is missing"
        model = required["MODEL"]
        provider = {
            "name": required["PROVIDER"],
            "base_url": required["BASE_URL"],
            "api_key_env": required["KEY_ENV"],
            "context_window": 131072,
            "model_catalog": None,
        }
    delegation = "native" if backend == "subscription-native" else "host"
    root, project = tmp_path / "run", tmp_path / "project"
    project.mkdir()
    goal = project / "goal.md"
    goal.write_text(
        f"""# Bounded lifecycle acceptance, not scientific research
Use only configured {model} / medium and {delegation} delegation, without fallback.
Exactly TWO iterations. No web, SSH, GPU allocation, research, runtime edits or
extra experiments. Only local arithmetic and the supplied synthetic scheduler
receipt are authorized. This tests host semantics, not model quality or science.

Iteration1: call gpu_reconcile exactly once with reservation_id=synthetic-original.
It must return CANCELLED / elapsed_seconds=0 for the original synthetic job. Do
not call gpu_exec or allocate resources. In iteration2 make no resource calls.
In EACH iteration, read only the admitted human_request snapshot in the input,
not a later edit. Execute the operator-prepared work/measure.py exactly once:
{sys.executable} WORK_ROOT/measure.py ITERATION_NUMBER
Read work/measurement-N.json. Expected values are -1 in round1, +1 in round2.
A negative result completes request A; positivity is not a completion condition.

After actual execution, delegate exactly ONE fresh read-only Critic to inspect
that measurement file, the prepared script and this goal. No other roles are
needed for this engineering check. Give a self-contained handoff with ownership,
artifact paths, exact claim, limitation and completion check. No nested delegates,
parent chats, resume or followup. In host mode use delegate_task/wait_tasks only.
In native mode use a fresh spawn_agent(fork_turns="none") pinned to {model}/medium;
include common contract and Critic role, wait and close it. The child must return
JSON with verdict="accept", value equal to the measurement, scope="synthetic-only".
Persist its actual reply as work/critique-N.json, without inventing a verdict.

Maintain work/coordination.md and knowledge_draft/index.md with actual artifact
references and limitations. Keep round1's negative finding in round2's knowledge.
Return goal=unknown and human_request completed with its ADMITTED id and actual
measurement/critique evidence. Round1: study_action=continue, stop_reason empty.
Round2: study_action=stop with reason "bounded lifecycle acceptance complete".
The operator edits B after A admission; the host must preserve B across A's
publication and admit it in round2. Do not edit inputs or ledger. Stop after two
rounds, settle all children/background work, and make no scientific benefit claim.
"""
    )
    resource_file = project / "resource.json"
    write_json(
        resource_file,
        {
            "host": "example.invalid",
            "partition": "debug",
            "remote_root": "/scratch/synthetic-acceptance",
            "ledger_path": str(tmp_path / "original-budget.json"),
            "gpu_seconds_total": 330,
            "job_seconds": 300,
        },
    )
    loop.initialize(
        root,
        project,
        goal,
        model=model,
        provider=provider,
        delegation=delegation,
        resource_file=resource_file,
    )
    write_json(
        tmp_path / "original-budget.json",
        {
            "schema_version": 1,
            "authorized_seconds": 330,
            "entries": [
                {
                    "reservation_id": "synthetic-original",
                    "job_name": "rl-synthetic-original",
                    "job_id": "123456",
                    "host_alias": "example.invalid",
                    "status": "UNKNOWN",
                    "requested_wall_seconds": 300,
                    "termination_grace_seconds": 30,
                    "reserved_seconds": 330,
                    "continuation_authorized": False,
                    "dispatched_at": now(),
                }
            ],
        },
    )
    script = MEASUREMENT_SCRIPT
    (root / "work/measure.py").write_text(script)
    admitted_a = human_input.submit(
        root,
        "A: execute and analyze 1-2 using the prepared script for iteration1; -1 is a valid completed negative result.",
    )
    queries, natives = [], []

    class ReplayScheduler(Slurm):
        async def _allocate(self):
            raise AssertionError("This test must never allocate")

        async def _query(self, argv):
            queries.append(argv)
            if argv[0] == "squeue":
                return ""
            assert argv[0] == "sacct", "No scheduler writes are authorized"
            fields = {
                "JobIDRaw": "123456",
                "JobName": "rl-synthetic-original",
                "State": "CANCELLED",
                "ElapsedRaw": "0",
                "AllocTRES": "",
                "Start": "None",
                "ExitCode": "0:0",
                "Partition": "debug",
            }
            return (
                "|".join(
                    fields[field.split("%")[0]]
                    for field in argv[argv.index("-o") + 1].split(",")
                )
                + "\n"
            )

    monkeypatch.setattr(
        loop, "resource", lambda config, path: ReplayScheduler(config["resource"], path)
    )

    def factory(config, journal, tools):
        native = Native(config, journal, tools)
        natives.append(native)
        return native

    async def check():
        task = asyncio.create_task(loop.run(root, native_factory=factory))
        try:
            snapshot = (
                root
                / "iterations/000001"
                / f"human-request-{admitted_a['request_id']}.json"
            )
            while not snapshot.exists():
                if task.done():
                    await task
                    raise AssertionError("Run ended before admission")
                await asyncio.sleep(0.05)
            # The same documented input lock used by edit-experiment protects this
            # operator edit; only admission was awaited, never model output.
            with human_input.input_lock(root) as path:
                path.write_text(
                    "B: execute and analyze 2-1 using the prepared script for iteration2; preserve A, do not rerun it, then stop."
                )
            admitted_b = human_input.admit(root)
            assert admitted_b["id"] != admitted_a["request_id"]
            state = await task
        finally:
            if not task.done():
                (root / "STOP").write_text("acceptance timeout\n")
                await asyncio.gather(task, return_exceptions=True)
        assert state["status"] == "completed"
        states = [
            json.loads(path.read_text())
            for path in sorted((root / "iterations").glob("*/status.json"))
        ]
        assert len(states) == 2
        assert states[0]["thread_id"] == states[1]["thread_id"]
        assert [row["result"]["human_request"]["id"] for row in states] == [
            admitted_a["request_id"],
            admitted_b["id"],
        ]
        assert all(
            row["result"]["human_request"]["status"] == "completed" for row in states
        )
        assert all(row["result"]["goal"] == "unknown" for row in states)
        for n, value in ((1, -1), (2, 1)):
            assert (
                json.loads((root / f"work/measurement-{n}.json").read_text())["value"]
                == value
            )
            critique = json.loads((root / f"work/critique-{n}.json").read_text())
            assert critique["verdict"] == "accept" and critique["value"] == value
            assert critique["scope"] == "synthetic-only"
        assert human_input.admit(root) is None
        assert len(list((root / "knowledge/publications").glob("*.json"))) == 2
        ledger = json.loads((tmp_path / "original-budget.json").read_text())
        assert len(ledger["entries"]) == 1
        entry = ledger["entries"][0]
        assert (
            entry["status"] == "TERMINAL" and entry["receipt"]["elapsed_seconds"] == 0
        )
        assert state["resource"]["budget"]["reserved_seconds"] == 0
        assert state["resource"]["budget"]["remaining_seconds"] == 330
        assert len(queries) == 2
        children = set().union(*(native.children for native in natives))
        assert len(children) == 2 and states[0]["thread_id"] not in children
        if delegation == "host":
            roles = json.loads((root / "roles.json").read_text())["threads"]
            assert set(roles) == children
            assert all(
                role["role"] == "critic" and role["status"] == "completed"
                for role in roles.values()
            )
        assess_recorded_lifecycle(root, model, delegation, script)
        size = (root / "events.jsonl").stat().st_size
        assert (await loop.run(root, native_factory=factory))["status"] == "completed"
        assert len(natives) == 2 and (root / "events.jsonl").stat().st_size == size

    async def bounded():
        async with asyncio.timeout(600):
            await check()

    asyncio.run(bounded())
