"""Incremental observational behavior metrics; no model calls or Study writes."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import signal
import threading

from research_loop.metrics import (
    Collector,
    TOKEN_KEYS,
    WAITS,
    _atomic,
    add,
    timestamp,
    usage,
)

THRESHOLD = 16 * 1024
LIMITATIONS = [
    "Observed tool waits are proxies, not demonstrated waste. Tool-free latency includes reasoning and scheduling and is never labeled waiting.",
    "Creation uses session_meta time, not a verified parent spawn receipt. Missing dependency_ready_at is unknown; creation-before-readiness is not inferred.",
    "Wait seconds sum completed intervals overlapping the phase. Fraction uses their union divided by observed session lifetime overlapping the phase; open waits are censored.",
    "Output bytes are UTF-8 string bytes, or compact ensure_ascii=False JSON bytes for structured outputs, not wire bytes or tokens. Missing output has unknown size.",
    "Next unique token_usage_record after output is an association, not marginal cost, causation or currency. Batched outputs share one usage group; overlapping categories must not be added.",
    "Repeated read means identical visible read-tool input or a read-like shell invocation, including repetitions before the phase. It does not demonstrate duplicate content or unnecessary reading.",
    "Only recorded top-level calls are visible; tools nested inside exec, unrecorded work, missing usage and source gaps remain unmeasured. Sources need chronological timestamps.",
]


def behavior_state():
    return {
        "calls": {},
        "pending_outputs": [],
        "usage_groups": [],
        "read_signatures": [],
        "dependency_ready_at": None,
        "first_tool_at": None,
        "first_delivery_at": None,
    }


def output_bytes(output):
    if isinstance(output, str):
        return len(output.encode("utf-8"))
    return len(
        json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def read_signature(name, value):
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    if name not in {"read", "read_file", "read_text"} and not (
        name in {"exec_command", "shell", "run", "exec"}
        and re.search(r"\b(?:cat|sed|head|tail)\s|\.read_text\s*\(", text)
        and "apply_patch" not in text
    ):
        return None
    # Hash only the visible invocation, never file or output contents.
    return hashlib.sha256((name + "\0" + text).encode()).hexdigest()


class BehaviorCollector(Collector):
    """Reuse the established append-only reader and durable single-writer protocol."""

    def __init__(self, root, output, **kwargs):
        checkpoint = Path(output) / "checkpoint.json"
        if checkpoint.exists():
            state = json.loads(checkpoint.read_text())
            if state.get("behavior_schema") != 1 or not isinstance(
                state.get("resource_presentations"), dict
            ):
                raise ValueError(
                    "unsupported behavior checkpoint; use a new output directory"
                )
        super().__init__(root, output, **kwargs)
        self.state["behavior_schema"] = 1
        if not checkpoint.exists():
            self.state["resource_presentations"] = {
                "enabled_at": datetime.now(timezone.utc).isoformat(),
                "read_from_at": self.since.isoformat(),
                "read_from_at_basis": "phase_event_time",
                "coverage_mode": "phase_history_and_append",
                "prior_source_bytes_not_replayed": False,
                "observed_calls": 0,
                "eligible": 0,
                "compacted": 0,
                "original_output_bytes": 0,
                "returned_output_bytes": 0,
                "missing_or_legacy": 0,
                "invalid_metadata": 0,
            }

    def _issue(self, name):
        super()._issue(name)
        if (
            name.startswith("behavior:")
            and name != "behavior:unmatched_or_ambiguous_readiness"
        ):
            permanent = self.state.setdefault("permanent_issues", [])
            if name not in permanent:
                permanent.append(name)

    def _ready(self, behavior, value):
        try:
            ready = timestamp(value)
        except (ValueError, TypeError, AttributeError):
            self._issue("behavior:invalid_dependency_ready_at")
            return
        if ready >= self.cutoff:
            self._issue("behavior:future_dependency_ready_at")
            return
        old = behavior["dependency_ready_at"]
        if old is not None and timestamp(old) != ready:
            self._issue("behavior:conflicting_dependency_ready_at")
            behavior["ready_conflict"] = True
        else:
            behavior["dependency_ready_at"] = ready.isoformat()

    def _session(self, task, row, at):
        b = task.setdefault("behavior", behavior_state())
        p = row.get("payload", {})
        if not isinstance(p, dict):
            return super()._session(task, row, at)
        event, kind = p.get("type"), row.get("type")
        rid = p.get("response_id")
        seen = rid in self.responses if isinstance(rid, str) else False
        super()._session(task, row, at)
        if kind == "event_msg" and event == "task_started":
            task["completed_at"] = None
        if kind == "session_meta" and "dependency_ready_at" in p:
            self._ready(b, p["dependency_ready_at"])
        if (kind == "event_msg" and event == "task_complete") or (
            kind == "response_item"
            and event == "message"
            and p.get("role") == "assistant"
            and p.get("phase") == "final_answer"
        ):
            if b["first_delivery_at"] is None:
                b["first_delivery_at"] = at.isoformat()
        if kind == "token_usage_record" or (
            kind == "event_msg" and event == "token_usage_record"
        ):
            if isinstance(rid, str) and not seen and rid in self.responses:
                if b["pending_outputs"] and self._inside(at):
                    b["usage_groups"].append(
                        {
                            "response_id": rid,
                            "at": at.isoformat(),
                            "call_ids": b["pending_outputs"].copy(),
                            "usage": {
                                **{k: p["usage"].get(k, 0) for k in TOKEN_KEYS},
                                "responses": 1,
                            },
                        }
                    )
                b["pending_outputs"] = []
            return
        if kind != "response_item":
            return
        cid = p.get("call_id", p.get("id"))
        if not isinstance(cid, str):
            return
        if event in {"function_call", "custom_tool_call"} and cid not in b["calls"]:
            name = str(p.get("name", "unknown")).rsplit("__", 1)[-1]
            # Native names may also be namespace.tool.
            name = name.rsplit(".", 1)[-1]
            if b["first_tool_at"] is None:
                b["first_tool_at"] = at.isoformat()
            signature = read_signature(name, p.get("input", p.get("arguments", "")))
            repeat = signature is not None and signature in b["read_signatures"]
            if signature is not None and not repeat:
                b["read_signatures"].append(signature)
            b["calls"][cid] = {
                "call_id": cid,
                "name": name,
                "started_at": at.isoformat(),
                "returned_at": None,
                "output_utf8_bytes": None,
                "explicit_truncated": None,
                "visible_read": signature is not None,
                "repeated_visible_read": repeat,
            }
        elif event in {"function_call_output", "custom_tool_call_output"}:
            call = b["calls"].get(cid)
            if call is None:
                if self._inside(at):
                    self._issue("behavior:orphan_tool_output")
                return
            if call["returned_at"] is not None:
                return
            if at < timestamp(call["started_at"]):
                self._issue("behavior:negative_call_duration")
                return
            call["returned_at"] = at.isoformat()
            if "output" in p:
                call["output_utf8_bytes"] = output_bytes(p["output"])
            else:
                self._issue("behavior:missing_tool_output")
            output = p.get("output")
            # Only explicit boolean markers, not guessed clipping from output length.
            marker = p.get("truncated")
            if type(marker) is not bool and isinstance(output, dict):
                marker = output.get("truncated")
            if type(marker) is not bool and isinstance(output, str):
                if re.search(
                    r"Warning: truncated output|Output truncated|\b\d+ tokens truncated\b",
                    output,
                    re.IGNORECASE,
                ):
                    marker = True
            call["explicit_truncated"] = marker if type(marker) is bool else None
            b["pending_outputs"].append(cid)

    def _at(self, row, key="timestamp"):
        if key == "timestamp" and "task_id" in row and "ready_at" in row:
            return super()._at(row, "ready_at")
        return super()._at(row, key)

    def _events(self):
        readiness = self.state.setdefault("behavior_readiness", [])
        for row, at in self._tail(self.root / "events.jsonl", "events"):
            self._source_at(at)
            if (
                row.get("event") == "resource.tool"
                and self._inside(at)
                and row.get("tool") in {"gpu_exec", "gpu_poll"}
            ):
                r = self.state["resource_presentations"]
                r["observed_calls"] += 1
                output = row.get("output")
                keys = ("output_compacted", "output_bytes", "returned_output_bytes")
                if not isinstance(output, dict) or any(
                    key not in output for key in keys
                ):
                    r["missing_or_legacy"] += 1
                elif type(output["output_compacted"]) is not bool or any(
                    type(output[key]) is not int or output[key] < 0 for key in keys[1:]
                ):
                    r["invalid_metadata"] += 1
                else:
                    r["eligible"] += 1
                    r["compacted"] += int(output["output_compacted"])
                    r["original_output_bytes"] += output["output_bytes"]
                    r["returned_output_bytes"] += output["returned_output_bytes"]
            if row.get("event") == "agent.dependency_ready":
                readiness.append(
                    {
                        "thread_id": row.get("thread_id"),
                        "ready_at": row.get("dependency_ready_at"),
                    }
                )
        path = self.root / "work/dispatch-readiness.jsonl"
        if path.exists():
            for row, at in self._tail(path, "readiness"):
                if (
                    row.get("confirmed") is not True
                    or row.get("confirmed_by") != "coordinator"
                ):
                    self._issue("behavior:unconfirmed_readiness_record")
                    continue
                readiness.append(
                    {"task_id": row.get("task_id"), "ready_at": row.get("ready_at")}
                )
        unmatched = []
        for record in readiness:
            matches = [
                task
                for task in self.state["tasks"].values()
                if (
                    isinstance(record.get("thread_id"), str)
                    and task["thread_id"] == record["thread_id"]
                )
                or (
                    isinstance(record.get("task_id"), str)
                    and task["task"] == record["task_id"]
                )
            ]
            if len(matches) != 1:
                self._issue("behavior:unmatched_or_ambiguous_readiness")
                unmatched.append(record)
                continue
            self._ready(
                matches[0].setdefault("behavior", behavior_state()), record["ready_at"]
            )
        self.state["behavior_readiness"] = unmatched

    def _allocation(self, config, status):
        # This sidecar does not need a resource ledger or inspect data/checkpoints.
        return {}

    def _task_summary(self, task, cutoff):
        b = task.get("behavior", behavior_state())
        since = self.since
        calls = list(b["calls"].values())
        started = timestamp(task["started_at"]) if task.get("started_at") else None
        completed = (
            timestamp(task["completed_at"]) if task.get("completed_at") else None
        )
        end = min(completed, cutoff) if completed else cutoff
        begin = max(started, since) if started else None
        lifetime = max(0.0, (end - begin).total_seconds()) if begin else None
        returned = [
            c
            for c in calls
            if c["returned_at"] and since <= timestamp(c["returned_at"]) < cutoff
        ]
        phase_calls = [c for c in calls if since <= timestamp(c["started_at"]) < cutoff]
        spans = []
        for c in calls:
            if c["name"] not in WAITS or not c["returned_at"] or begin is None:
                continue
            a, z = (
                max(timestamp(c["started_at"]), begin),
                min(timestamp(c["returned_at"]), end),
            )
            if z > a:
                spans.append((a, z))
        merged = []
        for a, z in sorted(spans):
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], z))
            else:
                merged.append((a, z))
        union = sum((z - a).total_seconds() for a, z in merged)
        total_bytes = sum(c["output_utf8_bytes"] or 0 for c in returned)
        large = [c for c in returned if (c["output_utf8_bytes"] or 0) > THRESHOLD]
        associated = {
            k: usage()
            for k in (
                "any_output",
                "wait_return",
                "large_output",
                "repeated_visible_read",
            )
        }
        for group in b["usage_groups"]:
            group_calls = [b["calls"][cid] for cid in group["call_ids"]]
            add(associated["any_output"], group["usage"])
            if any(c["name"] in WAITS for c in group_calls):
                add(associated["wait_return"], group["usage"])
            if any((c["output_utf8_bytes"] or 0) > THRESHOLD for c in group_calls):
                add(associated["large_output"], group["usage"])
            if any(c["repeated_visible_read"] for c in group_calls):
                add(associated["repeated_visible_read"], group["usage"])
        ready = b["dependency_ready_at"]
        delay = (
            (started - timestamp(ready)).total_seconds()
            if started and ready and not b.get("ready_conflict")
            else None
        )

        def latency(value):
            return (
                (timestamp(value) - started).total_seconds()
                if value and started
                else None
            )

        return {
            "thread_id": task["thread_id"],
            "task": task["task"],
            "role": task["role"],
            "parent": task["parent"],
            "is_router": task["is_router"],
            "created_at_proxy": task.get("started_at"),
            "completed_at": task.get("completed_at"),
            "started_before_phase": started < since if started else None,
            "active_in_phase": bool(
                task.get("last_source_event_at")
                and self.since <= timestamp(task["last_source_event_at"]) < cutoff
            ),
            "lifetime_phase_seconds": lifetime,
            "lifetime_right_censored": completed is None,
            "dependency_ready_at": ready,
            "dependency_ready_status": "conflicting"
            if b.get("ready_conflict")
            else "observed"
            if ready
            else "missing",
            "created_minus_dependency_ready_seconds": delay,
            "created_before_dependencies_ready": delay < 0
            if delay is not None
            else None,
            "creation_to_first_tool_seconds": latency(b["first_tool_at"]),
            "creation_to_first_delivery_seconds": latency(b["first_delivery_at"]),
            "wait_calls": sum(c["name"] in WAITS for c in phase_calls),
            "returned_wait_calls": sum(c["name"] in WAITS for c in returned),
            "open_wait_calls": sum(
                c["name"] in WAITS and c["returned_at"] is None for c in calls
            ),
            "wait_seconds": sum((z - a).total_seconds() for a, z in spans),
            "wait_union_seconds": union,
            "wait_lifetime_fraction": union / lifetime if lifetime else None,
            "tool_outputs": returned,
            "output_utf8_bytes": total_bytes,
            "output_bytes_missing": sum(
                c["output_utf8_bytes"] is None for c in returned
            ),
            "over_16kib_count": len(large),
            "over_16kib_bytes_fraction": sum(c["output_utf8_bytes"] for c in large)
            / total_bytes
            if total_bytes
            else None,
            "explicit_truncated_count": sum(
                c["explicit_truncated"] is True for c in returned
            ),
            "truncation_marker_missing": sum(
                c["explicit_truncated"] is None for c in returned
            ),
            "repeated_visible_read_calls": sum(
                c["repeated_visible_read"] for c in phase_calls
            ),
            "associated_usage": associated,
            "usage_groups": b["usage_groups"],
            "outputs_awaiting_usage": len(b["pending_outputs"]),
        }

    def _sample_history(self, pending):
        if "behavior" in pending and "resource_presentations" not in pending:
            raise ValueError("unsupported behavior sample; use a new output directory")
        if "behavior" not in pending:
            cutoff = timestamp(pending["window"]["until"])
            tasks = [
                self._task_summary(t, cutoff) for t in self.state["tasks"].values()
            ]
            active_ids = {
                t["thread_id"]
                for t in pending["llm"]["by_task"]
                if t["active_in_phase"]
            }
            for task in tasks:
                task["active_in_phase"] = task["thread_id"] in active_ids
            active_tasks = [t for t in tasks if t["active_in_phase"]]
            active_children = [t for t in active_tasks if not t["is_router"]]
            pending["behavior"] = {
                "threshold_bytes": THRESHOLD,
                "by_task": tasks,
            }

            def aggregate(cohort):
                byte_count = sum(t["output_utf8_bytes"] for t in cohort)
                large_bytes = sum(
                    c["output_utf8_bytes"]
                    for t in cohort
                    for c in t["tool_outputs"]
                    if (c["output_utf8_bytes"] or 0) > THRESHOLD
                )
                totals = {
                    key: sum(t[key] for t in cohort)
                    for key in (
                        "wait_calls",
                        "returned_wait_calls",
                        "open_wait_calls",
                        "wait_seconds",
                        "wait_union_seconds",
                        "output_utf8_bytes",
                        "output_bytes_missing",
                        "over_16kib_count",
                        "explicit_truncated_count",
                        "truncation_marker_missing",
                        "repeated_visible_read_calls",
                        "outputs_awaiting_usage",
                    )
                }
                totals["tasks"] = len(cohort)
                totals["over_16kib_bytes_fraction"] = (
                    large_bytes / byte_count if byte_count else None
                )
                totals["dependency_ready_missing_tasks"] = sum(
                    not t["is_router"] and t["dependency_ready_status"] == "missing"
                    for t in cohort
                )
                totals["associated_usage"] = {
                    category: usage()
                    for category in (
                        "any_output",
                        "wait_return",
                        "large_output",
                        "repeated_visible_read",
                    )
                }
                for task in cohort:
                    for category, value in task["associated_usage"].items():
                        add(totals["associated_usage"][category], value)
                return totals

            pending["behavior"]["total"] = aggregate(active_tasks)
            pending["behavior"]["children"] = aggregate(active_children)
            pending["behavior"]["by_role"] = {
                role: aggregate([t for t in active_tasks if t["role"] == role])
                for role in sorted({t["role"] for t in active_tasks})
            }
            pending["behavior"]["coverage"] = {
                "readiness_missing_threads": [
                    t["thread_id"]
                    for t in active_children
                    if t["dependency_ready_status"] == "missing"
                ],
                "unmatched_readiness_records": len(
                    self.state.get("behavior_readiness", [])
                ),
                "outputs_awaiting_usage": sum(
                    t["outputs_awaiting_usage"] for t in active_tasks
                ),
                "nested_calls_visible": False,
            }
            pending["limitations"] = LIMITATIONS + [
                "Sources are not an atomic snapshot; coverage cannot prove all activity was recorded. Source rotation/truncation skips overwritten bytes and remains a permanent gap.",
                "Usage is unique response_id token_usage_record, not summed token_count. Phase usage uses notification time in [since,until), even when associated output preceded the phase.",
                "Journal samples retain task aggregates; per-call output metadata and usage groups are in latest.json. Checkpoint-first recovery repairs interrupted publication without duplicate samples.",
            ]
            pending.pop("resources", None)
            pending.pop("allocation", None)
            # Avoid duplicating internal call bookkeeping in the ordinary LLM summary.
            for task in pending["llm"]["by_task"]:
                task.pop("behavior", None)
        if "resource_presentations" not in pending:
            presentation = dict(self.state["resource_presentations"])
            presentation["status"] = (
                "partial_metadata"
                if presentation["missing_or_legacy"] or presentation["invalid_metadata"]
                else "observed_metadata"
                if presentation["eligible"]
                else "no_records"
            )
            if not presentation["eligible"]:
                for key in (
                    "compacted",
                    "original_output_bytes",
                    "returned_output_bytes",
                ):
                    presentation[key] = None
            pending["resource_presentations"] = presentation
        history = json.loads(json.dumps(pending))
        for task in history["behavior"]["by_task"]:
            task.pop("tool_outputs", None)
            task.pop("usage_groups", None)
        return history


def compare(baseline, improved):
    """Compare observations in same-role completed, unmixed descendant cohorts."""
    snapshots = []
    for directory in (baseline, improved):
        snapshot = json.loads((Path(directory) / "latest.json").read_text())
        if not isinstance(
            snapshot.get("behavior", {}).get("by_task"), list
        ) or not isinstance(snapshot.get("resource_presentations"), dict):
            raise ValueError(
                "comparison requires current behavior collector latest.json"
            )
        snapshots.append(snapshot)

    def completed(snapshot):
        return [
            task
            for task in snapshot["behavior"]["by_task"]
            if not task["is_router"]
            and task.get("active_in_phase") is True
            and task.get("started_before_phase") is False
            and task.get("completed_at") is not None
            and task.get("lifetime_right_censored") is False
        ]

    cohorts = [completed(snapshot) for snapshot in snapshots]
    roles = sorted({t["role"] for t in cohorts[0]} & {t["role"] for t in cohorts[1]})

    def mean(values):
        return sum(values) / len(values) if values else None

    def readiness(tasks):
        observed = [t for t in tasks if t.get("dependency_ready_status") == "observed"]
        compared = [
            t["created_before_dependencies_ready"]
            for t in observed
            if type(t.get("created_before_dependencies_ready")) is bool
        ]
        return {
            "tasks": len(tasks),
            "observed": len(observed),
            "unknown_or_conflicting": len(tasks) - len(observed),
            "observed_fraction": len(observed) / len(tasks) if tasks else None,
            "creation_comparisons_known": len(compared),
            "created_before_ready_count": sum(compared) if compared else None,
        }

    def summary(tasks, snapshot):
        def numeric_values(key):
            return [t[key] for t in tasks if type(t.get(key)) in (int, float)]

        outputs = [c for t in tasks for c in t.get("tool_outputs", [])]
        sized = [c for c in outputs if type(c.get("output_utf8_bytes")) is int]
        large = [c for c in sized if c["output_utf8_bytes"] > THRESHOLD]
        byte_count = sum(c["output_utf8_bytes"] for c in sized)
        known_byte_tasks = [
            t["output_utf8_bytes"]
            for t in tasks
            if t.get("output_bytes_missing") == 0
            and type(t.get("output_utf8_bytes")) is int
        ]
        wait_pairs = [
            (t["wait_union_seconds"], t["lifetime_phase_seconds"])
            for t in tasks
            if type(t.get("wait_union_seconds")) in (int, float)
            and type(t.get("lifetime_phase_seconds")) in (int, float)
        ]
        lifetime = sum(seconds for _, seconds in wait_pairs)
        authorities = {
            t["thread_id"]: t.get("authority_missing")
            for t in snapshot.get("llm", {}).get("by_task", [])
        }
        associated = {}
        for category in (
            "any_output",
            "wait_return",
            "large_output",
            "repeated_visible_read",
        ):
            observed_usage = [
                t["associated_usage"][category]
                for t in tasks
                if isinstance(t.get("associated_usage", {}).get(category), dict)
            ]
            complete_usage = [
                t["associated_usage"][category]
                for t in tasks
                if isinstance(t.get("associated_usage", {}).get(category), dict)
                and t.get("outputs_awaiting_usage") == 0
                and authorities.get(t["thread_id"]) is False
            ]
            associated[category] = {
                "tasks_with_fields": len(observed_usage),
                "complete_tasks": len(complete_usage),
                "observed_totals": {
                    key: sum(values)
                    if (
                        values := [
                            u[key] for u in observed_usage if type(u.get(key)) is int
                        ]
                    )
                    else None
                    for key in (*TOKEN_KEYS, "responses")
                },
                "mean_per_complete_task": {
                    key: mean(
                        [u[key] for u in complete_usage if type(u.get(key)) is int]
                    )
                    for key in (*TOKEN_KEYS, "responses")
                },
            }
        return {
            "tasks": len(tasks),
            "thread_ids": [t["thread_id"] for t in tasks],
            "mean_wait_lifetime_fraction": mean(
                numeric_values("wait_lifetime_fraction")
            ),
            "wait_fraction_known_tasks": len(numeric_values("wait_lifetime_fraction")),
            "pooled_wait_lifetime_fraction": sum(wait for wait, _ in wait_pairs)
            / lifetime
            if lifetime
            else None,
            "mean_output_utf8_bytes_per_known_task": mean(known_byte_tasks),
            "output_bytes_known_tasks": len(known_byte_tasks),
            "known_size_outputs": len(sized),
            "outputs_missing_size": len(outputs) - len(sized),
            "over_16kib_count": len(large),
            "over_16kib_output_fraction": len(large) / len(sized) if sized else None,
            "over_16kib_byte_fraction": sum(c["output_utf8_bytes"] for c in large)
            / byte_count
            if byte_count
            else None,
            "associated_usage": associated,
            "tasks_with_pending_usage": sum(
                t.get("outputs_awaiting_usage", 0) > 0 for t in tasks
            ),
            "readiness": readiness(tasks),
        }

    result = {
        "shared_completed_roles": roles,
        "phases": {},
        "interpretation": "Descriptive observations only. Cohorts share recorded roles, not matched workloads. Missing values remain unknown. Usage is associated tokens, not causal cost or savings; overlapping usage categories must not be added. Resource metadata covers its explicitly reported interval only.",
    }
    for name, snapshot, complete in zip(("baseline", "improved"), snapshots, cohorts):
        active_children = [
            t
            for t in snapshot["behavior"]["by_task"]
            if not t["is_router"] and t.get("active_in_phase") is True
        ]
        included = [t for t in complete if t["role"] in roles]
        result["phases"][name] = {
            "phase": snapshot.get("phase"),
            "sample_id": snapshot.get("sample_id"),
            "window": snapshot.get("window"),
            "configuration": snapshot.get("configuration"),
            "coverage": snapshot.get("coverage"),
            "behavior_coverage": snapshot["behavior"].get("coverage"),
            "cohort": {
                "active_children": len(active_children),
                "completed_unmixed": len(complete),
                "included_shared_roles": len(included),
                "excluded_incomplete_or_mixed": len(active_children) - len(complete),
                "excluded_unshared_roles": len(complete) - len(included),
            },
            "active_children_readiness": readiness(active_children),
            "shared_role_cohort": summary(included, snapshot),
            "by_role": {
                role: summary([t for t in included if t["role"] == role], snapshot)
                for role in roles
            },
            "resource_presentations": snapshot["resource_presentations"],
        }
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    watch = commands.add_parser("watch")
    watch.add_argument("root", type=Path)
    watch.add_argument("--output", type=Path, required=True)
    watch.add_argument("--since", required=True)
    watch.add_argument("--phase", required=True)
    watch.add_argument("--interval", type=float, default=5)
    watch.add_argument("--once", action="store_true")
    watch.add_argument("--until")
    watch.add_argument("--baseline", type=Path)
    comparison = commands.add_parser("compare")
    comparison.add_argument("baseline", type=Path)
    comparison.add_argument("improved", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "compare":
            print(
                json.dumps(
                    compare(args.baseline, args.improved), ensure_ascii=False, indent=2
                )
            )
            return
        if (
            args.baseline is not None
            and args.baseline.resolve() == args.output.resolve()
        ):
            raise ValueError("baseline and improved output must differ")
        if args.interval <= 0:
            raise ValueError("interval must be positive")
        if args.until and not args.once:
            raise ValueError("--until requires --once")
        stopped = threading.Event()
        previous = {
            s: signal.signal(s, lambda *_: stopped.set())
            for s in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            with BehaviorCollector(
                args.root, args.output, since=args.since, phase=args.phase
            ) as collector:
                while not stopped.is_set():
                    result = collector.poll(args.until)
                    if args.baseline is not None:
                        _atomic(
                            args.output.parent / "behavior-comparison.json",
                            compare(args.baseline, args.output),
                        )
                    if args.once:
                        print(json.dumps(result, ensure_ascii=False, indent=2))
                        break
                    stopped.wait(args.interval)
        finally:
            for s, handler in previous.items():
                signal.signal(s, handler)
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
