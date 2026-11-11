"""Pure, bounded observations from an already frozen Router evidence prefix.

Only explicit delivered return structures establish task/operation observations.
Quoted prose, plans, examples and nested result strings remain untrusted notes.
This module reads no files and executes no code.
"""

from collections import Counter
from copy import deepcopy
import json
import re

from research_loop.router import validate_packet


STATE_LIMIT = 4000
CATEGORIES = ("tasks", "operations", "priority_notes", "plan_notes", "status_notes")
FIELDS = (
    "role",
    "task_name",
    "name",
    "status",
    "phase",
    "summary",
    "result_summary",
    "result",
)
PLAN_KEYS = {
    "next_action",
    "next_step",
    "plan",
    "prepared_plan",
    "prepared_operation",
    "review_requirements",
    "review_required",
    "completion_check",
}
PRIORITY_KEYS = {"human_request", "user_priority", "priority", "requested_work"}
STATUS_KEYS = {
    "status_notes",
    "pending",
    "completed",
    "in_flight",
    "blocked",
    "readiness",
}
TRANSPORT_KEYS = {
    "output",
    "wall_time_seconds",
    "elapsed_seconds",
    "exit_code",
    "session_id",
    "chunk_id",
    "original_token_count",
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _short(value, limit=180):
    if isinstance(value, str):
        return value[:limit]
    if value is None or isinstance(value, (bool, int, float)):
        return _json(value)
    return None


def _document(text):
    """Accept a whole JSON delivery, never braces embedded in prose or code."""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        # A rendered shell-tool transport has a fixed Output header. Its entire
        # output must still be JSON; arbitrary prose/fenced examples do not pass.
        if isinstance(text, str) and re.match(
            r"^(?:Script completed|Wall time:|Chunk ID:|Process exited with code)", text
        ):
            marker = "\nOutput:\n"
            if marker in text:
                try:
                    return json.loads(text.split(marker, 1)[1])
                except ValueError:
                    # Explicit stdout may contain consecutive JSON documents.
                    # Every non-whitespace character must belong to one of them.
                    output = text.split(marker, 1)[1]
                    documents, cursor = [], 0
                    decoder = json.JSONDecoder()
                    try:
                        while output[cursor:].strip():
                            cursor += len(output[cursor:]) - len(
                                output[cursor:].lstrip()
                            )
                            value, cursor = decoder.raw_decode(output, cursor)
                            documents.append({"type": "text", "text": _json(value)})
                        return {"content": documents} if documents else None
                    except ValueError:
                        pass
        return None


def _deliveries(value, depth=0):
    """Unwrap only explicit transport shapes, not arbitrary result/command keys."""
    if depth > 6 or not isinstance(value, dict):
        return
    if "output" in value and set(value) <= TRANSPORT_KEYS:
        child = value["output"]
        yield from _deliveries(
            _document(child) if isinstance(child, str) else child, depth + 1
        )
    elif isinstance(value.get("content"), list) and set(value) <= {
        "content",
        "isError",
        "is_error",
        "structuredContent",
        "_meta",
    }:
        for part in value["content"]:
            if isinstance(part, dict) and part.get("type") == "text":
                yield from _deliveries(_document(part.get("text")), depth + 1)
    else:
        yield value


def _collect(evidence):
    entities, notes = {}, {}
    parse_incomplete = False

    def note(category, field, text, eid, order):
        if isinstance(text, str) and text.strip():
            key = category, field, eid if field == "unstructured_excerpt" else None
            notes[key] = (
                {"field": field, "text": text[:180], "evidence_ids": [eid]},
                order,
            )

    def entity(category, value, eid, order, *, evaluation_status=False):
        identity = (
            "evaluation"
            if evaluation_status
            else (
                "task_id"
                if category == "tasks"
                else next(
                    (k for k in ("operation_id", "evaluation_id", "id") if k in value),
                    None,
                )
            )
        )
        if identity is None or value.get(identity) is None:
            return
        identifier = (
            value[identity] if evaluation_status else _short(value[identity], 160)
        )
        if (
            identifier is None
            or (not evaluation_status and not identifier)
            or isinstance(value[identity], str)
            and len(value[identity]) > 160
        ):
            return
        key = category, identity, identifier
        record = entities.setdefault(
            key, {identity: identifier, "_sources": {}, "_support": {}, "_order": order}
        )
        recorded = {identity: identifier}
        record["_sources"][identity] = eid
        for field in FIELDS:
            supplied = value.get(field)
            if supplied is None or field == "status" and not isinstance(supplied, str):
                continue
            content = _short(supplied)
            if content is not None and content.strip():
                if field in {"status", "role"} and len(str(supplied)) > 180:
                    continue
                label = (
                    field + "_excerpt"
                    if isinstance(supplied, str) and len(supplied) > 180
                    else field
                )
                record[label] = recorded[label] = content
                record["_sources"][label] = eid
        if evaluation_status:
            for field in ("cases_total", "cases_completed"):
                record[field] = recorded[field] = value[field]
                record["_sources"][field] = eid
        # This is explicitly decoded source data, not a synthesized completion
        # claim. Identity and its observed status appear together in the support.
        quote = "Decoded delivered record (untrusted data):\n" + _json(recorded)
        record["_support"].setdefault(eid, [])
        if quote not in record["_support"][eid]:
            record["_support"][eid].append(quote)
        record["_order"] = order

    def collect_notes(value, path, eid, order, depth=0):
        nonlocal parse_incomplete
        if depth > 10:
            parse_incomplete = True
            return
        if isinstance(value, list):
            for child in value[:200]:
                collect_notes(child, path, eid, order, depth + 1)
            if len(value) > 200:
                parse_incomplete = True
        elif isinstance(value, dict):
            if len(value) > 200:
                parse_incomplete = True
            for key, child in list(value.items())[:200]:
                if not isinstance(key, str):
                    continue
                child_path = (*path, key)
                label = ".".join(child_path)[-160:]
                category = (
                    "plan_notes"
                    if any(part in PLAN_KEYS for part in child_path)
                    else "priority_notes"
                    if any(p in PRIORITY_KEYS for p in child_path)
                    else "status_notes"
                    if key in STATUS_KEYS
                    or (key == "summary" and "previous_iteration_result" in path)
                    else None
                )
                if category:
                    content = _short(child)
                    if content is not None:
                        note(category, label, content, eid, order)
                # Strings under result/command/example are never decoded as
                # another document. Explicit plans stay quoted notes only.
                collect_notes(child, child_path, eid, order, depth + 1)

    for order, item in enumerate(evidence):
        text, eid = item["text"], item["id"]
        document = _document(text)
        deliveries = list(_deliveries(document))
        before = len(notes)
        for value in deliveries:
            if "event" in value:
                continue
            for category in ("tasks", "operations"):
                values = value.get(category)
                if isinstance(values, list):
                    for child in values[:200]:
                        if isinstance(child, dict) and "event" not in child:
                            entity(category, child, eid, order)
                    if len(values) > 200:
                        parse_incomplete = True
            if all(
                isinstance(value.get(k), str) and value[k].strip()
                for k in ("task_id", "role", "status")
            ):
                entity("tasks", value, eid, order)
            if (
                isinstance(value.get("status"), str)
                and value["status"].strip()
                and all(
                    type(value.get(k)) is int
                    for k in ("evaluation", "cases_total", "cases_completed")
                )
            ):
                entity("operations", value, eid, order, evaluation_status=True)
            collect_notes(value, (), eid, order)
        for line in text.splitlines():
            match = re.match(
                r"\s*(status[_ ]notes?|plan[_ ]notes?|next[_ ](?:action|step)|review[_ ]requirements?)\s*:\s*(.+)",
                line,
                re.IGNORECASE,
            )
            if match:
                category = (
                    "plan_notes"
                    if re.search(r"plan|next|review", match[1], re.IGNORECASE)
                    else "status_notes"
                )
                note(category, match[1], match[2], eid, order)
        if not deliveries and len(notes) == before:
            note("status_notes", "unstructured_excerpt", text, eid, order)
    candidates = []
    for (category, _, _), record in entities.items():
        sources = record.pop("_sources")
        order = record.pop("_order")
        record.setdefault("status", "unknown")
        record["evidence_ids"] = list(dict.fromkeys(sources.values()))
        candidates.append((category, record, order))
    candidates.extend(
        (category, record, order) for (category, _, _), (record, order) in notes.items()
    )
    return sorted(
        candidates,
        key=lambda item: (CATEGORIES.index(item[0]), -item[2], _json(item[1])),
    ), parse_incomplete


def _projection(selected, totals, parse_incomplete=False):
    result = {category: [] for category in CATEGORIES}
    for category, record, _ in selected:
        result[category].append({k: v for k, v in record.items() if k != "_support"})
    result["untrusted_evidence"] = True
    result["parse_incomplete"] = parse_incomplete
    result["task_state"] = "observations_only" if result["tasks"] else "unknown"
    result["current_plan"] = "unknown"
    result["critic_due"] = "unknown"
    result["coverage"] = {
        "task_records": len(result["tasks"]),
        "operation_records": len(result["operations"]),
        "quoted_notes": sum(len(result[k]) for k in CATEGORIES[2:]),
    }
    result["omitted_records"] = {
        key: totals[key] - len(result[key])
        for key in CATEGORIES
        if totals[key] > len(result[key])
    }
    return result


def _select(evidence, max_chars):
    candidates, incomplete = _collect(evidence)
    totals = Counter(category for category, _, _ in candidates)
    selected, chosen = [], set()

    def fits(candidate):
        return (
            len(_json(_projection([*selected, candidate], totals, incomplete)))
            <= max_chars
        )

    # Reserve one fitting observation per available category before filling by
    # the existing recency/priority order. This allocates space, not authority.
    # Reserved entries come first so packet-level trimming removes fill first.
    for category in CATEGORIES:
        for index, candidate in enumerate(candidates):
            if candidate[0] == category and fits(candidate):
                selected.append(candidate)
                chosen.add(index)
                break
    for index, candidate in enumerate(candidates):
        if index not in chosen and fits(candidate):
            selected.append(candidate)
    return selected, totals, incomplete


def project_state(evidence, max_chars=STATE_LIMIT):
    """Project whitelisted delivered records and verbatim, nonauthoritative notes."""
    validate_packet(
        {"goal": "prefix", "request": "observations", "state": {}, "evidence": evidence}
    )
    selected, totals, incomplete = _select(evidence, max_chars)
    result = _projection(selected, totals, incomplete)
    if len(_json(result)) > max_chars:
        raise ValueError("state budget cannot fit projection metadata")
    return result


def _support(record, eid, text):
    if "_support" in record:
        return "\n[…]\n".join(record["_support"][eid])
    value = record["text"]
    variants = [value, json.dumps(value, ensure_ascii=False)[1:-1]]
    for variant in variants:
        start = text.find(variant)
        if variant and start >= 0:
            return text[max(0, start - 35) : min(len(text), start + len(variant) + 35)]
    return None


def enrich_packet(packet_input, prefix_evidence=None, max_chars=12000):
    """Return a same-budget packet using only caller-supplied frozen evidence.

    Goal, request, history and base state are preserved. State facts retain their
    original evidence IDs and supporting decoded fields; notes retain verbatim
    excerpts. Whole records are omitted with counts if budgets require it.
    """
    validate_packet(packet_input)
    if type(max_chars) is not int or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    evidence = deepcopy(
        packet_input["evidence"] if prefix_evidence is None else prefix_evidence
    )
    validate_packet({**packet_input, "evidence": evidence})
    selected, totals, incomplete = _select(evidence, STATE_LIMIT)
    texts = {item["id"]: item["text"] for item in evidence}
    base = deepcopy(packet_input)
    base["evidence"] = []

    def assemble():
        packet = deepcopy(base)
        packet["state"]["router_state"] = _projection(selected, totals, incomplete)
        snippets = {}
        for _, record, _ in selected:
            for eid in record["evidence_ids"]:
                quote = _support(record, eid, texts[eid])
                if quote is None:
                    raise ValueError("state fact lacks a supporting source excerpt")
                snippets.setdefault(eid, [])
                if quote not in snippets[eid]:
                    snippets[eid].append(quote)
        packet["evidence"] = [
            {"id": item["id"], "text": "\n[…]\n".join(snippets[item["id"]])}
            for item in evidence
            if item["id"] in snippets
        ]
        return packet

    result = assemble()
    while len(_json(result)) > max_chars and selected:
        selected.pop()
        result = assemble()
    if len(_json(result)) > max_chars:
        raise ValueError("essential goal/request/base state cannot fit max_chars")
    chosen = {item["id"]: item for item in result["evidence"]}
    for item in reversed(evidence):
        if item["id"] in chosen:
            continue
        text = item["text"]
        candidate = {
            "id": item["id"],
            "text": text if len(text) <= 800 else text[:380] + "\n[…]\n" + text[-380:],
        }
        proposed = {**result, "evidence": [*result["evidence"], candidate]}
        if len(_json(proposed)) <= max_chars:
            result = proposed
            chosen[item["id"]] = candidate
    result["evidence"] = [
        chosen[item["id"]] for item in evidence if item["id"] in chosen
    ]
    validate_packet(result)
    return result
