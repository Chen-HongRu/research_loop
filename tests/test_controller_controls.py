"""Publication/control races at actual filesystem commit and admission boundaries."""

import asyncio
import json
import threading

import pytest

from research_loop import application, publication
from research_loop.configuration import prepare_configuration
from research_loop.controller import Controller
from research_loop.interventions import record_intervention
from research_loop.state import PublicationStore, write_json
from test_application import author


def test_invalid_workflow_revision_is_rejected_before_stopping_or_activation(
    tmp_path, monkeypatch
):
    async def scenario():
        root, package, _ = author(tmp_path, "simple")
        instance = Controller(root)
        original = instance.workflow
        (package / "workflow.py").write_text(
            "def create(settings):\n    raise RuntimeError('Invalid synthetic workflow')\n"
        )
        prepare_configuration(
            package.parent / "run.yaml",
            root / "configurations/rejected-revision",
            revision="rejected-revision",
        )
        application.intervene(
            root,
            kind="revise",
            payload={"revision": "rejected-revision", "affected": ["all"]},
            identity="invalid-workflow",
        )

        async def must_not_stop(*args):
            raise AssertionError("Invalid revision stopped existing work")

        monkeypatch.setattr(instance, "_stop_targets", must_not_stop)
        try:
            await instance._controls()
            receipt = application.get_intervention(root, "invalid-workflow")
            assert receipt["status"] == "rejected"
            assert "Invalid synthetic workflow" in receipt["reason"]
            assert (
                json.loads((root / "active.json").read_text())["revision"] == "r000001"
            )
            assert instance.config["revision"] == "r000001"
            assert instance.workflow is original
        finally:
            instance.journal.close()

    asyncio.run(scenario())


def proposal(action, **fields):
    return {
        "action": action,
        "result": {"answer": 42},
        "artifacts": [{"content": "42", "destination": "answer.txt"}],
        **fields,
    }


def controller(tmp_path, action):
    root, _, _ = author(tmp_path, "simple")
    instance = Controller(root)
    instance.workflow.complete = lambda *_: proposal(action)
    return root, instance


def stage_gate(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    stage = publication._stage

    def gated(*args):
        entered.set()
        if not release.wait(3):
            raise TimeoutError("Synthetic publication staging gate timed out")
        return stage(*args)

    monkeypatch.setattr(publication, "_stage", gated)
    return entered, release


async def finish(instance):
    return await instance._dispatch(
        {"action": "finish_round", "result_ref": str(instance.root / "goal.md")}
    )


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0)


def test_task_during_terminal_staging_is_rejected_and_does_not_block_next_input(
    tmp_path, monkeypatch
):
    async def scenario():
        root, instance = controller(tmp_path, "stop")
        entered, release = stage_gate(monkeypatch)
        dispatch = asyncio.create_task(finish(instance))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            application.intervene(
                root,
                kind="task",
                payload={"text": "arrived during staging"},
                identity="during-commit",
            )
            instance.wake.set()
            await until(lambda: instance.wake.is_set() is False)
            assert (
                application.get_intervention(root, "during-commit")["status"]
                == "received"
            )
            assert instance.routing["active_input"] is None
            release.set()
            assert await dispatch is True
            assert instance.state["status"] == "completed"
            assert (
                application.get_intervention(root, "during-commit")["status"]
                == "rejected"
            )
            assert instance.routing["active_input"] is None
            assert (
                json.loads((root / "routing.json").read_text())["active_input"] is None
            )
            assert PublicationStore(root).published_state()["status"] == "completed"
        finally:
            release.set()
            await asyncio.gather(dispatch, return_exceptions=True)
            instance.journal.close()
        application.intervene(
            root,
            kind="task",
            payload={"text": "new task after completion"},
            identity="next-task",
        )
        replacement = Controller(root)
        try:
            await replacement._controls()
            assert replacement.routing["active_input"] == "next-task"
            assert (
                application.get_intervention(root, "next-task")["status"] == "pending"
            )
            assert (
                application.get_intervention(root, "during-commit")["status"]
                == "rejected"
            )
        finally:
            replacement.journal.close()

    asyncio.run(scenario())


def test_continuation_defers_input_and_older_continue_cannot_override_newer_pause(
    tmp_path, monkeypatch
):
    async def scenario():
        root, instance = controller(tmp_path, "continue")
        entered, release = stage_gate(monkeypatch)
        dispatch = asyncio.create_task(finish(instance))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            application.intervene(
                root,
                kind="task",
                payload={"text": "next iteration task"},
                identity="deferred-task",
            )
            application.intervene(root, kind="continue", identity="older-continue")
            application.intervene(root, kind="pause", identity="newer-pause")
            instance.wake.set()
            await until(
                lambda: (
                    application.get_intervention(root, "newer-pause")["status"]
                    == "applied"
                )
            )
            assert (
                application.get_intervention(root, "older-continue")["status"]
                == "applied"
            )
            assert (
                application.get_intervention(root, "deferred-task")["status"]
                == "received"
            )
            assert instance.runtime.observe()["paused"]
            assert instance.routing["active_input"] is None
            release.set()
            assert await dispatch is False
            assert instance.routing["iteration"] == 2
            assert instance.runtime.observe()["paused"]
            await instance._controls()
            assert (
                application.get_intervention(root, "deferred-task")["status"]
                == "received"
            )
            application.intervene(
                root, kind="continue", identity="continue-next-boundary"
            )
            await instance._controls()
            assert not instance.runtime.observe()["paused"]
            assert instance.routing["active_input"] == "deferred-task"
            admitted = application.get_intervention(root, "deferred-task")
            assert admitted["status"] == "pending"
            assert admitted["acceptance"] == "next-admission"
            assert "work_outcome" not in admitted
        finally:
            release.set()
            await asyncio.gather(dispatch, return_exceptions=True)
            instance.journal.close()

    asyncio.run(scenario())


def test_continue_after_activation_cannot_change_terminal_publication_state(
    tmp_path, monkeypatch
):
    async def scenario():
        root, instance = controller(tmp_path, "stop")
        save = publication._save_publication
        loop = asyncio.get_running_loop()

        def inject_continue(root, state):
            save(root, state)
            assert PublicationStore(root).published_state()["status"] == "completed"
            application.intervene(root, kind="continue", identity="after-activation")
            loop.call_soon_threadsafe(instance.wake.set)

        monkeypatch.setattr(publication, "_save_publication", inject_continue)
        try:
            assert await finish(instance) is True
            assert instance.state["status"] == "completed"
            assert application.status(root)["status"] == "completed"
            assert PublicationStore(root).published_state()["status"] == "completed"
            assert (
                application.get_intervention(root, "after-activation")["status"]
                == "rejected"
            )
            assert instance.routing["active_input"] is None
        finally:
            instance.journal.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("newer_active_input", [False, True])
def test_activated_continuation_repairs_stale_routing_without_losing_newer_input(
    tmp_path, monkeypatch, newer_active_input
):
    async def scenario():
        root, _, _ = author(tmp_path, "simple")
        application.intervene(
            root,
            kind="task",
            payload={"text": "completed old task"},
            identity="old-task",
        )
        baseline = PublicationStore(root).prepare()
        await publication.commit(
            root,
            {"iteration": 1, "status": "running"},
            baseline,
            proposal(
                "continue", input_outcome={"id": "old-task", "status": "completed"}
            ),
        )
        active_input = "old-task"
        if newer_active_input:
            application.intervene(
                root,
                kind="task",
                payload={"text": "separate newer task"},
                identity="newer-task",
            )
            record_intervention(
                root, "newer-task", "pending", acceptance="next-admission"
            )
            active_input = "newer-task"
        write_json(root / "status.json", {"iteration": 1, "status": "failed"})
        write_json(
            root / "routing.json",
            {
                "iteration": 1,
                "admitted": ["old-run"],
                "tasks": [["job", "old-task.md", None]],
                "active_input": active_input,
                "recoveries": [],
            },
        )
        instance = Controller(root)

        def stop_at_local_channel_boundary():
            # Recovery has run; finish without opening a socket or admitting work.
            instance.stopping = True

        monkeypatch.setattr(instance, "_open_channel", stop_at_local_channel_boundary)
        await instance.run()
        routing = json.loads((root / "routing.json").read_text())
        assert routing["iteration"] == 2
        assert routing["admitted"] == []
        assert routing["tasks"] == []
        assert routing["active_input"] == ("newer-task" if newer_active_input else None)
        assert (
            application.get_intervention(root, "old-task")["work_outcome"]
            == "completed"
        )
        if newer_active_input:
            assert (
                application.get_intervention(root, "newer-task")["status"] == "pending"
            )
        assert list((root / "group-runs").glob("*/run.json")) == []
        assert len(list((root / "outputs/publications").glob("*.json"))) == 1
        assert PublicationStore(root).published_state()["next_iteration"] == 2

    asyncio.run(scenario())


def test_intervention_id_replay_returns_original_receipt_and_conflicts_fail(tmp_path):
    root, _, _ = author(tmp_path, "simple")
    original = application.intervene(root, kind="pause", identity="stable-id")
    assert application.intervene(root, kind="pause", identity="stable-id") == original
    applied = record_intervention(root, "stable-id", "applied", control_state="paused")
    assert application.intervene(root, kind="pause", identity="stable-id") == applied
    with pytest.raises(ValueError, match="different request"):
        application.intervene(
            root,
            kind="pause",
            payload={"reason": "changed request"},
            identity="stable-id",
        )
    assert application.get_intervention(root, "stable-id") == applied
