"""Behavioral checks of the real SQLite coordinator path."""

import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from research_loop.runtime.files import publish_artifact
from research_loop.runtime.store import (
    BudgetExceeded,
    ConflictError,
    InvalidTransition,
    OwnershipError,
    StateStore,
    StateStoreError,
    StoreExistsError,
)


def _crash_owner(root):
    store = StateStore(root, clock=lambda: 20)
    with store.coordinator() as owner:
        owner.create_task("task", {})
        iteration = owner.claim_task("task")
        owner.begin_attempt(
            "plan",
            request_id="crashed",
            iteration_id=iteration["id"],
            expected_revision=1,
        )
        owner.set_stop("operator stop")
        os._exit(0)


def _compete_owner(root, output):
    try:
        with StateStore(root).coordinator():
            output.put("acquired")
    except OwnershipError:
        output.put("locked")


class RuntimeStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = 10

    def store(self, **kwargs):
        return StateStore.create(
            self.root, config={"model": "test"}, clock=lambda: self.now, **kwargs
        )

    def artifact(self, **kwargs):
        return publish_artifact(self.root, kwargs)

    def claim(self, owner, name):
        owner.create_task(name, {"instruction": name})
        return owner.claim_task(name)

    def attempt(self, owner, iteration, role="plan", request_id=None):
        return owner.begin_attempt(
            role,
            request_id=request_id or f"{iteration['id']}-{role}",
            iteration_id=iteration["id"],
            expected_revision=iteration["revision"],
        )

    def accept(self, owner, attempt, reference=None):
        return owner.accept_attempt(
            attempt["id"],
            attempt["worker_token"],
            reference or self.artifact(result="ok"),
            expected_revision=attempt["expected_revision"],
        )

    def test_creation_is_explicit_and_only_database_is_authoritative(self):
        with self.assertRaises(StateStoreError):
            StateStore(self.root)
        self.assertEqual(list(self.root.iterdir()), [])
        store = self.store()
        self.assertTrue((self.root / "runtime.sqlite3").is_file())
        self.assertLessEqual(
            {path.name for path in self.root.iterdir()},
            {"runtime.sqlite3", "runtime.sqlite3-wal", "runtime.sqlite3-shm"},
        )
        with self.assertRaises(StoreExistsError):
            self.store()
        (self.root / "state.json").write_text("broken json")
        (self.root / "tasks.json").write_text('{"calls": -999}')
        snapshot = StateStore(self.root).snapshot()
        self.assertEqual(snapshot["config"], {"model": "test"})
        self.assertEqual(snapshot["counters"]["calls"], 0)
        snapshot["config"]["model"] = "changed snapshot"
        self.assertEqual(store.snapshot()["config"], {"model": "test"})
        json.dumps(snapshot)
        with sqlite3.connect(store.path) as conn:
            conn.execute("PRAGMA user_version=99")
        with self.assertRaises(StateStoreError):
            StateStore(self.root)

    def test_legacy_refused_and_failed_creation_cleans_only_new_file(self):
        legacy = self.root / "state.json"
        legacy.write_text("preserve")
        with self.assertRaises(StoreExistsError):
            self.store()
        self.assertEqual(legacy.read_text(), "preserve")
        legacy.unlink()
        sentinel = self.root / "sentinel"
        sentinel.write_text("preserve")
        with self.assertRaises(RuntimeError):
            StateStore.create(
                self.root,
                config={},
                clock=lambda: (_ for _ in ()).throw(RuntimeError("clock")),
            )
        self.assertEqual(sentinel.read_text(), "preserve")
        self.assertFalse((self.root / "runtime.sqlite3").exists())
        sidecar = self.root / "runtime.sqlite3-wal"
        sidecar.write_text("preserve unknown interrupted database")
        with self.assertRaises(StoreExistsError):
            self.store()
        self.assertEqual(sidecar.read_text(), "preserve unknown interrupted database")
        self.assertFalse((self.root / "runtime.sqlite3").exists())

    def test_independent_iterations_progress_and_failure_is_local(self):
        store = self.store()
        with store.coordinator() as owner:
            first = self.claim(owner, "first")
            second = self.claim(owner, "second")
            self.assertNotEqual(first["id"], second["id"])
            self.assertEqual(owner.claim_task("first"), first)
            attempt = self.attempt(owner, first)
            other = self.attempt(owner, second)
            owner.fail_attempt(
                attempt["id"],
                attempt["worker_token"],
                "worker failed",
                expected_revision=1,
            )
            self.accept(owner, other)
            snapshot = owner.snapshot()
            first, second = snapshot["iterations"]
            self.assertEqual((first["status"], first["revision"]), ("failed", 2))
            self.assertEqual(
                (second["status"], second["phase"], second["revision"]),
                ("active", "execute", 2),
            )
            with self.assertRaises(InvalidTransition):
                self.attempt(owner, first, "execute")
            for role in ("execute", "review"):
                attempt = self.attempt(owner, second, role)
                self.accept(owner, attempt)
                second = owner.snapshot()["iterations"][1]
            self.assertEqual((second["phase"], second["revision"]), ("publication", 4))
            self.assertEqual(owner.snapshot()["counters"]["iterations"], 2)

    def test_request_and_output_idempotency_and_conflicts(self):
        store = self.store(max_calls=1)
        with store.coordinator() as owner:
            iteration = self.claim(owner, "first")
            attempt = self.attempt(owner, iteration, request_id="once")
            self.assertEqual(self.attempt(owner, iteration, request_id="once"), attempt)
            with self.assertRaises(ConflictError):
                owner.begin_attempt("scout", request_id="once")
            reference = self.artifact(value=1)
            receipt = self.accept(owner, attempt, reference)
            self.assertEqual(self.accept(owner, attempt, reference), receipt)
            with self.assertRaises(ConflictError):
                self.accept(owner, attempt, self.artifact(value=2))
            self.assertEqual(owner.snapshot()["iterations"][0]["revision"], 2)
            self.assertEqual(owner.snapshot()["counters"]["calls"], 1)

    def test_real_sqlite_last_call_race_has_one_winner(self):
        store = self.store(max_calls=1)
        barrier = threading.Barrier(3)
        results = []
        with store.coordinator() as owner:

            def reserve(role):
                barrier.wait()
                try:
                    results.append(owner.begin_attempt(role, request_id=role))
                except BudgetExceeded:
                    results.append("exhausted")

            threads = [
                threading.Thread(target=reserve, args=(role,))
                for role in ("scout", "judge")
            ]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive())
            self.assertEqual(results.count("exhausted"), 1)
            self.assertEqual(owner.snapshot()["counters"]["calls"], 1)
            self.assertEqual(len(owner.snapshot()["attempts"]), 1)

    def test_committed_receipt_replays_across_owner_epochs_without_advancing(self):
        store = self.store()
        reference = self.artifact(result="accepted")
        with store.coordinator() as owner:
            iteration = self.claim(owner, "first")
            attempt = self.attempt(owner, iteration)
            receipt = self.accept(owner, attempt, reference)
        before = store.snapshot()
        with store.coordinator() as successor:
            self.assertNotEqual(receipt["owner_epoch"], successor.owner_epoch)
            self.assertEqual(self.accept(successor, attempt, reference), receipt)
            for changes, error in (
                ({"worker_token": "different"}, OwnershipError),
                ({"expected_revision": 2}, ConflictError),
                ({"output_ref": self.artifact(result="changed")}, ConflictError),
            ):
                arguments = {
                    "attempt_id": attempt["id"],
                    "worker_token": attempt["worker_token"],
                    "output_ref": reference,
                    "expected_revision": 1,
                    **changes,
                }
                with self.assertRaises(error):
                    successor.accept_attempt(**arguments)
            after = successor.snapshot()
            self.assertEqual(after["counters"], before["counters"])
            self.assertEqual(after["iterations"], before["iterations"])
            self.assertEqual(after["attempts"], before["attempts"])

    def test_rejections_do_not_spend_calls_or_advance_iterations(self):
        store = self.store()
        with store.coordinator() as owner:
            iteration = self.claim(owner, "first")
            for role, identity, revision, error in (
                ("execute", iteration["id"], 1, InvalidTransition),
                ("plan", iteration["id"], 2, ConflictError),
                ("plan", "unknown", 1, ConflictError),
            ):
                with self.assertRaises(error):
                    owner.begin_attempt(
                        role,
                        request_id=role + identity + str(revision),
                        iteration_id=identity,
                        expected_revision=revision,
                    )
            self.assertEqual(owner.snapshot()["counters"]["calls"], 0)
            attempt = self.attempt(owner, iteration)
            with self.assertRaises(InvalidTransition):
                self.attempt(owner, iteration, request_id="another")
            with self.assertRaises(OwnershipError):
                owner.accept_attempt(
                    attempt["id"], "wrong", self.artifact(), expected_revision=1
                )
            with self.assertRaises(ConflictError):
                owner.accept_attempt(
                    attempt["id"],
                    attempt["worker_token"],
                    self.artifact(),
                    expected_revision=2,
                )
            with self.assertRaises(ConflictError):
                owner.accept_attempt(
                    "missing", "wrong", self.artifact(), expected_revision=1
                )
            for reference in ("../outside.json", "state.json", "/tmp/artifact.json"):
                with self.assertRaises(ValueError):
                    self.accept(owner, attempt, reference)
            self.assertEqual(owner.snapshot()["iterations"][0], iteration)
            self.assertEqual(owner.snapshot()["counters"]["calls"], 1)

    def test_dependencies_immutable_and_task_admission_limit(self):
        store = self.store(max_iterations=1)
        with store.coordinator() as owner:
            owner.create_task("parent", {})
            owner.create_task("dependent", {}, dependencies=["parent"])
            with self.assertRaises(ConflictError):
                owner.create_task("parent", {"changed": True})
            with self.assertRaises(ConflictError):
                owner.create_task("unknown", {}, dependencies=["missing"])
            with self.assertRaises(InvalidTransition):
                owner.claim_task("dependent")
            owner.claim_task("parent")
            with self.assertRaises(BudgetExceeded):
                owner.claim_task("dependent")
            self.assertEqual(owner.snapshot()["counters"]["iterations"], 1)

    def test_os_lock_competition_and_closed_owner_rejected(self):
        store = self.store()
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        with store.coordinator() as owner:
            with self.assertRaises(OwnershipError):
                with StateStore(self.root).coordinator():
                    self.fail("second owner")
            process = context.Process(target=_compete_owner, args=(self.root, queue))
            process.start()
            self.assertEqual(queue.get(timeout=10), "locked")
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        with self.assertRaises(OwnershipError):
            owner.set_stop("closed")
        with self.assertRaises(OwnershipError):
            owner.snapshot()
        with store.coordinator() as successor:
            self.assertEqual(successor.owner_epoch, owner.owner_epoch + 1)

    def test_failed_close_revokes_handle_before_releasing_lock(self):
        store = self.store()
        context = store.coordinator()
        owner = context.__enter__()
        self.now = 15
        with patch.object(
            store, "_connection", side_effect=sqlite3.OperationalError("disk failure")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                context.__exit__(None, None, None)
        with self.assertRaises(OwnershipError):
            owner.create_task("unlocked-owner", {})
        self.now = 20
        with store.coordinator() as successor:
            self.assertEqual(successor.snapshot()["counters"]["elapsed_seconds"], 10)

    @unittest.skipUnless(hasattr(os, "fork"), "requires Unix fork")
    def test_forked_owner_cannot_mutate_or_unlock_parent(self):
        store = self.store()
        context = store.coordinator()
        owner = context.__enter__()
        try:
            pid = os.fork()
            if pid == 0:
                try:
                    try:
                        owner.set_stop("forked")
                    except OwnershipError:
                        context.__exit__(None, None, None)
                        os._exit(0)
                    os._exit(1)
                except BaseException:
                    os._exit(2)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            with self.assertRaises(OwnershipError):
                with store.coordinator():
                    self.fail("fork released parent's lock")
            owner.create_task("still-live", {})
        finally:
            context.__exit__(None, None, None)

    def test_crash_reopen_preserves_stop_counts_and_charges_open_interval(self):
        store = self.store(max_seconds=100)
        context = multiprocessing.get_context("spawn")
        process = context.Process(target=_crash_owner, args=(self.root,))
        process.start()
        process.join(10)
        self.assertEqual(process.exitcode, 0)
        before = store.snapshot()
        old = before["attempts"][0]
        self.now = 35
        with store.coordinator() as owner:
            snapshot = owner.snapshot()
            self.assertEqual(
                snapshot["counters"],
                {"calls": 1, "iterations": 1, "elapsed_seconds": 15},
            )
            self.assertEqual(snapshot["stop"]["reason"], "operator stop")
            with self.assertRaises(OwnershipError):
                self.accept(owner, old)
            replay = owner.begin_attempt(
                "plan",
                request_id="crashed",
                iteration_id=old["iteration_id"],
                expected_revision=1,
            )
            self.assertEqual(replay, old)
            self.assertLess(replay["owner_epoch"], owner.owner_epoch)
            with self.assertRaises(BudgetExceeded):
                owner.begin_attempt("scout", request_id="new")
            owner.set_stop("do not replace original stop")
        self.assertEqual(store.snapshot()["stop"]["reason"], "operator stop")

    def test_unknown_old_attempt_is_not_released_or_refunded(self):
        store = self.store()
        with store.coordinator() as owner:
            iteration = self.claim(owner, "first")
            attempt = self.attempt(owner, iteration, request_id="original")
        with store.coordinator() as successor:
            self.assertEqual(
                self.attempt(successor, iteration, request_id="original"), attempt
            )
            with self.assertRaises(InvalidTransition):
                self.attempt(successor, iteration, request_id="retry")
            with self.assertRaises(OwnershipError):
                successor.fail_attempt(
                    attempt["id"],
                    attempt["worker_token"],
                    "retry",
                    expected_revision=1,
                )
            self.assertEqual(successor.snapshot()["counters"]["calls"], 1)

    def test_parallel_advocates_have_independent_request_identities(self):
        store = self.store()
        with store.coordinator() as owner:
            first = owner.begin_attempt("advocate", request_id="proposal-a")
            second = owner.begin_attempt("advocate", request_id="proposal-b")
            self.assertNotEqual(first["worker_token"], second["worker_token"])
            self.accept(owner, first)
            self.accept(owner, second)
            self.assertEqual(owner.snapshot()["counters"]["calls"], 2)

    def test_positive_wall_budget_finite_clock_and_sqlite_durability(self):
        for seconds in (0, -1, float("inf"), float("nan"), True):
            with self.assertRaises(ValueError):
                self.store(max_seconds=seconds)
        with self.assertRaises(ValueError):
            StateStore.create(self.root, config={}, clock=lambda: float("nan"))
        store = self.store()
        with store._connection() as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2)

    def test_wall_time_shared_graceful_downtime_and_backward_clock(self):
        store = self.store(max_seconds=10)
        with store.coordinator() as owner:
            owner.begin_attempt("scout", request_id="one")
            owner.begin_attempt("judge", request_id="two")
            self.now = 13
            owner.create_task("first", {})
            self.assertEqual(owner.snapshot()["counters"]["elapsed_seconds"], 3)
            self.now = 12
            owner.create_task("second", {})
            self.assertEqual(owner.snapshot()["counters"]["elapsed_seconds"], 3)
            self.now = 15
        self.now = 100
        with store.coordinator() as owner:
            self.assertEqual(owner.snapshot()["counters"]["elapsed_seconds"], 5)
            self.now = 105
            for _ in range(2):
                with self.assertRaises(BudgetExceeded):
                    owner.claim_task("first")
            self.now = 108
            owner.set_stop("bookkeeping survives exhaustion")
        self.assertEqual(store.snapshot()["counters"]["elapsed_seconds"], 13)
        self.assertEqual(store.snapshot()["counters"]["iterations"], 0)

    def test_creation_window_blocks_new_calls_after_closed_owner_downtime(self):
        store = StateStore.create(
            self.root,
            config={
                "budget": {
                    "max_calls": None,
                    "max_iterations": None,
                    "window_seconds": 300,
                }
            },
            clock=lambda: self.now,
        )
        with store.coordinator() as owner:
            original = owner.begin_attempt("scout", request_id="before")
            owner.fail_attempt(
                original["id"], original["worker_token"], "done", expected_revision=None
            )
        self.now += 301
        with store.coordinator() as owner:
            with self.assertRaisesRegex(BudgetExceeded, "admission window"):
                owner.begin_attempt("scout", request_id="after")
            owner.set_stop("cleanup")
        self.assertEqual(store.snapshot()["counters"]["calls"], 1)

    def test_deadline_blocks_claim_but_not_recovery_bookkeeping_or_active_work(self):
        store = self.store(admission_deadline=15)
        with store.coordinator() as owner:
            first = self.claim(owner, "first")
            owner.create_task("second", {})
            self.now = 16
            with self.assertRaises(BudgetExceeded):
                owner.claim_task("second")
            attempt = self.attempt(owner, first)
            self.accept(owner, attempt)
            owner.set_stop("finished admissions")
            self.assertEqual(owner.claim_task("first")["id"], first["id"])
        with store.coordinator() as owner:
            owner.set_stop("recovery")
            self.assertEqual(owner.snapshot()["stop"]["reason"], "finished admissions")

    def test_stop_blocks_admission_but_can_accept_already_reserved_output(self):
        store = self.store()
        with store.coordinator() as owner:
            iteration = self.claim(owner, "first")
            attempt = self.attempt(owner, iteration)
            owner.set_stop("stop")
            self.accept(owner, attempt)
            owner.create_task("second", {})
            with self.assertRaises(BudgetExceeded):
                owner.claim_task("second")
            with self.assertRaises(BudgetExceeded):
                owner.begin_attempt("judge", request_id="denied")

    def test_evidence_metadata_checked_without_reading_contents(self):
        store = self.store()
        with store.coordinator() as owner:
            attempt = owner.begin_attempt("scout", request_id="one")
            reference = self.artifact()
            path = self.root / reference
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                self.accept(owner, attempt, reference)
            path.chmod(0o444)
            self.accept(owner, attempt, reference)
            # Metadata detects replacement without rereading/hashing evidence.
            path.unlink()
            path.write_text("replacement")
            path.chmod(0o444)
            with self.assertRaises(ConflictError):
                self.accept(owner, attempt, reference)


if __name__ == "__main__":
    unittest.main()
