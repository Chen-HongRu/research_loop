"""Exercise revision, admission, cancellation, and factual handoffs with real programs."""

import asyncio
import json
from pathlib import Path

import pytest

from research_loop.groups import GroupDefinition, GroupRuntime
from research_loop.records import list_records


PROGRAM = """
import json
async def run(ctx, payload):
    await ctx.services.work("first")
    await ctx.services.work("second")
    (ctx.work / "report.json").write_text(json.dumps({
        "revision": ctx.config_revision, "agent": ctx.definition.agents["write"],
        "payload": ctx.payload, "predecessor": ctx.predecessor,
    }))
    return {"summary": "done", "completion": "", "unfinished": [], "help": "", "artifacts": ["report.json"]}
"""


def definition(tmp_path, *, version="v1", agent="original"):
    path = tmp_path / f"program-{version}.py"
    path.write_text(PROGRAM)
    return GroupDefinition(
        "work", version, "Synthetic work", path, agents={"write": agent}
    )


def task(tmp_path):
    path = tmp_path / "task.md"
    path.write_text("Synthetic local task")
    return path


class Service:
    def __init__(self, ctx, *, gate=None, events=None, cleanup=None):
        self.ctx = ctx
        self.entered = asyncio.Event()
        self.gate = gate
        self.events = events if events is not None else []
        self.cleanup = cleanup
        self.calls = []

    async def work(self, label):
        self.calls.append(
            (label, self.ctx.config_revision, self.ctx.definition.agents["write"])
        )
        self.entered.set()
        try:
            if label == "first" and self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.events.append((self.ctx.run_id, "cancelled"))
            raise

    def close_admission(self):
        self.events.append((self.ctx.run_id, "close_admission"))

    async def stop(self):
        self.events.append((self.ctx.run_id, "stop"))
        if self.cleanup is not None:
            await self.cleanup.wait()

    async def settle(self):
        self.events.append((self.ctx.run_id, "settle"))

    async def close(self):
        self.events.append((self.ctx.run_id, "close"))


def test_revision_changes_only_new_executions_and_receipts_precede_work(tmp_path):
    async def scenario():
        gate = asyncio.Event()
        services = {}
        receipts = []

        def factory(ctx):
            # This executes before either program call can produce a side effect.
            receipt = json.loads(
                (
                    ctx.root / "rounds" / f"{ctx.round:06d}" / "input-receipt.json"
                ).read_text()
            )
            persisted = json.loads((ctx.root / "run.json").read_text())
            assert (
                receipt["config_revision"]
                == ctx.config_revision
                == persisted["config_revision"]
            )
            assert receipt["interventions"] == ctx.payload["interventions"]
            receipts.append(receipt)
            service = Service(
                ctx,
                gate=gate if ctx.config_revision == "one" and ctx.round == 1 else None,
            )
            services[(ctx.run_id, ctx.round)] = service
            return service

        runtime = GroupRuntime(
            tmp_path / "run", [definition(tmp_path)], factory, revision="one"
        )
        old = await runtime.start(
            "work",
            task(tmp_path),
            [],
            context={"question": "original"},
            predecessor="previous",
            interventions=["input-one"],
        )
        await asyncio.sleep(0)
        await services[(old, 1)].entered.wait()
        runtime.replace_definitions(
            [definition(tmp_path, version="v2", agent="replacement")], "two"
        )
        new = await runtime.start(
            "work", task(tmp_path), [], context={"question": "new"}
        )
        gate.set()
        await runtime.settle()
        assert services[(old, 1)].calls == [
            ("first", "one", "original"),
            ("second", "one", "original"),
        ]
        assert services[(new, 1)].calls == [
            ("first", "two", "replacement"),
            ("second", "two", "replacement"),
        ]
        report = json.loads(
            Path(runtime.observe(old)["output"]["artifacts"][0]).read_text()
        )
        assert report["payload"]["study_context"] == {"question": "original"}
        assert report["predecessor"] == "previous"
        runtime.hold(old)
        await runtime.resume(old, [], interventions=["input-two"])
        await runtime.settle()
        assert services[(old, 2)].calls[-1] == ("second", "one", "original")
        assert len(receipts) == 3

    asyncio.run(scenario())


def test_pause_preserves_active_output_and_blocks_start_and_resume(tmp_path):
    async def scenario():
        gate = asyncio.Event()
        created = []

        def factory(ctx):
            service = Service(ctx, gate=gate)
            created.append(service)
            return service

        runtime = GroupRuntime(tmp_path / "run", [definition(tmp_path)], factory)
        identity = await runtime.start("work", task(tmp_path), [])
        await asyncio.sleep(0)
        await created[0].entered.wait()
        runtime.pause()
        with pytest.raises(RuntimeError, match="paused"):
            await runtime.start("work", task(tmp_path), [])
        gate.set()
        await runtime.settle()
        assert runtime.observe(identity)["status"] == "returned"
        runtime.hold(identity)
        with pytest.raises(RuntimeError, match="paused"):
            await runtime.resume(identity, [])
        runtime.pause(paused=False)
        await runtime.resume(identity, [])
        await runtime.settle()
        assert runtime.observe(identity)["round"] == 2

    asyncio.run(scenario())


def test_targeted_stop_keeps_independent_execution_running(tmp_path):
    async def scenario():
        gates = [asyncio.Event(), asyncio.Event()]
        services = []

        def factory(ctx):
            service = Service(ctx, gate=gates[len(services)])
            services.append(service)
            return service

        runtime = GroupRuntime(tmp_path / "run", [definition(tmp_path)], factory)
        first = await runtime.start("work", task(tmp_path), [])
        second = await runtime.start("work", task(tmp_path), [])
        await asyncio.sleep(0)
        await asyncio.gather(*(service.entered.wait() for service in services))
        await runtime.stop(first)
        assert runtime.observe(first)["interrupted"]
        assert runtime.observe(second)["status"] == "running"
        assert not any(event[1] == "close_admission" for event in services[1].events)
        gates[1].set()
        await runtime.settle()
        assert runtime.observe(second)["status"] == "returned"

    asyncio.run(scenario())


def test_all_targets_receive_cancellation_before_slow_cleanup_completes(tmp_path):
    async def scenario():
        cleanup = asyncio.Event()
        events = []
        services = []

        def factory(ctx):
            service = Service(ctx, gate=asyncio.Event(), events=events, cleanup=cleanup)
            services.append(service)
            return service

        runtime = GroupRuntime(tmp_path / "run", [definition(tmp_path)], factory)
        identities = [await runtime.start("work", task(tmp_path), []) for _ in range(2)]
        await asyncio.sleep(0)
        await asyncio.gather(*(service.entered.wait() for service in services))
        stopping = asyncio.create_task(runtime.stop())
        for _ in range(20):
            if sum(event[1] == "cancelled" for event in events) == 2:
                break
            await asyncio.sleep(0)
        assert {
            identity for identity, action in events if action == "close_admission"
        } == set(identities)
        assert {
            identity for identity, action in events if action == "cancelled"
        } == set(identities)
        assert not stopping.done()
        cleanup.set()
        await stopping
        assert all(runtime.observe(identity)["interrupted"] for identity in identities)

    asyncio.run(scenario())


def test_actual_cross_run_artifact_refs_create_handoffs_only_for_consumers(tmp_path):
    async def scenario():
        runtime = GroupRuntime(tmp_path / "run", [definition(tmp_path)], Service)
        producer = await runtime.start("work", task(tmp_path), [])
        unrelated = await runtime.start("work", task(tmp_path), [])
        await runtime.settle()
        artifact = runtime.observe(producer)["output"]["artifacts"][0]
        consumer = await runtime.start("work", task(tmp_path), [artifact])
        await runtime.settle()
        assert runtime.observe(consumer)["dependencies"] == [producer]
        handoffs = list_records(runtime.root / consumer, "handoff")["items"]
        assert len(handoffs) == 1
        assert (
            handoffs[0]["sender"],
            handoffs[0]["receiver"],
            handoffs[0]["inputs"],
        ) == (producer, consumer, [artifact])
        assert list_records(runtime.root / unrelated, "handoff")["items"] == []
        assert list_records(runtime.root / producer, "handoff")["items"] == []

    asyncio.run(scenario())


def test_resume_feedback_dependency_has_an_explicit_handoff(tmp_path):
    async def scenario():
        runtime = GroupRuntime(tmp_path / "run", [definition(tmp_path)], Service)
        producer = await runtime.start("work", task(tmp_path), [])
        consumer = await runtime.start("work", task(tmp_path), [])
        await runtime.settle()
        artifact = runtime.observe(producer)["output"]["artifacts"][0]
        runtime.hold(consumer)
        await runtime.resume(consumer, [artifact])
        await runtime.settle()
        assert runtime.observe(consumer)["dependencies"] == [producer]
        handoffs = list_records(runtime.root / consumer, "handoff")["items"]
        assert len(handoffs) == 1
        assert handoffs[0]["round"] == 2
        assert handoffs[0]["inputs"] == [artifact]

    asyncio.run(scenario())
