"""Receipt-only accounting for the explicit coordinator-free task roster.

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
    stages, threads, paths = set(), set(), set()
    for row in roster:
        if not isinstance(row, dict):
            raise ValueError("roster rows must be objects")
        stage = row.get("stage")
        if stage not in ("plan", "prepare", "evaluate") or stage in stages:
            raise ValueError("roster stages must be unique specialist stages")
        stages.add(stage)
        if type(row.get("invoked")) is not bool:
            raise ValueError("roster invoked must be a boolean")
        for key, seen in (("thread_id", threads), ("history_path", paths)):
            value = row.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not value:
                raise ValueError(f"roster {key} must be a nonempty string or None")
            identity = str(Path(value).resolve()) if key == "history_path" else value
            if identity in seen:
                raise ValueError(f"roster {key} must be unique")
            seen.add(identity)


def summarize_usage(roster: list[dict]) -> dict:
    """Read each explicit history once and charge all receipts to its owner stage.

    Only newline-terminated JSONL records are published. Session identity must match
    the roster before any receipt is accepted. Missing/invalid/truncated evidence is
    reported in coverage; cumulative counters and task outcome never supply usage.
    Uninvoked rows are not read. A duplicate valid receipt establishes evidence for
    its task but contributes tokens only once across the complete roster.
    """
    _validate_roster(roster)
    total = zero_usage()
    by_stage = {stage: zero_usage() for stage in STAGES}
    issues, tasks, seen = [], [], set()
    histories_read = 0

    for row in roster:
        stage, thread_id = row["stage"], row.get("thread_id")
        task = {
            "stage": stage,
            "thread_id": thread_id,
            "invoked": row["invoked"],
            "measurable": not row["invoked"],
            "usage_records_observed": 0,
            "valid_receipts": 0,
            "duplicate_responses": 0,
            "issues": [],
        }
        tasks.append(task)

        def issue(code, line=None):
            item = {"stage": stage, "thread_id": thread_id, "code": code}
            if line is not None:
                item["line"] = line
            issues.append(item)
            task["issues"].append(code)

        if not row["invoked"]:
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
                for line_number, raw in enumerate(stream, 1):
                    if not raw.endswith(b"\n"):
                        issue("truncated_history", line_number)
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
        task["measurable"] = task["valid_receipts"] > 0
        if not task["measurable"]:
            issue("missing_usage_receipts")

    return {
        "total": total,
        "by_stage": by_stage,
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
        "coordinator": {"present": False, "model_calls": 0},
        "unassigned": zero_usage(),
    }
