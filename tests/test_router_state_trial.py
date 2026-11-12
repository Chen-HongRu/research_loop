"""Synthetic paired development trial; no model, remote host, or GPU calls."""

import asyncio
import copy
import json
import time

import pytest

from benchmarks import router_state_trial as trial


def decision(reason="pending"):
    return {
        "action": "wait",
        "assignments": [],
        "reason": reason,
        "evidence_ids": ["e1"],
        "missing_information": [],
    }


def usage(tokens):
    return {
        **dict.fromkeys(trial.replay.USAGE_KEYS, 0),
        "input_tokens": tokens,
        "total_tokens": tokens,
        "uncached_input_tokens": tokens,
        "responses": 1,
    }


def source_fixture(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for iteration in range(6, 12):
        directory = source / f"round-{iteration:06d}"
        directory.mkdir()
        packets, rows = [], []
        for index in range(4):
            identifier = f"round-{iteration:06d}-op-{index:04d}"
            packet = {
                "goal": "synthetic goal",
                "request": "route",
                "state": {"pending": True},
                "evidence": [{"id": "e1", "text": "Prior input only"}],
            }
            packets.append(
                {
                    "id": identifier,
                    "input": packet,
                    "assessment_input": {
                        **packet,
                        "evidence": [{"id": "e1", "text": "Larger prior input"}],
                    },
                    "baseline_action": {"FUTURE_ACTION_SECRET": True},
                    "future_outcome": "FUTURE_RESULT_SECRET",
                    "assessment_truncated": False,
                    "grouping_status": "receipt_terminated",
                }
            )
            rows.append(
                {
                    "id": identifier,
                    "candidate": decision(),
                    "router_usage": usage(7),
                    "grade": {
                        "baseline": "correct",
                        "candidate": "incorrect" if index == 0 else "correct",
                        "reason": "STALE_SCORE_SECRET",
                    },
                }
            )
            (directory / identifier).mkdir()
            trial.write_json(
                directory / identifier / "blind-label.json",
                {
                    "evaluable": True,
                    "reason": "Frozen blind label",
                    "required_work": ["wait"],
                },
            )
        trial.write_json(directory / "packets.json", {"packets": packets})
        trial.write_json(directory / "decisions.json", rows)
        trial.write_json(directory / "report.json", {"complete": True})
    return source


@pytest.fixture
def setup(tmp_path, monkeypatch):
    source = source_fixture(tmp_path)
    monkeypatch.setattr(trial.replay, "load_bootstrap", lambda _: None)
    monkeypatch.setattr(trial, "verify_source_policy", lambda _: None)
    monkeypatch.setattr(
        trial, "fingerprints", lambda: {"source": "synthetic-source-only-fingerprint"}
    )

    def enrich(packet, assessment):
        result = copy.deepcopy(packet)
        result["state"]["prior_fact"] = assessment["evidence"][0]["text"]
        return result

    monkeypatch.setattr(trial, "enrich_input", enrich)
    return source


def test_selection_fixes_six_regression_and_both_correct_control_pairs(tmp_path):
    source = source_fixture(tmp_path)
    selected = trial.select_cases(source)
    assert len(selected) == 12
    assert selected == trial.select_cases(source)
    for iteration in range(6, 12):
        pair = [case for case in selected if case["round"] == iteration]
        assert [case["stratum"] for case in pair] == ["prior_regression", "control"]
        assert pair[0]["id"].endswith("0000")
        assert pair[1]["id"] != pair[0]["id"]
    assert trial.select_cases(source, 1) == selected[:1]
    serialized = json.dumps(selected)
    assert "FUTURE_ACTION_SECRET" not in serialized
    assert "FUTURE_RESULT_SECRET" not in serialized
    assert "STALE_SCORE_SECRET" not in serialized


@pytest.mark.parametrize("limit", [0, 13, True, 1.5])
def test_selection_rejects_unbounded_or_invalid_limits(tmp_path, limit):
    with pytest.raises(ValueError, match="limit"):
        trial.select_cases(tmp_path, limit)


def test_selection_requires_both_correct_control(tmp_path):
    source = source_fixture(tmp_path)
    path = source / "round-000006/decisions.json"
    rows = trial.replay.read(path)
    for row in rows[1:]:
        row["grade"] = {"baseline": "incorrect", "candidate": "incorrect"}
    trial.write_json(path, rows)
    with pytest.raises(ValueError, match="both-correct"):
        trial.select_cases(source)


def test_future_and_baseline_changes_do_not_change_selected_inputs(tmp_path):
    source = source_fixture(tmp_path)
    before = trial.select_cases(source)
    for directory in source.glob("round-*"):
        path = directory / "packets.json"
        document = trial.replay.read(path)
        for packet in document["packets"]:
            packet["baseline_action"] = "different future action"
            packet["future_outcome"] = "different later result"
        trial.write_json(path, document)
    assert trial.select_cases(source) == before


def test_anonymous_mapping_is_deterministic_and_both_orders_are_possible():
    mappings = []
    for identifier in ("a", "b", "c", "d", "e"):
        pair, mapping = trial.anonymous_pair(identifier, {"version": 1}, {"version": 2})
        assert (pair, mapping) == trial.anonymous_pair(
            identifier, {"version": 1}, {"version": 2}
        )
        assert pair["a"]["version"] == (1 if mapping["a"] == "v1" else 2)
        mappings.append(mapping["a"])
    assert set(mappings) == {"v1", "v2"}


def test_complete_trial_preserves_blindness_mapping_and_24_call_cap(
    setup, tmp_path, monkeypatch
):
    output = tmp_path / "trial"
    observed = []

    async def infer(root, policy, payload, schema, deadline):
        assert (output / "selected-cases.json").exists()
        observed.append((policy, copy.deepcopy(payload)))
        assert "FUTURE_ACTION_SECRET" not in json.dumps(payload)
        assert "FUTURE_RESULT_SECRET" not in json.dumps(payload)
        assert "STALE_SCORE_SECRET" not in json.dumps(payload)
        if policy == trial.ROUTER_POLICY:
            assert payload["state"]["prior_fact"] == "Larger prior input"
            assert "v1" not in payload and "blind_label" not in payload
            return decision("new pending"), usage(10)
        assert policy == trial.PAIR_POLICY
        assert set(payload) == {
            "prefix",
            "frozen_assessment",
            "common_production_contract",
            "recommendations",
        }
        assert set(payload["recommendations"]) == {"a", "b"}
        return {
            "a": "correct",
            "b": "incorrect",
            "label_conflict": False,
            "reason": "synthetic judgment",
            "evidence_ids": ["e1"],
        }, usage(20)

    monkeypatch.setattr(trial.replay, "infer", infer)
    report = asyncio.run(
        trial.run_trial(setup, output, tmp_path / "bootstrap.json", time.time() + 60)
    )
    assert report["status"] == "completed"
    assert len(observed) == 24
    assert sum(policy == trial.ROUTER_POLICY for policy, _ in observed) == 12
    assert report["summary"]["selected"] == report["summary"]["jointly_evaluable"] == 12
    for row in report["rows"]:
        assert row[f"{row['anonymous_mapping']['a']}_grade"] == "correct"
        assert row[f"{row['anonymous_mapping']['b']}_grade"] == "incorrect"
    assert report["fresh_v2_usage"]["total_tokens"] == 120
    assert report["fresh_grade_usage"]["total_tokens"] == 240
    assert report["v1_cached_cost"]["usage"]["total_tokens"] == 84
    assert report["v1_cached_cost"]["fresh_calls"] == 0
    assert "stratified" in report["design"]
    assert any("no overall accuracy" in text for text in report["limitations"])
    with pytest.raises(FileExistsError):
        asyncio.run(
            trial.run_trial(
                setup, output, tmp_path / "bootstrap.json", time.time() + 60
            )
        )


@pytest.mark.parametrize("fail_phase", ["candidate", "grade"])
def test_failed_attempts_preserve_fees_and_never_retry(
    setup, tmp_path, monkeypatch, fail_phase
):
    output = tmp_path / "failed-trial"
    attempts = []

    async def infer(root, policy, payload, schema, deadline):
        phase = "candidate" if policy == trial.ROUTER_POLICY else "grade"
        attempts.append(phase)
        root.mkdir()
        trial.write_json(
            root / "accounting.json",
            {"usage": usage(11 if phase == "candidate" else 17), "issues": []},
        )
        if phase == fail_phase:
            raise TimeoutError("synthetic paid timeout")
        return decision(), usage(11)

    monkeypatch.setattr(trial.replay, "infer", infer)
    with pytest.raises(TimeoutError):
        asyncio.run(
            trial.run_trial(
                setup, output, tmp_path / "bootstrap.json", time.time() + 60
            )
        )
    report = trial.replay.read(output / "report.json")
    assert attempts == (
        ["candidate"] if fail_phase == "candidate" else ["candidate", "grade"]
    )
    assert report["status"] == "failed"
    assert report["summary"]["selected"] == 12
    assert report["summary"]["graded"] == 0
    assert len(report["failed"]) == 1 and len(report["unattempted"]) == 11
    assert report["fresh_v2_usage"]["total_tokens"] == 11
    assert report["fresh_grade_usage"]["total_tokens"] == (
        17 if fail_phase == "grade" else 0
    )
    assert len(report["calls"]) <= 24
    assert trial.replay.read(output / "status.json")["automatic_retry"] is False


def test_common_denominator_and_label_conflict_are_unjudgeable():
    grade = {
        "a": "correct",
        "b": "incorrect",
        "label_conflict": True,
        "reason": "label conflicts with common contract",
        "evidence_ids": [],
    }
    assert trial.validate_pair_grade(grade, {"evidence": []})["a"] == "U"
    assert trial.validate_pair_grade(grade, {"evidence": []})["b"] == "U"
    rows = [
        {"v1_grade": a, "v2_grade": b}
        for a, b in (
            ("correct", "U"),
            ("incorrect", "correct"),
            ("correct", "incorrect"),
            ("U", "correct"),
        )
    ]
    summary = trial.summarize(rows, 12)
    assert summary["jointly_evaluable"] == 2
    assert summary["v1_sample_accuracy"] == summary["v2_sample_accuracy"] == 0.5
    assert summary["paired_sample_accuracy_difference"] == 0
    assert summary["joint_coverage"] == 2 / 12


def test_missing_failed_receipt_is_unknown_not_zero_cost(setup, tmp_path, monkeypatch):
    async def infer(*args):
        raise TimeoutError("missing receipt")

    monkeypatch.setattr(trial.replay, "infer", infer)
    with pytest.raises(TimeoutError):
        asyncio.run(
            trial.run_trial(
                setup,
                tmp_path / "missing",
                tmp_path / "bootstrap.json",
                time.time() + 60,
                1,
            )
        )
    report = trial.replay.read(tmp_path / "missing/report.json")
    assert report["fresh_costs"]["observed_only"]
    assert len(report["fresh_costs"]["missing_receipts"]) == 1


def test_real_state_enrichment_path_and_source_contract_check(tmp_path):
    source = source_fixture(tmp_path)
    case = trial.select_cases(source, 1)[0]
    enriched = trial.enrich_input(case["input"], case["assessment_input"])
    assert "router_state" in enriched["state"]
    assert enriched["goal"] == case["input"]["goal"]
    assert len(json.dumps(enriched, ensure_ascii=False)) <= trial.MAX_CHARS
    trial.validate_packet(enriched)
    current = trial.fingerprints()
    candidate = {
        "policy_sha256": trial._hash(trial.ROUTER_POLICY),
        "model": trial.replay.MODEL,
        "effort": trial.replay.EFFORT,
        "source_sha256": current["source_sha256"],
    }
    trial.write_json(source / "candidate.json", candidate)
    trial.verify_source_policy(source)
    candidate["policy_sha256"] = "different"
    trial.write_json(source / "candidate.json", candidate)
    with pytest.raises(ValueError, match="policy"):
        trial.verify_source_policy(source)


def test_trial_code_change_blocks_model_admission(setup, tmp_path, monkeypatch):
    values = iter([{"code": "original"}, {"code": "changed"}])
    monkeypatch.setattr(trial, "fingerprints", lambda: next(values))

    async def infer(*args):
        pytest.fail("Code changed before admission; no model call is allowed")

    monkeypatch.setattr(trial.replay, "infer", infer)
    with pytest.raises(RuntimeError, match="code changed"):
        asyncio.run(
            trial.run_trial(
                setup,
                tmp_path / "changed",
                tmp_path / "bootstrap.json",
                time.time() + 60,
                1,
            )
        )
    report = trial.replay.read(tmp_path / "changed/report.json")
    assert report["calls"] == []


def test_fresh_selection_spans_six_rounds_with_balanced_fixed_strata(tmp_path):
    source = source_fixture(tmp_path)
    original = trial.select_cases(source)
    fresh = trial.select_fresh_cases(source)
    assert [case["round"] for case in fresh] == list(range(6, 12))
    assert [case["stratum"] for case in fresh] == ["prior_regression", "control"] * 3
    assert all(case in original for case in fresh)
    assert fresh == trial.select_fresh_cases(source)


def test_fresh_mode_grades_fresh_v1_and_caps_all_calls_at_eighteen(
    setup, tmp_path, monkeypatch
):
    output = tmp_path / "fresh"
    calls = []

    async def infer(root, policy, payload, schema, deadline):
        selected = trial.replay.read(output / "selected-cases.json")["cases"]
        assert len(selected) == 6
        if policy == trial.ROUTER_POLICY:
            version = "v2" if "prior_fact" in payload["state"] else "v1"
            calls.append(version)
            return decision("fresh-" + version), usage(5 if version == "v1" else 7)
        calls.append("grade")
        assert {item["reason"] for item in payload["recommendations"].values()} == {
            "fresh-v1",
            "fresh-v2",
        }
        return {
            "a": "correct",
            "b": "correct",
            "label_conflict": False,
            "reason": "common-contract score",
            "evidence_ids": ["e1"],
        }, usage(11)

    monkeypatch.setattr(trial.replay, "infer", infer)
    report = asyncio.run(
        trial.run_trial(
            setup, output, tmp_path / "bootstrap.json", time.time() + 60, fresh_v1=True
        )
    )
    assert report["status"] == "completed"
    assert report["v1_prediction_source"] == "fresh"
    assert len(calls) == 18
    assert calls.count("v1") == calls.count("v2") == calls.count("grade") == 6
    selected = trial.replay.read(output / "selected-cases.json")["cases"]
    expected = [
        phase for case in selected for phase in [*case["prediction_order"], "grade"]
    ]
    assert calls == expected
    assert report["fresh_v1_usage"]["total_tokens"] == 30
    assert report["fresh_v2_usage"]["total_tokens"] == 42
    assert report["fresh_grade_usage"]["total_tokens"] == 66
    assert report["fresh_costs"]["all_observed"]["total_tokens"] == 138
    assert report["v1_cached_cost"]["fresh_calls"] == 0
    assert all(row["v1"]["reason"] == "fresh-v1" for row in report["rows"])
    assert trial.replay.read(output / "status.json")["maximum_fresh_calls"] == 18


def test_fresh_mode_failed_grader_cost_includes_both_predictions(
    setup, tmp_path, monkeypatch
):
    output = tmp_path / "fresh-failed"
    attempts = []

    async def infer(root, policy, payload, schema, deadline):
        phase = (
            "grade"
            if policy == trial.PAIR_POLICY
            else "v2"
            if "prior_fact" in payload["state"]
            else "v1"
        )
        attempts.append(phase)
        root.mkdir()
        fees = {"v1": 3, "v2": 5, "grade": 7}
        trial.write_json(
            root / "accounting.json", {"usage": usage(fees[phase]), "issues": []}
        )
        if phase == "grade":
            raise TimeoutError("paid grading failure")
        return decision("fresh-" + phase), usage(fees[phase])

    monkeypatch.setattr(trial.replay, "infer", infer)
    with pytest.raises(TimeoutError):
        asyncio.run(
            trial.run_trial(
                setup,
                output,
                tmp_path / "bootstrap.json",
                time.time() + 60,
                fresh_v1=True,
            )
        )
    report = trial.replay.read(output / "report.json")
    assert len(attempts) == 3 and set(attempts) == {"v1", "v2", "grade"}
    assert len(report["calls"]) == 3 <= 18
    assert report["fresh_v1_usage"]["total_tokens"] == 3
    assert report["fresh_v2_usage"]["total_tokens"] == 5
    assert report["fresh_grade_usage"]["total_tokens"] == 7
    assert report["fresh_costs"]["all_observed"]["total_tokens"] == 15
    assert len(report["unattempted"]) == 5
    assert report["summary"]["selected"] == 6


def test_fresh_mode_does_not_allow_an_arbitrary_case_limit(setup, tmp_path):
    with pytest.raises(ValueError, match="fixes six"):
        asyncio.run(
            trial.run_trial(
                setup,
                tmp_path / "bad-limit",
                tmp_path / "bootstrap.json",
                time.time() + 60,
                limit=1,
                fresh_v1=True,
            )
        )
    assert not (tmp_path / "bad-limit").exists()
