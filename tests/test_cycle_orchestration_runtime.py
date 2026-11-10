"""Exercise editable orchestration through the fixed runner using synthetic receipts."""

import asyncio
import json
from pathlib import Path
import sys

import pytest

from benchmarks.experiment_cycle.runner import run

EXAMPLE = (
    Path(__file__).resolve().parents[1]
    / "benchmarks/experiment_cycle/example/case.json"
)


class ScriptedAgent:
    def __init__(self):
        self.calls = []

    async def __call__(
        self, stage, case, workspace, root, identity, model, effort, **kwargs
    ):
        self.calls.append((stage, kwargs))
        context = kwargs["context"]
        tid = "fixture-" + context
        history = root.parents[1] / f"{context}.jsonl"
        if not history.exists():
            history.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": tid}}) + "\n"
            )
        start = history.stat().st_size
        with history.open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "type": "token_usage_record",
                        "payload": {
                            "response_id": f"receipt-{len(self.calls)}",
                            "usage": {
                                "input_tokens": 100,
                                "cached_input_tokens": 30,
                                "output_tokens": 10,
                                "reasoning_output_tokens": 3,
                            },
                        },
                    }
                )
                + "\n"
            )
        identity(
            tid,
            str(history),
            history_start_bytes=start,
            history_end_bytes=history.stat().st_size,
        )
        if kwargs["message"]:
            return {"message": "refinement available"}
        if stage == "plan":
            return dict.fromkeys(
                ("hypothesis", "control", "change", "evaluation_rule", "outcomes"),
                "bounded fixture",
            )
        if stage == "prepare":
            (workspace / "experiment.py").write_text(
                'import json\nfrom pathlib import Path\nx=json.loads(Path("inputs.json").read_text())\n'
                'rows=[{"id":r["id"],"control_prediction":r["x"],"candidate_prediction":r["x"]+1} for r in x["observations"]]\n'
                'Path("result.json").write_text(json.dumps({"version":1,"observations":rows}))\n'
            )
            return {
                "command": [
                    sys.executable if a == "{python}" else a for a in case["command"]
                ],
                "smoke": "syntax check",
                "artifacts": ["experiment.py"],
            }
        return {
            "disposition": "supported",
            "metrics": {"control_mse": 1.0, "candidate_mse": 0.0, "delta_mse": -1.0},
            "limitations": "synthetic-only",
            "evidence": [case["result_file"]],
        }


async def cpu_executor(case, workspace, root, command, model, effort):
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await process.communicate()
    (root / "command.log").write_bytes(stdout)
    return {"exit_code": process.returncode, "log_path": str(root / "command.log")}


def run_program(tmp_path, name, body=None):
    path = None
    if body is not None:
        path = tmp_path / f"{name}.py"
        path.write_text(
            'COMMON = "fixture common"\nasync def orchestrate(team):\n'
            + "\n".join("    " + line for line in body.splitlines())
            + "\n"
        )
    agent = ScriptedAgent()
    output = tmp_path / name
    report = asyncio.run(
        run(
            EXAMPLE,
            output,
            agent=agent,
            executor=cpu_executor,
            orchestration=path,
            measurement_kind="synthetic-fixture",
        )
    )
    return report, agent, output


PLAN = 'await team.call("plan", role="plan", context="shared", instructions="plan")'
PREPARE = 'await team.call("prepare", role="experiment", context="shared", instructions="prepare")'
CRITIC = 'return await team.call("evaluate", role="critic", context="critic", instructions="evaluate")'
MESSAGE = 'await team.message("plan", role="plan", context="shared", instructions="refine", handoff={"note": "bounded"})'


def test_shared_context_refinement_matches_default_result(tmp_path):
    baseline, _, _ = run_program(tmp_path, "baseline")
    source = f"""{PLAN}
message = {MESSAGE}
if message["message"] == "refinement available":
    {PLAN}
{PREPARE}
await team.execute()
{CRITIC}"""
    report, agent, output = run_program(tmp_path, "shared", source)
    assert baseline["phase_protocol_pass"] and report["phase_protocol_pass"]
    assert report["correctness"] == baseline["correctness"]
    assert report["correctness"]["status"] == "pass"
    assert [(stage, kw["context"], kw["message"]) for stage, kw in agent.calls] == [
        ("plan", "shared", False),
        ("plan", "shared", True),
        ("plan", "shared", False),
        ("prepare", "shared", False),
        ("evaluate", "critic", False),
    ]
    assert agent.calls[1][1]["handoff"] == {"note": "bounded"}
    assert [call["kind"] for call in report["calls"]] == [
        "delivery",
        "message",
        "delivery",
        "delivery",
        "delivery",
    ]
    assert report["usage"]["coverage"]["complete"]
    assert report["usage"]["total"]["total_tokens"] == 550
    assert [s["tokens"]["total_tokens"] for s in report["stages"]] == [330, 110, 0, 110]
    assert {
        role: value["total_tokens"]
        for role, value in report["usage"]["by_role"].items()
    } == {"plan": 330, "experiment": 110, "critic": 110}
    roster = json.loads((output / "tasks.json").read_text())["tasks"]
    shared = roster[:4]
    assert (
        len({r["thread_id"] for r in shared})
        == len({r["history_path"] for r in shared})
        == 1
    )
    assert all(
        a["history_end_bytes"] == b["history_start_bytes"]
        for a, b in zip(shared, shared[1:])
    )
    assert roster[-1]["thread_id"] != shared[0]["thread_id"]


@pytest.mark.parametrize(
    ("body", "error", "count", "tokens"),
    [
        (PREPARE, "Phase order", 0, 0),
        (
            f"{PLAN}\n{PREPARE}\nawait team.execute()",
            "omitted a required phase",
            2,
            220,
        ),
        (
            f"{PLAN}\n{PREPARE}\nawait team.execute()\nawait team.execute()",
            "only once",
            2,
            220,
        ),
        ("\n".join([PLAN] + [MESSAGE] * 8), "budget exhausted", 8, 880),
    ],
)
def test_protocol_failures_preserve_cost(tmp_path, body, error, count, tokens):
    report, agent, _ = run_program(tmp_path, "failure", body)
    assert not report["phase_protocol_pass"]
    assert report["status"] == "failed"
    assert error in report["error"]
    assert len(agent.calls) == len(report["calls"]) == count
    assert report["usage"]["total"]["total_tokens"] == tokens
    assert report["correctness"]["status"] == "not-evaluable"
