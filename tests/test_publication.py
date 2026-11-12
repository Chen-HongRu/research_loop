"""Generic publication and recovery with real filesystem activation boundaries."""

import asyncio
import json
from pathlib import Path

import pytest

from research_loop import publication
from research_loop.interventions import get_intervention, submit_intervention
from research_loop.state import PublicationStopped, PublicationStore, writer


def request(root, identity):
    submit_intervention(
        root,
        {
            "id": identity,
            "kind": "task",
            "expected_revision": "revision-1",
            "payload": {"task": "synthetic work"},
        },
    )


def proposal(action="stop", **changes):
    return {
        "action": action,
        "result": {"answer": 42},
        "artifacts": [{"content": "# Answer\n\n42\n", "destination": "answer.md"}],
        **changes,
    }


@pytest.mark.parametrize("action", ["continue", "stop"])
def test_commit_and_repeated_recovery_are_generic(tmp_path, action):
    request(tmp_path, "first")
    state = {"iteration": 1, "status": "running"}
    store = PublicationStore(tmp_path)
    baseline = store.prepare()
    original = proposal(action, input_outcome={"id": "first", "status": "completed"})
    with writer(tmp_path):
        assert (
            asyncio.run(publication.commit(tmp_path, state, baseline, original))
            is state
        )
    committed = store.published_state()
    assert committed == state
    assert committed["action"] == action
    assert committed["proposal"] == original
    assert committed["status"] == (
        "iteration-completed" if action == "continue" else "completed"
    )
    assert committed.get("next_iteration") == (2 if action == "continue" else None)
    assert (store.current / "answer.md").read_text() == "# Answer\n\n42\n"
    receipt = get_intervention(tmp_path, "first")
    assert receipt["status"] == "applied"
    assert receipt["work_outcome"] == "completed"
    assert receipt["publication"] == committed["publication_version"]
    recovered = asyncio.run(
        publication.recover(tmp_path, {"iteration": 1, "status": "failed"})
    )
    assert recovered == committed
    paths = [
        tmp_path / "status.json",
        tmp_path / "result.json",
        tmp_path / "iterations/000001/status.json",
        tmp_path / "iterations/000001/result.json",
        tmp_path / "interventions/receipts/first.json",
    ]
    saved = [path.read_bytes() for path in paths]
    assert asyncio.run(publication.recover(tmp_path, recovered)) is recovered
    assert [path.read_bytes() for path in paths] == saved
    assert json.loads((tmp_path / "result.json").read_text()) == {"answer": 42}


def test_stop_before_activation_does_not_acknowledge_input(tmp_path, monkeypatch):
    import shutil

    request(tmp_path, "first")
    store = PublicationStore(tmp_path)
    baseline = store.prepare()
    state = {"iteration": 1, "status": "running"}
    copytree = shutil.copytree

    def stop_after_version_copy(source, destination, *args, **kwargs):
        copied = copytree(source, destination, *args, **kwargs)
        if Path(destination).parent == store.root / "versions":
            (tmp_path / "STOP").write_text("stop\n")
        return copied

    monkeypatch.setattr("research_loop.state.shutil.copytree", stop_after_version_copy)
    with pytest.raises(PublicationStopped, match="before publication activation"):
        asyncio.run(
            publication.commit(
                tmp_path,
                state,
                baseline,
                proposal(input_outcome={"id": "first", "status": "completed"}),
            )
        )
    assert str(store.current.resolve()) == baseline
    assert store.published_state() is None
    assert get_intervention(tmp_path, "first")["status"] == "received"
    assert state == {"iteration": 1, "status": "running"}
    assert not (tmp_path / "result.json").exists()


def test_crash_after_activation_recovers_without_affecting_new_input(
    tmp_path, monkeypatch
):
    request(tmp_path, "first")
    store = PublicationStore(tmp_path)
    baseline = store.prepare()
    publish = PublicationStore.publish

    def interrupted(*args, **kwargs):
        publish(*args, **kwargs)
        raise RuntimeError("crash after activation")

    with monkeypatch.context() as patch:
        patch.setattr(PublicationStore, "publish", interrupted)
        with pytest.raises(RuntimeError, match="crash after activation"):
            asyncio.run(
                publication.commit(
                    tmp_path,
                    {"iteration": 1, "status": "running"},
                    baseline,
                    proposal(input_outcome={"id": "first", "status": "completed"}),
                )
            )
    assert get_intervention(tmp_path, "first")["status"] == "received"
    request(tmp_path, "newer")
    newer = get_intervention(tmp_path, "newer")
    for _ in range(2):
        recovered = asyncio.run(
            publication.recover(tmp_path, {"iteration": 1, "status": "failed"})
        )
        assert recovered == store.published_state()
        assert get_intervention(tmp_path, "first")["work_outcome"] == "completed"
        assert get_intervention(tmp_path, "newer") == newer
    assert len(list((store.root / "versions").iterdir())) == 2


def test_recovery_retains_newer_unfinished_iteration(tmp_path):
    store = PublicationStore(tmp_path)
    baseline = store.prepare()
    asyncio.run(
        publication.commit(tmp_path, {"iteration": 1}, baseline, proposal("continue"))
    )
    state = {"iteration": 2, "status": "failed", "result": {"error": "new attempt"}}
    (tmp_path / "status.json").write_text(json.dumps(state))
    (tmp_path / "result.json").write_text(json.dumps(state["result"]))
    saved = [(tmp_path / name).read_bytes() for name in ("status.json", "result.json")]
    assert asyncio.run(publication.recover(tmp_path, state)) is state
    assert [
        (tmp_path / name).read_bytes() for name in ("status.json", "result.json")
    ] == saved


def test_staging_copies_only_declared_outputs_and_previous_publication(tmp_path):
    source = tmp_path / "selected.txt"
    source.write_text("selected output")
    store = PublicationStore(tmp_path)
    baseline = store.prepare()
    (store.draft / "unproposed.txt").write_text("unfinished unrelated edit")
    asyncio.run(
        publication.commit(
            tmp_path,
            {"iteration": 1},
            baseline,
            proposal(
                artifacts=[
                    {"source": str(source), "destination": "nested/selected.txt"}
                ]
            ),
        )
    )
    assert not (store.current / "unproposed.txt").exists()
    assert (store.current / "nested/selected.txt").read_text() == "selected output"
    source.write_text("later source mutation")
    assert (store.current / "nested/selected.txt").read_text() == "selected output"
    baseline = store.prepare()
    asyncio.run(publication.commit(tmp_path, {"iteration": 2}, baseline, proposal()))
    assert (store.current / "nested/selected.txt").read_text() == "selected output"
    assert (store.current / "answer.md").exists()


@pytest.mark.parametrize(
    "destination",
    ["/outside", "../outside", "a/../outside", "a/./b", "a//b", ".", "", "a\x00b"],
)
def test_invalid_destination_is_rejected_without_changing_pointer(
    tmp_path, destination
):
    store = PublicationStore(tmp_path)
    baseline = store.prepare()
    with pytest.raises(ValueError, match="relative"):
        asyncio.run(
            publication.commit(
                tmp_path,
                {"iteration": 1},
                baseline,
                proposal(artifacts=[{"content": "small", "destination": destination}]),
            )
        )
    assert str(store.current.resolve()) == baseline


@pytest.mark.parametrize(
    "destinations",
    [("same", "same"), ("nested", "nested/file"), ("nested/file", "nested")],
)
def test_duplicate_or_conflicting_destinations_are_rejected(tmp_path, destinations):
    store = PublicationStore(tmp_path)
    baseline = store.prepare()
    with pytest.raises(ValueError, match="unique"):
        asyncio.run(
            publication.commit(
                tmp_path,
                {"iteration": 1},
                baseline,
                proposal(
                    artifacts=[
                        {"content": "small", "destination": name}
                        for name in destinations
                    ]
                ),
            )
        )


def test_sparse_oversized_source_and_symlink_are_not_read(tmp_path):
    source = tmp_path / "large.bin"
    with source.open("wb") as stream:
        stream.truncate(publication.MAX_FILE_BYTES + 1)
    store = PublicationStore(tmp_path)
    baseline = store.prepare()
    with pytest.raises(ValueError, match="4 MiB"):
        asyncio.run(
            publication.commit(
                tmp_path,
                {"iteration": 1},
                baseline,
                proposal(
                    artifacts=[{"source": str(source), "destination": "large.bin"}]
                ),
            )
        )
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(ValueError, match="regular"):
        asyncio.run(
            publication.commit(
                tmp_path,
                {"iteration": 1},
                baseline,
                proposal(
                    artifacts=[{"source": str(link), "destination": "linked.bin"}]
                ),
            )
        )
    assert str(store.current.resolve()) == baseline


def test_selected_content_aggregate_limit_precedes_staging(tmp_path):
    store = PublicationStore(tmp_path)
    baseline = store.prepare()
    text = "x" * publication.MAX_FILE_BYTES
    artifacts = [
        {"content": text, "destination": f"part-{number}"} for number in range(5)
    ]
    with pytest.raises(ValueError, match="16 MiB"):
        asyncio.run(
            publication.commit(
                tmp_path, {"iteration": 1}, baseline, proposal(artifacts=artifacts)
            )
        )
    assert not (store.draft / "part-0").exists()
    assert str(store.current.resolve()) == baseline
