import json

import pytest
from pydantic import ValidationError

from research_loop.task_pool import (
    CancelTask,
    CreateTask,
    TaskPool,
    TaskRequest,
    UpdateTask,
)


def task(task_id, priority=100, dependencies=()):
    return TaskRequest(
        id=task_id,
        question=f"Investigate {task_id}",
        priority=priority,
        dependencies=dependencies,
    )


def create(request):
    return CreateTask(action="create", task=request)


def update(request, revision=1):
    return UpdateTask(action="update", task=request, expected_revision=revision)


def cancel(task_id, revision=1):
    return CancelTask(action="cancel", id=task_id, expected_revision=revision)


def ids(pool):
    return [state.request.id for state in pool.ready()]


def test_priority_dependency_and_review_lifecycle():
    pool = TaskPool([task("later", -10, ("first",)), task("first", 1000)])
    assert pool.apply(
        [create(task("auto-b", 0)), create(task("auto-a", 0))], pool.snapshot()
    )["applied"] == ["auto-b", "auto-a"]
    assert ids(pool) == ["first", "auto-a", "auto-b"]
    first = pool.claim("first")
    assert first.status == "running" and first.revision == 2
    assert ids(pool) == ["auto-a", "auto-b"]
    with pytest.raises(ValueError, match="not ready"):
        pool.claim("later")
    finished = pool.complete("first", "r0001")
    assert finished.status == "reviewed" and finished.record_id == "r0001"
    assert finished.revision == 3
    assert ids(pool) == ["later", "auto-a", "auto-b"]
    json.loads(json.dumps(pool.snapshot()))
    assert pool.snapshot()[0]["request"]["dependencies"] == ["first"]


def test_judge_cannot_change_human_running_or_reviewed_tasks():
    pool = TaskPool([task("human")])
    pool.apply([create(task("auto"))], pool.snapshot())
    snapshot = pool.snapshot()
    rejected = pool.apply([update(task("human"))], snapshot)
    assert rejected["applied"] == []
    assert "pending autonomous" in rejected["conflicts"][0]["reason"]
    pool.claim("auto")
    assert not pool.apply([cancel("auto")], snapshot)["applied"]
    running = pool.snapshot()
    assert not pool.apply([update(task("auto"), 2)], running)["applied"]
    pool.complete("auto", "r1")
    assert not pool.apply([cancel("auto", 3)], pool.snapshot())["applied"]
    assert not pool.apply([cancel("human")], snapshot)["applied"]


def test_ordered_capacity_replacement_and_permanent_ids():
    pool = TaskPool([], max_pending=1)
    pool.apply([create(task("a"))], [])
    snapshot = pool.snapshot()
    result = pool.apply(
        [create(task("too-early")), cancel("a"), create(task("b"))], snapshot
    )
    assert result["applied"] == ["a", "b"]
    assert result["conflicts"] == [
        {"index": 0, "id": "too-early", "reason": "pending capacity exceeded"}
    ]
    assert ids(pool) == ["b"]
    assert not pool.apply([create(task("a"))], pool.snapshot())["applied"]
    pool.claim("b")
    pool.add_human(task("human"))
    assert ids(pool) == ["human"]


def test_stale_snapshot_and_duplicate_targets_do_not_mutate():
    pool = TaskPool([])
    pool.apply([create(task("a"))], [])
    old = pool.snapshot()
    assert pool.apply([update(task("a", 50))], old)["applied"] == ["a"]
    stale = pool.apply([cancel("a", 2)], old)
    assert stale["conflicts"][0]["reason"] == "stale revision"
    before = pool.snapshot()
    result = pool.apply([cancel("a", 2), update(task("a"), 2)], before)
    assert len(result["conflicts"]) == 2
    assert pool.snapshot() == before
    assert not pool.apply([cancel("a", 2)], [])["applied"]


def test_dependency_validation_is_atomic_and_allows_ordered_creation():
    pool = TaskPool([])
    result = pool.apply([create(task("a")), create(task("b", dependencies=("a",)))], [])
    assert result == {"applied": ["a", "b"], "conflicts": []}
    before = pool.snapshot()
    cycle = pool.apply([update(task("a", dependencies=("b",)))], before)
    assert cycle["conflicts"][0]["reason"] == "dependency cycle"
    missing = pool.apply([create(task("missing", dependencies=("absent",)))], before)
    assert missing["conflicts"][0]["reason"] == "missing dependency: absent"
    assert pool.snapshot() == before
    assert pool.apply([cancel("a")], before)["applied"] == ["a"]
    assert ids(pool) == []  # Cancelled dependencies do not count as reviewed.


def test_failed_dependencies_remain_blocking_and_bad_transitions_raise():
    pool = TaskPool([task("a"), task("b", dependencies=("a",))])
    with pytest.raises(ValueError, match="not running"):
        pool.complete("a", "r1")
    pool.claim("a")
    assert pool.fail("a").status == "failed"
    assert ids(pool) == []
    with pytest.raises(ValueError, match="not running"):
        pool.fail("a")


@pytest.mark.parametrize(
    "requests, capacity, match",
    [
        ([task("a"), task("a")], 8, "already exists"),
        ([task("a", dependencies=("missing",))], 8, "missing dependency"),
        ([task("a", dependencies=("a",))], 8, "dependency cycle"),
        ([task("a"), task("b")], 1, "capacity"),
        ([], 0, "positive"),
    ],
)
def test_constructor_rejects_invalid_pool(requests, capacity, match):
    with pytest.raises(ValueError, match=match):
        TaskPool(requests, capacity)


@pytest.mark.parametrize("task_id", ["", "../bad", "a.b", "-a", "a" * 65])
def test_ids_are_safe_and_requests_frozen(task_id):
    with pytest.raises(ValidationError):
        task(task_id)
    request = task("valid_1-A")
    with pytest.raises(ValidationError):
        request.question = "changed"
