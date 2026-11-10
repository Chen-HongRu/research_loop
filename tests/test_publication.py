"""Real filesystem commit/recovery, with synthetic research results only."""

import asyncio
import json

import pytest

from research_loop import human_input, loop, publication
from research_loop.state import Knowledge, writer
from test_human_input import outcome
from test_loop import CONTINUE, prepare, scripted_native
from test_native import RESULT


@pytest.mark.parametrize("action", ["continue", "stop"])
def test_commit_and_repeated_recovery_through_public_entries(tmp_path, action):
    root = prepare(tmp_path)
    human_input.submit(root, "Synthetic request")
    request = human_input.admit(root)
    result = outcome(request, study_action=action)
    state = {
        "iteration": 1,
        "status": "running",
        "result": result,
        "human_request": request,
    }
    baseline = Knowledge(root).prepare()
    with writer(root):
        assert asyncio.run(publication.commit(root, state, baseline)) is state
        committed = Knowledge(root).published_state()
        assert state["status"] == (
            "iteration-completed" if action == "continue" else "completed"
        )
        assert state["knowledge_version"] == committed["knowledge_version"]
        if action == "continue":
            assert state["next_iteration"] == committed["next_iteration"] == 2
        else:
            assert "next_iteration" not in state
        assert human_input.admit(root) is None

        # The pointer is authoritative when live status has not been saved.
        recovered = asyncio.run(
            publication.recover(root, {"iteration": 1, "status": "failed"})
        )
        assert recovered == committed
        paths = [
            root / "status.json",
            root / "result.json",
            root / "iterations/000001/status.json",
        ]
        saved = [path.read_bytes() for path in paths]
        assert json.loads(saved[1]) == result
        assert json.loads(saved[2]) == committed
        assert asyncio.run(publication.recover(root, recovered)) is recovered
        assert [path.read_bytes() for path in paths] == saved
        assert Knowledge(root).published_state() == committed


def test_restart_after_activation_keeps_new_request_without_research_replay(
    tmp_path, monkeypatch
):
    root = prepare(tmp_path)
    human_input.submit(root, "First request")
    first = human_input.admit(root)
    publish = Knowledge.publish

    def interrupted_publish(*args, **kwargs):
        publish(*args, **kwargs)
        raise RuntimeError("synthetic interruption after activation")

    factory, clients = scripted_native(monkeypatch, [outcome(first)])
    with monkeypatch.context() as patch:
        patch.setattr(Knowledge, "publish", interrupted_publish)
        with pytest.raises(RuntimeError, match="after activation"):
            asyncio.run(loop.run(root, native_factory=factory))
    assert human_input.admit(root) == first
    with human_input.input_lock(root) as path:
        path.write_text("New request after interruption")
    newer = human_input.admit(root)
    committed = Knowledge(root).published_state()

    for _ in range(2):
        state = asyncio.run(loop.run(root, native_factory=factory))
        assert state == committed
        assert state["status"] == "completed"
        assert human_input.admit(root) == newer
        assert Knowledge(root).published_state() == committed
    assert len(clients) == 1


def test_recovery_keeps_newer_failed_iteration_and_pending_request(
    tmp_path, monkeypatch
):
    root = prepare(tmp_path)
    human_input.submit(root, "First request")
    first = human_input.admit(root)

    def replace_request(number, packet):
        if number == 1:
            with human_input.input_lock(root) as path:
                path.write_text("Second request")

    factory, _ = scripted_native(
        monkeypatch, [outcome(first, **CONTINUE), RESULT], replace_request, fail_at=2
    )
    with pytest.raises(RuntimeError, match="intermediate failure"):
        asyncio.run(loop.run(root, native_factory=factory))
    newer = human_input.admit(root)
    state = json.loads((root / "status.json").read_text())
    assert state["iteration"] == 2 and state["status"] == "failed"
    assert state["human_request"] == newer
    assert Knowledge(root).published_state()["iteration"] == 1
    paths = [
        root / "status.json",
        root / "result.json",
        root / "iterations/000001/status.json",
        root / "iterations/000002/status.json",
    ]
    saved = [path.read_bytes() for path in paths]
    with writer(root):
        for _ in range(2):
            assert asyncio.run(publication.recover(root, state)) is state
            assert human_input.admit(root) == newer
            assert [path.read_bytes() for path in paths] == saved
