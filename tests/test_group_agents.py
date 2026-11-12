"""Declarative Agent identity, lifecycle and cancellation without model calls."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from openai_codex.client import CodexConfig

from research_loop.group_agents import NativeGroupServices
from research_loop.state import Journal


class Trace:
    def __init__(self):
        self.rows = []

    def begin(self, kind, label, **data):
        identifier = f"call-{len(self.rows)}"
        self.rows.append({"id": identifier, "kind": kind, "label": label, **data})
        return identifier

    def end(self, identifier, status, **data):
        self.rows.append({"id": identifier, "status": status, **data})


class FakeNative:
    instances = []

    def __init__(self, config, journal):
        self.config = config
        self.calls = []
        self.lifecycle = []
        self.instances.append(self)

    async def open(self):
        self.lifecycle.append("open")

    async def verify(self, *args):
        self.lifecycle.append("verify")

    async def run(self, instructions, payload, schema, **kwargs):
        self.calls.append((instructions, payload, schema, kwargs))
        self.thread_id = kwargs["thread_id"] or f"thread-{len(self.instances)}"
        kwargs["on_started"](self.thread_id)
        return {"ok": True}

    async def stop(self):
        self.lifecycle.append("stop")

    async def settle(self):
        self.lifecycle.append("settle")

    async def close(self):
        self.lifecycle.append("close")


@pytest.fixture
def context(tmp_path, monkeypatch):
    FakeNative.instances = []

    def configure(root, project, writable, model, effort, **kwargs):
        return CodexConfig(cwd=str(root / "work")), {
            "model": model,
            "effort": effort,
            "allow_tools": kwargs["allow_tools"],
            "cwd": str(root / "work"),
            "delegation": "none",
        }

    monkeypatch.setattr("research_loop.group_agents.configuration", configure)
    root = tmp_path / "groups/run-1"
    work = root / "work"
    work.mkdir(parents=True)
    ctx = SimpleNamespace(
        run_id="run-1",
        root=root,
        work=work,
        study_root=tmp_path,
        state={},
        config_revision="revision-1",
        round=1,
        trace=Trace(),
        definition=SimpleNamespace(
            settings={},
            agents={"draft": "writer", "refine": "writer", "verify": "checker"},
        ),
    )
    ctx.save_state = lambda: (root / "state.json").write_text(json.dumps(ctx.state))
    return ctx


def services(ctx):
    journal = Journal(ctx.root / "events.jsonl")
    return NativeGroupServices(
        ctx,
        {
            "revision": ctx.config_revision,
            "project": str(ctx.work),
            "profiles": {
                "fast": {"model": "gpt-6.1-sol", "effort": "medium"},
                "deep": {"model": "gpt-6-astra", "effort": "high"},
            },
            "agents": {
                "writer": {
                    "instructions": "Write the assigned artifact.",
                    "profile": "fast",
                    "tools": "workspace",
                    "output_schema": {"type": "object"},
                },
                "checker": {
                    "instructions": "Check the supplied evidence.",
                    "profile": "deep",
                    "tools": "none",
                    "output_schema": None,
                },
            },
        },
        journal,
        native_factory=FakeNative,
    ), journal


def test_explicit_reuse_and_fresh_sessions_use_actual_profile(context):
    async def check():
        service, journal = services(context)
        try:
            await service.call(
                "draft", "shared", "draft", {"value": 1}, step_id="explicit-step"
            )
            await service.call("refine", "shared", "refine", {}, session="reuse")
            await service.call("verify", "separate", "check", {})
            await service.call("draft", "shared", "fresh", {})
            instances = FakeNative.instances
            assert instances[0].thread_id == instances[1].thread_id
            assert len({item.thread_id for item in instances}) == 3
            assert all(
                item.lifecycle == ["open", "verify", "settle", "close"]
                for item in instances
            )
            assert instances[2].calls[0][3]["model"] == "gpt-6-astra"
            assert instances[2].calls[0][3]["allow_tools"] is False
            assert instances[0].calls[0][0] == "Write the assigned artifact."
            calls = context.state["native_calls"]
            assert calls[0]["session_id"] == calls[1]["session_id"]
            assert calls[3]["session_id"] != calls[0]["session_id"]
            assert all(row["thread_id"] and row["call_id"] for row in calls)
            assert calls[0]["parent_step_id"] == "explicit-step"
            assert calls[1]["parent_step_id"] is None
            assert calls[1]["binding"] == "refine"
            assert calls[2]["effective"]["allow_tools"] is False
            assert len(context.state["native_sessions"]) == 3
            mapping = json.loads((context.study_root / "roles.json").read_text())[
                "threads"
            ]
            assert set(mapping) == {item.thread_id for item in instances}
            assert {row["role"] for row in mapping.values()} == {"writer", "checker"}
            assert all(
                row["group_run_id"] == context.run_id and row["status"] == "completed"
                for row in mapping.values()
            )
            await service.settle()
        finally:
            await service.close()
            journal.close()

    asyncio.run(check())


def test_reuse_rejects_missing_identity_different_agent_and_tools(context):
    async def check():
        service, journal = services(context)
        try:
            with pytest.raises(ValueError, match="compatible"):
                await service.call("draft", "missing", "x", {}, session="reuse")
            await service.call("draft", "shared", "x", {})
            with pytest.raises(ValueError, match="compatible"):
                await service.call("verify", "shared", "x", {}, session="reuse")
            with pytest.raises(ValueError, match="compatible"):
                await service.call(
                    "draft", "shared", "x", {}, session="reuse", allow_tools=False
                )
            assert len(FakeNative.instances) == 1
        finally:
            await service.close()
            journal.close()

    asyncio.run(check())


def test_definitions_are_snapshotted_and_cannot_change_within_run(context):
    async def check():
        service, journal = services(context)
        try:
            service.config["agents"]["writer"]["instructions"] = "changed external copy"
            await service.call("draft", "local", "x", {})
            assert FakeNative.instances[0].calls[0][0] == "Write the assigned artifact."
            context.definition.agents["draft"] = "checker"
            with pytest.raises(ValueError, match="cannot change"):
                services(context)
            context.run_id = "other-run"
            with pytest.raises(ValueError, match="another GroupRun"):
                services(context)
        finally:
            await service.close()
            journal.close()

    asyncio.run(check())


def test_settled_failure_is_recorded_without_retry(context):
    class Failing(FakeNative):
        async def run(self, *args, **kwargs):
            raise ValueError("exact schema failure")

    async def check():
        service, journal = services(context)
        service.agents.native_factory = Failing
        with pytest.raises(ValueError, match="exact schema failure"):
            await service.call("draft", "local", "x", {})
        with pytest.raises(ValueError, match="compatible"):
            await service.call("draft", "local", "x", {}, session="reuse")
        assert FakeNative.instances[0].lifecycle[-3:] == ["stop", "settle", "close"]
        assert context.state["native_calls"][0]["status"] == "failed"
        assert context.trace.rows[-1]["status"] == "failed"
        assert len(FakeNative.instances) == 1
        await service.settle()
        journal.close()

    asyncio.run(check())


def test_all_native_cancellations_are_issued_before_wait(context):
    async def check():
        service, journal = services(context)
        gate, seen = asyncio.Event(), set()

        class Active:
            def __init__(self, name):
                self.name = name

            async def stop(self):
                seen.add(self.name)
                await gate.wait()

        service.agents.active.update([Active("one"), Active("two")])
        pending = service.request_stop()
        assert service.agents.stopping and service.commands.stopping
        await asyncio.sleep(0)
        assert seen == {"one", "two"}
        with pytest.raises(RuntimeError, match="Stop requested"):
            await service.call("draft", "local", "x", {})
        gate.set()
        assert await pending == [None, None]
        service.agents.active.clear()
        await service.close()
        journal.close()

    asyncio.run(check())


def test_payload_and_output_records_do_not_alias_mutable_inputs(context):
    async def check():
        service, journal = services(context)
        payload = {"nested": ["initial"]}
        result = await service.call("draft", "local", "x", payload)
        payload["nested"].append("later")
        result["ok"] = False
        call = context.state["native_calls"][0]
        assert call["input"]["assignment"] == {"nested": ["initial"]}
        assert call["result"] == {"ok": True}
        await service.close()
        journal.close()

    asyncio.run(check())


def test_reloaded_session_failure_is_visible_to_settlement(context):
    class Failing(FakeNative):
        async def run(self, *args, **kwargs):
            raise RuntimeError("synthetic resumed failure")

        async def settle(self):
            raise RuntimeError("synthetic unsettled native work")

    async def check():
        service, journal = services(context)
        await service.call("draft", "local", "first", {})
        await service.close()
        journal.close()
        context.state = json.loads((context.root / "state.json").read_text())
        resumed, journal = services(context)
        resumed.agents.native_factory = Failing
        with pytest.raises(RuntimeError, match="resumed failure"):
            await resumed.call("refine", "local", "next", {}, session="reuse")
        with pytest.raises(RuntimeError, match="reconciliation"):
            await resumed.settle()
        session = context.state["native_contexts"]["local"]["session_id"]
        assert context.state["native_sessions"][session]["status"] == "unknown"
        journal.close()

    asyncio.run(check())


def test_actual_trace_records_calls_without_research_assumptions(context):
    async def check():
        del context.trace
        service, journal = services(context)
        await service.call("draft", "local", "write", {"synthetic": True})
        await service.close()
        journal.close()
        records = list((context.root / "records/call").glob("*.json"))
        assert len(records) == 1
        record = json.loads(records[0].read_text())
        assert record["status"] == "completed"
        assert record["thread_id"] == FakeNative.instances[0].thread_id
        assert record["binding"] == "draft"
        assert record["profile"] == {"model": "gpt-6.1-sol", "effort": "medium"}

    asyncio.run(check())


def test_cleanup_error_does_not_replace_exact_call_error(context):
    class Failing(FakeNative):
        async def run(self, *args, **kwargs):
            raise ValueError("original schema error")

        async def settle(self):
            raise RuntimeError("unsettled transport")

    async def check():
        service, journal = services(context)
        service.agents.native_factory = Failing
        with pytest.raises(ValueError, match="original schema error"):
            await service.call("draft", "local", "x", {})
        call = context.state["native_calls"][0]
        assert call["error"] == "ValueError: original schema error"
        assert call["cleanup_error"] == "RuntimeError: unsettled transport"
        assert FakeNative.instances[0].lifecycle[-1] == "close"
        journal.close()

    asyncio.run(check())
