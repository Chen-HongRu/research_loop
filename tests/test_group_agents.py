"""Synthetic leaf transport and real local CPU execution, without model calls."""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from research_loop.codex import Native, Tool, configuration
from openai_codex.client import CodexConfig
from research_loop.group_agents import GROUP_GUARD, NativeGroupServices
from research_loop.state import Journal


class FakeNative:
    instances = []
    outputs = []

    def __init__(self, config, journal):
        self.config = config
        self.calls = []
        self.lifecycle = []
        self.thread_id = None
        self.instances.append(self)

    async def open(self):
        self.lifecycle.append("open")

    async def verify(self, *args, **kwargs):
        self.lifecycle.append("verify")

    async def run(self, instructions, payload, schema, **kwargs):
        self.calls.append((instructions, payload, kwargs))
        self.thread_id = kwargs["thread_id"] or f"thread-{len(self.instances)}"
        kwargs["on_started"](self.thread_id)
        return self.outputs.pop(0) if self.outputs else {"ok": True}

    async def settle(self):
        self.lifecycle.append("settle")

    async def close(self):
        self.lifecycle.append("close")

    async def stop(self):
        self.lifecycle.append("stop")


@pytest.fixture
def context(tmp_path, monkeypatch):
    FakeNative.instances, FakeNative.outputs = [], []
    monkeypatch.setattr(
        "research_loop.group_agents.configuration",
        lambda *a, **kw: (CodexConfig(), {"delegation": kw["delegation"]}),
    )
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
        saves=0,
        round=1,
    )

    def save():
        ctx.saves += 1
        (root / "state.json").write_text(json.dumps(ctx.state))

    ctx.save_state = save
    return ctx


def services(ctx, tools=()):
    journal = Journal(ctx.root / "events.jsonl")
    return NativeGroupServices(
        ctx,
        {
            "project": str(ctx.work),
            "model": "gpt-6.1-sol",
            "effort": "medium",
            "resource": {"remote_root": "/authorized synthetic root"}
            if tools
            else None,
        },
        journal,
        tools,
        native_factory=FakeNative,
    ), journal


def test_context_reuse_fresh_roles_and_settled_lifecycle(context):
    async def check():
        service, journal = services(context)
        await service.call("proposer", "implementation", "refine", {}, {})
        await service.call("experimenter", "implementation", "implement", {}, {})
        await service.call("analyst", "analysis", "analyze", {}, {})
        await service.call("critic", "critique", "challenge", {}, {})
        instances = FakeNative.instances
        assert instances[0].thread_id == instances[1].thread_id
        assert len({instances[i].thread_id for i in (1, 2, 3)}) == 3
        assert all(item.calls[0][0] == GROUP_GUARD for item in instances)
        assert all(item.calls[0][2]["delegation"] == "none" for item in instances)
        assert all(
            item.lifecycle == ["open", "verify", "settle", "close"]
            for item in instances
        )
        with pytest.raises(ValueError, match="independent"):
            await service.call("critic", "analysis", "bad", {}, {})
        await service.settle()
        await service.close()
        journal.close()
        roles = json.loads((context.study_root / "roles.json").read_text())["threads"]
        assert len(roles) == 3
        assert all(row["group_run_id"] == context.run_id for row in roles.values())
        assert roles[instances[1].thread_id]["role"] == "experimenter"

    asyncio.run(check())


def test_contexts_cannot_cross_run_and_stop_admission(context):
    service, journal = services(context)
    context.run_id = "another-run"
    with pytest.raises(ValueError, match="another GroupRun"):
        services(context)
    context.run_id = "run-1"
    (context.study_root / "STOP").touch()
    with pytest.raises(RuntimeError, match="Stop requested"):
        asyncio.run(service.call("experimenter", "implementation", "run", {}, {}))
    assert not FakeNative.instances
    journal.close()


def test_call_failure_is_unknown_and_always_settles_closes(context):
    class Failing(FakeNative):
        async def run(self, *args, **kwargs):
            raise RuntimeError("synthetic turn failure")

    async def check():
        service, journal = services(context)
        service.native_factory = Failing
        with pytest.raises(RuntimeError, match="synthetic"):
            await service.call("experimenter", "implementation", "run", {}, {})
        assert FakeNative.instances[-1].lifecycle[-3:] == ["stop", "settle", "close"]
        with pytest.raises(RuntimeError, match="reconciliation"):
            await service.call("experimenter", "implementation", "repeat", {}, {})
        with pytest.raises(RuntimeError, match="reconciliation"):
            await service.settle()
        journal.close()

    asyncio.run(check())


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


def test_exp_adapter_small_cpu_path_and_resumed_phases(context):
    path = Path(__file__).parents[1] / "src/research_loop/group_programs/exp.py"
    spec = importlib.util.spec_from_file_location("exp_group_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plan = context.work / "plan.md"
    plan.write_text("Compute 6 * 7 and report the value; synthetic CPU test only.")
    context.definition.settings = {
        "plan": str(plan),
        "execution_backend": "local",
        "critic": True,
    }
    scientific_plan = context.work / "scientific-plan.md"
    scientific_plan.write_text(
        "One bounded synthetic CPU experiment; preserve the supplied task."
    )
    FakeNative.outputs = [
        {
            "summary": "planned",
            "help": "",
            "artifacts": [str(scientific_plan)],
            "command": "",
            "plan_artifact": str(scientific_plan),
        },
        {
            "summary": "prepared",
            "help": "",
            "artifacts": [],
            "command": f'{sys.executable} -c \'from pathlib import Path; Path("result.json").write_text("42")\'',
            "result_file": "result.json",
        },
        {
            "summary": "Observed 42; synthetic CPU evidence only",
            "help": "",
            "artifacts": [],
            "command": "",
            "claim": "The synthetic command wrote 42.",
            "evidence": ["result.json"],
            "limitations": ["Synthetic CPU check only"],
        },
    ]

    async def check():
        service, journal = services(context)
        context.services = service
        output = await module.run(context, {"task_ref": str(plan)})
        assert output["completion"] == "completed"
        assert "42" in output["summary"]
        assert all(Path(p).is_relative_to(context.work) for p in output["artifacts"])
        assert len(FakeNative.instances) == 3
        assert await module.run(context, {"task_ref": str(plan)}) == output
        assert len(FakeNative.instances) == 3
        await service.settle()
        await service.close()
        journal.close()

    asyncio.run(check())


def test_tool_free_and_leaf_configuration(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/auth.json").write_text("synthetic")
    monkeypatch.setattr(Path, "home", lambda: home)
    config, effective = configuration(
        tmp_path,
        tmp_path,
        [],
        "gpt-6.1-sol",
        "medium",
        delegation="none",
        allow_tools=False,
    )
    args = config.launch_args_override
    for feature in (
        "shell_tool",
        "unified_exec",
        "code_mode_host",
        "multi_agent",
        "multi_agent_v2",
        "browser_use",
    ):
        assert f"features.{feature}=false" in args
    assert 'web_search="disabled"' in args
    assert not effective["native_multi_agent"]
    assert not effective["allow_tools"]
    entry = json.loads(Path(effective["model_catalog"]).read_text())["models"][0]
    assert entry["apply_patch_tool_type"] is None
    assert entry["shell_type"] == "disabled"


def test_tool_free_native_rejects_activity_and_dynamic_callback(context):
    class Client:
        def __init__(self, **kwargs):
            self.interrupted = False
            self.events = iter(
                [
                    ("item/started", {"item": {"type": "commandExecution"}}),
                    ("turn/completed", {"turn": {"status": "interrupted"}}),
                ]
            )

        def start(self):
            pass

        def initialize(self):
            pass

        def close(self):
            pass

        def thread_start(self, definition):
            return {
                "thread": {"id": "router"},
                "model": "gpt-6.1-sol",
                "reasoningEffort": "medium",
            }

        def turn_start(self, *args):
            return {"turn": {"id": "turn"}}

        def next_turn_notification(self, *args):
            method, payload = next(self.events)
            return SimpleNamespace(method=method, payload=payload)

        def turn_interrupt(self, *args):
            self.interrupted = True

    async def check():
        journal = Journal(context.root / "native-events")
        native = Native(None, journal, client_factory=Client)
        await native.open()
        with pytest.raises(RuntimeError, match="Forbidden tool"):
            await native.run(
                "router",
                {},
                {},
                model="gpt-6.1-sol",
                effort="medium",
                delegation="none",
                allow_tools=False,
            )
        assert native.client.interrupted
        rejected = await native._tool(
            {"threadId": "router", "tool": "blocked", "arguments": {}}
        )
        assert not rejected["success"]
        assert native.delegator is None
        await native.close()
        journal.close()

    asyncio.run(check())


def test_real_runtime_exp_hold_feedback_resumes_only_blocked_evaluate(
    tmp_path, monkeypatch
):
    from research_loop.groups import GroupDefinition, GroupRuntime

    FakeNative.instances, FakeNative.outputs = (
        [],
        [
            {
                "summary": "planned",
                "help": "",
                "artifacts": ["scientific-plan.md"],
                "command": "",
                "plan_artifact": "scientific-plan.md",
            },
            {
                "summary": "prepared",
                "help": "",
                "artifacts": [],
                "command": f'{sys.executable} -c \'from pathlib import Path; p=Path("dispatches"); p.write_text(p.read_text()+"once" if p.exists() else "once")\'',
                "result_file": "dispatches",
            },
            {
                "summary": "need interpretation input",
                "help": "Supply the approved interpretation threshold",
                "artifacts": [],
                "command": "",
            },
            {
                "summary": "Evaluation completed using approved feedback",
                "help": "",
                "artifacts": [],
                "command": "",
                "claim": "The synthetic command ran once.",
                "evidence": ["dispatches"],
                "limitations": ["Synthetic CPU check only"],
            },
        ],
    )
    monkeypatch.setattr(
        "research_loop.group_agents.configuration", lambda *a, **kw: (CodexConfig(), {})
    )
    plan = tmp_path / "plan.md"
    plan.write_text(
        "Synthetic CPU receipt check, with interpretation threshold supplied separately."
    )
    feedback = tmp_path / "feedback.md"
    feedback.write_text("The approved threshold is one execution.")
    definition = GroupDefinition(
        "exp",
        "1",
        "synthetic",
        Path(__file__).parents[1] / "src/research_loop/group_programs/exp.py",
        {"execution_backend": "local"},
    )
    journal = Journal(tmp_path / "events")

    def factory(ctx):
        (ctx.work / "scientific-plan.md").write_text(
            "One bounded synthetic CPU command."
        )
        return NativeGroupServices(
            ctx,
            {
                "project": str(tmp_path / "project"),
                "model": "gpt-6.1-sol",
                "effort": "medium",
            },
            journal,
            native_factory=FakeNative,
        )

    async def check():
        runtime = GroupRuntime(tmp_path, [definition], service_factory=factory)
        run_id = await runtime.start("exp", str(plan), [])
        first = await runtime.wait()
        assert first["status"] == "returned", first
        assert first["output"]["help"]
        assert len(FakeNative.instances) == 3
        runtime.hold(run_id)
        await runtime.resume(run_id, [str(feedback)])
        second = await runtime.wait()
        assert second["status"] == "returned", second
        assert second["output"]["completion"] == "completed"
        assert (runtime.root / run_id / "work/dispatches").read_text() == "once"
        assert len(FakeNative.instances) == 4
        assert FakeNative.instances[3].calls[0][1]["assignment"]["feedback_refs"] == [
            str(feedback)
        ]
        assert FakeNative.instances[0].thread_id == FakeNative.instances[1].thread_id
        assert FakeNative.instances[1].thread_id != FakeNative.instances[2].thread_id
        assert FakeNative.instances[2].thread_id == FakeNative.instances[3].thread_id
        assert len(second["output"]["artifacts"]) == 7
        raw = next(
            Path(ref)
            for ref in second["output"]["artifacts"]
            if Path(ref).name == "dispatches"
        )
        assert raw.read_text() == "once"
        assert any("evaluate.json" in item for item in second["output"]["artifacts"])
        runtime.close(run_id)
        await runtime.close_runtime()

    try:
        asyncio.run(check())
    finally:
        journal.close()


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


def test_admitted_read_paths_are_files_and_queued_call_blocks_boundary(
    context, monkeypatch
):
    input_root = context.root / "rounds/000001"
    input_root.mkdir(parents=True)
    task, evidence, feedback = [
        context.study_root / name for name in ("task.md", "evidence.md", "feedback.md")
    ]
    input_root.joinpath("input.json").write_text(
        json.dumps(
            {
                "task_ref": str(task),
                "input_refs": [str(evidence)],
                "feedback_refs": [str(feedback)],
            }
        )
    )
    observed = []
    monkeypatch.setattr(
        "research_loop.group_agents.configuration",
        lambda *a, **kw: (observed.append(kw["readable_roots"]), {}),
    )

    async def check():
        service, journal = services(context)
        lock = service.locks.setdefault("implementation", asyncio.Lock())
        await lock.acquire()
        pending = asyncio.create_task(
            service.call("experimenter", "implementation", "prepare", {}, {})
        )
        with pytest.raises(RuntimeError, match="still active"):
            await service.settle()
        assert not service.active and service.pending_calls == 1
        lock.release()
        await pending
        assert observed == [[task, evidence, feedback]]
        assert context.study_root not in observed[0]
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


@pytest.mark.parametrize("project_is_study", [False, True])
def test_nested_study_permissions_deny_other_runs_allow_work_and_admitted_file(
    tmp_path, monkeypatch, project_is_study
):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/auth.json").write_text("synthetic")
    monkeypatch.setattr(Path, "home", lambda: home)
    project = tmp_path / "project"
    study = project if project_is_study else project / "study"
    own = study / "group-runs/own"
    runtime, work = own / "native-runtime", own / "work"
    runtime.mkdir(parents=True)
    work.mkdir()
    admitted = study / "group-runs/other/exports/000001/report.json"
    _, effective = configuration(
        runtime,
        project,
        [work],
        "gpt-6.1-sol",
        "medium",
        delegation="none",
        denied_roots=[study],
        readable_roots=[admitted],
    )
    paths = effective["filesystem"]

    def permission(path):
        matches = [
            (len(root.parts), value)
            for name, value in paths.items()
            if not name.startswith(":")
            and (root := Path(name))
            and path.is_relative_to(root)
        ]
        return max(matches)[1]

    assert (
        permission(study / "group-runs/other/native-runtime/native/history.jsonl")
        == "deny"
    )
    assert permission(study / "events.jsonl") == "deny"
    assert permission(study / "state.json") == "deny"
    assert permission(work / "implementation.py") == "write"
    assert permission(admitted) == "read"
    assert permission(runtime / "native/auth.json") == "deny"


def test_instruction_snapshot_survives_template_update_on_resume(context, monkeypatch):
    async def check():
        one, journal = services(context)
        original = context.state["native_instruction_snapshot"]
        monkeypatch.setattr("research_loop.group_agents.GROUP_GUARD", "changed guard")
        monkeypatch.setattr(
            "research_loop.group_agents._ROLE_SECTIONS",
            {"experimenter": "changed template"},
        )
        two, journal2 = services(context)
        await two.call("experimenter", "implementation", "resume", {}, {})
        guard, payload, _ = FakeNative.instances[-1].calls[0]
        assert guard == original["guard"]
        assert payload["role_template"] == original["roles"]["experimenter"]
        assert guard != "changed guard"
        await two.close()
        await one.close()
        journal.close()
        journal2.close()

    asyncio.run(check())


def test_exp_failed_execution_exports_explicit_code_and_directs_separate_repair(
    context,
):
    path = Path(__file__).parents[1] / "src/research_loop/group_programs/exp.py"
    spec = importlib.util.spec_from_file_location("failed_exp_group_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plan = context.work / "plan.md"
    plan.write_text("Synthetic known-failure receipt; never retry automatically.")
    code = context.work / "implementation.py"
    code.write_text("raise SystemExit(3)\n")
    scientific_plan = context.work / "scientific-plan.md"
    scientific_plan.write_text(
        "One bounded synthetic CPU experiment; preserve the supplied task."
    )
    FakeNative.outputs = [
        {
            "summary": "planned",
            "help": "",
            "artifacts": [str(scientific_plan)],
            "command": "",
            "plan_artifact": str(scientific_plan),
        },
        {
            "summary": "prepared",
            "help": "",
            "artifacts": [str(code)],
            "command": f"{sys.executable} implementation.py",
            "result_file": "unused.json",
        },
    ]
    context.definition.settings = {"execution_backend": "local"}

    async def check():
        service, journal = services(context)
        context.services = service
        output = await module.run(context, {"task_ref": str(plan)})
        assert "separate repair Run" in output["help"]
        assert str(code) in output["artifacts"]
        assert context.state["experiment_phases"]["execution"]["exit_code"] == 3
        assert (
            await module.run(
                context, {"task_ref": str(plan), "feedback_refs": [str(plan)]}
            )
            == output
        )
        assert len(FakeNative.instances) == 2
        await service.close()
        journal.close()

    asyncio.run(check())
