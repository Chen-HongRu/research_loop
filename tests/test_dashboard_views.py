"""Bounded dashboard projections with synthetic local Study records."""

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from research_loop.dashboard import server
from research_loop.state import PublicationStore


def write(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def append(root, name, values):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value) + "\n" for value in values))


def request(studies, path):
    handler_type = server.handler_for(studies, {"localhost"})
    handler = object.__new__(handler_type)
    handler.path, handler.headers, handler.wfile = (
        path,
        {"Host": "localhost"},
        io.BytesIO(),
    )
    response, headers = [], {}
    handler.send_response = response.append
    handler.send_error = lambda code, *args: response.append(code)
    handler.send_header = lambda key, value: headers.update({key: value})
    handler.end_headers = lambda: None
    handler.do_GET()
    body = handler.wfile.getvalue()
    return response[0], json.loads(body) if body else None, headers


@pytest.fixture
def long_study(tmp_path):
    for number in range(1, 51):
        write(
            tmp_path,
            f"iterations/{number:06d}/status.json",
            {
                "iteration": number,
                "status": "stopped" if number == 50 else "iteration-completed",
                "started_at": f"2026-01-01T00:{number:02d}:00Z",
                "ended_at": f"2026-01-01T00:{number:02d}:59Z",
                "result": {"summary": "EMBEDDED_STATUS_RESULT"},
                "resources": ["STATUS_RESOURCE_DETAIL"],
            },
        )
        if number < 50:
            write(
                tmp_path,
                f"iterations/{number:06d}/result.json",
                {
                    "summary": f"Result {number}: " + "s" * 400,
                    "next_action": "Recorded next test",
                    "limitations": ["FULL_RESULT_LIMITATION"],
                    "evidence": ["work/report.md#observed"],
                },
            )
    write(
        tmp_path,
        "status.json",
        {
            "status": "stopped",
            "iteration": 50,
            "thread_id": "router",
            "started_at": "2026-01-01T00:50:00Z",
            "ended_at": "2026-01-01T00:50:59Z",
            "result": {"summary": "EMBEDDED_STATUS_RESULT"},
            "resources": ["STATUS_RESOURCE_DETAIL"],
            "error": {
                "type": "SyntheticError",
                "message": "Stopped for test",
                "extra": "PRIVATE",
            },
        },
    )
    (tmp_path / "goal.md").write_text("FULL_GOAL_SENTINEL")
    (tmp_path / "work").mkdir()
    (tmp_path / "work/coordination50.md").write_text("FULL_WORK_SENTINEL")
    roles = {}
    events = []
    for number in range(65):
        identifier = f"child-{number:03d}"
        roles[identifier] = {
            "task": f"inspect-{number}",
            "role": "critic" if number % 2 else "scout",
            "status": "completed" if number % 2 else "running",
        }
        if number < 60:
            events.append(
                {
                    "event": "role.task.started",
                    "thread_id": identifier,
                    "at": "2026-01-01T00:50:30Z"
                    if number < 40
                    else "2026-01-01T00:49:30Z",
                }
            )
    write(tmp_path, "roles.json", {"version": 1, "threads": roles})
    for number in range(300):
        events.extend(
            [
                {
                    "event": "codex.event",
                    "at": "2026-01-01T00:50:30Z",
                    "method": "item/completed",
                    "data": {
                        "item": {
                            "id": f"message-{number}",
                            "type": "agentMessage",
                            "phase": "commentary",
                            "text": f"Message {number}: "
                            + "m" * 400
                            + "MESSAGE_DETAIL_SENTINEL",
                        }
                    },
                },
                {
                    "event": "resource.tool",
                    "tool": "gpu_status",
                    "success": True,
                    "arguments": {"detail": "TOOL_INPUT_SENTINEL"},
                    "output": {"detail": "TOOL_OUTPUT_SENTINEL"},
                },
            ]
        )
    append(tmp_path, "events.jsonl", events)
    append(
        tmp_path,
        "groups/router/one/native/sessions/2026/01/01/child.jsonl",
        [
            {
                "type": "session_meta",
                "timestamp": "2026-01-01T00:50:30Z",
                "payload": {"id": "child-000", "source": "cli"},
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "last_agent_message": "t" * 400 + "TASK_DETAIL_SENTINEL",
                },
            },
        ],
    )
    return tmp_path


def test_overview_is_lean_before_card_and_artifact_serialization(long_study):
    state = server.State(long_study)
    original_card, original_read = server.Session.card, Path.read_text

    def lean_card(self, *args, **kwargs):
        assert kwargs.get("detail") is False
        return original_card(self, *args, **kwargs)

    def no_context(path, *args, **kwargs):
        assert path.name != "goal.md" and "work" not in path.parts
        return original_read(path, *args, **kwargs)

    with (
        patch.object(server.Session, "card", lean_card),
        patch.object(
            server.State, "snapshot", side_effect=AssertionError("Full snapshot")
        ),
        patch.object(Path, "read_text", no_context),
    ):
        value = state.overview()
    assert value["status"]["status"] == "stopped"
    assert value["status"]["iteration"] == 50
    assert value["status"]["error"] == {
        "type": "SyntheticError",
        "message": "Stopped for test",
    }
    assert value["research"]["summary"]["iteration"] == 49
    assert value["research"]["summary"]["stale"] is True
    assert value["stats"] == {"agents": 40, "working": 20, "completed": 20}
    serialized = json.dumps(value)
    for excluded in (
        "SENTINEL",
        "EMBEDDED_STATUS_RESULT",
        "STATUS_RESOURCE_DETAIL",
        "FULL_RESULT_LIMITATION",
        "PRIVATE",
    ):
        assert excluded not in serialized
    assert set(value) == {
        "configuration",
        "as_of",
        "status",
        "last_event_at",
        "activity",
        "budget",
        "research",
        "publication",
        "stats",
    }


def test_fifty_iteration_history_pages_and_selected_detail(long_study):
    studies = server.Studies([long_study])
    with patch.object(
        server, "research_history", side_effect=AssertionError("Full history")
    ):
        status, page, headers = request(studies, "/api/history?page=1")
        assert status == 200 and headers["Cache-Control"] == "no-store"
        assert [row["iteration"] for row in page["iterations"]] == list(
            range(50, 40, -1)
        )
        assert page["counts"] == {"total": 50, "with_result": 49, "missing_result": 1}
        assert page["page_size"] == 10 and page["total_pages"] == 5
        assert page["iterations"][0]["result_available"] is False
        assert all(len(row["summary_preview"]) <= 240 for row in page["iterations"])
        assert "FULL_RESULT_LIMITATION" not in json.dumps(page)
        assert "result" not in page["iterations"][1]
        status, last, _ = request(studies, "/api/history?page=5")
        assert status == 200 and last["iterations"][-1]["iteration"] == 1
        assert request(studies, "/api/history?page=6")[1]["iterations"] == []
    status, detail, _ = request(studies, "/api/iteration?iteration=49")
    assert status == 200 and detail["iteration"] == 49
    assert detail["result"]["limitations"] == ["FULL_RESULT_LIMITATION"]
    assert detail["result"]["evidence_refs"] == ["work/report.md#observed"]
    assert (
        request(studies, "/api/iteration?iteration=50")[1]["result_available"] is False
    )
    assert request(studies, "/api/iteration?iteration=51")[0] == 404


def test_task_filters_previews_details_and_unknown_associations(long_study):
    studies = server.Studies([long_study])
    status, page, _ = request(studies, "/api/agents")
    assert status == 200 and len(page["agents"]) == 20
    assert page["total"] == 40 and page["total_pages"] == 2
    assert page["current_iteration"] == 50
    for card in page["agents"]:
        assert not {"messages", "result", "purpose"} & set(card)
        assert len(card["preview"]) <= 240
        assert card["iteration_source"] == "inferred-from-start-time"
    assert request(studies, "/api/agents?iteration=49&role=critic")[1]["total"] == 10
    unknown = request(studies, "/api/agents?iteration=unknown")[1]
    assert unknown["total"] == 5 and all(
        c["iteration"] is None for c in unknown["agents"]
    )
    assert request(studies, "/api/agents?iteration=all")[1]["total"] == 65
    assert request(studies, "/api/agents?iteration=all&page=4")[1]["agents"]
    status, detail, _ = request(studies, "/api/agent?id=child-000")
    assert status == 200 and "TASK_DETAIL_SENTINEL" in detail["result"]
    assert detail["iteration"] == 50 and detail["messages"]
    assert detail["history_available"] is True
    stub = request(studies, "/api/agent?id=child-064")[1]
    assert stub["messages"] == [] and stub["result"] is None
    assert stub["task_detail_available"] is False
    assert stub["history_available"] is False
    assert request(studies, "/api/agent?id=missing")[0] == 404


def test_activity_paging_retention_and_public_message_detail(long_study):
    studies = server.Studies([long_study])
    status, page, _ = request(studies, "/api/activity")
    assert status == 200 and page["total_messages"] == 300
    assert page["total_tools"] == page["retention"] == 250
    assert len(page["messages"]) == len(page["tools"]) == 20
    assert page["messages"][0]["id"] == "message-299"
    assert all(len(m["text"]) <= 240 for m in page["messages"])
    assert "SENTINEL" not in json.dumps(page)
    detail = request(studies, "/api/message?id=message-299")[1]
    assert "MESSAGE_DETAIL_SENTINEL" in detail["text"]
    assert request(studies, "/api/message?id=absent")[0] == 404
    last = request(studies, "/api/activity?page=15")[1]
    assert len(last["messages"]) == 20 and last["tools"] == []
    selected_tool = page["tools"][0]["id"]
    assert "TOOL_OUTPUT_SENTINEL" in json.dumps(
        request(studies, f"/api/tool?id={selected_tool}")[1]
    )
    assert request(studies, "/api/tool?id=resource-2")[0] == 404


@pytest.mark.parametrize(
    "route",
    [
        "/api/history?page=0",
        "/api/history?page=-1",
        "/api/activity?page=1.5",
        "/api/agents?page=",
        "/api/history?page=1&page=2",
        "/api/agents?role=../invalid",
        "/api/agents?iteration=latest",
        "/api/iteration?iteration=../1",
        "/api/iteration?iteration=0",
        "/api/iteration",
        "/api/message",
        "/api/agent?id=",
    ],
)
def test_invalid_parameters_return_400(tmp_path, route):
    write(tmp_path, "status.json", {"status": "running"})
    assert request(server.Studies([tmp_path]), route)[0] == 400


def test_study_scope_and_symlink_iteration_selection(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for root in (a, b):
        write(root, "status.json", {"status": "completed"})
        write(root, "iterations/000002/result.json", {"summary": root.name})
    studies = server.Studies([a, b])
    selected = server.Studies.identifier(b)
    assert (
        request(studies, f"/api/iteration?study={selected}&iteration=2")[1]["result"][
            "summary"
        ]
        == "b"
    )
    assert request(studies, "/api/overview?study=unknown")[0] == 404
    assert request(studies, "/api/iteration?iteration=1")[0] == 404
    outside = tmp_path / "outside"
    write(outside, "result.json", {"summary": "DO_NOT_FOLLOW"})
    (a / "iterations/000001").symlink_to(outside, target_is_directory=True)
    assert request(studies, "/api/iteration?iteration=1")[0] == 404
    assert "DO_NOT_FOLLOW" not in json.dumps(request(studies, "/api/history")[1])


def test_publication_requires_matching_activated_knowledge(tmp_path):
    write(tmp_path, "iterations/000001/result.json", {"summary": "Reported result"})
    knowledge = PublicationStore(tmp_path)
    baseline = knowledge.prepare()
    assert (
        server.iteration_detail(tmp_path, 1)["publication"]["status"] == "unpublished"
    )
    # Metadata for a version that never became active does not certify publication.
    write(
        tmp_path,
        "outputs/publications/not-activated.json",
        {"iteration": 1, "publication_version": "not-activated"},
    )
    assert server.publication_state(tmp_path, 1)["status"] == "unpublished"
    version = knowledge.publish(
        baseline, publication={"iteration": 1, "status": "completed"}
    )
    assert server.publication_state(tmp_path, 1) == {
        "status": "published",
        "iteration": 1,
        "publication_version": version,
    }
    assert server.publication_state(tmp_path, 2)["status"] == "unpublished"
    assert server.publication_state(tmp_path, None)["status"] == "unknown"
    baseline = str(knowledge.current.resolve())
    newer = knowledge.publish(
        baseline, publication={"iteration": 2, "status": "completed"}
    )
    assert server.publication_state(tmp_path, 1)["status"] == "unknown"
    assert server.publication_state(tmp_path, 2)["publication_version"] == newer
    knowledge.current.unlink()
    knowledge.current.symlink_to(Path("versions") / version, target_is_directory=True)
    write(
        tmp_path,
        f"outputs/publications/{version}.json",
        {"iteration": 1, "publication_version": "wrong"},
    )
    assert server.publication_state(tmp_path, 1)["status"] == "invalid"


@pytest.mark.parametrize("terminal", ["completed", "iteration-completed"])
def test_historical_publication_matches_own_final_archive(tmp_path, terminal):
    knowledge = PublicationStore(tmp_path)
    baseline = knowledge.prepare()
    version = knowledge.publish(baseline, publication={"iteration": 1})
    write(
        tmp_path,
        "iterations/000001/status.json",
        {"iteration": 1, "status": terminal, "publication_version": version},
    )
    current = knowledge.publish(
        str(knowledge.current.resolve()), publication={"iteration": 2}
    )
    assert server.publication_state(tmp_path, 1) == {
        "status": "historical",
        "iteration": 1,
        "publication_version": version,
    }
    assert server.publication_state(tmp_path, 2) == {
        "status": "published",
        "iteration": 2,
        "publication_version": current,
    }


@pytest.mark.parametrize(
    "archived_status,version,metadata",
    [
        (None, "orphan", {"iteration": 1, "publication_version": "orphan"}),
        ("starting", "inherited", {"iteration": 1, "publication_version": "inherited"}),
        ("failed", "inherited", {"iteration": 1, "publication_version": "inherited"}),
        ("completed", "prior", {"iteration": 0, "publication_version": "prior"}),
        ("completed", "wrong", {"iteration": 1, "publication_version": "other"}),
        (
            "completed",
            "../outside",
            {"iteration": 1, "publication_version": "../outside"},
        ),
    ],
)
def test_historical_publication_does_not_certify_orphan_or_inherited_metadata(
    tmp_path, archived_status, version, metadata
):
    write(tmp_path, "iterations/000001/result.json", {"summary": "Reported"})
    if archived_status:
        write(
            tmp_path,
            "iterations/000001/status.json",
            {"iteration": 1, "status": archived_status, "publication_version": version},
        )
    write(tmp_path, f"outputs/publications/{version}.json", metadata)
    assert server.publication_state(tmp_path, 1)["status"] == "unknown"


@pytest.mark.parametrize("symlink", ["file", "publications", "outputs"])
def test_historical_publication_rejects_symlink_records(tmp_path, symlink):
    write(
        tmp_path,
        "iterations/000001/status.json",
        {"iteration": 1, "status": "completed", "publication_version": "old"},
    )
    record = write(
        tmp_path,
        "outputs/publications/old.json",
        {"iteration": 1, "publication_version": "old"},
    )
    path = (
        record
        if symlink == "file"
        else tmp_path / "outputs/publications"
        if symlink == "publications"
        else tmp_path / "outputs"
    )
    target = tmp_path / "outside"
    path.rename(target)
    path.symlink_to(target, target_is_directory=symlink != "file")
    assert server.publication_state(tmp_path, 1)["status"] == "unknown"


@pytest.mark.parametrize("iteration", [None, 2, True, "1"])
def test_historical_publication_requires_consistent_archive_iteration(
    tmp_path, iteration
):
    write(
        tmp_path,
        "iterations/000001/status.json",
        {"iteration": iteration, "status": "completed", "publication_version": "old"},
    )
    write(
        tmp_path,
        "outputs/publications/old.json",
        {"iteration": 1, "publication_version": "old"},
    )
    assert server.publication_state(tmp_path, 1)["status"] == "unknown"


@pytest.mark.parametrize(
    "version,authority,expected",
    [
        (1, None, "legacy"),
        (2, "unique-request-test", "unique-request-test"),
        (None, None, "unknown"),
        (2, None, "unknown"),
    ],
)
def test_metrics_version_authority_and_optional_behavior(
    tmp_path, version, authority, expected
):
    value = {
        "phase": "synthetic",
        "llm": {"authority": authority, "measurable": False},
        "coverage": {"complete": False},
    }
    if version is not None:
        value["schema_version"] = version
    write(tmp_path, "metrics/current/latest.json", value)
    projected = server.metrics_snapshot(tmp_path)
    assert projected["schema_version"] == version
    assert projected["llm_authority"] == expected
    assert projected["llm_measurable"] is False
    assert projected["coverage"]["complete"] is False
    assert projected["behavior"] == {"available": False, "status": "missing"}


def test_behavior_can_be_available_without_ordinary_metrics(tmp_path):
    path = write(
        tmp_path,
        "metrics/behavior/latest.json",
        {
            "schema_version": 2,
            "phase": "synthetic",
            "behavior": {
                "total": {"wait_calls": 3, "secret": "PRIVATE"},
                "children": {"tasks": 2},
                "by_task": [{"transcript": "PRIVATE"}],
                "coverage": {"nested_calls_visible": False},
            },
            "resource_presentations": {"status": "partial_metadata", "eligible": 0},
        },
    )
    projected = server.metrics_snapshot(tmp_path)
    assert projected["available"] is False and projected["status"] == "missing"
    assert projected["behavior"]["available"] is True
    assert projected["behavior"]["total"] == {"wait_calls": 3}
    assert "PRIVATE" not in json.dumps(projected)
    path.unlink()
    outside = write(tmp_path, "outside.json", {"behavior": {}})
    path.symlink_to(outside)
    assert server.metrics_snapshot(tmp_path)["behavior"]["status"] == "invalid"


@pytest.mark.parametrize("complete", [False, True])
def test_behavior_preserves_source_and_readiness_coverage(tmp_path, complete):
    write(
        tmp_path,
        "metrics/behavior/latest.json",
        {
            "coverage": {
                "complete": complete,
                "selected_threads": 4,
                "usage_records_observed": 12,
                "secret": "PRIVATE",
            },
            "behavior": {
                "coverage": {
                    "unmatched_readiness_records": 2,
                    "outputs_awaiting_usage": 1,
                    "nested_calls_visible": False,
                }
            },
        },
    )
    projected = server.metrics_snapshot(tmp_path)["behavior"]
    assert projected["coverage"] == {
        "complete": complete,
        "selected_threads": 4,
        "usage_records_observed": 12,
        "unmatched_readiness_records": 2,
        "outputs_awaiting_usage": 1,
        "nested_calls_visible": False,
    }
    assert "PRIVATE" not in json.dumps(projected)


@pytest.mark.parametrize(
    "status,label",
    [
        ("stopped", "本轮已结束"),
        ("failed", "本轮已结束"),
        ("interrupted", "本轮已结束"),
        ("completed", "本轮已结束"),
        ("needs-reconciliation", "本轮已结束"),
        ("initialized", "尚未开始"),
    ],
)
def test_activity_uses_terminal_status_without_end_timestamp(tmp_path, status, label):
    write(
        tmp_path,
        "status.json",
        {"status": status, "started_at": "2026-01-01T00:50:00Z"},
    )
    activity = server.State(tmp_path).overview()["activity"]
    assert activity == {"label": label, "state": "idle"}


def test_execution_endpoint_pages_real_trace_without_loading_native_history(tmp_path):
    from research_loop.records import ExecutionTrace

    write(tmp_path, "status.json", {"status": "running"})
    group = tmp_path / "group-runs/group-one"
    group.mkdir(parents=True)
    write(group, "run.json", {"run_id": "group-one", "status": "running"})
    trace = ExecutionTrace(group, run_id="group-one", round=1, revision="r1")
    parent = trace.begin("step", "branch")
    child = trace.begin("step", "evaluate", parent_step_id=parent)
    trace.handoff(parent, child, ["explicit-report.json"])
    studies = server.Studies([tmp_path])
    with patch.object(
        server.Session, "update", side_effect=AssertionError("Native history scan")
    ):
        status, page, _ = request(
            studies, "/api/execution?kind=steps&run_id=group-one&limit=1"
        )
        assert status == 200
        assert [record["id"] for record in page["items"]] == [parent]
        assert page["next_cursor"] == parent
        status, detail, _ = request(
            studies, f"/api/execution?kind=steps&run_id=group-one&id={child}"
        )
        assert status == 200 and detail["parent_step_id"] == parent
        status, transfers, _ = request(
            studies, "/api/execution?kind=handoffs&run_id=group-one"
        )
        assert status == 200 and transfers["items"][0]["sender"] == parent
    assert (
        request(studies, "/api/execution?kind=calls&run_id=group-one&id=missing")[0]
        == 404
    )
    assert request(studies, "/api/execution?kind=steps&run_id=../escape")[0] == 400
    assert request(studies, "/api/execution?kind=steps")[0] == 400


def test_current_revision_nested_agents_and_outputs_are_read_without_role_guessing(
    tmp_path,
):
    from research_loop.records import record_thread

    write(tmp_path, "status.json", {"status": "running", "iteration": 1})
    write(tmp_path, "active.json", {"revision": "r1"})
    write(
        tmp_path,
        "configurations/r1/config.json",
        {
            "revision": "r1",
            "router": {"profile": "coordination"},
            "profiles": {"coordination": {"model": "gpt-6.1-sol", "effort": "medium"}},
            "agents": {"custom": {"instructions": "PRIVATE_INSTRUCTION"}},
        },
    )
    record_thread(
        tmp_path,
        "independent",
        role="custom-evaluator",
        task="readout",
        group_run_id="group-one",
        config_revision="r1",
        status="completed",
    )
    append(
        tmp_path,
        "group-runs/group-one/native-runtime/review/custom/none/native/sessions/trace.jsonl",
        [
            {
                "type": "session_meta",
                "timestamp": "2026-01-01T00:01:00Z",
                "payload": {"id": "independent", "source": "cli"},
            },
            {
                "type": "response_item",
                "timestamp": "2026-01-01T00:02:00Z",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [
                        {"type": "output_text", "text": "Actual retained result"}
                    ],
                },
            },
        ],
    )
    store = PublicationStore(tmp_path)
    baseline = store.prepare()
    version = store.publish(baseline, publication={"iteration": 1})
    snapshot = server.State(tmp_path).snapshot()
    card = snapshot["agents"][0]
    assert card["role"] == "custom-evaluator" and card["parent_id"] is None
    assert card["result"] == "Actual retained result"
    assert card["group_run_id"] == "group-one"
    assert snapshot["configuration"]["revision"] == "r1"
    assert "PRIVATE_INSTRUCTION" not in json.dumps(snapshot)
    assert server.publication_state(tmp_path, 1)["publication_version"] == version
