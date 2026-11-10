"""Real CPU process + scripted specialists; tokens are explicitly synthetic."""

import asyncio
import json
from pathlib import Path
import subprocess
import sys

from benchmarks.experiment_cycle.runner import run

EXAMPLE = (
    Path(__file__).resolve().parents[1]
    / "benchmarks/experiment_cycle/example/case.json"
)


class FixtureAgent:
    def __init__(self, fail=None, premature=False):
        self.called = []
        self.fail = fail
        self.premature = premature

    async def __call__(self, stage, case, workspace, root, identity, model, effort):
        self.called.append(stage)
        history = root / "history.jsonl"
        tid = "fixture-" + stage
        history.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": tid}})
            + "\n"
            + json.dumps(
                {
                    "type": "token_usage_record",
                    "payload": {
                        "response_id": tid,
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
        identity(tid, str(history))
        if stage == self.fail:
            raise RuntimeError("fixture stage failure")
        if stage == "plan":
            return dict.fromkeys(
                ("hypothesis", "control", "change", "evaluation_rule", "outcomes"),
                "bounded numeric fixture",
            )
        if stage == "prepare":
            (workspace / "experiment.py").write_text(
                'import json\nfrom pathlib import Path\nx=json.loads(Path("inputs.json").read_text())\n'
                'rows=[{"id":r["id"],"control_prediction":r["x"],"candidate_prediction":r["x"]+1} for r in x["observations"]]\n'
                'Path("result.json").write_text(json.dumps({"version":1,"observations":rows}))\n'
            )
            if self.premature:
                (workspace / case["result_file"]).write_text("{}")
            return {
                "command": [
                    sys.executable if a == "{python}" else a for a in case["command"]
                ],
                "smoke": "fixture syntax check",
                "artifacts": ["experiment.py"],
            }
        return {
            "disposition": "supported",
            "metrics": {"control_mse": 1.0, "candidate_mse": 0.0, "delta_mse": -1.0},
            "limitations": "synthetic-only",
            "evidence": [case["result_file"]],
        }


async def cpu_executor(case, workspace, root, command, model, effort):
    # Execute a real local process without using native auth in deterministic tests.
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await process.communicate()
    (root / "command.log").write_bytes(stdout)
    return {"exit_code": process.returncode, "log_path": str(root / "command.log")}


def test_failed_stage_preserves_cost_and_stops(tmp_path):
    agent = FixtureAgent(fail="prepare")
    report = asyncio.run(
        run(
            EXAMPLE,
            tmp_path / "run",
            agent=agent,
            executor=cpu_executor,
            measurement_kind="synthetic-fixture",
        )
    )
    assert agent.called == ["plan", "prepare"]
    assert [r["status"] for r in report["stages"]] == [
        "completed",
        "failed",
        "not-run",
        "not-run",
    ]
    assert report["usage"]["total"]["total_tokens"] == 220
    assert report["correctness"]["status"] == "not-evaluable"
    assert (
        report["wall_seconds"]
        == sum(r["wall_seconds"] for r in report["stages"])
        + report["runner_and_check_seconds"]
    )


def test_prepare_cannot_supply_experiment_results(tmp_path):
    report = asyncio.run(
        run(
            EXAMPLE,
            tmp_path / "run",
            agent=FixtureAgent(premature=True),
            executor=cpu_executor,
            measurement_kind="synthetic-fixture",
        )
    )
    assert "before Execute" in report["error"]
    assert report["stages"][2]["status"] == "not-run"


def test_inspect_does_not_require_auth(tmp_path):
    process = subprocess.run(
        [sys.executable, "-m", "benchmarks.experiment_cycle", "inspect", str(EXAMPLE)],
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["source"]["kind"] == "synthetic"


def test_run_requires_explicit_model_flag(tmp_path):
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "benchmarks.experiment_cycle",
            "run",
            str(EXAMPLE),
            "--output",
            str(tmp_path / "run"),
        ],
        capture_output=True,
        text=True,
    )
    assert process.returncode == 2
    assert not (tmp_path / "run").exists()


def test_successful_real_cpu_cycle_reconciles_usage_and_time(tmp_path):
    agent = FixtureAgent()
    report = asyncio.run(
        run(
            EXAMPLE,
            tmp_path / "run",
            agent=agent,
            executor=cpu_executor,
            measurement_kind="synthetic-fixture",
        )
    )
    assert agent.called == ["plan", "prepare", "evaluate"]
    assert report["correctness"]["status"] == "pass"
    assert report["usage"]["coverage"]["complete"]
    assert report["usage"]["total"]["total_tokens"] == 330
    assert sum(r["tokens"]["total_tokens"] for r in report["stages"]) == 330
    assert report["stages"][2]["tokens"]["total_tokens"] == 0
    assert report["stages"][2]["wall_seconds"] > 0
    assert report["usage"]["coordinator"]["present"] is False
    assert report["runner_and_check_seconds"] >= 0
    assert (tmp_path / "run/report.md").is_file()


def test_nonzero_execution_does_not_start_evaluation(tmp_path):
    async def fail(*args):
        return {"exit_code": 7}

    agent = FixtureAgent()
    report = asyncio.run(
        run(
            EXAMPLE,
            tmp_path / "run",
            agent=agent,
            executor=fail,
            measurement_kind="synthetic-fixture",
        )
    )
    assert agent.called == ["plan", "prepare"]
    assert report["stages"][2]["status"] == "failed"
    assert report["stages"][3]["status"] == "not-run"
    assert report["usage"]["total"]["total_tokens"] == 220


def test_reusing_output_does_not_overwrite_evidence(tmp_path):
    import pytest

    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "keep"
    marker.write_text("preserve")
    with pytest.raises((ValueError, FileExistsError)):
        asyncio.run(run(EXAMPLE, output, agent=FixtureAgent(), executor=cpu_executor))
    assert marker.read_text() == "preserve"


def test_missing_usage_is_not_zero_measured(tmp_path):
    class NoUsage(FixtureAgent):
        async def __call__(self, stage, case, workspace, root, identity, model, effort):
            result = await super().__call__(
                stage, case, workspace, root, identity, model, effort
            )
            (root / "history.jsonl").write_text(
                json.dumps(
                    {"type": "session_meta", "payload": {"id": "fixture-" + stage}}
                )
                + "\n"
            )
            return result

    report = asyncio.run(
        run(
            EXAMPLE,
            tmp_path / "run",
            agent=NoUsage(),
            executor=cpu_executor,
            measurement_kind="synthetic-fixture",
        )
    )
    assert report["correctness"]["status"] == "pass"
    assert not report["usage"]["coverage"]["complete"]
    assert not report["usage"]["coverage"]["measurable"]


def test_runtime_start_failure_is_recorded(tmp_path):
    async def broken(*args):
        raise RuntimeError("runtime did not start")

    report = asyncio.run(
        run(
            EXAMPLE,
            tmp_path / "run",
            agent=broken,
            executor=cpu_executor,
            measurement_kind="synthetic-fixture",
        )
    )
    assert report["stages"][0]["status"] == "failed"
    assert not report["usage"]["coverage"]["complete"]
    assert (tmp_path / "run/tasks.json").exists()


def test_frozen_config_disables_search_and_protects_inputs(tmp_path, monkeypatch):
    from benchmarks.experiment_cycle.runner import launch_config

    fake_home = tmp_path / "home"
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / ".codex/auth.json").write_text("{}")
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "prepare"
    config, effective = launch_config(
        root, workspace, writable=[workspace], model="gpt-6.1-sol", effort="medium"
    )
    args = config.launch_args_override
    assert 'web_search="disabled"' in args
    assert "project_doc_max_bytes=0" in args
    assert effective["project_doc_max_bytes"] == 0
    assert 'web_search="live"' not in args
    assert "features.multi_agent=false" in args
    assert "features.multi_agent_v2=false" in args
    assert effective["filesystem"][str(workspace / "inputs.json")] == "read"
    assert effective["filesystem"][str(workspace / ".cycle")] == "read"


def test_native_failure_settles_and_retains_actual_history(tmp_path, monkeypatch):
    from benchmarks.experiment_cycle import runner
    from benchmarks.experiment_cycle.case import load_case, build_workspace
    import pytest

    case = load_case(EXAMPLE)
    workspace = tmp_path / "workspace"
    build_workspace(case, workspace)
    root = tmp_path / "plan"
    root.mkdir()
    order = []
    monkeypatch.setattr(runner, "launch_config", lambda *args, **kwargs: (None, {}))

    class FailedNative:
        def __init__(self, config, journal):
            self.journal = journal
            self.thread_id = "failed-native"
            self.children = set()

        async def open(self):
            pass

        async def verify(self, *args):
            pass

        async def run(self, *args, **kwargs):
            kwargs["on_started"](self.thread_id)
            history = root / "native-history.jsonl"
            history.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": self.thread_id}})
                + "\n"
                + json.dumps(
                    {
                        "type": "token_usage_record",
                        "payload": {
                            "response_id": "paid-before-failure",
                            "usage": {"input_tokens": 200, "output_tokens": 20},
                        },
                    }
                )
                + "\n"
            )
            raise RuntimeError("original native turn failed")

        async def stop(self):
            order.append("stop")

        async def settle(self):
            order.append("settle")
            self.journal.emit(
                "codex.history.reference",
                thread_id=self.thread_id,
                path=str(root / "native-history.jsonl"),
            )
            raise RuntimeError("cleanup still uncertain")

        async def close(self):
            order.append("close")

    monkeypatch.setattr(runner, "Native", FailedNative)
    row = {"stage": "plan", "invoked": True, "thread_id": None, "history_path": None}

    def identity(tid, path):
        row.update(thread_id=tid, history_path=path or row["history_path"])

    with pytest.raises(RuntimeError, match="original native turn failed"):
        asyncio.run(
            runner.NativeAgent()(
                "plan", case, workspace, root, identity, "gpt-6.1-sol", "medium"
            )
        )
    assert order == ["stop", "settle", "close"]
    measured = runner.summarize_usage([row])
    assert measured["total"]["total_tokens"] == 220
    assert measured["coverage"]["complete"]


def test_prepare_schema_excludes_future_results():
    from benchmarks.experiment_cycle.runner import output_schema

    schema = output_schema(
        "prepare", {"command": ["{python}", "train.py"], "result_file": "metrics.json"}
    )
    assert schema["properties"]["artifacts"]["items"]["enum"] == ["train.py"]
    assert schema["properties"]["artifacts"]["minItems"] == 1
