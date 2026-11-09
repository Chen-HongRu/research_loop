import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from research_loop import loop
from research_loop.codex import Native

from test_native import Client, RESULT


def prepare(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    goal = tmp_path / "goal.md"
    goal.write_text("Synthetic CPU only; no model or remote work.")
    root = tmp_path / "run"
    loop.initialize(root, project, goal)
    return root


def test_inspect_is_offline_and_exposes_complete_application_policy(tmp_path):
    root = prepare(tmp_path)
    call = loop.inspect(root)
    assert call["tools"] == []
    assert 'fork_turns="none"' in call["instructions"]
    assert call["output_schema"]["properties"]["goal"]["enum"][-1] == "unknown"
    assert not (root / "native").exists()
    assert not (root / "events.jsonl").exists()


def test_real_cpu_smoke_and_completed_restart_no_replay(tmp_path, monkeypatch):
    root = prepare(tmp_path)
    calls = []

    class CPUClient(Client):
        def turn_start(self, thread_id, prompt, params):
            packet = json.loads(prompt)
            path = Path(packet["work_root"]) / "measurement.json"
            process = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import json,sys;open(sys.argv[1],'w').write(json.dumps({'delta':-1.0}))",
                    str(path),
                ],
                capture_output=True,
            )
            assert process.returncode == 0
            (Path(packet["knowledge_draft"]) / "index.md").write_text(
                "# Synthetic negative result\nDelta is -1.0; not a scientific finding.\n"
            )
            calls.append(path)
            return super().turn_start(thread_id, prompt, params)

    monkeypatch.setattr(
        loop, "configuration", lambda *args: (None, {"synthetic": True})
    )

    def factory(config, journal, tools):
        return Native(config, journal, tools, client_factory=CPUClient)

    state = asyncio.run(loop.run(root, native_factory=factory))
    assert state["status"] == "completed" and state["result"] == RESULT
    assert json.loads(calls[0].read_text()) == {"delta": -1.0}
    assert "negative result" in (root / "knowledge/current/index.md").read_text()
    assert asyncio.run(loop.run(root, native_factory=factory)) == state
    assert len(calls) == 1


def test_failed_turn_preserves_thread_draft_and_no_publication(tmp_path, monkeypatch):
    root = prepare(tmp_path)
    old = (root / "knowledge/current").resolve()

    class BrokenClient(Client):
        def next_turn_notification(self, turn_id):
            raise RuntimeError("synthetic transport failure")

    monkeypatch.setattr(loop, "configuration", lambda *args: (None, {}))

    def factory(config, journal, tools):
        return Native(config, journal, tools, client_factory=BrokenClient)

    import pytest

    with pytest.raises(RuntimeError, match="synthetic transport"):
        asyncio.run(loop.run(root, native_factory=factory))
    state = json.loads((root / "status.json").read_text())
    assert state["status"] == "failed" and state["thread_id"] == "lead"
    assert (root / "knowledge/current").resolve() == old
    assert not (root / "result.json").exists()


def test_resource_identity_is_frozen_relative_to_source_file(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    goal = project / "goal.md"
    goal.write_text("Synthetic")
    config_file = project / "resource.json"
    settings = {
        "host": "example.invalid",
        "partition": "debug",
        "remote_root": "/scratch/research",
        "ledger_path": "original-budget.json",
        "gpu_seconds_total": 600,
        "job_seconds": 60,
    }
    config_file.write_text(json.dumps(settings))
    root = tmp_path / "run"
    loop.initialize(root, project, goal, resource_file=config_file)
    settings["host"] = "changed.invalid"
    settings["ledger_path"] = "replacement-budget.json"
    config_file.write_text(json.dumps(settings))
    call = loop.inspect(root)
    assert call["config"]["resource"]["host"] == "example.invalid"
    assert call["config"]["resource"]["ledger_path"] == str(
        project / "original-budget.json"
    )
    assert not (project / "original-budget.json").exists()


def test_ledger_is_never_placed_in_agent_writable_work(tmp_path):
    import pytest

    project = tmp_path / "project"
    project.mkdir()
    goal = project / "goal"
    goal.write_text("Synthetic")
    root = tmp_path / "run"
    config = project / "resource.json"
    config.write_text(
        json.dumps(
            {
                "host": "example.invalid",
                "partition": "debug",
                "remote_root": "/scratch/research",
                "ledger_path": str(root / "work/budget.json"),
                "gpu_seconds_total": 600,
                "job_seconds": 60,
            }
        )
    )
    with pytest.raises(ValueError, match="outside agent-writable"):
        loop.initialize(root, project, goal, resource_file=config)
    assert not root.exists()


def test_unknown_resource_cleanup_retains_result_without_publishing(
    tmp_path, monkeypatch
):
    root = prepare(tmp_path)
    baseline = (root / "knowledge/current").resolve()

    class Unknown:
        def tools(self):
            return []

        def status(self):
            return {
                "reservations": [{"status": "UNKNOWN", "reservation_id": "original"}]
            }

        async def close(self):
            return self.status()

    monkeypatch.setattr(loop, "resource", lambda *args: Unknown())
    monkeypatch.setattr(loop, "configuration", lambda *args: (None, {}))

    def factory(config, journal, tools):
        return Native(config, journal, tools, client_factory=Client)

    result = asyncio.run(loop.run(root, native_factory=factory))
    assert result["status"] == "needs-reconciliation"
    assert (root / "knowledge/current").resolve() == baseline
    assert json.loads((root / "result.json").read_text()) == RESULT


def scripted_native(monkeypatch, results, on_turn=None, fail_at=None):
    clients = []
    monkeypatch.setattr(loop, "configuration", lambda *args: (None, {}))

    class ScriptedClient(Client):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.number = len(clients) + 1
            clients.append(self)
            notifications = list(self.notifications)
            notifications[1][1]["item"]["text"] = json.dumps(results[self.number - 1])
            self.notifications = iter(notifications)

        def turn_start(self, thread_id, prompt, params):
            self.packet = json.loads(prompt)
            if on_turn:
                on_turn(self.number, self.packet)
            return super().turn_start(thread_id, prompt, params)

        def next_turn_notification(self, turn_id):
            if self.number == fail_at:
                raise RuntimeError("synthetic intermediate failure")
            return super().next_turn_notification(turn_id)

    def factory(config, journal, tools):
        return Native(config, journal, tools, client_factory=ScriptedClient)

    return factory, clients


CONTINUE = {
    **RESULT,
    "goal": "not-achieved",
    "study_action": "continue",
    "stop_reason": "",
    "next_action": "Test the next falsifiable synthetic mechanism.",
}


def test_two_rounds_publish_before_resume_with_one_study_writer(tmp_path, monkeypatch):
    import pytest
    from research_loop.state import writer

    root = prepare(tmp_path)
    initial = (root / "knowledge/current").resolve()
    publication_lock_checks = []
    publish = loop.Knowledge.publish

    def locked_publish(knowledge, baseline, publication=None):
        with pytest.raises(RuntimeError, match="controller writer"):
            with writer(root):
                pass
        publication_lock_checks.append(True)
        return publish(knowledge, baseline, publication=publication)

    monkeypatch.setattr(loop.Knowledge, "publish", locked_publish)

    def on_turn(number, packet):
        assert packet["iteration"] == number
        with pytest.raises(RuntimeError, match="controller writer"):
            with writer(root):
                pass
        if number == 2:
            assert (
                "round 1" in Path(packet["published_knowledge_index_file"]).read_text()
            )
            assert packet["previous_iteration_result"] == CONTINUE
            assert (root / "knowledge/current").resolve() != initial
        (Path(packet["knowledge_draft"]) / "index.md").write_text(
            f"Reviewed synthetic round {number}\n"
        )

    stop = {**RESULT, "goal": "not-achieved"}
    factory, clients = scripted_native(monkeypatch, [CONTINUE, stop], on_turn)
    state = asyncio.run(loop.run(root, native_factory=factory))
    assert state["status"] == "completed" and state["iteration"] == 2
    assert (
        state["result"]["goal"] == "not-achieved"
    )  # The host follows action, not goal.
    assert len(clients) == 2 and all(client.closed == 1 for client in clients)
    assert ("thread/resume", "lead") in clients[1].calls
    assert len(publication_lock_checks) == 2
    for number, result in enumerate((CONTINUE, stop), 1):
        directory = root / "iterations" / f"{number:06d}"
        assert json.loads((directory / "result.json").read_text()) == result
        assert (
            json.loads((directory / "status.json").read_text())["iteration"] == number
        )
    assert asyncio.run(loop.run(root, native_factory=factory)) == state
    assert len(clients) == 2


def test_intermediate_failure_retains_draft_and_stops_next_round(tmp_path, monkeypatch):
    import pytest

    root = prepare(tmp_path)

    def on_turn(number, packet):
        (Path(packet["knowledge_draft"]) / "index.md").write_text(
            f"draft round {number}"
        )

    factory, clients = scripted_native(
        monkeypatch, [CONTINUE, CONTINUE], on_turn, fail_at=2
    )
    with pytest.raises(RuntimeError, match="intermediate failure"):
        asyncio.run(loop.run(root, native_factory=factory))
    state = json.loads((root / "status.json").read_text())
    assert state["status"] == "failed" and state["iteration"] == 2
    assert len(clients) == 2
    assert "round 1" in (root / "knowledge/current/index.md").read_text()
    assert "round 2" in (root / "knowledge/draft/index.md").read_text()
    assert not (root / "iterations/000003").exists()


def test_unknown_intermediate_resource_state_stops_without_publication(
    tmp_path, monkeypatch
):
    root = prepare(tmp_path)
    factory, clients = scripted_native(monkeypatch, [CONTINUE, CONTINUE])

    class Resource:
        def __init__(self):
            self.unknown = len(clients) >= 1

        def tools(self):
            return []

        def status(self):
            return {
                "reservations": [
                    {
                        "status": "UNKNOWN" if self.unknown else "TERMINAL",
                        "reservation_id": "original",
                    }
                ]
            }

        async def close(self):
            return self.status()

    monkeypatch.setattr(loop, "resource", lambda *args: Resource())
    state = asyncio.run(loop.run(root, native_factory=factory))
    assert state["status"] == "needs-reconciliation" and state["iteration"] == 2
    first = json.loads((root / "iterations/000001/status.json").read_text())
    assert (root / "knowledge/current").resolve().name == first["knowledge_version"]
    assert len(clients) == 2 and not (root / "iterations/000003").exists()
    assert json.loads((root / "iterations/000002/result.json").read_text()) == CONTINUE


def test_stop_between_rounds_has_no_next_dispatch(tmp_path, monkeypatch):
    root = prepare(tmp_path)
    publish = loop.Knowledge.publish

    def stop_after_publish(knowledge, baseline, publication=None):
        version = publish(knowledge, baseline, publication=publication)
        (root / "STOP").write_text("operator stop\n")
        return version

    monkeypatch.setattr(loop.Knowledge, "publish", stop_after_publish)
    factory, clients = scripted_native(monkeypatch, [CONTINUE])
    state = asyncio.run(loop.run(root, native_factory=factory))
    assert state["status"] == "stopped" and state["iteration"] == 1
    assert len(clients) == 1 and not (root / "iterations/000002").exists()
    assert (root / "knowledge/current").resolve().name == state["knowledge_version"]


@pytest.mark.parametrize(
    "legacy_status", ["completed", "failed", "stopped", "needs-reconciliation"]
)
def test_legacy_completed_continue_archives_original_records(
    tmp_path, monkeypatch, legacy_status
):
    root = prepare(tmp_path)
    legacy_result = {
        key: value
        for key, value in RESULT.items()
        if key not in {"study_action", "stop_reason"}
    }
    legacy_state = {
        "status": legacy_status,
        "thread_id": "lead",
        "result": legacy_result,
    }
    (root / "result.json").write_text(json.dumps(legacy_result))
    (root / "status.json").write_text(json.dumps(legacy_state))
    original = {
        name: (root / name).read_bytes() for name in ("result.json", "status.json")
    }
    factory, clients = scripted_native(monkeypatch, [RESULT])
    if legacy_status == "completed":
        assert asyncio.run(loop.run(root, native_factory=factory)) == legacy_state
    assert not clients
    state = asyncio.run(loop.run(root, continue_run=True, native_factory=factory))
    assert state["iteration"] == 2 and state["status"] == "completed"
    for name, content in original.items():
        assert (root / "iterations/000001" / name).read_bytes() == content
    assert ("thread/resume", "lead") in clients[0].calls


def test_resume_after_published_round_keeps_thread_and_does_not_replay(
    tmp_path, monkeypatch
):
    import pytest

    root = prepare(tmp_path)
    run_iteration = loop.run_iteration
    interrupted = False

    async def crash_after_publication(*args, **kwargs):
        nonlocal interrupted
        result = await run_iteration(*args, **kwargs)
        if not interrupted:
            interrupted = True
            raise RuntimeError("synthetic crash between iterations")
        return result

    monkeypatch.setattr(loop, "run_iteration", crash_after_publication)
    factory, clients = scripted_native(monkeypatch, [CONTINUE, RESULT])
    with pytest.raises(RuntimeError, match="between iterations"):
        asyncio.run(loop.run(root, native_factory=factory))
    state = json.loads((root / "status.json").read_text())
    assert state["status"] == "iteration-completed" and state["iteration"] == 1
    state = asyncio.run(loop.run(root, native_factory=factory))
    assert state["status"] == "completed" and state["iteration"] == 2
    assert [client.packet["iteration"] for client in clients] == [1, 2]
    assert ("thread/resume", "lead") in clients[1].calls


def test_stop_after_initial_admission_check_records_fresh_iteration(
    tmp_path, monkeypatch
):
    root = prepare(tmp_path)
    journal = loop.Journal

    class StopAtJournal(journal):
        def __init__(self, path):
            super().__init__(path)
            (root / "STOP").write_text("operator stop\n")

    monkeypatch.setattr(loop, "Journal", StopAtJournal)
    factory, clients = scripted_native(monkeypatch, [])
    state = asyncio.run(loop.run(root, native_factory=factory))
    assert state["status"] == "stopped" and state["iteration"] == 1
    assert not clients
    assert json.loads((root / "status.json").read_text()) == state


def test_stop_after_publication_resume_advances_without_overwriting_archive(
    tmp_path, monkeypatch
):
    root = prepare(tmp_path)
    publish = loop.Knowledge.publish
    published = 0

    def stop_after_first_publication(knowledge, baseline, publication=None):
        nonlocal published
        version = publish(knowledge, baseline, publication=publication)
        published += 1
        if published == 1:
            (root / "STOP").write_text("operator stop\n")
        return version

    monkeypatch.setattr(loop.Knowledge, "publish", stop_after_first_publication)
    factory, clients = scripted_native(monkeypatch, [CONTINUE, RESULT])
    state = asyncio.run(loop.run(root, native_factory=factory))
    assert state["status"] == "stopped" and state["iteration"] == 1
    assert state["next_iteration"] == 2
    directory = root / "iterations/000001"
    original = {
        name: (directory / name).read_bytes() for name in ("result.json", "status.json")
    }
    assert json.loads(original["status.json"])["status"] == "iteration-completed"
    resumed = asyncio.run(loop.run(root, continue_run=True, native_factory=factory))
    assert resumed["iteration"] == 2 and resumed["status"] == "completed"
    assert [client.packet["iteration"] for client in clients] == [1, 2]
    assert ("thread/resume", "lead") in clients[1].calls
    assert all(
        (directory / name).read_bytes() == content for name, content in original.items()
    )


def test_active_publication_recovers_pointer_commit_before_status_and_respects_stop(
    tmp_path, monkeypatch
):
    root = prepare(tmp_path)
    publish = loop.Knowledge.publish
    interrupted = False

    def crash_after_pointer(knowledge, baseline, publication=None):
        nonlocal interrupted
        version = publish(knowledge, baseline, publication=publication)
        if not interrupted:
            interrupted = True
            raise RuntimeError("synthetic crash after pointer commit")
        return version

    monkeypatch.setattr(loop.Knowledge, "publish", crash_after_pointer)
    factory, clients = scripted_native(monkeypatch, [CONTINUE, RESULT])
    with pytest.raises(RuntimeError, match="after pointer commit"):
        asyncio.run(loop.run(root, native_factory=factory))
    stale = json.loads((root / "status.json").read_text())
    assert stale["status"] == "failed" and stale["iteration"] == 1
    committed = loop.Knowledge(root).published_state()
    assert committed["iteration"] == 1 and committed["next_iteration"] == 2
    assert committed["status"] == "iteration-completed"
    (root / "STOP").write_text("operator stop\n")
    with pytest.raises(RuntimeError, match="STOP is recorded"):
        asyncio.run(loop.run(root, native_factory=factory))
    assert len(clients) == 1
    first_result = (root / "iterations/000001/result.json").read_bytes()
    state = asyncio.run(loop.run(root, continue_run=True, native_factory=factory))
    assert state["status"] == "completed" and state["iteration"] == 2
    assert [client.packet["iteration"] for client in clients] == [1, 2]
    assert (root / "iterations/000001/result.json").read_bytes() == first_result


def test_active_older_publication_does_not_replace_newer_failed_iteration(
    tmp_path, monkeypatch
):
    root = prepare(tmp_path)
    first_factory, first_clients = scripted_native(
        monkeypatch, [CONTINUE, CONTINUE], fail_at=2
    )
    with pytest.raises(RuntimeError, match="intermediate failure"):
        asyncio.run(loop.run(root, native_factory=first_factory))
    assert len(first_clients) == 2
    assert loop.Knowledge(root).published_state()["iteration"] == 1
    factory, clients = scripted_native(monkeypatch, [RESULT])
    state = asyncio.run(loop.run(root, native_factory=factory))
    assert state["iteration"] == 2 and state["status"] == "completed"
    assert clients[0].packet["iteration"] == 2
    assert ("thread/resume", "lead") in clients[0].calls


def test_stop_during_publication_copy_retains_draft_without_next_iteration(
    tmp_path, monkeypatch
):
    import shutil

    root = prepare(tmp_path)
    baseline = (root / "knowledge/current").resolve()
    copytree = shutil.copytree

    def stop_after_copy(*args, **kwargs):
        result = copytree(*args, **kwargs)
        (root / "STOP").write_text("operator stop\n")
        return result

    def on_turn(number, packet):
        (Path(packet["knowledge_draft"]) / "index.md").write_text(
            "retained iteration draft"
        )

    monkeypatch.setattr("research_loop.state.shutil.copytree", stop_after_copy)
    factory, clients = scripted_native(monkeypatch, [CONTINUE], on_turn)
    state = asyncio.run(loop.run(root, native_factory=factory))
    assert state["status"] == "stopped" and state["iteration"] == 1
    assert len(clients) == 1
    assert (root / "knowledge/current").resolve() == baseline
    assert (root / "knowledge/draft/index.md").read_text() == "retained iteration draft"
    assert json.loads((root / "iterations/000001/result.json").read_text()) == CONTINUE
    assert not (root / "iterations/000002").exists()


def test_scheduled_signal_during_snapshot_copy_prevents_activation(
    tmp_path, monkeypatch
):
    import os
    import shutil
    import signal
    import threading
    import time

    root = prepare(tmp_path)
    baseline = (root / "knowledge/current").resolve()
    copytree = shutil.copytree
    copying = threading.Event()
    factory, clients = scripted_native(monkeypatch, [RESULT])

    async def check():
        active_loop = asyncio.get_running_loop()

        def send_signal():
            # A regressed synchronous copy must fail safely before this callback runs.
            if copying.is_set():
                os.kill(os.getpid(), signal.SIGTERM)

        def copy_while_signal_is_scheduled(*args, **kwargs):
            result = copytree(*args, **kwargs)
            copying.set()
            active_loop.call_soon_threadsafe(active_loop.call_later, 0.01, send_signal)
            try:
                deadline = time.monotonic() + 2
                while not (root / "STOP").exists() and time.monotonic() < deadline:
                    time.sleep(0.005)
                assert (root / "STOP").exists(), (
                    "signal handling was blocked by snapshot copying"
                )
            finally:
                copying.clear()
            return result

        monkeypatch.setattr(
            "research_loop.state.shutil.copytree", copy_while_signal_is_scheduled
        )
        return await loop.run(root, native_factory=factory)

    state = asyncio.run(check())
    assert state["status"] == "stopped" and len(clients) == 1
    assert (root / "STOP").exists()
    assert (root / "knowledge/current").resolve() == baseline
    assert (root / "knowledge/draft/index.md").exists()
    assert not list((root / "knowledge/publications").glob("*.json"))
