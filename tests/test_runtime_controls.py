"""Trusted human ingress using real owner locking and SQLite, with no workers."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_runtime_backend_scheduler as backend_fixtures
import test_runtime_dispatch as dispatch_fixtures
from research_loop.runtime.controls import ControlInbox, receipt, submit_command
from research_loop.runtime.dispatch import RuntimeEvents
from research_loop.runtime.files import publish_artifact, publish_json
from research_loop.runtime.store import ConflictError, StateStore
from research_loop.runtime.task_pool import TaskPool


class RuntimeControlsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = StateStore.create(self.root, config={})
        self.context = self.store.coordinator()
        self.owner = self.context.__enter__()
        self.addCleanup(lambda: self.context.__exit__(None, None, None))
        self.inbox = ControlInbox(self.owner)

    def queue_task(self, request_id="create", **changes):
        payload = dict(id="human", specification={}, priority=100, dependencies=[])
        payload.update(changes)
        return submit_command(self.root, "task_create", payload, request_id)

    def test_concurrent_process_publishes_without_writer_ownership(self):
        script = """
import json, sys
from research_loop.runtime.controls import submit_command
from research_loop.runtime.store import StateStore, OwnershipError
root = sys.argv[1]
try:
    with StateStore(root).coordinator():
        raise AssertionError('second owner acquired lock')
except OwnershipError:
    pass
print(json.dumps(submit_command(root, 'planning', {'reason':'human request'}, 'child')))
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.root)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(result.stdout)["status"], "queued")
        self.assertIsNone(receipt(self.root, "child"))
        self.assertEqual(self.owner.snapshot()["counters"]["calls"], 0)
        self.inbox.drain()
        self.assertEqual(receipt(self.root, "child")["status"], "applied")
        self.assertEqual(self.owner.snapshot()["counters"]["calls"], 0)

    def test_task_commands_and_human_priority(self):
        self.owner.create_task("auto", {}, source="autonomous", priority=-10000)
        self.queue_task()
        self.inbox.drain()
        self.assertEqual(receipt(self.root, "create")["result"]["source"], "human")
        submit_command(
            self.root,
            "task_edit",
            dict(
                id="human",
                expected_revision=1,
                specification={"goal": "test"},
                priority=5,
                dependencies=[],
            ),
            "edit",
        )
        self.inbox.drain()
        self.assertEqual(receipt(self.root, "edit")["result"]["revision"], 2)
        self.queue_task("second", id="second")
        self.inbox.drain()
        self.assertEqual(self.owner.claim_next()["task_id"], "human")
        submit_command(
            self.root, "task_cancel", dict(id="second", expected_revision=1), "cancel"
        )
        self.inbox.drain()
        self.assertTrue(receipt(self.root, "cancel")["result"]["cancelled"])

    def test_replay_after_domain_commit_before_command_receipt(self):
        self.queue_task()
        with patch.object(self.inbox, "_finish", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.inbox.drain()
        self.assertEqual(receipt(self.root, "create")["status"], "pending")
        self.assertEqual(TaskPool(self.owner).snapshot()["tasks"][0]["revision"], 1)
        # SQLite's pending request survives even if its inbox pointer is lost.
        for pointer in (self.root / "command-inbox").glob("*.json"):
            pointer.unlink()
        self.inbox.drain()
        saved = receipt(self.root, "create")
        self.assertEqual(saved["status"], "applied")
        self.assertEqual(TaskPool(self.owner).snapshot()["tasks"][0]["revision"], 1)
        self.assertEqual(self.queue_task(), saved)

    def test_duplicate_id_conflict_preserves_original(self):
        first = self.queue_task()
        self.assertEqual(self.queue_task()["request_ref"], first["request_ref"])
        with self.assertRaises(ConflictError):
            self.queue_task(priority=3)
        self.inbox.drain()
        original = receipt(self.root, "create")
        with self.assertRaises(ConflictError):
            self.queue_task(priority=4)
        self.assertEqual(receipt(self.root, "create"), original)

    def test_owner_rejects_changed_id_artifact_without_overwrite(self):
        self.queue_task()
        self.inbox.drain()
        original = receipt(self.root, "create")
        reference = publish_artifact(
            self.root, dict(version=1, request_id="create", kind="scout", payload={})
        )
        publish_json(
            self.root / "command-inbox" / "different.json", dict(request_ref=reference)
        )
        self.assertEqual(self.inbox.drain()[0]["status"], "rejected")
        self.assertEqual(receipt(self.root, "create"), original)
        self.assertEqual(self.owner.snapshot()["counters"]["calls"], 0)

    def test_invalid_command_isolation_and_strict_publication(self):
        reference = publish_artifact(
            self.root,
            dict(
                version=1,
                request_id="invalid",
                kind="stop",
                payload={"reason": "missing boolean"},
            ),
        )
        folder = self.root / "command-inbox"
        folder.mkdir()
        publish_json(folder / "invalid.json", dict(request_ref=reference))
        self.queue_task()
        self.inbox.drain()
        self.assertEqual(receipt(self.root, "invalid")["status"], "rejected")
        self.assertEqual(receipt(self.root, "create")["status"], "applied")
        for kind, payload, rid in (
            ("stop", {"reason": "stop"}, "a"),
            ("stop", {"reason": "stop", "cancel_running": 1}, "b"),
            ("scout", {"request_id": "inject"}, "c"),
            ("scout", {}, " "),
            (
                "task_create",
                dict(
                    id="x",
                    specification={"x": float("nan")},
                    priority=1,
                    dependencies=[],
                ),
                "d",
            ),
            ("task_cancel", dict(id="x", expected_revision=True), "e"),
        ):
            with self.subTest(kind=kind, rid=rid), self.assertRaises(ValueError):
                submit_command(self.root, kind, payload, rid)

    def test_stop_does_not_execute_and_ack_is_idempotent(self):
        submit_command(
            self.root, "stop", dict(reason="human stop", cancel_running=False), "stop"
        )
        with patch(
            "research_loop.runtime.backend_scheduler.BackendScheduler.schedule",
            side_effect=AssertionError("must not execute"),
        ):
            self.inbox.drain()
        self.assertEqual(self.owner.snapshot()["stop"]["reason"], "human stop")
        self.assertEqual(self.owner.snapshot()["counters"]["calls"], 0)
        self.assertEqual(receipt(self.root, "stop")["status"], "applied")
        event = RuntimeEvents(self.owner).pending("human")[0]
        submit_command(
            self.root, "ack_event", dict(consumer="human", event_id=event["id"]), "ack"
        )
        self.inbox.drain()
        self.assertEqual(RuntimeEvents(self.owner).pending("human"), [])
        self.assertEqual(
            submit_command(
                self.root,
                "ack_event",
                dict(consumer="human", event_id=event["id"]),
                "ack",
            )["status"],
            "applied",
        )

    def test_scout_only_reserves_one_attempt_across_replay(self):
        submit_command(self.root, "scout", {}, "scout")
        with patch.object(self.inbox, "_finish", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.inbox.drain(admit_calls=True)
        self.inbox.drain(admit_calls=True)
        self.assertEqual(receipt(self.root, "scout")["status"], "applied")
        self.assertEqual(self.owner.snapshot()["counters"]["calls"], 1)

    def test_default_drain_defers_scout_until_execution_owner(self):
        submit_command(self.root, "scout", {}, "scout")
        self.inbox.drain()
        self.assertEqual(receipt(self.root, "scout")["status"], "pending")
        self.assertIsNone(receipt(self.root, "scout")["error"])
        self.assertEqual(self.owner.snapshot()["counters"]["calls"], 0)
        self.assertEqual(self.owner.snapshot()["attempts"], [])
        prior_epoch = self.owner.owner_epoch
        self.context.__exit__(None, None, None)
        self.context = self.store.coordinator()
        self.owner = self.context.__enter__()
        self.inbox = ControlInbox(self.owner)
        self.assertGreater(self.owner.owner_epoch, prior_epoch)
        self.inbox.drain(admit_calls=True)
        self.inbox.drain(admit_calls=True)
        self.assertEqual(receipt(self.root, "scout")["status"], "applied")
        self.assertEqual(self.owner.snapshot()["counters"]["calls"], 1)
        attempts = self.owner.snapshot()["attempts"]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["owner_epoch"], self.owner.owner_epoch)

    def test_disabled_planning_is_rejected_without_call(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore.create(
                directory, config={"runtime": {"planning": False}}
            )
            submit_command(directory, "planning", {"reason": "human request"}, "plan")
            with store.coordinator() as owner:
                ControlInbox(owner).drain()
                self.assertEqual(receipt(directory, "plan")["status"], "rejected")
                self.assertEqual(owner.snapshot()["counters"]["calls"], 0)

    def test_owner_unlinks_pointer_during_duplicate_publication(self):
        self.queue_task()
        import research_loop.runtime.controls as controls

        def finish_before_link(path, value):
            self.inbox.drain()
            # Reproduce link() reporting EEXIST before the owner unlinks it.
            raise FileExistsError()

        with patch.object(controls, "publish_json", side_effect=finish_before_link):
            duplicate = self.queue_task()
        self.assertEqual(duplicate["status"], "applied")
        self.assertEqual(duplicate, receipt(self.root, "create"))

    def test_artifact_corruption_preserves_pending_effect(self):
        self.queue_task()
        with patch.object(self.inbox, "_finish", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.inbox.drain()
        row = receipt(self.root, "create")
        path = self.root / row["request_ref"]
        path.chmod(0o644)
        self.inbox.drain()
        self.assertEqual(receipt(self.root, "create")["status"], "pending")
        self.assertTrue(receipt(self.root, "create")["error"])
        self.assertEqual(TaskPool(self.owner).snapshot()["tasks"][0]["revision"], 1)


class BackendControlTests(unittest.TestCase):
    # Use existing local synthetic operation setup; no processes/backend jobs.
    setUp = backend_fixtures.BackendSchedulerTests.setUp
    components = backend_fixtures.BackendSchedulerTests.components
    reserve = backend_fixtures.BackendSchedulerTests.reserve
    bind = backend_fixtures.BackendSchedulerTests.bind
    accept_result = backend_fixtures.BackendSchedulerTests.accept_result
    manual = backend_fixtures.BackendSchedulerTests.manual
    iteration = backend_fixtures.BackendSchedulerTests.iteration
    execute = backend_fixtures.BackendSchedulerTests.execute
    pass_once = backend_fixtures.BackendSchedulerTests.pass_once
    drain = backend_fixtures.BackendSchedulerTests.drain
    submitted = backend_fixtures.BackendSchedulerTests.submitted
    terminal = backend_fixtures.BackendSchedulerTests.terminal

    def test_cancel_and_stop_only_publish_requests(self):
        _, opid = self.submitted()
        calls = list(self.invoker.calls)
        submit_command(
            self.root,
            "cancel_operation",
            dict(operation_id=opid, reason="human cancel"),
            "cancel",
        )
        ControlInbox(self.owner).drain()
        self.assertEqual(receipt(self.root, "cancel")["status"], "applied")
        submit_command(
            self.root, "stop", dict(reason="human stop", cancel_running=True), "stop"
        )
        ControlInbox(self.owner).drain()
        self.assertEqual(
            receipt(self.root, "stop")["result"]["cancellation_requests"], [opid]
        )
        self.assertEqual(self.invoker.calls, calls)
        events = [
            e
            for e in RuntimeEvents(self.owner).pending("test")
            if e["kind"] == "backend_cancel_requested"
        ]
        self.assertEqual(len(events), 2)
        self.assertEqual(self.operations.get(opid)["state"], "submitted")

    def test_stop_partial_effect_remains_pending_and_replays(self):
        _, opid = self.submitted()
        submit_command(
            self.root, "stop", dict(reason="human stop", cancel_running=True), "stop"
        )
        with patch(
            "research_loop.runtime.backend_scheduler.BackendScheduler.request_cancel",
            side_effect=OSError("synthetic interrupted write"),
        ):
            ControlInbox(self.owner).drain()
        self.assertEqual(receipt(self.root, "stop")["status"], "pending")
        self.assertEqual(self.owner.snapshot()["stop"]["reason"], "human stop")
        ControlInbox(self.owner).drain()
        self.assertEqual(receipt(self.root, "stop")["status"], "applied")
        self.assertEqual(
            receipt(self.root, "stop")["result"]["cancellation_requests"], [opid]
        )

    def test_retry_action_only_records_trusted_request(self):
        _, opid = self.terminal()
        self.invoker.behavior["collection"] = ["failed"]
        self.pass_once()
        before = len(self.invoker.calls)
        submit_command(
            self.root,
            "retry_action",
            dict(operation_id=opid, action="collection", reason="human approved retry"),
            "retry",
        )
        ControlInbox(self.owner).drain()
        self.assertEqual(receipt(self.root, "retry")["status"], "applied")
        self.assertEqual(len(self.invoker.calls), before)
        with self.assertRaises(ValueError):
            submit_command(
                self.root,
                "retry_action",
                dict(operation_id=opid, action="submit", reason="forbidden"),
                "bad",
            )


class ReviewControlTests(unittest.TestCase):
    setUp = dispatch_fixtures.RuntimeDispatchTests.setUp
    _components = dispatch_fixtures.RuntimeDispatchTests._components
    reserve = dispatch_fixtures.RuntimeDispatchTests.reserve
    bind = dispatch_fixtures.RuntimeDispatchTests.bind
    accept_result = dispatch_fixtures.RuntimeDispatchTests.accept_result
    manual = dispatch_fixtures.RuntimeDispatchTests.manual
    iteration = dispatch_fixtures.RuntimeDispatchTests.iteration
    execution = dispatch_fixtures.RuntimeDispatchTests.execution
    completion = dispatch_fixtures.RuntimeDispatchTests.completion

    def test_explicit_failed_review_retry_reserves_without_execution(self):
        execute, _ = self.execution([])
        call = self.reserve("review", self.iteration(execute.iteration_id))
        placeholder = publish_artifact(
            self.root, {"synthetic_retirement": call.attempt_id}
        )
        run = self.ledger.register_orphan_for_retirement(
            call.attempt_id, placeholder, dispatch_fixtures.ADAPTER, 1000
        )["run"]
        observation = self.completion(run, status="not_started")
        self.ledger.observe(run["token"], observation)
        self.owner.fail_attempt(
            call.attempt_id,
            call.worker_token,
            "synthetic interrupted review",
            expected_revision=call.expected_revision,
        )
        failed = self.iteration(execute.iteration_id)
        payload = dict(
            iteration_id=failed["id"],
            expected_revision=failed["revision"],
            reason="human retry",
            proof_ref=observation["proof_ref"],
        )
        submit_command(self.root, "retry_review", payload, "review")
        before = self.owner.snapshot()["counters"]["calls"]
        ControlInbox(self.owner).drain()
        self.assertEqual(receipt(self.root, "review")["status"], "pending")
        self.assertIsNone(receipt(self.root, "review")["error"])
        self.assertEqual(self.owner.snapshot()["counters"]["calls"], before)
        ControlInbox(self.owner).drain(admit_calls=True)
        self.assertEqual(receipt(self.root, "review")["status"], "applied")
        self.assertEqual(self.owner.snapshot()["counters"]["calls"], before + 1)
        self.assertEqual(self.iteration(failed["id"])["phase"], "review")
        self.assertEqual(
            submit_command(self.root, "retry_review", payload, "review")["status"],
            "applied",
        )
        self.assertEqual(self.owner.snapshot()["counters"]["calls"], before + 1)


if __name__ == "__main__":
    unittest.main()
