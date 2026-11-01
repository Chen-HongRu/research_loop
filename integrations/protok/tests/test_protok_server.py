import importlib.util
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

SPEC = importlib.util.spec_from_file_location(
    "server", Path(__file__).resolve().parents[3] / "integrations/protok/server.py"
)
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


class ServerTests(unittest.TestCase):
    def test_budget_reserves_a_whole_job_before_any_agent_call(self):
        for used, seconds, expected_calls in (
            (100084, 3600, 1),
            (100085, 3600, 0),
            (19084, 1799, 0),
        ):
            with (
                self.subTest(used=used, seconds=seconds),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                (root / "control").mkdir()
                (root / "study").mkdir()
                SERVER.write(
                    root / "control/continuation.json",
                    {
                        "schema": 2,
                        "seconds": 86400,
                        "deadline": 100 + seconds,
                        "initial_gpu_seconds": 19084,
                        "additional_gpu_seconds": 86400,
                    },
                )
                SERVER.write(root / "study/study.json", {})
                SERVER.write(root / "study/state.json", {"phase": "done"})
                backend = Mock()
                backend.inspect.return_value = {
                    "outstanding": [],
                    "used_gpu_seconds": used,
                }
                backend._config.return_value = {"reserve_gpu_seconds": 3600}
                runner = Mock()
                runner.read.return_value = {"status": "ready"}

                def run(*args):
                    (root / "control/STOP").write_text("fixture stop")
                    return {"status": "ready"}

                runner.run_one.side_effect = run
                result = SERVER.drive(root, runner, backend, clock=lambda: 100)
                self.assertEqual(runner.run_one.call_count, expected_calls)
                self.assertEqual(result, "user_stop" if expected_calls else "budget")
                runner.init_study.assert_not_called()

    def test_stop_prevents_agent_or_backend_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "control").mkdir()
            SERVER.write(
                root / "control/continuation.json", {"schema": 2, "seconds": 86400}
            )
            (root / "control/STOP").write_text("paused")
            backend, runner = Mock(), Mock()
            self.assertEqual(SERVER.drive(root, runner, backend), "user_stop")
            backend.inspect.assert_not_called()
            runner.run_one.assert_not_called()


class ProTokServerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "control").mkdir()
        (self.root / "study").mkdir()
        self.write(
            "control/continuation.json",
            {
                "schema": 2,
                "seconds": 86400,
                "deadline": 10000,
                "initial_gpu_seconds": 100,
                "additional_gpu_seconds": 86400,
            },
        )
        self.write("study/study.json", {})
        self.write("study/state.json", {"status": "ready", "phase": "done"})
        self.backend = SimpleNamespace(
            inspect=Mock(return_value={"outstanding": [], "used_gpu_seconds": 100}),
            _config=Mock(return_value={"reserve_gpu_seconds": 100}),
        )
        self.runner = SimpleNamespace(
            read=SERVER.read,
            run_one=Mock(),
            repair_completed_control=Mock(return_value=False),
        )

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value))

    def test_handoff_waits_for_iteration_return(self):
        def iteration(root, backend):
            (root / "control/HANDOFF").write_text("switch next boundary")
            return {"status": "ready"}

        self.runner.run_one.side_effect = iteration
        result = SERVER.drive(self.root, self.runner, self.backend, clock=lambda: 0)
        self.assertEqual(result, "handoff")
        self.runner.run_one.assert_called_once()
        self.assertFalse((self.root / "control/STOP").exists())

    def test_existing_stop_prevents_another_iteration(self):
        (self.root / "control/STOP").touch()
        self.assertEqual(
            SERVER.drive(self.root, self.runner, self.backend), "user_stop"
        )
        self.runner.run_one.assert_not_called()

    def test_outstanding_effects_cannot_start_another_iteration(self):
        self.backend.inspect.return_value["outstanding"] = [{"fingerprint": "owned"}]
        with self.assertRaisesRegex(RuntimeError, "reconciliation"):
            SERVER.drive(self.root, self.runner, self.backend, clock=lambda: 0)
        self.runner.run_one.assert_not_called()

    def test_remaining_budget_is_still_enforced(self):
        self.assertEqual(
            SERVER.drive(self.root, self.runner, self.backend, clock=lambda: 9000),
            "budget",
        )
        self.runner.run_one.assert_not_called()

    def test_completed_control_repair_precedes_budget_gate(self):
        self.assertEqual(
            SERVER.drive(self.root, self.runner, self.backend, clock=lambda: 9000),
            "budget",
        )
        self.runner.repair_completed_control.assert_called_once_with(
            self.root, self.backend
        )

    def test_existing_review_can_finish_without_budget_for_new_allocation(self):
        self.write("study/state.json", {"status": "running", "phase": "review"})

        def finish(*args):
            (self.root / "control/STOP").touch()
            return {"status": "ready"}

        self.runner.run_one.side_effect = finish
        self.assertEqual(
            SERVER.drive(self.root, self.runner, self.backend, clock=lambda: 9000),
            "user_stop",
        )
        self.runner.run_one.assert_called_once()

    def test_scout_runs_off_the_driver_thread_and_closes_without_wait(self):
        entered, release = threading.Event(), threading.Event()
        self.write("study/state.json", {"iteration": 7})
        job = self.root / "control/jobs/0007/job.json"
        job.parent.mkdir(parents=True)
        job.write_text(json.dumps({"status": "submitted"}))

        def launch():
            entered.set()
            release.wait(2)

        manager = SimpleNamespace(launch_if_due=launch, close=Mock())
        scheduler = SERVER.ScoutScheduler(self.root, manager=manager, interval=0.01)
        try:
            scheduler.start()
            self.assertTrue(entered.wait(1))
            scheduler.close()
            manager.close.assert_called_once()
            self.assertTrue(scheduler.thread.is_alive())
        finally:
            release.set()
            scheduler.thread.join(2)

    def test_shutdown_settles_scheduler_and_owned_search(self):
        self.write("study/state.json", {"iteration": 7})
        manager = SimpleNamespace(launch_if_due=Mock(), close=Mock(), shutdown=Mock())
        scheduler = SERVER.ScoutScheduler(self.root, manager=manager, interval=0.01)
        scheduler.start()
        scheduler.shutdown()
        self.assertFalse(scheduler.thread.is_alive())
        manager.close.assert_called_once()
        manager.shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
