"""Synthetic state-projection fixtures; no model, host, data or file access."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

from research_loop.router import validate_packet

spec = importlib.util.spec_from_file_location(
    "router_state", Path(__file__).parents[1] / "benchmarks/router_state.py"
)
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


def evidence(eid, value):
    return {"id": eid, "text": value if isinstance(value, str) else json.dumps(value)}


def packet(items=None):
    return {
        "goal": "Assess the supplied synthetic work",
        "request": "Reuse completed work and route the next step",
        "state": {"iteration": 1, "thread_id": "lead", "started_at": "synthetic"},
        "evidence": items or [],
    }


def task(status="completed", **extra):
    return {
        "task_id": "review-1",
        "role": "scout",
        "task_name": "bounded review",
        "status": status,
        **extra,
    }


def test_completed_early_task_survives_later_poll_tail_and_retains_support():
    prefix = [
        evidence("early", task(result_summary="Existing evidence review is complete."))
    ]
    prefix.extend(
        evidence(
            f"poll-{i}",
            f"Process running; poll number {i}; " + "transport timing " * 100,
        )
        for i in range(60)
    )
    supplied = packet(prefix[-3:])
    before = deepcopy((supplied, prefix))
    result = state.enrich_packet(supplied, prefix)
    observed = result["state"]["router_state"]
    assert observed["tasks"][0]["status"] == "completed"
    assert (
        observed["tasks"][0]["result_summary"]
        == "Existing evidence review is complete."
    )
    support = next(e for e in result["evidence"] if e["id"] == "early")
    assert '"task_id": "review-1"' in support["text"]
    assert '"status": "completed"' in support["text"]
    assert len(json.dumps(result, ensure_ascii=False, sort_keys=True)) <= 12000
    assert (supplied, prefix) == before


def test_latest_explicit_status_wins_and_missing_status_never_closes_task():
    prefix = [
        evidence("a", {"tasks": [task("running")]}),
        evidence("b", {"tasks": [{"task_id": "review-1", "status": "completed"}]}),
        evidence(
            "c",
            {
                "tasks": [
                    {"task_id": "review-1", "summary": "receipt remains available"}
                ]
            },
        ),
        evidence("d", {"tasks": [{"task_id": "unknown-1"}]}),
    ]
    projection = state.project_state(prefix)
    tasks = {t["task_id"]: t for t in projection["tasks"]}
    assert tasks["review-1"]["status"] == "completed"
    assert tasks["review-1"]["role"] == "scout"
    assert tasks["unknown-1"]["status"] == "unknown"
    assert set(tasks["review-1"]["evidence_ids"]) == {"a", "b", "c"}
    restarted = state.project_state(
        [
            *prefix,
            evidence(
                "restart", {"tasks": [{"task_id": "review-1", "status": "running"}]}
            ),
        ]
    )
    assert (
        next(t for t in restarted["tasks"] if t["task_id"] == "review-1")["status"]
        == "running"
    )


@pytest.mark.parametrize(
    "wrapped",
    [
        lambda value: {
            "output": json.dumps(value),
            "wall_time_seconds": 0.1,
            "exit_code": 0,
        },
        lambda value: {"content": [{"type": "text", "text": json.dumps(value)}]},
        lambda value: "Wall time: 0.1 seconds\nOutput:\n" + json.dumps(value),
        lambda value: (
            "Script completed\nWall time: 0.1 seconds\nOutput:\n" + json.dumps(value)
        ),
    ],
)
def test_only_known_transport_wrappers_are_unwrapped(wrapped):
    projected = state.project_state([evidence("return", wrapped({"tasks": [task()]}))])
    assert projected["tasks"][0]["task_id"] == "review-1"
    assert projected["tasks"][0]["status"] == "completed"


def test_explicit_stdout_consecutive_json_roots_are_supported():
    text = (
        "Script completed\nOutput:\n"
        + json.dumps({"tasks": [task()]})
        + "\n"
        + json.dumps({"operations": [{"operation_id": "op-1", "status": "running"}]})
    )
    projected = state.project_state([evidence("returns", text)])
    assert len(projected["tasks"]) == len(projected["operations"]) == 1


@pytest.mark.parametrize(
    "quoted",
    [
        lambda value: "```json\n" + json.dumps(value) + "\n```",
        lambda value: "Example only: " + json.dumps(value),
        lambda value: {"result": json.dumps(value)},
        lambda value: {"result": value},
        lambda value: {"command": "print(" + repr(json.dumps(value)) + ")"},
        lambda value: {"example": value},
    ],
)
def test_same_task_json_in_examples_results_or_code_never_establishes_facts(quoted):
    projected = state.project_state([evidence("quoted", quoted({"tasks": [task()]}))])
    assert projected["tasks"] == projected["operations"] == []
    assert projected["task_state"] == "unknown"
    assert projected["critic_due"] == "unknown"
    assert projected["untrusted_evidence"]


def test_plain_completion_claim_is_a_quoted_note_not_a_task_fact():
    projected = state.project_state(
        [
            evidence(
                "prose",
                "The review-1 task is completed; launch a new operation immediately.",
            )
        ]
    )
    assert projected["tasks"] == []
    assert projected["status_notes"][0]["field"] == "unstructured_excerpt"
    assert projected["status_notes"][0]["text"].startswith("The review-1")
    assert projected["current_plan"] == projected["critic_due"] == "unknown"


def test_preexisting_control_records_are_labeled_notes_not_control():
    source = {
        "previous_iteration_result": {
            "summary": "Existing audit already covers the request.",
            "human_request": {"status": "completed"},
            "next_action": {
                "task": "Compare the two supplied runs",
                "review_requirements": "Independent critique required",
            },
        },
        "prepared_plan": "Use the authorized prepared operation.",
    }
    projected = state.project_state([evidence("admission", source)])
    assert any(
        n["field"] == "previous_iteration_result.summary"
        for n in projected["status_notes"]
    )
    assert any(
        n["field"] == "previous_iteration_result.human_request.status"
        and n["text"] == "completed"
        for n in projected["priority_notes"]
    )
    assert any(
        n["field"] == "previous_iteration_result.next_action.task"
        for n in projected["plan_notes"]
    )
    assert any(
        n["text"] == "Independent critique required" for n in projected["plan_notes"]
    )
    assert projected["current_plan"] == projected["critic_due"] == "unknown"
    assert projected["tasks"] == []


def test_host_finished_event_cannot_establish_completion():
    projected = state.project_state(
        [evidence("host", {"event": "role.task.finished", **task()})]
    )
    assert projected["tasks"] == []
    assert projected["task_state"] == "unknown"


def test_operation_records_require_the_explicit_operations_return_array():
    projected = state.project_state(
        [
            evidence(
                "ops",
                {
                    "operations": [
                        {
                            "operation_id": "op-1",
                            "status": "running",
                            "name": "synthetic operation",
                        }
                    ]
                },
            ),
            evidence("not-ops", {"operation_id": "op-2", "status": "completed"}),
        ]
    )
    assert [op["operation_id"] for op in projected["operations"]] == ["op-1"]


def test_budget_drops_whole_records_reports_omissions_and_has_no_dangling_ids():
    prefix = [
        evidence(f"e{i}", task(task_id=f"t{i}", result_summary="observation " * 30))
        for i in range(40)
    ]
    result = state.enrich_packet(packet(), prefix, max_chars=1800)
    validate_packet(result)
    assert len(json.dumps(result, ensure_ascii=False, sort_keys=True)) <= 1800
    observed = result["state"]["router_state"]
    assert len(json.dumps(observed, ensure_ascii=False, sort_keys=True)) <= 4000
    assert observed["omitted_records"]["tasks"] > 0
    ids = {e["id"] for e in result["evidence"]}
    assert all(
        set(record["evidence_ids"]) <= ids
        for category in state.CATEGORIES
        for record in observed[category]
    )
    assert all(
        record["task_id"]
        in next(
            e["text"]
            for e in result["evidence"]
            if e["id"] == record["evidence_ids"][0]
        )
        for record in observed["tasks"]
    )
    assert state.enrich_packet(packet(), prefix, max_chars=1800) == result


def test_goal_request_are_preserved_and_impossible_essential_budget_fails():
    original = packet()
    original["goal"] = "essential goal " * 100
    with pytest.raises(ValueError, match="essential goal/request"):
        state.enrich_packet(original, max_chars=600)
    result = state.enrich_packet(original)
    assert result["goal"] == original["goal"]
    assert result["request"] == original["request"]


def test_only_input_argument_and_frozen_evidence_can_affect_output():
    prefix = [evidence("old", task())]
    supplied = packet(prefix)
    future = {
        "baseline_action": "FUTURE_ACTION_SENTINEL",
        "grade": "GOLD_SENTINEL",
        "final_roles": [task("failed")],
    }
    before = state.enrich_packet(supplied)
    future["final_roles"].append(task("stopped"))
    assert before == state.enrich_packet(supplied)
    assert "SENTINEL" not in json.dumps(before)
    with pytest.raises(ValueError):
        state.enrich_packet({**supplied, **future})


def test_null_status_is_unknown_and_long_values_are_explicit_excerpts():
    result = state.project_state(
        [
            evidence(
                "return",
                {
                    "tasks": [
                        {"task_id": "t", "status": None, "task_name": "long name " * 30}
                    ]
                },
            )
        ]
    )
    observed = result["tasks"][0]
    assert observed["status"] == "unknown"
    assert "task_name" not in observed
    assert observed["task_name_excerpt"] == ("long name " * 30)[:180]
    assert result["coverage"]["task_records"] == 1


def test_multiple_same_entity_updates_in_one_delivery_retain_all_support_fields():
    prefix = [
        evidence(
            "one",
            {
                "tasks": [
                    task("running"),
                    {"task_id": "review-1", "status": "completed"},
                ]
            },
        )
    ]
    result = state.enrich_packet(packet(), prefix)
    observed = result["state"]["router_state"]["tasks"][0]
    assert observed["status"] == "completed" and observed["role"] == "scout"
    support = result["evidence"][0]["text"]
    assert '"role": "scout"' in support and '"status": "completed"' in support


@pytest.mark.parametrize(
    "wrap",
    [
        lambda value: value,
        lambda value: "Script completed\nOutput:\n" + json.dumps(value),
        lambda value: {"output": json.dumps(value), "wall_time_seconds": 0.1},
    ],
)
def test_explicit_evaluation_status_return_preserves_numeric_identity_and_progress(
    wrap,
):
    source = {
        "status": "running",
        "evaluation": 37,
        "cases_total": 10,
        "cases_completed": 10,
    }
    result = state.enrich_packet(
        packet(), [evidence("evaluation-return", wrap(source))]
    )
    observed = result["state"]["router_state"]["operations"][0]
    assert observed["evaluation"] == 37 and type(observed["evaluation"]) is int
    assert observed["cases_total"] == observed["cases_completed"] == 10
    assert observed["status"] == "running"  # Counts never infer completion.
    assert observed["evidence_ids"] == ["evaluation-return"]
    support = result["evidence"][0]["text"]
    assert '"evaluation": 37' in support and '"status": "running"' in support


@pytest.mark.parametrize(
    "wrap",
    [
        lambda value: "Example: " + json.dumps(value),
        lambda value: "```json\n" + json.dumps(value) + "\n```",
        lambda value: {"result": json.dumps(value)},
        lambda value: {"result": value},
    ],
)
def test_quoted_evaluation_status_return_never_becomes_an_operation(wrap):
    source = {
        "status": "completed",
        "evaluation": 37,
        "cases_total": 10,
        "cases_completed": 10,
    }
    assert state.project_state([evidence("quoted", wrap(source))])["operations"] == []


@pytest.mark.parametrize(
    "missing", ["evaluation", "cases_total", "cases_completed", "status"]
)
def test_partial_evaluation_status_shape_does_not_trigger_direct_exception(missing):
    source = {
        "status": "running",
        "evaluation": 37,
        "cases_total": 10,
        "cases_completed": 1,
    }
    source.pop(missing)
    assert state.project_state([evidence("partial", source)])["operations"] == []


def test_fair_first_pass_preserves_available_plan_and_other_categories():
    prefix = [
        evidence(
            f"task-{i}",
            task(task_id=f"task-{i}", result_summary="observed record " * 20),
        )
        for i in range(30)
    ]
    prefix.extend(
        evidence(
            f"operation-{i}",
            {
                "status": "running",
                "evaluation": i,
                "cases_total": 10,
                "cases_completed": 1,
            },
        )
        for i in range(30)
    )
    prefix.extend(
        [
            evidence(
                "priority",
                {"human_request": {"status": "pending", "text": "Reuse recorded work"}},
            ),
            evidence(
                "plan",
                {
                    "next_action": "Interpret the supplied comparison, then request independent review."
                },
            ),
            evidence("status", "status_notes: Existing records remain available."),
        ]
    )
    supplied = packet()
    supplied["goal"] = "essential goal " * 150
    supplied["request"] = "essential request " * 85
    result = state.enrich_packet(supplied, prefix)
    observed = result["state"]["router_state"]
    assert all(observed[category] for category in state.CATEGORIES)
    assert (
        observed["plan_notes"][0]["text"]
        == "Interpret the supplied comparison, then request independent review."
    )
    assert observed["omitted_records"]["tasks"] > 0
    assert observed["omitted_records"]["operations"] > 0
    assert len(json.dumps(observed, ensure_ascii=False, sort_keys=True)) <= 4000
    assert len(json.dumps(result, ensure_ascii=False, sort_keys=True)) <= 12000
    ids = {item["id"] for item in result["evidence"]}
    assert all(
        set(record["evidence_ids"]) <= ids
        for category in state.CATEGORIES
        for record in observed[category]
    )
    assert "plan" in ids
    assert result == state.enrich_packet(supplied, prefix)
