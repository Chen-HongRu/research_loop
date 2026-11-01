import json
import tempfile
import threading
import unittest
from pathlib import Path

from research_loop.debug_budget import DebugBudget


class DebugBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.budget = self.open()

    def open(self, **options):
        return DebugBudget(
            self.directory,
            scope_id=options.pop("scope_id", "failed-investigation-1"),
            limit_gpu_seconds=options.pop("limit_gpu_seconds", 1800),
            **options,
        )

    def submit(self, attempt, seconds=None):
        allocated = self.budget.reserve(attempt, seconds)
        self.budget.mark_submitting(attempt)
        self.budget.mark_submitted(attempt, f"job-{attempt}")
        return allocated

    def settle(self, attempt, seconds, state="FAILED", code="1:0"):
        return self.budget.settle(
            attempt, gpu_seconds=seconds, terminal_state=state, exit_code=code
        )

    def test_restart_preserves_failed_job_cost_and_same_total_cap(self):
        self.assertEqual(self.submit("first", 1000), 1000)
        status = self.settle("first", 720)
        self.assertEqual(status["used_gpu_seconds"], 720)
        self.budget = self.open()
        self.assertEqual(self.submit("retry"), 1080)
        self.settle("retry", 1080, "TIMEOUT", "0:15")
        restarted = self.open()
        self.assertEqual(restarted.remaining(), 0)
        self.assertEqual(restarted.status()["used_gpu_seconds"], 1800)
        with self.assertRaisesRegex(ValueError, "exhausted"):
            restarted.reserve("another-restart")

    def test_reservation_reports_used_reserved_and_remaining_separately(self):
        self.assertEqual(self.budget.reserve("first", 600), 600)
        status = self.budget.status()
        self.assertEqual(status["used_gpu_seconds"], 0)
        self.assertEqual(status["reserved_gpu_seconds"], 600)
        self.assertEqual(status["remaining_gpu_seconds"], 1200)
        self.assertEqual(status["active_attempt"], "first")
        with self.assertRaisesRegex(ValueError, "Outstanding"):
            self.budget.reserve("other", 1)
        self.budget.mark_submitting("first")
        self.budget.mark_submitted("first", "job-first")
        self.settle("first", 599)
        self.assertEqual(self.submit("second", 2000), 1201)

    def test_one_second_boundary_is_admissible_and_no_more(self):
        self.submit("first")
        self.settle("first", 1799)
        self.assertEqual(self.submit("last", 60), 1)
        self.assertEqual(self.budget.remaining(), 0)
        self.settle("last", 1, "COMPLETED", "0:0")
        self.assertTrue(self.budget.status()["exhausted"])
        with self.assertRaises(ValueError):
            self.budget.reserve("over")

    def test_unknown_submission_survives_restart_and_blocks_new_gpu_work(self):
        self.budget.reserve("uncertain", 600)
        self.budget.mark_submitting("uncertain")
        self.budget = self.open()
        self.assertEqual(self.budget.status()["reserved_gpu_seconds"], 600)
        with self.assertRaisesRegex(ValueError, "Outstanding"):
            self.budget.reserve("retry", 10)
        with self.assertRaisesRegex(ValueError, "forbidden"):
            self.budget.release("uncertain")
        with self.assertRaisesRegex(ValueError, "reconciled"):
            self.settle("uncertain", 0)
        self.budget.mark_submitted("uncertain", "recovered-exact-job-id")
        self.settle("uncertain", 600)
        self.assertEqual(self.budget.reserve("retry", 10), 10)

    def test_submitted_without_terminal_accounting_keeps_allocation_reserved(self):
        self.submit("waiting", 500)
        self.budget = self.open()
        with self.assertRaises(ValueError):
            self.budget.reserve("new")
        with self.assertRaises(ValueError):
            self.budget.release("waiting")
        self.assertEqual(self.budget.status()["used_gpu_seconds"], 0)
        self.assertEqual(self.budget.status()["reserved_gpu_seconds"], 500)

    def test_same_id_operations_and_receipts_are_idempotent(self):
        self.assertEqual(self.budget.reserve("same", 500), 500)
        self.assertEqual(self.open().reserve("same", 500), 500)
        with self.assertRaisesRegex(ValueError, "Conflicting reservation"):
            self.budget.reserve("same", 400)
        self.budget.mark_submitting("same")
        self.budget.mark_submitting("same")
        self.budget.mark_submitted("same", "job-same")
        self.budget.mark_submitted("same", "job-same")
        self.budget.mark_submitting("same")
        before = self.settle("same", 400)
        after = self.open().settle(
            "same", gpu_seconds=400, terminal_state="failed", exit_code="1:0"
        )
        self.assertEqual(before, after)
        self.assertEqual(after["used_gpu_seconds"], 400)
        with self.assertRaisesRegex(ValueError, "cannot be reserved again"):
            self.budget.reserve("same", 500)

    def test_conflicting_settlement_cannot_reduce_or_replace_charges(self):
        self.submit("charged", 700)
        before = self.settle("charged", 600)
        for changes in (
            {"gpu_seconds": 500},
            {"gpu_seconds": 650},
            {"terminal_state": "TIMEOUT"},
            {"exit_code": "2:0"},
        ):
            receipt = dict(gpu_seconds=600, terminal_state="FAILED", exit_code="1:0")
            receipt.update(changes)
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(ValueError, "Conflicting terminal"),
            ):
                self.budget.settle("charged", **receipt)
        self.assertEqual(self.budget.status(), before)

    def test_only_known_unsubmitted_reservation_can_be_released(self):
        self.budget.reserve("not-launched", 700)
        released = self.budget.release("not-launched")
        self.assertEqual(released["used_gpu_seconds"], 0)
        self.assertEqual(released["reserved_gpu_seconds"], 0)
        self.assertEqual(released["remaining_gpu_seconds"], 1800)
        self.assertEqual(self.budget.release("not-launched"), released)
        with self.assertRaises(ValueError):
            self.budget.reserve("not-launched", 700)
        self.assertEqual(self.budget.reserve("new", 700), 700)

    def test_overspend_is_recorded_and_fails_closed_across_restart(self):
        for allocated, spent in ((100, 101), (1800, 1801)):
            with self.subTest(allocated=allocated):
                directory = self.directory / str(allocated)
                budget = DebugBudget(
                    directory, scope_id="scope", limit_gpu_seconds=1800
                )
                budget.reserve("overrun", allocated)
                budget.mark_submitting("overrun")
                budget.mark_submitted("overrun", "job")
                status = budget.settle(
                    "overrun",
                    gpu_seconds=spent,
                    terminal_state="FAILED",
                    exit_code="1:0",
                )
                self.assertEqual(status["used_gpu_seconds"], spent)
                self.assertEqual(status["reserved_gpu_seconds"], 0)
                self.assertEqual(status["remaining_gpu_seconds"], 0)
                self.assertTrue(status["exhausted"])
                reopened = DebugBudget(
                    directory, scope_id="scope", limit_gpu_seconds=1800
                )
                with self.assertRaisesRegex(ValueError, "exhausted"):
                    reopened.reserve("cannot-reset")
                self.assertEqual(reopened.status(), status)

    def test_existing_scope_or_cap_cannot_be_overwritten(self):
        self.submit("failed", 100)
        self.settle("failed", 100)
        before = self.budget.path.read_bytes()
        for options in ({"scope_id": "other"}, {"limit_gpu_seconds": 3600}):
            with (
                self.subTest(options=options),
                self.assertRaisesRegex(ValueError, "configuration differs"),
            ):
                self.open(**options)
            self.assertEqual(self.budget.path.read_bytes(), before)

    def test_invalid_terminal_receipts_and_job_conflicts_leave_budget_unchanged(self):
        self.budget.reserve("attempt", 600)
        with self.assertRaisesRegex(ValueError, "mark_submitting"):
            self.budget.mark_submitted("attempt", "job")
        self.budget.mark_submitting("attempt")
        self.budget.mark_submitted("attempt", "job")
        before = self.budget.status()
        with self.assertRaisesRegex(ValueError, "Conflicting job"):
            self.budget.mark_submitted("attempt", "different-job")
        for changes in (
            {"gpu_seconds": -1},
            {"gpu_seconds": True},
            {"gpu_seconds": 2.0},
            {"terminal_state": "RUNNING"},
            {"terminal_state": "UNKNOWN"},
            {"exit_code": "unknown"},
            {"exit_code": ""},
            {"terminal_state": "COMPLETED", "exit_code": "1:0"},
        ):
            receipt = dict(gpu_seconds=30, terminal_state="FAILED", exit_code="1:0")
            receipt.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.budget.settle("attempt", **receipt)
        self.assertEqual(self.budget.status(), before)

    def test_independent_instances_serialize_reservation(self):
        barrier = threading.Barrier(2)
        outcomes = []

        def reserve(attempt):
            budget = self.open()
            barrier.wait(timeout=2)
            try:
                outcomes.append((attempt, budget.reserve(attempt, 900)))
            except ValueError:
                outcomes.append((attempt, None))

        threads = [
            threading.Thread(target=reserve, args=(attempt,)) for attempt in ("a", "b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(
            sorted(value for _, value in outcomes if value is not None), [900]
        )
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(self.budget.status()["reserved_gpu_seconds"], 900)

    def test_corrupt_state_is_not_silently_reinitialized(self):
        self.budget.path.write_text("not JSON")
        with self.assertRaises(json.JSONDecodeError):
            self.open()
        self.assertEqual(self.budget.path.read_text(), "not JSON")


if __name__ == "__main__":
    unittest.main()
