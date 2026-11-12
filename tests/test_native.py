"""Transport decisions and factual history, without a model or remote service."""

import asyncio
import json
import threading
from pathlib import Path

import pytest
from openai_codex.models import Notification
from openai_codex.generated.v2_all import (
    DynamicToolSpec,
    ItemCompletedNotification,
    TurnCompletedNotification,
)

from research_loop.codex import Native, Tool, configuration
from research_loop.state import Journal

RESULT = {"summary": "synthetic transport result"}
RESULT_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


class Client:
    def __init__(self, config=None, approval_handler=None):
        self.handler = approval_handler
        self.calls = []
        self.definition = None
        self.closed = 0
        self.initialized = False
        self.notifications = iter(
            [
                (
                    "item/completed",
                    {
                        "item": {
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": json.dumps(RESULT),
                        }
                    },
                ),
                ("turn/completed", {"turn": {"status": "completed"}}),
            ]
        )

    def start(self):
        pass

    def close(self):
        self.closed += 1

    def initialize(self):
        self.initialized = True

    def request(self, method, params, **kwargs):
        assert self.initialized, "SDK handshake is required before requests"
        self.calls.append((method, params))
        if method == "account/read":
            return {"account": {"type": "chatgpt"}}
        if method == "model/list":
            return {
                "data": [
                    {
                        "model": "gpt-6.1-sol",
                        "supportedReasoningEfforts": [{"reasoningEffort": "medium"}],
                    }
                ]
            }
        if method == "thread/loaded/list":
            return {"data": ["lead"]}
        return {}

    def thread_start(self, params):
        self.definition = params
        return {
            "model": "gpt-6.1-sol",
            "reasoningEffort": "medium",
            "thread": {"id": "lead"},
        }

    def thread_resume(self, thread_id, params):
        self.calls.append(("thread/resume", thread_id))
        return self.thread_start(params)

    def turn_start(self, thread_id, prompt, params):
        self.calls.append(("turn/start", params))
        return {"turn": {"id": "turn"}}

    def next_turn_notification(self, turn_id):
        method, params = next(self.notifications)
        if method == "item/completed":
            item = {"id": "item", **params["item"]}
            if item["type"] == "collabAgentToolCall":
                item.update(
                    tool="spawnAgent",
                    status="completed",
                    senderThreadId="lead",
                    agentsStates={},
                )
            payload = ItemCompletedNotification.model_validate(
                {
                    "threadId": "lead",
                    "turnId": turn_id,
                    "completedAtMs": 0,
                    "item": item,
                }
            )
        else:
            payload = TurnCompletedNotification.model_validate(
                {
                    "threadId": "lead",
                    "turn": {"id": turn_id, "items": [], **params["turn"]},
                }
            )
        return Notification(method=method, payload=payload)

    def thread_read(self, thread_id, include_turns):
        self.calls.append(("thread/read", thread_id))
        return {"thread": {"turns": [{"status": "completed", "items": []}]}}

    def turn_interrupt(self, thread_id, turn_id):
        self.calls.append(("turn/interrupt", thread_id))


def test_native_structured_result_and_factual_history(tmp_path):
    async def check():
        journal = Journal(tmp_path / "events")
        native = Native(None, journal, client_factory=Client)
        await native.open()
        await native.verify("gpt-6.1-sol", "medium")
        result = await native.run(
            "native lead", {}, RESULT_SCHEMA, model="gpt-6.1-sol", effort="medium"
        )
        assert result == RESULT  # Empty evidence is preserved, not judged by the host.
        assert native.client.definition["developerInstructions"] == "native lead"
        assert "baseInstructions" not in native.client.definition
        assert native.client.definition["dynamicTools"] == []
        assert native.client.calls[2][1]["outputSchema"] == RESULT_SCHEMA
        await native.settle()
        assert ("thread/read", "lead") in native.client.calls
        cleaned = [
            params["threadId"]
            for method, params in native.client.calls
            if method == "thread/backgroundTerminals/clean"
        ]
        assert cleaned == ["lead"]  # A finished/unloaded child is not a live terminal.
        await native.close()
        await native.close()
        assert native.client.closed == 1
        journal.close()
        rows = [
            json.loads(line) for line in (tmp_path / "events").read_text().splitlines()
        ]
        assert any(
            row["event"] == "codex.history.reference" and row["thread_id"] == "lead"
            for row in rows
        )

    asyncio.run(check())


def test_stop_during_turn_start_interrupts_the_acknowledged_turn(tmp_path):
    async def check():
        entering, release = threading.Event(), threading.Event()

        class SlowClient(Client):
            def turn_start(self, thread_id, prompt, params):
                entering.set()
                assert release.wait(5)
                return super().turn_start(thread_id, prompt, params)

        journal = Journal(tmp_path / "events")
        native = Native(None, journal, client_factory=SlowClient)
        await native.open()
        task = asyncio.create_task(
            native.run("lead", {}, RESULT_SCHEMA, model="gpt-6.1-sol", effort="medium")
        )
        assert await asyncio.to_thread(entering.wait, 5)
        await native.stop()
        release.set()
        await task
        assert ("turn/interrupt", "lead") in native.client.calls
        await native.close()
        journal.close()

    asyncio.run(check())


def test_interrupt_timeout_closes_transport_and_releases_rpc_worker(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("research_loop.codex._INTERRUPT_TIMEOUT", 0.02)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class BlockedInterruptClient(Client):
        def turn_interrupt(self, thread_id, turn_id):
            self.calls.append(("turn/interrupt", thread_id))
            entered.set()
            try:
                release.wait()
                raise RuntimeError("Synthetic transport closed")
            finally:
                finished.set()

        def close(self):
            super().close()
            release.set()

    async def check():
        journal = Journal(tmp_path / "events")
        native = Native(None, journal, client_factory=BlockedInterruptClient)
        await native.open()
        native.thread_id, native.turn_id, native.active = "lead", "turn", True
        try:
            with pytest.raises(
                RuntimeError, match="execution outcome requires reconciliation"
            ) as outcome:
                await asyncio.wait_for(native.stop(), 1)
            assert isinstance(outcome.value.__cause__, TimeoutError)
            assert entered.is_set()
            assert native.client.closed == 1
            assert await asyncio.to_thread(finished.wait, 1)
            assert native.active and not native.settled
            assert native.closed
            assert native.client.calls == [("turn/interrupt", "lead")]
        finally:
            release.set()
            await native.close()
            journal.close()

    asyncio.run(check())


def test_active_child_is_not_reported_settled(tmp_path):
    async def check():
        class ActiveClient(Client):
            def thread_read(self, thread_id, include_turns):
                assert include_turns is False
                return {
                    "thread": {
                        "status": {"type": "active"},
                        "path": "synthetic-rollout.jsonl",
                    }
                }

        journal = Journal(tmp_path / "events")
        native = Native(None, journal, client_factory=ActiveClient)
        await native.open()
        native.thread_id = "lead"
        with pytest.raises(RuntimeError, match="still active"):
            await native.settle()
        assert not native.settled
        journal.close()
        assert any(
            method == "thread/backgroundTerminals/clean"
            for method, _ in native.client.calls
        )
        await native.close()

    asyncio.run(check())


def test_external_tool_callback_is_lead_only_and_stops_before_dispatch(tmp_path):
    async def check():
        calls = []

        async def effect(args):
            calls.append(args)
            return {"ok": True}

        journal = Journal(tmp_path / "events")
        tool = Tool("resource", "Synthetic", {"type": "object"}, effect)
        native = Native(None, journal, [tool], client_factory=Client)
        await native.open()
        native.thread_id = "lead"
        params = {"threadId": "child", "tool": "resource", "arguments": {}}
        rejected = await asyncio.to_thread(
            native.client.handler, "item/tool/call", params
        )
        assert not rejected["success"] and not calls
        params["threadId"] = "lead"
        accepted = await asyncio.to_thread(
            native.client.handler, "item/tool/call", params
        )
        assert accepted["success"] and calls == [{}]
        await native.stop()
        rejected = await native._tool(params)
        assert not rejected["success"] and calls == [{}]
        with pytest.raises(RuntimeError, match="Stop requested"):
            await native.run("unused", {}, {}, model="gpt-6.1-sol", effort="medium")
        assert not any(method == "turn/start" for method, _ in native.client.calls)
        await native.close()
        journal.close()

    asyncio.run(check())


def test_exact_model_has_no_fallback(tmp_path):
    async def check():
        journal = Journal(tmp_path / "events")
        native = Native(None, journal, client_factory=Client)
        await native.open()
        with pytest.raises(RuntimeError, match="exact configured"):
            await native.verify("another-model", "medium")
        assert not any(method == "turn/start" for method, _ in native.client.calls)
        await native.close()
        journal.close()

    asyncio.run(check())


def test_native_configuration_uses_ordinary_workspace_without_forwarding_keys(
    tmp_path, monkeypatch
):
    home = tmp_path / "owner"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/auth.json").write_text("synthetic-login-fixture")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_NOT_A_CREDENTIAL")
    root = tmp_path / "run"
    root.mkdir()
    config, effective = configuration(
        root, tmp_path, [root / "work"], "gpt-6.1-sol", "medium"
    )
    args = config.launch_args_override
    assert "features.multi_agent=false" in args
    assert "features.multi_agent_v2_dynamic_tools=false" in args
    assert not any("SYNTHETIC_NOT_A_CREDENTIAL" in value for value in args)
    assert effective["filesystem"][":root"] == "read"
    assert effective["filesystem"][":slash_tmp"] == "write"
    assert effective["filesystem"][":tmpdir"] == "write"
    assert effective["sandbox_mode"] == "workspace-write"
    assert config.cwd == str(root / "work")
    assert 'sandbox_mode="workspace-write"' in args
    assert "deny" not in effective["filesystem"].values()
    assert effective["filesystem"][str((root / "work").resolve())] == "write"


def test_registered_tools_only_configuration_suppresses_builtins(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/auth.json").write_text("synthetic")
    monkeypatch.setattr(Path, "home", lambda: home)
    root = tmp_path / "restricted"
    root.mkdir()
    config, effective = configuration(
        root,
        tmp_path,
        [root / "work"],
        "synthetic-model",
        "medium",
        allow_builtin_tools=False,
    )
    args = config.launch_args_override
    assert effective["allow_tools"] is True
    assert effective["allow_builtin_tools"] is False
    assert effective["sandbox_mode"] == "workspace-write"
    for feature in (
        "shell_tool",
        "unified_exec",
        "code_mode_host",
        "computer_use",
        "browser_use",
        "view_image",
        "multi_agent",
        "multi_agent_v2",
    ):
        assert f"features.{feature}=false" in args
    assert 'web_search="disabled"' in args
    entry = json.loads(Path(effective["model_catalog"]).read_text())["models"][0]
    assert entry["shell_type"] == "disabled"
    assert entry["apply_patch_tool_type"] is None
    assert entry["multi_agent_version"] is None
    descriptor = Tool("registered", "Synthetic", {"type": "object"}, None).descriptor()
    assert descriptor["type"] == "function"
    assert DynamicToolSpec.model_validate(descriptor).root.name == "registered"


@pytest.mark.parametrize(
    "item",
    [
        {"type": "commandExecution"},
        {"type": "fileChange"},
        {"type": "webSearch"},
        {"type": "browserToolCall"},
        {"type": "computerToolCall"},
        {"type": "collabAgentToolCall"},
        {"type": "dynamicToolCall", "tool": "unregistered"},
        {"type": "function_call", "name": "exec_command"},
    ],
)
def test_registered_tools_only_native_rejects_unregistered_activity(tmp_path, item):
    from types import SimpleNamespace

    class UnexpectedTool(Client):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.notifications = iter(
                [
                    ("item/started", {"item": item}),
                    ("turn/completed", {"turn": {"status": "interrupted"}}),
                ]
            )

        def next_turn_notification(self, turn_id):
            method, payload = next(self.notifications)
            return SimpleNamespace(method=method, payload=payload)

    async def check():
        journal = Journal(tmp_path / "events")
        native = Native(None, journal, client_factory=UnexpectedTool)
        await native.open()
        with pytest.raises(RuntimeError, match="Forbidden tool"):
            await native.run(
                "restricted",
                {},
                {},
                model="gpt-6.1-sol",
                effort="medium",
                allow_builtin_tools=False,
            )
        assert ("turn/interrupt", "lead") in native.client.calls
        rejected = await native._tool(
            {
                "threadId": "lead",
                "tool": "unregistered",
                "arguments": {},
            }
        )
        assert not rejected["success"]
        assert "Unregistered tool" in rejected["contentItems"][0]["text"]
        await native.close()
        journal.close()

    asyncio.run(check())


@pytest.mark.parametrize("resume", [False, True])
def test_native_start_and_resume_use_workspace_sandbox_and_explicit_cwd(
    tmp_path, resume
):
    from openai_codex.client import CodexConfig

    class LeafClient(Client):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.notifications = iter(
                [
                    (
                        "item/completed",
                        {
                            "item": {
                                "type": "agentMessage",
                                "phase": "final_answer",
                                "text": json.dumps(RESULT),
                            }
                        },
                    ),
                    ("turn/completed", {"turn": {"status": "completed"}}),
                ]
            )

    async def check():
        cwd = str(tmp_path / "work")
        journal = Journal(tmp_path / "events")
        native = Native(CodexConfig(cwd=cwd), journal, client_factory=LeafClient)
        await native.open()
        result = await native.run(
            "policy",
            {},
            RESULT_SCHEMA,
            model="gpt-6.1-sol",
            effort="medium",
            thread_id="existing" if resume else None,
        )
        assert result == RESULT
        assert native.client.definition["sandbox"] == "workspace-write"
        assert native.client.definition["cwd"] == cwd
        assert "permissions" not in native.client.definition
        await native.close()
        journal.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    "deltas,final,observed",
    [
        ([json.dumps({"value": "x" * 40})], None, 53),
        ([" " * 8] * 3, None, 24),
        ([], json.dumps({"value": "x" * 40}), 53),
    ],
)
def test_output_limit_interrupts_stream_or_oversized_final(
    tmp_path, deltas, final, observed
):
    from types import SimpleNamespace

    class LimitedClient(Client):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.interrupted = False
            self.read_after_interrupt = False
            self.release = threading.Event()
            events = [("item/agentMessage/delta", {"delta": delta}) for delta in deltas]
            if final is not None:
                events.append(
                    (
                        "item/completed",
                        {"item": {"type": "agentMessage", "text": final}},
                    )
                )
            self.notifications = iter(events)

        def next_turn_notification(self, turn_id):
            if self.interrupted:
                self.read_after_interrupt = True
                self.release.wait(5)
                raise RuntimeError("Transport never emitted turn/completed")
            method, payload = next(self.notifications)
            return SimpleNamespace(method=method, payload=payload)

        def turn_interrupt(self, thread_id, turn_id):
            super().turn_interrupt(thread_id, turn_id)
            # Acknowledge the interrupt without ever emitting turn/completed.
            self.interrupted = True

        def close(self):
            self.release.set()
            super().close()

    async def check():
        journal = Journal(tmp_path / "events")
        native = Native(None, journal, client_factory=LimitedClient)
        await native.open()
        try:
            with pytest.raises(
                RuntimeError, match="Native output exceeded max_output_chars"
            ):
                async with asyncio.timeout(1):
                    await native.run(
                        "bounded",
                        {},
                        {},
                        model="gpt-6.1-sol",
                        effort="medium",
                        max_output_chars=16,
                    )
            assert native.stopping and native.active
            assert not native.settled and not native.closed
            assert not native.client.read_after_interrupt
            assert native.client.calls.count(("turn/interrupt", "lead")) == 1
            await native.settle()
        finally:
            await native.close()
            journal.close()
        assert native.settled and native.closed
        rows = [
            json.loads(line) for line in (tmp_path / "events").read_text().splitlines()
        ]
        limits = [row for row in rows if row["event"] == "native.output_limit"]
        assert len(limits) == 1
        assert limits[0]["observed_chars"] == observed
        assert limits[0]["max_output_chars"] == 16
        assert set(limits[0]) == {"at", "event", "observed_chars", "max_output_chars"}

    asyncio.run(check())


@pytest.mark.parametrize("limit", [None, len(json.dumps(RESULT))])
def test_output_limit_preserves_default_and_does_not_double_count_final(
    tmp_path, limit
):
    from types import SimpleNamespace

    class CompleteClient(Client):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            text = json.dumps(RESULT)
            self.notifications = iter(
                [
                    ("item/agentMessage/delta", {"delta": text[:10]}),
                    ("item/agentMessage/delta", {"delta": text[10:]}),
                    (
                        "item/completed",
                        {"item": {"type": "agentMessage", "text": text}},
                    ),
                    ("turn/completed", {"turn": {"status": "completed"}}),
                ]
            )

        def next_turn_notification(self, turn_id):
            method, payload = next(self.notifications)
            return SimpleNamespace(method=method, payload=payload)

    async def check():
        journal = Journal(tmp_path / "events")
        native = Native(None, journal, client_factory=CompleteClient)
        await native.open()
        try:
            result = await native.run(
                "bounded",
                {},
                {},
                model="gpt-6.1-sol",
                effort="medium",
                max_output_chars=limit,
            )
            assert result == RESULT
            assert ("turn/interrupt", "lead") not in native.client.calls
        finally:
            await native.close()
            journal.close()

    asyncio.run(check())


@pytest.mark.parametrize("limit", [0, -1, True, False, 1.5, "16"])
def test_output_limit_requires_positive_integer_before_admission(tmp_path, limit):
    async def check():
        journal = Journal(tmp_path / "events")
        native = Native(None, journal, client_factory=Client)
        try:
            with pytest.raises(
                ValueError, match="max_output_chars must be a positive integer"
            ):
                await native.run(
                    "bounded",
                    {},
                    {},
                    model="gpt-6.1-sol",
                    effort="medium",
                    max_output_chars=limit,
                )
            assert native.client.definition is None
            assert native.client.calls == []
        finally:
            await native.close()
            journal.close()

    asyncio.run(check())
