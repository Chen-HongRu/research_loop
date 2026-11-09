"""Generic SSH process protocol tests, with no model or remote compute."""

import asyncio
import base64
import json
from pathlib import Path

import pytest

from research_loop import loop
from research_loop.ssh_process import SSHProcess


@pytest.fixture
def settings(tmp_path):
    return {
        "adapter": "ssh-process",
        "host": "example.invalid",
        "remote_root": "/project",
        "remote_python": "/project/.venv/bin/python",
        "remote_launcher": "/project/launcher.py",
        "ledger_path": str(tmp_path / "registry.json"),
    }


def run(awaitable):
    return asyncio.run(awaitable)


def test_init_inspect_register_tools_without_network_or_registry_write(
    tmp_path, settings, monkeypatch
):
    async def forbidden(*args, **kwargs):
        raise AssertionError("unexpected SSH")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    project = tmp_path / "project"
    project.mkdir()
    goal = tmp_path / "goal.md"
    goal.write_text("Synthetic offline preparation")
    source = tmp_path / "resource.json"
    source.write_text(json.dumps(settings))
    root = tmp_path / "run"
    loop.initialize(root, project, goal, resource_file=source)
    call = loop.inspect(root)
    assert {tool["name"] for tool in call["tools"]} == {
        "ssh_prepare",
        "ssh_start",
        "ssh_status",
        "ssh_stop",
        "ssh_fetch",
    }
    assert not call["input"]["resource"]["allow_start"]
    assert call["permissions"]["shell_network"] is False
    assert not Path(settings["ledger_path"]).exists()
    assert not Path(settings["ledger_path"] + ".lock").exists()
    source.write_text(json.dumps({**settings, "allow_start": True, "gpu": "0"}))
    adapter = loop.resource(loop.read_config(root), root)
    with pytest.raises(ValueError, match="disabled"):
        run(adapter.start("job", ["python", "-c", "print(1)"]))


def test_start_records_identity_before_effect_and_freezes_device(tmp_path, settings):
    adapter = SSHProcess({**settings, "allow_start": True, "gpu": "2"}, tmp_path)
    calls = []

    async def remote(action, **arguments):
        calls.append((action, arguments))
        stored = json.loads(Path(settings["ledger_path"]).read_text())
        assert stored["entries"][0]["job_id"] == "original"
        assert stored["entries"][0]["status"] == "UNKNOWN"
        return {
            "state": "completed",
            "job_id": "original",
            "exit_code": 0,
            "controller_token": arguments["controller_token"],
        }

    adapter._remote = remote
    run(adapter.start("original", ["python", "-c", "print(1)"]))
    assert calls[0][1]["gpu"] == "2"
    assert not calls[0][1]["cpu_smoke"]
    schema = next(
        tool.parameters for tool in adapter.tools() if tool.name == "ssh_start"
    )
    assert "gpu" not in schema["properties"]
    with pytest.raises(ValueError, match="reserved"):
        run(adapter.start("original", ["python"]))
    assert len(calls) == 1
    assert run(adapter.close())["reservations"][0]["status"] == "TERMINAL"


def test_uncertain_dispatch_reconnects_exact_id_without_retry(tmp_path, settings):
    config = {**settings, "allow_start": True, "cpu_smoke": True}
    adapter = SSHProcess(config, tmp_path)

    async def broken(action, **arguments):
        raise ConnectionError("synthetic disconnect after possible dispatch")

    adapter._remote = broken
    with pytest.raises(ConnectionError):
        run(adapter.start("original", ["python"]))
    assert adapter.status()["reservations"][0]["status"] == "UNKNOWN"
    with pytest.raises(ValueError, match="outstanding"):
        run(adapter.start("replacement", ["python"]))
    adapter._lock.close()
    adapter._lock = None
    resumed = SSHProcess(config, tmp_path)
    calls = []

    async def terminal(action, **arguments):
        calls.append((action, arguments))
        return {
            "state": "failed",
            "job_id": arguments["job_id"],
            "exit_code": 7,
            "controller_token": arguments["controller_token"],
        }

    resumed._remote = terminal
    assert run(resumed.reconcile("original"))["exit_code"] == 7
    assert len(calls) == 1 and calls[0][0] == "status"
    assert calls[0][1]["job_id"] == "original"
    with pytest.raises(ValueError, match="owned"):
        run(resumed.stop("unrecorded"))
    assert run(resumed.close())["reservations"][0]["status"] == "TERMINAL"


def test_close_stops_only_jobs_owned_by_original_run(tmp_path, settings):
    config = {**settings, "allow_start": True, "cpu_smoke": True}
    owner = tmp_path / "owner"
    adapter = SSHProcess(config, owner)

    async def remote(action, **arguments):
        return {
            "job_id": arguments["job_id"],
            "state": "running",
            "controller_token": arguments["controller_token"],
        }

    adapter._remote = remote
    run(adapter.start("original", ["python"]))
    adapter._lock.close()
    adapter._lock = None
    stranger = SSHProcess(config, tmp_path / "other-run")

    async def forbidden(*args, **kwargs):
        raise AssertionError("must not control another run's process")

    stranger._remote = forbidden
    result = run(stranger.close())
    assert result["reservations"][0]["status"] == "UNKNOWN"
    assert result["cleanup_errors"] == []


def test_fetch_copies_only_owned_artifact_and_never_overwrites(tmp_path, settings):
    adapter = SSHProcess({**settings, "allow_start": True, "cpu_smoke": True}, tmp_path)
    content = b"synthetic binary result"

    async def remote(action, **arguments):
        if action == "start":
            return {
                "state": "completed",
                "job_id": "artifact",
                "exit_code": 0,
                "controller_token": arguments["controller_token"],
            }
        return {
            "job_id": "artifact",
            "offset": arguments["offset"],
            "size": len(content),
            "content_base64": base64.b64encode(content).decode(),
        }

    adapter._remote = remote
    run(adapter.start("artifact", ["python"]))
    result = run(adapter.fetch("artifact", "results/value.npz"))
    assert Path(result["local_path"]).read_bytes() == content
    with pytest.raises(FileExistsError):
        run(adapter.fetch("artifact", "results/value.npz"))
    with pytest.raises(ValueError, match="traversal"):
        run(adapter.fetch("artifact", "../outside"))
    with pytest.raises(ValueError, match="owned"):
        run(adapter.fetch("other", "result"))
    (tmp_path / "work/artifacts/artifact/link").symlink_to(tmp_path)
    with pytest.raises(OSError):
        run(adapter.fetch("artifact", "link/outside"))
    run(adapter.close())


def test_stop_and_config_identity_are_preserved(tmp_path, settings):
    config = {**settings, "allow_start": True, "cpu_smoke": True}
    adapter = SSHProcess(config, tmp_path)
    calls = []

    async def remote(action, **arguments):
        calls.append(action)
        return {
            "job_id": "original",
            "state": "starting" if action == "start" else "stopped",
            "controller_token": arguments["controller_token"],
        }

    adapter._remote = remote
    run(adapter.start("original", ["python"]))
    assert run(adapter.close())["reservations"][0]["status"] == "TERMINAL"
    assert calls == ["start", "stop", "status"]
    with pytest.raises(ValueError, match="identity"):
        SSHProcess({**config, "host": "changed.invalid"}, tmp_path)
    with pytest.raises(ValueError, match="frozen"):
        SSHProcess({**settings, "allow_start": True}, tmp_path)


def test_gpu_accounting_readers_mark_job_registry_not_applicable(tmp_path, settings):
    from research_loop.dashboard.server import State
    from research_loop.efficiency import report
    from research_loop.metrics import Collector

    (tmp_path / "config.json").write_text(json.dumps({"resource": settings}))
    (tmp_path / "status.json").write_text("{}")
    # No registry exists yet: the readers must not mistake this for a missing GPU ledger.
    assert State(tmp_path).snapshot()["budget"]["state"] == "not-applicable"
    allocation = report(tmp_path, until="2026-01-01T00:03:00Z")["allocation"]
    assert allocation["state"] == "not-applicable"
    assert allocation["elapsed_seconds"] is None
    with Collector(
        tmp_path,
        tmp_path.parent / (tmp_path.name + "-metrics"),
        since="2026-01-01T00:00:00Z",
        phase="test",
    ) as collector:
        allocation = collector.poll("2026-01-01T00:03:00Z")["allocation"]
        assert allocation["elapsed_seconds"] is None
        assert allocation["dispatched_jobs"] is None
