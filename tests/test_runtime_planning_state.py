"""Frozen planning inputs and short transaction boundaries on real SQLite."""

from contextlib import contextmanager
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from research_loop.runtime.context import ContextSnapshot, build_context
from research_loop.runtime import planning_state
from research_loop.runtime.planning_state import PlanningLedger
from research_loop.runtime.store import ConflictError, StateStore
from research_loop.runtime.updates import CallIdentity, Updater


class PlanningStateTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = StateStore.create(
            temporary.name,
            config={"goal": "Synthetic planning", "permissions": {}},
            clock=lambda: 1,
        )
        session = self.store.coordinator()
        self.owner = session.__enter__()
        self.addCleanup(lambda: session.__exit__(None, None, None))
        self.ledger = PlanningLedger(self.owner)
        self.updater = Updater(
            self.owner,
            validators={
                "advocate": lambda output, _: output,
                "judge": lambda output, _: output,
            },
        )
        self.ledger.trigger("start", "pool_low", {"version": 0})

    def call(self, attempt):
        return CallIdentity.from_attempt(self.store.snapshot()["study_id"], attempt)

    def accept(self, call, output):
        self.updater.bind_context(call, self.ledger.context(call))
        proposal = self.updater.validate_output(call, output, [])
        self.updater.apply_update(proposal, proposal.revisions)

    def altered(self, package, key, value):
        # Construct an otherwise internally valid package with a forged protected
        # input, rather than relying on incidental manifest token-count failures.
        context, manifest = package.context, package.manifest
        context[key] = value
        sources = [
            {
                name: source[name]
                for name in ("id", "key", "ref", "revision", "reason", "allow_feedback")
            }
            for source in manifest["sources"]
        ]
        snapshot = ContextSnapshot.capture(
            study_id=manifest["study_id"],
            revisions=manifest["revisions"],
            inputs=context,
            sources=sources,
            protected_keys=manifest["protected_keys"],
            allowed_evidence=manifest["allowed_evidence"],
        )
        return build_context(
            snapshot, manifest["role"], None, manifest["policy_ref"], manifest["budget"]
        )

    def test_changed_event_intent_is_rejected_without_losing_original(self):
        before = self.ledger.pending()
        self.assertEqual(
            self.ledger.trigger("start", "pool_low", {"version": 0}), before[0]
        )
        with self.assertRaises(ConflictError):
            self.ledger.trigger("start", "pool_low", {"version": 1})
        self.assertEqual(self.ledger.pending(), before)

    def test_claims_select_only_task_metadata_inside_writer(self):
        self.owner.create_task("A", {"instruction": "Synthetic payload A"})
        self.owner.create_task("B", {"instruction": "Synthetic payload B"})
        original = self.owner._transaction

        @contextmanager
        def metadata_only():
            with original() as (conn, control):

                def authorize(action, table, column, *_):
                    if (
                        action == sqlite3.SQLITE_READ
                        and table == "tasks"
                        and column == "specification_json"
                    ):
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                conn.set_authorizer(authorize)
                yield conn, control

        with patch.object(self.owner, "_transaction", metadata_only):
            self.assertEqual(self.owner.claim_next()["task_id"], "A")
            self.assertEqual(self.owner.claim_task("B")["task_id"], "B")

    def test_new_event_during_capture_prevents_stale_admission_without_spending_calls(
        self,
    ):
        original = planning_state.render_research

        def render(snapshot, publications):
            self.ledger.trigger("new-input", "human_task", {"id": "fresh"})
            return original(snapshot, publications)

        with patch.object(planning_state, "render_research", render):
            with self.assertRaisesRegex(ConflictError, "advanced"):
                self.ledger.begin()
        self.assertEqual(self.store.snapshot()["counters"]["calls"], 0)
        self.assertEqual(len(self.ledger.pending()), 2)
        round_record = self.ledger.begin()
        self.assertEqual(
            (round_record["event_from"], round_record["event_through"]), (1, 2)
        )
        self.assertEqual(self.store.snapshot()["counters"]["calls"], 2)

    def test_pool_and_advocate_inputs_are_protected_but_research_selection_is_replaceable(
        self,
    ):
        round_record = self.ledger.begin()
        calls = self.ledger.calls(round_record["id"])
        a, b = self.call(calls["advocate_a"]), self.call(calls["advocate_b"])
        package = self.ledger.context(
            a,
            policy=lambda *_: {
                "research": {
                    "action": "truncate",
                    "characters": 3,
                    "reason": "Synthetic quota",
                }
            },
        )
        self.assertEqual(package.context["research"], "# R")
        forged = self.altered(package, "planning", {})
        with self.assertRaisesRegex(ConflictError, "planning"):
            self.updater.bind_context(a, forged)
        self.updater.bind_context(a, package)
        proposal = self.updater.validate_output(a, {"proposals": []}, [])
        self.updater.apply_update(proposal, proposal.revisions)
        self.owner.fail_attempt(
            b.attempt_id, b.worker_token, "Synthetic failure", expected_revision=None
        )
        judge = self.call(self.ledger.reserve_judge(round_record["id"]))
        package = self.ledger.context(judge)
        self.assertEqual(package.context["advocate_a"]["output"], {"proposals": []})
        self.assertEqual(package.context["advocate_b"]["status"], "failed")
        forged = self.altered(package, "advocate_a", {"status": "failed"})
        with self.assertRaisesRegex(ConflictError, "advocate_a"):
            self.updater.bind_context(judge, forged)
        self.updater.bind_context(judge, package)

    def test_replaced_frozen_knowledge_is_rejected_before_feedback_acceptance(self):
        round_record = self.ledger.begin()
        call = self.call(self.ledger.calls(round_record["id"])["advocate_a"])
        self.updater.bind_context(call, self.ledger.context(call))
        proposal = self.updater.validate_output(call, {"proposals": []}, [])
        path = self.store.root / round_record["knowledge_ref"]
        replacement = path.with_suffix(".replacement")
        replacement.write_text(
            json.dumps({"version": 0, "research": "replaced", "frontiers": None})
        )
        replacement.chmod(0o444)
        replacement.replace(path)
        with self.assertRaises(ConflictError):
            self.updater.apply_update(proposal, proposal.revisions)
        self.assertEqual(
            self.ledger.calls(round_record["id"])["advocate_a"]["status"], "active"
        )

    def test_capture_context_and_feedback_body_reads_never_hold_write_transaction(self):
        inside = [False]
        original_transaction = self.owner._transaction
        original_read = Path.read_text

        @contextmanager
        def transaction():
            with original_transaction() as value:
                inside[0] = True
                try:
                    yield value
                finally:
                    inside[0] = False

        def read(path, *args, **kwargs):
            self.assertFalse(
                inside[0], "Planning read a payload in a write transaction"
            )
            return original_read(path, *args, **kwargs)

        with (
            patch.object(self.owner, "_transaction", transaction),
            patch.object(Path, "read_text", read),
        ):
            round_record = self.ledger.begin()
            a = self.call(self.ledger.calls(round_record["id"])["advocate_a"])
            self.accept(a, {"proposals": []})
        self.assertEqual(
            self.ledger.calls(round_record["id"])["advocate_a"]["status"], "accepted"
        )


if __name__ == "__main__":
    unittest.main()
