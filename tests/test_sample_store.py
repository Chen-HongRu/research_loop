"""Sample publication faults through both real collectors and local files."""

import json

import pytest

from research_loop import metrics, sample_store
from research_loop.behavior_metrics import BehaviorCollector
from test_efficiency import receipt
from test_identity import prepare
from test_metrics import END, START, append, session


def open_collector(root, kind):
    cls = metrics.Collector if kind == "ordinary" else BehaviorCollector
    return cls(root, root.parent / "samples", since=START, phase="test")


def read_samples(output):
    return [
        json.loads(line) for line in (output / "samples.jsonl").read_text().splitlines()
    ]


class PartialJournal:
    def __init__(self, stream):
        self.stream = stream

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stream.close()

    def write(self, data):
        self.stream.write(data[: max(1, len(data) // 2)])
        self.stream.flush()
        raise OSError("synthetic partial journal write")


@pytest.mark.parametrize("kind", ["ordinary", "behavior"])
@pytest.mark.parametrize(
    "stage",
    [
        "pending_checkpoint",
        "partial_journal",
        "after_journal",
        "after_latest",
        "before_checkpoint",
        "after_checkpoint",
    ],
)
def test_interrupted_second_sample_recovers_once_and_keeps_new_source_data(
    tmp_path, monkeypatch, kind, stage
):
    root = prepare(tmp_path)
    path = session(root, events=[receipt("one", START, 10)])
    with open_collector(root, kind) as c:
        c.poll(END)
        output = c.output
        first = (output / "samples.jsonl").read_bytes()
        append(path, receipt("two", "2026-01-01T00:02:30Z", 20))
        atomic = sample_store._atomic
        fdopen = sample_store.os.fdopen

        def interrupt_pending(destination, value):
            atomic(destination, value)
            if destination.name == "checkpoint.json":
                raise OSError("synthetic pending checkpoint interruption")

        def interrupt_publication(destination, value):
            if destination.name == "latest.json":
                if stage == "after_journal":
                    raise OSError("synthetic after journal interruption")
                if stage == "after_latest":
                    atomic(destination, value)
                    raise OSError("synthetic after latest interruption")
            if destination.name == "checkpoint.json":
                if stage == "before_checkpoint":
                    raise OSError("synthetic before checkpoint interruption")
                if stage == "after_checkpoint":
                    atomic(destination, value)
                    raise OSError("synthetic after checkpoint interruption")
            atomic(destination, value)

        def partial_journal(fd, mode, **kwargs):
            stream = fdopen(fd, mode, **kwargs)
            return PartialJournal(stream) if mode == "r+b" else stream

        with monkeypatch.context() as patch:
            if stage == "pending_checkpoint":
                patch.setattr(metrics, "_atomic", interrupt_pending)
            elif stage == "partial_journal":
                patch.setattr(sample_store.os, "fdopen", partial_journal)
            else:
                patch.setattr(sample_store, "_atomic", interrupt_publication)
            with pytest.raises(OSError, match="synthetic"):
                c.poll(END)
        assert (output / "samples.jsonl").read_bytes().startswith(first)

    # Recovery must publish the checkpointed sample, not absorb later source data.
    append(path, receipt("three", "2026-01-01T00:02:40Z", 30))
    with open_collector(root, kind) as recovered:
        samples = read_samples(output)
        assert [s["sample_id"] for s in samples] == ["test:1", "test:2"]
        assert [s["llm"]["total"]["responses"] for s in samples] == [1, 2]
        assert [s["llm"]["total"]["input_tokens"] for s in samples] == [10, 30]
        assert all(s["schema_version"] == 2 for s in samples)
        assert (output / "samples.jsonl").read_bytes().startswith(first)
        latest = json.loads((output / "latest.json").read_text())
        checkpoint = json.loads((output / "checkpoint.json").read_text())
        assert latest["sample_id"] == "test:2"
        assert latest["llm"]["total"]["responses"] == 2
        assert "pending_sample" not in checkpoint
        assert checkpoint["sample_offset"] == (output / "samples.jsonl").stat().st_size
        assert recovered.poll(END)["llm"]["total"]["responses"] == 3
    saved = {
        name: (output / name).read_bytes()
        for name in (
            "samples.jsonl",
            "latest.json",
            "checkpoint.json",
        )
    }
    for _ in range(2):
        with open_collector(root, kind):
            assert {name: (output / name).read_bytes() for name in saved} == saved
    assert [s["sample_id"] for s in read_samples(output)] == [
        "test:1",
        "test:2",
        "test:3",
    ]


@pytest.mark.parametrize("kind", ["ordinary", "behavior"])
def test_shortened_committed_journal_is_rejected_without_fabricating_history(
    tmp_path, monkeypatch, kind
):
    root = prepare(tmp_path)
    path = session(root, events=[receipt("one", START, 10)])
    with open_collector(root, kind) as c:
        c.poll(END)
        output = c.output
        append(path, receipt("two", "2026-01-01T00:02:00Z", 20))
        with monkeypatch.context() as patch:

            def interrupt():
                raise OSError("synthetic before publication")

            patch.setattr(c, "_publish_pending", interrupt)
            with pytest.raises(OSError, match="synthetic"):
                c.poll(END)
    journal = output / "samples.jsonl"
    journal.write_bytes(journal.read_bytes()[:-1])
    saved = {
        name: (output / name).read_bytes()
        for name in (
            "samples.jsonl",
            "latest.json",
            "checkpoint.json",
        )
    }
    with pytest.raises(ValueError, match="shorter than committed checkpoint"):
        open_collector(root, kind)
    assert {name: (output / name).read_bytes() for name in saved} == saved


@pytest.mark.parametrize("kind", ["ordinary", "behavior"])
def test_another_interruption_during_recovery_still_publishes_once(
    tmp_path, monkeypatch, kind
):
    root = prepare(tmp_path)
    session(root, events=[receipt("one", START, 10)])
    with open_collector(root, kind) as c:
        output = c.output
        with monkeypatch.context() as patch:

            def interrupt():
                raise OSError("synthetic initial interruption")

            patch.setattr(c, "_publish_pending", interrupt)
            with pytest.raises(OSError, match="synthetic"):
                c.poll(END)
    atomic = sample_store._atomic
    with monkeypatch.context() as patch:

        def interrupt_latest(destination, value):
            atomic(destination, value)
            if destination.name == "latest.json":
                raise OSError("synthetic recovery interruption")

        patch.setattr(sample_store, "_atomic", interrupt_latest)
        with pytest.raises(OSError, match="synthetic recovery"):
            open_collector(root, kind)
    for _ in range(2):
        with open_collector(root, kind):
            assert [s["sample_id"] for s in read_samples(output)] == ["test:1"]
