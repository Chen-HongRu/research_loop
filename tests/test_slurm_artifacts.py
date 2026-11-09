"""Prepared script transport and bounded reads use only local synthetic files."""

import asyncio
import json
import os
import shlex
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from research_loop import loop
from research_loop.slurm import REMOTE_READ, SCRIPT_BYTES, Slurm
from test_slurm import FAKE_SSH


@asynccontextmanager
async def resource_fixture(tmp_path, monkeypatch, *, block_script_drain=False):
    study, remote = tmp_path / "study", tmp_path / "remote"
    (study / "work").mkdir(parents=True)
    remote.mkdir()
    script = tmp_path / "fake_ssh.py"
    # Capture the actual stdin bytes, then run the same Bash invocation with EOF.
    # This modifies the synthetic transport only, never a real remote host.
    fake = FAKE_SSH.replace(
        'elif command == "srun":',
        """elif command == "python3":
    os.execv(sys.executable, [sys.executable, *argv[1:]])
elif command == "srun":
    if argv[-1].endswith("exec bash -s --"):
        import subprocess
        data = sys.stdin.buffer.read()
        (root / "received-script").write_bytes(data)
        index = argv.index("bash")
        sys.exit(subprocess.run(argv[index:], input=data).returncode)
""",
    )
    script.write_text(fake)
    config = {
        "host": "example.invalid",
        "partition": "debug",
        "remote_root": str(remote),
        "ledger_path": str(tmp_path / "budget.json"),
        "gpu_seconds_total": 600,
        "job_seconds": 120,
    }
    calls, create = [], asyncio.create_subprocess_exec
    drain_entered = asyncio.Event()

    async def spawn(*argv, **kwargs):
        if argv[0] == "bash":
            return await create(*argv, **kwargs)
        assert argv[0] == "ssh"
        calls.append(shlex.split(argv[-1]))
        child = await create(
            sys.executable, str(script), str(remote), argv[-1], **kwargs
        )
        if block_script_drain and calls[-1][0] == "srun":
            original_drain = child.stdin.drain

            async def blocked_drain():
                await original_drain()
                drain_entered.set()
                await asyncio.Event().wait()

            child.stdin.drain = blocked_drain
        return child

    monkeypatch.setattr("research_loop.slurm.asyncio.create_subprocess_exec", spawn)
    adapter = Slurm(config, study)
    try:
        yield SimpleNamespace(
            adapter=adapter,
            study=study,
            remote=remote,
            calls=calls,
            ledger=Path(config["ledger_path"]),
            drain_entered=drain_entered,
        )
    finally:
        await adapter.close()


async def invoke(adapter, name, **arguments):
    tool = next(tool for tool in adapter.tools() if tool.name == name)
    return await tool.call(arguments)


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "outside",
        "work_symlink",
        "escape",
        "directory",
        "fifo",
        "oversize",
        "empty",
        "nul",
        "syntax",
    ],
)
def test_invalid_scripts_fail_before_any_allocation(tmp_path, monkeypatch, kind):
    async def check():
        async with resource_fixture(tmp_path, monkeypatch) as env:
            path = env.study / "work/prepared.sh"
            if kind == "outside":
                path = tmp_path / "outside.sh"
                path.write_text("true\n")
            elif kind == "work_symlink":
                outside = tmp_path / "outside-work"
                outside.mkdir()
                (outside / path.name).write_text("true\n")
                path.parent.rmdir()
                path.parent.symlink_to(outside, target_is_directory=True)
            elif kind == "escape":
                outside = tmp_path / "outside.sh"
                outside.write_text("true\n")
                path.symlink_to(outside)
            elif kind == "directory":
                path.mkdir()
            elif kind == "fifo":
                os.mkfifo(path)
            elif kind == "oversize":
                with path.open("wb") as stream:
                    stream.truncate(SCRIPT_BYTES + 1)
            elif kind == "empty":
                path.write_bytes(b" \n")
            elif kind == "nul":
                path.write_bytes(b"true\0\n")
            elif kind == "syntax":
                path.write_text("printf should-not-execute > touched\nif then\n")
            with pytest.raises((ValueError, OSError)):
                await invoke(env.adapter, "gpu_exec_script", script_path=str(path))
            assert not env.calls and not env.ledger.exists()
            assert env.adapter._ledger is None
            assert not (env.remote / "scheduler.json").exists()
            assert not (env.remote / "touched").exists()

    asyncio.run(check())


@pytest.mark.parametrize("script_bytes", [19000, SCRIPT_BYTES])
def test_script_uses_exact_prepared_bytes_and_eof_without_argv_payload(
    tmp_path, monkeypatch, script_bytes
):
    async def check():
        async with resource_fixture(tmp_path, monkeypatch) as env:
            prefix = b"#!/bin/bash\n# "
            suffix = b"\nprintf 'prepared-ok\\n'\nread -r unused && exit 9 || printf 'EOF\\n'\n"
            prepared = (
                prefix + b"x" * (script_bytes - len(prefix) - len(suffix)) + suffix
            )
            assert len(prepared) == script_bytes
            path = env.study / "work/prepared script.sh"
            path.write_bytes(prepared)
            (env.study / "work/alias.sh").symlink_to(path)
            result = await asyncio.wait_for(
                invoke(env.adapter, "gpu_exec_script", script_path="alias.sh"), 5
            )
            assert not result["running"] and result["exit_code"] == 0
            assert result["output"] == "prepared-ok\nEOF\n"
            assert (env.remote / "received-script").read_bytes() == prepared
            command = next(call for call in env.calls if call[0] == "srun")
            assert "x" * 100 not in " ".join(command)
            assert command[-1].endswith("exec bash -s --")
            receipt = await invoke(env.adapter, "gpu_release")
            assert receipt["elapsed_seconds"] == 7
            assert env.adapter.status()["budget"]["used_seconds"] == 7

    asyncio.run(check())


def test_script_poll_rejects_input_but_preserves_completion(tmp_path, monkeypatch):
    async def check():
        async with resource_fixture(tmp_path, monkeypatch) as env:
            (env.study / "work/prepared.sh").write_text("sleep 1.5\nprintf done\n")
            result = await invoke(
                env.adapter, "gpu_exec_script", script_path="prepared.sh"
            )
            assert result["running"]
            with pytest.raises(ValueError, match="noninteractive"):
                await invoke(
                    env.adapter,
                    "gpu_poll",
                    session_id=result["session_id"],
                    chars="bad\n",
                )
            output = result["output"]
            while result["running"]:
                result = await invoke(
                    env.adapter, "gpu_poll", session_id=result["session_id"]
                )
                output += result["output"]
            assert result["exit_code"] == 0 and output == "done"

    asyncio.run(asyncio.wait_for(check(), 6))


def test_remote_small_tail_and_replacement_reads_leave_ledger_untouched(
    tmp_path, monkeypatch
):
    async def check():
        async with resource_fixture(tmp_path, monkeypatch) as env:
            unusual = env.remote / "summary'; no-shell-command; '.json"
            unusual.write_text('{"status":"synthetic"}\n')
            result = await invoke(env.adapter, "remote_read", path=str(unusual))
            assert result["complete"] and not result["truncated"]
            assert json.loads(result["content"]) == {"status": "synthetic"}
            assert result["bytes_read"] == unusual.stat().st_size
            assert result["offset"] == 0
            assert not env.ledger.exists() and env.adapter._ledger is None
            # Even STOP and an existing ledger do not turn observation into dispatch.
            env.adapter._open()
            before = env.ledger.read_bytes(), env.ledger.stat().st_mtime_ns
            (env.study / "STOP").write_text("synthetic operator stop\n")
            (env.remote / "run.log").write_bytes(b"0123456789")
            tail = await invoke(
                env.adapter, "remote_read", path="run.log", max_bytes=4, tail=True
            )
            head = await invoke(env.adapter, "remote_read", path="run.log", max_bytes=4)
            assert (tail["offset"], tail["bytes_read"], tail["content"]) == (
                6,
                4,
                "6789",
            )
            assert (head["offset"], head["content"]) == (0, "0123")
            assert tail["truncated"] and not tail["complete"]
            (env.remote / "bytes.log").write_bytes(b"a\xffb")
            replacement = await invoke(env.adapter, "remote_read", path="bytes.log")
            assert (
                replacement["content"] == "a\ufffdb" and replacement["bytes_read"] == 3
            )
            assert before == (env.ledger.read_bytes(), env.ledger.stat().st_mtime_ns)
            assert env.adapter.status()["reservations"] == []
            assert all(call[0] == "python3" for call in env.calls)

    asyncio.run(check())


def test_remote_metadata_only_never_opens_file_contents_and_maximum_window(
    tmp_path, monkeypatch
):
    async def check():
        async with resource_fixture(tmp_path, monkeypatch) as env:
            path = env.remote / "synthetic-checkpoint.bin"
            with path.open("wb") as stream:
                stream.truncate(131072)
            path.chmod(0)
            try:
                result = await invoke(
                    env.adapter, "remote_read", path=path.name, metadata_only=True
                )
            finally:
                path.chmod(0o600)
            assert (
                result["size_bytes"] == 131072
                and result["mtime_ns"] == path.stat().st_mtime_ns
            )
            assert result["metadata_only"] and result["bytes_read"] == 0
            assert result["content"] is None and not result["complete"]
            window = await invoke(
                env.adapter, "remote_read", path=path.name, max_bytes=65536
            )
            assert window["bytes_read"] == 65536 and window["truncated"]
            assert not env.ledger.exists()
            assert all(call[0] == "python3" for call in env.calls)

    asyncio.run(check())


@pytest.mark.parametrize("kind", ["outside", "escape", "directory", "fifo", "missing"])
def test_remote_read_rejects_escape_and_nonregular_files_without_allocation(
    tmp_path, monkeypatch, kind
):
    async def check():
        async with resource_fixture(tmp_path, monkeypatch) as env:
            path = env.remote / "artifact"
            if kind in {"outside", "escape"}:
                outside = tmp_path / "outside.txt"
                outside.write_text("synthetic")
                if kind == "outside":
                    path = outside
                else:
                    path.symlink_to(outside)
            elif kind == "directory":
                path.mkdir()
            elif kind == "fifo":
                os.mkfifo(path)
            with pytest.raises(RuntimeError):
                await invoke(env.adapter, "remote_read", path=str(path))
            assert not env.ledger.exists() and env.adapter._ledger is None
            assert len(env.calls) == 1 and env.calls[0][0] == "python3"

    asyncio.run(check())


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": ""},
        {"path": "nul\0"},
        {"path": 1},
        {"path": "x", "max_bytes": True},
        {"path": "x", "max_bytes": 0},
        {"path": "x", "max_bytes": 65537},
        {"path": "x", "max_bytes": 1.5},
        {"path": "x", "tail": 1},
        {"path": "x", "metadata_only": "true"},
    ],
)
def test_remote_read_validates_types_and_bounds_before_ssh(
    tmp_path, monkeypatch, arguments
):
    async def check():
        async with resource_fixture(tmp_path, monkeypatch) as env:
            with pytest.raises(ValueError):
                await invoke(env.adapter, "remote_read", **arguments)
            assert not env.calls and not env.ledger.exists()

    asyncio.run(check())


def test_native_and_host_inspect_expose_identical_artifact_tools(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    goal = tmp_path / "goal.md"
    goal.write_text("Synthetic artifact-tool inspection; no model or GPU.")
    settings = tmp_path / "resource.json"
    settings.write_text(
        json.dumps(
            {
                "host": "example.invalid",
                "partition": "debug",
                "remote_root": "/synthetic",
                "ledger_path": str(tmp_path / "budget.json"),
                "gpu_seconds_total": 600,
                "job_seconds": 120,
            }
        )
    )
    calls = []
    for mode in ("native", "host"):
        root = tmp_path / mode
        loop.initialize(root, project, goal, resource_file=settings, delegation=mode)
        calls.append(loop.inspect(root))
    assert calls[0]["tools"] == calls[1]["tools"]
    names = {tool["name"] for tool in calls[0]["tools"]}
    assert {"gpu_exec_script", "remote_read", "gpu_exec", "gpu_poll"} <= names
    assert not (tmp_path / "budget.json").exists()


def test_cancelled_script_transfer_is_unknown_and_never_replayed(tmp_path, monkeypatch):
    async def check():
        async with resource_fixture(
            tmp_path, monkeypatch, block_script_drain=True
        ) as env:
            (env.study / "work/prepared.sh").write_text("printf effect > effect.txt\n")
            task = asyncio.create_task(env.adapter._exec_script("prepared.sh"))
            await env.drain_entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            entry = env.adapter._ledger["entries"][0]
            assert entry["status"] == "UNKNOWN" and not entry["continuation_authorized"]
            session = next(iter(env.adapter._sessions.values()))
            assert session.error.startswith("script transfer uncertain:")
            assert session.stdin_closed
            await session.done.wait()
            # EOF after cancellation can still allow the original command to execute.
            assert (env.remote / "effect.txt").read_text() == "effect"
            with pytest.raises(RuntimeError, match="allocation uncertain"):
                await env.adapter._exec("printf never-replay")
            assert len([call for call in env.calls if call[0] == "srun"]) == 1
            scheduler_path = env.remote / "scheduler.json"
            scheduler = json.loads(scheduler_path.read_text())
            scheduler["jobs"][0]["state"] = "CANCELLED"
            scheduler_path.write_text(json.dumps(scheduler))
            receipt = await env.adapter._reconcile(entry["reservation_id"])
            assert (
                receipt["job_id"] == entry["job_id"] and receipt["elapsed_seconds"] == 7
            )
            # Only an exact original-job reconciliation admits another command.
            env.calls.clear()
            # The next command session remains interactive and uses the usual drain.
            result = await env.adapter._exec("printf next")
            assert not result["running"] and result["output"] == "next"
            assert len([call for call in env.calls if call[0] == "srun"]) == 1

    asyncio.run(asyncio.wait_for(check(), 8))


def test_remote_read_rejects_root_symlink_replacement_at_open(tmp_path):
    root, outside = tmp_path / "remote", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "artifact.json").write_text('{"source":"inside"}')
    (outside / "artifact.json").write_text('{"source":"outside"}')
    # Replace the already-resolved root precisely when the fixed query opens it.
    setup = """
import os, sys
from pathlib import Path
real_open = os.open
def replace_root(path, flags, *args, **kwargs):
    if str(path) == sys.argv[1]:
        root = Path(sys.argv[1])
        root.rename(root.with_name("previous-root"))
        root.symlink_to(sys.argv[6], target_is_directory=True)
        os.open = real_open
    return real_open(path, flags, *args, **kwargs)
os.open = replace_root
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            setup + REMOTE_READ,
            str(root),
            "artifact.json",
            "8192",
            "0",
            "0",
            str(outside),
        ],
        capture_output=True,
        timeout=5,
    )
    assert result.returncode != 0 and result.stdout == b""
    assert root.is_symlink()
