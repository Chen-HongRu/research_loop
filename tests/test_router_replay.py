"""Replay accounting, isolation and blind-assessment ordering."""

import asyncio
import json
import time

import pytest

from benchmarks import router_replay as replay


def test_history_rejects_tools_and_retains_cost(tmp_path):
    path = tmp_path / "native/sessions/a.jsonl"
    path.parent.mkdir(parents=True)
    packet = {"evidence": []}
    rows = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"text": json.dumps(packet)}],
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call", "name": "exec_command"},
        },
        {
            "type": "token_usage_record",
            "payload": {
                "response_id": "r1",
                "usage": {"input_tokens": 20, "output_tokens": 3},
            },
        },
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    result = replay.inspect_history(tmp_path, packet)
    assert result["usage"]["total_tokens"] == 23
    assert not result["isolation_valid"]


def test_missing_history_is_not_free_success(tmp_path):
    assert not replay.inspect_history(tmp_path, {})["isolation_valid"]


@pytest.mark.parametrize("extra_turn", [False, True])
def test_history_requires_single_expected_turn_and_retains_receipts(
    tmp_path, extra_turn
):
    path = tmp_path / "native/sessions/a.jsonl"
    path.parent.mkdir(parents=True)
    rows = [
        {"type": "session_meta", "payload": {"id": "thread-1"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "turn_id": "turn-2" if extra_turn else "turn-1",
                "content": [{"text": "{}"}],
            },
        },
        {
            "type": "token_usage_record",
            "payload": {"response_id": "r1", "usage": {"input_tokens": 7}},
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "turn-1"},
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    result = replay.inspect_history(tmp_path, {}, "thread-1", expected_turn="turn-1")
    assert result["isolation_valid"] == (not extra_turn)
    assert result["usage"]["total_tokens"] == 7


def test_pair_coverage_denominator():
    rows = [
        {"grade": {"candidate": x}} for x in ("correct", "incorrect", "not_evaluable")
    ]
    assert replay.counts(rows, "candidate")["provisional_accuracy"] == 0.5
    assert replay.counts(rows, "candidate")["coverage"] == 2 / 3


def test_joint_pair_metrics_do_not_subtract_different_conditional_denominators():
    rows = [
        {"grade": {"baseline": b, "candidate": c}}
        for b, c in [
            ("correct", "not_evaluable"),
            ("incorrect", "correct"),
            ("correct", "incorrect"),
            ("incorrect", "incorrect"),
            ("not_evaluable", "correct"),
        ]
    ]
    result = replay.paired_counts(rows, 10)
    assert result["jointly_evaluable"] == 3
    assert result["improved"] == result["regressed"] == result["same"] == 1
    assert result["baseline_accuracy"] == result["candidate_accuracy"] == 1 / 3
    assert result["accuracy_difference"] == 0
    assert result["coverage"] == 0.3
    assert replay.counts(rows, "candidate", 10)["coverage"] == 0.4
    assert replay.counts(rows, "candidate", 10)["conditional_judgable_coverage"] == 0.8


@pytest.mark.parametrize("truncated", [False, True])
def test_blind_label_precedes_candidate_and_action_exposure(
    tmp_path, monkeypatch, truncated
):
    packet = {
        "goal": "goal",
        "request": "request",
        "state": {},
        "evidence": [{"id": "e1", "text": "work pending"}],
    }
    extracted = {
        "packets": [
            {
                "id": "op1",
                "input": packet,
                "baseline_action": {"secret": "actual-action"},
                "prefix_truncated": truncated,
                "grouping_status": "receipt_terminated",
            }
        ]
    }
    monkeypatch.setattr(replay, "extract_round", lambda *_: extracted)
    observed = []

    async def infer(root, policy, payload, schema, deadline):
        observed.append((policy, payload))
        usage = dict.fromkeys(
            (*replay.TOKEN_KEYS, "responses", "total_tokens", "uncached_input_tokens"),
            1,
        )
        if policy == replay.LABEL_POLICY:
            assert "actual-action" not in json.dumps(payload)
            return {"evaluable": True}, usage
        if policy == replay.ROUTER_POLICY:
            assert "actual-action" not in json.dumps(payload)
            return {
                "action": "wait",
                "assignments": [],
                "reason": "pending",
                "evidence_ids": ["e1"],
                "missing_information": [],
            }, usage
        return {
            "baseline": "correct",
            "candidate": "correct",
            "reason": "pending",
            "evidence_ids": ["e1"],
        }, usage

    monkeypatch.setattr(replay, "infer", infer)
    report = asyncio.run(
        replay.replay_round(tmp_path, 6, tmp_path / "output", time.time() + 60)
    )
    assert observed[0][0] == replay.LABEL_POLICY
    assert observed[1][0] == replay.ROUTER_POLICY
    assert len(observed) == (2 if truncated else 3)
    assert report["candidate"]["not_evaluable"] == int(truncated)


def synthetic_packets(n=1):
    small = {
        "goal": "goal",
        "request": "request",
        "state": {},
        "evidence": [{"id": "e1", "text": "small"}],
    }
    large = {**small, "evidence": [{"id": "e1", "text": "larger assessment context"}]}
    return {
        "packets": [
            {
                "id": f"op{i}",
                "input": small,
                "assessment_input": large,
                "assessment_truncated": False,
                "prefix_truncated": True,
                "grouping_status": "receipt_terminated",
                "baseline_action": {"action": "wait"},
            }
            for i in range(n)
        ]
    }


def usage(tokens):
    return {
        **dict.fromkeys(replay.USAGE_KEYS, 0),
        "input_tokens": tokens,
        "total_tokens": tokens,
        "uncached_input_tokens": tokens,
        "responses": 1,
    }


def wait_decision():
    return {
        "action": "wait",
        "assignments": [],
        "reason": "pending",
        "evidence_ids": ["e1"],
        "missing_information": [],
    }


@pytest.mark.parametrize("failed_phase", ["candidate", "grade"])
def test_failed_paid_call_preserves_all_costs_and_full_opportunity_count(
    tmp_path, monkeypatch, failed_phase
):
    extracted = synthetic_packets(2)
    monkeypatch.setattr(replay, "extract_round", lambda *_: extracted)

    async def infer(root, policy, payload, schema, deadline):
        phase = (
            "label"
            if policy == replay.LABEL_POLICY
            else "candidate"
            if policy == replay.ROUTER_POLICY
            else "grade"
        )
        root.mkdir()
        replay.write_json(
            root / "accounting.json",
            {
                "usage": usage({"label": 10, "candidate": 20, "grade": 30}[phase]),
                "issues": [],
            },
        )
        if phase == failed_phase:
            raise RuntimeError("synthetic paid failure")
        return ({"evaluable": True} if phase == "label" else wait_decision()), usage(
            10 if phase == "label" else 20
        )

    monkeypatch.setattr(replay, "infer", infer)
    output = tmp_path / "output"
    with pytest.raises(RuntimeError, match="synthetic paid failure"):
        asyncio.run(replay.replay_round(tmp_path, 1, output, time.time() + 60))
    report = replay.read(output / "report.json")
    assert report["opportunities"] == 2
    assert report["replayed"] == 0
    assert report["failed"] == ["op0"]
    assert report["unattempted"] == ["op1"]
    assert report["router"]["total_tokens"] == 20
    assert report["grading_tokens"] == (10 if failed_phase == "candidate" else 40)
    assert not report["costs"]["observed_only"]
    assert len(report["calls"]) == (2 if failed_phase == "candidate" else 3)
    assert report["candidate"]["coverage"] == 0


def test_larger_blind_assessment_small_candidate_and_deadline_coverage(
    tmp_path, monkeypatch
):
    extracted = synthetic_packets(2)
    monkeypatch.setattr(replay, "extract_round", lambda *_: extracted)
    clock = [1]
    monkeypatch.setattr(replay.time, "time", lambda: clock[0])

    async def infer(root, policy, payload, schema, deadline):
        if policy == replay.LABEL_POLICY:
            assert payload == extracted["packets"][0]["assessment_input"]
            return {"evaluable": True}, usage(10)
        if policy == replay.ROUTER_POLICY:
            assert payload == extracted["packets"][0]["input"]
            return wait_decision(), usage(20)
        assert payload["prefix"] == extracted["packets"][0]["assessment_input"]
        clock[0] = 100
        return {
            "baseline": "correct",
            "candidate": "correct",
            "reason": "pending",
            "evidence_ids": ["e1"],
        }, usage(30)

    monkeypatch.setattr(replay, "infer", infer)
    report = asyncio.run(replay.replay_round(tmp_path, 1, tmp_path / "out", 50))
    assert report["replayed"] == 1
    assert report["opportunities"] == 2
    assert report["replay_coverage"] == report["candidate"]["coverage"] == 0.5
    assert report["candidate"]["conditional_judgable_coverage"] == 1
    assert report["unattempted"] == ["op1"]


def test_missing_receipt_is_unknown_cost_not_free_success():
    costs = replay.call_costs(
        [{"phase": "candidate", "root": "synthetic", "usage": None}]
    )
    assert costs["observed_only"]
    assert costs["missing_receipts"] == ["synthetic"]


def test_candidate_manifest_is_frozen_not_silently_replaced(tmp_path, monkeypatch):
    manifest = {"policy_sha256": "synthetic-a", "source_sha256": {"code.py": "a"}}
    monkeypatch.setattr(replay, "candidate_manifest", lambda: manifest)
    replay.freeze_candidate(tmp_path)
    before = (tmp_path / "candidate.json").read_text()
    replay.freeze_candidate(tmp_path)
    manifest["source_sha256"]["code.py"] = "b"
    with pytest.raises(RuntimeError, match="Frozen candidate"):
        replay.freeze_candidate(tmp_path)
    assert (tmp_path / "candidate.json").read_text() == before


def test_watch_failure_stores_partial_report_and_blocks_retry(tmp_path, monkeypatch):
    baseline, output = tmp_path / "baseline", tmp_path / "output"
    baseline.mkdir()
    report_path = baseline / "measured.json"
    replay.write_json(report_path, {"rounds": [{"round": 1, "cost_evaluable": True}]})
    replay.write_json(
        baseline / "observer-status.json",
        {"latest_report": str(report_path), "status": "stopped"},
    )
    monkeypatch.setattr(replay, "isolation_probe", lambda *_: None)
    calls = []

    async def fail(snapshot, iteration, round_output, deadline):
        calls.append(iteration)
        round_output.mkdir()
        replay.write_json(
            round_output / "report.json", {"partial": True, "grading_tokens": 10}
        )
        raise RuntimeError("paid failure")

    monkeypatch.setattr(replay, "replay_round", fail)
    with pytest.raises(RuntimeError, match="paid failure"):
        asyncio.run(replay.watch(baseline, output, time.time() + 60))
    assert replay.read(output / "status.json")["status"] == "failed"
    assert (
        replay.read(output / "ledger.json")["rounds"]["1"]["report"]["grading_tokens"]
        == 10
    )
    with pytest.raises(RuntimeError, match="requires inspection"):
        asyncio.run(replay.watch(baseline, output, time.time() + 60))
    assert calls == [1]


def test_probe_accepts_masked_host_file_only_after_allowed_read_succeeds(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    def config(root):
        (root / "project").mkdir()
        return SimpleNamespace(
            launch_args_override=("codex", "app-server", "--listen", "stdio://")
        ), {}

    monkeypatch.setattr(replay, "isolated_config", config)
    results = iter(
        [
            SimpleNamespace(returncode=0, stdout="synthetic-allowed\n", stderr=""),
            SimpleNamespace(
                returncode=1, stdout="", stderr="No such file or directory"
            ),
        ]
    )
    monkeypatch.setattr(replay.subprocess, "run", lambda *a, **kw: next(results))
    result = replay.isolation_probe(tmp_path)
    assert result["passed"] and result["host_file_exists"]
    assert result["denial_mode"] == "masked_or_permission_denied"


def bootstrap_trace(
    root,
    policy="synthetic policy",
    *,
    instruction="standard native instructions",
    environment_root=None,
    arg0="abc123",
    metadata=None,
):
    root.mkdir(parents=True)
    packet = synthetic_packets()["packets"][0]["input"]
    replay.write_json(root / "input.json", packet)
    (root / "policy.txt").write_text(policy)
    messages = [
        ("developer", instruction),
        ("developer", policy + "\nstandard automatic permissions"),
        ("developer", "standard role setup"),
        ("developer", "standard delegation mode"),
        (
            "user",
            f"<environment_context>cwd={environment_root or root}/project arg0={root}/native/tmp/arg0-linux/codex-arg0{arg0}</environment_context>",
        ),
        ("user", json.dumps(packet)),
    ]
    events = [
        {"type": "session_meta", "payload": {"id": "thread-1"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}},
    ]
    if metadata is None:
        metadata = [
            {
                "type": "additional_tools",
                "id": "generated-1",
                "tools": [{"name": "synthetic", "description": "fixed description"}],
            }
        ]
    events += [{"type": "response_item", "payload": item} for item in metadata]
    events += [
        {
            "type": "response_item",
            "payload": {"type": "message", "role": role, "content": [{"text": text}]},
        }
        for role, text in messages
    ]
    events += [
        {
            "type": "token_usage_record",
            "payload": {"response_id": "r1", "usage": {"input_tokens": 7}},
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "turn-1"},
        },
    ]
    path = root / "native/sessions/a.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return packet


def test_explicit_calibration_accepts_only_normalized_bootstrap(tmp_path, monkeypatch):
    source = tmp_path / "synthetic-source"
    bootstrap_trace(source)
    manifest = replay.freeze_bootstrap(
        source, tmp_path / "bootstrap.json", controlled_input=True
    )
    monkeypatch.setattr(replay, "_BOOTSTRAP", manifest)
    root = tmp_path / "fresh-call"
    packet = bootstrap_trace(root, policy="different label policy", arg0="NEW456")
    assert replay.inspect_history(
        root, packet, expected_policy="different label policy"
    )["isolation_valid"]
    assert any("$POLICY" in item.get("text", "") for item in manifest["templates"])
    assert "$ARG0" in manifest["templates"][-1]["text"]
    assert str(source) not in json.dumps(manifest)


@pytest.mark.parametrize(
    "alteration", ["instruction", "environment", "root", "post_input"]
)
def test_calibrated_audit_rejects_foreign_bootstrap_content(tmp_path, alteration):
    source = tmp_path / "source"
    bootstrap_trace(source)
    manifest = replay.freeze_bootstrap(
        source, tmp_path / "bootstrap.json", controlled_input=True
    )
    root = tmp_path / "call"
    kwargs = {}
    if alteration == "instruction":
        kwargs["instruction"] = "foreign scientific instruction"
    if alteration == "environment":
        kwargs["environment_root"] = "/path/to/private-study"
    if alteration == "root":
        kwargs["environment_root"] = source
    packet = bootstrap_trace(root, **kwargs)
    if alteration == "post_input":
        path = root / "native/sessions/a.jsonl"
        with path.open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "developer",
                            "content": [{"text": "late instruction"}],
                        },
                    }
                )
                + "\n"
            )
    result = replay.inspect_history(
        root, packet, expected_policy="synthetic policy", bootstrap=manifest
    )
    assert not result["isolation_valid"]
    assert result["usage"]["total_tokens"] == 7


def test_bootstrap_requires_attestation_and_rejects_forbidden_context(tmp_path):
    source = tmp_path / "source"
    bootstrap_trace(source, instruction="read /path/to/private-study")
    with pytest.raises(ValueError, match="attestation"):
        replay.freeze_bootstrap(source, tmp_path / "bootstrap.json")
    with pytest.raises(ValueError, match="Forbidden"):
        replay.freeze_bootstrap(
            source,
            tmp_path / "bootstrap.json",
            controlled_input=True,
            forbidden_roots=["/path/to/private-study"],
        )
    assert not (tmp_path / "bootstrap.json").exists()


def test_infer_refuses_uncalibrated_model_admission(tmp_path, monkeypatch):
    monkeypatch.setattr(replay, "_BOOTSTRAP", None)
    with pytest.raises(RuntimeError, match="bootstrap"):
        asyncio.run(replay.infer(tmp_path / "call", "policy", {}, {}, time.time() + 60))
    assert not (tmp_path / "call").exists()


@pytest.mark.parametrize(
    "change", ["description", "nested_id", "late_tools", "configuration"]
)
def test_bootstrap_freezes_tool_and_configuration_metadata(tmp_path, change):
    source = tmp_path / "source"
    tools = {
        "type": "additional_tools",
        "id": "generated-source",
        "tools": [{"id": "fixed-nested-id", "description": "fixed description"}],
    }
    bootstrap_trace(source, metadata=[tools])
    manifest = replay.freeze_bootstrap(
        source, tmp_path / "bootstrap-v2.json", controlled_input=True
    )
    root = tmp_path / "call"
    altered = {**tools, "id": "generated-call", "tools": [dict(tools["tools"][0])]}
    if change == "description":
        altered["tools"][0]["description"] = "foreign instruction"
    elif change == "nested_id":
        altered["tools"][0]["id"] = "different nested id"
    metadata = [altered]
    if change == "configuration":
        metadata.append(
            {"type": "configuration_update", "id": "generated", "setting": "foreign"}
        )
    packet = bootstrap_trace(root, metadata=metadata)
    if change == "late_tools":
        path = root / "native/sessions/a.jsonl"
        with path.open("a") as stream:
            stream.write(json.dumps({"type": "response_item", "payload": tools}) + "\n")
    assert not replay.inspect_history(
        root, packet, expected_policy="synthetic policy", bootstrap=manifest
    )["isolation_valid"]


def test_bootstrap_normalizes_only_top_metadata_id_and_freezes_configuration(tmp_path):
    source = tmp_path / "source"
    metadata = [
        {"type": "additional_tools", "id": "generated-source", "tools": []},
        {"type": "configuration_update", "id": "generated-source", "mode": "fixed"},
    ]
    bootstrap_trace(source, metadata=metadata)
    manifest = replay.freeze_bootstrap(
        source, tmp_path / "bootstrap-v2.json", controlled_input=True
    )
    for item in metadata:
        item["id"] = "generated-new"
    root = tmp_path / "call"
    packet = bootstrap_trace(root, metadata=metadata)
    assert replay.inspect_history(
        root, packet, expected_policy="synthetic policy", bootstrap=manifest
    )["isolation_valid"]
    assert manifest["templates"][0]["role"] == "additional_tools"
    assert "id" not in manifest["templates"][0]["payload"]


def test_collecting_watcher_waits_for_later_round(tmp_path, monkeypatch):
    baseline, output = tmp_path / "baseline", tmp_path / "output"
    baseline.mkdir()
    measured = baseline / "measured.json"
    replay.write_json(measured, {"rounds": []})
    status = baseline / "observer-status.json"
    replay.write_json(status, {"latest_report": str(measured), "status": "collecting"})
    monkeypatch.setattr(replay, "isolation_probe", lambda *_: None)
    calls = []

    async def arrival(_):
        replay.write_json(measured, {"rounds": [{"round": 2, "cost_evaluable": True}]})
        replay.write_json(status, {"latest_report": str(measured), "status": "stopped"})

    async def run(snapshot, iteration, destination, deadline):
        calls.append(iteration)
        return {"complete": True}

    monkeypatch.setattr(replay.asyncio, "sleep", arrival)
    monkeypatch.setattr(replay, "replay_round", run)
    asyncio.run(replay.watch(baseline, output, time.time() + 60))
    assert calls == [2]
