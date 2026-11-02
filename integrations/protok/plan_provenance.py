"""Read the accepted scientific Plan behind an explicit execution continuation."""

import json
import re
from pathlib import Path

from research_loop.harness import completed_response


def _json(root, path):
    try:
        path.resolve().relative_to(root.resolve())
        if path.is_symlink() or path.stat().st_size > 256_000:
            return None
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def accepted_plan_response(root, ref, state, *, evidence_paths=None):
    """Verify an accepted Plan; canonical fields come from controller selection."""
    root = Path(root)
    if not isinstance(ref, str) or not re.fullmatch(
        r"iterations/[0-9]{4,}/plan/attempt-[0-9]+/(?:canonical-)?response.json", ref
    ):
        return None
    calls = state.get("calls", {})
    accepted = state.get("accepted_calls", {})
    if (
        not isinstance(calls, dict)
        or not isinstance(accepted, dict)
        or not any(
            key.startswith("plan-")
            and isinstance(call, str)
            and ref in {call + "/response.json", call + "/canonical-response.json"}
            and accepted.get(key)
            for key, call in calls.items()
        )
    ):
        return None
    path = root / "study" / ref
    paths = list(
        dict.fromkeys(
            [
                path,
                path.parent / "response.json",
                path.parent / "completed.json",
                path.parent / "process.json",
            ]
        )
    )
    for receipt in paths:
        if evidence_paths is not None:
            evidence_paths.append(str(receipt.relative_to(root)))
        if receipt.exists() and _json(root, receipt) is None:
            return None
    raw = completed_response(path.parent)
    if not isinstance(raw, dict) or _json(root, path.parent / "response.json") != raw:
        return None
    if path.name == "response.json":
        return raw
    selected = state.get("selected") or {}
    proposal = selected.get("proposal") if isinstance(selected, dict) else None
    if (
        not isinstance(proposal, dict)
        or "base_experiment" not in proposal
        or "base_experiment" in raw
    ):
        return None
    canonical = {**raw, "base_experiment": proposal["base_experiment"]}
    return canonical if _json(root, path) == canonical else None


def execution_plan(root, identity, *, evidence_paths=None):
    """Resolve only recorded links; never infer a continuation from equal code."""
    root = Path(root)
    if not re.fullmatch(r"[0-9]{4,}", str(identity)):
        return None
    evidence = evidence_paths if evidence_paths is not None else []

    def load(path):
        evidence.append(str(path.relative_to(root)))
        return _json(root, path)

    def context(number, *, target=False):
        folder = root / "study/iterations" / number
        meta = load(folder / "iteration.json")
        if not meta or meta.get("id") != number:
            return None
        publication_path = folder / "publication.json"
        if publication_path.exists():
            publication = load(publication_path)
            if (
                not publication
                or meta.get("status") != "reviewed"
                or publication.get("metadata") != meta
                or not isinstance(publication.get("state_after"), dict)
            ):
                return None
            return meta, publication["state_after"]
        if target:
            state = load(root / "study/state.json")
            if state and state.get("iteration") == int(number):
                return meta, state
        return None

    def link(number, meta, state):
        marker = meta.get("continuation")
        if isinstance(marker, dict):
            if (
                state.get("plan_ref") != marker.get("plan_ref")
                or not isinstance(marker.get("directive"), str)
                or not isinstance(marker.get("authorization"), str)
                or not marker["authorization"].strip()
            ):
                return None
            return marker, False
        marker = meta.get("operator_resume")
        if not isinstance(marker, dict) or marker.get("target_iteration") != number:
            return None
        directory = root / "study/iterations" / number / "operator-plan"
        receipt = load(directory / "operator.json")
        output = load(directory / "response.json")
        selected = state.get("selected") or {}
        if (
            state.get("plan_ref") != f"iterations/{number}/operator-plan/response.json"
            or not isinstance(selected, dict)
            or selected.get("source") != "operator"
            or not receipt
            or receipt.get("source") != "operator"
            or not isinstance(receipt.get("authorization"), str)
            or not receipt["authorization"].strip()
            or any(receipt.get(key) != value for key, value in marker.items())
            or not output
            or output.get("decision") != "investigate"
            or output.get("base_experiment") != marker.get("source_iteration")
            or not isinstance(output.get("contract"), dict)
        ):
            return None
        return {
            **marker,
            "authorization": receipt["authorization"],
            "directive": output.get("markdown", ""),
        }, True

    target = context(identity, target=True)
    if target is None:
        return None
    number, (meta, state) = identity, target
    first = None
    claimed_refs = []
    while meta.get("continuation") or meta.get("operator_resume"):
        resolved = link(number, meta, state)
        if resolved is None:
            return None
        marker, legacy = resolved
        parent = marker.get("source_iteration")
        if (
            not isinstance(parent, str)
            or not re.fullmatch(r"[0-9]{4,}", parent)
            or not 1 <= int(parent) < int(number)
        ):
            return None
        if first is None:
            first = {**marker, "legacy": legacy}
        if not legacy:
            claimed_refs.append(marker["plan_ref"])
        source = context(parent)
        if source is None:
            return None
        meta, state = source
        if meta.get("workspace_ref") != marker.get("source_workspace_ref"):
            return None
        number = parent
    if first is None:
        return None
    plan_ref = state.get("plan_ref")
    calls = state.get("calls", {})
    if (
        not isinstance(plan_ref, str)
        or not re.fullmatch(
            rf"iterations/{re.escape(number)}/plan/attempt-[0-9]+/(?:canonical-)?response.json",
            plan_ref,
        )
        or any(ref != plan_ref for ref in claimed_refs)
        or not isinstance(calls, dict)
        or not any(
            key.startswith("plan-")
            and isinstance(call, str)
            and plan_ref in {call + "/response.json", call + "/canonical-response.json"}
            and state.get("accepted_calls", {}).get(key)
            for key, call in calls.items()
        )
    ):
        return None
    path = root / "study" / plan_ref
    for receipt in (path, path.parent / "completed.json", path.parent / "process.json"):
        value = load(receipt)
        if value is None and receipt.exists():
            return None
    output = accepted_plan_response(root, plan_ref, state, evidence_paths=evidence)
    if (
        not isinstance(output, dict)
        or output.get("decision") != "investigate"
        or not isinstance(output.get("contract"), dict)
    ):
        return None
    return {
        "kind": "execution_continuation",
        "source_iteration": first["source_iteration"],
        "target_iteration": identity,
        "plan_iteration": number,
        "plan_ref": "study/" + plan_ref,
        "output": output,
        "output_owner": "controller"
        if path.name == "canonical-response.json"
        else "agent",
        "authorization": first["authorization"],
        "directive": first["directive"],
        "legacy": first["legacy"],
        "evidence_paths": list(dict.fromkeys(evidence)),
    }
