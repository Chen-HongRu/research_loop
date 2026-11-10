"""Synthetic receipt evidence for the coordinator-free benchmark."""

import copy
import json
from pathlib import Path

import pytest

from benchmarks.experiment_cycle.accounting import summarize_usage, zero_usage


def receipt(rid, inputs=10, cached=2, output=3, reasoning=1):
    return {
        "timestamp": "1900-01-01T00:00:00Z",
        "type": "token_usage_record",
        "payload": {
            "response_id": rid,
            "usage": {
                "input_tokens": inputs,
                "cached_input_tokens": cached,
                "output_tokens": output,
                "reasoning_output_tokens": reasoning,
            },
        },
    }


def task(tmp_path, stage, records=(), *, thread_id=None, tail=b""):
    thread_id = thread_id or f"synthetic-{stage}"
    path = tmp_path / f"{stage}.jsonl"
    events = [{"type": "session_meta", "payload": {"id": thread_id}}, *records]
    path.write_bytes(
        b"".join(json.dumps(event).encode() + b"\n" for event in events) + tail
    )
    return {
        "stage": stage,
        "thread_id": thread_id,
        "history_path": str(path),
        "invoked": True,
    }


def codes(result):
    return {item["code"] for item in result["coverage"]["issues"]}


def test_exact_stage_sum_includes_compaction_failed_task_and_late_timestamps(tmp_path):
    roster = [
        task(
            tmp_path,
            "plan",
            [receipt("plan"), {"type": "compacted"}, receipt("compaction")],
        ),
        task(tmp_path, "prepare", [receipt("failed")]),
        task(tmp_path, "evaluate", [receipt("evaluate")]),
    ]
    roster[1]["outcome"] = "failed"
    before = copy.deepcopy(roster)
    files = {
        row["history_path"]: Path(row["history_path"]).read_bytes() for row in roster
    }
    result = summarize_usage(roster)
    expected = {
        "input_tokens": 40,
        "cached_input_tokens": 8,
        "output_tokens": 12,
        "reasoning_output_tokens": 4,
        "uncached_input_tokens": 32,
        "total_tokens": 52,
        "responses": 4,
    }
    assert result["total"] == expected
    for key, value in expected.items():
        assert sum(stage[key] for stage in result["by_stage"].values()) == value
    assert result["by_stage"]["plan"]["responses"] == 2
    assert result["by_stage"]["prepare"]["responses"] == 1
    assert result["by_stage"]["execute"] == zero_usage()
    assert result["unassigned"] == zero_usage()
    assert result["coordinator"] == {"present": False, "model_calls": 0}
    assert result["coverage"]["complete"]
    assert result["coverage"]["measurable"]
    assert result["coverage"]["histories_read"] == 3
    assert roster == before
    for path, content in files.items():
        with open(path, "rb") as stream:
            assert stream.read() == content


def test_response_ids_deduplicate_globally_in_roster_order(tmp_path):
    result = summarize_usage(
        [
            task(tmp_path, "plan", [receipt("shared"), receipt("shared")]),
            task(tmp_path, "prepare", [receipt("shared"), receipt("unique")]),
            task(tmp_path, "evaluate", [receipt("shared")]),
        ]
    )
    assert result["total"]["responses"] == 2
    assert result["by_stage"]["plan"]["responses"] == 1
    assert result["by_stage"]["prepare"]["responses"] == 1
    assert result["by_stage"]["evaluate"]["responses"] == 0
    assert result["coverage"]["duplicate_responses"] == 3
    assert result["coverage"]["complete"]


@pytest.mark.parametrize("kind", ["absent", "missing", "empty", "invalid"])
def test_invoked_task_without_receipts_is_unmeasured(tmp_path, kind):
    row = task(tmp_path, "plan")
    if kind == "absent":
        row["history_path"] = None
    elif kind == "missing":
        row["history_path"] = str(tmp_path / "missing.jsonl")
    elif kind == "invalid":
        row = task(tmp_path, "plan", [receipt("bad", inputs=-1)])
    result = summarize_usage([row])
    assert result["total"] == zero_usage()
    assert not result["coverage"]["complete"]
    assert not result["coverage"]["measurable"]
    expected = {
        "absent": "absent_history",
        "missing": "missing_history",
        "empty": "missing_usage_receipts",
        "invalid": "invalid_usage_record",
    }
    assert expected[kind] in codes(result)


def test_startup_failure_before_identity_preserves_unknown_cost():
    result = summarize_usage(
        [
            {
                "stage": "plan",
                "invoked": True,
                "thread_id": None,
                "history_path": None,
            }
        ]
    )
    assert not result["coverage"]["complete"]
    assert not result["coverage"]["measurable"]
    assert codes(result) == {"missing_thread_id", "absent_history"}


@pytest.mark.parametrize("tail", [b'{"type":', json.dumps(receipt("tail")).encode()])
def test_unpublished_tail_is_not_counted_and_marks_partial_sum(tmp_path, tail):
    result = summarize_usage([task(tmp_path, "plan", [receipt("valid")], tail=tail)])
    assert result["total"]["responses"] == 1
    assert not result["coverage"]["complete"]
    assert codes(result) == {"truncated_history"}


def test_invalid_lines_and_receipts_preserve_valid_partial_sum(tmp_path):
    invalid = receipt("corrected", inputs=-1)
    row = task(tmp_path, "plan", [invalid, receipt("corrected")])
    with open(row["history_path"], "ab") as stream:
        stream.write(b"invalid json\n[]\n")
    result = summarize_usage([row])
    assert result["total"]["responses"] == 1
    assert codes(result) == {"invalid_usage_record", "invalid_json", "invalid_record"}
    assert not result["coverage"]["complete"]


def test_malformed_duplicate_does_not_make_an_invoked_task_measurable(tmp_path):
    result = summarize_usage(
        [
            task(tmp_path, "plan", [receipt("shared")]),
            task(tmp_path, "prepare", [receipt("shared", inputs=-1)]),
        ]
    )
    assert result["total"]["responses"] == 1
    assert not result["coverage"]["complete"]
    assert not result["coverage"]["measurable"]
    assert "invalid_usage_record" in codes(result)


@pytest.mark.parametrize("identity", [None, "wrong-thread"])
def test_session_identity_required_before_charging_receipts(tmp_path, identity):
    row = task(tmp_path, "plan", [receipt("one")])
    events = [receipt("one")]
    if identity is not None:
        events.insert(0, {"type": "session_meta", "payload": {"id": identity}})
    with open(row["history_path"], "w") as stream:
        stream.writelines(json.dumps(event) + "\n" for event in events)
    result = summarize_usage([row])
    assert result["total"] == zero_usage()
    assert not result["coverage"]["complete"]
    assert not result["coverage"]["measurable"]
    assert codes(result) == {
        "missing_session_identity" if identity is None else "session_identity_mismatch"
    }


def test_shared_history_windows_attribute_calls_and_deduplicate(tmp_path):
    path = tmp_path / "shared.jsonl"
    chunks = [
        [
            {"type": "session_meta", "payload": {"id": "shared"}},
            receipt("plan"),
            {"type": "compacted"},
        ],
        [receipt("failed"), receipt("plan")],
        [receipt("eval")],
    ]
    positions = [0]
    with path.open("wb") as stream:
        for chunk in chunks:
            for event in chunk:
                stream.write(json.dumps(event).encode() + b"\n")
            positions.append(stream.tell())
    roster = [
        {
            "stage": stage,
            "thread_id": "shared",
            "history_path": str(path),
            "invoked": True,
            "history_start_bytes": positions[i],
            "history_end_bytes": positions[i + 1],
            "outcome": "failed" if i == 1 else "ok",
        }
        for i, stage in enumerate(("plan", "prepare", "evaluate"))
    ]
    result = summarize_usage(roster)
    assert result["coverage"]["complete"]
    assert result["coverage"]["duplicate_responses"] == 1
    assert result["total"]["total_tokens"] == 39
    assert [
        result["by_stage"][stage]["responses"]
        for stage in ("plan", "prepare", "evaluate")
    ] == [1, 1, 1]


@pytest.mark.parametrize(
    "kind", ["overlap", "whole", "partial", "past_end", "negative"]
)
def test_invalid_shared_windows_are_reported(tmp_path, kind):
    first = task(tmp_path, "plan", [receipt("one")])
    size = Path(first["history_path"]).stat().st_size
    first.update(history_start_bytes=0, history_end_bytes=size)
    second = dict(
        first, stage="prepare", history_start_bytes=size, history_end_bytes=size
    )
    if kind == "overlap":
        second["history_start_bytes"] = 0
    elif kind == "whole":
        del second["history_start_bytes"], second["history_end_bytes"]
    elif kind == "partial":
        second["history_start_bytes"] = size - 1
    elif kind == "past_end":
        second["history_end_bytes"] = size + 1
    else:
        second["history_start_bytes"] = -1
    result = summarize_usage([first, second])
    assert not result["coverage"]["complete"]
    assert "invalid_history_window" in codes(result)


def test_uninvoked_task_not_read_and_explicit_zero_receipt_is_measured(tmp_path):
    row = task(tmp_path, "prepare", [receipt("must-not-read", inputs=999)])
    row["invoked"] = False
    result = summarize_usage(
        [task(tmp_path, "plan", [receipt("zero", 0, 0, 0, 0)]), row]
    )
    assert result["total"]["total_tokens"] == 0
    assert result["total"]["responses"] == 1
    assert result["coverage"]["complete"]
    assert result["coverage"]["measurable"]
    assert result["coverage"]["histories_read"] == 1


def test_empty_buckets_are_independent():
    result = summarize_usage([])
    result["by_stage"]["execute"]["input_tokens"] = 100
    assert result["total"] == zero_usage()
    assert result["unassigned"] == zero_usage()
    assert result["by_stage"]["plan"] == zero_usage()


def test_roles_contexts_and_coordinator_reconcile_unique_responses(tmp_path):
    plan = task(tmp_path, "plan", [receipt("shared"), receipt("planning")])
    assistant = task(tmp_path, "prepare", [receipt("shared"), receipt("assistant")])
    assistant.update(
        stage="plan", role="assistant", context="shared-context", call_id="c2"
    )
    plan.update(role="coordinator", context="shared-context", call_id="c1")
    result = summarize_usage([plan, assistant])
    assert result["coverage"]["complete"]
    assert result["total"]["responses"] == 3
    assert result["by_stage"]["plan"]["responses"] == 3
    assert result["by_role"]["coordinator"]["responses"] == 2
    assert result["by_role"]["assistant"]["responses"] == 1
    assert result["by_context"]["shared-context"] == result["total"]
    assert result["coordinator"] == {
        "present": True,
        "model_calls": 2,
        "usage": result["by_role"]["coordinator"],
    }
    assert result["coverage"]["tasks"][1]["call_id"] == "c2"
    assert result["coverage"]["tasks"][1]["context"] == "shared-context"


def test_legacy_role_defaults_to_stage_and_windows_are_traced(tmp_path):
    row = task(tmp_path, "plan", [receipt("one")])
    row.update(
        history_start_bytes=0,
        history_end_bytes=Path(row["history_path"]).stat().st_size,
    )
    result = summarize_usage([row])
    assert result["by_role"]["plan"] == result["total"]
    assert result["by_context"] == {}
    assert result["coverage"]["tasks"][0]["role"] == "plan"
    assert (
        result["coverage"]["tasks"][0]["history_end_bytes"] == row["history_end_bytes"]
    )


@pytest.mark.parametrize(
    ("settlement", "expected_issue"),
    [(False, "unsettled_runtime"), ("yes", "invalid_settlement_evidence")],
)
def test_unsettled_or_invalid_settlement_preserves_observed_usage(
    tmp_path, settlement, expected_issue
):
    row = task(tmp_path, "plan", [receipt("observed")])
    row["settlement_complete"] = settlement
    result = summarize_usage([row])
    assert result["total"]["responses"] == 1
    assert result["total"]["total_tokens"] == 13
    assert result["coverage"]["measurable"]
    assert not result["coverage"]["complete"]
    assert codes(result) == {expected_issue}
    assert result["coverage"]["tasks"][0]["settlement_complete"] == settlement
