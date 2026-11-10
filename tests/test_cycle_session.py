"""The window supervisor uses wall time, retains costs, and never models its polling."""

import json
from types import SimpleNamespace

import pytest

from benchmarks.experiment_cycle import session


def fixture(tmp_path, monkeypatch):
    study = tmp_path / "study"
    study.mkdir()
    (study / "evaluator.json").write_text(json.dumps({"max_evaluations": None}))
    (study / "status.json").write_text(json.dumps({"status": "completed"}))
    (study / "evaluation-ledger.json").write_text(json.dumps({"charged": 7}))
    clock = SimpleNamespace(now=1000)
    monkeypatch.setattr(session.time, "time", lambda: clock.now)
    monkeypatch.setattr(
        session.time,
        "sleep",
        lambda seconds: setattr(clock, "now", clock.now + seconds),
    )

    async def ready(*args):
        return None

    monkeypatch.setattr(session, "readiness", ready)
    monkeypatch.setattr(session, "settlement", ready)
    return study, clock


def test_window_resumes_finished_lead_without_count_cap_and_keeps_ledger(
    tmp_path, monkeypatch
):
    study, clock = fixture(tmp_path, monkeypatch)
    starts = []

    def start(argv, **kwargs):
        starts.append(argv)
        return SimpleNamespace(pid=999, returncode=0, poll=lambda: 0)

    monkeypatch.setattr(session.subprocess, "Popen", start)
    result = session.supervise(study, 4 / 3600)
    assert result["status"] == "completed"
    assert len(starts) == 4
    assert result["deadline_unix"] == 1004
    assert all("--continue" in argv and "--preserve-stop" in argv for argv in starts)
    assert json.loads((study / "evaluation-ledger.json").read_text()) == {"charged": 7}
    assert (study / "STOP").exists()


def test_existing_window_does_not_get_extended_by_restart(tmp_path, monkeypatch):
    study, clock = fixture(tmp_path, monkeypatch)
    original = session.prepare_window(study, 10)
    clock.now += 120
    assert (
        session.prepare_window(study, 10)["deadline_unix"] == original["deadline_unix"]
    )


def test_deadline_interrupts_active_owned_controller(tmp_path, monkeypatch):
    study, clock = fixture(tmp_path, monkeypatch)

    class Child:
        pid = 1234
        returncode = None
        signals = []

        def poll(self):
            return self.returncode

        def send_signal(self, value):
            self.signals.append(value)

        def wait(self, timeout):
            self.returncode = 0
            return 0

    child = Child()
    monkeypatch.setattr(session.subprocess, "Popen", lambda *args, **kwargs: child)
    result = session.supervise(study, 2 / 3600)
    assert result["status"] == "completed"
    assert child.signals == [session.signal.SIGINT]
    assert (study / "STOP").read_text() == "authorized time window ended\n"


def test_operator_stop_is_not_reopened(tmp_path, monkeypatch):
    study, clock = fixture(tmp_path, monkeypatch)
    (study / "STOP").write_text("user stop")
    with pytest.raises(ValueError, match="Operator STOP"):
        session.supervise(study, 10)


def test_unexpected_controller_failure_stops_for_diagnosis(tmp_path, monkeypatch):
    study, clock = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        session.subprocess,
        "Popen",
        lambda *args, **kwargs: SimpleNamespace(pid=999, returncode=1, poll=lambda: 1),
    )
    result = session.supervise(study, 10)
    assert result["status"] == "blocked"
    assert result["exit_code"] == 1


def test_unknown_remote_at_deadline_is_not_clean_completion(tmp_path, monkeypatch):
    study, clock = fixture(tmp_path, monkeypatch)

    async def unknown(*args):
        return {"reconciliation_required": True}

    monkeypatch.setattr(session, "settlement", unknown)
    monkeypatch.setattr(
        session.subprocess,
        "Popen",
        lambda *args, **kwargs: SimpleNamespace(pid=999, returncode=0, poll=lambda: 0),
    )
    result = session.supervise(study, 1 / 3600)
    assert result["status"] == "needs-reconciliation"


def test_real_owned_cpu_process_settles_at_window_end(tmp_path, monkeypatch):
    import sys

    study = tmp_path / "study"
    study.mkdir()
    (study / "evaluator.json").write_text(json.dumps({"max_evaluations": None}))
    (study / "status.json").write_text(json.dumps({"status": "stopped"}))

    async def ready(*args):
        return None

    monkeypatch.setattr(session, "readiness", ready)
    monkeypatch.setattr(session, "settlement", ready)
    real_popen = session.subprocess.Popen
    children = []

    def start(argv, **kwargs):
        process = real_popen(
            [
                sys.executable,
                "-c",
                "import signal,time,sys;signal.signal(signal.SIGINT,lambda *args:sys.exit(0));time.sleep(30)",
            ],
            **kwargs,
        )
        children.append(process)
        return process

    monkeypatch.setattr(session.subprocess, "Popen", start)
    result = session.supervise(study, 1.2 / 3600)
    assert result["status"] == "completed"
    assert len(children) == 1 and children[0].returncode == 0
    assert (study / "STOP").exists()
