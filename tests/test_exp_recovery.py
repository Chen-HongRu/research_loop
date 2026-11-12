"""Recovery uses published receipts, never reruns or rereads scientific inputs."""

from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from research_loop.group_programs.exp import EVALUATE_SCHEMA
from research_loop.group_programs.exp_recovery import (
    _strict_evaluation,
    build_recovery_bundle,
)
from research_loop.state import write_json


@pytest.fixture
def settled_exp(tmp_path):
    root = tmp_path / "group-runs" / "synthetic-exp"
    (root / "work").mkdir(parents=True)
    canonical = tmp_path / "private-input.json"
    canonical.write_text("x" * 69_120)
    raw = {
        "summary": "Independent calculation matched the frozen result.",
        "help": "",
        "artifacts": [],
        "command": "",
        "claim": "Observed 42 in this bounded synthetic experiment.",
        "limitations": ["Synthetic evidence only."],
        "evidence": ["metrics.json", "report.md", str(canonical)],
    }
    original = {**raw, "help": "Host rejected evidence filenames."}
    later = {
        **raw,
        "summary": "See the original independent calculation.",
        "claim": "Prior assessment retained.",
        "evidence": ["metrics.json"],
    }
    receipt = {"exit_code": 0, "running": False, "log_path": "synthetic-command.log"}
    files = {
        "metrics.json": {"answer": 42},
        "report.md": "Observed 42. Synthetic only.\n",
        "execution.json": {**receipt, "backend": "local"},
        "command-001.log": "",
        "evaluate.json": later,
        "scientific-plan.md": "Frozen synthetic contract.\n",
        "audit.py": "# Synthetic source; must not execute.\n",
        "plan.json": {"plan_artifact": "scientific-plan.md"},
        "prepare.json": {"result_file": "metrics.json"},
        "report.json": {"summary": "Completed synthetic experiment."},
    }
    for number in (1, 2):
        mapping = {}
        for name, value in files.items():
            path = root / "exports" / f"{number:06d}" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if name == "evaluate.json" and number == 1:
                value = original
            if isinstance(value, dict):
                write_json(path, value)
            else:
                path.write_text(value)
            mapping[name] = str(path)
        metadata = root / "rounds" / f"{number:06d}"
        metadata.mkdir(parents=True)
        write_json(metadata / "artifacts.json", mapping)
        write_json(metadata / "output.json", {"artifacts": list(mapping.values())})
        write_json(metadata / "input.json", {"input_refs": [str(canonical)]})
    write_json(
        root / "run.json",
        {
            "run_id": root.name,
            "group_id": "exp",
            "version": "synthetic-v1",
            "status": "closed",
            "round": 2,
            "input_refs": [str(canonical)],
        },
    )
    write_json(
        root / "executions.json",
        {"prepared-experiment": {"status": "completed", "receipt": receipt}},
    )
    write_json(
        root / "state.json",
        {
            "native_calls": [
                {
                    "role": "critic",
                    "context": "critic",
                    "status": "completed",
                    "result": raw,
                },
                {
                    "role": "critic",
                    "context": "critic",
                    "status": "completed",
                    "result": later,
                },
            ],
            "native_contexts": {
                "critic": {
                    "role": "critic",
                    "thread_id": "synthetic-independent-thread",
                }
            },
        },
    )
    return root, canonical, raw


def test_recovery_preserves_original_and_reads_only_small_published_evidence(
    settled_exp, tmp_path, monkeypatch
):
    root, canonical, raw = settled_exp
    original_path = root / "exports/000001/evaluate.json"
    original_bytes = original_path.read_bytes()
    open_file = Path.open
    reads = []

    def guarded_open(path, mode="r", *args, **kwargs):
        if "r" in mode:
            assert path != canonical, "Canonical scientific input must never be read"
            assert not path.is_relative_to(root / "work")
            reads.append(path)
        return open_file(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", guarded_open)
        manifest_path, manifest = build_recovery_bundle(root, tmp_path / "bundle")
    assert manifest_path.is_file()
    assert manifest["warnings"] == []
    assert manifest["limitations"]
    assert all(item["assessment_round"] == 1 for item in manifest["limitations"])
    assert all(item["source_round"] == 2 for item in manifest["limitations"])
    assert len(manifest["artifacts"]) == 11
    assert original_path.read_bytes() == original_bytes
    assert Path(manifest["original_evaluation_ref"]).read_bytes() == original_bytes
    evaluation = json.loads(Path(manifest["evaluation_ref"]).read_text())
    for field in ("summary", "claim", "limitations", "help"):
        assert evaluation[field] == raw[field]
    assert evaluation["input_citations"] == [str(canonical)]
    assert evaluation["provenance"]["raw_call_index"] == 0
    assert evaluation["provenance"]["source_round"] == 1
    assert evaluation["boundary_errors"] == [
        {"origin": "host-publication", "help": "Host rejected evidence filenames."}
    ]
    assert manifest["metrics_ref"] in evaluation["evidence"]
    assert manifest["report_ref"] in evaluation["evidence"]
    assert manifest["original_evaluation_ref"] != manifest["recovery_evaluation_ref"]
    for artifact in manifest["artifacts"]:
        assert (
            Path(artifact["ref"]).read_bytes()
            == Path(artifact["source_ref"]).read_bytes()
        )
    assert all("transcript" not in path.name for path in reads)


@pytest.mark.parametrize(
    "status,help_text,accepted",
    [
        ("ready", "", True),
        ("ready", "No blocker; retain the limitations.", True),
        ("blocked", "Supply the missing evaluation input.", False),
        ("blocked", "", False),
        ("invalid", "", False),
    ],
)
def test_recovery_accepts_ready_current_receipts_without_clearing_notes(
    settled_exp, tmp_path, status, help_text, accepted
):
    root, _, _ = settled_exp
    state = json.loads((root / "state.json").read_text())
    state["native_calls"][0]["result"].update(status=status, help=help_text)
    write_json(root / "state.json", state)
    original_path = root / "exports/000001/evaluate.json"
    original = json.loads(original_path.read_text())
    original["status"] = status
    write_json(original_path, original)
    original_bytes = original_path.read_bytes()

    _, manifest = build_recovery_bundle(root, tmp_path / "current-receipt-bundle")
    assert bool(manifest["warnings"]) is not accepted
    assert original_path.read_bytes() == original_bytes
    if accepted:
        evaluation = json.loads(Path(manifest["evaluation_ref"]).read_text())
        assert evaluation["status"] == status
        assert evaluation["help"] == help_text
        assert evaluation["provenance"]["raw_call_index"] == 0
        assert Path(manifest["original_evaluation_ref"]).read_bytes() == original_bytes


def test_historical_receipt_schema_is_separate_from_new_model_schema(settled_exp):
    _, _, legacy = settled_exp
    assert "status" not in legacy
    assert _strict_evaluation(legacy)
    assert "status" in EVALUATE_SCHEMA["required"]
    current = {**legacy, "status": "ready"}
    assert _strict_evaluation(current)
    assert not _strict_evaluation({**current, "unexpected": "field"})
    del current["claim"]
    assert not _strict_evaluation(current)


@pytest.mark.parametrize("scope_field", ["project_root", "published_knowledge_root"])
def test_fixed_readonly_scope_citations_need_no_input_content_reads(
    settled_exp, tmp_path, monkeypatch, scope_field
):
    root, _, _ = settled_exp
    scope = tmp_path / "declared-input-scope"
    source = scope / "inputs/frozen.json"
    source.parent.mkdir(parents=True)
    source.write_text("x" * 69_120)
    state = json.loads((root / "state.json").read_text())
    ref = os.path.relpath(source, root / "work")
    state["native_calls"][0]["result"]["evidence"].append(ref)
    state["native_study_context"] = {"input": {scope_field: str(scope)}}
    write_json(root / "state.json", state)
    write_json(
        root / "exports/000001/evaluate.json",
        {
            **state["native_calls"][0]["result"],
            "help": "Host rejected evidence filenames.",
        },
    )
    original_open = Path.open

    def forbid_input_contents(path, mode="r", *args, **kwargs):
        assert not path.is_relative_to(scope), (
            "Scientific input contents must never be read"
        )
        return original_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", forbid_input_contents)
        _, manifest = build_recovery_bundle(root, tmp_path / "scoped-bundle")
    assert manifest["warnings"] == []
    assert manifest["input_scope_roots"] == [str(scope)]
    evaluation = json.loads(Path(manifest["evaluation_ref"]).read_text())
    assert str(source) in evaluation["input_citations"]
    assert str(source) in evaluation["evidence"]
    assert evaluation["provenance"]["input_scope_roots"] == [str(scope)]
    assert evaluation["recovery_limitations"] == manifest["limitations"]
    assert str(source) not in {item["source_ref"] for item in manifest["artifacts"]}


@pytest.mark.parametrize(
    "fault", ["unknown-root", "scope-as-output", "symlink", "missing"]
)
def test_readonly_scope_never_admits_unknown_inputs_or_outputs(
    settled_exp, tmp_path, fault
):
    root, _, _ = settled_exp
    scope = tmp_path / "declared-input-scope"
    scope.mkdir()
    source = scope / "frozen.json"
    source.write_text("Synthetic input; metadata only.")
    state = json.loads((root / "state.json").read_text())
    state["native_study_context"] = {"input": {"project_root": str(scope)}}
    if fault == "unknown-root":
        source = tmp_path / "unknown.json"
        source.write_text("Outside declared scope.")
    elif fault == "symlink":
        link = scope / "linked.json"
        link.symlink_to(source.name)
        source = link
    elif fault == "missing":
        source = scope / "missing.json"
    field = "artifacts" if fault == "scope-as-output" else "evidence"
    state["native_calls"][0]["result"][field].append(str(source))
    write_json(root / "state.json", state)
    write_json(
        root / "exports/000001/evaluate.json",
        {
            **state["native_calls"][0]["result"],
            "help": "Host rejected evidence filenames.",
        },
    )
    _, manifest = build_recovery_bundle(root, tmp_path / "blocked-scoped-bundle")
    assert manifest["warnings"]
    assert manifest["evaluation_ref"] is None


@pytest.mark.parametrize(
    "fault",
    [
        "scientific-help",
        "duplicate-call",
        "unknown-input",
        "unsettled-execution",
        "nonzero-execution",
        "changed-science",
        "shared-context",
    ],
)
def test_authority_failure_never_fabricates_completed_evaluation(
    settled_exp, tmp_path, fault
):
    root, _, _ = settled_exp
    state = json.loads((root / "state.json").read_text())
    if fault == "scientific-help":
        state["native_calls"][0]["result"]["help"] = "Missing a frozen evaluator."
    elif fault == "duplicate-call":
        state["native_calls"].append(deepcopy(state["native_calls"][0]))
    elif fault == "unknown-input":
        state["native_calls"][0]["result"]["evidence"].append("unknown.json")
        exported = {
            **state["native_calls"][0]["result"],
            "help": "Host rejected evidence filenames.",
        }
        write_json(root / "exports/000001/evaluate.json", exported)
    elif fault == "changed-science":
        state["native_calls"][0]["result"]["claim"] = "Different claim."
    elif fault == "shared-context":
        state["native_contexts"]["plan"] = {
            "role": "proposer",
            "thread_id": "synthetic-independent-thread",
        }
    else:
        ledger = json.loads((root / "executions.json").read_text())
        if fault == "unsettled-execution":
            ledger["prepared-experiment"]["status"] = "unknown"
        else:
            ledger["prepared-experiment"]["receipt"]["exit_code"] = 1
        write_json(root / "executions.json", ledger)
    write_json(root / "state.json", state)
    _, manifest = build_recovery_bundle(root, tmp_path / "blocked-bundle")
    assert manifest["warnings"]
    assert manifest["evaluation_ref"] is None
    assert not (tmp_path / "blocked-bundle/evaluation.json").exists()
    assert manifest["original_evaluation_ref"]


def test_recovery_rejects_unpublished_or_escaping_artifacts(settled_exp, tmp_path):
    root, _, _ = settled_exp
    metadata = root / "rounds/000002/artifacts.json"
    mapping = json.loads(metadata.read_text())
    mapping["metrics.json"] = str(tmp_path / "unpublished.json")
    write_json(metadata, mapping)
    with pytest.raises(ValueError, match="explicitly published"):
        build_recovery_bundle(root, tmp_path / "bad-bundle")
    assert not (tmp_path / "bad-bundle").exists()
