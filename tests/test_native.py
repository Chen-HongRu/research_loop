"""Transport decisions and factual history, without a model or remote service."""

import asyncio
import json
import threading
from pathlib import Path

import pytest
from openai_codex.models import Notification, UnknownNotification
from openai_codex.generated.v2_all import (
    ItemCompletedNotification,
    TurnCompletedNotification,
)

from research_loop.codex import Native, Tool, configuration
from research_loop.instructions import RESULT_SCHEMA
from research_loop.state import Journal

RESULT = {
    "summary": "合成测试",
    "goal": "unknown",
    "evidence": [],
    "limitations": ["Synthetic transport, no model"],
    "next_action": "",
    "study_action": "stop",
    "stop_reason": "Synthetic transport check is complete; no research work authorized.",
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
                            "type": "collabAgentToolCall",
                            "receiverThreadIds": ["review"],
                        }
                    },
                ),
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


def test_native_schema_delegation_history_and_no_host_science_gate(tmp_path):
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
        assert ("thread/read", "review") in native.client.calls
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
            row["event"] == "codex.history.reference" and row["thread_id"] == "review"
            for row in rows
        )

    asyncio.run(check())


def test_experimental_notification_keeps_factual_delegate_history(tmp_path):
    async def check():
        class ExperimentalClient(Client):
            def next_turn_notification(self, turn_id):
                if not getattr(self, "sent_unknown", False):
                    self.sent_unknown = True
                    return Notification(
                        method="item/completed",
                        payload=UnknownNotification(
                            params={
                                "threadId": "lead",
                                "turnId": turn_id,
                                "item": {
                                    "type": "subAgentActivity",
                                    "agentThreadId": "fresh-child",
                                    "kind": "completed",
                                },
                            }
                        ),
                    )
                return super().next_turn_notification(turn_id)

        journal = Journal(tmp_path / "events")
        native = Native(None, journal, client_factory=ExperimentalClient)
        await native.open()
        assert (
            await native.run(
                "lead", {}, RESULT_SCHEMA, model="gpt-6.1-sol", effort="medium"
            )
            == RESULT
        )
        assert "fresh-child" in native.children
        await native.settle()
        assert ("thread/read", "fresh-child") in native.client.calls
        await native.close()
        journal.close()

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


def test_native_configuration_isolates_secrets_and_enables_delegation(
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
    assert "features.multi_agent=true" in args
    assert "features.multi_agent_v2_dynamic_tools=false" in args
    assert not any("SYNTHETIC_NOT_A_CREDENTIAL" in value for value in args)
    assert effective["filesystem"][str((home / ".codex").resolve())] == "deny"
    assert effective["filesystem"][str((root / "work").resolve())] == "write"


def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_PROVIDER_KEY", "SYNTHETIC_NOT_A_CREDENTIAL")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "owner")
    return {
        "name": "synthetic",
        "base_url": "https://provider.example/v1",
        "api_key_env": "SYNTHETIC_PROVIDER_KEY",
        "model_catalog": None,
        "context_window": 65536,
    }


def test_provider_configuration_keeps_the_key_out_of_records(tmp_path, monkeypatch):
    settings = provider(tmp_path, monkeypatch)
    root = tmp_path / "run"
    root.mkdir()
    config, effective = configuration(
        root,
        tmp_path,
        [root / "work"],
        "synthetic-model",
        "medium",
        provider=settings,
    )
    args = config.launch_args_override
    assert "features.multi_agent=true" in args
    assert "features.multi_agent_v2=false" in args
    assert 'web_search="disabled"' in args
    assert 'model_provider="synthetic"' in args
    assert not (root / "native/auth.json").exists()
    catalog = json.loads((root / "native/models.json").read_text())
    entry = catalog["models"][0]
    assert entry["slug"] == "synthetic-model"
    assert entry["multi_agent_version"] == "v1"
    assert entry["supports_search_tool"] is False
    assert "medium" in [item["effort"] for item in entry["supported_reasoning_levels"]]
    assert effective["model_provider"] == "synthetic"
    assert effective["multi_agent_version"] == "v1"
    assert effective["web_search"] is False
    key_tokens = [
        token for token in args if token.startswith("SYNTHETIC_PROVIDER_KEY=")
    ]
    assert key_tokens == ["SYNTHETIC_PROVIDER_KEY=SYNTHETIC_NOT_A_CREDENTIAL"]
    assert not any(
        "SYNTHETIC_NOT_A_CREDENTIAL" in value
        for value in effective.values()
        if isinstance(value, str)
    )


def test_provider_configuration_requires_the_key_environment(tmp_path, monkeypatch):
    settings = provider(tmp_path, monkeypatch)
    monkeypatch.delenv("SYNTHETIC_PROVIDER_KEY")
    root = tmp_path / "run"
    root.mkdir()
    with pytest.raises(RuntimeError, match="SYNTHETIC_PROVIDER_KEY"):
        configuration(
            root,
            tmp_path,
            [root / "work"],
            "synthetic-model",
            "medium",
            provider=settings,
        )


def test_provider_verify_accepts_the_generated_catalog_without_chatgpt(tmp_path):
    async def check():
        class ProviderClient(Client):
            def request(self, method, params, **kwargs):
                if method == "account/read":
                    return {"account": None, "requiresOpenaiAuth": False}
                if method == "model/list":
                    return {
                        "data": [
                            {
                                "model": "synthetic-model",
                                "supportedReasoningEfforts": [
                                    {"reasoningEffort": "medium"}
                                ],
                            }
                        ]
                    }
                return super().request(method, params, **kwargs)

        journal = Journal(tmp_path / "events")
        native = Native(None, journal, client_factory=ProviderClient)
        await native.open()
        with pytest.raises(RuntimeError, match="ChatGPT subscription"):
            await native.verify("synthetic-model", "medium")
        await native.verify("synthetic-model", "medium", provider={"name": "synthetic"})
        with pytest.raises(RuntimeError, match="provider model/reasoning effort"):
            await native.verify("other-model", "medium", provider={"name": "synthetic"})
        await native.close()
        journal.close()

    asyncio.run(check())


def test_supplied_provider_catalog_must_use_plaintext_v1_delegation(
    tmp_path, monkeypatch
):
    settings = provider(tmp_path, monkeypatch)
    root = tmp_path / "run"
    root.mkdir()
    entry = {
        "slug": "synthetic-model",
        "multi_agent_version": "v2",
        "supports_search_tool": True,
    }
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"models": [entry]}))
    settings["model_catalog"] = str(catalog)

    def configure():
        return configuration(
            root,
            tmp_path,
            [root / "work"],
            "synthetic-model",
            "medium",
            provider=settings,
        )

    with pytest.raises(RuntimeError, match="multi_agent_version v1"):
        configure()
    entry["multi_agent_version"] = "v1"
    catalog.write_text(json.dumps({"models": [entry]}))
    with pytest.raises(RuntimeError, match="supports_search_tool=false"):
        configure()
    entry["supports_search_tool"] = False
    catalog.write_text(json.dumps({"models": [entry]}))
    _, effective = configure()
    assert effective["model_catalog"] == str(catalog)
