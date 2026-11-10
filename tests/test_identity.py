"""Identity facts and their use by the two existing statistics readers."""

import pytest

from research_loop.efficiency import report
from research_loop.identity import resolve
from test_metrics import END, START, append, collector, record, session, write


def metadata(tid, parent=None, task="root"):
    return {"thread_id": tid, "parent": parent, "task": task}


def test_native_and_host_ancestry_excludes_unrelated_and_missing_history():
    identities = resolve(
        "lead",
        [
            metadata("grandchild", "native", "analyst_result"),
            metadata("native", "lead", "critic_review"),
            metadata("lead"),
            metadata("host", task="root"),
            metadata("hostchild", "host", "experimenter_cpu"),
            metadata("unrelated", task="scout_other"),
            metadata("orphan", "missing", "scout_orphan"),
        ],
        {
            "host": {"task": "host-task", "role": "scout"},
            "missing": {"task": "missing-history", "role": "critic"},
        },
    )
    assert set(identities) == {"lead", "native", "grandchild", "host", "hostchild"}
    assert identities["lead"].role == "lead"
    assert identities["native"].membership_source == "native-descendant"
    assert identities["native"].role_source == "inferred-from-task-name"
    assert identities["grandchild"].role == "analyst"
    assert identities["host"].membership_source == "host-role-record"
    assert identities["host"].role_source == "host-role-record"
    assert identities["host"].task == "host-task"
    assert identities["host"].role == "scout"
    assert identities["hostchild"].role == "experimenter"


def test_explicit_role_precedence_prefix_only_and_duplicate_identity():
    identities = resolve(
        "lead",
        [
            metadata("lead"),
            metadata("child", "lead", "critic_first"),
            metadata("child", "lead", "scout_second"),
            metadata("unknown", "lead", "audit_critic"),
            metadata("duplicate", "unrelated", "critic_first"),
            metadata("duplicate", "lead", "scout_second"),
            metadata("descendant", "duplicate", "analyst_result"),
        ],
        {
            "lead": {"role": "scout"},
            "child": {"role": "analyst", "task": "chosen-task"},
        },
    )
    assert identities["lead"].role == "lead"
    assert identities["child"].role == "analyst"
    assert identities["child"].task == "chosen-task"
    assert identities["unknown"].role == "unclassified"
    assert identities["unknown"].role_source == "unknown"
    # The later record establishes membership; identity still uses the first.
    assert identities["duplicate"].parent == "unrelated"
    assert identities["duplicate"].task == "critic_first"
    assert identities["duplicate"].role == "critic"
    assert identities["descendant"].role == "analyst"
    assert len(identities) == 5


def prepare(tmp_path):
    root = tmp_path / "study"
    write(root / "status.json", {"thread_id": "lead"})
    write(root / "config.json", {})
    write(root / "gpu-budget.json", {"entries": []})
    append(
        root / "events.jsonl", {"at": START, "event": "lead.input", "thread_id": "lead"}
    )
    return root


@pytest.mark.parametrize("reader", ["offline", "incremental"])
def test_mixed_delegation_membership_in_real_readers(tmp_path, reader):
    root = prepare(tmp_path)
    for tid, parent, task in (
        ("lead", None, "root"),
        ("native", "lead", "critic_review"),
        ("grandchild", "native", "analyst_result"),
        ("host", None, "root"),
        ("hostchild", "host", "experimenter_cpu"),
        ("unrelated", None, "scout_other"),
        ("orphan", "missing", "scout_orphan"),
    ):
        session(root, tid, parent, task, events=[record(tid)])
    write(
        root / "roles.json",
        {
            "threads": {
                "host": {"role": "scout", "task": "host-task"},
                # Also present in the native tree: must not be counted twice.
                "native": {"role": "critic", "task": "critic_review"},
                "missing": {"role": "critic", "task": "missing-history"},
            }
        },
    )
    if reader == "offline":
        result = report(root, since=START, until=END)
    else:
        with collector(root) as c:
            result = c.poll(END)
            assert c.poll(END)["llm"]["total"] == result["llm"]["total"]
    assert result["coverage"]["selected_threads"] == 5
    assert result["coverage"]["excluded_session_threads"] == 2
    tasks = {task["thread_id"]: task for task in result["llm"]["by_task"]}
    assert {tid: task["role"] for tid, task in tasks.items()} == {
        "lead": "lead",
        "native": "critic",
        "grandchild": "analyst",
        "host": "scout",
        "hostchild": "experimenter",
    }
    # Identity evidence is internal, not added to existing public statistics.
    assert all(
        "role_source" not in task and "membership_source" not in task
        for task in tasks.values()
    )


def test_incremental_late_mapping_keeps_cached_name_and_refreshes_explicit_role(
    tmp_path,
):
    root = prepare(tmp_path)
    session(root, events=[record("lead")])
    session(root, "native", "lead", "critic_original", events=[record("native")])
    session(root, "host", task="root", events=[record("host")])
    with collector(root) as c:
        first = c.poll(END)
        assert first["coverage"]["selected_threads"] == 2
        write(
            root / "roles.json",
            {
                "threads": {
                    "native": {"role": "analyst", "task": "changed-name"},
                    "host": {"role": "scout", "task": "scout-late"},
                }
            },
        )
        second = c.poll(END)
        tasks = {task["thread_id"]: task for task in second["llm"]["by_task"]}
        assert tasks["native"]["task"] == "critic_original"
        assert tasks["native"]["role"] == "analyst"
        assert tasks["host"]["task"] == "scout-late"
        assert tasks["host"]["role"] == "scout"
        write(root / "roles.json", {"threads": {}})
        third = c.poll(END)
        tasks = {task["thread_id"]: task for task in third["llm"]["by_task"]}
        assert tasks["native"]["role"] == "analyst"
        assert tasks["host"]["role"] == "scout"
    # Offline auditing reads the current mapping rather than cached task identity.
    tasks = {
        task["thread_id"]: task
        for task in report(root, since=START, until=END)["llm"]["by_task"]
    }
    assert tasks["native"]["role"] == "critic"
    assert "host" not in tasks


@pytest.mark.parametrize("mapping", [None, {}, {"role": 7, "task": 9}])
def test_existing_incomplete_host_mapping_behavior_is_retained(tmp_path, mapping):
    root = prepare(tmp_path)
    session(root, events=[record("lead")])
    session(root, "host", task="scout_candidate", events=[record("host")])
    write(root / "roles.json", {"threads": {"host": mapping}})
    with collector(root) as c:
        result = c.poll(END)
        tasks = {task["thread_id"]: task for task in result["llm"]["by_task"]}
        if mapping is None:
            assert "host" not in tasks
        else:
            # Preserve the historical parentless-task initialization fallback.
            assert tasks["host"]["role"] == "lead"
            assert tasks["host"]["task"] == "scout_candidate"
    if mapping is None or mapping == {}:
        tasks = {
            task["thread_id"]: task
            for task in report(root, since=START, until=END)["llm"]["by_task"]
        }
        assert tasks["host"]["role"] == "scout"
