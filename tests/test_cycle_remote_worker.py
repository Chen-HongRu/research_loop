import pytest

from benchmarks.experiment_cycle.remote_worker import validate_candidate


BASE = """\
from torch import nn

def tiny_cnn():
    return nn.Sequential(nn.Conv2d(3, 8, 3), nn.ReLU())

def train():
    return 1
"""


def test_candidate_may_change_layers():
    validate_candidate(BASE, BASE.replace("nn.ReLU()", "nn.GELU(), nn.Dropout(0.1)"))


@pytest.mark.parametrize(
    "candidate",
    [
        BASE.replace("return 1", "return 2"),
        BASE.replace("nn.ReLU()", "os.system(1)"),
        BASE.replace("nn.ReLU()", "nn.ReLU(foo())"),
        BASE.replace("return nn.Sequential", "import os\n    return nn.Sequential"),
        BASE.replace("nn.ReLU()", "nn.ReLU(**{})"),
        BASE.replace("nn.ReLU()", "nn.Sequential(nn.ReLU())"),
        BASE.replace("def tiny_cnn():", "def tiny_cnn(x):"),
    ],
)
def test_candidate_rejects_outside_contract(candidate):
    with pytest.raises(ValueError):
        validate_candidate(BASE, candidate)


def test_busy_preflight_is_read_only_and_not_ready(monkeypatch):
    from types import SimpleNamespace
    from benchmarks.experiment_cycle import remote_worker

    def query(argv, **kwargs):
        text = (
            "0, GPU-example, 600, 23000\n"
            if "--query-gpu=index,uuid,memory.used,memory.free" in argv
            else "12345\n"
        )
        return SimpleNamespace(stdout=text)

    monkeypatch.setattr(remote_worker.subprocess, "run", query)
    result = remote_worker.preflight({"gpu_index": 0, "gpu_uuid": "GPU-example"})
    assert not result["ready"]
    assert "no job started" in result["reason"]


def test_direct_execution_keeps_fixed_command_and_selected_gpu(tmp_path):
    from benchmarks.experiment_cycle.remote_worker import execution_command

    config = {
        "data_root": "/path/to/public/data",
        "controller_root": "/path/to/hidden/controller",
        "gpu_index": 0,
        "gpu_uuid": "GPU-example",
        "python": "/path/to/python",
        "codex_bin": "/path/to/codex",
    }
    argv, env = execution_command(
        config,
        tmp_path / "code",
        tmp_path / "write",
        tmp_path / "code/train.py",
        tmp_path / "write/result",
    )
    assert argv[0] == config["python"]
    assert "bwrap" not in argv
    assert argv[argv.index("--epochs") + 1] == "1"
    assert config["controller_root"] not in argv
    assert env["CUDA_VISIBLE_DEVICES"] == "GPU-example"


def test_failed_cleanup_remains_uncertain(tmp_path, monkeypatch):
    import json
    import subprocess
    from types import SimpleNamespace
    from benchmarks.experiment_cycle import remote_worker as worker

    source = tmp_path / "baseline.py"
    source.write_text(BASE)
    config = {
        "baseline_script": str(source),
        "python": "/example/python",
        "gpu_index": 0,
        "gpu_uuid": "GPU-example",
    }
    monkeypatch.setattr(worker, "operator_config", lambda project: config)
    monkeypatch.setattr(worker, "preflight", lambda config: {"ready": True})
    monkeypatch.setattr(worker, "execution_command", lambda *args: (["fake"], {}))
    monkeypatch.setattr(
        worker.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(["/example/prefix"])),
    )

    class Process:
        pid = 12345

        def poll(self):
            return None

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("fake", timeout)

    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: Process())

    def failed_kill(*args):
        raise OSError("cannot confirm process termination")

    monkeypatch.setattr(worker.os, "killpg", failed_kill)
    receipt = worker.run_operation(tmp_path, "cleanup-test", {"candidate_source": BASE})
    assert receipt["exit_code"] == 1
    assert "cleanup_uncertain" in receipt
    state = json.loads(
        (tmp_path / "evidence/benchmark-baseline/cleanup-test/state.json").read_text()
    )
    assert state["status"] == "uncertain"


def test_gpu_selection_is_operator_allowlisted(tmp_path):
    import json
    from benchmarks.experiment_cycle.remote_worker import operator_config

    path = tmp_path / "evidence/benchmark-collection/config.json"
    path.parent.mkdir(parents=True)
    config = {
        key: str(tmp_path)
        for key in (
            "data_root",
            "controller_root",
            "python",
            "baseline_script",
            "grade_script",
            "codex_bin",
        )
    }
    config.update(
        gpu_uuid="GPU-original",
        gpu_index=0,
        gpu_profiles={
            "GPU-second": {"gpu_index": 2, "gpu_lock": str(tmp_path / "gpu-2.lock")}
        },
    )
    path.write_text(json.dumps(config))
    assert operator_config(tmp_path)["gpu_index"] == 0
    selected = operator_config(tmp_path, "GPU-second")
    assert selected["gpu_uuid"] == "GPU-second"
    assert selected["gpu_index"] == 2
    assert selected["data_root"] == str(tmp_path)
    with pytest.raises(ValueError, match="allowlist"):
        operator_config(tmp_path, "GPU-unapproved")
    config["gpu_profiles"]["GPU-second"]["grade_script"] = "/different/grader"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="profile"):
        operator_config(tmp_path, "GPU-second")
