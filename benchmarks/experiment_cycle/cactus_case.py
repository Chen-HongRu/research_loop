"""Development-only Cactus AUC case backed by remote execution receipts."""

from __future__ import annotations

import math
from pathlib import Path, PurePosixPath

from .case import MAX_JSON_BYTES, _json, _keys, _number, _relative, _text, _within

METRICS = ("baseline_auc", "candidate_auc", "delta_auc")


def load_case(path: str | Path) -> dict:
    """Load only small case metadata and paths; do not inspect training data."""
    manifest = Path(path).absolute()
    if any(part.is_symlink() for part in (manifest, *manifest.parents)):
        raise ValueError("manifest must not traverse symlinks")
    manifest = manifest.resolve(strict=True)
    root = manifest.parent
    case = _keys(
        _json(manifest),
        {
            "version",
            "case_type",
            "case_id",
            "source",
            "task",
            "workspace",
            "command",
            "result_file",
            "device",
            "limits",
            "remote",
        },
        "case",
    ).copy()
    if (
        type(case["version"]) is not int
        or case["version"] != 1
        or case["case_type"] != "cactus-dev"
    ):
        raise ValueError("unsupported case version or type")
    _text(case["case_id"], "case_id")
    source = _keys(case["source"], {"kind", "reference"}, "source")
    if source[
        "kind"
    ] != "mle-bench-lite-development" or "aerial-cactus-identification" not in _text(
        source["reference"], "source.reference"
    ):
        raise ValueError(
            "source must identify the aerial-cactus-identification development task"
        )
    if case["device"] != "cuda:0" or case["result_file"] != "metrics.json":
        raise ValueError("Cactus case requires cuda:0 and metrics.json")
    limits = _keys(case["limits"], {"stage_seconds", "execute_seconds"}, "limits")
    for name, value in limits.items():
        _number(value, name, positive=True)
    remote = _keys(
        case["remote"],
        {
            "ssh_host",
            "project_root",
            "data_root",
            "controller_root",
            "python",
            "gpu_uuid",
        },
        "remote",
    )
    for name, value in remote.items():
        _text(value, f"remote.{name}")
    for name in ("project_root", "data_root", "controller_root", "python"):
        p = PurePosixPath(remote[name])
        if not p.is_absolute() or ".." in p.parts:
            raise ValueError(f"remote.{name} must be an absolute path without '..'")
    command = case["command"]
    if (
        not isinstance(command, list)
        or len(command) < 2
        or command[0] != "{python}"
        or command[1] != "train.py"
    ):
        raise ValueError("command must execute train.py via {python}")
    for argument in command:
        _text(argument, "command argument")
        if "{" in argument.replace("{python}", "") or "}" in argument.replace(
            "{python}", ""
        ):
            raise ValueError("only the {python} command placeholder is supported")
    for name in ("workspace", "task"):
        case[name] = _within(root, case[name], name)
    if (
        not case["workspace"].is_dir()
        or not _within(case["workspace"], "train.py", "command script").is_file()
    ):
        raise ValueError("workspace must contain train.py")
    if not case["task"].is_file() or case["task"].stat().st_size > MAX_JSON_BYTES:
        raise ValueError("task must be a small text file")
    case["task_text"] = case["task"].read_text(encoding="utf-8")
    case["root"] = root
    case["metric_names"] = METRICS
    return case


def check(case: dict, workspace: str | Path, evaluation: dict | None) -> dict:
    """Compare claimed AUCs with trusted host-owned execution receipts."""
    checks = []
    limitations = [
        "Aerial Cactus Identification uses a custom development split, not the full official competition evaluation.",
        "The check relies on remote control and execution receipts; it does not establish method validity or general benefit.",
    ]

    def result(status: str, detail: str, metrics: dict | None = None) -> dict:
        checks.append(
            {
                "name": "cactus_receipt_and_evaluation",
                "status": status,
                "detail": detail,
            }
        )
        return {
            "status": status,
            "checks": checks,
            "limitations": limitations,
            "metrics": metrics or {},
        }

    try:
        workspace = Path(workspace).resolve(strict=True)
        receipt = _json(_within(workspace, ".cycle/execute.json", "execution receipt"))
        raw = _keys(
            _json(_within(workspace, "metrics.json", "metrics")),
            set(METRICS),
            "metrics",
        )
        if (
            type(receipt.get("exit_code")) is not int
            or receipt["exit_code"] != 0
            or receipt.get("gpu_uuid") != case["remote"]["gpu_uuid"]
        ):
            raise ValueError("missing or unsuccessful execution or GPU receipt")
        scores = []
        for name in ("baseline", "candidate"):
            item = receipt.get(name)
            if (
                not isinstance(item, dict)
                or type(item.get("exit_code")) is not int
                or item["exit_code"] != 0
                or item.get("metric") != "roc_auc"
                or item.get("split") != "dev"
            ):
                raise ValueError(f"invalid {name} execution receipt")
            _text(item.get("operation_id"), f"{name} operation_id")
            score = _number(item.get("score"), f"{name} score")
            if not 0 <= score <= 1:
                raise ValueError(f"{name} score outside [0, 1]")
            scores.append(score)
        metrics = dict(
            zip(METRICS, (scores[0], scores[1], scores[1] - scores[0]), strict=True)
        )
        evaluation = _keys(
            evaluation,
            {"disposition", "metrics", "limitations", "evidence"},
            "evaluation",
        )
        if evaluation["disposition"] not in (
            "supported",
            "contradicted",
            "insufficient_evidence",
        ):
            raise ValueError("unsupported evaluation disposition")
        supplied = _keys(evaluation["metrics"], set(METRICS), "evaluation.metrics")
        _text(evaluation["limitations"], "evaluation.limitations")
        evidence = evaluation["evidence"]
        if not isinstance(evidence, list) or "metrics.json" not in evidence:
            raise ValueError("evaluation must cite metrics.json")
        for reference in evidence:
            _relative(reference, "evaluation evidence")
            if not _within(workspace, reference, "evaluation evidence").is_file():
                raise ValueError("evaluation evidence must reference files")
        for name in METRICS:
            _number(raw[name], name)
            _number(supplied[name], name)
    except (OSError, ValueError, TypeError) as error:
        return result("not-evaluable", str(error))

    if any(
        not math.isclose(values[name], metrics[name], rel_tol=1e-9, abs_tol=1e-12)
        for values in (raw, supplied)
        for name in METRICS
    ):
        return result("fail", "Metrics disagree with execution receipts", metrics)
    expected = "supported" if metrics["delta_auc"] > 0 else "contradicted"
    if evaluation["disposition"] != expected:
        return result("fail", f"Receipt requires {expected}", metrics)
    return result("pass", "AUCs and disposition agree with execution receipts", metrics)
