"""Read-only, aggregate diagnostics of recorded controller and native activity."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from time import perf_counter

TOKEN_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
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


def _tokens(value=None):
    value = value or {}
    return {key: value.get(key, 0) for key in TOKEN_KEYS}


def _add(target, value):
    for key in TOKEN_KEYS:
        target[key] += value[key]


def _usage():
    return {**_tokens(), "usage_updates": 0, "duplicate_updates": 0, "compactions": 0}


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
    lead = status.get("thread_id")
    if not isinstance(lead, str) or not lead:
        reader.problem("status", "missing_thread_id")
        lead = None
    roles_path = root / "roles.json"
    roles = reader.json(roles_path, "roles") if roles_path.exists() else {}
    role_threads = roles.get("threads") if isinstance(roles, dict) else None
    if not isinstance(role_threads, dict):
        role_threads = {}
    # First lead.input is the default window start, independent of file order.
    events = []
    starts = []
    for row in reader.rows(root / "events.jsonl", "events"):
        at = reader.at(row, "events", "at")
        if at is None:
            continue
        if row.get("event") == "lead.input" and row.get("thread_id") == lead:
            starts.append(at)
        if row.get("event") == "resource.tool":
            events.append((at, row))
    if start is None:
        start = min(starts) if starts else None
        if start is None:
            reader.problem("events", "missing_lead_input")
    if start is not None and start >= end:
        raise ValueError("since must be earlier than until")

    def inside(at):
        return at is not None and start is not None and start <= at < end

    directory = (
        Path(sessions_dir) if sessions_dir is not None else root / "native" / "sessions"
    )
    paths = sorted(directory.rglob("*.jsonl")) if directory.is_dir() else []
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
    selected = {lead} if lead else set()
    selected |= {tid for tid in role_threads if tid in sessions}
    while True:
        children = {
            tid
            for tid, (_, meta) in sessions.items()
            if isinstance(meta.get("source"), dict)
            and meta["source"]
            .get("subagent", {})
            .get("thread_spawn", {})
            .get("parent_thread_id")
            in selected
        }
        if children <= selected:
            break
        selected |= children
    if lead and lead not in sessions:
        reader.problem("sessions", "missing_lead")
    totals = _usage()
    by_role = {}
    tasks = []
    tool_names = Counter()
    complete_threads = set()
    records_total = _tokens()
    record_count = 0
    for tid in sorted(selected & sessions.keys()):
        path, meta = sessions[tid]
        mapped = (
            role_threads.get(tid) if isinstance(role_threads.get(tid), dict) else {}
        )
        task = mapped.get("task") or meta.get("agent_path", "/root").rsplit("/", 1)[-1]
        role = (
            "lead"
            if tid == lead
            else mapped.get("role")
            or next(
                (
                    r
                    for r in ("scout", "proposer", "experimenter", "analyst", "critic")
                    if task.lower().startswith(r)
                ),
                "unclassified",
            )
        )
        usage = _usage()
        previous = _tokens()
        seen_records = set()
        seen_calls = set()
        records = _tokens()
        started_at = first_tool_at = completed_at = None
        pending_waits = {}
        returned_waits = set()
        waits = {name: _waits() for name in sorted(WAIT_TOOLS)}
        for row in reader.rows(path, "sessions"):
            at = reader.at(row, "sessions")
            if at is None or at >= end:
                continue
            payload = row.get("payload", {})
            if not isinstance(payload, dict):
                reader.problem("sessions", "invalid_payload")
                continue
            kind = row.get("type")
            event = payload.get("type")
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
                try:
                    current = _tokens(payload["info"]["total_token_usage"])
                    if any(type(v) is not int or v < 0 for v in current.values()):
                        raise ValueError
                    delta = {k: current[k] - previous[k] for k in TOKEN_KEYS}
                except (KeyError, TypeError, ValueError, AttributeError):
                    reader.problem("sessions", "invalid_token_usage")
                    continue
                previous = current
                if any(v < 0 for v in delta.values()):
                    reader.problem("sessions", "cumulative_counter_decrease")
                    continue  # Rebaseline; the reset interval cannot be recovered exactly.
                if inside(at):
                    if any(delta.values()):
                        _add(usage, delta)
                        usage["usage_updates"] += 1
                    else:
                        usage["duplicate_updates"] += 1
            if kind == "token_usage_record" or (
                kind == "event_msg" and event == "token_usage_record"
            ):
                rid = payload.get("response_id")
                if not rid:
                    reader.problem("sessions", "usage_record_missing_response_id")
                elif rid not in seen_records:
                    seen_records.add(rid)
                    if inside(at):
                        record = _tokens(payload.get("usage"))
                        if all(type(v) is int and v >= 0 for v in record.values()):
                            _add(records, record)
                            record_count += 1
                        else:
                            reader.problem("sessions", "invalid_usage_record")
            if not inside(at):
                continue
            if kind == "compacted":
                usage["compactions"] += 1
            if kind == "event_msg" and event == "task_complete" and tid != lead:
                complete_threads.add(tid)
            if kind == "response_item" and event in {
                "function_call",
                "custom_tool_call",
            }:
                cid = payload.get("call_id", payload.get("id"))
                if cid is None or cid not in seen_calls:
                    seen_calls.add(cid)
                    tool_names[payload.get("name", "unknown")] += 1
        _add(records_total, records)
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
                "usage": _derived(usage),
            }
        )

    calls = Counter()
    failed = Counter()
    empty_polls = 0
    output_bytes = 0
    for at, event in events:
        if not inside(at) or event.get("thread_id") != lead:
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
    config = (
        reader.json(root / "config.json", "config")
        if (root / "config.json").exists()
        else {}
    )
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
        ledger_path = root / "gpu-budget.json"
    resource_expected = bool(
        explicit_ledger or resource_config or status.get("resource") or events
    )
    available = ledger_path.exists()
    ledger = (
        reader.json(ledger_path, "ledger") if available or resource_expected else {}
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
        "schema_version": 1,
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
        },
        "llm": {
            "total": _derived(totals),
            "by_role": {r: _derived(u) for r, u in by_role.items()},
            "by_task": tasks,
            "reconciliation": {
                "unique_response_records": record_count,
                "record_usage": _derived(records_total),
                "record_minus_cumulative": {
                    k: records_total[k] - totals[k] for k in TOKEN_KEYS
                },
                "interpretation": "Separate cross-check; positive differences may include uncounted compaction usage or boundary timing. Never added to cumulative totals.",
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
            "Recorded usage is assigned by notification time, without prorating responses across window boundaries; input includes cached and output includes reasoning tokens.",
            "Host-delegated roles use roles.json; other delegates are inferred from task names. Confirmed descendants and mapped host threads are included. Native tool counts exclude calls nested in code execution.",
            "Allocation seconds are full settled elapsed time for jobs dispatched in the window, not window overlap or GPU core utilization; CANCELLED is a terminal state, not automatically failure.",
            "Wait durations sum observed call-to-return intervals for window-started calls returned before cutoff; parallel waits overlap and do not measure wall-clock time or waste.",
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
