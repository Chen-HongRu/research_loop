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


def test_sandbox_has_no_hidden_controller_permission(tmp_path):
    from benchmarks.experiment_cycle.remote_worker import sandbox_command

    config = {
        "data_root": "/path/to/public/data",
        "controller_root": "/path/to/hidden/controller",
        "gpu_index": 0,
        "gpu_uuid": "GPU-example",
        "python": "/path/to/python",
        "codex_bin": "/path/to/codex",
    }
    argv, env = sandbox_command(
        config,
        tmp_path / "code",
        tmp_path / "write",
        tmp_path / "code/train.py",
        tmp_path / "write/result",
        tmp_path / "home",
        ["/path/to/python-prefix"],
    )
    assert "--unshare-net" in argv
    assert "--tmpfs" in argv
    assert argv[argv.index("--tmpfs") + 1] == config["controller_root"]
    # Tests do not depend on local GPU device files existing.
    assert "/dev/nvidia1" not in argv
    assert ["--ro-bind", "/path/to/public/data", "/path/to/public/data"] == argv[
        argv.index("/path/to/public/data") - 1 : argv.index("/path/to/public/data") + 2
    ]
    assert env["CUDA_VISIBLE_DEVICES"] == "GPU-example"


def test_unavailable_sandbox_blocks_before_gpu_query(monkeypatch):
    import subprocess
    from benchmarks.experiment_cycle import remote_worker

    calls = []

    def denied(argv, **kwargs):
        calls.append(argv)
        raise subprocess.CalledProcessError(1, argv, stderr="namespace denied")

    monkeypatch.setattr(remote_worker.subprocess, "run", denied)
    result = remote_worker.preflight({})
    assert result["ready"] is False
    assert "sandbox unavailable" in result["reason"]
    assert len(calls) == 1


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
    monkeypatch.setattr(worker, "sandbox_command", lambda *args: (["fake"], {}))
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
