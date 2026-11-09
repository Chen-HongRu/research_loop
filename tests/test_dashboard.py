"""Public-message projection checks; no model, remote data or network calls."""

import json

from research_loop.dashboard import server


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
