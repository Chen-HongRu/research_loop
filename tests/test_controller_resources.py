"""Resource ownership and cancellation using real Groups and a synthetic Slurm edge."""

import asyncio
import copy
import json

import pytest
import yaml

from research_loop import application
from research_loop.codex import Tool
from research_loop.controller import Controller
from research_loop.groups import GroupDefinition
from research_loop.state import PublicationStore


PROGRAM = """
import asyncio
import json

async def run(ctx, payload):
    try:
        if ctx.definition.id == "gpu":
            await ctx.services.execute_prepared("synthetic command", operation_id="original", backend="resource")
        ctx.state["entered"] = True
        ctx.save_state()
        while not (ctx.work / "release").exists():
            await asyncio.sleep(0.001)
        (ctx.work / "result.json").write_text(json.dumps({"answer": 42}))
        return {"summary": "done", "completion": "completed", "unfinished": [], "help": "", "artifacts": ["result.json"]}
    except asyncio.CancelledError:
        ctx.state["cancelled"] = True
        ctx.save_state()
        raise
"""

WORKFLOW = """
class Workflow:
    router_policy = "Synthetic resource ownership test; no model calls."
    max_active_groups = 4
    def context(self, *args):
        return {}
    def route(self, packet):
        return packet
    def advance(self, view):
        return None
    def complete(self, view, result_ref):
        return {"action": "stop", "result": {"answer": 42}, "artifacts": [{"content": "42", "destination": "answer.txt"}]}
def create(settings):
    return Workflow()
"""


class FakeSlurm:
    instances = []

    def __init__(self, config, root):
        self.config = config
        self.calls = []
        self.close_calls = 0
        self.close_entered = asyncio.Event()
        self.close_gate = None
        self.close_error = None
        self.close_result = {
            "reservations": [
                {"reservation_id": "synthetic-original", "status": "TERMINAL"}
            ],
            "cleanup_errors": [],
        }
        self.instances.append(self)

    def tools(self):
        return [
            Tool(
                "gpu_exec",
                "Synthetic authorized command",
                {"type": "object"},
                self.execute,
            )
        ]

    async def execute(self, arguments):
        self.calls.append(copy.deepcopy(arguments))
        return {
            "session_id": f"command-{len(self.calls)}",
            "exit_code": 0,
            "running": False,
        }

    async def close(self):
        self.close_calls += 1
        self.close_entered.set()
        if self.close_gate is not None:
            await self.close_gate.wait()
        if self.close_error is not None:
            raise self.close_error
        return copy.deepcopy(self.close_result)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    FakeSlurm.instances = []
    monkeypatch.setattr("research_loop.slurm.Slurm", FakeSlurm)
    author = tmp_path / "author"
    author.mkdir()
    (author / "workflow.py").write_text(WORKFLOW)
    (author / "group.py").write_text(PROGRAM)
    (author / "goal.md").write_text("Synthetic bounded resource ownership check")
    (author / "resource.json").write_text(
        json.dumps(
            {
                "host": "example",
                "partition": "synthetic",
                "remote_root": "/synthetic",
                "gpu_seconds_total": 60,
                "job_seconds": 60,
                "ledger_path": "ledger.json",
            }
        )
    )
    declaration = {
        "version": 1,
        "workflow": {"program": "workflow.py"},
        "router": {"profile": "main"},
        "profiles": {"main": {"model": "gpt-6.1-sol", "effort": "medium"}},
        "agents": {},
        "groups": {
            name: {
                "version": "1",
                "description": "Synthetic bounded work",
                "program": "group.py",
            }
            for name in ("cpu", "gpu")
        },
        "execution": {"resource": "resource.json"},
    }
    source = author / "run.yaml"
    source.write_text(yaml.safe_dump(declaration))
    root = tmp_path / "run"
    application.prepare(source, root, project=author, goal=author / "goal.md")
    return root


async def entered(controller, identity):
    async with asyncio.timeout(2):
        while True:
            path = controller.root / "group-runs" / identity / "state.json"
            if path.exists() and json.loads(path.read_text()).get("entered"):
                return
            row = controller.runtime.observe(identity)
            if row["status"] != "running":
                pytest.fail(f"Group failed before reaching its gate: {row}")
            await asyncio.sleep(0.001)


async def start(controller, group, *, task=None):
    await controller._dispatch(
        {
            "action": "start",
            "group_id": group,
            "task_ref": str(task or controller.root / "goal.md"),
            "input_refs": [],
        }
    )
    identity = controller.runtime.observe()["runs"][-1]["run_id"]
    await entered(controller, identity)
    return identity


async def dispose(controller):
    for adapter in FakeSlurm.instances:
        if adapter.close_gate is not None:
            adapter.close_gate.set()
    try:
        await controller.runtime.stop()
        await asyncio.gather(
            *controller.adapter_closing.values(), return_exceptions=True
        )
    finally:
        controller.journal.close()


def test_stopping_independent_cpu_group_keeps_gpu_owner_allocation(prepared):
    async def scenario():
        controller = Controller(prepared)
        try:
            gpu = await start(controller, "gpu")
            cpu = await start(controller, "cpu")
            adapter = FakeSlurm.instances[0]
            assert len(adapter.calls) == 1
            await controller._stop_targets({cpu})
            assert adapter.close_calls == 0
            assert controller.runtime.observe(gpu)["status"] == "running"
            assert controller.runtime.observe(cpu)["interrupted"]
            assert controller.runtime.observe(cpu)["status"] == "failed"
            assert not controller.state.get("cleanup_errors")
        finally:
            await dispose(controller)

    asyncio.run(scenario())


def test_stopping_gpu_owner_closes_resource_while_independent_group_stays_running(
    prepared,
):
    async def scenario():
        controller = Controller(prepared)
        try:
            gpu = await start(controller, "gpu")
            cpu = await start(controller, "cpu")
            adapter = FakeSlurm.instances[0]
            adapter.close_gate = asyncio.Event()
            stopping = asyncio.create_task(controller._stop_targets({gpu}))
            async with asyncio.timeout(2):
                await adapter.close_entered.wait()
                while controller.runtime.observe(gpu)["status"] == "running":
                    await asyncio.sleep(0)
            assert not stopping.done()
            assert controller.runtime.observe(cpu)["status"] == "running"
            cancelled = json.loads(
                (prepared / "group-runs" / gpu / "state.json").read_text()
            )
            assert cancelled["cancelled"]
            adapter.close_gate.set()
            await stopping
            assert controller.runtime.observe(gpu)["interrupted"]
            assert adapter.close_calls == 1
            assert controller.runtime.observe(cpu)["status"] == "running"
        finally:
            await dispose(controller)

    asyncio.run(scenario())


def test_all_groups_are_cancelled_before_slow_resource_cleanup_finishes(prepared):
    async def scenario():
        controller = Controller(prepared)
        try:
            gpu = await start(controller, "gpu")
            cpu = await start(controller, "cpu")
            adapter = FakeSlurm.instances[0]
            adapter.close_gate = asyncio.Event()
            stopping = asyncio.create_task(controller._stop_targets())
            async with asyncio.timeout(2):
                await adapter.close_entered.wait()
                while not all(
                    json.loads(
                        (prepared / "group-runs" / identity / "state.json").read_text()
                    ).get("cancelled")
                    for identity in (gpu, cpu)
                ):
                    await asyncio.sleep(0)
            assert not stopping.done()
            adapter.close_gate.set()
            await stopping
            assert all(
                controller.runtime.observe(identity)["interrupted"]
                for identity in (gpu, cpu)
            )
            assert adapter.close_calls == 1
        finally:
            await dispose(controller)

    asyncio.run(scenario())


def test_same_resource_across_revisions_reuses_one_backend(prepared):
    async def scenario():
        controller = Controller(prepared)
        try:
            first = await start(controller, "gpu")
            config = copy.deepcopy(controller.config)
            config["revision"] = "r000002"
            controller.configs[config["revision"]] = config
            controller.config = config
            controller.runtime.replace_definitions(
                [GroupDefinition.from_json(row) for row in config["groups"]],
                config["revision"],
            )
            task = prepared / "second-task.md"
            task.write_text("A distinct synthetic task on the same authorized backend")
            second = await start(controller, "gpu", task=task)
            assert len(FakeSlurm.instances) == 1
            assert len(FakeSlurm.instances[0].calls) == 2
            assert controller.runtime.observe(first)["config_revision"] == "r000001"
            assert controller.runtime.observe(second)["config_revision"] == "r000002"
            assert all(
                controller.runtime.observe(identity)["status"] == "running"
                for identity in (first, second)
            )
        finally:
            await dispose(controller)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["unknown", "cleanup-error", "exception"])
def test_targeted_cleanup_failure_blocks_later_publication(prepared, failure):
    async def scenario():
        controller = Controller(prepared)
        try:
            gpu = await start(controller, "gpu")
            adapter = FakeSlurm.instances[0]
            if failure == "unknown":
                adapter.close_result["reservations"][0]["status"] = "UNKNOWN"
            elif failure == "cleanup-error":
                adapter.close_result["cleanup_errors"] = [
                    "Synthetic cancellation was not confirmed"
                ]
            else:
                adapter.close_error = RuntimeError("Synthetic cleanup transport failed")
            baseline = PublicationStore(prepared).prepare()
            await controller._stop_targets({gpu})
            assert controller.state.get("cleanup_errors")
            assert controller._unknown()
            # Final shutdown consumes the same failed cleanup receipt too.
            await controller._stop_targets()
            controller.runtime.close(gpu)
            with pytest.raises(RuntimeError, match="settled|cleanup|reconciliation"):
                await controller._dispatch(
                    {"action": "finish_round", "result_ref": str(prepared / "goal.md")}
                )
            assert adapter.close_calls == 1  # Failed cleanup is not silently retried.
            assert str(PublicationStore(prepared).current.resolve()) == baseline
            assert PublicationStore(prepared).published_state() is None
        finally:
            await dispose(controller)

    asyncio.run(scenario())
