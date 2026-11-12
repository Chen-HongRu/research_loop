"""Query actual persisted trace facts without scanning unrelated record bodies."""

import json
from pathlib import Path

import pytest

from research_loop import queries
from research_loop.records import ExecutionTrace
from research_loop.state import write_json


@pytest.fixture
def run(tmp_path):
    root = tmp_path / "run"
    directory = root / "group-runs" / "run-one"
    directory.mkdir(parents=True)
    write_json(directory / "run.json", {"run_id": "run-one", "status": "running"})
    write_json(root / "status.json", {"status": "paused", "reason": "operator"})
    write_json(root / "active.json", {"revision": "revision-one"})
    return (
        root,
        directory,
        ExecutionTrace(directory, run_id="run-one", round=1, revision="revision-one"),
    )


def test_queries_preserve_explicit_branch_and_handoff_facts(run):
    root, directory, trace = run
    with trace.step("choose") as parent:
        with trace.step("branch-a", parent_step_id=parent) as child:
            call = trace.begin("call", "writer", step_id=child, agent_id="writer")
            trace.end(call, result={"answer": 3})
        operation = trace.begin("operation", "local", step_id=parent)
        trace.end(operation, exit_code=0)
        handoff = trace.handoff(call, "reviewer", {"answer": 3}, parent_step_id=parent)
    assert queries.get_run(root, "run-one") == {
        "run_id": "run-one",
        "status": "running",
    }
    assert queries.get_run(root) == {
        "status": "paused",
        "reason": "operator",
        "config_revision": "revision-one",
    }
    assert queries.get_step(root, "run-one", child)["parent_step_id"] == parent
    assert queries.get_call(root, "run-one", call)["result"] == {"answer": 3}
    assert queries.get_operation(root, "run-one", operation)["exit_code"] == 0
    assert queries.get_handoff(root, "run-one", handoff)["sender"] == call
    for listing in (
        queries.list_steps,
        queries.list_calls,
        queries.list_operations,
        queries.list_handoffs,
    ):
        records = listing(root, "run-one")["items"]
        assert records
        assert all(record["config_revision"] == "revision-one" for record in records)
    assert json.loads((directory / "run.json").read_text())["status"] == "running"


def test_execution_pagination_opens_only_requested_page(run, monkeypatch):
    root, _, trace = run
    identities = [trace.begin("step", str(i)) for i in range(7)]
    reads = []
    original = Path.read_text

    def spy(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    first = queries.list_steps(root, "run-one", limit=2)
    assert [record["id"] for record in first["items"]] == identities[:2]
    assert first["next_cursor"] == identities[1]
    assert len(reads) == 2
    second = queries.list_steps(root, "run-one", cursor=first["next_cursor"], limit=2)
    assert [record["id"] for record in second["items"]] == identities[2:4]
    assert len(reads) == 4


def test_run_pagination_opens_only_requested_page(run, monkeypatch):
    root, _, _ = run
    for name in ("run-two", "run-three"):
        (root / "group-runs" / name).mkdir()
        write_json(
            root / "group-runs" / name / "run.json",
            {"run_id": name, "status": "closed"},
        )
    reads = []
    original = Path.read_text

    def spy(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    result = queries.list_runs(root, limit=1)
    assert [record["run_id"] for record in result["items"]] == ["run-one"]
    assert result["next_cursor"] == "run-one"
    assert len(reads) == 1


def test_missing_records_are_explicit_and_no_handoffs_are_invented(run):
    root, _, trace = run
    trace.begin("call", "unrelated")
    assert queries.list_handoffs(root, "run-one") == {"items": [], "next_cursor": None}
    with pytest.raises(FileNotFoundError):
        queries.get_handoff(root, "run-one", "missing")
    with pytest.raises(FileNotFoundError):
        queries.get_run(root, "missing")


@pytest.mark.parametrize("identity", ["../escape", "", "/tmp", "..", "a/b"])
def test_invalid_paths_rejected(run, identity):
    root, _, _ = run
    with pytest.raises(ValueError):
        queries.get_run(root, identity)
    with pytest.raises(ValueError):
        queries.get_step(root, "run-one", identity)
    with pytest.raises(ValueError):
        queries.list_steps(root, "run-one", cursor=identity)


@pytest.mark.parametrize("limit", [True, 0, 201, "1"])
def test_invalid_page_limit(run, limit):
    root, _, _ = run
    with pytest.raises(ValueError):
        queries.list_runs(root, limit=limit)
    with pytest.raises(ValueError):
        queries.list_steps(root, "run-one", limit=limit)


def test_symlink_escape_rejected(run, tmp_path):
    root, directory, trace = run
    outside = tmp_path / "outside.json"
    outside.write_text('{"private": "synthetic"}')
    identity = trace.begin("step", "test")
    path = directory / "records/step" / f"{identity}.json"
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        queries.get_step(root, "run-one", identity)
    with pytest.raises(ValueError, match="escapes"):
        queries.list_steps(root, "run-one")
