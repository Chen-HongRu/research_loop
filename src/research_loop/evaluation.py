"""Deterministic Frontier views from reviewed, version-bound evaluation records.

The caller supplies published records and controls feedback visibility. This module
does not read artifacts, aggregate repeated measurements, or establish their truth.
"""

from __future__ import annotations

from copy import deepcopy
import math


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _finite(value: object) -> bool:
    # bool is a JSON boolean, not a measurement. Python integers are finite.
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _keys(value: object, names: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != names:
        raise ValueError(f"{label} must contain exactly {sorted(names)}")


def validate_evaluation(value: dict) -> dict:
    """Validate a fixed evaluation definition, returning the original dictionary."""
    _keys(
        value,
        {
            "id",
            "comparison_scope",
            "protocol",
            "data_role",
            "allow_feedback",
            "objectives",
            "constraints",
        },
        "evaluation",
    )
    for name in ("id", "comparison_scope", "protocol"):
        if not _text(value[name]):
            raise ValueError(f"evaluation: invalid {name}")
    if value["data_role"] not in ("development", "final"):
        raise ValueError("evaluation: invalid data_role")
    if type(value["allow_feedback"]) is not bool:
        raise ValueError("evaluation: allow_feedback must be boolean")
    if value["data_role"] == "final" and value["allow_feedback"]:
        raise ValueError("final evaluation cannot allow feedback")
    if not isinstance(value["objectives"], list) or not value["objectives"]:
        raise ValueError("evaluation: objectives must be a nonempty list")
    if not isinstance(value["constraints"], list):
        raise ValueError("evaluation: constraints must be a list")
    names = set()
    for kind in ("objectives", "constraints"):
        for metric in value[kind]:
            fields = (
                {"name", "direction", "unit"}
                if kind == "objectives"
                else {"name", "operator", "value", "unit"}
            )
            _keys(metric, fields, kind)
            if not _text(metric["name"]) or not _text(metric["unit"]):
                raise ValueError(f"{kind}: name and unit must be nonempty strings")
            if metric["name"] in names:
                raise ValueError(f"duplicate metric name: {metric['name']}")
            names.add(metric["name"])
            if kind == "objectives":
                if metric["direction"] not in ("maximize", "minimize"):
                    raise ValueError("objectives: invalid direction")
            elif metric["operator"] not in ("le", "ge") or not _finite(metric["value"]):
                raise ValueError("constraints: invalid operator or finite bound")
    return value


_CANDIDATE_FIELDS = {
    "id",
    "experiment_id",
    "artifact",
    "evaluation_id",
    "comparison_scope",
    "metrics",
    "evidence",
    "verified",
    "valid",
}


def _exclusion(definition: dict, candidate: dict) -> str | None:
    missing = _CANDIDATE_FIELDS - candidate.keys()
    if missing:
        return "missing_fields:" + ",".join(sorted(missing))
    if set(candidate) != _CANDIDATE_FIELDS:
        return "invalid_fields"
    if candidate["evaluation_id"] != definition["id"]:
        return "evaluation_mismatch"
    if candidate["comparison_scope"] != definition["comparison_scope"]:
        return "comparison_scope_mismatch"
    if type(candidate["verified"]) is not bool or type(candidate["valid"]) is not bool:
        return "invalid_status"
    if not candidate["verified"]:
        return "unverified"
    if not candidate["valid"]:
        return "invalid"
    if not _text(candidate["experiment_id"]):
        return "missing_experiment"
    artifact = candidate["artifact"]
    if (
        not isinstance(artifact, dict)
        or set(artifact) != {"path", "version"}
        or not all(_text(artifact[name]) for name in ("path", "version"))
    ):
        return "missing_artifact_version"
    evidence = candidate["evidence"]
    if not isinstance(evidence, list) or not evidence or not all(map(_text, evidence)):
        return "missing_evidence"
    metrics = candidate["metrics"]
    if not isinstance(metrics, dict) or not all(map(_text, metrics)):
        return "invalid_metrics"
    required = {
        metric["name"]
        for kind in ("objectives", "constraints")
        for metric in definition[kind]
    }
    missing = required - metrics.keys()
    if missing:
        return "missing_metrics:" + ",".join(sorted(missing))
    nonfinite = [name for name, result in metrics.items() if not _finite(result)]
    if nonfinite:
        return "nonfinite_metrics:" + ",".join(sorted(nonfinite))
    infeasible = []
    for constraint in definition["constraints"]:
        result = metrics[constraint["name"]]
        bound = constraint["value"]
        if (constraint["operator"] == "le" and result > bound) or (
            constraint["operator"] == "ge" and result < bound
        ):
            infeasible.append(constraint["name"])
    if infeasible:
        return "infeasible:" + ",".join(sorted(infeasible))
    return None


def _dominates(left: dict, right: dict, objectives: list[dict]) -> bool:
    strictly_better = False
    for objective in objectives:
        a = left["metrics"][objective["name"]]
        b = right["metrics"][objective["name"]]
        if objective["direction"] == "maximize":
            if a < b:
                return False
            strictly_better |= a > b
        else:
            if a > b:
                return False
            strictly_better |= a < b
    return strictly_better


def build_frontier(definition: dict, candidates: list[dict]) -> dict:
    """Rebuild a Pareto view from all records, including previously dominated ones.

    Records with missing eligibility evidence are excluded explicitly. Missing or
    duplicate identities are errors because an exclusion cannot unambiguously name
    them. All returned records are copies; inputs and prior views remain unchanged.
    """
    validate_evaluation(definition)
    if not isinstance(candidates, list):
        raise ValueError("candidates must be a list")
    seen = set()
    for candidate in candidates:
        if not isinstance(candidate, dict) or not _text(candidate.get("id")):
            raise ValueError("candidate must have a nonempty id")
        if candidate["id"] in seen:
            raise ValueError(f"duplicate candidate id: {candidate['id']}")
        seen.add(candidate["id"])
    eligible, excluded = [], []
    for candidate in sorted(candidates, key=lambda record: record["id"]):
        reason = _exclusion(definition, candidate)
        if reason:
            excluded.append({"id": candidate["id"], "reason": reason})
        else:
            eligible.append(candidate)
    frontier = []
    for candidate in eligible:
        if any(
            _dominates(other, candidate, definition["objectives"]) for other in eligible
        ):
            excluded.append({"id": candidate["id"], "reason": "dominated"})
        else:
            frontier.append(deepcopy(candidate))
    return {
        "evaluation_id": definition["id"],
        "comparison_scope": definition["comparison_scope"],
        "candidate_ids": [candidate["id"] for candidate in frontier],
        "excluded": sorted(excluded, key=lambda record: record["id"]),
        "candidates": frontier,
    }
