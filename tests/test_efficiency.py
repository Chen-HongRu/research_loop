import json

import pytest

from research_loop.efficiency import main, report


START = "2026-01-01T00:01:00Z"
END = "2026-01-01T00:03:00Z"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def rows(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(v) + "\n" for v in values))


def session(path, tid, parent=None, task="root", events=()):
    source = (
        {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
        if parent
        else "cli"
    )
    rows(
        path,
        [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": tid, "agent_path": f"/root/{task}", "source": source},
            },
            *events,
        ],
    )


def token(at, inputs, cached, output, reasoning):
    return {
        "timestamp": at,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": inputs,
                    "cached_input_tokens": cached,
                    "output_tokens": output,
                    "reasoning_output_tokens": reasoning,
                }
            },
        },
    }


def receipt(rid, at, inputs, cached=0, output=0, reasoning=0):
    return {
        "timestamp": at,
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


def configure(root, resource):
    write_json(root / "active.json", {"revision": "r1"})
    write_json(
        root / "configurations/r1/config.json",
        {
            "revision": "r1",
            "profiles": {"coordination": {"model": "gpt-6.1-sol", "effort": "medium"}},
            "router": {"profile": "coordination"},
            "resource": resource,
        },
    )


@pytest.fixture
def study(tmp_path):
    write_json(tmp_path / "status.json", {"started_at": START})
    write_json(tmp_path / "run.json", {"id": "run-one", "created_at": START})
    write_json(
        tmp_path / "roles.json",
        {"threads": {"router": {"role": "router", "task": "routing"}}},
    )
    configure(tmp_path, {"ledger_path": str(tmp_path / "gpu-budget.json")})
    rows(
        tmp_path / "events.jsonl",
        [{"at": START, "event": "run.started", "thread_id": "router"}],
    )
    write_json(tmp_path / "gpu-budget.json", {"entries": []})
    return tmp_path


def test_usage_window_dedup_reconciliation_and_tree(study):
    before = token("2026-01-01T00:00:30Z", 100, 30, 20, 10)
    during = token("2026-01-01T00:02:00Z", 160, 50, 30, 15)
    record = {
        "timestamp": "2026-01-01T00:02:05Z",
        "type": "event_msg",
        "payload": {
            "type": "token_usage_record",
            "response_id": "response",
            "usage": {
                "input_tokens": 80,
                "cached_input_tokens": 20,
                "output_tokens": 12,
                "reasoning_output_tokens": 5,
            },
        },
    }
    session(
        study / "groups/router/one/native/sessions/lead.jsonl",
        "router",
        events=[
            before,
            during,
            during,
            record,
            record,
            {"timestamp": "2026-01-01T00:02:06Z", "type": "compacted"},
            token(END, 999, 50, 50, 15),
        ],
    )
    session(
        study / "groups/router/one/native/sessions/child.jsonl",
        "child",
        "router",
        "critic_one",
        [
            token("2026-01-01T00:02:00Z", 10, 0, 3, 1),
            receipt(
                "child-response", "2026-01-01T00:02:05Z", 10, output=3, reasoning=1
            ),
        ],
    )
    session(
        study / "groups/router/one/native/sessions/other.jsonl",
        "other",
        events=[token("2026-01-01T00:02:00Z", 9999, 0, 0, 0)],
    )
    result = report(study, until=END)
    total = result["llm"]["total"]
    assert total == {
        "input_tokens": 90,
        "cached_input_tokens": 20,
        "uncached_input_tokens": 70,
        "output_tokens": 15,
        "reasoning_output_tokens": 6,
        "responses": 2,
        "duplicate_responses": 1,
        "compactions": 1,
    }
    assert (
        result["llm"]["reconciliation"]["record_minus_cumulative"]["input_tokens"] == 20
    )
    assert result["llm"]["reconciliation"]["unique_response_records"] == 2
    assert result["llm"]["reconciliation"]["cumulative_usage"]["input_tokens"] == 70
    assert result["coverage"]["excluded_session_threads"] == 1
    assert result["coverage"]["issues"]["sessions:unmapped_threads"] == 1


def test_resource_tools_agents_ledger_and_truncated_tail(study):
    poll = {
        "at": START,
        "event": "resource.tool",
        "thread_id": "router",
        "tool": "gpu_poll",
        "success": True,
        "output": {"running": True, "output": ""},
    }
    with (study / "events.jsonl").open("a") as stream:
        stream.write(json.dumps(poll) + "\n")
        stream.write(json.dumps({**poll, "success": False}) + "\n")
        stream.write('{"at":')
    events = [
        {
            "timestamp": START,
            "type": "response_item",
            "payload": {"type": "function_call", "name": name, "call_id": name},
        }
        for name in ["spawn_agent", "wait_agent", "sleep"]
    ]
    session(
        study / "groups/router/one/native/sessions/a.jsonl", "router", events=events
    )
    session(
        study / "groups/router/one/native/sessions/b.jsonl",
        "child",
        "router",
        events=[
            {
                "timestamp": START,
                "type": "event_msg",
                "payload": {"type": "task_complete"},
            }
        ],
    )
    write_json(
        study / "gpu-budget.json",
        {
            "entries": [
                {
                    "dispatched_at": START,
                    "status": "TERMINAL",
                    "receipt": {"state": "CANCELLED", "elapsed_seconds": 9},
                },
                {
                    "dispatched_at": START,
                    "status": "UNKNOWN",
                    "receipt": {"state": "COMPLETED", "elapsed_seconds": 999},
                },
            ]
        },
    )
    result = report(study, since=START, until=END)
    assert result["resources"]["calls"] == 2
    assert result["resources"]["failed_calls"] == 1
    assert result["resources"]["successful_empty_running_gpu_polls"] == 1
    assert result["resources"]["output_json_bytes"] > 0
    assert result["agents"]["spawn_calls"] == 1
    assert result["agents"]["wait_sleep_calls"] == 2
    assert result["agents"]["descendant_threads_with_task_complete"] == 1
    assert result["allocation"]["elapsed_seconds"] == 9
    assert result["allocation"]["unknown_jobs"] == 1
    assert result["coverage"]["issues"]["events:incomplete_tail"] == 1
    assert not result["coverage"]["complete"]


def test_counter_reset_is_reported_without_negative_usage(study):
    session(
        study / "groups/router/one/native/sessions/a.jsonl",
        "router",
        events=[
            token("2026-01-01T00:00:00Z", 100, 0, 10, 0),
            token(START, 10, 0, 1, 0),
            token("2026-01-01T00:02:00Z", 20, 0, 2, 0),
        ],
    )
    result = report(study, until=END)
    assert result["llm"]["total"]["input_tokens"] == 0
    assert result["llm"]["reconciliation"]["cumulative_usage"]["input_tokens"] == 10
    assert not result["llm"]["measurable"]
    assert result["coverage"]["usage_records_missing_threads"] == ["router"]
    assert result["coverage"]["issues"]["sessions:cumulative_counter_decrease"] == 1


def test_streamed_output_without_usage_is_unknown_cost_not_awaiting(study):
    session(study / "groups/router/one/native/sessions/a.jsonl", "router")
    with (study / "events.jsonl").open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "at": "2026-01-01T00:02:00Z",
                    "event": "codex.event",
                    "method": "item/agentMessage/delta",
                    "data": {"threadId": "router", "delta": " " * 100},
                }
            )
            + "\n"
        )
    result = report(study, until=END)
    assert not result["llm"]["measurable"]
    assert not result["coverage"]["complete"]
    assert result["coverage"]["usage_records_missing_threads"] == ["router"]
    assert result["coverage"]["awaiting_first_usage_threads"] == []


def test_nonresource_review_callback_does_not_require_resource_ledger(study):
    (study / "gpu-budget.json").unlink()
    configure(study, None)
    session(
        study / "groups/router/one/native/sessions/a.jsonl",
        "router",
        events=[receipt("reviewed", START, 100, output=10)],
    )
    with (study / "events.jsonl").open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "at": START,
                    "event": "resource.tool",
                    "thread_id": "router",
                    "tool": "synthetic_readonly_review",
                    "resource_effect": False,
                    "success": True,
                    "output": {"verdict": "accept"},
                }
            )
            + "\n"
        )
    result = report(study, until=END)
    assert result["coverage"]["complete"]
    assert "ledger:missing" not in result["coverage"]["issues"]


def test_missing_and_invalid_sources_are_visible(tmp_path):
    (tmp_path / "events.jsonl").write_text("not json\n")
    result = report(tmp_path, until=END)
    assert result["window"]["since"] is None
    assert not result["coverage"]["complete"]
    assert result["coverage"]["issues"]["events:invalid_json"] == 1
    assert not result["allocation"]["available"]


def test_timezone_required_and_cli(study, capsys):
    with pytest.raises(ValueError, match="timezone"):
        report(study, since="2026-01-01T00:01:00", until=END)
    with pytest.raises(ValueError, match="earlier"):
        report(study, since=END, until=START)
    session(study / "groups/router/one/native/sessions/a.jsonl", "router")
    main([str(study), "--until", END])
    assert json.loads(capsys.readouterr().out)["schema_version"] == 2


def test_wait_pairing_and_task_timestamps(study):
    def tool(at, kind, **payload):
        return {
            "timestamp": at,
            "type": "response_item",
            "payload": {"type": kind, **payload},
        }

    session(
        study / "groups/router/one/native/sessions/a.jsonl",
        "router",
        events=[
            tool(
                "2026-01-01T00:00:30Z",
                "function_call",
                name="wait_agent",
                call_id="prior",
            ),
            tool(START, "function_call", name="wait_agent", call_id="one"),
            tool(
                "2026-01-01T00:01:05Z",
                "function_call_output",
                call_id="one",
                output=json.dumps({"timed_out": True}),
            ),
            tool(
                "2026-01-01T00:01:05Z",
                "function_call_output",
                call_id="one",
                output=json.dumps({"timed_out": True}),
            ),
            tool(
                "2026-01-01T00:01:10Z",
                "function_call",
                name="wait_agent",
                call_id="two",
            ),
            tool(
                "2026-01-01T00:01:12Z",
                "function_call_output",
                call_id="two",
                output=json.dumps({"message": "timed_out: true"}),
            ),
            tool("2026-01-01T00:01:15Z", "function_call", name="sleep", call_id="open"),
            tool(END, "function_call_output", call_id="open", output="{}"),
            {
                "timestamp": "2026-01-01T00:02:00Z",
                "type": "event_msg",
                "payload": {"type": "task_complete"},
            },
        ],
    )
    result = report(study, until=END)
    task = result["llm"]["by_task"][0]
    assert task["started_at"] == "2026-01-01T00:00:00+00:00"
    assert task["first_tool_at"] == "2026-01-01T00:00:30+00:00"
    assert task["completed_at"] == "2026-01-01T00:02:00+00:00"
    assert task["waits_by_tool"]["wait_agent"] == {
        "calls": 2,
        "returned_calls": 2,
        "observed_wait_seconds": 7,
        "wait_agent_timeouts": 1,
    }
    assert task["waits_by_tool"]["sleep"]["returned_calls"] == 0
    assert result["agents"]["waits_by_tool"]["wait_agent"]["observed_wait_seconds"] == 7


def test_optional_ledger_and_not_dispatched(study):
    session(study / "groups/router/one/native/sessions/a.jsonl", "router")
    (study / "gpu-budget.json").unlink()
    configure(study, None)
    result = report(study, until=END)
    assert not result["allocation"]["available"]
    assert result["coverage"]["complete"]
    configure(study, {"ledger_path": str(study / "gpu-budget.json")})
    result = report(study, until=END)
    assert result["coverage"]["issues"]["ledger:missing"] == 1
    write_json(
        study / "gpu-budget.json",
        {
            "entries": [
                {
                    "dispatched_at": START,
                    "status": "TERMINAL",
                    "receipt": {"state": "NOT_DISPATCHED", "elapsed_seconds": 0},
                },
                {
                    "dispatched_at": START,
                    "job_id": "123",
                    "status": "TERMINAL",
                    "receipt": {"state": "CANCELLED", "elapsed_seconds": 2},
                },
            ]
        },
    )
    allocation = report(study, until=END)["allocation"]
    assert allocation["reservations"] == 2
    assert allocation["dispatched_jobs"] == 1
    assert allocation["not_dispatched"] == 1
    assert allocation["nonterminal_jobs"] == 0
    assert allocation["elapsed_seconds"] == 2


def test_frozen_ledger_and_explicit_snapshot_override(study, tmp_path, capsys):
    session(study / "groups/router/one/native/sessions/a.jsonl", "router")

    def ledger(seconds):
        return {
            "entries": [
                {
                    "dispatched_at": START,
                    "job_id": "123",
                    "status": "TERMINAL",
                    "receipt": {"state": "COMPLETED", "elapsed_seconds": seconds},
                }
            ]
        }

    external = tmp_path / "external/ledger.json"
    snapshot = tmp_path / "snapshot/ledger.json"
    write_json(external, ledger(9))
    write_json(snapshot, ledger(4))
    write_json(study / "gpu-budget.json", ledger(999))
    configure(study, {"ledger_path": str(external)})
    result = report(study, until=END)
    assert result["allocation"]["elapsed_seconds"] == 9
    assert str(external) not in json.dumps(result)
    assert (
        report(study, until=END, ledger_path=snapshot)["allocation"]["elapsed_seconds"]
        == 4
    )
    external.unlink()
    result = report(study, until=END)
    assert not result["allocation"]["available"]
    assert result["coverage"]["issues"]["ledger:missing"] == 1
    main([str(study), "--until", END, "--ledger-path", str(snapshot)])
    assert json.loads(capsys.readouterr().out)["allocation"]["elapsed_seconds"] == 4


def test_host_role_mapping_attributes_unparented_child_threads(study):
    session(
        study / "groups/router/one/native/sessions/lead.jsonl",
        "router",
        parent=None,
        task="root",
    )
    session(
        study / "groups/router/one/native/sessions/host.jsonl",
        "child",
        parent=None,
        task="root",
        events=[
            token("2026-01-01T00:02:00Z", 10, 0, 5, 0),
            receipt("host-response", "2026-01-01T00:02:01Z", 10, output=5),
        ],
    )
    write_json(
        study / "roles.json",
        {
            "version": 1,
            "threads": {
                "router": {"role": "router", "task": "routing"},
                "child": {
                    "role": "scout",
                    "task": "scout-frontier",
                    "task_id": "scout-001",
                    "status": "completed",
                },
            },
        },
    )
    out = report(study)
    assert out["coverage"]["selected_threads"] == 2
    assert out["llm"]["by_role"]["scout"]["input_tokens"] == 10
    assert out["llm"]["by_role"]["scout"]["uncached_input_tokens"] == 10


def test_current_metadata_default_window_and_explicit_tool_outcomes(study):
    write_json(study / "status.json", {"started_at": START, "ended_at": END})
    # No coordinator input event or singleton status thread is needed.
    rows(study / "events.jsonl", [])
    events = []
    outcomes = [
        {"success": False},
        {"exit_code": 0},
        {"exit_code": 2},
        "error appears in ordinary scientific text",
        {"isError": True},
    ]
    for i, output in enumerate(outcomes):
        events.extend(
            [
                {
                    "timestamp": START,
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "read_file",
                        "call_id": str(i),
                    },
                },
                {
                    "timestamp": "2026-01-01T00:02:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": str(i),
                        "output": output,
                    },
                },
            ]
        )
    events.append(receipt("structured", "2026-01-01T00:02:01Z", 10))
    events.sort(key=lambda event: event["timestamp"])
    session(
        study / "groups/router/one/native/sessions/router.jsonl",
        "router",
        events=events,
    )
    result = report(study)
    assert result["window"]["since"] == "2026-01-01T00:01:00+00:00"
    assert result["window"]["until"] == "2026-01-01T00:03:00+00:00"
    assert result["configuration"]["revision"] == "r1"
    assert result["agents"]["tool_outcomes"] == {
        "returned_calls": 5,
        "failed_calls": 3,
        "unknown_outcome_calls": 1,
        "failed_by_tool": {"read_file": 3},
    }
    assert result["coverage"]["complete"]


def test_resource_events_do_not_require_a_model_thread_identity(study):
    session(study / "groups/router/one/native/sessions/router.jsonl", "router")
    rows(
        study / "events.jsonl",
        [
            {
                "at": START,
                "event": "resource.tool",
                "tool": "gpu_exec",
                "success": False,
                "output": {"error": "synthetic transport failure"},
            }
        ],
    )
    result = report(study, until=END)
    assert result["resources"]["calls"] == result["resources"]["failed_calls"] == 1


def test_active_revision_path_is_validated_before_reading(study):
    write_json(study / "active.json", {"revision": "../escape"})
    with pytest.raises(ValueError, match="revision"):
        report(study, until=END)


def command_receipt(*, exit_code=0, output="synthetic stdout"):
    return {
        "chunk_id": "synthetic-chunk",
        "wall_time_seconds": 0.01,
        "exit_code": exit_code,
        "original_token_count": 4,
        "output": output,
    }


def text_block(value):
    return {"type": "input_text", "text": json.dumps(value)}


def test_nested_exec_receipts_are_separate_from_outer_outcomes_and_match_collector(
    study,
):
    from research_loop.metrics import Collector

    # The script itself succeeds while a search-like command returns nonzero.
    # Command stdout deliberately resembles another receipt and must remain opaque.
    payload = {
        "type": "custom_tool_call_output",
        "call_id": "exec-one",
        "success": True,
        "output": [
            {"type": "input_text", "text": "Script completed"},
            text_block(
                command_receipt(
                    exit_code=1, output=json.dumps(command_receipt(exit_code=7))
                )
            ),
            text_block({"status": "fulfilled", "value": command_receipt()}),
            text_block(command_receipt()),
        ],
    }
    session(
        study / "groups/router/one/native/sessions/router.jsonl",
        "router",
        events=[
            {
                "timestamp": START,
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "exec-one",
                    "input": "synthetic bounded command",
                },
            },
            {
                "timestamp": "2026-01-01T00:02:00Z",
                "type": "response_item",
                "payload": payload,
            },
            receipt("nested-usage", "2026-01-01T00:02:01Z", 10),
        ],
    )
    offline = report(study, until=END)["agents"]["tool_outcomes"]
    assert offline == {
        "returned_calls": 1,
        "failed_calls": 0,
        "unknown_outcome_calls": 0,
        "failed_by_tool": {},
        "nested_command_receipts": {
            "observed_receipts": 3,
            "zero_exit_receipts": 2,
            "nonzero_exit_receipts": 1,
        },
    }
    with Collector(
        study, study.parent / "nested-metrics", since=START, phase="nested"
    ) as collector:
        first = collector.poll(END)
        assert first["tools"] == offline
        assert collector.poll(END)["tools"] == offline
    with Collector(
        study, study.parent / "nested-metrics", since=START, phase="nested"
    ) as collector:
        assert collector.poll(END)["tools"] == offline


@pytest.mark.parametrize(
    "output",
    [
        [text_block({"exit_code": 1})],
        [text_block({"status": "rejected", "value": command_receipt(exit_code=1)})],
        [{"type": "output_text", "text": json.dumps(command_receipt(exit_code=1))}],
        [text_block(command_receipt(exit_code=True))],
        [text_block({**command_receipt(), "wall_time_seconds": float("nan")})],
        [text_block({"wrapper": command_receipt(exit_code=1)})],
        [{"type": "input_text", "text": "command failed with exit_code=1"}],
    ],
)
def test_nested_parser_rejects_unrecognized_text_and_metadata(output):
    from research_loop.efficiency import record_tool_outcome, tool_outcomes

    result = tool_outcomes()
    record_tool_outcome(result, "exec", {"output": output})
    assert result["unknown_outcome_calls"] == 1
    assert "nested_command_receipts" not in result


def test_receipt_shaped_text_from_another_tool_is_not_a_nested_exec_receipt():
    from research_loop.efficiency import record_tool_outcome, tool_outcomes

    result = tool_outcomes()
    record_tool_outcome(
        result, "read_file", {"output": [text_block(command_receipt(exit_code=1))]}
    )
    assert result["failed_calls"] == 0
    assert "nested_command_receipts" not in result
