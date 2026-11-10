"""Frozen, bounded numeric cases for the experiment-cycle development benchmark.

This checker verifies a deliberately narrow paired-prediction experiment. It is
not a general scientific validity checker or an isolation boundary for code.
"""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path
from typing import Any


MAX_JSON_BYTES = 1_048_576
MAX_OBSERVATIONS = 256
METRICS = {"control_mse", "candidate_mse", "delta_mse"}


def _keys(value: Any, expected: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} must contain exactly {sorted(expected)}")
    return value


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    try:
        finite = math.isfinite(value) if isinstance(value, (float, int)) else False
    except OverflowError:
        finite = False
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not finite
        or (positive and value <= 0)
    ):
        raise ValueError(
            f"{label} must be a finite {'positive ' if positive else ''}number"
        )
    return float(value)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    return value


def _relative(value: Any, label: str) -> Path:
    path = Path(_text(value, label))
    if path.is_absolute() or ".." in path.parts or path == Path("."):
        raise ValueError(f"{label} must be a nonempty relative path without '..'")
    return path


def _within(root: Path, value: Any, label: str, *, exists: bool = True) -> Path:
    relative = _relative(value, label)
    path = root
    for part in relative.parts:
        path /= part
        if path.is_symlink():
            raise ValueError(f"{label} must not traverse symlinks")
    resolved = path.resolve(strict=exists)
    if not resolved.is_relative_to(root):
        raise ValueError(f"{label} escapes its root")
    return resolved


def _json(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError("JSON evidence must be a regular file no larger than 1 MiB")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON evidence must be an object")
    return value


def _observations(value: Any, *, oracle: bool) -> list[dict]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_OBSERVATIONS:
        raise ValueError(f"observations must contain 1 to {MAX_OBSERVATIONS} rows")
    keys = {"id", "control_prediction", "candidate_prediction"}
    if oracle:
        keys.add("target")
    seen = set()
    for row in value:
        _keys(row, keys, "observation")
        identity = _text(row["id"], "observation id")
        if identity in seen:
            raise ValueError("observation ids must be unique")
        seen.add(identity)
        for name in keys - {"id"}:
            _number(row[name], name)
    return value


def _load_oracle(path: Path) -> dict:
    oracle = _keys(
        _json(path),
        {
            "version",
            "observations",
            "abs_tolerance",
            "rel_tolerance",
            "minimum_improvement",
        },
        "oracle",
    )
    if type(oracle["version"]) is not int or oracle["version"] != 1:
        raise ValueError("unsupported oracle version")
    _observations(oracle["observations"], oracle=True)
    for name in ("abs_tolerance", "rel_tolerance", "minimum_improvement"):
        if _number(oracle[name], name) < 0:
            raise ValueError(f"{name} must be nonnegative")
    return oracle


def load_case(path: str | Path) -> dict:
    """Read small frozen metadata; do not scan or hash data/checkpoint contents."""
    manifest = Path(path).absolute()
    if any(part.is_symlink() for part in (manifest, *manifest.parents)):
        raise ValueError("manifest must not traverse symlinks")
    manifest = manifest.resolve(strict=True)
    root = manifest.parent
    case = _keys(
        _json(manifest),
        {
            "version",
            "case_id",
            "source",
            "task",
            "workspace",
            "command",
            "result_file",
            "device",
            "limits",
            "oracle",
        },
        "case",
    ).copy()
    if type(case["version"]) is not int or case["version"] != 1:
        raise ValueError("unsupported case version")
    _text(case["case_id"], "case_id")
    source = _keys(case["source"], {"kind", "reference"}, "source")
    if not isinstance(source["kind"], str) or source["kind"] not in {
        "synthetic",
        "historical",
    }:
        raise ValueError("source.kind must be synthetic or historical")
    _text(source["reference"], "source.reference")
    if case["device"] != "cpu":
        raise ValueError("only cpu cases are supported")
    limits = _keys(case["limits"], {"stage_seconds", "execute_seconds"}, "limits")
    for name, value in limits.items():
        _number(value, name, positive=True)
    command = case["command"]
    if not isinstance(command, list) or not command:
        raise ValueError("command must be a nonempty argv list")
    for argument in command:
        _text(argument, "command argument")
        if "{" in argument.replace("{python}", "") or "}" in argument.replace(
            "{python}", ""
        ):
            raise ValueError("only the {python} command placeholder is supported")
    if len(command) < 2 or command[0] != "{python}" or command[1].startswith("-"):
        raise ValueError("command must execute a workspace Python script via {python}")
    _relative(case["result_file"], "result_file")
    for name in ("workspace", "task", "oracle"):
        case[name] = _within(root, case[name], name)
    if not case["workspace"].is_dir():
        raise ValueError("workspace must be a directory")
    script = _within(case["workspace"], command[1], "command script")
    if not script.is_file() or script.suffix != ".py":
        raise ValueError("command script must be a workspace .py file")
    if case["oracle"].is_relative_to(case["workspace"]):
        raise ValueError("oracle must be outside the starting workspace")
    if not case["task"].is_file() or case["task"].stat().st_size > MAX_JSON_BYTES:
        raise ValueError("task must be a small text file")
    case["task_text"] = case["task"].read_text(encoding="utf-8")
    case["root"] = root
    _load_oracle(case["oracle"])
    return case


def validate_output_directory(case: dict, destination: str | Path) -> Path:
    """Keep all attempt outputs disjoint from the frozen case directory."""
    destination = Path(destination).absolute()
    for path in (destination, *destination.parents):
        if path.is_symlink():
            raise ValueError("output directory must not traverse symlinks")
    destination = destination.resolve()
    root = case["root"]
    if destination.is_relative_to(root) or root.is_relative_to(destination):
        raise ValueError("output directory and frozen case must be disjoint")
    return destination


def build_workspace(case: dict, destination: str | Path) -> Path:
    """Copy only the declared starting tree, rejecting links and Git history."""
    destination = validate_output_directory(case, destination)
    source = case["workspace"]
    for path in source.rglob("*"):
        if path.is_symlink() or path.name == ".git":
            raise ValueError("starting workspace must not contain symlinks or .git")
        if not path.is_file() and not path.is_dir():
            raise ValueError("starting workspace contains a special file")
    shutil.copytree(source, destination)
    return destination


def check(case: dict, workspace: str | Path, evaluation: dict | None) -> dict:
    """Recompute bounded paired metrics and check the restricted conclusion."""
    checks: list[dict] = []
    limitations = [
        "Checks cover only the frozen paired numeric case, not general research benefit.",
        "Raw observations are candidate-produced; this is not proof of computation provenance.",
    ]
    if case["source"]["kind"] == "synthetic":
        limitations.append(
            "SYNTHETIC development fixture; not historical or held-out evidence."
        )

    def record(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    def result(metrics: dict | None = None) -> dict:
        statuses = {item["status"] for item in checks}
        status = (
            "fail"
            if "fail" in statuses
            else ("not-evaluable" if "not-evaluable" in statuses else "pass")
        )
        return {
            "status": status,
            "checks": checks,
            "limitations": limitations,
            "metrics": metrics or {},
        }

    try:
        oracle = _load_oracle(case["oracle"])
        workspace = Path(workspace).resolve(strict=True)
        raw = _keys(
            _json(_within(workspace, case["result_file"], "result_file")),
            {"version", "observations"},
            "result",
        )
        if type(raw["version"]) is not int or raw["version"] != 1:
            raise ValueError("unsupported result version")
        rows = _observations(raw["observations"], oracle=False)
    except (OSError, ValueError, TypeError) as error:
        record("raw_evidence", "not-evaluable", str(error))
        return result()

    expected = oracle["observations"]
    if [row["id"] for row in rows] != [row["id"] for row in expected]:
        record(
            "matched_observations",
            "not-evaluable",
            "Missing, reordered or unmatched observation ids",
        )
        return result()
    record("matched_observations", "pass", f"{len(rows)} frozen paired observations")

    def close(left: float, right: float) -> bool:
        return math.isclose(
            left,
            right,
            abs_tol=oracle["abs_tolerance"],
            rel_tol=oracle["rel_tolerance"],
        )

    correct = all(
        close(row[name], reference[name])
        for row, reference in zip(rows, expected, strict=True)
        for name in ("control_prediction", "candidate_prediction")
    )
    record(
        "predictions",
        "pass" if correct else "fail",
        "Predictions compared with frozen oracle",
    )
    try:
        control = sum(
            (row["control_prediction"] - ref["target"]) ** 2
            for row, ref in zip(rows, expected, strict=True)
        ) / len(rows)
        candidate = sum(
            (row["candidate_prediction"] - ref["target"]) ** 2
            for row, ref in zip(rows, expected, strict=True)
        ) / len(rows)
        metrics = {
            "control_mse": control,
            "candidate_mse": candidate,
            "delta_mse": candidate - control,
        }
        if not all(math.isfinite(value) for value in metrics.values()):
            raise ValueError("recomputed metrics must be finite")
    except (OverflowError, ValueError) as error:
        record("metrics", "fail", str(error))
        return result()

    try:
        evaluation = _keys(
            evaluation,
            {"disposition", "metrics", "limitations", "evidence"},
            "evaluation",
        )
        if not isinstance(evaluation["disposition"], str) or evaluation[
            "disposition"
        ] not in {"supported", "contradicted", "insufficient_evidence"}:
            raise ValueError("unsupported evaluation disposition")
        supplied = _keys(evaluation["metrics"], METRICS, "evaluation.metrics")
        for name in METRICS:
            _number(supplied[name], name)
        _text(evaluation["limitations"], "evaluation.limitations")
        evidence = evaluation["evidence"]
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("evaluation.evidence must be a nonempty list")
        for reference in evidence:
            if not _within(workspace, reference, "evaluation evidence").is_file():
                raise ValueError("evaluation evidence must reference files")
        if case["result_file"] not in evidence:
            raise ValueError("evaluation must cite result_file")
    except (OSError, ValueError, TypeError) as error:
        record("evaluation_evidence", "not-evaluable", str(error))
        return result(metrics)

    metric_match = all(close(supplied[name], metrics[name]) for name in METRICS)
    record(
        "evaluation_metrics",
        "pass" if metric_match else "fail",
        "Metrics independently recomputed from predictions and frozen targets",
    )
    improvement = control - candidate
    disposition = (
        "supported" if improvement > oracle["minimum_improvement"] else "contradicted"
    )
    record(
        "evaluation_disposition",
        "pass" if evaluation["disposition"] == disposition else "fail",
        f"Frozen rule requires {disposition}",
    )
    return result(metrics)
