"""Deterministic, incremental local sidecar; never invokes models or resource tools."""

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import threading
from time import perf_counter, process_time

from .identity import resolve, session_paths
from .efficiency import (
    aggregate_tool_outcomes,
    frozen_configuration,
    record_tool_outcome,
    tool_outcomes,
)
from .sample_store import _atomic, publish_pending
from .token_usage import AUTHORITY, TOKEN_KEYS, accept_record, cumulative_counts

VERSION = 2
WAITS = {"wait", "wait_agent", "sleep"}
TERMINAL = {
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "TIMEOUT",
    "OUT_OF_MEMORY",
    "NODE_FAIL",
    "PREEMPTED",
    "BOOT_FAIL",
    "DEADLINE",
    "REVOKED",
}
LIMITATIONS = [
    "Unique token_usage_record response IDs are authoritative, including compaction requests. token_count is a separate cross-check, never added. Input includes cached; output includes reasoning.",
    "Usage belongs to notification time in [since, until), without prorating requests. Startup and first-3/5 refer to the thread's lifetime response order, restricted to the phase window.",
    "Membership uses explicit Agent/Router roles.json mappings and native descendants; unmapped histories remain a coverage gap; descendants without explicit roles stay unclassified. Tool counts exclude dynamically nested calls.",
    "Background association is a heuristic: visible cat/sed/read_text references to goal.md, knowledge/index.md or AGENTS.md, excluding apply_patch, paired via tool output to the next usage record. Association is not demonstrated waste.",
    "Wait seconds are summed call-to-return intervals for window-started calls returned before cutoff, with overlapping parallel waits. Structured timed_out counts require a JSON boolean.",
    "Allocation seconds are whole settled elapsed time for window-dispatched jobs, not exact window overlap, core utilization or money; cancellation is not automatically a failure.",
    "Tool failures require explicit structured outcome fields; plain-text outcomes and tool-free time remain unmeasured, not inferred idle or waste.",
    "Sources are not an atomic snapshot; local coverage cannot prove all activity was recorded. Future-dated rows defer their file tail until a later cutoff; timestamps are expected to be chronological within a source.",
    "Checkpoint-first journal repair prevents duplicate samples after interruption. Power-loss durability depends on filesystem fsync semantics; output files must not be externally edited.",
]


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return result.astimezone(timezone.utc)


def usage():
    return dict.fromkeys((*TOKEN_KEYS, "responses"), 0)


def add(target, other):
    for key in target:
        target[key] += other.get(key, 0)


def derived(value):
    return {
        **value,
        "uncached_input_tokens": value["input_tokens"] - value["cached_input_tokens"],
    }


def task_state(tid, task, parent, role):
    return {
        "thread_id": tid,
        "task": task,
        "parent": parent,
        "role": role,
        "usage": usage(),
        "startup": usage(),
        "first_3": usage(),
        "first_5": usage(),
        "background_associated": usage(),
        "later_background_associated": usage(),
        "lifetime_responses": 0,
        "compactions": 0,
        "duplicate_responses": 0,
        "tool_calls": {},
        "tool_outcomes": tool_outcomes(),
        "waits": {
            n: {"calls": 0, "returned_calls": 0, "seconds": 0.0, "timed_out": 0}
            for n in sorted(WAITS)
        },
        "pending_calls": {},
        "seen_calls": [],
        "returned_calls": [],
        "background_pending": False,
        "cumulative_previous": dict.fromkeys(TOKEN_KEYS, 0),
        "cumulative_window": usage(),
        "models": [],
        "started_at": None,
        "completed_at": None,
        "last_source_event_at": None,
        "consumption_evidence": False,
    }


class Collector:
    """Single writer for one phase; poll reads only appended trace/event bytes.

    Bootstrap discovers session metadata once. Each later discovery reads only the
    first line of new files; known rollout bodies are never rescanned.
    """

    def __init__(
        self, root, output, *, since, phase, sessions_dir=None, ledger_path=None
    ):
        self.root = Path(root).resolve()
        self.output = Path(output).resolve()
        if self.output == self.root or self.root in self.output.parents:
            raise ValueError("metrics output must be outside the Study")
        self.since = timestamp(since)
        self.sessions_dir = Path(sessions_dir).resolve() if sessions_dir else None
        self.explicit_ledger = Path(ledger_path).resolve() if ledger_path else None
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.output, 0o700)
        self.lock = os.open(self.output / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock)
            self.lock = None
            raise RuntimeError("metrics output already has an active writer") from None
        identity = {
            "source_format": "frozen-workflow-v1",
            "root": str(self.root),
            "since": self.since.isoformat(),
            "phase": phase,
            "sessions_dir": str(self.sessions_dir) if self.sessions_dir else None,
            "ledger_path": str(self.explicit_ledger) if self.explicit_ledger else None,
        }
        try:
            checkpoint = self.output / "checkpoint.json"
            if checkpoint.exists():
                self.state = json.loads(checkpoint.read_text())
                if (
                    self.state.get("version") != VERSION
                    or self.state.get("identity") != identity
                ):
                    raise ValueError(
                        "checkpoint configuration differs; use a new output directory"
                    )
                self._publish_pending()
            else:
                if (self.output / "samples.jsonl").exists():
                    raise ValueError(
                        "samples exist without checkpoint; use a new output directory"
                    )
                self.state = {
                    "version": VERSION,
                    "identity": identity,
                    "offsets": {},
                    "metadata": {},
                    "tasks": {},
                    "seen_responses": [],
                    "resources": {
                        "calls": 0,
                        "failed_calls": 0,
                        "successful_empty_running_gpu_polls": 0,
                        "output_json_bytes": 0,
                        "by_tool": {},
                        "failed_by_tool": {},
                    },
                    "issues": {},
                    "sequence": 0,
                    "sample_offset": 0,
                    "bytes_read": 0,
                    "collector_seconds": 0.0,
                    "collector_cpu_seconds": 0.0,
                }
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _issue(self, name):
        self.tick_issues.add(name)
        self.state["issues"][name] = self.state["issues"].get(name, 0) + 1
        if name.rsplit(":", 1)[-1] in {
            "invalid_json",
            "rotated_or_truncated",
            "invalid_timestamp",
            "invalid_payload",
            "invalid_cumulative_usage",
            "cumulative_counter_decrease",
            "missing_response_id",
            "invalid_usage_record",
            "negative_wait_duration",
            "missing_resource_success",
        }:
            permanent = self.state.setdefault("permanent_issues", [])
            if name not in permanent:
                permanent.append(name)

    def _json(self, path, label, *, optional=False):
        try:
            data = path.read_bytes()
            self.tick_bytes += len(data)
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except FileNotFoundError:
            if not optional:
                self._issue(label + ":missing")
        except (OSError, ValueError, UnicodeError):
            self._issue(label + ":unreadable_or_invalid")
        return {}

    def _at(self, row, key="timestamp"):
        try:
            return timestamp(row[key])
        except (KeyError, ValueError, TypeError, AttributeError):
            self._issue("sources:invalid_timestamp")
            return None

    def _tail(self, path, label):
        key = str(path)
        try:
            stat = path.stat()
            identity = [stat.st_dev, stat.st_ino]
            saved = self.state["offsets"].setdefault(
                key, {"identity": identity, "offset": 0}
            )
            if saved["identity"] != identity or stat.st_size < saved["offset"]:
                self._issue(label + ":rotated_or_truncated")
                # Never replay overwritten bytes: events and compactions lack IDs.
                # Coverage remains incomplete after a rotation/truncation.
                saved.update(identity=identity, offset=stat.st_size)
            with path.open("rb") as stream:
                stream.seek(saved["offset"])
                remaining = stat.st_size - saved["offset"]
                while remaining > 0:
                    line = stream.readline(remaining)
                    self.tick_bytes += len(line)
                    remaining -= len(line)
                    if not line:
                        self._issue(label + ":changed_during_read")
                        break
                    if not line.endswith(b"\n"):
                        self._issue(label + ":incomplete_tail")
                        break
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError
                    except (ValueError, UnicodeError):
                        self._issue(label + ":invalid_json")
                        saved["offset"] += len(line)
                        continue
                    at = self._at(row, "at" if label == "events" else "timestamp")
                    if at is not None and at >= self.cutoff:
                        break
                    saved["offset"] += len(line)
                    if at is not None:
                        yield row, at
        except OSError:
            self._issue(label + ":unreadable_or_missing")

    def _discover(self):
        try:
            paths = session_paths(self.root, self.sessions_dir)
        except OSError:
            paths = []
            self._issue("sessions:unreadable")
        if not paths:
            self._issue("sessions:missing")
        for path in paths:
            key = str(path)
            if key in self.state["metadata"]:
                continue
            try:
                with path.open("rb") as stream:
                    line = stream.readline()
                self.tick_bytes += len(line)
                if not line.endswith(b"\n"):
                    continue
                row = json.loads(line)
                p = row["payload"]
                if row.get("type") != "session_meta" or not isinstance(
                    p.get("id"), str
                ):
                    raise ValueError
                source = p.get("source")
                parent = (
                    source.get("subagent", {})
                    .get("thread_spawn", {})
                    .get("parent_thread_id")
                    if isinstance(source, dict)
                    else None
                )
                self.state["metadata"][key] = {
                    "thread_id": p["id"],
                    "parent": parent,
                    "task": str(p.get("agent_path", "root")).rsplit("/", 1)[-1],
                    "cli_version": p.get("cli_version"),
                }
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                self._issue("sessions:invalid_or_unreadable_metadata")
        roles = {}
        roles_path = self.root / "roles.json"
        if roles_path.exists():
            try:
                document = self._json(roles_path, "roles")
                threads = (
                    document.get("threads", {}) if isinstance(document, dict) else None
                )
                if not isinstance(threads, dict):
                    raise ValueError
                roles = {
                    tid: row
                    for tid, row in threads.items()
                    if isinstance(tid, str) and isinstance(row, dict)
                }
            except (OSError, ValueError, AttributeError):
                self._issue("roles:invalid")
        known = {m["thread_id"] for m in self.state["metadata"].values()}
        # Preserve the collector's typed-field validation, unlike the offline
        # reader's truthy-field fallback. Neither changes checkpoint contents.
        normalized_roles = {
            tid: {
                key: row.get(key)
                if isinstance(row.get(key), str) and row[key]
                else None
                for key in ("task", "role", "group_run_id")
            }
            for tid, row in roles.items()
        }
        identities = resolve(self.state["metadata"].values(), normalized_roles)
        selected = identities.keys()
        if known - selected:
            self._issue("sessions:unmapped_threads")
        if roles.keys() - known:
            self._issue("sessions:mapped_history_missing")
        seen = set()
        for path, meta in self.state["metadata"].items():
            tid = meta["thread_id"]
            if tid not in selected:
                continue
            if tid in seen:
                self._issue("sessions:duplicate_thread")
                continue
            seen.add(tid)
            identity = identities[tid]
            explicit_role = identity.role_source == "thread-record"
            task = self.state["tasks"].setdefault(
                tid,
                task_state(tid, identity.task, meta["parent"], identity.role),
            )
            task["is_router"] = identity.is_router
            if explicit_role and task.get("role") != identity.role:
                task["role"] = identity.role
            for row, at in self._tail(Path(path), "sessions"):
                self._source_at(at)
                self._session(task, row, at)
        return len(paths), len(seen), len(known - selected)

    def _inside(self, at):
        return self.since <= at < self.cutoff

    def _source_at(self, at):
        previous = self.state.get("last_source_event_at")
        if previous is None or at > timestamp(previous):
            self.state["last_source_event_at"] = at.isoformat()

    def _session(self, task, row, at):
        previous = task.get("last_source_event_at")
        if previous is None or at > timestamp(previous):
            task["last_source_event_at"] = at.isoformat()
        p = row.get("payload", {})
        if not isinstance(p, dict):
            self._issue("sessions:invalid_payload")
            return
        kind, event = row.get("type"), p.get("type")
        if kind == "response_item" and (
            event in {"function_call", "custom_tool_call", "reasoning"}
            or (event == "message" and p.get("role") == "assistant")
        ):
            task["consumption_evidence"] = True
        inside = self._inside(at)
        if kind == "session_meta":
            task["started_at"] = at.isoformat()
        if kind == "event_msg" and event == "task_complete":
            task["completed_at"] = at.isoformat()
        if kind == "turn_context":
            setting = {
                "model": p.get("model"),
                "effort": p.get("effort", p.get("reasoning_effort")),
            }
            if setting not in task["models"]:
                task["models"].append(setting)
        if kind == "compacted" and inside:
            task["compactions"] += 1
        if kind == "event_msg" and event == "token_count" and p.get("info"):
            current = cumulative_counts(p["info"])
            if current is not None:
                if any(current.values()):
                    task["consumption_evidence"] = True
                delta = {
                    k: current[k] - task["cumulative_previous"][k] for k in TOKEN_KEYS
                }
                task["cumulative_previous"] = current
                if any(v < 0 for v in delta.values()):
                    self._issue("sessions:cumulative_counter_decrease")
                elif inside:
                    add(task["cumulative_window"], delta)
            else:
                self._issue("sessions:invalid_cumulative_usage")
        if kind == "token_usage_record" or (
            kind == "event_msg" and event == "token_usage_record"
        ):
            value, issue = accept_record(p, self.responses)
            if issue == "duplicate":
                if inside:
                    task["duplicate_responses"] += 1
                return
            if issue:
                self._issue("sessions:" + issue)
                return
            task["lifetime_responses"] += 1
            n = task["lifetime_responses"]
            if inside:
                add(task["usage"], value)
                if n == 1:
                    add(task["startup"], value)
                if n <= 3:
                    add(task["first_3"], value)
                if n <= 5:
                    add(task["first_5"], value)
                if task["background_pending"]:
                    add(task["background_associated"], value)
                    if n > 1:
                        add(task["later_background_associated"], value)
            task["background_pending"] = False
        if kind != "response_item":
            return
        cid = p.get("call_id", p.get("id"))
        if not isinstance(cid, str):
            return
        if event in {"function_call", "custom_tool_call"}:
            if cid in self.calls[task["thread_id"]]:
                return
            self.calls[task["thread_id"]].add(cid)
            name = str(p.get("name", "unknown")).rsplit("__", 1)[-1]
            text = p.get("input", p.get("arguments", ""))
            background = (
                isinstance(text, str)
                and bool(re.search(r"\bcat\b|\bsed\b|read_text\s*\(", text))
                and bool(re.search(r"goal\.md|knowledge/index\.md|AGENTS\.md", text))
                and "apply_patch" not in text
            )
            task["pending_calls"][cid] = {
                "name": name,
                "at": at.isoformat(),
                "inside": inside,
                "background": background,
            }
            if inside:
                task["tool_calls"][name] = task["tool_calls"].get(name, 0) + 1
                if name in WAITS:
                    task["waits"][name]["calls"] += 1
        elif event in {"function_call_output", "custom_tool_call_output"}:
            pending = task["pending_calls"].pop(cid, None)
            if pending is None:
                return
            if pending["background"]:
                task["background_pending"] = True
            name = pending["name"]
            if pending["inside"]:
                record_tool_outcome(task["tool_outcomes"], name, p)
            if name in WAITS and pending["inside"]:
                seconds = (at - timestamp(pending["at"])).total_seconds()
                if seconds < 0:
                    self._issue("sessions:negative_wait_duration")
                    return
                wait = task["waits"][name]
                wait["returned_calls"] += 1
                wait["seconds"] += seconds
                output = p.get("output")
                if isinstance(output, str):
                    try:
                        output = json.loads(output)
                    except ValueError:
                        output = None
                if isinstance(output, dict) and output.get("timed_out") is True:
                    wait["timed_out"] += 1

    def _events(self):
        for row, at in self._tail(self.root / "events.jsonl", "events"):
            self._source_at(at)
            if (
                not self._inside(at)
                or row.get("event") != "resource.tool"
                or row.get("resource_effect") is False
            ):
                continue
            r = self.state["resources"]
            name = str(row.get("tool", "unknown"))
            r["calls"] += 1
            r["by_tool"][name] = r["by_tool"].get(name, 0) + 1
            if row.get("success") is False:
                r["failed_calls"] += 1
                r["failed_by_tool"][name] = r["failed_by_tool"].get(name, 0) + 1
            elif row.get("success") is not True:
                r["unknown_success_calls"] = r.get("unknown_success_calls", 0) + 1
                self._issue("events:missing_resource_success")
            output = row.get("output")
            r["output_json_bytes"] += len(
                json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode()
            )
            if (
                name == "gpu_poll"
                and row.get("success") is True
                and isinstance(output, dict)
                and output.get("running") is True
                and isinstance(output.get("output", ""), str)
                and not output.get("output", "").strip()
            ):
                r["successful_empty_running_gpu_polls"] += 1

    def _allocation(self, config, status):
        resource = config.get("resource") or {}
        configured = resource.get("ledger_path") if isinstance(resource, dict) else None
        if configured is not None and (
            not isinstance(configured, str) or not configured
        ):
            raise ValueError("configured ledger_path must be a nonempty path string")
        path = self.explicit_ledger or (
            Path(configured) if isinstance(configured, str) and configured else None
        )
        expected = bool(
            self.explicit_ledger
            or resource
            or status.get("resource")
            or self.state["resources"]["calls"]
        )
        ledger = (
            self._json(path, "ledger", optional=not expected)
            if path is not None
            else {}
        )
        if expected and path is None:
            self._issue("ledger:undeclared")
        result = {
            "available": bool(ledger),
            "source": "explicit"
            if self.explicit_ledger
            else "frozen_config"
            if configured
            else "undeclared",
            "reservations": 0,
            "dispatched_jobs": 0,
            "not_dispatched": 0,
            "settled_jobs": 0,
            "elapsed_seconds": 0,
            "unknown_jobs": 0,
            "nonterminal_jobs": 0,
            "states": {},
        }
        entries = ledger.get("entries", [] if not ledger else None)
        if not isinstance(entries, list):
            self._issue("ledger:invalid_entries")
            return result
        for entry in entries:
            if not isinstance(entry, dict):
                self._issue("ledger:invalid_entry")
                continue
            at = (
                self._at(entry, "dispatched_at") if entry.get("dispatched_at") else None
            )
            if at is None or not self._inside(at):
                continue
            result["reservations"] += 1
            receipt = entry.get("receipt") or {}
            if not isinstance(receipt, dict):
                self._issue("ledger:invalid_receipt")
                continue
            state, elapsed = (
                receipt.get("state", "UNKNOWN"),
                receipt.get("elapsed_seconds"),
            )
            if entry.get("job_id") or receipt.get("job_id"):
                result["dispatched_jobs"] += 1
            if (
                entry.get("status") == "TERMINAL"
                and state == "NOT_DISPATCHED"
                and elapsed == 0
            ):
                result["not_dispatched"] += 1
            elif (
                entry.get("status") == "TERMINAL"
                and state in TERMINAL
                and type(elapsed) is int
                and elapsed >= 0
            ):
                result["settled_jobs"] += 1
                result["elapsed_seconds"] += elapsed
            elif entry.get("status") == "UNKNOWN" or state == "UNKNOWN":
                result["unknown_jobs"] += 1
            else:
                result["nonterminal_jobs"] += 1
            result["states"][state] = result["states"].get(state, 0) + 1
        return result

    def _publish_pending(self):
        publish_pending(self.output, self.state, self._sample_history)

    def _sample_history(self, pending):
        history = {
            key: pending[key]
            for key in (
                "schema_version",
                "sample_id",
                "phase",
                "window",
                "configuration",
                "source_versions",
                "coverage",
                "resources",
                "tools",
                "allocation",
                "reader",
            )
        }
        history["last_source_event_at"] = pending.get("last_source_event_at")
        history["llm"] = {
            key: pending["llm"][key]
            for key in ("authority", "measurable", "total", "by_role")
        }
        history["history"] = {
            "task_details": "Full by_task details are in latest.json, omitted from per-tick history."
        }
        return history

    def poll(self, cutoff=None):
        if self.lock is None:
            raise RuntimeError("collector is closed")
        started, cpu = perf_counter(), process_time()
        self.cutoff = (
            timestamp(cutoff)
            if isinstance(cutoff, str)
            else cutoff or datetime.now(timezone.utc)
        )
        self.cutoff = timestamp(self.cutoff.isoformat())
        if self.cutoff <= self.since:
            raise ValueError("cutoff must be later than since")
        if self.state.get("cutoff") and self.cutoff < timestamp(self.state["cutoff"]):
            raise ValueError("cutoff cannot move backwards")
        self.tick_bytes, self.tick_issues = 0, set()
        self.responses = set(self.state["seen_responses"])
        self.calls = {
            tid: set(t["seen_calls"]) for tid, t in self.state["tasks"].items()
        }
        status = self._json(self.root / "status.json", "status")
        # New task call sets are lazily created by _session.
        self.calls = _CallSets(self.calls)
        discovered, selected, excluded = self._discover()
        self._events()
        config = frozen_configuration(self.root, self._json)
        allocation = self._allocation(config, status)
        observed_records = len(self.responses)
        missing_record_threads = [
            tid
            for tid, t in self.state["tasks"].items()
            if not t["lifetime_responses"]
            and (
                t.get("consumption_evidence")
                or any(t["cumulative_previous"].values())
                or self.calls.get(tid)
            )
        ]
        awaiting_first_usage_threads = [
            tid
            for tid, t in self.state["tasks"].items()
            if not t["lifetime_responses"] and tid not in missing_record_threads
        ]
        if missing_record_threads:
            self._issue("sessions:usage_records_missing")
        self.state["seen_responses"] = sorted(self.responses)
        for tid, calls in self.calls.items():
            self.state["tasks"][tid]["seen_calls"] = sorted(calls)
        self.state["sequence"] += 1
        self.state["cutoff"] = self.cutoff.isoformat()
        total, roles, tasks = usage(), {}, []
        for t in self.state["tasks"].values():
            public = {
                k: v
                for k, v in t.items()
                if k
                not in {
                    "pending_calls",
                    "seen_calls",
                    "returned_calls",
                    "background_pending",
                    "cumulative_previous",
                }
            }
            public["started_before_phase"] = (
                timestamp(t["started_at"]) < self.since if t["started_at"] else None
            )
            public["usage_records_observed"] = t["lifetime_responses"]
            public["authority_missing"] = t["thread_id"] in missing_record_threads
            public["awaiting_first_usage"] = (
                t["thread_id"] in awaiting_first_usage_threads
            )
            public["active_in_phase"] = bool(
                t.get("last_source_event_at")
                and self._inside(timestamp(t["last_source_event_at"]))
            )
            public["mixed"] = (
                public["active_in_phase"]
                and public["started_before_phase"] is not False
            )
            for key in (
                "usage",
                "startup",
                "first_3",
                "first_5",
                "background_associated",
                "later_background_associated",
                "cumulative_window",
            ):
                public[key] = derived(t[key])
            tasks.append(public)
            add(total, t["usage"])
            role = roles.setdefault(
                t["role"],
                {
                    "usage": usage(),
                    "startup": usage(),
                    "first_3": usage(),
                    "first_5": usage(),
                    "background_associated": usage(),
                    "later_background_associated": usage(),
                    "tool_calls": 0,
                    "wait_calls": 0,
                    "wait_timeouts": 0,
                    "wait_seconds": 0.0,
                },
            )
            for key in (
                "usage",
                "startup",
                "first_3",
                "first_5",
                "background_associated",
                "later_background_associated",
            ):
                add(role[key], t[key])
            role["tool_calls"] += sum(t["tool_calls"].values())
            for w in t["waits"].values():
                role["wait_calls"] += w["calls"]
                role["wait_timeouts"] += w["timed_out"]
                role["wait_seconds"] += w["seconds"]
        for role in roles.values():
            for key in (
                "usage",
                "startup",
                "first_3",
                "first_5",
                "background_associated",
                "later_background_associated",
            ):
                role[key] = derived(role[key])
        elapsed, cpu_elapsed = perf_counter() - started, process_time() - cpu
        self.state["bytes_read"] += self.tick_bytes
        self.state["collector_seconds"] += elapsed
        self.state["collector_cpu_seconds"] += cpu_elapsed
        result = {
            "schema_version": VERSION,
            "sample_id": f"{self.state['identity']['phase']}:{self.state['sequence']}",
            "phase": self.state["identity"]["phase"],
            "last_source_event_at": self.state.get("last_source_event_at"),
            "window": {
                "since": self.since.isoformat(),
                "until": self.cutoff.isoformat(),
                "interval": "since <= notification time < until",
            },
            "configuration": {
                "revision": config.get("revision"),
                "router": config.get("router"),
                "profiles": config.get("profiles", {}),
            },
            "source_versions": sorted(
                {
                    m["cli_version"]
                    for m in self.state["metadata"].values()
                    if isinstance(m.get("cli_version"), str)
                }
            ),
            "coverage": {
                "complete": not self.tick_issues
                and not self.state.get("permanent_issues"),
                "usage_records_observed": observed_records,
                "usage_records_missing_threads": missing_record_threads,
                "awaiting_first_usage_threads": awaiting_first_usage_threads,
                "issues": dict(self.state["issues"]),
                "permanent_issues": self.state.get("permanent_issues", []),
                "tick_issues": sorted(self.tick_issues),
                "session_files_discovered": discovered,
                "selected_threads": selected,
                "excluded_session_threads": excluded,
            },
            "llm": {
                "authority": AUTHORITY,
                "measurable": bool(observed_records) and not missing_record_threads,
                "total": derived(total),
                "by_role": roles,
                "by_task": tasks,
            },
            "tools": aggregate_tool_outcomes(tasks),
            "resources": {
                **self.state["resources"],
                "successful_calls": self.state["resources"]["calls"]
                - self.state["resources"]["failed_calls"]
                - self.state["resources"].get("unknown_success_calls", 0),
                "unknown_success_calls": self.state["resources"].get(
                    "unknown_success_calls", 0
                ),
            },
            "allocation": allocation,
            "reader": {
                "bytes_read": self.tick_bytes,
                "cumulative_bytes_read": self.state["bytes_read"],
                "elapsed_seconds": elapsed,
                "cpu_seconds": cpu_elapsed,
                "cumulative_elapsed_seconds": self.state["collector_seconds"],
                "cumulative_cpu_seconds": self.state["collector_cpu_seconds"],
                "timing_scope": "source reading and aggregation; excludes checkpoint/journal publication",
                "previous_poll_total_seconds": self.state.get(
                    "previous_poll_total_seconds"
                ),
                "previous_poll_total_cpu_seconds": self.state.get(
                    "previous_poll_total_cpu_seconds"
                ),
            },
            "limitations": LIMITATIONS,
        }
        # JSON round-trip detaches mutable state from the returned/persisted sample.
        result = json.loads(json.dumps(result))
        self.state["pending_sample"] = result
        _atomic(self.output / "checkpoint.json", self.state)
        self._publish_pending()
        # The next sample reports this complete poll, including durable publication.
        # Its last value can be absent after a restart, without affecting counters.
        self.state["previous_poll_total_seconds"] = perf_counter() - started
        self.state["previous_poll_total_cpu_seconds"] = process_time() - cpu
        return result


class _CallSets(dict):
    def __missing__(self, key):
        self[key] = set()
        return self[key]


def compare(baseline, optimized):
    """Descriptive phase comparison, without a causal-improvement claim."""

    def one(directory):
        value = json.loads((Path(directory) / "latest.json").read_text())
        total, tasks = value["llm"]["total"], value["llm"]["by_task"]
        children = [
            t for t in tasks if not t["is_router"] and t.get("active_in_phase", True)
        ]
        startups = [t for t in children if t["startup"]["responses"]]

        def ratio(a, b):
            return a / b if b else None

        completed = [
            t for t in children if t.get("completed_at") and not t.get("mixed", True)
        ]
        cohorts = {}
        for role in sorted({t["role"] for t in children}):
            cohort = [t for t in completed if t["role"] == role]
            totals = usage()
            for t in cohort:
                add(totals, t["usage"])
            cohorts[role] = {
                "n": len(cohort),
                "whole_task_usage": derived(totals) if cohort else None,
                "mean_whole_task_usage": {
                    k: ratio(v, len(cohort)) for k, v in derived(totals).items()
                },
            }
        return {
            "phase": value["phase"],
            "window": value["window"],
            "configuration": value["configuration"],
            "coverage": value["coverage"],
            "task_cohorts": {
                "completed_unmixed": len(completed),
                "pending": sum(not t.get("completed_at") for t in children),
                "mixed": sum(t.get("mixed", True) for t in children),
                "children_with_startup": len(startups),
                "completed_by_role": cohorts,
                "router": [t for t in tasks if t["is_router"]],
            },
            "totals": {
                "llm": total,
                "resources": value["resources"],
                "tools": value["tools"],
                "allocation": value["allocation"],
            },
            "normalized": {
                "uncached_input_per_response": ratio(
                    total["uncached_input_tokens"], total["responses"]
                ),
                "startup_uncached_input_per_child": ratio(
                    sum(t["startup"]["uncached_input_tokens"] for t in startups),
                    len(startups),
                ),
                "background_associated_uncached_input_per_response": ratio(
                    sum(
                        t["background_associated"]["uncached_input_tokens"]
                        for t in tasks
                    ),
                    total["responses"],
                ),
                "wait_timeout_fraction": ratio(
                    sum(w["timed_out"] for t in tasks for w in t["waits"].values()),
                    sum(
                        w["returned_calls"] for t in tasks for w in t["waits"].values()
                    ),
                ),
                "resource_failure_per_dispatch": ratio(
                    value["resources"]["failed_calls"],
                    value["allocation"]["dispatched_jobs"],
                ),
            },
        }

    return {
        "schema_version": VERSION,
        "baseline": one(baseline),
        "optimized": one(optimized),
        "interpretation": "Descriptive phase totals and ratios only. Different workloads, model settings, coverage and elapsed experiment time prevent a causal claim about strategy improvement.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    watch = commands.add_parser("watch")
    watch.add_argument("root", type=Path)
    watch.add_argument("--output", type=Path, required=True)
    watch.add_argument("--since", required=True)
    watch.add_argument("--phase", required=True)
    watch.add_argument("--interval", type=float, default=5)
    watch.add_argument("--sessions-dir", type=Path)
    watch.add_argument("--ledger-path", type=Path)
    watch.add_argument(
        "--baseline",
        type=Path,
        help="Refresh comparison.json beside the phase directory after each sample",
    )
    watch.add_argument("--once", action="store_true")
    watch.add_argument("--until", help="fixed exclusive cutoff, for a single snapshot")
    comparison = commands.add_parser("compare")
    comparison.add_argument("baseline", type=Path)
    comparison.add_argument("optimized", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "compare":
            print(json.dumps(compare(args.baseline, args.optimized), indent=2))
            return
        if args.interval <= 0:
            raise ValueError("interval must be positive")
        if args.until and not args.once:
            raise ValueError("--until requires --once")
        stopped = threading.Event()
        previous = {}
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, lambda *_: stopped.set())
        try:
            with Collector(
                args.root,
                args.output,
                since=args.since,
                phase=args.phase,
                sessions_dir=args.sessions_dir,
                ledger_path=args.ledger_path,
            ) as collector:
                while not stopped.is_set():
                    result = collector.poll(cutoff=args.until)
                    if args.baseline is not None:
                        _atomic(
                            args.output.parent / "comparison.json",
                            compare(args.baseline, args.output),
                        )
                    if args.once:
                        print(json.dumps(result, indent=2))
                        break
                    stopped.wait(args.interval)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    except (ValueError, RuntimeError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
