"""Real local CPU execution and synthetic resource receipts, without remote calls."""

import asyncio
import json
import shlex
import sys
import venv
from pathlib import Path
from types import SimpleNamespace
import pytest
from research_loop.codex import Tool
from research_loop.environment import observe
from research_loop.execution import Commands
from research_loop.state import Journal


@pytest.fixture
def context(tmp_path):
    root = tmp_path / "groups/run-1"
    work = root / "work"
    work.mkdir(parents=True)
    ctx = SimpleNamespace(
        run_id="run-1",
        root=root,
        work=work,
        study_root=tmp_path,
        state={},
        definition=SimpleNamespace(settings={}),
        round=1,
    )
    ctx.save_state = lambda: (root / "state.json").write_text(json.dumps(ctx.state))
    return ctx


def services(ctx, tools=()):
    journal = Journal(ctx.root / "events.jsonl")
    return Commands(
        ctx,
        {
            "project": str(ctx.work),
            "resource": {"remote_root": "/authorized synthetic root"}
            if tools
            else None,
        },
        journal,
        tools,
    ), journal


def test_local_cpu_receipt_is_durable_and_never_repeated(context):
    async def check():
        service, journal = services(context)
        command = f"{sys.executable} -c 'from pathlib import Path; Path(\"answer\").write_text(str(6 * 7))'"
        receipt = await service.execute_prepared(command, backend="local")
        assert receipt["exit_code"] == 0
        assert (context.work / "answer").read_text() == "42"
        (context.work / "answer").unlink()
        assert await service.execute_prepared(command, backend="local") == receipt
        assert not (context.work / "answer").exists()
        await service.settle()
        await service.close()
        journal.close()

    asyncio.run(check())


@pytest.mark.parametrize("virtualenv", [True, False])
def test_prepared_local_command_uses_selected_runtime(context, monkeypatch, virtualenv):
    selected = context.root / "selected runtime"
    python = selected / "bin/python"
    if virtualenv:
        venv.EnvBuilder(with_pip=False, symlinks=True).create(selected)
        sites = list((selected / "lib").glob("python*/site-packages"))
        assert len(sites) == 1
        metadata = sites[0] / "synthetic_marker-7.1.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: synthetic-marker\nVersion: 7.1\n"
        )
    else:
        python.parent.mkdir(parents=True)
        python.symlink_to(sys._base_executable)
    snapshot = observe({"python": str(python)})
    monkeypatch.setenv("PYTHONHOME", str(context.root / "wrong python home"))
    monkeypatch.setenv("PYTHONPATH", str(context.root / "wrong import path"))
    monkeypatch.setenv("VIRTUAL_ENV", str(context.root / "wrong virtualenv"))
    monkeypatch.setenv("PYTHONNOUSERSITE", "0")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "0")
    monkeypatch.setenv("PREPARED_COMMAND_SETTING", "preserved")
    script = context.work / "runtime witness.py"
    script.write_text(
        "import importlib.metadata, json, os, sys\n"
        "from pathlib import Path\n"
        "Path('witness.json').write_text(json.dumps({\n"
        "'executable': sys.executable, 'prefix': sys.prefix,\n"
        "'packages': {d.metadata['Name']: d.version "
        "for d in importlib.metadata.distributions() if d.metadata['Name']},\n"
        "'environment': {key: os.environ[key] for key in "
        "('HOME', 'TMPDIR', 'VIRTUAL_ENV', 'PYTHONHOME', 'PYTHONPATH', 'PYTHONNOUSERSITE', "
        "'PYTHONDONTWRITEBYTECODE', 'PREPARED_COMMAND_SETTING') "
        "if key in os.environ},\n"
        "'tmp_exists': Path(os.environ['TMPDIR']).is_dir()}))\n"
    )

    async def check():
        service, journal = services(context)
        service.config["execution_environment"] = snapshot
        try:
            receipt = await service.execute_prepared(
                "python " + shlex.quote(str(script)), backend="local"
            )
            assert receipt["exit_code"] == 0, Path(receipt["log_path"]).read_text()
            witness = json.loads((context.work / "witness.json").read_text())
            assert witness["executable"] == str(python)
            assert witness["prefix"] == snapshot["local"]["prefix"]
            if virtualenv:
                assert witness["packages"]["synthetic-marker"] == "7.1"
            env = witness["environment"]
            assert "PYTHONHOME" not in env
            assert "PYTHONPATH" not in env
            assert env.get("VIRTUAL_ENV") == (
                snapshot["local"]["prefix"] if virtualenv else None
            )
            assert env["HOME"] == str(context.work)
            assert env["TMPDIR"] == str(context.work / "tmp")
            assert witness["tmp_exists"]
            assert env["PYTHONNOUSERSITE"] == "1"
            assert env["PYTHONDONTWRITEBYTECODE"] == "1"
            assert env["PREPARED_COMMAND_SETTING"] == "preserved"
            await service.settle()
        finally:
            await service.close()
            journal.close()

    asyncio.run(check())


def test_unknown_resource_dispatch_is_not_retried(context):
    calls = []

    async def uncertain(args):
        calls.append(args)
        assert context.state["executions"]["experiment"]["status"] == "dispatching"
        raise RuntimeError("transport lost after dispatch")

    async def check():
        service, journal = services(context, [Tool("gpu_exec", "fake", {}, uncertain)])
        with pytest.raises(RuntimeError, match="transport lost"):
            await service.execute_prepared("prepared")
        with pytest.raises(RuntimeError, match="never redispatch"):
            await service.execute_prepared("prepared")
        assert len(calls) == 1
        with pytest.raises(RuntimeError, match="reconciliation"):
            await service.settle()
        journal.close()

    asyncio.run(check())


def test_resource_command_polls_same_tool_instance_until_terminal(context):
    calls = []

    async def execute(args):
        calls.append(("exec", args))
        return {"session_id": "original", "running": True, "exit_code": None}

    async def poll(args):
        calls.append(("poll", args))
        return {
            "session_id": "original",
            "running": False,
            "exit_code": 0,
            "log_path": "synthetic.log",
        }

    async def check():
        service, journal = services(
            context,
            [Tool("gpu_exec", "fake", {}, execute), Tool("gpu_poll", "fake", {}, poll)],
        )
        receipt = await service.execute_prepared("prepared")
        assert receipt["exit_code"] == 0
        assert calls == [
            ("exec", {"command": service._resource_command("prepared")}),
            ("poll", {"session_id": "original"}),
        ]
        assert await service.execute_prepared("prepared") == receipt
        assert len(calls) == 2
        await service.settle()
        journal.close()

    asyncio.run(check())


def test_slurm_ssh_255_receipt_is_unknown_even_with_null_error(context):
    async def execute(args):
        return {
            "session_id": "original",
            "running": False,
            "exit_code": 255,
            "output": "SSH transport lost",
            "log_path": "synthetic.log",
            "error": None,
            "reservation_id": "original-reservation",
        }

    async def check():
        service, journal = services(context, [Tool("gpu_exec", "fake", {}, execute)])
        with pytest.raises(RuntimeError, match="reconciliation"):
            await service.execute_prepared("prepared")
        assert context.state["executions"]["experiment"]["status"] == "unknown"
        with pytest.raises(RuntimeError, match="reconciliation"):
            await service.settle()
        journal.close()

    asyncio.run(check())


def test_remote_work_namespace_is_run_specific_and_root_required(context):
    import shlex

    service, journal = services(context)
    with pytest.raises(ValueError, match="authorized absolute remote_root"):
        service._resource_command("prepared")
    service.config["resource"] = {"remote_root": "/authorized root/with 'quote"}
    first = service._resource_command("printf ready")
    expected = "/authorized root/with 'quote/.group-runs/run-1"
    assert shlex.split(first.split(" && ")[0]) == ["mkdir", "-p", expected]
    assert service.execution_work == expected
    context.run_id = "run-2"
    second = service._resource_command("printf ready")
    assert first != second
    assert "/.group-runs/run-2" in second
    journal.close()


def test_stop_settles_real_local_process_without_new_admission(context):
    async def check():
        service, journal = services(context)
        task = asyncio.create_task(
            service.execute_prepared(
                f"{sys.executable} -c 'import time; time.sleep(30)'", backend="local"
            )
        )
        while not service.processes:
            await asyncio.sleep(0)
        await service.stop()
        try:
            receipt = await asyncio.wait_for(task, 3)
            assert receipt["exit_code"] != 0
        except RuntimeError:
            # A shell child surviving its parent is killed and conservatively unknown.
            assert context.state["executions"]["experiment"]["status"] == "unknown"
        assert not service.processes
        with pytest.raises(RuntimeError, match="Stop requested"):
            await service.execute_prepared(
                "printf unexpected", operation_id="new", backend="local"
            )
        journal.close()

    asyncio.run(check())


def test_shared_resource_lock_covers_dispatch_through_terminal(context):
    async def check():
        gate, entered = asyncio.Event(), asyncio.Event()
        calls = []

        async def execute(args):
            calls.append(args)
            return {
                "session_id": f"session-{len(calls)}",
                "running": True,
                "exit_code": None,
            }

        async def poll(args):
            if args["session_id"] == "session-1":
                entered.set()
                await gate.wait()
            return {"session_id": args["session_id"], "running": False, "exit_code": 0}

        tools = [
            Tool("gpu_exec", "fake", {}, execute),
            Tool("gpu_poll", "fake", {}, poll),
        ]
        one, journal = services(context, tools)
        other = SimpleNamespace(**vars(context))
        other.root = context.root.parent / "run-2"
        other.work = other.root / "work"
        other.work.mkdir(parents=True)
        other.run_id, other.state, other.save_state = "run-2", {}, lambda: None
        two, journal2 = services(other, tools)
        shared = asyncio.Lock()
        one.resource_lock = two.resource_lock = shared
        first = asyncio.create_task(one.execute_prepared("prepared"))
        await entered.wait()
        second = asyncio.create_task(two.execute_prepared("prepared"))
        await asyncio.sleep(0)
        assert len(calls) == 1
        with pytest.raises(RuntimeError, match="still active"):
            await two.settle()
        gate.set()
        assert all(
            receipt["exit_code"] == 0 for receipt in await asyncio.gather(first, second)
        )
        assert len(calls) == 2
        assert calls[0]["command"] != calls[1]["command"]
        await one.settle()
        await two.settle()
        journal.close()
        journal2.close()

    asyncio.run(check())


def test_settled_original_receipt_can_be_adopted_without_executing(context):
    async def check():
        service, journal = services(context)
        command = f'{sys.executable} -c \'from pathlib import Path; Path("once").write_text("original")\''
        receipt = await service.execute_prepared(
            command, operation_id="once", backend="local"
        )
        original = json.loads(json.dumps(context.state["executions"]["once"]))
        await service.close()
        journal.close()
        context.state = {}
        context.run_id = "recovery-run"
        recovered, journal = services(context)
        assert recovered.adopt_execution("once", original) == receipt
        (context.work / "once").unlink()
        assert (
            await recovered.execute_prepared(
                command, operation_id="once", backend="local"
            )
            == receipt
        )
        assert not (context.work / "once").exists()
        assert context.state["executions"]["once"]["origin_group_run_id"] == "run-1"
        with pytest.raises(ValueError, match="cannot be rebound"):
            recovered.adopt_execution(
                "once", {**original, "origin_group_run_id": "another-run"}
            )
        with pytest.raises(ValueError, match="settled"):
            recovered.adopt_execution(
                "unknown", {**original, "operation_id": "unknown", "status": "unknown"}
            )
        await recovered.close()
        journal.close()

    asyncio.run(check())


def test_operation_trace_keeps_original_identity(context):
    async def check():
        from research_loop.records import ExecutionTrace

        context.config_revision = "revision-1"
        context.trace = ExecutionTrace(
            context.root,
            run_id=context.run_id,
            round=1,
            revision=context.config_revision,
        )
        service, journal = services(context)
        await service.execute_prepared(
            "exit 3", operation_id="known-failure", backend="local"
        )
        record_paths = list((context.root / "records/operation").glob("*.json"))
        assert len(record_paths) == 1
        record = json.loads(record_paths[0].read_text())
        assert record["origin_group_run_id"] == "run-1"
        assert record["operation_id"] == "known-failure"
        assert record["status"] == "completed"
        assert record["receipt"]["exit_code"] == 3
        await service.close()
        journal.close()

    asyncio.run(check())
