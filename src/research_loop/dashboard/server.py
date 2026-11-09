"""Read-only presentation of public Codex messages, delegates and tool activity."""

import argparse
import json
import os
import re
import shutil
import subprocess
import threading
from collections import OrderedDict
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PUBLIC_PHASES = {None, "commentary", "partial_answer", "final_answer"}
TOOL_TITLES = {
    "gpu_status": "查看资源预算",
    "gpu_exec": "运行 GPU 任务",
    "gpu_poll": "等待或读取 GPU 输出",
    "gpu_reconcile": "核对原作业结算",
    "gpu_release": "释放 GPU 分配",
}
# Display-only translations of task names chosen by the lead, not prescribed roles.
TASK_WORDS = {
    "probe": "诊断",
    "replication": "复现",
    "plan": "方案",
    "crossover": "交叉对照",
    "continuation": "采样续接",
    "script": "脚本实现",
    "review": "审查",
    "source": "源码",
    "audit": "核查",
    "verify": "核验",
}


ROLE_LABELS = {
    "Scout": "探索",
    "Proposer": "方案",
    "Experimenter": "实验",
    "Analyst": "分析",
    "Critic": "独立批判",
    "unknown": "未确认角色",
}
ARTIFACT_LIMIT = 64 * 1024


def task_role(task):
    """Only explicit role words in the task name are evidence of a role."""
    words = set(re.split(r"[^a-z]+", task.lower()))
    roles = [role for role in ROLE_LABELS if role.lower() in words]
    return roles[0] if len(roles) == 1 else "unknown"


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError):
        return None


def artifact(root, names):
    """Read named Markdown work products, bounded and confined to work_root."""
    missing = {"text": "", "path": None, "at": None, "available": False}
    work = root / "work"
    for name in names:
        path = work / name
        try:
            # Reject symlinks, including a redirected work directory.
            if work.is_symlink() or path.is_symlink():
                continue
            if not path.resolve().is_relative_to(work.resolve()):
                continue
            with path.open("rb") as stream:
                raw = stream.read(ARTIFACT_LIMIT + 1)
            text = raw[:ARTIFACT_LIMIT].decode("utf-8", errors="replace")
            if len(raw) > ARTIFACT_LIMIT:
                text += "\n\n[内容超过 64 KiB，已截断]"
            return {
                "text": text,
                "path": f"work/{name}",
                "at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
                "available": True,
            }
        except OSError:
            continue
    return missing


def iteration_metadata(root):
    rows = []
    for path in sorted((root / "iterations").glob("[0-9]*/status.json")):
        value = read_json(path, {})
        if not isinstance(value, dict):
            continue
        start = timestamp(value.get("started_at"))
        iteration = value.get("iteration")
        if start is not None and isinstance(iteration, int):
            rows.append((start, iteration))
    return sorted(rows)


def research_state(root, status):
    current = status.get("iteration")
    latest = None
    for path in sorted((root / "iterations").glob("[0-9]*/result.json"), reverse=True):
        if not path.parent.name.isdecimal():
            continue
        iteration = int(path.parent.name)
        if isinstance(current, int) and iteration > current:
            continue
        result = read_json(path)
        if isinstance(result, dict) and isinstance(result.get("summary"), str):
            latest = path, iteration, result
            break
    summary = {
        "text": "",
        "source": None,
        "at": None,
        "iteration": None,
        "provisional": True,
        "stale": False,
    }
    next_action = {key: value for key, value in summary.items() if key != "at"}
    if latest:
        path, iteration, result = latest
        stale = isinstance(current, int) and iteration != current
        for key, target in (("summary", summary), ("next_action", next_action)):
            target.update(
                text=public_text(result.get(key)),
                source=f"{path.relative_to(root)}#{key}",
                iteration=iteration,
                provisional=stale,
                stale=stale,
            )
        summary["at"] = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
    prefix = f"iteration{current}" if isinstance(current, int) else ""
    return {
        "summary": summary,
        "next_action": next_action,
        "coordination": artifact(
            root,
            [
                f"coordination{current}.md",
                f"{prefix}-coordination.md",
                "coordination.md",
                f"{prefix}-plan.md",
            ],
        ),
        "frontier": artifact(
            root, [f"frontier{current}.md", f"{prefix}-frontier.md", "frontier.md"]
        ),
        "policy": {
            "name": "Scout / Proposer / Experimenter / Analyst / Critic",
            "application": "角色政策展示；实际调用与 fresh context 遵循情况未核验",
        },
    }


def supervisor_process(supervisor):
    """Resolve a managed service live; a saved PID is only a fallback."""
    service = supervisor.get("service", "")
    if (
        isinstance(service, str)
        and re.fullmatch(r"[A-Za-z0-9_.@:-]+\.service", service)
        and shutil.which("systemctl")
    ):
        try:
            result = subprocess.run(
                [
                    "systemctl",
                    "show",
                    "-p",
                    "MainPID",
                    "-p",
                    "ActiveState",
                    "--",
                    service,
                ],
                capture_output=True,
                text=True,
                timeout=2,
            )
            values = dict(
                line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
            )
            pid = int(values.get("MainPID", "0")) or None
            if result.returncode == 0:
                if values.get("ActiveState") == "active" and pid:
                    return pid, "alive"
                if values.get("ActiveState") in {"inactive", "failed"}:
                    return pid, "dead"
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        # Failed service inspection does not prove the recorded process died.
        return None, "unknown"
    pid, alive = supervisor.get("pid"), "unknown"
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        try:
            os.kill(pid, 0)
            alive = "alive"
        except ProcessLookupError:
            alive = "dead"
        except (PermissionError, OSError):
            pass
    return pid, alive


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


METRICS_LIMIT = 2 * 1024 * 1024


def _metrics_file(root, name):
    """Read one bounded JSON object contained by the resolved metrics root."""
    try:
        base = (root / "metrics").resolve(strict=True)
        path = (base / name).resolve(strict=True)
        if not path.is_relative_to(base):
            return None, "invalid"
        with path.open("rb") as stream:
            data = stream.read(METRICS_LIMIT + 1)
        if len(data) > METRICS_LIMIT:
            return None, "invalid"
        value = json.loads(data)
        return (value, "available") if isinstance(value, dict) else (None, "invalid")
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError, RuntimeError):
        return None, "invalid"


def _fields(value, keys):
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in keys
        if key in value and isinstance(value[key], (str, int, float, bool))
    }


def metrics_snapshot(root):
    value, status = _metrics_file(root, "current/latest.json")
    if value is None:
        return {"available": False, "status": status}
    if not isinstance(value.get("phase"), str) or not isinstance(
        value.get("llm"), dict
    ):
        return {"available": False, "status": "invalid"}
    llm = value["llm"]
    result = {
        "available": True,
        "status": "available",
        "phase": value["phase"],
        "last_source_event_at": value.get("last_source_event_at")
        if isinstance(value.get("last_source_event_at"), str)
        else None,
        "llm_measurable": llm.get("measurable")
        if isinstance(llm.get("measurable"), bool)
        else None,
        "window": _fields(value.get("window"), ("since", "until")),
        "coverage": _fields(
            value.get("coverage"),
            (
                "complete",
                "session_files_discovered",
                "selected_threads",
                "excluded_session_threads",
                "usage_records_observed",
            ),
        ),
        "llm": _fields(
            llm.get("total"),
            (
                "input_tokens",
                "cached_input_tokens",
                "uncached_input_tokens",
                "output_tokens",
                "responses",
            ),
        ),
        "resources": _fields(
            value.get("resources"),
            ("calls", "failed_calls", "successful_empty_running_gpu_polls"),
        ),
        "allocation": _fields(
            value.get("allocation"),
            ("available", "dispatched_jobs", "elapsed_seconds", "unknown_jobs"),
        ),
        "reader": _fields(
            value.get("reader"),
            ("bytes_read", "elapsed_seconds", "previous_poll_total_seconds"),
        ),
    }
    tasks = llm.get("by_task")
    if isinstance(tasks, list) and all(
        isinstance(t, dict)
        and all(k in t for k in ("role", "active_in_phase", "completed_at", "mixed"))
        for t in tasks
    ):
        children = [
            t for t in tasks if t["role"] != "lead" and t["active_in_phase"] is True
        ]
        result["tasks"] = {
            "completed": sum(
                bool(t["completed_at"]) and t["mixed"] is False for t in children
            ),
            "pending": sum(not t["completed_at"] for t in children),
            "mixed": sum(t["mixed"] is True for t in children),
        }
    roles = llm.get("by_role")
    if isinstance(roles, dict) and all(
        isinstance(r, dict) and isinstance(r.get("wait_timeouts"), int)
        for r in roles.values()
    ):
        result["wait_timeouts"] = sum(r["wait_timeouts"] for r in roles.values())
    comparison, comparison_status = _metrics_file(root, "comparison.json")
    result["comparison_status"] = comparison_status
    if comparison is not None:
        phases = {}
        for name in ("baseline", "optimized"):
            item = comparison.get(name)
            if not isinstance(item, dict):
                result["comparison_status"] = "invalid"
                break
            totals = item.get("totals") if isinstance(item.get("totals"), dict) else {}
            phases[name] = {
                "phase": item.get("phase")
                if isinstance(item.get("phase"), str)
                else None,
                "tasks": _fields(
                    item.get("task_cohorts"), ("completed_unmixed", "pending", "mixed")
                ),
                "coverage": _fields(
                    item.get("coverage"), ("complete", "usage_records_observed")
                ),
                "llm": _fields(
                    totals.get("llm"),
                    (
                        "input_tokens",
                        "cached_input_tokens",
                        "uncached_input_tokens",
                        "output_tokens",
                        "responses",
                    ),
                ),
            }
        else:
            result["comparison"] = phases
    return result


def public_text(value):
    """Select explicit message text, never reasoning or encrypted content."""
    if isinstance(value, str):
        return "" if value.startswith("gAAAAA") else value
    if isinstance(value, list):
        return "\n".join(
            public_text(item.get("text", ""))
            for item in value
            if isinstance(item, dict) and item.get("type") in {"text", "output_text"}
        ).strip()
    return ""


def message(identifier, at, text, phase=None):
    return {"id": identifier, "at": at, "text": text, "phase": phase}


class Lines:
    """Read only newly appended complete JSONL records."""

    def __init__(self, path):
        self.path = path
        self.identity = None
        self.offset = 0
        self.pending = b""

    def drain(self):
        try:
            stat = self.path.stat()
            identity = stat.st_dev, stat.st_ino
            reset = self.identity is not None and (
                identity != self.identity or stat.st_size < self.offset
            )
            if reset:
                self.offset, self.pending = 0, b""
            self.identity = identity
            with self.path.open("rb") as stream:
                stream.seek(self.offset)
                chunk = stream.read()
                self.offset = stream.tell()
        except OSError:
            return [], False
        lines = (self.pending + chunk).split(b"\n")
        self.pending = lines.pop()
        records = []
        for line in lines:
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    records.append(value)
            except (ValueError, UnicodeDecodeError):
                continue
        return records, reset


class Session:
    """The pinned native rollout is the available source of child replies."""

    def __init__(self, path):
        self.reader = Lines(path)
        self.reset()

    def reset(self):
        self.id = None
        self.parent_id = None
        self.path = ""
        self.name = ""
        self.messages = OrderedDict()
        self.status = "unknown"
        self.last_at = None
        self.started_at = None
        self.tool_count = 0

    def update(self):
        records, reset = self.reader.drain()
        if reset:
            self.reset()
        for record in records:
            payload = record.get("payload") or {}
            kind, typ = record.get("type"), payload.get("type")
            at = record.get("timestamp")
            if kind == "session_meta":
                self.id = payload.get("id")
                self.started_at = payload.get("timestamp") or at
                source = payload.get("source")
                if isinstance(source, dict):
                    spawn = (source.get("subagent") or {}).get("thread_spawn") or {}
                    self.parent_id = spawn.get("parent_thread_id")
                    self.path = spawn.get("agent_path") or ""
                    self.name = spawn.get("agent_nickname") or ""
            elif kind == "event_msg":
                if typ == "task_started":
                    self.status, self.last_at = "working", at
                elif typ == "task_complete":
                    self.status, self.last_at = "completed", at
                    text = public_text(payload.get("last_agent_message"))
                    if text:
                        existing = next(
                            (m for m in self.messages.values() if m["text"] == text),
                            None,
                        )
                        if existing:
                            existing["phase"] = "final_answer"
                        else:
                            identifier = payload.get("turn_id") or f"final-{at}"
                            self.messages[identifier] = message(
                                identifier, at, text, "final_answer"
                            )
                elif typ in {"turn_aborted", "task_failed"}:
                    self.status = "failed" if typ == "task_failed" else "interrupted"
                    self.last_at = at
            elif kind == "response_item":
                if typ == "message" and payload.get("role") == "assistant":
                    phase = payload.get("phase")
                    if phase in PUBLIC_PHASES:
                        text = public_text(payload.get("content"))
                        if text:
                            identifier = payload.get("id") or f"message-{at}"
                            self.messages[identifier] = message(
                                identifier, at, text, phase
                            )
                            self.last_at = at
                elif typ in {"function_call", "custom_tool_call"}:
                    self.tool_count += 1

    def card(self, activity=None):
        activity = activity or {}
        path = self.path or activity.get("path") or ""
        task = path.rsplit("/", 1)[-1]
        title = "".join(TASK_WORDS.get(word, word) for word in task.split("_"))
        messages = list(self.messages.values())
        finals = [m for m in messages if m["phase"] == "final_answer"]
        introductions = [m for m in messages if m["phase"] == "commentary"]
        status = (
            self.status
            if self.status != "unknown"
            else activity.get("status", "unknown")
        )
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "name": self.name,
            "task_name": task,
            "role": task_role(task),
            "role_label": ROLE_LABELS[task_role(task)],
            "role_source": "inferred-from-task-name"
            if task_role(task) != "unknown"
            else "unknown",
            "fresh_context": "unknown",
            "started_at": self.started_at or activity.get("started_at"),
            "iteration": None,
            "title": title or "子智能体",
            "purpose": introductions[0]["text"] if introductions else "",
            "status": status,
            "last_at": self.last_at or activity.get("at"),
            "messages": messages,
            "result": finals[-1]["text"] if finals else None,
            "tool_count": self.tool_count,
            "task_detail_available": False,
        }


class State:
    def __init__(self, root):
        self.root = Path(root)
        self.reader = Lines(self.root / "events.jsonl")
        self.sessions = {}
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.lead_id = None
        self.last_event_at = None
        self.event_count = 0
        self.messages = OrderedDict()
        self.tools = OrderedDict()
        self.agents = {}
        self.plan = []

    def tool(self, identifier, **fields):
        entry = self.tools.setdefault(identifier, {"id": identifier})
        entry.update(fields)
        # Only the operation inspector is bounded; it cannot evict human replies.
        while len(self.tools) > 250:
            self.tools.popitem(last=False)

    def record(self, event):
        self.event_count += 1
        kind, at = event.get("event"), event.get("at")
        if kind == "lead.input":
            self.lead_id = event.get("thread_id")
        if kind in {"codex.event", "resource.tool"}:
            self.last_event_at = at
        if kind == "resource.tool":
            name = event.get("tool")
            pending = next(
                (
                    tool
                    for tool in reversed(self.tools.values())
                    if tool.get("name") == name and tool.get("status") == "inProgress"
                ),
                None,
            )
            identifier = pending["id"] if pending else f"resource-{self.event_count}"
            self.tool(
                identifier,
                at=at,
                title=TOOL_TITLES.get(name, "资源操作"),
                name=name,
                status="completed" if event.get("success") else "failed",
                input=event.get("arguments"),
                output=event.get("output"),
                thread_id=event.get("thread_id"),
            )
        elif kind == "codex.event":
            data = event.get("data") or {}
            method, item = event.get("method"), data.get("item") or {}
            typ, identifier = item.get("type"), item.get("id")
            if method == "turn/plan/updated":
                self.plan = data.get("plan") or []
            if typ == "agentMessage" and method == "item/completed":
                phase = item.get("phase")
                if phase in PUBLIC_PHASES and (text := public_text(item.get("text"))):
                    identifier = identifier or f"message-{self.event_count}"
                    self.messages[identifier] = message(identifier, at, text, phase)
            elif typ == "subAgentActivity":
                thread = item.get("agentThreadId")
                if thread:
                    activity = self.agents.setdefault(thread, {})
                    state = {
                        "started": "working",
                        "completed": "completed",
                        "interacted": "working",
                        "interrupted": "interrupted",
                    }.get(item.get("kind"))
                    activity.update(path=item.get("agentPath"), at=at)
                    if item.get("kind") == "started":
                        activity.setdefault("started_at", at)
                    if state and not (
                        state == "interrupted" and activity.get("status") == "completed"
                    ):
                        activity["status"] = state
            elif (
                typ
                in {"commandExecution", "dynamicToolCall", "fileChange", "webSearch"}
                and identifier
            ):
                name = item.get("tool")
                title = (
                    TOOL_TITLES.get(name)
                    if typ == "dynamicToolCall"
                    else {
                        "commandExecution": "执行本地命令",
                        "fileChange": "修改工作文件",
                        "webSearch": "检索参考资料",
                    }.get(typ)
                )
                fields = {
                    "at": at,
                    "title": title or "执行工具",
                    "name": name,
                    "status": "failed"
                    if item.get("success") is False
                    else item.get("status", "inProgress"),
                    "thread_id": data.get("threadId"),
                    "input": item.get(
                        "command",
                        item.get("arguments", item.get("changes", item.get("query"))),
                    ),
                }
                if typ == "commandExecution" and item.get("commandActions"):
                    actions = item["commandActions"]
                    if len(actions) == 1 and actions[0].get("type") in {
                        "read",
                        "search",
                        "listFiles",
                    }:
                        fields["title"] = {
                            "read": "阅读代码或文档",
                            "search": "搜索代码",
                            "listFiles": "查看文件",
                        }[actions[0]["type"]]
                if "aggregatedOutput" in item:
                    fields["output"] = item["aggregatedOutput"]
                self.tool(identifier, **fields)

    def update(self):
        records, reset = self.reader.drain()
        if reset:
            self.reset()
        for event in records:
            self.record(event)
        # Discover only this run's native histories; read each incrementally.
        for path in (self.root / "native/sessions").glob("*/*/*/*.jsonl"):
            session = self.sessions.setdefault(path, Session(path))
            session.update()

    def tool_detail(self, identifier):
        with self.lock:
            self.update()
            record = self.tools.get(identifier)
            return dict(record) if record else None

    def snapshot(self):
        with self.lock:
            self.update()
            status = read_json(self.root / "status.json", {}) or {}
            self.lead_id = self.lead_id or status.get("thread_id")
            config = read_json(self.root / "config.json", {}) or {}
            resource = config.get("resource") or {}
            ledger_path = (
                Path(resource["ledger_path"]) if resource.get("ledger_path") else None
            )
            ledger = read_json(ledger_path) if ledger_path else None
            budget = {"state": "unavailable"}
            if isinstance(ledger, dict) and isinstance(ledger.get("entries"), list):
                try:
                    entries = ledger["entries"]
                    used = sum(
                        e["receipt"]["elapsed_seconds"]
                        for e in entries
                        if e["status"] == "TERMINAL"
                    )
                    reserved = sum(
                        e["reserved_seconds"]
                        for e in entries
                        if e["status"] != "TERMINAL"
                    )
                    total = ledger["authorized_seconds"]
                    budget = {
                        "state": "recorded",
                        "authorized_seconds": total,
                        "used_seconds": used,
                        "reserved_seconds": reserved,
                        "remaining_seconds": max(0, total - used - reserved),
                        "unsettled": sum(e["status"] != "TERMINAL" for e in entries),
                    }
                except (KeyError, TypeError, ValueError):
                    budget = {"state": "unreadable"}
            elif ledger_path and not ledger_path.exists():
                lock = ledger_path.with_name(ledger_path.name + ".lock")
                budget = {
                    "state": "missing ledger; reconciliation needed"
                    if lock.exists() and lock.stat().st_size
                    else "not yet created",
                    "configured_seconds": resource.get("gpu_seconds_total"),
                }
            supervisor = read_json(self.root / "supervisor.json", {}) or {}
            pid, alive = supervisor_process(supervisor)
            messages, children, known = list(self.messages.values()), [], set()
            descendants = {self.lead_id} if self.lead_id else set()
            for _ in self.sessions:
                added = {
                    s.id
                    for s in self.sessions.values()
                    if s.parent_id in descendants and s.id
                }
                if added <= descendants:
                    break
                descendants.update(added)
            for session in self.sessions.values():
                if session.id == self.lead_id:
                    if not messages:
                        messages = list(session.messages.values())
                elif session.id and (
                    session.id in descendants or session.id in self.agents
                ):
                    children.append(session.card(self.agents.get(session.id)))
                    known.add(session.id)
            for identifier, activity in self.agents.items():
                if identifier not in known:
                    stub = Session(self.root / "unavailable")
                    stub.id = identifier
                    children.append(stub.card(activity))
            boundaries = iteration_metadata(self.root)
            for child in children:
                started = timestamp(child["started_at"])
                if started is not None:
                    matches = [
                        iteration for start, iteration in boundaries if start <= started
                    ]
                    child["iteration"] = matches[-1] if matches else None
            tools = list(self.tools.values())
            active = [tool for tool in tools if tool.get("status") == "inProgress"]
            activity = {"label": "正在处理，等待下一条阶段性进展", "state": "working"}
            if active:
                activity["label"] = active[-1]["title"]
            elif (
                tools
                and tools[-1].get("name") == "gpu_poll"
                and (tools[-1].get("output") or {}).get("running")
            ):
                activity["label"] = "GPU 任务正在运行，等待输出"
            if status.get("status") not in {
                "starting",
                "running",
                "iteration-completed",
            }:
                activity = {
                    "label": "本轮已结束" if status.get("ended_at") else "尚未开始",
                    "state": "idle",
                }
            elif alive == "dead":
                activity = {"label": "控制进程已退出，等待检查", "state": "interrupted"}
            try:
                goal = (self.root / "goal.md").read_text(encoding="utf-8")
            except OSError:
                goal = "研究目标暂时不可读"
            return {
                "as_of": datetime.now(UTC).isoformat(),
                "status": status,
                "supervisor_pid": pid,
                "supervisor_alive": alive,
                "last_event_at": self.last_event_at,
                "goal": goal,
                "research": research_state(self.root, status),
                "budget": budget,
                "messages": messages,
                "latest_summary": messages[-1]["text"] if messages else "",
                "agents": children,
                "activity": activity,
                "tools": [
                    {
                        key: value
                        for key, value in tool.items()
                        if key not in {"input", "output"}
                    }
                    for tool in tools
                ],
                "plan": self.plan,
                "stats": {
                    "events": self.event_count,
                    "messages": len(messages),
                    "agents": len(children),
                },
                "curves": read_json(self.root / "inputs/historical-curves.json", {}),
            }


def handler_for(state, allowed_hosts):
    directory = Path(__file__).resolve().parent
    static = {
        "/": (directory / "index.html", "text/html; charset=utf-8"),
        "/app.js": (directory / "app.js", "text/javascript; charset=utf-8"),
        "/style.css": (directory / "style.css", "text/css; charset=utf-8"),
        "/markdown-it.js": (
            directory / "vendor/markdown-it/markdown-it.umd.min.js",
            "text/javascript; charset=utf-8",
        ),
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Host") not in allowed_hosts:
                self.send_error(403)
                return
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/api/state":
                body = json.dumps(state.snapshot(), ensure_ascii=False).encode()
                content_type = "application/json; charset=utf-8"
            elif path == "/api/metrics":
                body = json.dumps(
                    metrics_snapshot(state.root), ensure_ascii=False
                ).encode()
                content_type = "application/json; charset=utf-8"
            elif path == "/api/tool":
                identifier = parse_qs(parsed.query).get("id", [""])[0]
                tool = state.tool_detail(identifier)
                if tool is None:
                    self.send_error(404, "Tool record unavailable in current view")
                    return
                body = json.dumps(tool, ensure_ascii=False).encode()
                content_type = "application/json; charset=utf-8"
            elif path in static:
                source, content_type = static[path]
                try:
                    body = source.read_bytes()
                except OSError:
                    self.send_error(404)
                    return
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--allowed-host", action="append", default=[])
    args = parser.parse_args()
    allowed = {f"127.0.0.1:{args.port}", f"localhost:{args.port}", *args.allowed_host}
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port), handler_for(State(args.study.resolve()), allowed)
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
