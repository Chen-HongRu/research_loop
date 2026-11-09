"""Public-message projection checks; no model, remote data or network calls."""

import json
import io
import subprocess
from unittest.mock import patch

import pytest

from research_loop.dashboard import server


def test_metrics_projection_and_endpoint(tmp_path):
    initialize(tmp_path)
    metrics = tmp_path / "external-metrics"
    phase = metrics / "baseline"
    phase.mkdir(parents=True)
    (tmp_path / "metrics").symlink_to(metrics, target_is_directory=True)
    (metrics / "current").symlink_to("baseline", target_is_directory=True)
    (phase / "latest.json").write_text(
        json.dumps(
            {
                "phase": "baseline",
                "last_source_event_at": "2026-01-01T12:00:00Z",
                "window": {"until": "now", "secret": "PRIVATE"},
                "coverage": {"complete": False, "issues": {"PRIVATE": 1}},
                "llm": {
                    "total": {"input_tokens": 10, "secret": "PRIVATE"},
                    "by_role": {"scout": {"wait_timeouts": 2}},
                    "by_task": [
                        {
                            "role": "scout",
                            "active_in_phase": True,
                            "completed_at": "now",
                            "mixed": False,
                        }
                    ],
                },
                "resources": {"failed_calls": 1},
                "reader": {"bytes_read": 5},
                "secret": "PRIVATE",
            }
        )
    )
    (metrics / "comparison.json").write_text(
        json.dumps(
            {
                name: {
                    "phase": name,
                    "task_cohorts": {"completed_unmixed": 1, "secret": "PRIVATE"},
                    "totals": {"llm": {"input_tokens": 3}},
                }
                for name in ("baseline", "optimized")
            }
        )
    )
    result = server.metrics_snapshot(tmp_path)
    assert result["available"] and result["coverage"]["complete"] is False
    assert result["last_source_event_at"] == "2026-01-01T12:00:00Z"
    assert result["tasks"] == {"completed": 1, "pending": 0, "mixed": 0}
    assert result["wait_timeouts"] == 2
    assert result["comparison"]["baseline"]["llm"]["input_tokens"] == 3
    assert "PRIVATE" not in json.dumps(result)
    handler_type = server.handler_for(server.State(tmp_path), {"localhost"})
    for path in ("/api/metrics", "/api/state"):
        handler = object.__new__(handler_type)
        handler.path, handler.headers, handler.wfile = (
            path,
            {"Host": "localhost"},
            io.BytesIO(),
        )
        handler.send_response = lambda code: None
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.do_GET()
        assert isinstance(json.loads(handler.wfile.getvalue()), dict)


def test_metrics_missing_invalid_oversize_and_escape(tmp_path):
    assert server.metrics_snapshot(tmp_path) == {
        "available": False,
        "status": "missing",
    }
    metrics = tmp_path / "metrics"
    current = metrics / "current"
    current.mkdir(parents=True)
    latest = current / "latest.json"
    for data in ("[]", "{", "x" * (server.METRICS_LIMIT + 1)):
        latest.write_text(data)
        assert server.metrics_snapshot(tmp_path)["status"] == "invalid"
    latest.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text('{"phase":"x","llm":{}}')
    latest.symlink_to(outside)
    assert server.metrics_snapshot(tmp_path)["status"] == "invalid"
    latest.unlink()
    current.rmdir()
    current.symlink_to(tmp_path, target_is_directory=True)
    (tmp_path / "latest.json").write_text(outside.read_text())
    assert server.metrics_snapshot(tmp_path)["status"] == "invalid"


def test_service_pid_overrides_stale_record_and_failed_queries_stay_unknown():
    saved = {"pid": 123, "service": "research-example.service"}
    with patch.object(server.shutil, "which", return_value="/usr/bin/systemctl"):
        with patch.object(
            server.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 0, "MainPID=456\nActiveState=active\n"
            ),
        ):
            assert server.supervisor_process(saved) == (456, "alive")
        with patch.object(
            server.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 0, "MainPID=0\nActiveState=failed\n"
            ),
        ):
            assert server.supervisor_process(saved) == (None, "dead")
        with patch.object(
            server.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("systemctl", 2),
        ):
            assert server.supervisor_process(saved) == (None, "unknown")


def append(path, *values):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        for value in values:
            stream.write(json.dumps(value) + "\n")


def item(value, method="item/completed"):
    return {
        "at": "2026-01-01T12:00:00Z",
        "event": "codex.event",
        "method": method,
        "data": {"threadId": "lead", "item": value},
    }


def initialize(root):
    (root / "status.json").write_text(
        json.dumps({"status": "running", "thread_id": "lead"})
    )


def test_milestones_survive_polling_and_private_reasoning_is_not_projected(tmp_path):
    initialize(tmp_path)
    log = tmp_path / "events.jsonl"
    append(
        log,
        item(
            {
                "id": "first",
                "type": "agentMessage",
                "phase": "commentary",
                "text": "首个真实进展",
            }
        ),
    )
    for number in range(400):
        append(
            log,
            {
                "event": "resource.tool",
                "tool": "gpu_poll",
                "success": True,
                "output": {"running": True, "n": number},
            },
        )
    append(
        log,
        item({"id": "private", "type": "reasoning", "text": "PRIVATE_REASONING"}),
        item(
            {
                "id": "partial",
                "type": "agentMessage",
                "phase": "partial_answer",
                "text": "阶段性结论",
            }
        ),
    )
    state = server.State(tmp_path)
    result = state.snapshot()
    assert [m["text"] for m in result["messages"]] == ["首个真实进展", "阶段性结论"]
    assert len(result["tools"]) == 250
    assert "PRIVATE_REASONING" not in json.dumps(result)
    assert state.snapshot()["stats"] == result["stats"]


def test_child_responsibility_result_and_followup_have_separate_states(tmp_path):
    initialize(tmp_path)
    path = tmp_path / "native/sessions/2026/01/01/child.jsonl"
    meta = {
        "type": "session_meta",
        "payload": {
            "id": "child",
            "source": {
                "subagent": {
                    "thread_spawn": {
                        "parent_thread_id": "lead",
                        "agent_path": "/root/plan_review",
                        "agent_nickname": "Ada",
                    }
                }
            },
        },
    }
    reply = {
        "type": "response_item",
        "timestamp": "2026-01-01T12:01:00Z",
        "payload": {
            "type": "message",
            "id": "reply",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "建议先补一个对照。"}],
        },
    }
    append(
        path,
        meta,
        {"type": "event_msg", "payload": {"type": "task_started"}},
        {
            "type": "response_item",
            "payload": {"type": "reasoning", "content": "PRIVATE_CHILD_REASONING"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "agent_message",
                "content": [
                    {
                        "type": "encrypted_content",
                        "encrypted_content": "gAAAAA_NOT_A_PUBLIC_TASK",
                    }
                ],
            },
        },
        reply,
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn",
                "last_agent_message": "建议先补一个对照。",
            },
        },
    )
    append(
        tmp_path / "events.jsonl",
        item(
            {
                "type": "subAgentActivity",
                "agentThreadId": "child",
                "agentPath": "/root/plan_review",
                "kind": "completed",
            }
        ),
        item(
            {
                "type": "subAgentActivity",
                "agentThreadId": "child",
                "agentPath": "/root/plan_review",
                "kind": "interrupted",
            }
        ),
    )
    state = server.State(tmp_path)
    result = state.snapshot()
    child = result["agents"][0]
    assert child["title"] == "方案审查"
    assert child["name"] == "Ada" and child["status"] == "completed"
    assert child["result"] == "建议先补一个对照。" and len(child["messages"]) == 1
    assert "PRIVATE_CHILD_REASONING" not in json.dumps(result)
    assert "gAAAAA" not in json.dumps(result)
    append(path, {"type": "event_msg", "payload": {"type": "task_started"}})
    assert state.snapshot()["agents"][0]["status"] == "working"


def test_reader_waits_for_complete_lines_and_resets_on_replacement(tmp_path):
    path = tmp_path / "events"
    path.write_bytes(b'{"message":"one"}\n{"message":')
    reader = server.Lines(path)
    assert reader.drain()[0] == [{"message": "one"}]
    assert reader.drain()[0] == []
    with path.open("ab") as stream:
        stream.write(b'"two"}\n')
    assert reader.drain()[0] == [{"message": "two"}]
    replacement = tmp_path / "replacement"
    replacement.write_text('{"message":"new"}\n')
    replacement.replace(path)
    assert reader.drain() == ([{"message": "new"}], True)


def test_native_and_resource_views_are_one_operation_and_preserve_failure(tmp_path):
    initialize(tmp_path)
    log = tmp_path / "events.jsonl"
    append(
        log,
        item(
            {
                "type": "dynamicToolCall",
                "id": "call",
                "tool": "gpu_exec",
                "status": "inProgress",
                "arguments": {"command": "synthetic"},
            },
            "item/started",
        ),
        {
            "event": "resource.tool",
            "tool": "gpu_exec",
            "success": False,
            "arguments": {"command": "synthetic"},
            "output": {"error": "SyntheticFailure"},
        },
        item(
            {
                "type": "dynamicToolCall",
                "id": "call",
                "tool": "gpu_exec",
                "status": "completed",
                "success": False,
                "arguments": {"command": "synthetic"},
            }
        ),
    )
    state = server.State(tmp_path)
    result = state.snapshot()
    assert len(result["tools"]) == 1
    assert result["tools"][0]["status"] == "failed"
    assert "output" not in result["tools"][0]
    assert "input" not in result["tools"][0]
    assert state.tool_detail("call")["output"] == {"error": "SyntheticFailure"}
    assert state.tool_detail("missing") is None


def test_missing_native_history_does_not_invent_a_task_result(tmp_path):
    initialize(tmp_path)
    append(
        tmp_path / "events.jsonl",
        item(
            {
                "type": "subAgentActivity",
                "agentThreadId": "child",
                "agentPath": "/root/probe_script",
                "kind": "started",
            }
        ),
    )
    child = server.State(tmp_path).snapshot()["agents"][0]
    assert child["title"] == "诊断脚本实现"
    assert child["status"] == "working"
    assert child["result"] is None and child["messages"] == []


def test_explicit_roles_do_not_reclassify_legacy_tasks():
    assert server.task_role("scout_alternatives") == "Scout"
    assert server.task_role("gca_Analyst_readout") == "Analyst"
    assert server.task_role("experimenter-state") == "Experimenter"
    assert server.task_role("plan_review") == "unknown"
    assert server.task_role("probe_script") == "unknown"
    assert server.task_role("critic_proposer") == "unknown"


def test_current_meta_records_take_precedence_over_old_generic_records(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    for name in ("coordination", "frontier"):
        (work / f"{name}.md").write_text("Old generic record")
        (work / f"{name}9.md").write_text("Actual current team record")
    research = server.research_state(tmp_path, {"iteration": 9})
    for name in ("coordination", "frontier"):
        assert research[name]["path"] == f"work/{name}9.md"
        assert research[name]["text"] == "Actual current team record"


def test_research_result_keeps_iteration_provenance_and_actual_work_text(tmp_path):
    initialize(tmp_path)
    (tmp_path / "status.json").write_text(
        json.dumps({"iteration": 8, "status": "running"})
    )
    iteration = tmp_path / "iterations/000007"
    iteration.mkdir(parents=True)
    (iteration / "result.json").write_text(
        json.dumps(
            {
                "summary": "Observed result",
                "next_action": "Proposed next test",
                "reasoning": "PRIVATE_RESULT_REASONING",
                "encrypted_content": "gAAAAA_HIDDEN",
            }
        )
    )
    work = tmp_path / "work"
    work.mkdir()
    (work / "iteration8-plan.md").write_text("# Actual plan\nUnstructured draft")
    research = server.State(tmp_path).snapshot()["research"]
    assert research["summary"]["iteration"] == 7
    assert research["summary"]["stale"] is True
    assert research["summary"]["provisional"] is True
    assert research["summary"]["source"] == "iterations/000007/result.json#summary"
    assert research["next_action"]["source"].endswith("#next_action")
    assert research["coordination"]["path"] == "work/iteration8-plan.md"
    assert research["coordination"]["text"] == "# Actual plan\nUnstructured draft"
    assert research["frontier"]["available"] is False
    assert "PRIVATE_RESULT_REASONING" not in json.dumps(research)
    assert "gAAAAA" not in json.dumps(research)
    (tmp_path / "status.json").write_text(json.dumps({"iteration": 7}))
    assert server.State(tmp_path).snapshot()["research"]["summary"]["stale"] is False


def test_artifacts_reject_symlinks_and_bound_content(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    private = tmp_path / "private.md"
    private.write_text("PRIVATE_OUTSIDE_WORK")
    (work / "coordination.md").symlink_to(private)
    result = server.research_state(tmp_path, {"iteration": 1})
    assert result["coordination"]["available"] is False
    assert "PRIVATE_OUTSIDE_WORK" not in json.dumps(result)
    (work / "frontier.md").write_text("x" * (server.ARTIFACT_LIMIT + 1000))
    frontier = server.research_state(tmp_path, {})["frontier"]
    assert frontier["available"] is True
    assert len(frontier["text"]) < server.ARTIFACT_LIMIT + 100
    assert "截断" in frontier["text"]
    (work / "frontier.md").unlink()
    work.rename(tmp_path / "actual-work")
    work.symlink_to(tmp_path / "actual-work", target_is_directory=True)
    assert server.research_state(tmp_path, {})["frontier"]["available"] is False


def test_agent_iteration_comes_from_session_start_not_latest_activity(tmp_path):
    initialize(tmp_path)
    for iteration, start in ((1, "2026-01-01T11:00:00Z"), (2, "2026-01-01T12:00:00Z")):
        path = tmp_path / f"iterations/{iteration:06d}/status.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"iteration": iteration, "started_at": start}))
    append(
        tmp_path / "native/sessions/2026/01/01/child.jsonl",
        {
            "type": "session_meta",
            "timestamp": "2026-01-01T11:30:00Z",
            "payload": {
                "id": "child",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": "lead",
                            "agent_path": "/root/critic_design",
                        }
                    }
                },
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T12:30:00Z",
            "payload": {"type": "task_complete"},
        },
    )
    child = server.State(tmp_path).snapshot()["agents"][0]
    assert child["iteration"] == 1
    assert child["started_at"] == "2026-01-01T11:30:00Z"
    assert child["role"] == "Critic"
    assert child["fresh_context"] == "unknown"


def host_mapping(root, status="completed"):
    (root / "roles.json").write_text(
        json.dumps(
            {
                "version": 1,
                "threads": {
                    "host-child": {
                        "role": "critic",
                        "task": "inspect-result",
                        "task_id": "critic-001",
                        "status": status,
                    }
                },
            }
        )
    )


def host_history(root):
    append(
        root / "native/sessions/2026/01/01/host.jsonl",
        {
            "type": "session_meta",
            "timestamp": "2026-01-01T12:01:00Z",
            "payload": {"id": "host-child", "source": "cli"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "content": "PRIVATE_HOST_REASONING",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "agent_message",
                "content": [
                    {
                        "type": "encrypted_content",
                        "encrypted_content": "PRIVATE_ENCRYPTED",
                    }
                ],
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T12:02:00Z",
            "payload": {
                "type": "task_complete",
                "last_agent_message": "Synthetic verdict.",
            },
        },
    )


@pytest.mark.parametrize(
    "status,expected",
    [
        ("queued", "pending"),
        ("running", "working"),
        ("completed", "completed"),
        ("failed", "failed"),
    ],
)
def test_host_role_uses_explicit_identity_and_host_terminal_status(
    tmp_path, status, expected
):
    initialize(tmp_path)
    host_mapping(tmp_path, status)
    host_history(tmp_path)
    # An independent, unmapped CLI thread must not become a Study delegate.
    append(
        tmp_path / "native/sessions/2026/01/01/unrelated.jsonl",
        {
            "type": "session_meta",
            "payload": {"id": "unrelated", "source": "cli"},
        },
    )
    result = server.State(tmp_path).snapshot()
    assert len(result["agents"]) == 1
    child = result["agents"][0]
    assert child["id"] == "host-child"
    assert child["task_name"] == "inspect-result"
    assert child["role"] == "Critic"
    assert child["role_source"] == "host-role-record"
    assert child["status"] == expected
    assert child["result"] == "Synthetic verdict."
    assert "PRIVATE_" not in json.dumps(result)


def test_native_and_host_roles_coexist_without_duplicate_cards(tmp_path):
    initialize(tmp_path)
    host_mapping(tmp_path)
    host_history(tmp_path)
    for tid in ("native-child", "host-child"):
        append(
            tmp_path / "events.jsonl",
            item(
                {
                    "type": "subAgentActivity",
                    "kind": "completed",
                    "agentThreadId": tid,
                    "agentPath": "/root/scout-sources",
                }
            ),
        )
    append(
        tmp_path / "native/sessions/2026/01/01/native.jsonl",
        {
            "type": "session_meta",
            "payload": {
                "id": "native-child",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": "lead",
                            "agent_path": "/root/scout-sources",
                        }
                    },
                },
            },
        },
    )
    result = server.State(tmp_path).snapshot()
    assert result["stats"]["agents"] == 2
    cards = {c["id"]: c for c in result["agents"]}
    native = cards["native-child"]
    assert native["parent_id"] == "lead"
    assert native["role"] == "Scout"
    assert native["role_source"] == "inferred-from-task-name"
    assert native["status"] == "completed"
    assert cards["host-child"]["role"] == "Critic"


def test_host_role_without_history_has_original_iteration_and_refreshes(tmp_path):
    initialize(tmp_path)
    for n, start in ((1, "2026-01-01T11:00:00Z"), (2, "2026-01-01T13:00:00Z")):
        path = tmp_path / f"iterations/{n:06d}/status.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"iteration": n, "started_at": start}))
    append(
        tmp_path / "events.jsonl",
        {
            "event": "role.task.started",
            "at": "2026-01-01T12:01:00Z",
            "thread_id": "host-child",
        },
        {
            "event": "role.task.finished",
            "at": "2026-01-01T13:30:00Z",
            "thread_id": "host-child",
        },
    )
    state = server.State(tmp_path)
    assert not state.snapshot()["agents"]
    host_mapping(tmp_path, "running")
    child = state.snapshot()["agents"][0]
    assert child["iteration"] == 1
    assert child["status"] == "working"
    assert child["result"] is None
    host_mapping(tmp_path, "failed")
    host_history(tmp_path)
    child = state.snapshot()["agents"][0]
    assert child["iteration"] == 1
    assert child["status"] == "failed"
    assert child["result"] == "Synthetic verdict."


@pytest.mark.parametrize(
    "content",
    [
        "{",
        "[]",
        '{"threads": []}',
        '{"threads": {"bad": null, "invalid": {"task": 1}}}',
        '{"threads": {"bad": {"task": "x", "role": [], "status": []}}}',
        '{"threads": {"native-child": {"task": "broken", "role": "invalid", "status": "completed"}}}',
        '{"threads": {"native-child": {"task": "broken", "role": "critic", "status": "invalid"}}}',
    ],
)
def test_invalid_host_metadata_preserves_native_cards(tmp_path, content):
    initialize(tmp_path)
    append(
        tmp_path / "events.jsonl",
        item(
            {
                "type": "subAgentActivity",
                "kind": "completed",
                "agentThreadId": "native-child",
                "agentPath": "/root/critic-review",
            }
        ),
    )
    state = server.State(tmp_path)
    before = state.snapshot()["agents"]
    (tmp_path / "roles.json").write_text(content)
    assert state.snapshot()["agents"] == before
