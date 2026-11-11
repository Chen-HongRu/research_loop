"""Local checks of the development case and host-owned receipt contract."""

import json

import pytest

from benchmarks.experiment_cycle.cactus_case import check, load_case
from benchmarks.experiment_cycle.case import build_workspace, validate_output_directory


@pytest.fixture
def setup_case(tmp_path):
    root = tmp_path / "case"
    (root / "workspace").mkdir(parents=True)
    (root / "workspace/train.py").write_text("pass\n")
    (root / "task.md").write_text("Development task\n")
    manifest = {
        "version": 1,
        "case_type": "cactus-dev",
        "case_id": "cactus",
        "source": {
            "kind": "mle-bench-lite-development",
            "reference": "aerial-cactus-identification",
        },
        "task": "task.md",
        "workspace": "workspace",
        "command": ["{python}", "train.py"],
        "result_file": "metrics.json",
        "device": "cuda:0",
        "limits": {"stage_seconds": 10, "execute_seconds": 30},
        "remote": {
            "ssh_host": "example.invalid",
            "project_root": "/project",
            "data_root": "/data",
            "controller_root": "/controller",
            "python": "/python",
            "gpu_uuid": "GPU-example",
        },
    }
    (root / "case.json").write_text(json.dumps(manifest))
    case = load_case(root / "case.json")
    workspace = build_workspace(case, tmp_path / "attempt")
    (workspace / ".cycle").mkdir()
    return case, workspace, manifest


def evidence(workspace, baseline=0.6, candidate=0.8):
    receipt = {"exit_code": 0, "gpu_uuid": "GPU-example"}
    for name, score in (("baseline", baseline), ("candidate", candidate)):
        receipt[name] = {
            "exit_code": 0,
            "score": score,
            "operation_id": name + "-op",
            "metric": "roc_auc",
            "split": "dev",
        }
    metrics = {
        "baseline_auc": baseline,
        "candidate_auc": candidate,
        "delta_auc": candidate - baseline,
    }
    (workspace / ".cycle/execute.json").write_text(json.dumps(receipt))
    (workspace / "metrics.json").write_text(json.dumps(metrics))
    return receipt, {
        "disposition": "supported" if candidate > baseline else "contradicted",
        "metrics": metrics,
        "limitations": "Development split only",
        "evidence": ["metrics.json"],
    }


@pytest.mark.parametrize("baseline,candidate", [(0.6, 0.8), (0.8, 0.6), (0.7, 0.7)])
def test_receipts_and_signed_delta(setup_case, baseline, candidate):
    case, workspace, _ = setup_case
    _, evaluation = evidence(workspace, baseline, candidate)
    assert check(case, workspace, evaluation)["status"] == "pass"
    assert case["metric_names"] == ("baseline_auc", "candidate_auc", "delta_auc")


@pytest.mark.parametrize(
    "mutation",
    ["missing", "gpu", "operation", "metric", "split", "status", "nonfinite"],
)
def test_invalid_receipt_is_not_evaluable(setup_case, mutation):
    case, workspace, _ = setup_case
    receipt, evaluation = evidence(workspace)
    path = workspace / ".cycle/execute.json"
    if mutation == "missing":
        path.unlink()
    else:
        if mutation == "gpu":
            receipt["gpu_uuid"] = "other"
        elif mutation == "operation":
            receipt["baseline"].pop("operation_id")
        elif mutation == "metric":
            receipt["baseline"]["metric"] = "mse"
        elif mutation == "split":
            receipt["candidate"]["split"] = "test"
        elif mutation == "status":
            receipt["candidate"]["exit_code"] = 1
        else:
            receipt["candidate"]["score"] = float("nan")
        path.write_text(json.dumps(receipt))
    assert check(case, workspace, evaluation)["status"] == "not-evaluable"


def test_numeric_and_disposition_contradictions_fail(setup_case):
    case, workspace, _ = setup_case
    _, evaluation = evidence(workspace)
    evaluation["metrics"]["delta_auc"] = -0.2
    assert check(case, workspace, evaluation)["status"] == "fail"
    _, evaluation = evidence(workspace)
    evaluation["disposition"] = "contradicted"
    assert check(case, workspace, evaluation)["status"] == "fail"


@pytest.mark.parametrize(
    "references",
    [
        [],
        ["../case/task.md"],
        ["metrics.json", "missing"],
        ["metrics.json explains the result"],
    ],
)
def test_invalid_evidence_is_not_evaluable(setup_case, references):
    case, workspace, _ = setup_case
    _, evaluation = evidence(workspace)
    evaluation["evidence"] = references
    assert check(case, workspace, evaluation)["status"] == "not-evaluable"


def test_case_and_output_path_isolation(setup_case):
    case, workspace, manifest = setup_case
    with pytest.raises(ValueError):
        validate_output_directory(case, case["root"] / "attempt")
    manifest["workspace"] = "../attempt"
    (case["root"] / "case.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        load_case(case["root"] / "case.json")
    _, evaluation = evidence(workspace)
    (workspace / "metrics.json").unlink()
    (workspace / "metrics.json").symlink_to(case["task"])
    assert check(case, workspace, evaluation)["status"] == "not-evaluable"


def test_explicit_intervention_requires_verified_receipt(setup_case):
    case, workspace, _ = setup_case
    case["intervention_id"] = 1
    receipt, evaluation = evidence(workspace)
    assert check(case, workspace, evaluation)["status"] == "not-evaluable"
    receipt.update(intervention_id=1, intervention_verified=True)
    (workspace / ".cycle/execute.json").write_text(json.dumps(receipt))
    assert check(case, workspace, evaluation)["status"] == "pass"
    receipt["intervention_id"] = 2
    (workspace / ".cycle/execute.json").write_text(json.dumps(receipt))
    assert check(case, workspace, evaluation)["status"] == "not-evaluable"
