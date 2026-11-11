"""Dataset preparation and occupied-GPU refusal never start models/training."""

import asyncio
import json
from pathlib import Path

from benchmarks.experiment_cycle import collection


def deployment(tmp_path):
    (tmp_path / "starting.py").write_text(
        "from torch import nn\ndef tiny_cnn():\n return nn.Sequential(nn.ReLU())\n"
    )
    (tmp_path / "metadata.json").write_text("{}")
    config = {
        "starting_source": "starting.py",
        "data_metadata": "metadata.json",
        "remote": {
            "ssh_host": "example-host",
            "project_root": "/path/to/project",
            "data_root": "/path/to/public/data",
            "controller_root": "/path/to/private/controller",
            "python": "/path/to/python",
            "gpu_uuid": "GPU-example",
        },
    }
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(config))
    return path


def test_prepare_ten_distinct_cycles_without_dispatch(tmp_path, monkeypatch):
    async def no_remote(*args, **kwargs):
        raise AssertionError("prepare must not dispatch")

    monkeypatch.setattr(collection, "remote", no_remote)
    result = collection.prepare(deployment(tmp_path), tmp_path / "cases")
    spec = json.loads(Path(result["manifest"]).read_text())
    assert result["status"] == "prepared-not-started"
    assert spec["model"] == "gpt-6.1-terra"
    assert len(spec["cases"]) == spec["target_cycles"] == 10
    tasks = [
        (tmp_path / "cases" / case).parent.joinpath("task.md").read_text()
        for case in spec["cases"]
    ]
    assert len(set(tasks)) == 10
    assert all("one epoch" in task and "BCEWithLogitsLoss" in task for task in tasks)


def test_occupied_card_stops_before_model_or_output(tmp_path, monkeypatch):
    collection.prepare(deployment(tmp_path), tmp_path / "cases")
    calls = []

    async def busy(config, action, *args, **kwargs):
        calls.append(action)
        return {"ready": False, "reason": "occupied"}

    async def no_model(*args, **kwargs):
        raise AssertionError("occupied GPU must be checked before model dispatch")

    monkeypatch.setattr(collection, "remote", busy)
    monkeypatch.setattr(collection, "run", no_model)
    output = tmp_path / "runs"
    result = asyncio.run(collection.collect(tmp_path / "cases", output))
    assert result == {
        "status": "blocked",
        "reason": "occupied",
        "model_calls": 0,
        "cycles_completed": 0,
    }
    assert calls == ["preflight"]
    assert not output.exists()


def test_parallel_workers_stop_after_failure_and_retain_costs(tmp_path, monkeypatch):
    path = deployment(tmp_path)
    config = json.loads(path.read_text())
    config["remote"]["gpu_uuids"] = ["GPU-0", "GPU-2", "GPU-5"]
    path.write_text(json.dumps(config))
    collection.prepare(path, tmp_path / "cases")
    _, cases = collection.load_collection(tmp_path / "cases")
    assert [case["remote"]["gpu_uuid"] for case in cases] == [
        "GPU-0",
        "GPU-2",
        "GPU-5",
        "GPU-0",
        "GPU-2",
        "GPU-5",
        "GPU-0",
        "GPU-2",
        "GPU-5",
        "GPU-0",
    ]
    running = set()
    started = []
    all_started = asyncio.Event()

    async def ready(*args, **kwargs):
        return {"ready": True}

    async def runner(path, output, **kwargs):
        case = collection.cactus_case.load_case(path)
        gpu = case["remote"]["gpu_uuid"]
        assert gpu not in running
        running.add(gpu)
        started.append(gpu)
        if len(started) == 3:
            all_started.set()
        await all_started.wait()
        if gpu != "GPU-0":
            await asyncio.sleep(0.01)
        running.remove(gpu)
        return {
            "status": "failed" if gpu == "GPU-0" else "completed",
            "phase_protocol_pass": True,
            "correctness": {"status": "pass"},
            "usage": {"coverage": {"complete": True}, "total": {"total_tokens": 7}},
            "wall_seconds": 2,
        }

    monkeypatch.setattr(collection, "remote", ready)
    monkeypatch.setattr(collection, "run", runner)
    result = asyncio.run(collection.collect(tmp_path / "cases", tmp_path / "runs"))
    assert len(started) == 3
    assert result["status"] == "needs-attention"
    assert result["cycles_completed"] == 2
    assert result["recorded_tokens"] == 21
    assert result["cycle_wall_seconds"] == 6
    assert result["batch_wall_seconds"] >= 0
    assert {a["gpu_uuid"] for a in result["attempts"]} == set(started)
    assert json.loads((tmp_path / "runs/collection-status.json").read_text()) == result


def test_exception_retains_written_report_costs(tmp_path, monkeypatch):
    collection.prepare(deployment(tmp_path), tmp_path / "cases")

    async def ready(*args, **kwargs):
        return {"ready": True}

    async def runner(path, output, **kwargs):
        output.mkdir()
        (output / "report.json").write_text(
            json.dumps({"usage": {"total": {"total_tokens": 13}}, "wall_seconds": 4})
        )
        raise RuntimeError("post-report failure")

    monkeypatch.setattr(collection, "remote", ready)
    monkeypatch.setattr(collection, "run", runner)
    result = asyncio.run(collection.collect(tmp_path / "cases", tmp_path / "runs"))
    assert result["status"] == "needs-attention"
    assert result["recorded_tokens"] == 13
    assert result["cycle_wall_seconds"] == 4
    assert len(result["attempts"]) == 1


def test_remote_command_parses_optional_gpu_and_operation(tmp_path, monkeypatch):
    import shlex
    import subprocess
    import sys

    commands = []

    class Process:
        returncode = 0

        async def communicate(self, data):
            return b"{}", b""

    async def start(*args, **kwargs):
        commands.append(shlex.split(args[-1]))
        return Process()

    monkeypatch.setattr(collection.asyncio, "create_subprocess_exec", start)
    config = {
        "python": sys.executable,
        "project_root": str(Path(__file__).resolve().parents[1]),
        "ssh_host": "example-host",
        "gpu_uuid": "GPU-example",
    }
    asyncio.run(collection.remote(config, "status", "not-an-operation"))
    argv = commands[0]
    argv[argv.index("--project") + 1] = str(tmp_path)
    result = subprocess.run(argv, capture_output=True, text=True)
    # Missing status is an application JSON error, not argparse's exit 2.
    assert result.returncode == 1
    assert "FileNotFoundError" in json.loads(result.stdout)["error"]


def test_carry_verified_cycle_retains_cost_without_rerun(tmp_path, monkeypatch):
    collection.prepare(deployment(tmp_path), tmp_path / "cases")
    case = collection.load_collection(tmp_path / "cases")[1][0]
    prior = tmp_path / "prior"
    prior.mkdir()
    (prior / "orchestration.py").write_text(collection.load_orchestration().source)
    report = {
        "case_id": case["case_id"],
        "status": "completed",
        "phase_protocol_pass": True,
        "correctness": {"status": "pass"},
        "configuration": {"model": collection.MODEL},
        "stages": [
            {"stage": s, "status": "completed"}
            for s in ("plan", "prepare", "execute", "evaluate")
        ],
        "usage": {"coverage": {"complete": True}, "total": {"total_tokens": 100}},
        "wall_seconds": 2.0,
    }
    path = prior / "report.json"
    path.write_text(json.dumps(report))
    calls = []

    async def ready(*args, **kwargs):
        return {"ready": True}

    async def run(path, *args, **kwargs):
        calls.append(collection.cactus_case.load_case(path)["case_id"])
        return report

    monkeypatch.setattr(collection, "remote", ready)
    monkeypatch.setattr(collection, "run", run)
    result = asyncio.run(
        collection.collect(
            tmp_path / "cases", tmp_path / "output", completed_reports=[path]
        )
    )
    assert case["case_id"] not in calls
    assert len(calls) == 9
    assert result["cycles_completed"] == 10
    assert result["carried_cycles"] == 1
    assert result["recorded_tokens"] == 1000
    assert result["cycle_wall_seconds"] == 20
    assert result["attempts"][0]["carried_forward"] is True


def test_lost_transport_recovers_original_receipt_without_redispatch(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "train.py").write_text("synthetic")
    output = tmp_path / "execute"
    output.mkdir()
    calls = []

    async def query(config, action, identity=None, request=None):
        calls.append((action, identity))
        if action == "run":
            raise RuntimeError("remote transport returned no receipt (exit 255)")
        return {
            "exit_code": 0,
            "operation_id": identity,
            "baseline": {"score": 0.5},
            "candidate": {"score": 0.6},
        }

    monkeypatch.setattr(collection, "remote", query)
    case = {"remote": {}, "limits": {"execute_seconds": 5}}
    result = asyncio.run(
        collection.RemoteExecutor()(case, workspace, output, [], None, None)
    )
    assert result["exit_code"] == 0
    assert [action for action, _ in calls] == ["run", "reconcile"]
    assert calls[0][1] == calls[1][1]
    assert json.loads((workspace / "metrics.json").read_text())["candidate_auc"] == 0.6


def test_undispatched_fence_allows_fresh_identity_only(tmp_path, monkeypatch):
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "train.py").write_text("synthetic")
    output = tmp_path / "execute"
    output.mkdir()
    calls = []

    async def query(config, action, identity=None, request=None):
        calls.append((action, identity))
        if len(calls) == 1:
            raise RuntimeError("remote transport returned no receipt (exit 255)")
        if action == "reconcile":
            return {
                "exit_code": 1,
                "operation_id": identity,
                "status": "not_dispatched",
                "dispatch_fenced": True,
            }
        return {
            "exit_code": 0,
            "operation_id": identity,
            "baseline": {"score": 0.5},
            "candidate": {"score": 0.6},
        }

    async def no_delay(seconds):
        pass

    monkeypatch.setattr(collection, "remote", query)
    monkeypatch.setattr(collection.asyncio, "sleep", no_delay)
    case = {"remote": {}, "limits": {"execute_seconds": 5}}
    result = asyncio.run(
        collection.RemoteExecutor()(case, workspace, output, [], None, None)
    )
    assert result["exit_code"] == 0
    assert [action for action, _ in calls] == ["run", "reconcile", "run"]
    assert calls[0][1] != calls[2][1]
    operation = json.loads((output / "remote-operation.json").read_text())
    assert operation["attempts"][0]["receipt"]["dispatch_fenced"] is True


def test_unknown_operation_is_not_redispatched(tmp_path, monkeypatch):
    import pytest

    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "train.py").write_text("synthetic")
    output = tmp_path / "execute"
    output.mkdir()
    calls = []

    async def query(config, action, identity=None, request=None):
        calls.append(action)
        if action == "run":
            raise RuntimeError("remote transport returned no receipt (exit 255)")
        return {"status": "admitted", "operation_id": identity}

    monkeypatch.setattr(collection, "remote", query)
    case = {"remote": {}, "limits": {"execute_seconds": 5}}
    with pytest.raises(RuntimeError, match="no receipt"):
        asyncio.run(
            collection.RemoteExecutor()(case, workspace, output, [], None, None)
        )
    assert calls == ["run", "reconcile"]


def test_fenced_retry_rechecks_dispatch_deadline(tmp_path, monkeypatch):
    import pytest

    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "train.py").write_text("synthetic")
    output = tmp_path / "execute"
    output.mkdir()
    calls = []

    async def query(config, action, identity=None, request=None):
        calls.append(action)
        if action == "run":
            raise RuntimeError("remote transport returned no receipt (exit 255)")
        return {
            "exit_code": 1,
            "operation_id": identity,
            "status": "not_dispatched",
            "dispatch_fenced": True,
        }

    async def no_delay(seconds):
        pass

    monkeypatch.setattr(collection, "remote", query)
    monkeypatch.setattr(collection.asyncio, "sleep", no_delay)
    case = {"remote": {}, "limits": {"execute_seconds": 5}}
    with pytest.raises(RuntimeError, match="stopped before admission"):
        asyncio.run(
            collection.RemoteExecutor(can_dispatch=lambda: not calls)(
                case, workspace, output, [], None, None
            )
        )
    assert calls == ["run", "reconcile"]
