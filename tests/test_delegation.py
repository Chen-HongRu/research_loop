"""Host-delegation compatibility mode without a model or remote service."""

import asyncio
import itertools
import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from research_loop import instructions
from research_loop.codex import Native
from research_loop.delegation import HostDelegation
from research_loop.state import Journal


class Object:
    def __init__(self, value):
        self.value = value

    def model_dump(self, **kwargs):
        return self.value


class Client:
    _ids = itertools.count(1)

    def __init__(self, *, complete=True, hold=False, violation=False):
        self.lock = threading.Lock()
        self.complete = complete
        self.violation = violation
        self.hold = threading.Event() if hold else None
        self.queues = {}
        self.threads = 0
        self.turns = 0
        self.interrupted = []
        self.definitions = []

    def start(self):
        pass

    def initialize(self):
        pass

    def close(self):
        pass

    def thread_start(self, definition):
        self.definitions.append(definition)
        with self.lock:
            self.threads += 1
            thread_id = f"thread-{next(self._ids)}"
        return Object(
            {
                "model": definition["model"],
                "reasoningEffort": definition["config"]["model_reasoning_effort"],
                "thread": {"id": thread_id},
            }
        )

    def turn_start(self, thread_id, message, params):
        with self.lock:
            self.turns += 1
            turn_id = f"turn-{next(self._ids)}"
        stream = queue.Queue()
        self.queues[turn_id] = stream

        def feed():
            if self.hold is not None:
                self.hold.wait(5)
            if self.violation:
                stream.put(
                    SimpleNamespace(
                        method="item/completed",
                        payload={"item": {"type": "collabAgentToolCall"}},
                    )
                )
            elif self.complete:
                stream.put(
                    SimpleNamespace(
                        method="item/completed",
                        payload={
                            "item": {
                                "type": "agentMessage",
                                "phase": "final_answer",
                                "text": "role summary",
                            }
                        },
                    )
                )
            stream.put(
                SimpleNamespace(
                    method="turn/completed",
                    payload={"turn": {"status": "completed"}},
                )
            )

        threading.Thread(target=feed, daemon=True).start()
        return Object({"turn": {"id": turn_id}})

    def next_turn_notification(self, turn_id):
        return self.queues[turn_id].get(timeout=5)

    def turn_interrupt(self, thread_id, turn_id):
        self.interrupted.append((thread_id, turn_id))
        self.queues[turn_id].put(
            SimpleNamespace(
                method="turn/completed",
                payload={"turn": {"status": "interrupted"}},
            )
        )

    def request(self, method, params, **kwargs):
        if method == "thread/loaded/list":
            return {"data": []}
        return {}

    def thread_read(self, thread_id, include_turns=False):
        return {"thread": {"status": {"type": "idle"}, "path": "synthetic.jsonl"}}


def delegator(tmp_path, client, **kwargs):
    return HostDelegation(
        client,
        Journal(tmp_path / "events.jsonl"),
        model="synthetic-model",
        effort="medium",
        roles_path=tmp_path / "roles.json",
        **kwargs,
    )


def wait_for(instance, ids, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = instance.wait(ids)["tasks"]
        if all(row["status"] in {"completed", "failed"} for row in rows):
            return rows
        time.sleep(0.01)
    raise AssertionError("delegated tasks did not finish")


def test_fresh_role_task_collects_a_summary_and_records_identity(tmp_path):
    client = Client()
    instance = delegator(tmp_path, client)
    started = instance.start("scout", "scout-frontier", "Inspect the supplied sources.")
    assert started["status"] == "queued"
    rows = wait_for(instance, [started["task_id"]])
    assert rows[0]["status"] == "completed"
    assert rows[0]["result"] == "role summary"
    instance.settle()
    instance.close()

    definition = client.definitions[0]
    assert definition["dynamicTools"] == []
    assert "Research role templates" not in definition["developerInstructions"]
    assert "## Scout" in definition["developerInstructions"]
    assert "Do not spawn or delegate" in definition["developerInstructions"]

    roles = json.loads((tmp_path / "roles.json").read_text())
    child = next(iter(roles["threads"].values()))
    assert child == {
        "role": "scout",
        "task": "scout-frontier",
        "task_id": started["task_id"],
        "status": "completed",
    }
    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    assert [row["event"] for row in events] == [
        "role.task.queued",
        "role.task.started",
        "role.task.finished",
    ]


def test_delegation_validates_roles_names_and_active_limit(tmp_path):
    client = Client(hold=True)
    instance = delegator(tmp_path, client, max_active=1)
    with pytest.raises(ValueError, match="Unknown research role"):
        instance.start("manager", "task", "message")
    with pytest.raises(ValueError, match="task_name"):
        instance.start("scout", "Not A Name", "message")
    with pytest.raises(ValueError, match="non-empty"):
        instance.start("scout", "task", "")
    first = instance.start("scout", "first", "message")
    with pytest.raises(ValueError, match="At most 1"):
        instance.start("analyst", "second", "message")
    client.hold.set()
    wait_for(instance, [first["task_id"]])
    instance.close()


def test_native_delegation_violation_fails_the_role_task(tmp_path):
    client = Client(violation=True)
    instance = delegator(tmp_path, client)
    started = instance.start("critic", "critic-check", "Review the claim.")
    rows = wait_for(instance, [started["task_id"]])
    assert rows[0]["status"] == "failed"
    assert "native delegation" in rows[0]["error"]
    instance.settle()
    instance.close()


def test_stop_interrupts_a_running_role_task_and_settles(tmp_path):
    client = Client(hold=True)
    instance = delegator(tmp_path, client)
    started = instance.start("experimenter", "smoke", "Prepare a smoke.")
    time.sleep(0.05)
    instance.stop()
    instance.settle()
    rows = instance.wait([started["task_id"]])["tasks"]
    assert rows[0]["status"] == "failed"
    assert client.interrupted
    instance.close()


def test_native_installs_host_tools_and_settles_child_threads(tmp_path):
    async def check():
        client = Client()
        journal = Journal(tmp_path / "events.jsonl")
        native = Native(None, journal, client_factory=lambda **kwargs: client)
        await native.open()
        native.install_delegation("synthetic-model", "medium", tmp_path / "roles.json")
        assert {"delegate_task", "wait_tasks"} <= set(native.tools)
        started = await native.tools["delegate_task"].call(
            {
                "role": "analyst",
                "task_name": "analysis",
                "message": "Analyze the synthetic result.",
            }
        )
        while True:
            status = await native.tools["wait_tasks"].call(
                {"task_ids": [started["task_id"]]}
            )
            if status["tasks"][0]["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        await native.settle()
        assert len(native.children) == 1
        await native.close()
        journal.close()

    asyncio.run(check())


def test_compat_instructions_use_host_tools_and_the_same_role_contract():
    native = instructions.instructions("native")
    compat = instructions.instructions("host")
    assert 'fork_turns="none"' in native
    assert "Research role templates follow" in native
    assert "delegate_task" in compat and "wait_tasks" in compat
    assert "spawn_agent" not in compat
    assert "Research role templates follow" not in compat
    assert "wait_tasks" in compat and "does not block" in compat
    with pytest.raises(ValueError, match="native or host"):
        instructions.instructions("other")
    scout = instructions.role_instructions("scout")
    assert "## Common contract" in scout and "## Scout" in scout
    assert "Do not spawn or delegate" in scout
    with pytest.raises(ValueError, match="Unknown research role"):
        instructions.role_instructions("reviewer")


def test_role_identity_survives_a_new_delegator_and_names_are_unique(tmp_path):
    first_client = Client()
    first = delegator(tmp_path, first_client)
    one = first.start("scout", "frontier", "Synthetic assignment")
    wait_for(first, [one["task_id"]])
    first.close()

    second_client = Client()
    second = delegator(tmp_path, second_client)
    two = second.start("analyst", "results", "Synthetic assignment")
    wait_for(second, [two["task_id"]])
    with pytest.raises(ValueError, match="already used"):
        second.start("scout", "results", "Another assignment")
    second.close()

    roles = json.loads((tmp_path / "roles.json").read_text())["threads"]
    assert sorted(row["role"] for row in roles.values()) == ["analyst", "scout"]


class BlockingStart(Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entered = threading.Event()
        self.release = threading.Event()

    def thread_start(self, definition):
        self.entered.set()
        assert self.release.wait(5)
        return super().thread_start(definition)


def test_delegate_task_returns_before_the_child_starts(tmp_path):
    client = BlockingStart()
    instance = delegator(tmp_path, client)
    began = time.monotonic()
    started = instance.start("scout", "frontier", "Synthetic assignment")
    elapsed = time.monotonic() - began
    assert started["status"] == "queued"
    assert elapsed < 1
    assert client.entered.wait(5)
    client.release.set()
    rows = wait_for(instance, [started["task_id"]])
    assert rows[0]["status"] == "completed"
    instance.settle()
    instance.close()


def test_v1_native_spawn_function_call_fails_the_role_task(tmp_path):
    class V1Spawn(Client):
        def turn_start(self, thread_id, message, params):
            with self.lock:
                self.turns += 1
                turn_id = f"turn-{next(self._ids)}"
            stream = queue.Queue()
            self.queues[turn_id] = stream
            stream.put(
                SimpleNamespace(
                    method="item/completed",
                    payload={
                        "item": {
                            "type": "function_call",
                            "name": "spawn_agent",
                            "arguments": "{}",
                        }
                    },
                )
            )
            stream.put(
                SimpleNamespace(
                    method="turn/completed",
                    payload={"turn": {"status": "interrupted"}},
                )
            )
            return Object({"turn": {"id": turn_id}})

    client = V1Spawn()
    instance = delegator(tmp_path, client)
    started = instance.start("scout", "frontier", "Synthetic assignment")
    rows = wait_for(instance, [started["task_id"]])
    assert rows[0]["status"] == "failed"
    assert "native delegation" in rows[0]["error"]
    assert client.interrupted
    instance.close()


def test_malformed_roles_mapping_aborts_before_model_work(tmp_path):
    path = tmp_path / "roles.json"
    path.write_text(json.dumps({"version": 1, "threads": []}))
    with pytest.raises(RuntimeError, match="invalid shape"):
        delegator(tmp_path, Client())
    path.write_text("{not json")
    with pytest.raises(RuntimeError, match="unreadable"):
        delegator(tmp_path, Client())


def test_settle_timeout_interrupts_a_live_role(tmp_path):
    client = Client(hold=True)
    instance = delegator(tmp_path, client)
    started = instance.start("experimenter", "smoke", "Prepare a smoke.")
    instance.settle(timeout=0.05)
    rows = instance.wait([started["task_id"]])["tasks"]
    assert rows[0]["status"] == "failed"
    assert client.interrupted
    instance.close()
