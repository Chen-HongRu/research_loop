"""Synthetic trace fixtures; never include real studies or agent transcripts."""

import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "router_packets", Path(__file__).parents[1] / "benchmarks/router_packets.py"
)
packets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packets)


def at(second):
    return f"2026-01-01T00:{second // 60:02d}:{second % 60:02d}Z"


def row(second, subtype, **payload):
    return {
        "timestamp": at(second),
        "type": "response_item",
        "payload": {"type": subtype, **payload},
    }


def receipt(second, rid):
    return {
        "timestamp": at(second),
        "type": "token_usage_record",
        "payload": {"response_id": rid},
    }


def user(second=11, text="initial request"):
    return row(
        second, "message", role="user", content=[{"type": "input_text", "text": text}]
    )


def fixture(root, events, extra_status=None):
    status = dict(
        iteration=1,
        status="iteration-completed",
        thread_id="lead",
        started_at=at(10),
        ended_at=at(200),
        human_request={"id": "request", "text": "admitted request"},
    )
    status.update(extra_status or {})
    target = root / "iterations/000001/status.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(status))
    (root / "goal.md").write_text("synthetic goal")
    trace = root / "native/sessions/lead.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text(
        "".join(
            json.dumps(e) + "\n"
            for e in [
                {"timestamp": at(0), "type": "session_meta", "payload": {"id": "lead"}},
                *events,
            ]
        )
    )
    return root


def input_text(packet):
    # Every future-leak sentinel assertion covers both independently bounded views.
    return json.dumps([packet["input"], packet["assessment_input"]])


def test_first_input_parallel_siblings_and_current_tool_output_do_not_leak(tmp_path):
    root = fixture(
        tmp_path,
        [
            user(),
            row(
                12,
                "message",
                role="assistant",
                content=[{"text": "CURRENT_COMMENTARY_SENTINEL"}],
            ),
            row(
                13,
                "function_call",
                name="delegate_task",
                arguments="CURRENT_ACTION_SENTINEL",
                call_id="a",
            ),
            row(
                14,
                "function_call",
                name="delegate_task",
                arguments="PARALLEL_SIBLING_SENTINEL",
                call_id="b",
            ),
            row(
                15,
                "function_call_output",
                call_id="a",
                output="CURRENT_TOOL_OUTPUT_SENTINEL",
            ),
            receipt(16, "response-1"),
            row(
                17,
                "message",
                role="assistant",
                content=[{"text": "NEXT_FINAL_SENTINEL"}],
            ),
            receipt(18, "response-2"),
        ],
        {
            "result": {"verdict": "FINAL_RESULT_SENTINEL"},
            "roles": "FINAL_ROLES_SENTINEL",
        },
    )
    (root / "events.jsonl").write_text(
        json.dumps(
            {
                "at": at(12),
                "event": "role.task.finished",
                "result": "LATE_CHILD_SENTINEL",
            }
        )
        + "\n"
    )
    report = packets.extract_round(root, 1)
    first, second = report["packets"]
    assert first["id"] == "round-000001-op-0001"
    assert len(first["baseline_action"]["items"]) == 3
    assert first["grouping_status"] == "receipt_terminated"
    assert "initial request" in input_text(first)
    for sentinel in [
        "CURRENT_COMMENTARY",
        "CURRENT_ACTION",
        "PARALLEL_SIBLING",
        "CURRENT_TOOL_OUTPUT",
        "NEXT_FINAL",
        "FINAL_RESULT",
        "FINAL_ROLES",
        "LATE_CHILD",
    ]:
        assert sentinel not in input_text(first)
    assert "CURRENT_TOOL_OUTPUT_SENTINEL" in input_text(second)
    assert "NEXT_FINAL_SENTINEL" not in input_text(second)
    assert "LATE_CHILD_SENTINEL" not in input_text(second)
    assert "FINAL_RESULT_SENTINEL" not in input_text(second)
    assert [e["id"] for e in second["input"]["evidence"]] == ["e00001", "e00002"]


def test_no_route_wait_is_preserved_and_unchanged_pending_is_merged(tmp_path):
    root = fixture(
        tmp_path,
        [
            user(),
            row(12, "function_call", name="functions.exec", input="poll", call_id="a"),
            receipt(13, "r1"),
            row(
                14,
                "function_call_output",
                call_id="a",
                output='{"status":"running","session_id":1,"output":"","wall_time_seconds":1}',
            ),
            row(15, "function_call", name="functions.exec", input="poll", call_id="b"),
            receipt(16, "r2"),
            row(
                17,
                "function_call_output",
                call_id="b",
                output='{"status":"running","session_id":1,"output":"","wall_time_seconds":2}',
            ),
            row(18, "function_call", name="functions.exec", input="poll", call_id="c"),
            receipt(19, "r3"),
            row(
                20,
                "function_call_output",
                call_id="c",
                output='{"status":"completed","session_id":1,"output":"new result"}',
            ),
            row(
                21,
                "message",
                role="assistant",
                content=[{"text": "No further delegation is needed."}],
            ),
            receipt(22, "r4"),
        ],
    )
    report = packets.extract_round(root, 1)
    assert len(report["packets"]) == 3
    assert report["coverage"]["merged_pending_inputs"] == 1
    assert report["packets"][1]["baseline_action"]["response_id"] == "r2"
    assert report["packets"][2]["baseline_action"]["response_id"] == "r4"
    assert "new result" in input_text(report["packets"][2])


def test_changed_pending_and_visible_agent_deliveries_are_inputs(tmp_path):
    root = fixture(
        tmp_path,
        [
            user(),
            row(12, "custom_tool_call", name="exec", input="poll", call_id="a"),
            receipt(13, "r1"),
            row(
                14,
                "custom_tool_call_output",
                call_id="a",
                output="Process running; new progress",
            ),
            row(15, "agent_message", message="visible child delivery"),
            row(16, "message", role="assistant", content=[{"text": "action"}]),
            receipt(17, "r2"),
        ],
    )
    report = packets.extract_round(root, 1)
    assert len(report["packets"]) == 2
    assert len(report["packets"][1]["input"]["evidence"]) == 3
    assert "visible child delivery" in input_text(report["packets"][1])


def test_opaque_reasoning_and_unterminated_or_interleaved_groups_are_unknown(tmp_path):
    root = fixture(
        tmp_path,
        [
            user(),
            row(12, "reasoning", encrypted_content="CIPHER_SENTINEL"),
            row(13, "custom_tool_call", name="exec", input="first", call_id="a"),
            row(14, "custom_tool_call_output", call_id="a", output="intervening input"),
            row(15, "custom_tool_call", name="exec", input="second", call_id="b"),
            receipt(16, "r1"),
            row(17, "custom_tool_call_output", call_id="b", output="next input"),
            row(18, "message", role="assistant", content=[{"text": "last"}]),
        ],
    )
    result = packets.extract_round(root, 1)
    assert [p["grouping_status"] for p in result["packets"]] == ["unknown", "unknown"]
    assert result["packets"][0]["baseline_action"]["opaque_reasoning"]
    assert "CIPHER_SENTINEL" not in input_text(result["packets"][0])
    assert "intervening input" not in input_text(result["packets"][0])
    assert "unterminated_response_group" in result["coverage"]["issues"]


def test_receipt_before_output_does_not_prove_a_group(tmp_path):
    root = fixture(
        tmp_path,
        [
            user(),
            receipt(12, "early"),
            row(13, "function_call", name="wait_tasks", arguments="{}"),
        ],
    )
    result = packets.extract_round(root, 1)
    assert result["packets"][0]["grouping_status"] == "unknown"
    assert result["coverage"]["orphan_receipts"] == 1


def test_bounded_recent_evidence_and_deterministic_output(tmp_path):
    root = fixture(
        tmp_path,
        [
            user(text="old" * 1000),
            row(12, "function_call", name="read", call_id="a"),
            receipt(13, "r1"),
            row(14, "function_call_output", call_id="a", output="latest" * 1000),
            row(15, "message", role="assistant", content=[{"text": "action"}]),
            receipt(16, "r2"),
        ],
    )
    result = packets.extract_round(root, 1, max_chars=512)
    assert result == packets.extract_round(root, 1, max_chars=512)
    second = result["packets"][1]
    assert second["prefix_truncated"]
    assert len(json.dumps(second["input"], ensure_ascii=False)) <= 512
    assert second["input"]["evidence"][-1]["text"].endswith("latest")
    assert second["input"]["evidence"][-1]["id"] == "e00002"


@pytest.mark.parametrize(
    "change",
    [
        {"status": "running"},
        {"thread_id": None},
        {"iteration": 2},
        {"ended_at": at(10)},
    ],
)
def test_rejects_noncompleted_or_malformed_rounds(tmp_path, change):
    root = fixture(tmp_path, [user()], change)
    with pytest.raises(ValueError):
        packets.extract_round(root, 1)


def test_cli_is_read_only_and_requires_new_external_file(tmp_path):
    root = fixture(
        tmp_path / "source",
        [
            user(),
            row(12, "message", role="assistant", content=[{"text": "final"}]),
            receipt(13, "r1"),
        ],
    )
    output = tmp_path / "packets.json"
    before = {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    packets.main(["extract", str(root), "--iteration", "1", "--output", str(output)])
    assert json.loads(output.read_text())["coverage"]["packet_count"] == 1
    assert before == {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    for target in [output, root / "new.json"]:
        with pytest.raises(SystemExit):
            packets.main(
                ["extract", str(root), "--iteration", "1", "--output", str(target)]
            )


def test_candidate_reduction_has_richer_blind_assessment_and_coverage(tmp_path):
    root = fixture(
        tmp_path,
        [
            user(text="original evidence " * 150),
            row(12, "function_call", name="read", call_id="a"),
            receipt(13, "r1"),
            row(
                14, "function_call_output", call_id="a", output="recent evidence " * 150
            ),
            row(
                15, "message", role="assistant", content=[{"text": "CURRENT_SENTINEL"}]
            ),
            receipt(16, "r2"),
        ],
    )
    result = packets.extract_round(root, 1, max_chars=512)
    second = result["packets"][1]
    assert second["prefix_truncated"] and not second["assessment_truncated"]
    candidate_ids = {e["id"] for e in second["input"]["evidence"]}
    assessment_ids = {e["id"] for e in second["assessment_input"]["evidence"]}
    assert candidate_ids < assessment_ids
    assert "CURRENT_SENTINEL" not in input_text(second)
    assert result["coverage"]["candidate_dropped_evidence_items"] == 1
    assert result["coverage"]["assessment_dropped_evidence_items"] == 0
    capped = packets.extract_round(root, 1, max_chars=512, max_assessment_chars=512)
    assert capped["packets"][1]["assessment_truncated"]
    assert capped["coverage"]["assessment_prefix_truncated_packets"] == 2
    with pytest.raises(ValueError, match="assessment prefix limit"):
        packets.extract_round(root, 1, max_chars=1000, max_assessment_chars=512)


@pytest.mark.parametrize("terminal", ["failed", "stopped"])
def test_closed_failure_attempts_keep_partial_packets_without_receipts(
    tmp_path, terminal
):
    root = fixture(
        tmp_path,
        [user(), row(12, "function_call", name="read", call_id="a")],
        {"status": terminal},
    )
    result = packets.extract_round(root, 1)
    assert result["status"] == terminal
    assert result["packets"][0]["grouping_status"] == "unknown"
    assert result["coverage"]["receipts"] == 0
    assert result["coverage"]["issues"] == [
        "ambiguous_response_grouping",
        "unterminated_response_group",
    ]


def test_native_agent_message_event_is_current_assistant_output_not_input(tmp_path):
    root = fixture(
        tmp_path,
        [
            user(),
            {
                "timestamp": at(12),
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "OWN_CURRENT_OUTPUT_SENTINEL",
                },
            },
            row(
                13,
                "message",
                role="assistant",
                content=[{"text": "OWN_CURRENT_OUTPUT_SENTINEL"}],
            ),
            receipt(14, "r1"),
        ],
    )
    result = packets.extract_round(root, 1)
    assert len(result["packets"]) == 1
    assert "OWN_CURRENT_OUTPUT_SENTINEL" not in input_text(result["packets"][0])
    assert result["coverage"]["visible_inputs"] == 1


def test_unrenderable_inbound_input_marks_only_subsequent_packets_incomplete(tmp_path):
    root = fixture(
        tmp_path,
        [
            user(),
            row(12, "function_call", name="read", call_id="a"),
            receipt(13, "r1"),
            row(14, "agent_message", encrypted_content="INBOUND_CIPHER_SENTINEL"),
            row(
                15, "message", role="assistant", content=[{"text": "CURRENT_SENTINEL"}]
            ),
            receipt(16, "r2"),
        ],
    )
    result = packets.extract_round(root, 1)
    assert [p["input_incomplete"] for p in result["packets"]] == [False, True]
    assert result["coverage"]["visible_inputs"] == 1
    assert result["coverage"]["incomplete_input_packets"] == 1
    assert len(result["coverage"]["unrenderable_input_refs"]) == 1
    assert "unrenderable_visible_input" in result["coverage"]["issues"]
    for packet in result["packets"]:
        assert "INBOUND_CIPHER_SENTINEL" not in input_text(packet)
        assert all(e["text"].strip() for e in packet["input"]["evidence"])


def test_reasoning_receipt_alone_does_not_prove_a_visible_action_group(tmp_path):
    root = fixture(
        tmp_path,
        [user(), row(12, "reasoning", encrypted_content="opaque"), receipt(13, "r1")],
    )
    packet = packets.extract_round(root, 1)["packets"][0]
    assert packet["grouping_status"] == "unknown"
    assert packet["baseline_action"]["opaque_reasoning"]


def test_orphan_receipt_taints_following_action_group_until_new_visible_input(tmp_path):
    root = fixture(
        tmp_path,
        [
            user(),
            receipt(12, "early"),
            row(13, "function_call", name="read", call_id="a"),
            receipt(14, "later"),
            row(15, "function_call_output", call_id="a", output="new explicit input"),
            row(16, "function_call", name="read", call_id="b"),
            receipt(17, "bound"),
        ],
    )
    result = packets.extract_round(root, 1)
    assert [p["grouping_status"] for p in result["packets"]] == [
        "unknown",
        "receipt_terminated",
    ]
    assert result["coverage"]["orphan_receipts"] == 1
    assert result["packets"][0]["baseline_action"]["response_id"] is None
    assert result["packets"][0]["baseline_action"]["receipt_response_id"] == "later"


def test_nested_encrypted_or_nontext_message_content_is_never_rendered_as_evidence(
    tmp_path,
):
    root = fixture(
        tmp_path,
        [
            user(),
            row(
                12,
                "agent_message",
                content={"encrypted_content": "NESTED_CIPHER_SENTINEL"},
            ),
            row(
                13, "message", role="assistant", content=[{"text": "CURRENT_SENTINEL"}]
            ),
            receipt(14, "r1"),
        ],
    )
    result = packets.extract_round(root, 1)
    packet = result["packets"][0]
    assert packet["input_incomplete"]
    assert "NESTED_CIPHER_SENTINEL" not in input_text(packet)
    assert len(packet["input"]["evidence"]) == 1
