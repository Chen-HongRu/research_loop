from concurrent.futures import ProcessPoolExecutor
import json
import multiprocessing
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from integrations.protok.shared_budget import (
    LEDGER,
    POLICY,
    SharedBudget,
    initialize_shared_budget,
    install_shared_budget,
)


def policy(now=1000):
    return {
        "schema": 1,
        "budget_id": "new-24h",
        "authorization": "Explicit new allowance",
        "started_at": now,
        "deadline": now + 86400,
        "limit_gpu_seconds": 86400,
        "overrun_reserve_seconds": 300,
        "historical_gpu_seconds": dict(
            formal=43987, research=1381, debug=169, long=0, manual=0
        ),
        "baseline_settled": True,
    }


def reserve_process(arguments):
    control, index, seconds = arguments
    try:
        with patch("integrations.protok.shared_budget.time.time", return_value=1001):
            SharedBudget(control).reserve(
                str(index), "long" if index else "formal", seconds
            )
        return True
    except ValueError:
        return False


class SharedBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.control = Path(temporary.name)
        self.policy = policy()
        initialize_shared_budget(self.control, self.policy)
        self.budget = SharedBudget(self.control)

    @staticmethod
    def terminal(seconds=100):
        return dict(
            owned_job_id="123",
            terminal_state="COMPLETED",
            exit_code="0:0",
            gpu_seconds=seconds,
        )

    def reserve(self, key="a", channel="formal", seconds=1800):
        with patch("integrations.protok.shared_budget.time.time", return_value=1001):
            self.budget.reserve(key, channel, seconds)

    def test_all_channels_sum_full_reservations_and_actual_cost(self):
        self.assertEqual(self.budget.status()["used_gpu_seconds"], 0)
        for channel in ("formal", "debug", "research", "long", "manual"):
            self.reserve(channel, channel)
            self.budget.reconcile(channel, {"owned_job_id": "123"})
        self.assertEqual(self.budget.status()["reserved_gpu_seconds"], 5 * 2100)
        self.budget.reconcile("formal", self.terminal())
        self.budget.reconcile("formal", self.terminal())
        self.assertEqual(self.budget.status()["used_gpu_seconds"], 100)
        self.assertEqual(
            self.budget.status()["remaining_gpu_seconds"], 86400 - 100 - 4 * 2100
        )

    def test_processes_cannot_over_reserve_total(self):
        with ProcessPoolExecutor(
            max_workers=2, mp_context=multiprocessing.get_context("fork")
        ) as pool:
            self.assertEqual(
                sum(
                    pool.map(
                        reserve_process, [(self.control, i, 50000) for i in range(2)]
                    )
                ),
                1,
            )
        self.assertEqual(self.budget.status()["reserved_gpu_seconds"], 50300)

    def test_cutoff_margin_and_total_cap(self):
        with patch("integrations.protok.shared_budget.time.time", return_value=87300):
            with self.assertRaises(ValueError):
                self.budget.reserve("a", "long", 60)
        self.reserve(seconds=86000)
        self.budget.reconcile("a", self.terminal(86000))
        with self.assertRaises(ValueError):
            self.reserve("b", seconds=101)

    def test_unknown_survives_restart_retains_full_bound_and_cannot_resubmit(self):
        self.reserve()
        self.budget.reconcile("a", {"status": "submitting"})
        self.budget = SharedBudget(self.control)
        with self.assertRaises(ValueError):
            self.reserve()
        with self.assertRaises(ValueError):
            self.reserve("b", "manual", 84301)
        with self.assertRaises(ValueError):
            self.budget.reconcile("a", self.terminal(-1))
        self.assertEqual(self.budget.status()["reserved_gpu_seconds"], 2100)

    def test_overrun_breaches_and_terminal_accounting_is_immutable(self):
        self.reserve()
        self.budget.reconcile("a", self.terminal(2101))
        self.assertTrue(self.budget.status()["breached"])
        with self.assertRaises(ValueError):
            self.reserve("b", "long")
        with self.assertRaises(ValueError):
            self.budget.reconcile("a", self.terminal(1800))

    def test_missing_or_mutated_state_fails_closed_and_cannot_reset(self):
        with self.assertRaises(ValueError):
            initialize_shared_budget(self.control, self.policy)
        (self.control / POLICY).write_text(
            json.dumps({**self.policy, "limit_gpu_seconds": 86000})
        )
        with self.assertRaises(ValueError):
            self.reserve()
        (self.control / POLICY).unlink()
        with self.assertRaises(FileNotFoundError):
            install_shared_budget(SimpleNamespace(), self.control, channel="formal")

    def test_wrapper_reserves_before_submit_and_settles_failed_wait(self):
        def submit(config, record, state, attempt):
            self.assertEqual(self.budget.status()["reserved_gpu_seconds"], 2100)
            attempt.update(status="running", owned_job_id="123")

        def wait(config, session, record, state, attempt):
            self.assertEqual(session["deadline"], 87400)
            attempt.update(self.terminal(10), terminal_state="FAILED", exit_code="1:0")
            raise RuntimeError("Job failed")

        backend = SimpleNamespace(submit=submit, wait_for_job=wait)
        install_shared_budget(backend, self.control, channel="research")
        attempt = {"directory": "attempt-1", "budget_seconds": 1800}
        with patch("integrations.protok.shared_budget.time.time", return_value=1001):
            backend.submit({}, self.control / "research/1", {}, attempt)
        with self.assertRaisesRegex(RuntimeError, "Job failed"):
            backend.wait_for_job(
                {}, {"deadline": float("inf")}, self.control / "research/1", {}, attempt
            )
        self.assertEqual(self.budget.status()["used_gpu_seconds"], 10)
        self.assertEqual(self.budget.status()["reserved_gpu_seconds"], 0)

    def test_lost_submit_retains_bound_and_refusal_never_calls_native_submit(self):
        backend = SimpleNamespace(
            submit=Mock(side_effect=RuntimeError("transport lost")), wait_for_job=Mock()
        )
        original = backend.submit
        install_shared_budget(backend, self.control, channel="debug")
        with patch("integrations.protok.shared_budget.time.time", return_value=1001):
            with self.assertRaisesRegex(RuntimeError, "transport lost"):
                backend.submit(
                    {},
                    self.control / "debug/1",
                    {},
                    {"directory": "a", "budget_seconds": 1800},
                )
            with self.assertRaises(ValueError):
                backend.submit(
                    {},
                    self.control / "debug/2",
                    {},
                    {"directory": "b", "budget_seconds": 84301},
                )
        self.assertEqual(original.call_count, 1)
        self.assertEqual(self.budget.status()["reserved_gpu_seconds"], 2100)
        proof = json.loads(
            (self.control / "debug/2/shared-budget-refused.json").read_text()
        )
        self.assertFalse(proof["native_submit_called"])
        self.assertFalse(proof["reserved"])

    def test_no_policy_preserves_old_backend(self):
        (self.control / POLICY).unlink()
        (self.control / LEDGER).unlink()
        backend = SimpleNamespace(submit=Mock(), wait_for_job=Mock())
        original = backend.submit
        install_shared_budget(backend, self.control, channel="long")
        self.assertIs(backend.submit, original)


class ActiveLaneBudgetTests(unittest.TestCase):
    def fixture(self, cls, method):
        instance = cls(method)
        instance.setUp()
        self.addCleanup(instance.doCleanups)
        return instance

    def test_debug_actual_cost_and_unknown_recovery(self):
        from integrations.protok.tests.test_debug_backend import DebugBackendTests

        f = self.fixture(
            DebugBackendTests,
            "test_success_is_unscored_and_settled_receipt_is_idempotent",
        )
        initialize_shared_budget(f.control, policy(time.time() - 1))
        f.test_success_is_unscored_and_settled_receipt_is_idempotent()
        self.assertEqual(SharedBudget(f.control).status()["used_gpu_seconds"], 600)

    def test_formal_continuation_hooks_active_policy_receipt(self):
        from integrations.protok.continuation import install_hooks
        from integrations.protok.tests.test_protok_continuation import ContinuationTest

        f = self.fixture(
            ContinuationTest,
            "test_uncapped_new_records_keep_original_budget_and_history",
        )
        initialize_shared_budget(f.control, policy())
        with patch("integrations.protok.continuation.time.time", return_value=1001):
            backend, config, lease = f.validate()

            def submit(config, record, state, attempt):
                self.assertEqual(
                    SharedBudget(f.control).status()["reserved_gpu_seconds"], 2100
                )
                attempt.update(status="running", owned_job_id="123")
                backend.save_state(record, state)

            def wait(config, session, record, state, attempt):
                attempt.update(SharedBudgetTests.terminal(15), status="finished")
                backend.save_state(record, state)

            backend.submit = submit
            backend.wait_for_job = wait
            install_hooks(backend, config, lease, lambda: None, control_dir=f.control)
            record, state = backend.reserve_record(
                config, "new-shared-formal", "baseline"
            )
            attempt = {
                "directory": "seed-42-attempt-1",
                "budget_seconds": 1800,
                "status": "prepared",
            }
            state["attempts"].append(attempt)
            backend.submit(config, record, state, attempt)
            backend.wait_for_job(config, {"deadline": 22600}, record, state, attempt)
        self.assertEqual(SharedBudget(f.control).status()["used_gpu_seconds"], 15)

    def test_debug_unknown_receipt_recovers_without_new_submission(self):
        from integrations.protok.tests.test_debug_backend import DebugBackendTests

        f = self.fixture(
            DebugBackendTests,
            "test_unknown_submission_blocks_new_attempt_and_recovers_without_resubmission",
        )
        initialize_shared_budget(f.control, policy(time.time() - 1))
        f.test_unknown_submission_blocks_new_attempt_and_recovers_without_resubmission()
        self.assertEqual(SharedBudget(f.control).status()["used_gpu_seconds"], 600)

    def test_research_active_policy_receipt(self):
        from integrations.protok.tests.test_research_backend import ResearchBackendTests

        f = self.fixture(
            ResearchBackendTests,
            "test_script_receipt_is_bounded_nonformal_and_repeatable",
        )
        initialize_shared_budget(f.control, policy(time.time() - 1))
        f.test_script_receipt_is_bounded_nonformal_and_repeatable()
        self.assertEqual(SharedBudget(f.control).status()["used_gpu_seconds"], 17)

    def test_automatic_long_active_policy_receipt(self):
        from integrations.protok import long_training as lane
        from integrations.protok.tests.test_long_training import (
            LongTrainingTests,
            Transport,
            setup,
            completion,
            fake_snapshot,
        )

        f = self.fixture(LongTrainingTests, "test_recipe_and_complete_curve")
        root, control, req, config, backend = setup(f.tmp_path)
        initialize_shared_budget(control, policy(99))
        transport = Transport()
        remote = config["remote_root"] + "/long-training/" + req["id"]
        with (
            patch.object(lane, "_load_backend", return_value=(transport, config)),
            patch.object(lane, "_snapshot", side_effect=fake_snapshot),
            patch.object(
                lane,
                "_collect",
                return_value={
                    "completion": completion(req, "b" * 64, remote),
                    "progress": None,
                },
            ),
            patch.object(lane.time, "time", return_value=100.0),
        ):
            result = lane.execute(
                backend, root, req, root / "workspace", deadline=100000.0
            )
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(SharedBudget(control).status()["used_gpu_seconds"], 3000)

    def test_manual_long_active_policy_receipt(self):
        from integrations.protok.tests.test_manual_long_backend import (
            ManualLongBackendTests,
        )

        f = self.fixture(
            ManualLongBackendTests,
            "test_new_manual_candidate_runs_without_short_receipt_and_automatic_budget",
        )
        initialize_shared_budget(f.control, policy(99))
        f.test_new_manual_candidate_runs_without_short_receipt_and_automatic_budget()
        self.assertEqual(SharedBudget(f.control).status()["used_gpu_seconds"], 3000)
        state = json.loads((f.control / LEDGER).read_text())
        self.assertEqual(
            {entry["channel"] for entry in state["attempts"].values()}, {"manual"}
        )


if __name__ == "__main__":
    unittest.main()
