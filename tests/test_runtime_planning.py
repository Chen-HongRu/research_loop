"""Bounded planning dispatch and deterministic recovery on real SQLite state."""

from collections import Counter
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from research_loop.runtime.planning import PlanningRunner
from research_loop.runtime.planning_state import PlanningLedger
from research_loop.runtime.store import BudgetExceeded, InvalidTransition, StateStore
from research_loop.runtime.task_pool import TaskPool


def create(identity):
    return dict(
        action="create",
        id=identity,
        specification={"instruction": identity},
        priority=100,
        dependencies=[],
    )


class RuntimePlanningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.open_store(self.root)
        self.addCleanup(lambda: self.session.__exit__(None, None, None))
        self.main_thread = threading.get_ident()

    def open_store(self, root, *, max_calls=100):
        self.store = StateStore.create(
            root,
            config={"goal": "Synthetic bounded planning"},
            clock=lambda: 10,
            max_calls=max_calls,
        )
        self.session = self.store.coordinator()
        self.owner = self.session.__enter__()
        self.ledger = PlanningLedger(self.owner)
        self.pool = TaskPool(self.owner)

    def trigger(self, identity="event"):
        return self.ledger.trigger(identity, "manual", {"reason": identity})

    def validate_advocate(self, output, context):
        self.assertEqual(threading.get_ident(), self.main_thread)
        if set(output) != {"summary"} or not isinstance(output["summary"], str):
            raise ValueError("Expected an Advocate summary")
        return output

    def validate_judge(self, output, context):
        self.assertEqual(threading.get_ident(), self.main_thread)
        if set(output) != {"changes"} or not isinstance(output["changes"], list):
            raise ValueError("Expected Judge changes")
        return output

    def runner(self, invoke=None, *, policy=None, validators=None):
        return PlanningRunner(
            self.owner,
            invoke or self.invoke,
            validators
            or {"advocate": self.validate_advocate, "judge": self.validate_judge},
            policy=policy,
        )

    def invoke(self, call, package):
        output = (
            {"summary": "Bounded proposal"}
            if call.role == "advocate"
            else {"changes": [create("proposed")]}
        )
        return dict(output=output, evidence_refs=[])

    def rows(self, table):
        with self.store._connection(readonly=True) as conn:
            return [
                dict(row)
                for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]

    def test_idle_event_deduplication_and_one_bounded_round(self):
        runner = self.runner()
        self.assertIsNone(runner.run_once())
        self.assertEqual(self.store.snapshot()["counters"]["calls"], 0)
        event = self.trigger()
        self.assertEqual(self.trigger(), event)
        result = runner.run_once()
        self.assertEqual(result["status"], "applied")
        self.assertEqual(
            result["receipt"]["applied"], [{"id": "proposed", "revision": 1}]
        )
        self.assertEqual(self.store.snapshot()["counters"]["calls"], 3)
        self.assertEqual(self.ledger.pending(), [])
        self.assertIsNone(runner.run_once())
        self.assertEqual(self.store.snapshot()["counters"]["calls"], 3)

    def test_old_native_round_library_metadata_does_not_block_judge_application(self):
        import json
        from research_loop.runtime import planning_state

        original = planning_state.publish_artifact

        def historical_manifest(root, value):
            if isinstance(value, dict) and "round_id" in value:
                value = dict(
                    value, knowledge_libraries=[{"alias": "study", "version": 99}]
                )
            return original(root, value)

        self.trigger()
        with patch.object(planning_state, "publish_artifact", historical_manifest):
            result = self.runner().run_once()
        self.assertEqual(result["status"], "applied")
        self.assertEqual(
            result["receipt"]["applied"], [{"id": "proposed", "revision": 1}]
        )
        record = self.rows("planning_rounds")[0]
        raw = json.loads((self.root / record["snapshot_ref"]).read_text())
        self.assertEqual(raw["knowledge_libraries"][0]["version"], 99)
        self.assertEqual(self.store.snapshot()["counters"]["calls"], 3)

    def test_advocates_overlap_and_updates_remain_on_coordinator_thread(self):
        self.trigger()
        barrier = threading.Barrier(2)
        observed = []
        lock = threading.Lock()

        def invoke(call, package):
            if call.role == "advocate":
                self.assertNotEqual(threading.get_ident(), self.main_thread)
                barrier.wait(timeout=3)
            else:
                self.assertEqual(threading.get_ident(), self.main_thread)
            with lock:
                observed.append(call.role)
            return self.invoke(call, package)

        result = self.runner(invoke).run_once()
        self.assertEqual(result["status"], "applied")
        self.assertEqual(Counter(observed), {"advocate": 2, "judge": 1})
        self.assertEqual({row["status"] for row in self.rows("attempts")}, {"accepted"})

    def test_one_failed_advocate_still_uses_survivor_and_existing_pool(self):
        self.pool.create_human(
            "existing", {"instruction": "Human task"}, request_id="human"
        )
        lock = threading.Lock()
        count = 0
        seen_pool = []

        def invoke(call, package):
            nonlocal count
            if call.role == "advocate":
                with lock:
                    count += 1
                    current = count
                if current == 1:
                    raise RuntimeError("Synthetic Advocate failure")
            else:
                seen_pool.extend(
                    task["id"] for task in package.context["planning"]["pool"]["tasks"]
                )
            return self.invoke(call, package)

        result = self.runner(invoke).run_once()
        self.assertEqual(result["status"], "applied")
        self.assertEqual(seen_pool, ["existing"])
        advocates = [row for row in self.rows("attempts") if row["role"] == "advocate"]
        self.assertEqual(
            Counter(row["status"] for row in advocates), {"failed": 1, "accepted": 1}
        )
        self.assertEqual(
            {row["id"] for row in self.store.snapshot()["tasks"]},
            {"existing", "proposed"},
        )
        self.assertEqual(self.store.snapshot()["iterations"], [])

    def test_both_advocates_failed_do_not_reserve_judge_or_retry(self):
        self.trigger()
        observed = []

        def invoke(call, package):
            observed.append(call.role)
            raise RuntimeError("x" * 2000)

        runner = self.runner(invoke)
        result = runner.run_once()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(observed, ["advocate", "advocate"])
        self.assertTrue(
            all(len(row["failure_reason"]) <= 1000 for row in self.rows("attempts"))
        )
        self.assertEqual(self.store.snapshot()["tasks"], [])
        self.assertIsNone(runner.run_once())
        self.assertEqual(self.store.snapshot()["counters"]["calls"], 2)

    def test_new_event_during_round_remains_for_next_round(self):
        self.trigger("first")
        produced = False

        def validate(output, context):
            nonlocal produced
            if not produced:
                self.trigger("during-round")
                produced = True
            return self.validate_advocate(output, context)

        runner = self.runner(
            validators={"advocate": validate, "judge": self.validate_judge}
        )
        first = runner.run_once()
        self.assertEqual(first["status"], "applied")
        self.assertEqual(len(self.ledger.pending()), 1)
        second = runner.run_once()
        self.assertEqual(second["status"], "applied")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.ledger.pending(), [])
        self.assertEqual(self.store.snapshot()["counters"]["calls"], 6)

    def test_context_preflight_failure_settles_both_undispatched_slots(self):
        self.trigger()
        invoked = []

        def policy(*args):
            raise ValueError("Synthetic context preflight failure")

        result = self.runner(
            lambda *args: invoked.append(args), policy=policy
        ).run_once()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(invoked, [])
        self.assertEqual(
            [row["status"] for row in self.rows("attempts")], ["failed", "failed"]
        )
        self.assertEqual(self.store.snapshot()["counters"]["calls"], 2)

    def test_stop_after_reservation_preserves_advocates_but_prevents_judge(self):
        self.trigger()
        stopped = False

        def validate(output, context):
            nonlocal stopped
            if not stopped:
                self.owner.set_stop("Operator stop after reservation")
                stopped = True
            return self.validate_advocate(output, context)

        result = self.runner(
            validators={"advocate": validate, "judge": self.validate_judge}
        ).run_once()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            [row["status"] for row in self.rows("attempts")], ["accepted", "accepted"]
        )
        self.assertEqual(self.store.snapshot()["counters"]["calls"], 2)
        self.assertEqual(self.store.snapshot()["tasks"], [])

    def test_call_budget_applies_to_both_advocates_and_judge(self):
        self.session.__exit__(None, None, None)
        self.open_store(self.root / "limited", max_calls=2)
        self.trigger()
        result = self.runner().run_once()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.store.snapshot()["counters"]["calls"], 2)
        self.assertEqual(
            [row["role"] for row in self.rows("attempts")], ["advocate", "advocate"]
        )
        self.assertEqual(self.store.snapshot()["tasks"], [])

    def test_advocate_pair_admission_is_atomic_when_budget_is_too_small(self):
        self.session.__exit__(None, None, None)
        self.open_store(self.root / "insufficient", max_calls=1)
        self.trigger()
        with self.assertRaises(BudgetExceeded):
            self.runner().run_once()
        self.assertEqual(self.store.snapshot()["counters"]["calls"], 0)
        self.assertEqual(len(self.ledger.pending()), 1)

    def test_judge_exception_or_malformed_callback_only_fails_own_attempt(self):
        self.trigger()

        def invoke(call, package):
            if call.role == "judge":
                return {
                    "output": {"changes": []},
                    "evidence_refs": [],
                    "unknown": "field",
                }
            return self.invoke(call, package)

        result = self.runner(invoke).run_once()
        self.assertEqual(result["status"], "failed")
        attempts = self.rows("attempts")
        self.assertEqual(
            [row["status"] for row in attempts], ["accepted", "accepted", "failed"]
        )
        self.assertEqual(self.store.snapshot()["tasks"], [])

    def test_engine_rejects_invalid_judge_batches_before_acceptance(self):
        invalid_batches = [
            [{"action": "unknown", "id": "invalid"}],
            [create("duplicate"), create("duplicate")],
            [create("forged") | {"source": "human"}],
        ]
        for index, changes in enumerate(invalid_batches):
            self.trigger(f"invalid-batch-{index}")

            def invoke(call, package):
                if call.role == "judge":
                    return {"output": {"changes": changes}, "evidence_refs": []}
                return self.invoke(call, package)

            result = self.runner(
                invoke,
                validators={
                    "advocate": self.validate_advocate,
                    "judge": lambda output, _: output,
                },
            ).run_once()
            self.assertEqual(result["status"], "failed")
            self.assertEqual(
                self.ledger.calls(result["id"])["judge"]["status"], "failed"
            )
            self.assertEqual(self.store.snapshot()["tasks"], [])
            self.assertEqual(self.ledger.pending(), [])
            self.assertEqual(self.rows("task_batches"), [])
        self.trigger("valid-after-invalid")
        result = self.runner().run_once()
        self.assertEqual(result["status"], "applied")
        self.assertEqual(
            result["receipt"]["applied"], [{"id": "proposed", "revision": 1}]
        )
        self.assertEqual(self.store.snapshot()["counters"]["calls"], 12)

    def test_interrupt_before_judge_acceptance_is_never_redispatched(self):
        self.trigger()
        observed = []

        def invoke(call, package):
            observed.append(call.attempt_id)
            if call.role == "judge":
                raise KeyboardInterrupt("Synthetic interruption")
            return self.invoke(call, package)

        runner = self.runner(invoke)
        with self.assertRaises(KeyboardInterrupt):
            runner.run_once()
        round_id = self.rows("planning_rounds")[0]["id"]
        self.session.__exit__(None, None, None)
        self.session = self.store.coordinator()
        self.owner = self.session.__enter__()
        self.ledger = PlanningLedger(self.owner)
        runner = self.runner(invoke)
        with self.assertRaises(InvalidTransition):
            runner.run_once()
        with self.assertRaises(InvalidTransition):
            runner.settle_accepted_judge(round_id)
        self.assertEqual(len(observed), 3)
        self.assertEqual(self.store.snapshot()["counters"]["calls"], 3)
        self.assertEqual(self.ledger.calls(round_id)["judge"]["status"], "active")

    def test_interrupted_accepted_judge_settles_without_model_replay(self):
        self.trigger()
        runner = self.runner()
        with patch.object(
            runner.pool,
            "apply_judge",
            side_effect=KeyboardInterrupt("after acceptance"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                runner.run_once()
        round_id = self.rows("planning_rounds")[0]["id"]
        self.assertEqual(self.ledger.calls(round_id)["judge"]["status"], "accepted")
        self.assertEqual(self.store.snapshot()["tasks"], [])
        self.session.__exit__(None, None, None)
        self.session = self.store.coordinator()
        self.owner = self.session.__enter__()
        self.ledger = PlanningLedger(self.owner)
        resumed = self.runner(
            lambda *_: self.fail("Settlement dispatched a model callback")
        )
        with self.assertRaises(InvalidTransition):
            resumed.run_once()
        result = resumed.settle_accepted_judge(round_id)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(
            result["receipt"]["applied"], [{"id": "proposed", "revision": 1}]
        )
        self.assertEqual(self.store.snapshot()["counters"]["calls"], 3)
        self.assertEqual(len(self.rows("task_batches")), 1)

    def test_explicit_role_validators_are_required(self):
        for validators in (
            {},
            {"advocate": self.validate_advocate},
            {"advocate": None, "judge": self.validate_judge},
        ):
            with self.subTest(validators=validators), self.assertRaises(ValueError):
                PlanningRunner(self.owner, self.invoke, validators)


if __name__ == "__main__":
    unittest.main()
