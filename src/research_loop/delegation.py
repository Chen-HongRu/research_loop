"""Host-driven role tasks for provider-compatible research delegation.

Codex native V2 delegation sends role tasks as OpenAI-specific agent messages.
This module implements the same orchestration contract with plain host threads:
each role task is a new Codex thread whose first user message is the complete
assignment, and the lead collects summaries through polling tool calls.
"""

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .instructions import ROLE_NAMES, role_instructions
from .state import now, write_json

MAX_ACTIVE = 4
TASK_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

DELEGATE_TOOL = {
    "name": "delegate_task",
    "description": (
        "Start a fresh research role context. The host adds the common contract "
        "and role template; message must be the complete self-contained "
        "assignment. Returns immediately with a task id."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "role": {"type": "string", "enum": list(ROLE_NAMES)},
            "task_name": {
                "type": "string",
                "description": "Unique short name for this assignment",
            },
            "message": {
                "type": "string",
                "description": "Complete self-contained role assignment",
            },
        },
        "required": ["role", "task_name", "message"],
        "additionalProperties": False,
    },
}

WAIT_TOOL = {
    "name": "wait_tasks",
    "description": (
        "Return the current status of delegated tasks, including final summaries "
        "for finished tasks. This does not block; if work is pending, do other "
        "useful coordination and check again."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Task ids to inspect; empty means all tasks",
            }
        },
        "required": ["task_ids"],
        "additionalProperties": False,
    },
}


def _plain(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    return value


@dataclass
class RoleTask:
    id: str
    role: str
    name: str
    message: str
    status: str = "queued"
    thread_id: str | None = None
    turn_id: str | None = None
    result: str | None = None
    error: str | None = None
    queued_at: str = field(default_factory=now)
    ended_at: str | None = None


class HostDelegation:
    """Run role assignments as independent Codex threads.

    Native Codex children cannot see host dynamic tools, so the lead receives
    delegate_task/wait_tasks while the host itself starts and drains each child
    turn. The client's reader thread must stay free, so tool calls only queue or
    snapshot work and never wait for a child turn.
    """

    def __init__(
        self,
        client,
        journal,
        *,
        model,
        effort,
        roles_path=None,
        on_thread=None,
        max_active=MAX_ACTIVE,
    ):
        self.client = client
        self.journal = journal
        self.model = model
        self.effort = effort
        self.roles_path = Path(roles_path) if roles_path else None
        self.on_thread = on_thread
        self.max_active = max_active
        self.roles = {}
        if self.roles_path is not None and self.roles_path.exists():
            try:
                document = json.loads(self.roles_path.read_text())
                if isinstance(document.get("threads"), dict):
                    self.roles = document["threads"]
            except (OSError, ValueError):
                pass
        self.tasks = {}
        self._counter = 0
        self._threads = []
        self._lock = threading.Lock()
        self._record_lock = threading.Lock()
        self._stopping = False

    def start(self, role, task_name, message):
        if role not in ROLE_NAMES:
            raise ValueError(f"Unknown research role: {role}")
        if not TASK_NAME.fullmatch(task_name or ""):
            raise ValueError(
                "task_name uses lowercase letters, digits, dot, underscore or hyphen"
            )
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty role assignment")
        with self._lock:
            if self._stopping:
                raise ValueError("Stop requested before role admission")
            active = sum(
                task.status in {"queued", "running"} for task in self.tasks.values()
            )
            if active >= self.max_active:
                raise ValueError(f"At most {self.max_active} delegated tasks active")
            if any(task.name == task_name for task in self.tasks.values()):
                raise ValueError(f"task_name already used: {task_name}")
            self._counter += 1
            task = RoleTask(
                id=f"{role}-{self._counter:03d}",
                role=role,
                name=task_name,
                message=message,
            )
            self.tasks[task.id] = task
        self._record()
        self.journal.emit(
            "role.task.queued",
            task_id=task.id,
            role=role,
            task_name=task_name,
        )
        worker = threading.Thread(target=self._run, args=(task,), daemon=True)
        self._threads.append(worker)
        worker.start()
        return {"task_id": task.id, "role": role, "status": "queued"}

    def wait(self, task_ids):
        with self._lock:
            if task_ids:
                missing = [item for item in task_ids if item not in self.tasks]
                if missing:
                    raise ValueError(f"Unknown task ids: {', '.join(missing)}")
                selected = [self.tasks[item] for item in task_ids]
            else:
                selected = list(self.tasks.values())
            rows = [
                {
                    "task_id": task.id,
                    "role": task.role,
                    "task_name": task.name,
                    "status": task.status,
                    "thread_id": task.thread_id,
                    **({"result": task.result} if task.status == "completed" else {}),
                    **({"error": task.error} if task.error else {}),
                }
                for task in selected
            ]
        return {"tasks": rows}

    def stop(self):
        with self._lock:
            self._stopping = True
            running = [
                (task.thread_id, task.turn_id)
                for task in self.tasks.values()
                if task.status == "running" and task.thread_id and task.turn_id
            ]
        for thread_id, turn_id in running:
            try:
                self.client.turn_interrupt(thread_id, turn_id)
            except Exception:
                pass

    def settle(self, timeout=30):
        deadline = time.monotonic() + timeout
        for worker in self._threads:
            worker.join(max(0, deadline - time.monotonic()))
        if any(worker.is_alive() for worker in self._threads):
            raise RuntimeError(
                "Host-delegated roles are still active; retain the draft and "
                "inspect the original thread histories"
            )

    def close(self):
        self.stop()
        try:
            self.settle()
        except RuntimeError:
            pass

    def _run(self, task):
        final = ""
        try:
            if self._stopping:
                raise RuntimeError("Stop requested before role admission")
            started = _plain(
                self.client.thread_start(
                    {
                        "model": self.model,
                        "allowProviderModelFallback": False,
                        "developerInstructions": role_instructions(task.role),
                        "permissions": "research",
                        "approvalPolicy": "never",
                        "config": {"model_reasoning_effort": self.effort},
                        "dynamicTools": [],
                    }
                )
            )
            if (
                started.get("model") != self.model
                or started.get("reasoningEffort") != self.effort
            ):
                raise RuntimeError("Codex changed the role model or reasoning effort")
            thread_id = started["thread"]["id"]
            with self._lock:
                task.thread_id = thread_id
                task.status = "running"
            if self.on_thread:
                self.on_thread(thread_id)
            self._record()
            self.journal.emit(
                "role.task.started",
                task_id=task.id,
                role=task.role,
                task_name=task.name,
                thread_id=thread_id,
                model=started.get("model"),
                effort=started.get("reasoningEffort"),
            )
            turn = _plain(
                self.client.turn_start(thread_id, task.message, {"effort": self.effort})
            )
            turn_id = turn["turn"]["id"]
            with self._lock:
                task.turn_id = turn_id
            status = "completed"
            error = None
            violation = None
            while True:
                notification = self.client.next_turn_notification(turn_id)
                method = notification.method
                payload = _plain(notification.payload)
                item = payload.get("item", {}) if isinstance(payload, dict) else {}
                if method == "item/completed" and item.get("type") in {
                    "collabAgentToolCall",
                    "subAgentActivity",
                }:
                    violation = (
                        "A delegated role attempted native delegation; roles must "
                        "return proposed work to the coordinator"
                    )
                    try:
                        self.client.turn_interrupt(thread_id, turn_id)
                    except Exception:
                        pass
                    continue
                if (
                    method == "item/completed"
                    and item.get("type") == "agentMessage"
                    and item.get("phase") in (None, "final_answer")
                ):
                    final = item.get("text", "")
                if method == "turn/completed":
                    turn_data = payload["turn"]
                    if violation is not None:
                        status = "failed"
                        error = violation
                    elif turn_data["status"] != "completed":
                        status = "failed"
                        error = (
                            f"Role turn ended: {turn_data['status']}; "
                            f"{turn_data.get('error')}"
                        )
                    break
            if status == "completed" and not final.strip():
                status = "failed"
                error = "Role turn completed without a final summary"
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            with self._lock:
                thread_id, turn_id = task.thread_id, task.turn_id
            if thread_id and turn_id:
                try:
                    self.client.turn_interrupt(thread_id, turn_id)
                except Exception:
                    pass
            self._finish(task, "failed", None, message)
            return
        self._finish(task, status, final if status == "completed" else None, error)

    def _finish(self, task, status, result, error):
        with self._lock:
            task.status = status
            task.result = result
            task.error = error
            task.ended_at = now()
        self._record()
        self.journal.emit(
            "role.task.finished",
            task_id=task.id,
            role=task.role,
            task_name=task.name,
            thread_id=task.thread_id,
            status=status,
            result_chars=len(result or ""),
            error=error,
        )

    def _record(self):
        if self.roles_path is None:
            return
        with self._lock:
            for task in self.tasks.values():
                if task.thread_id:
                    self.roles[task.thread_id] = {
                        "role": task.role,
                        "task": task.name,
                        "task_id": task.id,
                        "status": task.status,
                    }
            threads = dict(self.roles)
        with self._record_lock:
            write_json(self.roles_path, {"version": 1, "threads": threads})
