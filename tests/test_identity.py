"""Authoritative dynamic identities and current native runtime discovery."""

import json

import pytest

from research_loop.efficiency import report
from research_loop.identity import resolve, session_paths
from test_metrics import (
    END,
    START,
    append,
    collector,
    configure,
    record,
    row,
    session,
    write,
)


def metadata(tid, parent=None, task="root"):
    return {"thread_id": tid, "parent": parent, "task": task}


def test_recorded_identity_and_ancestry_do_not_infer_scientific_roles():
    identities = resolve(
        [
            metadata("router"),
            metadata("child", "router", "critic_review"),
            metadata("grandchild", "child", "analyst_result"),
            metadata("agent"),
            metadata("unrelated", task="scout_other"),
        ],
        {
            "router": {"role": "router"},
            "agent": {"role": "custom-evaluator", "task": "evaluate"},
            "missing": {"role": "writer"},
        },
    )
    assert set(identities) == {"router", "child", "grandchild", "agent"}
    assert identities["router"].role == "router"
    assert identities["child"].role == identities["grandchild"].role == "unclassified"
    assert identities["child"].role_source == "unknown"
    assert identities["child"].membership_source == "native-descendant"
    assert identities["agent"].role == "custom-evaluator"
    assert identities["agent"].task == "evaluate"


def test_duplicate_ancestry_keeps_first_identity_without_role_guessing():
    identities = resolve(
        [
            metadata("router"),
            metadata("duplicate", "unrelated", "critic_first"),
            metadata("duplicate", "router", "scout_second"),
            metadata("descendant", "duplicate"),
        ],
        {"router": {"role": "router"}},
    )
    assert set(identities) == {"router", "duplicate", "descendant"}
    assert identities["duplicate"].parent == "unrelated"
    assert identities["duplicate"].task == "critic_first"
    assert identities["duplicate"].role == "unclassified"


@pytest.mark.parametrize("mapping", [None, {}, {"role": 7, "task": 9}])
def test_missing_or_incomplete_mapping_never_assigns_a_fixed_role(mapping):
    identities = resolve(
        [metadata("agent", task="scout_candidate")], {"agent": mapping}
    )
    if mapping is None:
        assert identities == {}
    else:
        assert identities["agent"].role == "unclassified"
        assert identities["agent"].task == "scout_candidate"


def prepare(tmp_path):
    """Shared current-run fixture for usage and sample publication tests."""
    root = tmp_path / "study"
    write(root / "run.json", {"id": "run-one", "created_at": START})
    write(root / "status.json", {"started_at": START})
    configure(root, None)
    write(
        root / "roles.json",
        {"threads": {"router": {"role": "router", "task": "routing"}}},
    )
    append(root / "events.jsonl", {"at": START, "event": "run.started"})
    return root


def session_at(directory, identity, *, response):
    append(
        directory / f"{identity}.jsonl",
        row(
            "session_meta",
            "2026-01-01T00:00:00Z",
            id=identity,
            source="cli",
            agent_path="/root",
        ),
        record(response),
    )


def router_study(tmp_path):
    root = prepare(tmp_path)
    for name in ("one", "two"):
        session_at(
            root / f"groups/router/{name}/native/sessions",
            f"router-{name}",
            response="shared-router-response",
        )
        session_at(
            root
            / f"group-runs/{name}/native-runtime/context/agent/workspace/native/sessions",
            f"group-{name}",
            response=f"group-{name}",
        )
    session_at(
        root / "groups/router/one/native/sessions", "unmapped", response="unmapped"
    )
    write(
        root / "roles.json",
        {
            "threads": {
                "router-one": {"role": "router"},
                "router-two": {"role": "router"},
                "group-one": {"role": "custom-writer"},
                "group-two": {"role": "custom-evaluator"},
                "missing-history": {"role": "writer"},
            }
        },
    )
    for path in (
        root / "artifacts/raw.jsonl",
        root / "groups/router/one/packet.jsonl",
        root / "group-runs/one/events.jsonl",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not a session\n")
    return root


@pytest.mark.parametrize("reader", ["offline", "incremental"])
def test_nested_agent_discovery_dedup_and_honest_identity_coverage(tmp_path, reader):
    root = router_study(tmp_path)
    if reader == "offline":
        result = report(root, until=END)
    else:
        with collector(root) as current:
            result = current.poll(END)
            assert current.poll(END)["llm"]["total"] == result["llm"]["total"]
    assert result["coverage"]["session_files_discovered"] == 5
    assert result["coverage"]["selected_threads"] == 4
    assert result["coverage"]["excluded_session_threads"] == 1
    assert result["llm"]["total"]["responses"] == 3
    assert result["llm"]["total"]["input_tokens"] == 300
    assert not result["coverage"]["complete"]
    assert {task["thread_id"]: task["role"] for task in result["llm"]["by_task"]} == {
        "router-one": "router",
        "router-two": "router",
        "group-one": "custom-writer",
        "group-two": "custom-evaluator",
    }


@pytest.mark.parametrize("reader", ["offline", "incremental"])
def test_explicit_snapshot_does_not_include_other_runtimes(tmp_path, reader):
    root = router_study(tmp_path)
    directory = (
        root / "group-runs/one/native-runtime/context/agent/workspace/native/sessions"
    )
    if reader == "offline":
        result = report(root, until=END, sessions_dir=directory)
    else:
        with collector(root, sessions_dir=directory) as current:
            result = current.poll(END)
    assert result["coverage"]["session_files_discovered"] == 1
    assert result["coverage"]["selected_threads"] == 1
    assert result["llm"]["total"]["input_tokens"] == 100


def test_new_router_and_agent_runtimes_preserve_old_offsets(tmp_path):
    root = router_study(tmp_path)
    with collector(root) as current:
        first = current.poll(END)
        offsets = {
            path: value["offset"] for path, value in current.state["offsets"].items()
        }
        session_at(
            root / "group-runs/new/native-runtime/review/custom/none/native/sessions",
            "new-agent",
            response="new-agent",
        )
        mappings = json.loads((root / "roles.json").read_text())
        mappings["threads"]["new-agent"] = {"role": "custom-reviewer"}
        write(root / "roles.json", mappings)
        second = current.poll(END)
        assert (
            second["llm"]["total"]["input_tokens"]
            == first["llm"]["total"]["input_tokens"] + 100
        )
        assert all(
            current.state["offsets"][path]["offset"] == offset
            for path, offset in offsets.items()
        )


def test_late_mapping_adds_previous_unmapped_history_once(tmp_path):
    root = prepare(tmp_path)
    session(root, events=[record("router")])
    session(
        root, "custom-agent", task="critic_name_is_not_a_role", events=[record("agent")]
    )
    with collector(root) as current:
        first = current.poll(END)
        assert first["coverage"]["selected_threads"] == 1
        write(
            root / "roles.json",
            {
                "threads": {
                    "router": {"role": "router"},
                    "custom-agent": {"role": "custom-evaluator", "task": "evaluate"},
                }
            },
        )
        second = current.poll(END)
        agent = next(
            task
            for task in second["llm"]["by_task"]
            if task["thread_id"] == "custom-agent"
        )
        assert agent["role"] == "custom-evaluator"
        assert agent["task"] == "evaluate"
        assert second["llm"]["total"]["responses"] == 2
        assert current.poll(END)["llm"]["total"]["responses"] == 2


def test_only_managed_native_subtrees_are_discovered(tmp_path):
    root = router_study(tmp_path)
    paths = session_paths(root)
    assert len(paths) == 5
    assert all("/native/sessions/" in str(path) for path in paths)
