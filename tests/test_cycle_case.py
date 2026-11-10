"""Deterministic validation of the explicitly synthetic cycle fixture."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks.experiment_cycle.case import (
    build_workspace,
    check,
    load_case,
    validate_output_directory,
)


EXAMPLE = Path(__file__).resolve().parents[1] / "benchmarks/experiment_cycle/example"


@pytest.fixture
def case():
    return load_case(EXAMPLE / "case.json")


def write_result(case, workspace, *, offset=1):
    inputs = json.loads((workspace / "inputs.json").read_text())
    result = {
        "version": 1,
        "observations": [
            {
                "id": row["id"],
                "control_prediction": row["x"],
                "candidate_prediction": row["x"] + offset,
            }
            for row in inputs["observations"]
        ],
    }
    (workspace / case["result_file"]).write_text(json.dumps(result))


def evaluation(**changes):
    return {
        "disposition": "supported",
        "metrics": {"control_mse": 1, "candidate_mse": 0, "delta_mse": -1},
        "limitations": "Four fixed SYNTHETIC rows; no historical or general benefit claim.",
        "evidence": ["result.json"],
        **changes,
    }


def frozen_copy(tmp_path):
    import shutil

    directory = tmp_path / "frozen"
    shutil.copytree(EXAMPLE, directory)
    return directory, json.loads((directory / "case.json").read_text())


def test_reference_passes_and_metrics_are_recomputed(case, tmp_path):
    workspace = build_workspace(case, tmp_path / "candidate")
    write_result(case, workspace)
    result = check(case, workspace, evaluation())
    assert result["status"] == "pass"
    assert result["metrics"] == {"control_mse": 1, "candidate_mse": 0, "delta_mse": -1}
    assert any("SYNTHETIC" in text for text in result["limitations"])
    assert not (workspace / "oracle.json").exists()
    assert case["task"].is_absolute()
    assert case["source"]["kind"] == "synthetic"


def test_real_cpu_reference_command(case, tmp_path):
    workspace = build_workspace(case, tmp_path / "candidate")
    (workspace / "experiment.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "rows = json.loads(Path('inputs.json').read_text())['observations']\n"
        "observations = [dict(id=r['id'], control_prediction=r['x'], "
        "candidate_prediction=r['x'] + 1) for r in rows]\n"
        "Path('result.json').write_text(json.dumps(dict(version=1, observations=observations)))\n"
    )
    command = [part.replace("{python}", sys.executable) for part in case["command"]]
    completed = subprocess.run(command, cwd=workspace, timeout=10, check=False)
    assert completed.returncode == 0
    assert check(case, workspace, evaluation())["status"] == "pass"


def test_wrong_candidate_fails_even_with_matching_claim(case, tmp_path):
    workspace = build_workspace(case, tmp_path / "candidate")
    write_result(case, workspace, offset=0)
    result = check(
        case,
        workspace,
        evaluation(
            disposition="contradicted",
            metrics={"control_mse": 1, "candidate_mse": 1, "delta_mse": 0},
        ),
    )
    assert result["status"] == "fail"
    assert (
        next(item for item in result["checks"] if item["name"] == "predictions")[
            "status"
        ]
        == "fail"
    )


@pytest.mark.parametrize(
    "claim",
    [
        evaluation(metrics={"control_mse": 1, "candidate_mse": 0, "delta_mse": 1}),
        evaluation(disposition="contradicted"),
    ],
)
def test_wrong_evaluation_fails(case, tmp_path, claim):
    workspace = build_workspace(case, tmp_path / "candidate")
    write_result(case, workspace)
    assert check(case, workspace, claim)["status"] == "fail"


@pytest.mark.parametrize(
    "claim",
    [
        None,
        {},
        evaluation(evidence=[]),
        evaluation(evidence=["inputs.json"]),
        evaluation(evidence=["../oracle.json"]),
        evaluation(metrics={"control_mse": True, "candidate_mse": 0, "delta_mse": -1}),
    ],
)
def test_missing_or_malformed_evaluation_is_not_evaluable(case, tmp_path, claim):
    workspace = build_workspace(case, tmp_path / "candidate")
    write_result(case, workspace)
    assert check(case, workspace, claim)["status"] == "not-evaluable"


def test_missing_raw_evidence_is_not_evaluable(case, tmp_path):
    workspace = build_workspace(case, tmp_path / "candidate")
    assert check(case, workspace, evaluation())["status"] == "not-evaluable"


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "nonfinite", "bool", "extra", "version"]
)
def test_invalid_observations_are_not_evaluable(case, tmp_path, mutation):
    workspace = build_workspace(case, tmp_path / "candidate")
    write_result(case, workspace)
    path = workspace / case["result_file"]
    raw = json.loads(path.read_text())
    if mutation == "missing":
        raw["observations"].pop()
    elif mutation == "duplicate":
        raw["observations"][1]["id"] = "a"
    elif mutation == "nonfinite":
        raw["observations"][0]["candidate_prediction"] = float("nan")
    elif mutation == "bool":
        raw["observations"][0]["candidate_prediction"] = True
    elif mutation == "extra":
        raw["observations"][0]["target"] = -1
    else:
        raw["version"] = True
    path.write_text(json.dumps(raw))
    assert check(case, workspace, evaluation())["status"] == "not-evaluable"


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("device", "cuda"),
        ("result_file", "../result.json"),
        ("workspace", "/tmp"),
        ("task", "../task.md"),
        ("oracle", "workspace/inputs.json"),
        ("source", {"kind": "invented", "reference": "unknown"}),
        ("limits", {"stage_seconds": 10, "execute_seconds": 0}),
        ("command", ["sh", "-c", "true"]),
        ("command", ["{python}", "-c", "pass"]),
        ("command", ["{python}", "../outside.py"]),
        ("command", ["{python}", "experiment.py", "{unsupported}"]),
    ],
)
def test_rejects_case_schema_and_path_escapes(tmp_path, field, value):
    directory, manifest = frozen_copy(tmp_path)
    manifest[field] = value
    (directory / "case.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        load_case(directory / "case.json")


def test_rejects_unknown_manifest_fields(tmp_path):
    directory, manifest = frozen_copy(tmp_path)
    manifest["unexpected"] = True
    (directory / "case.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        load_case(directory / "case.json")


@pytest.mark.parametrize(
    "target", ["case.json", "oracle.json", "workspace/experiment.py"]
)
def test_rejects_frozen_symlink_paths(tmp_path, target):
    directory, _ = frozen_copy(tmp_path)
    original = directory / target
    outside = tmp_path / "outside"
    original.rename(outside)
    original.symlink_to(outside)
    with pytest.raises(ValueError):
        load_case(directory / "case.json")


@pytest.mark.parametrize("name", ["private-link", ".git"])
def test_workspace_rejects_links_and_git_history(tmp_path, name):
    directory, _ = frozen_copy(tmp_path)
    if name == ".git":
        (directory / "workspace" / name).mkdir()
    else:
        (directory / "workspace" / name).symlink_to(directory / "oracle.json")
    case = load_case(directory / "case.json")
    with pytest.raises(ValueError):
        build_workspace(case, tmp_path / "candidate")
    assert not (tmp_path / "candidate").exists()


def test_output_and_frozen_case_must_be_disjoint(case):
    for destination in [case["root"], case["root"] / "attempt", case["root"].parent]:
        with pytest.raises(ValueError):
            validate_output_directory(case, destination)


def test_result_symlink_cannot_expose_oracle(case, tmp_path):
    workspace = build_workspace(case, tmp_path / "candidate")
    (workspace / case["result_file"]).symlink_to(case["oracle"])
    assert check(case, workspace, evaluation())["status"] == "not-evaluable"


def test_improvement_threshold_is_strict(tmp_path):
    directory, _ = frozen_copy(tmp_path)
    oracle_path = directory / "oracle.json"
    oracle = json.loads(oracle_path.read_text())
    oracle["minimum_improvement"] = 1
    oracle_path.write_text(json.dumps(oracle))
    case = load_case(directory / "case.json")
    workspace = build_workspace(case, tmp_path / "candidate")
    write_result(case, workspace)
    assert (
        check(case, workspace, evaluation(disposition="contradicted"))["status"]
        == "pass"
    )


def test_prose_evidence_is_not_silently_repaired(case, tmp_path):
    workspace = build_workspace(case, tmp_path / "candidate")
    write_result(case, workspace)
    malformed = evaluation(
        evidence=["result.json demonstrates the expected improvement."]
    )
    result = check(case, workspace, malformed)
    assert result["status"] == "not-evaluable"
    assert result["metrics"] == {"control_mse": 1, "candidate_mse": 0, "delta_mse": -1}
    assert check(case, workspace, evaluation())["status"] == "pass"


def test_native_evidence_schema_only_permits_artifact_paths(case):
    from benchmarks.experiment_cycle.runner import output_schema

    allowed = output_schema("evaluate", case)["properties"]["evidence"]["items"]["enum"]
    assert case["result_file"] in allowed
    assert ".cycle/execute.json" in allowed
    assert "result.json demonstrates the expected improvement." not in allowed
