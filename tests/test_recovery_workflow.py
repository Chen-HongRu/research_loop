"""Same completion gate for reuse-only Router and controlled Lead publication."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest

from research_loop import group_router, human_input, loop
from research_loop.codex import CodexConfig
from research_loop.group_programs.exp_recovery import build_recovery_bundle
from research_loop.group_programs.synthesis import recovery_author_schema
from research_loop.groups import GroupDefinition
from research_loop.instructions import RESULT_SCHEMA
from research_loop.recovery import _legacy_recovery_author_schema, evidence_packet
from test_exp_recovery import settled_exp as _settled_exp

settled_exp = _settled_exp


def test_lead_selects_only_known_current_or_legacy_author_schema():
    current = recovery_author_schema(RESULT_SCHEMA)
    assert not _legacy_recovery_author_schema(current, RESULT_SCHEMA)
    legacy = deepcopy(current)
    del legacy["properties"]["status"]
    legacy["required"].remove("status")
    assert _legacy_recovery_author_schema(legacy, RESULT_SCHEMA)
    assert "status" in current["required"]
    for field in ("help", "knowledge_files"):
        malformed = deepcopy(legacy)
        del malformed["properties"][field]
        malformed["required"].remove(field)
        with pytest.raises(ValueError, match="Unsupported recovery Author schema"):
            _legacy_recovery_author_schema(malformed, RESULT_SCHEMA)


def test_recovery_packet_accepts_96_case_small_report_without_rewriting(
    settled_exp, tmp_path
):
    source, _, _ = settled_exp
    manifest_path, manifest = build_recovery_bundle(source, tmp_path / "bundle")
    report = {
        "cases": [
            dict(
                seed=1001 + index,
                status="FAILED",
                elapsed=2.0,
                reference_duration=9.0,
                joint_within_threshold_fraction=0.005,
                limitation="Synthetic bounded report only.",
            )
            for index in range(96)
        ],
        "notes": "",
    }
    report["notes"] = "x" * (41504 - len(json.dumps(report)))
    content = json.dumps(report)
    assert len(content.encode()) == 41504
    metrics = Path(manifest["metrics_ref"])
    metrics.write_text(content)

    packet = evidence_packet(manifest_path)
    recovered = next(
        item["content"]
        for item in packet["evidence_files"]
        if item["kind"] == "metrics"
    )
    assert recovered == report
    assert len(recovered["cases"]) == 96
    assert metrics.read_text() == content
    assert len(json.dumps(packet, ensure_ascii=False)) <= 64000


@pytest.mark.parametrize("boundary", ["file", "packet"])
def test_recovery_packet_keeps_finite_file_and_total_limits(
    settled_exp, tmp_path, boundary
):
    source, _, _ = settled_exp
    manifest_path, manifest = build_recovery_bundle(source, tmp_path / "bundle")
    metrics = Path(manifest["metrics_ref"])
    if boundary == "file":
        metrics.write_text(json.dumps({"synthetic_report": "x" * (64 * 1024)}))
        error = "small report files"
    else:
        metrics.write_text(json.dumps({"synthetic_report": "x" * 40000}))
        Path(manifest["report_ref"]).write_text("x" * 40000)
        error = "model input budget"
    with pytest.raises(ValueError, match=error):
        evidence_packet(manifest_path)


def candidate(sources):
    request = sources["study_context"]["input"].get("human_request")
    result = dict(
        summary="Recovered the existing independent assessment.",
        goal="achieved",
        limitations=["Synthetic only; no new computation."],
        next_action="",
        study_action="stop",
        stop_reason="The bounded recovery request is complete.",
    )
    if request:
        result["human_request"] = dict(
            id=request["id"],
            status="completed",
            reason="Existing execution and independent assessment are published.",
        )
    return dict(
        summary=result["summary"],
        status="ready",
        help="Unreviewed candidate prepared; host independent review is next.",
        study_result=result,
        knowledge_files=[
            dict(
                source="knowledge/index.md",
                target="index.md",
                content="# Recovery\n\n[Finding](finding.md)\n",
            ),
            dict(
                source="knowledge/finding.md",
                target="finding.md",
                content="Existing result retained; synthetic only.",
            ),
        ],
        stop_basis="goal-satisfied",
    )


@pytest.mark.parametrize("mode", ["router", "lead"])
@pytest.mark.parametrize("with_request", [False, True])
@pytest.mark.parametrize("current_receipt", [False, True])
def test_recovery_publication_uses_same_completion_gate_without_experiment(
    settled_exp,
    tmp_path,
    monkeypatch,
    mode,
    with_request,
    current_receipt,
    program_revision=None,
):
    source, _, _ = settled_exp
    if current_receipt:
        state = json.loads((source / "state.json").read_text())
        state["native_calls"][0]["result"].update(
            status="ready", help="No blocker; retain the limitations."
        )
        (source / "state.json").write_text(json.dumps(state))
        original = source / "exports/000001/evaluate.json"
        value = json.loads(original.read_text())
        value["status"] = "ready"
        original.write_text(json.dumps(value))
    project = tmp_path / "project"
    project.mkdir()
    goal = tmp_path / "goal.md"
    goal.write_text(
        "Recover and publish already settled evidence; never execute the experiment."
    )
    root = tmp_path / mode
    pinned_source = None
    if program_revision is not None:
        historical = subprocess.run(
            [
                "git",
                "show",
                f"{program_revision}:src/research_loop/group_programs/synthesis.py",
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
        )
        if historical.returncode:
            pytest.skip(
                "Historical source probe requires the repository's old Git objects"
            )
        pinned_source = tmp_path / "historical-synthesis.py"
        pinned_source.write_bytes(historical.stdout)
        version = {"91ccf18": "5", "5f2f817": "6"}[program_revision]
        monkeypatch.setattr(
            "research_loop.workflow.definitions",
            lambda **kwargs: [
                GroupDefinition(
                    "synthesis",
                    version,
                    "Pinned recovery",
                    pinned_source,
                    {"produces_study_result": True},
                )
            ],
        )
    loop.initialize(root, project, goal, coordinator=mode, recover_exp=source)
    if with_request:
        human_input.submit(root, goal.read_text())
    assert {g["id"] for g in loop.read_config(root)["groups"]} == {"synthesis"}
    assert loop.read_config(root)["groups"][0]["version"] == (
        version if pinned_source else "7"
    )
    calls = []
    thread_ids = []

    class Fake:
        def __init__(self, config, journal, tools=()):
            self.tools = {tool.name: tool for tool in tools}

        async def open(self):
            pass

        async def verify(self, *args, **kwargs):
            pass

        async def stop(self):
            pass

        async def settle(self):
            pass

        async def close(self):
            pass

        async def run(self, instructions, payload, schema, **kwargs):
            thread = f"thread-{len(thread_ids)}"
            thread_ids.append(thread)
            if kwargs.get("on_started"):
                kwargs["on_started"](thread)
            if "action" in schema["properties"]:
                raise AssertionError("The fixed recovery profile needs no Router model")
            if "review_candidate" in self.tools:
                calls.append("lead")
                assert kwargs["allow_builtin_tools"] is False
                if pinned_source:
                    assert "review_protocol" not in payload
                else:
                    assert (
                        payload["review_protocol"]["review_step"]
                        == "through_review_candidate_tool"
                    )
                value = candidate(payload)
                if "status" not in schema["properties"]:
                    del value["status"]
                    value["help"] = ""
                reviewed = await self.tools["review_candidate"].call(deepcopy(value))
                assert reviewed["review"]["verdict"] == "accept"
                return reviewed["accepted_candidate"]
            assert kwargs["allow_tools"] is False
            if "verdict" in schema["properties"]:
                calls.append("critic")
                assert (
                    payload["assignment"]["analysis_record"]["study_result"]["goal"]
                    == "achieved"
                )
                return dict(
                    summary="Independent review complete",
                    verdict="accept",
                    reason="The candidate preserves existing evidence and limitations.",
                    artifacts=[],
                )
            calls.append("analyst")
            assert (
                payload["assignment"]["review_protocol"]["review_step"]
                == "after_ready_candidate_return"
            )
            return candidate(payload["assignment"])

    def config(*args, **kwargs):
        return CodexConfig(cwd=str(project)), {}

    monkeypatch.setattr(group_router, "configuration", config)
    monkeypatch.setattr("research_loop.group_agents.configuration", config)
    monkeypatch.setattr("research_loop.recovery.configuration", config)

    async def no_execution(*args, **kwargs):
        raise AssertionError("Recovery must not execute an experiment")

    monkeypatch.setattr(
        "research_loop.group_agents.NativeGroupServices.execute_prepared", no_execution
    )
    state = asyncio.run(loop.run(root, native_factory=Fake))
    assert state["status"] == "completed"
    if with_request:
        assert state["result"]["human_request"]["status"] == "completed"
        assert (root / "inputs/human.md").read_text() == ""
    else:
        assert "human_request" not in state["result"]
    assert (root / "knowledge/current/finding.md").is_file()
    assert (root / "knowledge/current/recovery-evidence/evaluation.json").is_file()
    assert calls == (["analyst", "critic"] if mode == "router" else ["lead", "critic"])
    assert state["thread_id"] == thread_ids[0]
    assert len(set(thread_ids)) == len(thread_ids)
    assert len(list((root / "group-runs").glob("*/run.json"))) == 1
    source_record = json.loads((source / "run.json").read_text())
    assert source_record["status"] == "closed" and source_record["round"] == 2
    if pinned_source:
        assert (
            root / "groups/programs/synthesis.py"
        ).read_bytes() == pinned_source.read_bytes()
        run_state = json.loads(
            next((root / "group-runs").glob("*/state.json")).read_text()
        )
        assert "status" not in run_state["lead_accepted_candidate"]


@pytest.mark.parametrize("revision", ["91ccf18", "5f2f817"])
def test_pinned_legacy_lead_reaches_typed_review_and_publication(
    settled_exp, tmp_path, monkeypatch, revision
):
    test_recovery_publication_uses_same_completion_gate_without_experiment(
        settled_exp,
        tmp_path,
        monkeypatch,
        mode="lead",
        with_request=True,
        current_receipt=False,
        program_revision=revision,
    )
