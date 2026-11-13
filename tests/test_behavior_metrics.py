"""Synthetic histories only: no model, GPU, private transcripts or remote calls."""

import json

import pytest

from research_loop.behavior_metrics import BehaviorCollector, compare, main

START = "2026-01-01T00:01:00Z"
END = "2026-01-01T00:03:00Z"


def append(path, *rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


def row(kind, seconds=60, **payload):
    at = f"2026-01-01T00:{seconds // 60:02}:{seconds % 60:02}Z"
    return {"timestamp": at, "type": kind, "payload": payload}


def call(cid, name="wait_agent", seconds=60, arguments="{}"):
    return row(
        "response_item",
        seconds,
        type="function_call",
        call_id=cid,
        name=name,
        arguments=arguments,
    )


def result(cid, seconds=90, output="ok", **extra):
    return row(
        "response_item",
        seconds,
        type="function_call_output",
        call_id=cid,
        output=output,
        **extra,
    )


def record(rid="r", seconds=100):
    return row(
        "token_usage_record",
        seconds,
        response_id=rid,
        usage={"input_tokens": 100, "output_tokens": 10},
    )


@pytest.fixture
def study(tmp_path):
    root = tmp_path / "study"
    root.mkdir()
    (root / "status.json").write_text(json.dumps({"started_at": START}))
    (root / "active.json").write_text(json.dumps({"revision": "r1"}))
    config = root / "configurations/r1/config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"revision": "r1", "profiles": {}, "router": {}}))
    (root / "roles.json").write_text(
        json.dumps(
            {
                "threads": {
                    "router": {"role": "router", "task": "routing"},
                    "child": {"role": "analyst", "task": "analyst_one"},
                }
            }
        )
    )
    append(
        root / "events.jsonl",
        {"at": START, "event": "run.started", "thread_id": "router"},
    )
    for tid, task, parent in (
        ("router", "root", None),
        ("child", "analyst_one", "router"),
    ):
        source = (
            {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
            if parent
            else "cli"
        )
        append(
            root / "groups/router/one/native/sessions" / f"{tid}.jsonl",
            row("session_meta", 0, id=tid, source=source, agent_path=f"/root/{task}"),
        )
    return root


def collector(study):
    return BehaviorCollector(
        study, study.parent / "behavior", since=START, phase="test"
    )


def child(sample):
    return next(t for t in sample["behavior"]["by_task"] if t["thread_id"] == "child")


def test_batched_outputs_utf8_dedup_and_unknown_ready(study):
    p = study / "groups/router/one/native/sessions/child.jsonl"
    append(
        p,
        call("a"),
        call("b", "read_file", arguments="x"),
        result("a"),
        result("b", output="文" * 6000, truncated=True),
        record(),
        record(),
        result("b", output="ignored"),
        row("event_msg", 120, type="task_complete"),
    )
    with collector(study) as c:
        sample = c.poll(END)
        t = child(sample)
        assert (
            sample["behavior"]["children"]["associated_usage"]["any_output"][
                "responses"
            ]
            == 1
        )
        assert sample["behavior"]["total"]["over_16kib_count"] == 1
        assert t["wait_seconds"] == 30
        assert t["wait_lifetime_fraction"] == 0.5
        assert t["creation_to_first_tool_seconds"] == 60
        assert t["creation_to_first_delivery_seconds"] == 120
        assert t["dependency_ready_status"] == "missing"
        assert t["created_before_dependencies_ready"] is None
        assert t["output_utf8_bytes"] == 18002
        assert t["over_16kib_count"] == 1
        assert t["explicit_truncated_count"] == 1
        assert t["associated_usage"]["any_output"]["responses"] == 1
        assert t["associated_usage"]["wait_return"]["input_tokens"] == 100
        assert t["associated_usage"]["large_output"]["input_tokens"] == 100
        assert t["usage_groups"][0]["call_ids"] == ["a", "b"]
        assert child(c.poll(END))["output_utf8_bytes"] == 18002
        with pytest.raises(RuntimeError, match="writer"):
            collector(study)
    with collector(study) as c:
        assert child(c.poll(END))["output_utf8_bytes"] == 18002
    samples = [
        json.loads(x)
        for x in (study.parent / "behavior/samples.jsonl").read_text().splitlines()
    ]
    assert len(samples) == len({s["sample_id"] for s in samples}) == 3
    assert all("behavior" in sample for sample in samples)


def test_cross_phase_partial_output_repeat_and_union(study):
    p = study / "groups/router/one/native/sessions/child.jsonl"
    append(
        p,
        call("pre", "read_file", 30, "path"),
        result("pre", 40),
        record("pre", 45),
        call("a", seconds=50),
        call("b", seconds=70),
        result("a", 80),
        result("b", 90),
        call("repeat", "read_file", 91, "path"),
    )
    partial = json.dumps(result("repeat", 92, "summary"))
    with p.open("a") as stream:
        stream.write(partial[:35])
    with collector(study) as c:
        t = child(c.poll(END))
        assert t["wait_calls"] == 1
        assert t["returned_wait_calls"] == 2
        assert t["wait_seconds"] == 40
        assert t["wait_union_seconds"] == 30
        assert t["wait_lifetime_fraction"] == 0.25
        assert t["started_before_phase"] is True
        assert t["repeated_visible_read_calls"] == 1
        assert t["outputs_awaiting_usage"] == 2
        assert "sessions:incomplete_tail" in c.state["issues"]
    with p.open("a") as stream:
        stream.write(partial[35:] + "\n")
    append(p, record("post", 95))
    with collector(study) as c:
        t = child(c.poll(END))
        assert len(t["tool_outputs"]) == 3
        assert t["associated_usage"]["repeated_visible_read"]["responses"] == 1
        assert t["outputs_awaiting_usage"] == 0


def test_explicit_readiness_and_missing_is_not_inferred(study):
    append(
        study / "work/dispatch-readiness.jsonl",
        {"task_id": "analyst_one", "ready_at": START, "confirmed": False},
        {
            "task_id": "analyst_one",
            "ready_at": START,
            "confirmed": True,
            "confirmed_by": "coordinator",
        },
    )
    with collector(study) as c:
        t = child(c.poll(END))
        assert t["dependency_ready_status"] == "observed"
        assert t["created_minus_dependency_ready_seconds"] == -60
        assert t["created_before_dependencies_ready"] is True
        assert t["creation_to_first_tool_seconds"] is None


def test_cli_once_and_future_tail(study, capsys):
    append(
        study / "groups/router/one/native/sessions/child.jsonl",
        call("future", seconds=180),
    )
    main(
        [
            "watch",
            str(study),
            "--output",
            str(study.parent / "behavior"),
            "--since",
            START,
            "--phase",
            "test",
            "--once",
            "--until",
            END,
        ]
    )
    assert child(json.loads(capsys.readouterr().out))["wait_calls"] == 0
    with collector(study) as c:
        assert child(c.poll("2026-01-01T00:04:00Z"))["wait_calls"] == 1


def test_recovery_republishes_one_sample_after_interruption(study, monkeypatch):
    import research_loop.sample_store as module

    append(
        study / "groups/router/one/native/sessions/child.jsonl",
        call("a"),
        result("a"),
        record(),
    )
    original = module._atomic

    def interrupted(path, value):
        if path.name == "latest.json":
            raise OSError("synthetic interruption")
        return original(path, value)

    with collector(study) as c:
        monkeypatch.setattr(module, "_atomic", interrupted)
        with pytest.raises(OSError, match="synthetic"):
            c.poll(END)
    monkeypatch.setattr(module, "_atomic", original)
    with collector(study) as c:
        assert c.state["sequence"] == 1
        assert (
            child(json.loads((c.output / "latest.json").read_text()))["wait_calls"] == 1
        )
        assert child(c.poll(END))["wait_calls"] == 1
    samples = [
        json.loads(x)
        for x in (study.parent / "behavior/samples.jsonl").read_text().splitlines()
    ]
    assert [s["sample_id"] for s in samples] == ["test:1", "test:2"]
    assert "tool_outputs" not in child(samples[0])


def test_usage_missing_id_persistent_gap_and_resume(study):
    append(
        study / "groups/router/one/native/sessions/child.jsonl",
        call("a"),
        result("a", output="Warning: truncated output"),
        row("token_usage_record", 100, response_id={"invalid": "id"}, usage={}),
        row("event_msg", 120, type="task_complete"),
        row("event_msg", 130, type="task_started"),
    )
    with collector(study) as c:
        t = child(c.poll(END))
        assert t["explicit_truncated_count"] == 1
        assert t["lifetime_right_censored"] is True
        assert t["lifetime_phase_seconds"] == 120
        assert t["outputs_awaiting_usage"] == 1
        assert c.poll(END)["coverage"]["complete"] is False


def test_phase_cohorts_exclude_historical_completed_and_orphan(study):
    sessions = study / "groups/router/one/native/sessions"
    source = {"subagent": {"thread_spawn": {"parent_thread_id": "router"}}}
    for tid in ("old_done", "old_orphan"):
        append(
            sessions / f"{tid}.jsonl",
            row("session_meta", 0, id=tid, source=source, agent_path=f"/root/{tid}"),
            call(f"{tid}-wait", seconds=10),
        )
    append(
        sessions / "old_done.jsonl",
        result("old_done-wait", 20),
        row("event_msg", 30, type="task_complete"),
    )
    # Both Router and child have real activity during this phase.
    append(sessions / "router.jsonl", record("lead-active"))
    append(sessions / "child.jsonl", call("child-active"))
    with collector(study) as c:
        sample = c.poll(END)
    b = sample["behavior"]
    active_ids = {
        t["thread_id"] for t in sample["llm"]["by_task"] if t["active_in_phase"]
    }
    assert active_ids == {"router", "child"}
    assert {t["thread_id"] for t in b["by_task"] if t["active_in_phase"]} == active_ids
    assert b["total"]["tasks"] == 2
    assert b["children"]["tasks"] == 1
    assert b["total"]["open_wait_calls"] == 1
    assert b["children"]["dependency_ready_missing_tasks"] == 1
    assert b["total"]["dependency_ready_missing_tasks"] == 1
    assert b["coverage"]["readiness_missing_threads"] == ["child"]
    assert "unclassified" not in b["by_role"]
    assert "resources" not in sample and "allocation" not in sample
    assert {t["thread_id"] for t in b["by_task"]} == {
        "router",
        "child",
        "old_done",
        "old_orphan",
    }


def test_readiness_ready_at_only_and_late_session_discovery(study):
    path = study / "work/dispatch-readiness.jsonl"
    append(
        path,
        {
            "task_id": "late_task",
            "ready_at": START,
            "confirmed": True,
            "confirmed_by": "coordinator",
        },
    )
    with collector(study) as c:
        assert c.poll(END)["behavior"]["coverage"]["unmatched_readiness_records"] == 1
        source = {"subagent": {"thread_spawn": {"parent_thread_id": "router"}}}
        append(
            study / "groups/router/one/native/sessions/late.jsonl",
            row(
                "session_meta",
                80,
                id="late",
                source=source,
                agent_path="/root/late_task",
            ),
        )
        sample = c.poll(END)
        t = next(t for t in sample["behavior"]["by_task"] if t["thread_id"] == "late")
        assert t["dependency_ready_status"] == "observed"
        assert t["created_minus_dependency_ready_seconds"] == 20
        assert sample["behavior"]["coverage"]["unmatched_readiness_records"] == 0


def resource_event(at=START, tool="gpu_exec", **output):
    return {
        "at": at,
        "event": "resource.tool",
        "thread_id": "router",
        "tool": tool,
        "output": output,
    }


def test_resource_presentations_exact_metadata_and_phase_scope(study):
    append(
        study / "events.jsonl",
        resource_event(
            at="2026-01-01T00:00:20Z",
            output_compacted=True,
            output_bytes=999,
            returned_output_bytes=1,
        ),
        resource_event(
            output_compacted=True, output_bytes=1000, returned_output_bytes=100
        ),
        resource_event(
            tool="gpu_poll",
            output_compacted=False,
            output_bytes=12,
            returned_output_bytes=12,
        ),
        resource_event(
            tool="other",
            output_compacted=True,
            output_bytes=999,
            returned_output_bytes=1,
        ),
        resource_event(output="legacy output"),
        resource_event(
            output_compacted="true", output_bytes=5, returned_output_bytes=1
        ),
        resource_event(
            output_compacted=False, output_bytes=True, returned_output_bytes=1
        ),
        resource_event(
            at=END, output_compacted=True, output_bytes=999, returned_output_bytes=1
        ),
    )
    with collector(study) as c:
        sample = c.poll(END)
        r = sample["resource_presentations"]
        assert r["observed_calls"] == 5
        assert r["eligible"] == 2
        assert r["compacted"] == 1
        assert r["original_output_bytes"] == 1012
        assert r["returned_output_bytes"] == 112
        assert r["missing_or_legacy"] == 1
        assert r["invalid_metadata"] == 2
        assert r["status"] == "partial_metadata"
        assert r["coverage_mode"] == "phase_history_and_append"
        assert r["prior_source_bytes_not_replayed"] is False
        assert sample["llm"]["total"]["responses"] == 0
        assert c.poll(END)["resource_presentations"] == r
    with collector(study) as c:
        assert c.poll(END)["resource_presentations"] == r


def test_resource_presentations_missing_metadata_and_old_checkpoint_rejected(study):
    append(study / "events.jsonl", resource_event(output="unmeasured output"))
    with collector(study) as c:
        first = c.poll(END)["resource_presentations"]
        assert first["eligible"] == 0
        assert first["missing_or_legacy"] == 1
        assert first["compacted"] is None
        assert first["original_output_bytes"] is None
        assert first["returned_output_bytes"] is None
        checkpoint = c.output / "checkpoint.json"
    state = json.loads(checkpoint.read_text())
    state.pop("resource_presentations")
    checkpoint.write_text(json.dumps(state))
    before = checkpoint.read_bytes()
    with pytest.raises(ValueError, match="unsupported behavior checkpoint"):
        collector(study)
    assert checkpoint.read_bytes() == before


@pytest.mark.parametrize("enriched", [False, True])
def test_current_pending_recovery_preserves_resource_counters(
    study, monkeypatch, enriched
):
    from research_loop.sample_store import _atomic

    append(
        study / "events.jsonl",
        resource_event(
            output_compacted=True, output_bytes=600, returned_output_bytes=60
        ),
    )
    c = collector(study)

    def crash():
        if enriched:
            c._sample_history(c.state["pending_sample"])
            _atomic(c.output / "checkpoint.json", c.state)
        raise OSError("interrupted publication")

    monkeypatch.setattr(c, "_publish_pending", crash)
    with pytest.raises(OSError, match="interrupted publication"):
        c.poll(END)
    c.close()
    with collector(study) as restarted:
        recovered = json.loads((restarted.output / "latest.json").read_text())
        r = recovered["resource_presentations"]
        assert r["observed_calls"] == r["eligible"] == r["compacted"] == 1
        assert r["original_output_bytes"] == 600
        assert r["returned_output_bytes"] == 60
        assert restarted.poll(END)["resource_presentations"] == r
        samples = (restarted.output / "samples.jsonl").read_text().splitlines()
        assert [json.loads(line)["sample_id"] for line in samples] == [
            "test:1",
            "test:2",
        ]


@pytest.mark.parametrize("source", ["pending", "latest"])
def test_old_behavior_sample_rejected(study, source):
    with collector(study) as c:
        sample = c.poll(END)
        output = c.output
    sample.pop("resource_presentations")
    checkpoint = output / "checkpoint.json"
    if source == "pending":
        state = json.loads(checkpoint.read_text())
        state["pending_sample"] = sample
        checkpoint.write_text(json.dumps(state))
    else:
        (output / "latest.json").write_text(json.dumps(sample))
    before = {path: path.read_bytes() for path in output.iterdir() if path.is_file()}
    with pytest.raises(ValueError, match="behavior"):
        if source == "pending":
            collector(study)
        else:
            compare(output, output)
    assert {path: path.read_bytes() for path in before} == before


def test_compare_shared_complete_roles_unknown_ready_and_atomic_watch(
    study, capsys, monkeypatch
):
    from research_loop import sample_store

    path = study / "groups/router/one/native/sessions/child.jsonl"
    meta = json.loads(path.read_text().strip())
    meta["timestamp"] = "2026-01-01T00:01:10Z"
    path.write_text(json.dumps(meta) + "\n")
    append(
        path,
        call("a", seconds=80),
        result("a", 90),
        record(),
        row("event_msg", 120, type="task_complete"),
    )
    baseline = study.parent / "behavior"
    improved = study.parent / "improved"
    with collector(study) as c:
        baseline_sample = c.poll(END)
    # Synthetic comparison edge cases: one unshared role and one incomplete task.
    baseline_sample["behavior"]["by_task"].extend(
        [
            {**child(baseline_sample), "thread_id": "only-baseline", "role": "critic"},
            {
                **child(baseline_sample),
                "thread_id": "incomplete",
                "completed_at": None,
                "lifetime_right_censored": True,
            },
        ]
    )
    baseline_latest = baseline / "latest.json"
    baseline_latest.write_text(json.dumps(baseline_sample))
    baseline_before = baseline_latest.read_bytes()
    append(
        study / "events.jsonl",
        {
            "at": START,
            "event": "agent.dependency_ready",
            "thread_id": "child",
            "dependency_ready_at": START,
        },
    )
    comparison_path = study.parent / "behavior-comparison.json"
    comparison_path.write_text(json.dumps({"old": "valid snapshot"}))
    original_replace = sample_store.os.replace
    replacements = []

    def observed_replace(source, destination):
        if destination == comparison_path:
            assert json.loads(comparison_path.read_text()) == {"old": "valid snapshot"}
            assert json.loads(source.read_text())["shared_completed_roles"] == [
                "analyst"
            ]
            replacements.append(destination)
        return original_replace(source, destination)

    monkeypatch.setattr(sample_store.os, "replace", observed_replace)
    main(
        [
            "watch",
            str(study),
            "--output",
            str(improved),
            "--since",
            START,
            "--phase",
            "improved",
            "--baseline",
            str(baseline),
            "--once",
            "--until",
            END,
        ]
    )
    capsys.readouterr()
    assert replacements == [comparison_path]
    assert baseline_latest.read_bytes() == baseline_before
    assert not comparison_path.with_suffix(".json.tmp").exists()
    main(["compare", str(baseline), str(improved)])
    comparison = json.loads(capsys.readouterr().out)
    assert comparison == json.loads(comparison_path.read_text())
    b = comparison["phases"]["baseline"]
    i = comparison["phases"]["improved"]
    assert b["cohort"]["excluded_unshared_roles"] == 1
    assert b["cohort"]["excluded_incomplete_or_mixed"] == 1
    assert b["shared_role_cohort"]["tasks"] == i["shared_role_cohort"]["tasks"] == 1
    assert b["shared_role_cohort"]["mean_wait_lifetime_fraction"] == 0.2
    assert (
        b["shared_role_cohort"]["associated_usage"]["wait_return"]["observed_totals"][
            "input_tokens"
        ]
        == 100
    )
    assert b["shared_role_cohort"]["readiness"]["observed_fraction"] == 0
    assert b["shared_role_cohort"]["readiness"]["created_before_ready_count"] is None
    assert i["shared_role_cohort"]["readiness"]["observed_fraction"] == 1
    assert i["shared_role_cohort"]["readiness"]["created_before_ready_count"] == 0
    assert b["resource_presentations"]["status"] == "no_records"
    assert i["resource_presentations"]["status"] == "no_records"


def test_parentless_group_agent_is_included_without_invented_native_parent(study):
    path = (
        study
        / "group-runs/one/native-runtime/review/router/none/native/sessions/agent.jsonl"
    )
    append(
        path,
        row("session_meta", 70, id="independent", source="cli", agent_path="/root"),
        call("review-wait", seconds=80),
        result("review-wait", 90),
        record("independent", 100),
        row("event_msg", 120, type="task_complete"),
    )
    mappings = json.loads((study / "roles.json").read_text())
    mappings["threads"]["independent"] = {
        "role": "router",
        "group_run_id": "one",
        "task": "review",
    }
    (study / "roles.json").write_text(json.dumps(mappings))
    with collector(study) as current:
        sample = current.poll(END)
    agent = next(
        task
        for task in sample["behavior"]["by_task"]
        if task["thread_id"] == "independent"
    )
    assert agent["parent"] is None
    assert agent["is_router"] is False
    assert agent["wait_union_seconds"] == 10
    assert sample["behavior"]["children"]["tasks"] == 1
