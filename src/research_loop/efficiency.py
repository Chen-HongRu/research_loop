"""Read-only, aggregate diagnostics of recorded controller and native activity."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
import re
from pathlib import Path
from time import perf_counter

from .identity import resolve, session_paths
from .token_usage import AUTHORITY, TOKEN_KEYS, accept_record, cumulative_counts

WAIT_TOOLS = {"wait", "wait_agent", "sleep"}


def _waits():
    return {
        "calls": 0,
        "returned_calls": 0,
        "observed_wait_seconds": 0.0,
        "wait_agent_timeouts": 0,
    }


TERMINAL_STATES = {
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


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return result


def frozen_configuration(root, read):
    """Read only the active pointer and normalized small configuration metadata."""
    active = read(Path(root) / "active.json", "active")
    revision = active.get("revision")
    if revision is None:
        return {}
    if not isinstance(revision, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", revision
    ):
        raise ValueError("Invalid active configuration revision")
    config = read(Path(root) / "configurations" / revision / "config.json", "config")
    if config and config.get("revision") != revision:
        raise ValueError("Active configuration revision does not match its metadata")
    return config


def tool_outcome(payload):
    """Use explicit structured outcomes; ordinary text is unmeasured."""
    output = payload.get("output")
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except ValueError:
            output = None
    values = [payload, output] if isinstance(output, dict) else [payload]
    known = False
    for value in values:
        for key in ("isError", "is_error"):
            if type(value.get(key)) is bool:
                known = True
                if value[key]:
                    return "failed"
        if type(value.get("success")) is bool:
            known = True
            if not value["success"]:
                return "failed"
        if type(value.get("exit_code")) is int:
            known = True
            if value["exit_code"] != 0:
                return "failed"
    return "succeeded" if known else "unknown"


def nested_command_receipts(name, payload):
    """Read the observed code-mode exec envelope, never command stdout."""
    output = payload.get("output")
    if name != "exec" or not isinstance(output, list):
        return
    for block in output:
        if not isinstance(block, dict) or block.get("type") != "input_text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        try:
            value = json.loads(text)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("status") == "fulfilled":
            value = value.get("value")
        if not isinstance(value, dict):
            continue
        if (
            isinstance(value.get("chunk_id"), str)
            and bool(value["chunk_id"])
            and type(value.get("wall_time_seconds")) in (int, float)
            and math.isfinite(value["wall_time_seconds"])
            and value["wall_time_seconds"] >= 0
            and type(value.get("exit_code")) is int
            and type(value.get("original_token_count")) is int
            and value["original_token_count"] >= 0
            and isinstance(value.get("output"), str)
        ):
            yield value


def _nested_outcomes():
    return {"observed_receipts": 0, "zero_exit_receipts": 0, "nonzero_exit_receipts": 0}


def tool_outcomes():
    return {
        "returned_calls": 0,
        "failed_calls": 0,
        "unknown_outcome_calls": 0,
        "failed_by_tool": {},
    }


def aggregate_tool_outcomes(tasks):
    result = tool_outcomes()
    failed = Counter()
    for task in tasks:
        value = task["tool_outcomes"]
        for key in ("returned_calls", "failed_calls", "unknown_outcome_calls"):
            result[key] += value[key]
        failed.update(value["failed_by_tool"])
        if "nested_command_receipts" in value:
            nested = result.setdefault("nested_command_receipts", _nested_outcomes())
            for key, count in value["nested_command_receipts"].items():
                nested[key] += count
    result["failed_by_tool"] = dict(failed)
    return result


def record_tool_outcome(target, name, payload):
    target["returned_calls"] += 1
    for receipt in nested_command_receipts(name, payload):
        nested = target.setdefault("nested_command_receipts", _nested_outcomes())
        nested["observed_receipts"] += 1
        key = (
            "zero_exit_receipts"
            if receipt["exit_code"] == 0
            else "nonzero_exit_receipts"
        )
        nested[key] += 1
    outcome = tool_outcome(payload)
    if outcome == "failed":
        target["failed_calls"] += 1
        target["failed_by_tool"][name] = target["failed_by_tool"].get(name, 0) + 1
    elif outcome == "unknown":
        target["unknown_outcome_calls"] += 1


def _tokens(value=None):
    value = value or {}
    return {key: value.get(key, 0) for key in TOKEN_KEYS}


def _add(target, value):
    for key in TOKEN_KEYS:
        target[key] += value[key]


def _usage():
    return {**_tokens(), "responses": 0, "duplicate_responses": 0, "compactions": 0}


def _derived(value):
    return {
        **value,
        "uncached_input_tokens": value["input_tokens"] - value["cached_input_tokens"],
    }


class _Reader:
    def __init__(self):
        self.bytes = 0
        self.files = 0
        self.issues = Counter()

    def problem(self, source, kind):
        self.issues[f"{source}:{kind}"] += 1

    def json(self, path, source):
        try:
            raw = path.read_bytes()
            self.bytes += len(raw)
            self.files += 1
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except FileNotFoundError:
            self.problem(source, "missing")
        except (ValueError, UnicodeError):
            self.problem(source, "invalid_json")
        return {}

    def rows(self, path, source):
        try:
            stream = path.open("rb")
        except FileNotFoundError:
            self.problem(source, "missing")
            return
        self.files += 1
        with stream:
            # Bound this read to the snapshot size; a live writer may keep appending.
            remaining = path.stat().st_size
            while remaining:
                line = stream.readline(remaining)
                remaining -= len(line)
                self.bytes += len(line)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    self.problem(source, "incomplete_tail")
                    break
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError
                    yield row
                except (ValueError, UnicodeError):
                    self.problem(
                        source,
                        "incomplete_tail"
                        if not remaining and not line.endswith(b"\n")
                        else "invalid_json",
                    )

    def at(self, row, source, key="timestamp"):
        try:
            return _time(row[key])
        except (KeyError, TypeError, ValueError, AttributeError):
            self.problem(source, "invalid_timestamp")
            return None


def report(
    root: Path,
    *,
    since: str | None = None,
    until: str | None = None,
    sessions_dir: Path | None = None,
    ledger_path: Path | None = None,
) -> dict:
    """Aggregate [since, until) records without writing to a Study or invoking tools."""
    started = perf_counter()
    end = _time(until) if until else datetime.now(timezone.utc)
    start = _time(since) if since else None
    if start is not None and start >= end:
        raise ValueError("since must be earlier than until")
    root = Path(root)
    reader = _Reader()
    status = reader.json(root / "status.json", "status")
    if until is None and status.get("ended_at"):
        end = reader.at(status, "status", "ended_at") or end
    identity = reader.json(root / "run.json", "run")
    config = frozen_configuration(root, reader.json)
    roles_path = root / "roles.json"
    roles = reader.json(roles_path, "roles") if roles_path.exists() else {}
    role_threads = roles.get("threads") if isinstance(roles, dict) else None
    if not isinstance(role_threads, dict):
        role_threads = {}
    events = []
    streamed_threads = set()
    for row in reader.rows(root / "events.jsonl", "events"):
        at = reader.at(row, "events", "at")
        if at is None:
            continue
        if row.get("event") == "resource.tool":
            events.append((at, row))
        data = row.get("data")
        if (
            at < end
            and row.get("event") == "codex.event"
            and str(row.get("method", "")).endswith("/delta")
            and isinstance(data, dict)
            and isinstance(data.get("delta"), str)
            and data["delta"]
        ):
            if isinstance(data.get("threadId"), str):
                streamed_threads.add(data["threadId"])
    if start is None:
        declared = status.get("started_at") or identity.get("created_at")
        if declared:
            start = reader.at({"at": declared}, "run", "at")
        else:
            reader.problem("run", "missing_start_time")
    if start is not None and start >= end:
        raise ValueError("since must be earlier than until")

    def inside(at):
        return at is not None and start is not None and start <= at < end

    paths = session_paths(root, sessions_dir)
    if not paths:
        reader.problem("sessions", "missing")
    sessions = {}
    for path in paths:
        rows = reader.rows(path, "session_metadata")
        meta = next(rows, {})
        rows.close()
        payload = meta.get("payload", {})
        if (
            meta.get("type") != "session_meta"
            or not isinstance(payload, dict)
            or not payload.get("id")
        ):
            reader.problem("sessions", "missing_metadata")
            continue
        tid = payload["id"]
        if tid in sessions:
            reader.problem("sessions", "duplicate_thread")
            continue
        sessions[tid] = (path, payload)
    identities = resolve(
        [
            {
                "thread_id": tid,
                "parent": meta["source"]
                .get("subagent", {})
                .get("thread_spawn", {})
                .get("parent_thread_id")
                if isinstance(meta.get("source"), dict)
                else None,
                "task": meta.get("agent_path", "/root"),
            }
            for tid, (_, meta) in sessions.items()
        ],
        role_threads,
    )
    selected = identities.keys()
    if sessions.keys() - selected:
        reader.problem("sessions", "unmapped_threads")
    if role_threads.keys() - sessions.keys():
        reader.problem("sessions", "mapped_history_missing")
    totals = _usage()
    by_role = {}
    tasks = []
    tool_names = Counter()
    complete_threads = set()
    cumulative_total = _tokens()
    seen_records = set()
    missing_record_threads = []
    awaiting_first_usage_threads = []
    # Match the collector's initial file-discovery order for Study-wide receipt
    # deduplication, including duplicate IDs observed before the window.
    for tid in identities:
        path, meta = sessions[tid]
        task, role = identities[tid].task, identities[tid].role
        usage = _usage()
        previous = _tokens()
        seen_calls = set()
        cumulative = _tokens()
        lifetime_responses = 0
        consumption_evidence = tid in streamed_threads
        token_cutoff_reached = False
        started_at = first_tool_at = completed_at = None
        pending_tools = {}
        outcomes = tool_outcomes()
        pending_waits = {}
        returned_waits = set()
        waits = {name: _waits() for name in sorted(WAIT_TOOLS)}
        for row in reader.rows(path, "sessions"):
            at = reader.at(row, "sessions")
            if at is None:
                continue
            if at >= end:
                token_cutoff_reached = True
                continue
            payload = row.get("payload", {})
            if not isinstance(payload, dict):
                reader.problem("sessions", "invalid_payload")
                continue
            kind = row.get("type")
            event = payload.get("type")
            # Match incremental token tail cutoff without changing the offline
            # report's existing handling of non-token activity records.
            if token_cutoff_reached and (
                kind == "token_usage_record"
                or kind == "event_msg"
                and event in {"token_usage_record", "token_count"}
            ):
                continue
            if (
                not token_cutoff_reached
                and kind == "response_item"
                and (
                    event in {"function_call", "custom_tool_call", "reasoning"}
                    or (event == "message" and payload.get("role") == "assistant")
                )
            ):
                consumption_evidence = True
            if kind == "session_meta":
                started_at = at.isoformat()
            if kind == "event_msg" and event == "task_complete":
                completed_at = at.isoformat()
            if kind == "response_item" and event in {
                "function_call",
                "custom_tool_call",
            }:
                if first_tool_at is None:
                    first_tool_at = at.isoformat()
                call_id = payload.get("call_id", payload.get("id"))
                if (
                    inside(at)
                    and isinstance(call_id, str)
                    and call_id not in seen_calls
                ):
                    pending_tools.setdefault(call_id, payload.get("name", "unknown"))
                name = payload.get("name")
                cid = payload.get("call_id", payload.get("id"))
                if (
                    inside(at)
                    and name in WAIT_TOOLS
                    and cid is not None
                    and cid not in pending_waits
                ):
                    pending_waits[cid] = (name, at)
                    waits[name]["calls"] += 1
            if kind == "response_item" and event in {
                "function_call_output",
                "custom_tool_call_output",
            }:
                cid = payload.get("call_id")
                if cid in pending_tools:
                    record_tool_outcome(outcomes, pending_tools.pop(cid), payload)
                if cid in pending_waits and cid not in returned_waits:
                    name, called_at = pending_waits[cid]
                    duration = (at - called_at).total_seconds()
                    if duration >= 0:
                        returned_waits.add(cid)
                        waits[name]["returned_calls"] += 1
                        waits[name]["observed_wait_seconds"] += duration
                        output = payload.get("output")
                        if isinstance(output, str):
                            try:
                                output = json.loads(output)
                            except ValueError:
                                output = None
                        if (
                            name == "wait_agent"
                            and isinstance(output, dict)
                            and output.get("timed_out") is True
                        ):
                            waits[name]["wait_agent_timeouts"] += 1
            if kind == "event_msg" and event == "token_count" and payload.get("info"):
                current = cumulative_counts(payload["info"])
                if current is None:
                    reader.problem("sessions", "invalid_cumulative_usage")
                    continue
                if any(current.values()):
                    consumption_evidence = True
                delta = {k: current[k] - previous[k] for k in TOKEN_KEYS}
                previous = current
                if any(v < 0 for v in delta.values()):
                    reader.problem("sessions", "cumulative_counter_decrease")
                    continue  # Rebaseline; the reset interval cannot be recovered exactly.
                if inside(at):
                    _add(cumulative, delta)
            if kind == "token_usage_record" or (
                kind == "event_msg" and event == "token_usage_record"
            ):
                value, issue = accept_record(payload, seen_records)
                if issue == "duplicate":
                    if inside(at):
                        usage["duplicate_responses"] += 1
                elif issue:
                    reader.problem("sessions", issue)
                else:
                    lifetime_responses += 1
                    if inside(at):
                        _add(usage, value)
                        usage["responses"] += 1
            if not inside(at):
                continue
            if kind == "compacted":
                usage["compactions"] += 1
            if kind == "event_msg" and event == "task_complete":
                complete_threads.add(tid)
            if kind == "response_item" and event in {
                "function_call",
                "custom_tool_call",
            }:
                cid = payload.get("call_id", payload.get("id"))
                if cid is None or cid not in seen_calls:
                    seen_calls.add(cid)
                    tool_names[payload.get("name", "unknown")] += 1
        _add(cumulative_total, cumulative)
        authority_missing = not lifetime_responses and consumption_evidence
        awaiting_first_usage = not lifetime_responses and not consumption_evidence
        if authority_missing:
            missing_record_threads.append(tid)
        elif awaiting_first_usage:
            awaiting_first_usage_threads.append(tid)
        for key in usage:
            totals[key] += usage[key]
        aggregate = by_role.setdefault(role, _usage())
        for key in usage:
            aggregate[key] += usage[key]
        tasks.append(
            {
                "thread_id": tid,
                "task": task,
                "role": role,
                "started_at": started_at,
                "first_tool_at": first_tool_at,
                "completed_at": completed_at,
                "waits_by_tool": waits,
                "tool_outcomes": outcomes,
                "usage": _derived(usage),
                "cumulative_window": _derived(cumulative),
                "usage_records_observed": lifetime_responses,
                "authority_missing": authority_missing,
                "awaiting_first_usage": awaiting_first_usage,
            }
        )

    if missing_record_threads:
        reader.problem("sessions", "usage_records_missing")

    calls = Counter()
    failed = Counter()
    empty_polls = 0
    output_bytes = 0
    resource_events = [
        (at, event) for at, event in events if event.get("resource_effect") is not False
    ]
    for at, event in resource_events:
        if not inside(at):
            continue
        name = event.get("tool", "unknown")
        calls[name] += 1
        if event.get("success") is False:
            failed[name] += 1
        elif event.get("success") is not True:
            reader.problem("events", "missing_resource_success")
        output = event.get("output")
        output_bytes += len(
            json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode()
        )
        if (
            name == "gpu_poll"
            and event.get("success") is True
            and isinstance(output, dict)
        ):
            text = output.get("output", "")
            if (
                output.get("running") is True
                and isinstance(text, str)
                and not text.strip()
            ):
                empty_polls += 1
    explicit_ledger = ledger_path is not None
    resource_config = config.get("resource") or {}
    configured_ledger = (
        resource_config.get("ledger_path")
        if isinstance(resource_config, dict)
        else None
    )
    if ledger_path is not None:
        ledger_path = Path(ledger_path)
    elif configured_ledger is not None:
        if not isinstance(configured_ledger, str) or not configured_ledger:
            raise ValueError("configured ledger_path must be a nonempty path string")
        ledger_path = Path(configured_ledger)
    else:
        ledger_path = None
    resource_expected = bool(
        explicit_ledger or resource_config or status.get("resource") or resource_events
    )
    if resource_expected and ledger_path is None:
        reader.problem("ledger", "undeclared")
    available = ledger_path is not None and ledger_path.exists()
    ledger = (
        reader.json(ledger_path, "ledger")
        if ledger_path is not None and (available or resource_expected)
        else {}
    )
    allocation = {
        "available": available,
        "reservations": 0,
        "dispatched_jobs": 0,
        "not_dispatched": 0,
        "settled_jobs": 0,
        "elapsed_seconds": 0,
        "unknown_jobs": 0,
        "nonterminal_jobs": 0,
        "states": {},
    }
    states = Counter()
    entries = ledger.get("entries", [] if not available else None)
    if not isinstance(entries, list):
        reader.problem("ledger", "invalid_entries")
        entries = []
    for entry in entries:
        if not isinstance(entry, dict):
            reader.problem("ledger", "invalid_entry")
            continue
        if not entry.get("dispatched_at"):
            continue
        if not inside(reader.at(entry, "ledger", "dispatched_at")):
            continue
        allocation["reservations"] += 1
        receipt = entry.get("receipt") or {}
        state = receipt.get("state", "UNKNOWN")
        elapsed = receipt.get("elapsed_seconds")
        if entry.get("job_id") or receipt.get("job_id"):
            allocation["dispatched_jobs"] += 1
        if (
            entry.get("status") == "TERMINAL"
            and state == "NOT_DISPATCHED"
            and elapsed == 0
        ):
            allocation["not_dispatched"] += 1
            states[state] += 1
        elif (
            entry.get("status") == "TERMINAL"
            and state in TERMINAL_STATES
            and type(elapsed) is int
            and elapsed >= 0
        ):
            allocation["settled_jobs"] += 1
            allocation["elapsed_seconds"] += elapsed
            states[state] += 1
        elif entry.get("status") == "UNKNOWN" or state == "UNKNOWN":
            allocation["unknown_jobs"] += 1
        else:
            allocation["nonterminal_jobs"] += 1
    allocation["states"] = dict(states)
    return {
        "schema_version": 2,
        "configuration": {
            "revision": config.get("revision"),
            "router": config.get("router"),
            "profiles": config.get("profiles", {}),
        },
        "window": {
            "since": start.isoformat() if start else None,
            "until": end.isoformat(),
            "interval": "since <= at < until",
        },
        "coverage": {
            "complete": not reader.issues,
            "issues": dict(reader.issues),
            "session_files_discovered": len(paths),
            "selected_threads": len(selected & sessions.keys()),
            "excluded_session_threads": len(sessions.keys() - selected),
            "usage_records_observed": len(seen_records),
            "usage_records_missing_threads": missing_record_threads,
            "awaiting_first_usage_threads": awaiting_first_usage_threads,
        },
        "llm": {
            "authority": AUTHORITY,
            "measurable": bool(seen_records) and not missing_record_threads,
            "total": _derived(totals),
            "by_role": {r: _derived(u) for r, u in by_role.items()},
            "by_task": tasks,
            "reconciliation": {
                "unique_response_records": totals["responses"],
                "cumulative_usage": _derived(cumulative_total),
                "record_minus_cumulative": {
                    k: totals[k] - cumulative_total[k] for k in TOKEN_KEYS
                },
                "interpretation": "Cumulative counts are a separate cross-check, never a fallback or added to request usage. Differences may include compaction usage, boundary timing or missing records.",
            },
        },
        "resources": {
            "calls": sum(calls.values()),
            "by_tool": dict(calls),
            "failed_calls": sum(failed.values()),
            "failed_by_tool": dict(failed),
            "successful_empty_running_gpu_polls": empty_polls,
            "output_json_bytes": output_bytes,
        },
        "agents": {
            "spawn_calls": tool_names["spawn_agent"],
            "descendant_threads_with_task_complete": len(complete_threads),
            "tool_calls_by_name": dict(tool_names),
            "tool_outcomes": aggregate_tool_outcomes(tasks),
            "waits_by_tool": {
                name: {
                    key: sum(task["waits_by_tool"][name][key] for task in tasks)
                    for key in _waits()
                }
                for name in sorted(WAIT_TOOLS)
            },
            "wait_sleep_calls": sum(
                n
                for name, n in tool_names.items()
                if name in {"wait", "wait_agent", "sleep"}
            ),
        },
        "allocation": allocation,
        "reader": {
            "elapsed_seconds": perf_counter() - started,
            "bytes_read": reader.bytes,
            "file_reads": reader.files,
        },
        "limitations": [
            "Usage is unique Study-wide response_id token_usage_record, assigned by notification time in [since, until), including compaction requests. Input includes cached and output includes reasoning; cumulative token_count is only a cross-check.",
            "Missing request records never fall back to cumulative counts. Threads with consumption evidence and no valid receipts are incomplete; threads without such evidence await first usage. This cannot detect every missing request within an otherwise recorded thread.",
            "Agent and Router identities use roles.json plus native session metadata; mapped threads and confirmed descendants are included. Unmapped histories remain a coverage gap. Native tool counts exclude calls nested in code execution.",
            "Allocation seconds are full settled elapsed time for jobs dispatched in the window, not window overlap or GPU core utilization; CANCELLED is a terminal state, not automatically failure.",
            "Wait durations sum observed call-to-return intervals for window-started calls returned before cutoff; parallel waits overlap and do not measure wall-clock time or waste.",
            "Tool failures require structured isError/is_error, success or exit_code outcomes. Plain-text outputs are unmeasured; tool-free time is not labeled idle or waste.",
            "Coverage describes readable local records, not proof all remote/native activity was recorded; live sources are not an atomic snapshot. No inference of research quality, wasted work or monetary cost.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--sessions-dir", type=Path)
    parser.add_argument("--ledger-path", type=Path)
    args = parser.parse_args(argv)
    try:
        result = report(
            args.root,
            since=args.since,
            until=args.until,
            sessions_dir=args.sessions_dir,
            ledger_path=args.ledger_path,
        )
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
