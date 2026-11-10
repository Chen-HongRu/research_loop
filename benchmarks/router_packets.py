"""Extract retrospective, input-anchored Lead packets without making model calls.

Raw snapshots and packet exports can contain private text. Keep them outside Git.
Receipts delimit response groups; they do not certify complete trace capture.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re


OUTPUTS = {"function_call_output", "custom_tool_call_output"}
ACTIONS = {"function_call", "custom_tool_call", "reasoning"}
TERMINAL = {"completed", "iteration-completed", "failed", "stopped"}


def _time(value):
    at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return at.astimezone(timezone.utc)


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            part["text"]
            for part in value
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _message_text(payload):
    value = payload.get("content", payload.get("message", payload.get("text")))
    if isinstance(value, str):
        return value, bool(value.strip())
    if isinstance(value, list):
        text = _text(value)
        return text, bool(text.strip()) and all(
            isinstance(part, dict) and isinstance(part.get("text"), str)
            for part in value
        )
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"], bool(value["text"].strip())
    return "", False


def _events(root, thread, start, end):
    events = []
    found = False
    for path in sorted((root / "native" / "sessions").rglob("*.jsonl")):
        with path.open(encoding="utf-8") as stream:
            meta = json.loads(next(stream))
            if meta.get("type") != "session_meta":
                raise ValueError("native trace lacks session metadata")
            if meta.get("payload", {}).get("id") != thread:
                continue
            found = True
            for line, raw in enumerate(stream, 2):
                if not raw.strip():
                    continue
                row = json.loads(raw)
                payload = row.get("payload", {})
                if not isinstance(payload, dict):
                    raise ValueError("native payload must be an object")
                if any(
                    t is not None and t != thread
                    for t in (row.get("thread_id"), payload.get("thread_id"))
                ):
                    raise ValueError("native thread identity mismatch")
                at = _time(row["timestamp"])
                if start <= at < end:
                    ref = {"path": str(path.relative_to(root)), "line": line}
                    events.append((at, ref["path"], line, row, ref))
    if not found:
        raise ValueError("missing Lead native trace")
    return sorted(events, key=lambda e: e[:3])


def _pending_key(payload, calls):
    """Only coalesce demonstrably unchanged pending deliveries, never results."""
    output = _text(payload.get("output", ""))
    call = calls.get(payload.get("call_id"), {})
    name = str(call.get("name", "")).rsplit(".", 1)[-1]
    if not any(word in name for word in ("wait", "poll", "exec", "stdin")):
        return None
    try:
        value = json.loads(output)
    except (ValueError, TypeError):
        value = None
    if isinstance(value, dict):
        # These fields describe transport/timing, rather than delivered work.
        value = {
            k: v
            for k, v in value.items()
            if k not in {"wall_time_seconds", "elapsed_seconds", "chunk_id"}
        }
        output = json.dumps(value, sort_keys=True, ensure_ascii=False)
    if not re.search(
        r"\b(pending|running|waiting|no new output|no messages|timed out|timeout)\b",
        output,
        re.IGNORECASE,
    ):
        return None
    return name, output


def _bounded(goal, request, state, evidence, max_chars):
    value = {
        "goal": goal,
        "request": request,
        "state": state,
        "evidence": [dict(e) for e in evidence],
    }
    truncated = False
    while True:
        excess = len(json.dumps(value, ensure_ascii=False)) - max_chars
        if excess <= 0:
            return value, truncated
        truncated = True
        if len(value["evidence"]) > 1:
            value["evidence"].pop(0)
            continue
        fields = [(value, "goal", False)]
        fields.append((value, "request", False))
        if value["evidence"]:
            fields.append((value["evidence"][0], "text", True))
        container, key, recent = max(fields, key=lambda f: len(f[0][f[1]]))
        text = container[key]
        if not text:
            raise ValueError("max_chars is too small for packet metadata")
        keep = max(0, len(text) - max(1, excess))
        container[key] = text[-keep:] if recent and keep else text[:keep]


def extract_round(
    snapshot: Path, iteration: int, max_chars=12000, max_assessment_chars=100000
):
    """Return packets from a closed terminal raw snapshot; never modify its files.

    A packet samples the first input and each changed visible delivery batch.
    Assistant actions (including prior actions) and host journal events never
    enter the prefix. No hindsight correctness label is generated.
    """
    root = Path(snapshot)
    if type(iteration) is not int or iteration < 1 or max_chars < 512:
        raise ValueError("iteration must be positive and max_chars at least 512")
    if max_assessment_chars < max_chars:
        raise ValueError("assessment prefix limit must be at least the candidate limit")
    status_path = f"iterations/{iteration:06d}/status.json"
    status = _read(root / status_path)
    if status.get("iteration", iteration) != iteration:
        raise ValueError("iteration identity mismatch")
    if status.get("status") not in TERMINAL:
        raise ValueError("only closed terminal baseline rounds can be extracted")
    start, end = _time(status["started_at"]), _time(status["ended_at"])
    if start >= end:
        raise ValueError("invalid iteration window")
    snapshot_meta = root / "snapshot.json"
    if snapshot_meta.exists() and _time(_read(snapshot_meta)["at"]) < end:
        raise ValueError("snapshot cutoff precedes iteration end")
    thread = status.get("thread_id")
    if not isinstance(thread, str) or not thread:
        raise ValueError("missing Lead thread identity")
    goal = (root / "goal.md").read_text(encoding="utf-8")
    admitted = status.get("human_request")
    request = (
        admitted["text"]
        if isinstance(admitted, dict)
        and isinstance(admitted.get("text"), str)
        and admitted["text"].strip()
        else "No additional human request."
    )
    state = {
        "iteration": iteration,
        "thread_id": thread,
        "started_at": status["started_at"],
    }
    evidence, refs, packets, calls = [], {}, [], {}
    group, anchor, group_refs = [], None, []
    changed, interleaved, ambiguous = False, False, False
    incomplete, anchor_incomplete = False, False
    orphan_since_input = False
    unrenderable_refs = []
    pending_key = None
    merged, ignored, receipts, orphan_receipts = 0, 0, 0, 0
    seen_receipts = set()
    issues = []

    def finish(response_id=None):
        nonlocal group, anchor, group_refs, interleaved, ambiguous
        nonlocal anchor_incomplete
        if group and anchor is not None:
            packet_input, truncated = _bounded(goal, request, state, anchor, max_chars)
            assessment_input, assessment_truncated = _bounded(
                goal, request, state, anchor, max_assessment_chars
            )
            known_group = bool(
                response_id
                and not ambiguous
                and any(p.get("type") != "reasoning" for p in group)
            )
            packets.append(
                {
                    "id": f"round-{iteration:06d}-op-{len(packets) + 1:04d}",
                    "iteration": iteration,
                    "input": packet_input,
                    "assessment_input": assessment_input,
                    "assessment_truncated": assessment_truncated,
                    "input_incomplete": anchor_incomplete,
                    "baseline_action": {
                        "response_id": response_id if known_group else None,
                        "receipt_response_id": response_id,
                        "items": group,
                        "opaque_reasoning": any(
                            bool(p.get("encrypted_content")) for p in group
                        ),
                    },
                    "source_refs": {
                        "admission": ["goal.md", status_path],
                        "input": [refs[e["id"]] for e in anchor],
                        "baseline_action": group_refs,
                    },
                    "prefix_truncated": truncated,
                    "grouping_status": "receipt_terminated"
                    if known_group
                    else "unknown",
                }
            )
        group, anchor, group_refs = [], None, []
        interleaved = ambiguous = False
        anchor_incomplete = False

    for _, _, _, row, ref in _events(root, thread, start, end):
        p, kind = row["payload"], row.get("type")
        subtype = p.get("type")
        receipt = kind == "token_usage_record" or (
            kind == "event_msg" and subtype == "token_usage_record"
        )
        if receipt:
            rid = p.get("response_id")
            if not isinstance(rid, str) or not rid or rid in seen_receipts:
                issues.append("duplicate_or_missing_receipt_id")
                continue
            seen_receipts.add(rid)
            receipts += 1
            if not group:
                orphan_receipts += 1
                orphan_since_input = True
            group_refs.append(ref)
            finish(rid)
            continue
        visible = kind == "response_item" and (
            subtype in OUTPUTS
            or subtype == "agent_message"
            or (subtype == "message" and p.get("role") == "user")
        )
        if visible:
            complete = True
            if subtype in OUTPUTS:
                key = _pending_key(p, calls)
                if key is not None and key == pending_key:
                    merged += 1
                    continue
                pending_key = key
                text = _text(p.get("output", ""))
            else:
                pending_key = None
                text, complete = _message_text(p)
            orphan_since_input = False
            if not complete or not text.strip():
                incomplete = True
                changed = True
                unrenderable_refs.append(ref)
                issues.append("unrenderable_visible_input")
                if group:
                    interleaved = True
                if not text.strip():
                    continue
            eid = f"e{len(evidence) + 1:05d}"
            evidence.append({"id": eid, "text": text})
            refs[eid] = {"id": eid, **ref}
            changed = True
            if group:
                interleaved = True
            continue
        action = kind == "response_item" and (
            subtype in ACTIONS
            or (subtype == "message" and p.get("role") == "assistant")
        )
        if not action:
            ignored += 1
            continue
        if not group:
            anchor = [dict(e) for e in evidence] if changed else None
            anchor_incomplete = incomplete
            ambiguous = orphan_since_input
            changed = False
        elif interleaved:
            ambiguous = True
        group.append(p)
        group_refs.append(ref)
        if subtype in {"function_call", "custom_tool_call"}:
            calls[p.get("call_id")] = p
    if group:
        issues.append("unterminated_response_group")
        finish()
    if not evidence:
        issues.append("no_visible_inputs")
    if any(p["grouping_status"] == "unknown" for p in packets):
        issues.append("ambiguous_response_grouping")
    return {
        "schema_version": 1,
        "iteration": iteration,
        "status": status["status"],
        "packets": packets,
        "coverage": {
            "packet_count": len(packets),
            "visible_inputs": len(evidence),
            "merged_pending_inputs": merged,
            "receipts": receipts,
            "orphan_receipts": orphan_receipts,
            "unrenderable_input_refs": unrenderable_refs,
            "incomplete_input_packets": sum(p["input_incomplete"] for p in packets),
            "ignored_nonvisible_events": ignored,
            "issues": sorted(set(issues)),
            "trace_completeness": "unverified",
            "candidate_prefix_limit_chars": max_chars,
            "assessment_prefix_limit_chars": max_assessment_chars,
            "candidate_prefix_truncated_packets": sum(
                p["prefix_truncated"] for p in packets
            ),
            "assessment_prefix_truncated_packets": sum(
                p["assessment_truncated"] for p in packets
            ),
            "candidate_dropped_evidence_items": sum(
                len(p["source_refs"]["input"]) - len(p["input"]["evidence"])
                for p in packets
            ),
            "assessment_dropped_evidence_items": sum(
                len(p["source_refs"]["input"]) - len(p["assessment_input"]["evidence"])
                for p in packets
            ),
        },
        "limitations": [
            "Retrospective input-anchored samples; no live counterfactual execution or correctness labels.",
            "Candidate prefix reduction is an intervention; richer assessment inputs have a separate bound and truncation flag.",
            "Encrypted reasoning is opaque and retained only in baseline_action; unrenderable visible inputs mark subsequent packets incomplete.",
            "Native event_msg.agent_message is an assistant output duplicate, never an inbound delivery.",
            "An orphan receipt taints following grouping until a new visible input boundary; receipt IDs are not inferred across that gap.",
            "Unknown grouping may combine outputs across missing/delayed receipts; input freezes before the first output.",
            "Host journal events and final status/results are excluded from inputs; prior assistant actions are omitted.",
            "Only recognized unchanged pending outputs are merged; changed outputs remain separate opportunities.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["extract"])
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--iteration", required=True, type=int)
    parser.add_argument("--max-chars", type=int, default=12000)
    parser.add_argument("--max-assessment-chars", type=int, default=100000)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.output.resolve().is_relative_to(args.snapshot.resolve()):
        parser.error("output must be outside the read-only source snapshot")
    try:
        result = extract_round(
            args.snapshot, args.iteration, args.max_chars, args.max_assessment_chars
        )
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    except (ValueError, OSError, KeyError, StopIteration) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
