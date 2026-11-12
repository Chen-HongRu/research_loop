"""One request-usage contract across offline, incremental and resumed readers."""

import json

import pytest

from research_loop.efficiency import report
from research_loop.behavior_metrics import BehaviorCollector
from research_loop.token_usage import TOKEN_KEYS, accept_record
from test_efficiency import receipt, token
from test_identity import prepare
from test_metrics import END, START, append, collector, row, session, write


def assert_parity(root, sample, until=END):
    offline = report(root, since=START, until=until)
    keys = (*TOKEN_KEYS, "uncached_input_tokens", "responses")

    def tokens(value):
        return {key: value[key] for key in keys}

    assert tokens(offline["llm"]["total"]) == sample["llm"]["total"]
    assert offline["llm"]["authority"] == sample["llm"]["authority"]
    assert offline["llm"]["measurable"] == sample["llm"]["measurable"]
    assert offline["coverage"]["complete"] == sample["coverage"]["complete"]
    left = {t["thread_id"]: t for t in offline["llm"]["by_task"]}
    right = {t["thread_id"]: t for t in sample["llm"]["by_task"]}
    assert left.keys() == right.keys()
    for tid, task in left.items():
        other = right[tid]
        assert tokens(task["usage"]) == other["usage"]
        assert task["usage"]["duplicate_responses"] == other["duplicate_responses"]
        for key in (*TOKEN_KEYS, "uncached_input_tokens"):
            assert task["cumulative_window"][key] == other["cumulative_window"][key]
        for key in (
            "usage_records_observed",
            "authority_missing",
            "awaiting_first_usage",
        ):
            assert task[key] == other[key]
    for role, usage in offline["llm"]["by_role"].items():
        assert tokens(usage) == sample["llm"]["by_role"][role]["usage"]
    for key in ("usage_records_missing_threads", "awaiting_first_usage_threads"):
        assert sorted(offline["coverage"][key]) == sorted(sample["coverage"][key])
    assert (
        offline["coverage"]["usage_records_observed"]
        == sample["coverage"]["usage_records_observed"]
    )
    return offline


def test_windowed_mapped_usage_matches_incremental_and_restarts(tmp_path):
    root = prepare(tmp_path)
    before = receipt("before", "2026-01-01T00:00:30Z", 100)
    request = receipt("request", START, 120, cached=50, output=10, reasoning=2)
    path = session(
        root,
        events=[
            before,
            token("2026-01-01T00:00:30Z", 100, 0, 0, 0),
            request,
            request,
            receipt("before", "2026-01-01T00:01:02Z", 100),
            token("2026-01-01T00:01:03Z", 220, 50, 10, 2),
            row("compacted", "2026-01-01T00:01:40Z"),
            receipt("compaction", "2026-01-01T00:02:00Z", 5, output=1),
        ],
    )
    session(
        root, "native", "router", "critic_test", events=[receipt("native", START, 20)]
    )
    session(root, "host", task="root", events=[receipt("host", START, 30)])
    session(root, "unrelated", events=[receipt("other", START, 99999)])
    write(
        root / "roles.json",
        {
            "threads": {
                "router": {"role": "router"},
                "host": {"role": "custom-agent", "task": "host"},
            }
        },
    )
    halfway = "2026-01-01T00:01:30Z"
    with collector(root) as c:
        first = c.poll(halfway)
        assert_parity(root, first, halfway)
        assert first["llm"]["total"]["input_tokens"] == 170
    append(
        path,
        receipt("later", "2026-01-01T00:02:10Z", 7),
        token(
            "2026-01-01T00:02:11Z", 2, 0, 0, 0
        ),  # Counter reset affects only reconciliation.
        receipt("boundary", END, 500),
    )
    with collector(root) as c:
        resumed = c.poll(END)
        assert_parity(root, resumed)
        assert resumed["llm"]["total"]["input_tokens"] == 182
        assert resumed["llm"]["total"]["responses"] == 5
        assert_parity(root, c.poll(END))
    with collector(root) as c:
        assert_parity(root, c.poll(END))
        later = "2026-01-01T00:03:01Z"
        sample = c.poll(later)
        assert_parity(root, sample, later)
        assert sample["llm"]["total"]["input_tokens"] == 682


def test_missing_receipts_never_use_cumulative_fallback_and_can_arrive_later(tmp_path):
    root = prepare(tmp_path)
    session(root, events=[receipt("router", START, 10)])
    path = session(
        root,
        "child",
        "router",
        "critic_test",
        events=[
            row("response_item", START, type="message", role="assistant"),
            token(START, 999, 0, 0, 0),
        ],
    )
    session(root, "awaiting", "router", "scout_pending")
    with collector(root) as c:
        sample = c.poll(END)
        offline = assert_parity(root, sample)
        assert sample["llm"]["total"]["input_tokens"] == 10
        assert not sample["llm"]["measurable"]
        assert not sample["coverage"]["complete"]
        assert not offline["coverage"]["complete"]
        assert sample["coverage"]["usage_records_missing_threads"] == ["child"]
        assert (
            offline["llm"]["reconciliation"]["cumulative_usage"]["input_tokens"] == 999
        )
    append(path, receipt("child", "2026-01-01T00:02:00Z", 15))
    with collector(root) as c:
        sample = c.poll(END)
        offline = assert_parity(root, sample)
        assert sample["llm"]["measurable"]
        assert sample["coverage"]["complete"]
        assert offline["coverage"]["complete"]
        assert sample["llm"]["total"]["input_tokens"] == 25


@pytest.mark.parametrize(
    "payload",
    [
        {"usage": {"input_tokens": 10}},
        {"response_id": "request", "usage": {"input_tokens": -1}},
        {"response_id": "request", "usage": {"input_tokens": True}},
        {
            "response_id": "request",
            "usage": {"input_tokens": 1, "cached_input_tokens": 2},
        },
        {"response_id": "request", "usage": []},
        {"response_id": "request"},
        {"response_id": "request", "usage": {}},
        {"response_id": "request", "usage": {"unrelated": 10}},
    ],
)
def test_invalid_receipts_and_later_valid_record_match_after_restart(tmp_path, payload):
    root = prepare(tmp_path)
    path = session(
        root,
        events=[
            row(
                "response_item", START, type="function_call", name="exec", call_id="one"
            ),
            {"timestamp": START, "type": "token_usage_record", "payload": payload},
        ],
    )
    with collector(root) as c:
        sample = c.poll(END)
        offline = assert_parity(root, sample)
        assert sample["llm"]["total"]["input_tokens"] == 0
        assert not sample["coverage"]["complete"]
        assert not offline["coverage"]["complete"]
    append(path, receipt("request", "2026-01-01T00:02:00Z", 20))
    with collector(root) as c:
        sample = c.poll(END)
        assert_parity(root, sample)
        assert sample["llm"]["total"]["input_tokens"] == 20


def test_complete_json_without_newline_waits_for_publication_in_both_readers(tmp_path):
    root = prepare(tmp_path)
    path = session(root)
    with path.open("a") as stream:
        stream.write(json.dumps(receipt("tail", START, 42)))
    with collector(root) as c:
        sample = c.poll(END)
        assert_parity(root, sample)
        assert sample["llm"]["total"]["responses"] == 0
    with path.open("a") as stream:
        stream.write("\n")
    with collector(root) as c:
        sample = c.poll(END)
        assert_parity(root, sample)
        assert sample["llm"]["total"]["input_tokens"] == 42


def test_response_ids_are_study_wide_and_invalid_record_does_not_reserve_id():
    seen = set()
    value, issue = accept_record(
        {"response_id": "one", "usage": {"input_tokens": -1}}, seen
    )
    assert issue == "invalid_usage_record" and not seen
    value, issue = accept_record(
        {"response_id": "one", "usage": {"input_tokens": 10}}, seen
    )
    assert issue is None and value["input_tokens"] == 10
    value, issue = accept_record(
        {"response_id": "one", "usage": {"input_tokens": 999}}, seen
    )
    assert value is None and issue == "duplicate"


@pytest.mark.parametrize(
    "info",
    [
        {"last_token_usage": {"input_tokens": 5}},
        {"total_token_usage": {}},
        {"total_token_usage": []},
        "invalid",
    ],
)
def test_malformed_cumulative_notification_does_not_reset_baseline(tmp_path, info):
    root = prepare(tmp_path)
    session(
        root,
        events=[
            token(START, 100, 0, 0, 0),
            row("event_msg", "2026-01-01T00:01:10Z", type="token_count", info=info),
            token("2026-01-01T00:02:00Z", 120, 0, 0, 0),
            receipt("request", "2026-01-01T00:02:01Z", 120),
        ],
    )
    with collector(root) as c:
        sample = c.poll(END)
        offline = assert_parity(root, sample)
        assert sample["llm"]["total"]["input_tokens"] == 120
        assert (
            offline["llm"]["reconciliation"]["cumulative_usage"]["input_tokens"] == 120
        )
        assert not sample["coverage"]["complete"]
    with collector(root) as c:
        assert_parity(root, c.poll(END))


def test_explicit_zero_receipt_is_measured_not_missing(tmp_path):
    root = prepare(tmp_path)
    session(root, events=[receipt("zero", START, 0)])
    with collector(root) as c:
        sample = c.poll(END)
        assert_parity(root, sample)
        assert sample["llm"]["total"]["input_tokens"] == 0
        assert sample["llm"]["total"]["responses"] == 1
        assert sample["llm"]["measurable"]
        assert sample["coverage"]["complete"]


def test_cross_thread_duplicate_uses_same_file_order_and_prewindow_identity(tmp_path):
    root = prepare(tmp_path)
    write(root / "roles.json", {"threads": {"zrouter": {"role": "router"}}})
    path = session(
        root, "zrouter", events=[receipt("shared", "2026-01-01T00:00:30Z", 100)]
    )
    path.rename(path.with_name("a.jsonl"))
    path = session(
        root, "achild", "zrouter", "critic_test", events=[receipt("shared", START, 100)]
    )
    path.rename(path.with_name("z.jsonl"))
    with collector(root) as c:
        sample = c.poll(END)
        assert_parity(root, sample)
        assert sample["llm"]["total"]["input_tokens"] == 0
        assert sample["coverage"]["usage_records_observed"] == 1
    with collector(root) as c:
        assert_parity(root, c.poll(END))


def test_token_tail_cutoff_matches_even_with_later_out_of_order_receipt(tmp_path):
    root = prepare(tmp_path)
    session(root, events=[receipt("future", END, 100), receipt("earlier", START, 100)])
    with collector(root) as c:
        sample = c.poll(END)
        assert_parity(root, sample)
        assert sample["llm"]["total"]["responses"] == 0


def test_old_checkpoint_is_rejected_without_rewriting_historical_samples(tmp_path):
    root = prepare(tmp_path)
    session(root, events=[receipt("one", START, 10)])
    with collector(root) as c:
        c.poll(END)
        output = c.output
    checkpoint = output / "checkpoint.json"
    saved = json.loads(checkpoint.read_text())
    saved["version"] = 1
    checkpoint.write_text(json.dumps(saved))
    history = (output / "samples.jsonl").read_bytes()
    with pytest.raises(ValueError, match="new output directory"):
        collector(root)
    assert json.loads(checkpoint.read_text())["version"] == 1
    assert (output / "samples.jsonl").read_bytes() == history


def test_behavior_collector_retains_pending_outputs_until_corrected_receipt(tmp_path):
    root = prepare(tmp_path)
    path = session(
        root,
        events=[
            row(
                "response_item",
                START,
                type="function_call",
                call_id="call",
                name="exec",
                arguments="{}",
            ),
            row(
                "response_item",
                START,
                type="function_call_output",
                call_id="call",
                output="synthetic",
            ),
            row("token_usage_record", START, response_id="request"),
        ],
    )

    def behavior():
        return BehaviorCollector(
            root, root.parent / "behavior", since=START, phase="test"
        )

    with behavior() as c:
        sample = c.poll(END)
        assert sample["llm"]["total"]["responses"] == 0
        assert not sample["coverage"]["complete"]
    append(path, receipt("request", "2026-01-01T00:02:00Z", 20))
    with behavior() as c:
        sample = c.poll(END)
        task = sample["behavior"]["by_task"][0]
        assert task["associated_usage"]["any_output"]["input_tokens"] == 20
        assert sample["llm"]["total"]["responses"] == 1
