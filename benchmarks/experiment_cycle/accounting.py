"""Receipt-only accounting for the explicit task roster.

Sums are measured partial sums when coverage is incomplete, never estimates of
missing consumption, billing, or wasted tokens. Roster order owns duplicate IDs.
"""

import json
from pathlib import Path

from research_loop.token_usage import AUTHORITY, TOKEN_KEYS, accept_record

STAGES = ("plan", "prepare", "execute", "evaluate")


def zero_usage() -> dict:
    """Return an independent empty bucket; coverage determines measurability."""
    return dict.fromkeys(
        (*TOKEN_KEYS, "uncached_input_tokens", "total_tokens", "responses"), 0
    )


def _add(bucket, usage):
    for key in (*TOKEN_KEYS, "responses"):
        bucket[key] += usage[key]
    bucket["uncached_input_tokens"] = (
        bucket["input_tokens"] - bucket["cached_input_tokens"]
    )
    bucket["total_tokens"] = bucket["input_tokens"] + bucket["output_tokens"]


def _validate_roster(roster):
    if not isinstance(roster, list):
        raise ValueError("roster must be a list")
    for row in roster:
        if not isinstance(row, dict):
            raise ValueError("roster rows must be objects")
        if row.get("stage") not in STAGES:
            raise ValueError("roster stage must be a recognized stage")
        if type(row.get("invoked")) is not bool:
            raise ValueError("roster invoked must be a boolean")
        for key in ("thread_id", "history_path", "role", "context", "call_id"):
            value = row.get(key)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"roster {key} must be a nonempty string or None")


def _window(row):
    start, end = row.get("history_start_bytes"), row.get("history_end_bytes")
    if start is None and end is None:
        return None
    if type(start) is not int or type(end) is not int or start < 0 or end < start:
        return False
    return (start, end)


def _ambiguous_rows(roster):
    bad = set()
    for i, row in enumerate(roster):
        if not row["invoked"]:
            continue
        window = _window(row)
        if window is False:
            bad.add(i)
        for j in range(i):
            other = roster[j]
            if not other["invoked"]:
                continue
            same_path = (
                row.get("history_path")
                and other.get("history_path")
                and (
                    Path(row["history_path"]).resolve()
                    == Path(other["history_path"]).resolve()
                )
            )
            same_thread = row.get("thread_id") and row.get("thread_id") == other.get(
                "thread_id"
            )
            if not (same_path or same_thread):
                continue
            other_window = _window(other)
            if (
                not same_path
                or window is None
                or other_window is None
                or window is False
                or other_window is False
                or max(window[0], other_window[0]) < min(window[1], other_window[1])
            ):
                bad.update((i, j))
    return bad


def summarize_usage(roster: list[dict]) -> dict:
    """Charge receipts within each optional half-open byte window to its stage.

    Only newline-terminated JSONL records are published. Session identity must match
    the roster before any receipt is accepted. Missing/invalid/truncated evidence is
    reported in coverage; cumulative counters and task outcome never supply usage.
    Windowed histories read the first session metadata line for identity and require
    newline-aligned bounds. Uninvoked rows are not read. A duplicate valid receipt establishes evidence for
    its task but contributes tokens only once across the complete roster.
    """
    _validate_roster(roster)
    total = zero_usage()
    by_stage = {stage: zero_usage() for stage in STAGES}
    by_role, by_context = {}, {}
    coordinator_usage = zero_usage()
    coordinator_present = any(row.get("role") == "coordinator" for row in roster)
    issues, tasks, seen = [], [], set()
    histories_read = 0

    ambiguous = _ambiguous_rows(roster)
    for row_index, row in enumerate(roster):
        stage, thread_id = row["stage"], row.get("thread_id")
        role, context = row.get("role") or stage, row.get("context")
        by_role.setdefault(role, zero_usage())
        if context is not None:
            by_context.setdefault(context, zero_usage())
        task = {
            "stage": stage,
            "role": role,
            "thread_id": thread_id,
            "invoked": row["invoked"],
            "measurable": not row["invoked"],
            "usage_records_observed": 0,
            "valid_receipts": 0,
            "duplicate_responses": 0,
            "issues": [],
        }
        for key in (
            "call_id",
            "context",
            "history_start_bytes",
            "history_end_bytes",
            "settlement_complete",
        ):
            if key in row:
                task[key] = row[key]
        tasks.append(task)

        def issue(code, line=None):
            item = {"stage": stage, "thread_id": thread_id, "code": code}
            if line is not None:
                item["line"] = line
            issues.append(item)
            task["issues"].append(code)

        if not row["invoked"]:
            continue
        if "settlement_complete" in row:
            if type(row["settlement_complete"]) is not bool:
                issue("invalid_settlement_evidence")
            elif not row["settlement_complete"]:
                issue("unsettled_runtime")
        if row_index in ambiguous:
            issue("invalid_history_window")
            continue
        if thread_id is None:
            issue("missing_thread_id")
        path = row.get("history_path")
        if path is None:
            issue("absent_history")
            continue
        records, identities = [], []
        try:
            with Path(path).open("rb") as stream:
                histories_read += 1
                window = _window(row)
                if window is not None:
                    start, end = window
                    stream.seek(0, 2)
                    if end > stream.tell():
                        issue("invalid_history_window")
                        continue
                    if end:
                        stream.seek(end - 1)
                        if stream.read(1) != b"\n":
                            issue("invalid_history_window")
                            continue
                    if start:
                        stream.seek(start - 1)
                        if stream.read(1) != b"\n":
                            issue("invalid_history_window")
                            continue
                        stream.seek(0)
                        try:
                            meta = json.loads(stream.readline())
                        except (ValueError, UnicodeDecodeError):
                            meta = None
                        if (
                            isinstance(meta, dict)
                            and meta.get("type") == "session_meta"
                        ):
                            payload = meta.get("payload")
                            identities.append(
                                payload.get("id") if isinstance(payload, dict) else None
                            )
                    stream.seek(start)
                line_number = 0
                while window is None or stream.tell() < end:
                    line_number += 1
                    raw = (
                        stream.readline()
                        if window is None
                        else stream.readline(end - stream.tell())
                    )
                    if not raw:
                        break
                    if not raw.endswith(b"\n"):
                        issue(
                            "truncated_history"
                            if window is None
                            else "invalid_history_window",
                            line_number,
                        )
                        break
                    try:
                        record = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        issue("invalid_json", line_number)
                        continue
                    if not isinstance(record, dict):
                        issue("invalid_record", line_number)
                        continue
                    if record.get("type") == "session_meta":
                        payload = record.get("payload")
                        identities.append(
                            payload.get("id") if isinstance(payload, dict) else None
                        )
                    elif record.get("type") == "token_usage_record":
                        records.append((line_number, record.get("payload")))
        except FileNotFoundError:
            issue("missing_history")
            continue
        except OSError:
            issue("unreadable_history")
            continue
        task["usage_records_observed"] = len(records)
        if not identities:
            issue("missing_session_identity")
            continue
        if thread_id is None or any(identity != thread_id for identity in identities):
            issue("session_identity_mismatch")
            continue
        for line_number, payload in records:
            usage, problem = accept_record(
                payload if isinstance(payload, dict) else {}, seen
            )
            if problem == "duplicate":
                # accept_record checks IDs before fields. A malformed repeated
                # ID must not establish valid evidence for an otherwise empty task.
                _, duplicate_problem = accept_record(payload, set())
                if duplicate_problem:
                    issue(duplicate_problem, line_number)
                else:
                    task["duplicate_responses"] += 1
                    task["valid_receipts"] += 1
            elif problem:
                issue(problem, line_number)
            else:
                task["valid_receipts"] += 1
                _add(total, usage)
                _add(by_stage[stage], usage)
                _add(by_role[role], usage)
                if context is not None:
                    _add(by_context[context], usage)
                if row.get("role") == "coordinator":
                    _add(coordinator_usage, usage)
        task["measurable"] = task["valid_receipts"] > 0
        if not task["measurable"]:
            issue("missing_usage_receipts")

    return {
        "total": total,
        "by_stage": by_stage,
        "by_role": by_role,
        "by_context": by_context,
        "coverage": {
            "authority": AUTHORITY,
            "complete": not issues,
            "measurable": all(task["measurable"] for task in tasks),
            "issues": issues,
            "tasks": tasks,
            "invoked_tasks": sum(task["invoked"] for task in tasks),
            "histories_read": histories_read,
            "usage_records_observed": sum(
                task["usage_records_observed"] for task in tasks
            ),
            "duplicate_responses": sum(task["duplicate_responses"] for task in tasks),
        },
        "coordinator": (
            {
                "present": True,
                "model_calls": coordinator_usage["responses"],
                "usage": coordinator_usage,
            }
            if coordinator_present
            else {"present": False, "model_calls": 0}
        ),
        "unassigned": zero_usage(),
    }
