import json
import os

import pytest

from research_loop.metrics import Collector, compare, main

START = "2026-01-01T00:01:00Z"
END = "2026-01-01T00:03:00Z"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def append(path, *values):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        for value in values:
            stream.write(json.dumps(value) + "\n")


def row(kind, at=START, **payload):
    return {"timestamp": at, "type": kind, "payload": payload}


def record(rid="one", at=START):
    return row(
        "token_usage_record",
        at,
        response_id=rid,
        usage={
            "input_tokens": 100,
            "cached_input_tokens": 80,
            "output_tokens": 10,
            "reasoning_output_tokens": 4,
        },
    )


def session(
    root, tid="lead", parent=None, task="root", at="2026-01-01T00:00:00Z", events=()
):
    source = (
        {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
        if parent
        else "cli"
    )
    path = root / "native/sessions" / f"{tid}.jsonl"
    append(
        path,
        row(
            "session_meta",
            at,
            id=tid,
            agent_path=f"/root/{task}",
            source=source,
            cli_version="0.167.0",
        ),
        *events,
    )
    return path


@pytest.fixture
def study(tmp_path):
    root = tmp_path / "study"
    write(root / "status.json", {"thread_id": "lead"})
    write(root / "config.json", {"model": "test-model", "effort": "medium"})
    write(root / "gpu-budget.json", {"entries": []})
    append(
        root / "events.jsonl", {"at": START, "event": "lead.input", "thread_id": "lead"}
    )
    return root


def collector(study, output=None, **kwargs):
    return Collector(
        study,
        output or study.parent / "metrics",
        since=START,
        phase="baseline",
        **kwargs,
    )


def test_incremental_partial_restart_and_permissions(study):
    path = session(study, events=[record()])
    with collector(study) as c:
        first = c.poll(END)
        assert first["llm"]["total"]["input_tokens"] == 100
        second = c.poll(END)
        small_bytes = sum(
            (study / n).stat().st_size
            for n in ("status.json", "config.json", "gpu-budget.json")
        )
        assert second["reader"]["bytes_read"] == small_bytes
        partial = json.dumps(record("two", "2026-01-01T00:02:00Z"))
        with path.open("a") as stream:
            stream.write(partial[:20])
        assert c.poll(END)["llm"]["total"]["responses"] == 1
        checkpoint = json.loads((c.output / "checkpoint.json").read_text())
        assert "token_usage_record" not in json.dumps(checkpoint)
        with path.open("a") as stream:
            stream.write(partial[20:] + "\n")
        assert c.poll(END)["llm"]["total"]["responses"] == 2
        with pytest.raises(RuntimeError, match="writer"):
            collector(study)
    with collector(study) as c:
        assert c.poll(END)["llm"]["total"]["responses"] == 2
    output = study.parent / "metrics"
    samples = [
        json.loads(line) for line in (output / "samples.jsonl").read_text().splitlines()
    ]
    assert len({s["sample_id"] for s in samples}) == len(samples) == 5
    assert os.stat(output).st_mode & 0o777 == 0o700
    for name in ("latest.json", "samples.jsonl", "checkpoint.json", ".lock"):
        assert os.stat(output / name).st_mode & 0o777 == 0o600


def test_boundaries_tree_records_compaction_and_cohorts(study):
    session(
        study,
        events=[
            record("before", "2026-01-01T00:00:59Z"),
            record(),
            record(),
            row("compacted", "2026-01-01T00:01:01Z"),
            row(
                "event_msg",
                "2026-01-01T00:02:00Z",
                type="token_count",
                info={"total_token_usage": {"input_tokens": 99999}},
            ),
            record("end", END),
        ],
    )
    session(
        study,
        "child",
        "lead",
        "critic_one",
        START,
        [
            record("child"),
            row("event_msg", "2026-01-01T00:02:00Z", type="task_complete"),
        ],
    )
    session(study, "grandchild", "child", "analyst_one", START, [record("grandchild")])
    session(study, "other", events=[record("other")])
    with collector(study) as c:
        result = c.poll(END)
        total = result["llm"]["total"]
        assert total["responses"] == 3
        assert total["input_tokens"] == 300
        assert total["cached_input_tokens"] == 240
        assert total["uncached_input_tokens"] == 60
        assert total["output_tokens"] == 30
        assert total["reasoning_output_tokens"] == 12
        assert result["coverage"]["selected_threads"] == 3
        assert result["coverage"]["excluded_session_threads"] == 1
        lead = next(t for t in result["llm"]["by_task"] if t["role"] == "lead")
        assert lead["compactions"] == 1
        assert lead["duplicate_responses"] == 1
        assert lead["startup"]["responses"] == 0
        assert lead["mixed"]
        assert c.poll("2026-01-01T00:03:01Z")["llm"]["total"]["responses"] == 4
    comparison = compare(study.parent / "metrics", study.parent / "metrics")
    cohort = comparison["baseline"]["task_cohorts"]
    assert cohort["completed_unmixed"] == 1
    assert (
        cohort["completed_by_role"]["critic"]["whole_task_usage"]["input_tokens"] == 100
    )
    assert cohort["completed_by_role"]["analyst"]["whole_task_usage"] is None


def test_waits_and_consumed_background_association(study):
    def tool(event, at, **payload):
        return row("response_item", at, type=event, **payload)

    session(
        study,
        events=[
            tool(
                "function_call",
                START,
                name="exec",
                call_id="read",
                arguments="cat goal.md knowledge/index.md AGENTS.md",
            ),
            tool(
                "function_call_output",
                "2026-01-01T00:01:01Z",
                call_id="read",
                output="private output must not persist",
            ),
            record(at="2026-01-01T00:01:01.500000Z"),
            tool(
                "function_call",
                "2026-01-01T00:01:02Z",
                name="wait_agent",
                call_id="wait",
            ),
            tool(
                "function_call_output",
                "2026-01-01T00:01:07Z",
                call_id="wait",
                output='{"timed_out":true}',
            ),
            tool(
                "function_call_output",
                "2026-01-01T00:01:08Z",
                call_id="wait",
                output='{"timed_out":true}',
            ),
            tool(
                "function_call",
                "2026-01-01T00:01:09Z",
                name="exec",
                call_id="patch",
                arguments="cat goal.md; apply_patch ...",
            ),
            tool(
                "function_call_output",
                "2026-01-01T00:01:10Z",
                call_id="patch",
                output="no",
            ),
            record("two", "2026-01-01T00:01:11Z"),
            tool(
                "function_call",
                "2026-01-01T00:01:12Z",
                name="exec",
                call_id="reread",
                arguments='Path("goal.md").read_text()',
            ),
            tool(
                "function_call_output",
                "2026-01-01T00:01:13Z",
                call_id="reread",
                output="no",
            ),
            record("three", "2026-01-01T00:01:14Z"),
        ],
    )
    with collector(study) as c:
        result = c.poll(END)
        task = result["llm"]["by_task"][0]
        assert task["background_associated"]["responses"] == 2
        assert task["later_background_associated"]["responses"] == 1
        assert task["waits"]["wait_agent"] == {
            "calls": 1,
            "returned_calls": 1,
            "seconds": 5.0,
            "timed_out": 1,
        }
        assert task["first_3"]["responses"] == 3
        assert "private output" not in (c.output / "checkpoint.json").read_text()


def test_resources_ledger_precedence_and_missing_coverage(study):
    session(study)
    event = {
        "at": START,
        "event": "resource.tool",
        "thread_id": "lead",
        "tool": "gpu_poll",
        "success": True,
        "output": {"running": True, "output": ""},
    }
    append(study / "events.jsonl", event, {**event, "success": False})
    ledger = study.parent / "ledger.json"
    write(
        ledger,
        {
            "entries": [
                {
                    "dispatched_at": START,
                    "job_id": "a",
                    "status": "TERMINAL",
                    "receipt": {"state": "CANCELLED", "elapsed_seconds": 9},
                },
                {
                    "dispatched_at": START,
                    "status": "TERMINAL",
                    "receipt": {"state": "NOT_DISPATCHED", "elapsed_seconds": 0},
                },
            ]
        },
    )
    write(study / "config.json", {"resource": {"ledger_path": str(ledger)}})
    with collector(study) as c:
        result = c.poll(END)
        assert result["resources"]["calls"] == 2
        assert result["resources"]["failed_calls"] == 1
        assert result["resources"]["successful_empty_running_gpu_polls"] == 1
        assert result["allocation"]["elapsed_seconds"] == 9
        assert result["allocation"]["not_dispatched"] == 1
        assert result["allocation"]["dispatched_jobs"] == 1
        assert result["allocation"]["source"] == "frozen_config"
    with collector(
        study, study.parent / "explicit", ledger_path=study / "gpu-budget.json"
    ) as c:
        assert c.poll(END)["allocation"]["elapsed_seconds"] == 0
    (study / "events.jsonl").unlink()
    with collector(study) as c:
        assert not c.poll(END)["coverage"]["complete"]


def test_journal_repair_after_checkpoint_commit(study, monkeypatch):
    session(study, events=[record()])
    c = collector(study)
    original = c._publish_pending

    def crash():
        raise OSError("interrupted after checkpoint")

    monkeypatch.setattr(c, "_publish_pending", crash)
    with pytest.raises(OSError):
        c.poll(END)
    c.close()
    with collector(study) as restarted:
        assert (
            json.loads((restarted.output / "latest.json").read_text())["llm"]["total"][
                "responses"
            ]
            == 1
        )
        assert restarted.poll(END)["llm"]["total"]["responses"] == 1
        samples = (restarted.output / "samples.jsonl").read_text().splitlines()
        assert [json.loads(s)["sample_id"] for s in samples] == [
            "baseline:1",
            "baseline:2",
        ]
    assert original


def test_timezone_compare_zero_and_cli(study, capsys):
    with pytest.raises(ValueError, match="timezone"):
        Collector(study, study.parent / "bad", since="2026-01-01T00:00:00", phase="bad")
    session(study)
    with collector(study) as c:
        with pytest.raises(ValueError, match="timezone"):
            c.poll("2026-01-01T00:02:00")
        c.poll(END)
        with pytest.raises(ValueError, match="backwards"):
            c.poll("2026-01-01T00:02:00Z")
    comparison = compare(study.parent / "metrics", study.parent / "metrics")
    assert all(v is None for v in comparison["baseline"]["normalized"].values())
    main(
        [
            "watch",
            str(study),
            "--output",
            str(study.parent / "cli"),
            "--since",
            START,
            "--phase",
            "test",
            "--once",
            "--until",
            END,
        ]
    )
    assert json.loads(capsys.readouterr().out)["phase"] == "test"


def test_truncate_does_not_silently_replay(study):
    path = session(study, events=[record()])
    with collector(study) as c:
        c.poll(END)
        path.write_text("")
        result = c.poll(END)
        assert result["llm"]["total"]["responses"] == 1
        assert not result["coverage"]["complete"]
        assert "sessions:rotated_or_truncated" in result["coverage"]["tick_issues"]


def test_cpu_only_and_usage_format_coverage(study):
    (study / "gpu-budget.json").unlink()
    path = session(study)
    with collector(study) as c:
        empty = c.poll(END)
        assert empty["coverage"]["complete"]
        assert not empty["allocation"]["available"]
        assert not empty["llm"]["measurable"]
        append(
            path,
            row(
                "event_msg",
                "2026-01-01T00:02:00Z",
                type="token_count",
                info={"total_token_usage": {"input_tokens": 10}},
            ),
        )
        unsupported = c.poll(END)
        assert not unsupported["coverage"]["complete"]
        assert (
            "sessions:usage_records_missing" in unsupported["coverage"]["tick_issues"]
        )
        append(path, record())
        recovered = c.poll(END)
        assert recovered["coverage"]["complete"]
        assert recovered["llm"]["measurable"]


def test_journal_repair_after_sample_append(study, monkeypatch):
    import research_loop.metrics as metrics

    session(study, events=[record()])
    original = metrics._atomic

    def interrupted(path, value):
        if path.name == "latest.json":
            raise OSError("interrupted after journal append")
        return original(path, value)

    c = collector(study)
    with monkeypatch.context() as patch:
        patch.setattr(metrics, "_atomic", interrupted)
        with pytest.raises(OSError):
            c.poll(END)
    c.close()
    with collector(study) as restarted:
        samples = (restarted.output / "samples.jsonl").read_text().splitlines()
        assert len(samples) == 1
        assert json.loads(samples[0])["llm"]["total"]["responses"] == 1
        restarted.poll(END)
        assert len((restarted.output / "samples.jsonl").read_text().splitlines()) == 2


def test_sigterm_exits_cleanly(study):
    import subprocess
    import sys
    import time

    session(study)
    output = study.parent / "signal"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "research_loop.metrics",
            "watch",
            str(study),
            "--output",
            str(output),
            "--since",
            START,
            "--phase",
            "signal",
            "--interval",
            "60",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while (
            not (output / "latest.json").exists()
            and time.monotonic() < deadline
            and process.poll() is None
        ):
            time.sleep(0.02)
        assert (output / "latest.json").exists()
        process.terminate()
        _, error = process.communicate(timeout=3)
        assert process.returncode == 0, error.decode()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.parametrize(
    "evidence",
    [
        row("response_item", START, type="message", role="assistant"),
        row(
            "response_item",
            START,
            type="function_call",
            name="exec",
            call_id="evidence",
        ),
        row(
            "event_msg",
            START,
            type="token_count",
            info={"total_token_usage": {"input_tokens": 10}},
        ),
    ],
)
def test_authority_coverage_is_per_thread(study, evidence):
    session(study, events=[record()])
    child = session(
        study,
        "child",
        "lead",
        "critic_one",
        START,
        [row("turn_context", START, model="test-model", effort="medium"), evidence],
    )
    with collector(study) as c:
        result = c.poll(END)
        assert result["llm"]["total"]["responses"] == 1
        assert not result["llm"]["measurable"]
        assert result["coverage"]["usage_records_missing_threads"] == ["child"]
        append(child, record("child"))
        result = c.poll(END)
        assert result["llm"]["measurable"]
        assert result["coverage"]["complete"]


def test_compact_history_and_source_time_survive_empty_ticks(study):
    path = session(study, events=[record()])
    session(study, "unrelated", events=[record("unrelated", "2026-01-01T00:02:59Z")])
    output = study.parent / "metrics"
    with collector(study) as c:
        first = c.poll(END)
        assert first["last_source_event_at"] == "2026-01-01T00:01:00+00:00"
        second = c.poll("2026-01-01T00:04:00Z")
        assert second["last_source_event_at"] == first["last_source_event_at"]
        assert second["window"]["until"] != first["window"]["until"]
        append(path, record("new", "2026-01-01T00:02:00Z"))
        assert (
            c.poll("2026-01-01T00:04:00Z")["last_source_event_at"]
            == "2026-01-01T00:02:00+00:00"
        )
    latest = json.loads((output / "latest.json").read_text())
    history = [
        json.loads(line) for line in (output / "samples.jsonl").read_text().splitlines()
    ]
    assert latest["llm"]["by_task"]
    assert all("by_task" not in item["llm"] for item in history)
    assert all("latest.json" in item["history"]["task_details"] for item in history)
    with collector(study) as c:
        restarted = c.poll("2026-01-01T00:05:00Z")
        assert restarted["last_source_event_at"] == latest["last_source_event_at"]


def test_old_orphan_context_and_delivery_do_not_poison_usage_or_cohorts(study):
    session(study, events=[record()])
    session(
        study,
        "orphan",
        "lead",
        "experimenter_orphan",
        "2026-01-01T00:00:00Z",
        [
            row(
                "turn_context",
                "2026-01-01T00:00:10Z",
                model="test-model",
                effort="medium",
            ),
            row("response_item", "2026-01-01T00:00:20Z", type="message", role="user"),
            row("response_item", "2026-01-01T00:00:30Z", type="agent_message"),
        ],
    )
    with collector(study) as c:
        result = c.poll(END)
        assert result["coverage"]["complete"]
        assert result["llm"]["measurable"]
        assert result["llm"]["total"]["responses"] == 1
        assert result["coverage"]["awaiting_first_usage_threads"] == ["orphan"]
        assert not result["coverage"]["usage_records_missing_threads"]
        orphan = next(t for t in result["llm"]["by_task"] if t["thread_id"] == "orphan")
        assert orphan["awaiting_first_usage"]
        assert not orphan["authority_missing"]
        assert not orphan["active_in_phase"]
        assert not orphan["mixed"]
        assert orphan["last_source_event_at"] == "2026-01-01T00:00:30+00:00"
    output = study.parent / "metrics"
    cohorts = compare(output, output)["baseline"]["task_cohorts"]
    assert cohorts["pending"] == 0
    assert cohorts["mixed"] == 0


def test_watch_refreshes_comparison_without_model_calls(study, capsys):
    session(study, events=[record()])
    with collector(study) as c:
        c.poll(END)
    focused = study.parent / "focused"
    main(
        [
            "watch",
            str(study),
            "--output",
            str(focused),
            "--since",
            START,
            "--phase",
            "focused",
            "--once",
            "--until",
            END,
            "--baseline",
            str(study.parent / "metrics"),
        ]
    )
    snapshot = json.loads(capsys.readouterr().out)
    comparison = json.loads((study.parent / "comparison.json").read_text())
    assert comparison["optimized"]["totals"]["llm"] == snapshot["llm"]["total"]
    assert comparison["baseline"]["totals"]["llm"]["responses"] == 1
    assert comparison["optimized"]["phase"] == "focused"
