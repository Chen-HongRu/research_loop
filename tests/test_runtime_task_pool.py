"""Human ownership and snapshot-bound Judge mutations on real SQLite state."""

from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from research_loop.runtime.store import ConflictError, InvalidTransition, StateStore
from research_loop.runtime.task_pool import TaskPool
from research_loop.runtime.updates import CallIdentity, Updater


def create(identity, **changes):
    return (
        dict(
            action="create",
            id=identity,
            specification={"instruction": identity},
            priority=100,
            dependencies=[],
        )
        | changes
    )


def update(identity, revision=1, **changes):
    return create(identity, action="update", expected_revision=revision) | changes


class RuntimeTaskPoolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = StateStore.create(
            self.root, config={"goal": "Synthetic task planning"}, clock=lambda: 10
        )
        self.context = self.store.coordinator()
        self.owner = self.context.__enter__()
        self.addCleanup(lambda: self.context.__exit__(None, None, None))
        self.pool = TaskPool(self.owner)
        self.sequence = 0

    def task(self, identity):
        return next(
            task for task in self.store.snapshot()["tasks"] if task["id"] == identity
        )

    def rows(self, table):
        with self.store._connection(readonly=True) as conn:
            return [
                dict(row)
                for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]

    def auto(self, identity, **kwargs):
        return self.owner.create_task(
            identity, {"instruction": identity}, source="autonomous", **kwargs
        )

    def pending_limit(self, limit):
        with self.owner._transaction() as (conn, _):
            conn.execute(
                "UPDATE control SET config_json=?",
                (json.dumps({"task_pool": {"max_pending": limit}}),),
            )

    def test_pending_limit_validation_and_zero(self):
        for index, settings in enumerate(
            (
                None,
                [],
                {"max_pending": True},
                {"max_pending": -1},
                {"max_pending": 1.5},
                {"max_pendng": 1},
            )
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                StateStore.create(
                    self.root / f"invalid-{index}", config={"task_pool": settings}
                )
        self.pending_limit(0)
        before = self.store.snapshot()
        with self.assertRaisesRegex(ConflictError, "max_pending"):
            self.auto("direct")
        with self.assertRaisesRegex(ConflictError, "max_pending"):
            self.pool.create_human("human", {}, request_id="human")
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.rows("task_revisions"), [])
        self.assertEqual(self.rows("task_requests"), [])

    def test_pending_limit_counts_blocked_and_excludes_cancelled_and_claimed(self):
        self.pending_limit(2)
        self.auto("dependency")
        self.pool.create_human(
            "blocked", {}, request_id="blocked", dependencies=["dependency"]
        )
        with self.assertRaisesRegex(ConflictError, "max_pending"):
            self.auto("full")
        self.owner.claim_task("dependency")
        receipt = self.pool.create_human("human", {}, request_id="human")
        self.assertEqual(
            self.pool.create_human("human", {}, request_id="human"), receipt
        )
        self.pool.cancel_human("human", 1, request_id="cancel-human")
        self.auto("replacement")
        with self.assertRaisesRegex(ConflictError, "max_pending"):
            self.auto("still-full")

    def test_pending_limit_preserves_over_capacity_history_and_allows_edits(self):
        self.auto("old-a")
        self.auto("old-b")
        self.auto("old-c")
        history = self.rows("task_revisions")
        # Imported histories can already contain more pending tasks than the cap.
        self.pending_limit(1)
        self.assertEqual(self.rows("task_revisions"), history)
        self.pool.edit_human(
            "old-a", 1, request_id="edit", specification={}, priority=0, dependencies=[]
        )
        self.pool.cancel_human("old-a", 2, request_id="cancel-a")
        self.pool.cancel_human("old-b", 1, request_id="cancel-b")
        with self.assertRaisesRegex(ConflictError, "max_pending"):
            self.auto("at-cap")
        self.pool.cancel_human("old-c", 1, request_id="cancel-c")
        self.auto("below-cap")

    def test_pending_limit_serializes_competing_creates(self):
        self.pending_limit(1)
        barrier = threading.Barrier(3)
        outcomes = []

        def create_one(identity):
            barrier.wait()
            try:
                self.pool.create_human(identity, {}, request_id=identity)
                outcomes.append("created")
            except ConflictError:
                outcomes.append("full")

        threads = [
            threading.Thread(target=create_one, args=(name,)) for name in ("a", "b")
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(outcomes, ["created", "full"])
        self.assertEqual(len(self.rows("task_revisions")), 1)

    def test_judge_pending_limit_follows_batch_order_and_replays_conflicts(self):
        self.auto("cancel-me")
        call, _, _ = self.judge(
            [
                create("too-early"),
                dict(action="cancel", id="cancel-me", expected_revision=1),
                create("fits"),
                create("too-late"),
            ]
        )
        self.pending_limit(2)  # cancel-me plus the human planning trigger.
        receipt = self.pool.apply_judge(call)
        self.assertEqual(
            receipt["applied"],
            [{"id": "cancel-me", "revision": 2}, {"id": "fits", "revision": 1}],
        )
        self.assertEqual(
            receipt["conflicts"],
            [
                {"index": 0, "id": "too-early", "reason": "max_pending"},
                {"index": 3, "id": "too-late", "reason": "max_pending"},
            ],
        )
        self.pending_limit(None)
        self.assertEqual(self.pool.apply_judge(call), receipt)
        self.assertNotIn(
            "too-early", [task["id"] for task in self.store.snapshot()["tasks"]]
        )

    def judge(self, changes, *, cite_knowledge=False):
        from research_loop.runtime.planning_state import PlanningLedger

        self.sequence += 1
        self.pool.create_human(
            f"trigger-{self.sequence}",
            {},
            request_id=f"trigger-{self.sequence}",
            priority=10000,
        )
        ledger = PlanningLedger(self.owner)
        round_record = ledger.begin()
        calls = ledger.calls(round_record["id"])
        for slot, attempt in calls.items():
            if slot == "advocate_a":
                advocate = CallIdentity.from_attempt(
                    self.store.snapshot()["study_id"], attempt
                )
                updater = Updater(
                    self.owner, validators={"advocate": lambda output, _: output}
                )
                updater.bind_context(advocate, ledger.context(advocate))
                proposed = updater.validate_output(advocate, {"proposals": []}, [])
                updater.apply_update(proposed, proposed.revisions)
            else:
                self.owner.fail_attempt(
                    attempt["id"],
                    attempt["worker_token"],
                    f"synthetic {slot} failure",
                    expected_revision=None,
                )
        attempt = ledger.reserve_judge(round_record["id"])
        call = CallIdentity.from_attempt(self.store.snapshot()["study_id"], attempt)
        updater = Updater(self.owner, validators={"judge": lambda output, _: output})
        updater.bind_context(call, ledger.context(call))
        refs = [round_record["knowledge_ref"]] if cite_knowledge else []
        proposal = updater.validate_output(call, {"changes": changes}, refs)
        updater.apply_update(proposal, proposal.revisions)
        return call, round_record, ledger

    def replace_artifact(self, reference):
        path = self.root / reference
        path.chmod(0o644)
        path.write_text('{"replacement":"changed after Judge acceptance"}')
        path.chmod(0o444)

    def test_human_exact_request_replay_survives_claim_and_reopen(self):
        receipt = self.pool.create_human(
            "request", {"instruction": "original"}, request_id="human-1", priority=20
        )
        self.assertEqual(
            self.pool.create_human(
                "request",
                {"instruction": "original"},
                request_id="human-1",
                priority=20,
            ),
            receipt,
        )
        self.assertEqual(len(self.rows("planning_events")), 1)
        self.assertEqual(len(self.rows("task_revisions")), 1)
        with self.assertRaises(ConflictError):
            self.pool.create_human(
                "request", {"instruction": "changed"}, request_id="human-1", priority=20
            )
        self.owner.claim_task("request")
        self.context.__exit__(None, None, None)
        self.context = self.store.coordinator()
        self.owner = self.context.__enter__()
        self.pool = TaskPool(self.owner)
        self.assertEqual(
            self.pool.create_human(
                "request",
                {"instruction": "original"},
                request_id="human-1",
                priority=20,
            ),
            receipt,
        )
        self.assertEqual(len(self.rows("task_revisions")), 1)
        self.assertEqual(len(self.rows("planning_events")), 1)

    def test_human_edit_promotes_autonomous_and_cancellation_is_permanent(self):
        self.auto("automatic")
        edited = self.pool.edit_human(
            "automatic",
            1,
            request_id="takeover",
            specification={"instruction": "operator revision"},
            priority=2,
            dependencies=[],
        )
        self.assertEqual((edited["revision"], edited["source"]), (2, "human"))
        cancelled = self.pool.cancel_human("automatic", 2, request_id="cancel")
        self.assertTrue(cancelled["cancelled"])
        self.assertEqual(
            self.pool.cancel_human("automatic", 2, request_id="cancel"), cancelled
        )
        history = self.rows("task_revisions")
        self.assertEqual(
            [(row["revision"], row["source"], row["origin_kind"]) for row in history],
            [(1, "autonomous", "direct"), (2, "human", "human"), (3, "human", "human")],
        )
        self.assertEqual(
            [row["producer_key"] for row in self.rows("planning_events")],
            ["human:takeover", "human:cancel"],
        )
        with self.assertRaises(ConflictError):
            self.pool.create_human("automatic", {}, request_id="resurrect")
        with self.assertRaises(InvalidTransition):
            self.pool.edit_human(
                "automatic",
                3,
                request_id="edit-cancelled",
                specification={},
                priority=1,
                dependencies=[],
            )
        with self.assertRaises(InvalidTransition):
            self.owner.claim_task("automatic", 3)

    def test_human_cycle_missing_dependencies_and_stale_edits_are_atomic(self):
        self.pool.create_human("a", {}, request_id="a")
        self.pool.create_human("b", {}, request_id="b", dependencies=["a"])
        before = self.store.snapshot()
        events = self.rows("planning_events")
        for dependencies in (["b"], ["missing"]):
            with self.assertRaises(ConflictError):
                self.pool.edit_human(
                    "a",
                    1,
                    request_id=str(dependencies),
                    specification={},
                    priority=0,
                    dependencies=dependencies,
                )
        with self.assertRaises(ConflictError):
            self.pool.cancel_human("a", 2, request_id="stale")
        with self.assertRaises(ValueError):
            self.pool.create_human("invalid", {}, request_id="invalid", priority=True)
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.rows("planning_events"), events)
        self.assertEqual(len(self.rows("task_requests")), 2)

    def test_human_priority_tier_dependencies_and_claimed_terminal_freeze(self):
        self.auto("auto", priority=-100)
        self.pool.create_human("slow", {}, request_id="slow", priority=999)
        self.pool.create_human("ready", {}, request_id="ready", priority=1)
        self.pool.create_human(
            "blocked", {}, request_id="blocked", priority=-999, dependencies=["auto"]
        )
        self.pool.create_human("cancelled", {}, request_id="cancelled", priority=-1000)
        self.pool.cancel_human("cancelled", 1, request_id="cancelled-again")
        with self.assertRaises(InvalidTransition):
            self.owner.claim_task("auto")
        iteration = self.owner.claim_next()
        self.assertEqual(iteration["task_id"], "ready")
        attempt = self.owner.begin_attempt(
            "plan",
            request_id="failed-plan",
            iteration_id=iteration["id"],
            expected_revision=1,
        )
        self.owner.fail_attempt(
            attempt["id"],
            attempt["worker_token"],
            "terminal failure",
            expected_revision=1,
        )
        for operation in (
            lambda: self.pool.cancel_human("ready", 1, request_id="cancel-terminal"),
            lambda: self.pool.edit_human(
                "ready",
                1,
                request_id="edit-terminal",
                specification={},
                priority=-100,
                dependencies=[],
            ),
        ):
            with self.assertRaises(InvalidTransition):
                operation()
        self.assertEqual(self.owner.claim_next()["task_id"], "slow")
        self.assertEqual(self.owner.claim_next()["task_id"], "auto")
        self.assertIsNone(self.owner.claim_next())

    def test_human_edit_and_claim_race_freezes_exact_revision(self):
        self.pool.create_human("race", {}, request_id="create-race")
        barrier = threading.Barrier(3)
        outcomes = []

        def edit():
            barrier.wait()
            try:
                outcomes.append(
                    self.pool.edit_human(
                        "race",
                        1,
                        request_id="edit-race",
                        specification={"new": True},
                        priority=0,
                        dependencies=[],
                    )
                )
            except InvalidTransition:
                outcomes.append("claimed-first")

        def claim():
            barrier.wait()
            outcomes.append(self.owner.claim_next())

        threads = [threading.Thread(target=edit), threading.Thread(target=claim)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        iteration = self.store.snapshot()["iterations"][0]
        self.assertEqual(iteration["task_revision"], self.task("race")["revision"])
        self.assertEqual(len(outcomes), 2)
        history = self.rows("task_revisions")
        self.assertEqual(len(history), self.task("race")["revision"])

    def test_human_event_failure_rolls_back_task_history_and_request(self):
        self.auto("atomic")
        before = self.store.snapshot()
        history = self.rows("task_revisions")
        with patch.object(
            self.owner, "_planning_event", side_effect=RuntimeError("event failure")
        ):
            with self.assertRaisesRegex(RuntimeError, "event failure"):
                self.pool.edit_human(
                    "atomic",
                    1,
                    request_id="atomic-edit",
                    specification={"changed": True},
                    priority=1,
                    dependencies=[],
                )
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.rows("task_revisions"), history)
        self.assertEqual(self.rows("task_requests"), [])
        self.assertEqual(self.rows("planning_events"), [])

    def test_snapshot_explains_blocked_human_and_ready_autonomous_without_mutation(
        self,
    ):
        self.auto("cancelled-dependency")
        self.pool.cancel_human(
            "cancelled-dependency", 1, request_id="cancel-dependency"
        )
        self.pool.create_human(
            "blocked-human",
            {},
            request_id="blocked",
            priority=-100,
            dependencies=["cancelled-dependency"],
        )
        self.auto("ready-auto", priority=-1000)
        before = self.store.snapshot()
        with patch.object(
            Path,
            "read_text",
            side_effect=AssertionError("inspection read artifact body"),
        ):
            snapshot = self.pool.snapshot()
        self.assertEqual(snapshot["version"], before["pool_version"])
        by_id = {row["id"]: row for row in snapshot["tasks"]}
        self.assertEqual(
            by_id["blocked-human"]["blocked_reasons"],
            ["dependency:cancelled-dependency:cancelled"],
        )
        self.assertFalse(by_id["blocked-human"]["ready"])
        self.assertEqual(by_id["cancelled-dependency"]["status"], "cancelled")
        self.assertTrue(by_id["ready-auto"]["ready"])
        self.assertEqual(
            [row["id"] for row in snapshot["tasks"]],
            ["blocked-human", "cancelled-dependency", "ready-auto"],
        )
        self.assertEqual(self.store.snapshot(), before)

    def test_snapshot_dependency_reasons_follow_declared_order_and_include_claim_metadata(
        self,
    ):
        self.auto("unclaimed")
        self.auto("active", priority=-100)
        active = self.owner.claim_task("active")
        self.auto("failed", priority=-200)
        failed = self.owner.claim_task("failed")
        attempt = self.owner.begin_attempt(
            "plan",
            request_id="inspection-failed",
            iteration_id=failed["id"],
            expected_revision=1,
        )
        self.owner.fail_attempt(
            attempt["id"],
            attempt["worker_token"],
            "fixture failure",
            expected_revision=1,
        )
        self.pool.create_human(
            "blocked",
            {},
            request_id="blocked-inspection",
            dependencies=["failed", "unclaimed", "active"],
        )
        rows = {row["id"]: row for row in self.pool.snapshot()["tasks"]}
        self.assertEqual(
            rows["blocked"]["blocked_reasons"],
            [
                "dependency:failed:failed",
                "dependency:unclaimed:unclaimed",
                "dependency:active:unpublished",
            ],
        )
        self.assertEqual(rows["active"]["iteration"], active)
        self.assertEqual(rows["active"]["status"], "claimed")
        self.assertFalse(rows["active"]["ready"])

    def test_judge_rejects_cited_knowledge_replacement_before_first_apply(self):
        call, round_record, _ = self.judge(
            [create("must-not-appear")], cite_knowledge=True
        )
        before, history = self.store.snapshot(), self.rows("task_revisions")
        self.replace_artifact(round_record["knowledge_ref"])
        with self.assertRaises(ConflictError):
            self.pool.apply_judge(call)
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.rows("task_revisions"), history)
        self.assertEqual(self.rows("task_batches"), [])

    def test_judge_replay_rechecks_original_accepted_advocate_metadata(self):
        call, round_record, ledger = self.judge([create("committed")])
        receipt = self.pool.apply_judge(call)
        ledger.finish(round_record["id"], "applied", receipt=receipt)
        before, history = self.store.snapshot(), self.rows("task_revisions")
        self.replace_artifact(
            ledger.calls(round_record["id"])["advocate_a"]["output_ref"]
        )
        with self.assertRaises(ConflictError):
            self.pool.apply_judge(call)
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.rows("task_revisions"), history)
        self.assertEqual(len(self.rows("task_batches")), 1)

    def test_judge_commit_rechecks_sources_changed_after_preflight(self):
        call, round_record, _ = self.judge([create("raced")], cite_knowledge=True)
        original_changes = self.pool._changes

        def changed_after_validation(output):
            changes = original_changes(output)
            self.replace_artifact(round_record["knowledge_ref"])
            return changes

        before = self.store.snapshot()
        with patch.object(self.pool, "_changes", side_effect=changed_after_validation):
            with self.assertRaises(ConflictError):
                self.pool.apply_judge(call)
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.rows("task_batches"), [])

    def test_judge_payload_reads_remain_outside_writer_for_apply_and_replay(self):
        call, _, _ = self.judge([create("outside-writer")], cite_knowledge=True)
        inside = [False]
        original_transaction, original_read = self.owner._transaction, Path.read_text

        @contextmanager
        def tracked():
            with original_transaction() as transaction:
                inside[0] = True
                try:
                    yield transaction
                finally:
                    inside[0] = False

        def read_outside(path, *args, **kwargs):
            self.assertFalse(inside[0], "Judge payload read inside writer")
            return original_read(path, *args, **kwargs)

        with (
            patch.object(self.owner, "_transaction", tracked),
            patch.object(Path, "read_text", read_outside),
        ):
            receipt = self.pool.apply_judge(call)
            self.assertEqual(self.pool.apply_judge(call), receipt)

    def test_judge_protected_targets_conflict_without_dropping_valid_changes(self):
        self.auto("claimed", priority=0)
        self.owner.claim_task("claimed")
        self.auto("pending")
        self.pool.create_human("human", {}, request_id="human")
        changes = [
            update("claimed"),
            update("human"),
            update("not-shown"),
            update("pending", priority=5),
            create("new"),
        ]
        call, _, _ = self.judge(changes)
        events = self.rows("planning_events")
        receipt = self.pool.apply_judge(call)
        self.assertEqual(
            receipt["applied"],
            [{"id": "pending", "revision": 2}, {"id": "new", "revision": 1}],
        )
        self.assertEqual(
            [row["reason"] for row in receipt["conflicts"]],
            ["claimed", "human_owned", "not_in_snapshot"],
        )
        self.assertEqual(self.task("new")["source"], "autonomous")
        self.assertEqual(self.rows("planning_events"), events)
        self.assertEqual(self.pool.apply_judge(call), receipt)
        self.assertEqual(len(self.rows("task_batches")), 1)
        self.assertEqual(self.rows("task_revisions")[-1]["origin_kind"], "judge")

    def test_judge_human_promotion_after_snapshot_and_replay_never_downgrades(self):
        self.auto("takeover")
        call, round_record, ledger = self.judge(
            [update("takeover"), create("unrelated")]
        )
        self.pool.edit_human(
            "takeover",
            1,
            request_id="manual",
            specification={"operator": True},
            priority=1,
            dependencies=[],
        )
        receipt = self.pool.apply_judge(call)
        self.assertEqual(receipt["conflicts"][0]["reason"], "human_owned")
        self.assertEqual(receipt["applied"], [{"id": "unrelated", "revision": 1}])
        self.assertEqual(self.task("takeover")["source"], "human")
        ledger.finish(round_record["id"], "applied", receipt=receipt)
        self.context.__exit__(None, None, None)
        self.context = self.store.coordinator()
        self.owner = self.context.__enter__()
        self.pool = TaskPool(self.owner)
        self.assertEqual(self.pool.apply_judge(call), receipt)
        self.assertEqual(self.task("takeover")["specification"], {"operator": True})

    def test_accepted_judge_recovers_before_batch_after_owner_reopen(self):
        self.auto("recovery")
        call, _, _ = self.judge([update("recovery")])
        calls_before = self.store.snapshot()["counters"]["calls"]
        self.context.__exit__(None, None, None)
        self.context = self.store.coordinator()
        self.owner = self.context.__enter__()
        self.pool = TaskPool(self.owner)
        receipt = self.pool.apply_judge(call)
        self.assertEqual(receipt["applied"], [{"id": "recovery", "revision": 2}])
        self.assertEqual(self.store.snapshot()["counters"]["calls"], calls_before)
        self.assertEqual(self.pool.apply_judge(call), receipt)

    def test_judge_cycles_and_missing_dependencies_are_per_entry_conflicts(self):
        self.auto("a")
        self.auto("b", dependencies=["a"])
        call, _, _ = self.judge(
            [
                update("a", dependencies=["b"]),
                create("missing", dependencies=["no-such-task"]),
                create("valid"),
                create("dependent", dependencies=["valid"]),
            ]
        )
        receipt = self.pool.apply_judge(call)
        self.assertEqual(
            [row["reason"] for row in receipt["conflicts"]],
            ["dependency_cycle", "missing_dependency"],
        )
        self.assertEqual(
            [row["id"] for row in receipt["applied"]], ["valid", "dependent"]
        )
        self.assertEqual(self.task("a")["dependencies"], [])

    def test_judge_duplicate_ids_and_source_escalation_reject_entire_batch(self):
        for index, changes in enumerate(
            (
                [create("duplicate"), create("duplicate")],
                [create("valid"), create("escalate", source="human")],
                [{"action": [], "id": "malformed"}],
            )
        ):
            call, round_record, ledger = self.judge(changes)
            before = self.store.snapshot()
            with self.assertRaises(ValueError):
                self.pool.apply_judge(call)
            self.assertEqual(self.store.snapshot(), before)
            self.assertEqual(self.rows("task_batches"), [])
            ledger.finish(
                round_record["id"], "failed", error=f"malformed fixture {index}"
            )

    def test_judge_cannot_guess_a_revision_not_visible_in_active_snapshot(self):
        self.auto("future")
        call, _, _ = self.judge([update("future", 2), create("unrelated")])
        receipt = self.pool.apply_judge(call)
        self.assertEqual(receipt["applied"], [{"id": "unrelated", "revision": 1}])
        self.assertEqual(receipt["conflicts"][0]["reason"], "revision_not_visible")
        self.assertEqual(self.task("future")["revision"], 1)

    def test_judge_cannot_mutate_unseen_task_created_after_context_binding(self):
        call, _, _ = self.judge([update("unseen", 1), create("independent")])
        self.auto("unseen")
        receipt = self.pool.apply_judge(call)
        self.assertEqual(receipt["applied"], [{"id": "independent", "revision": 1}])
        self.assertEqual(receipt["conflicts"][0]["reason"], "not_in_snapshot")
        self.assertEqual(self.task("unseen")["revision"], 1)

    def test_terminal_round_cannot_apply_previously_unapplied_judge(self):
        self.auto("pending")
        call, round_record, ledger = self.judge([update("pending")])
        ledger.finish(
            round_record["id"],
            "failed",
            error="explicit terminal failure",
        )
        before = self.store.snapshot()
        with self.assertRaises(InvalidTransition):
            self.pool.apply_judge(call)
        self.assertEqual(self.store.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
