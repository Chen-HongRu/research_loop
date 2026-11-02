import importlib.util
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location(
    "server", Path(__file__).resolve().parents[3] / "integrations/protok/server.py"
)
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


class ServerTests(unittest.TestCase):
    def test_recovery_matches_fresh_record_identity_not_only_source_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fresh = root / "control/debug/0009/formal-001"
            name = "fingerprint--debug-0009-formal-001"
            SERVER.write(
                root / "control/jobs/0001/job.json", {"fingerprint": "fingerprint"}
            )
            SERVER.write(
                root / "control/jobs/0009/recovery.json", {"job_directory": str(fresh)}
            )
            SERVER.write(
                fresh / "job.json",
                {
                    "fingerprint": "fingerprint",
                    "record_name": name,
                    "fresh_attempt": "debug-0009-formal-001",
                },
            )
            backend = Mock()
            backend.inspect.side_effect = [
                {"outstanding": [{"fingerprint": "fingerprint", "record": name}]},
                {"outstanding": []},
            ]
            runner = SimpleNamespace(
                reconcile_debug_allocations=Mock(), ProTokError=RuntimeError
            )
            SERVER.recover_owned(root, runner, backend)
            backend.evaluate.assert_called_once_with(
                fresh / "source",
                fresh / "runtime-config.json",
                timeout_seconds=240,
                recover=True,
                fresh_attempt="debug-0009-formal-001",
            )

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
        waits = []

        def stop_after_wait(seconds):
            waits.append(seconds)
            (self.root / "control/STOP").touch()

        self.assertEqual(
            SERVER.drive(
                self.root,
                self.runner,
                self.backend,
                clock=lambda: 0,
                wait=stop_after_wait,
            ),
            "user_stop",
        )
        self.assertEqual(waits, [5])
        status = SERVER.read(self.root / "control/supervisor-status.json")
        self.assertEqual(status["status"], "blocked")
        self.assertIn("reconciliation", status["error"])
        self.runner.run_one.assert_not_called()

    def test_transient_inspection_failure_resumes_after_backoff(self):
        self.backend.inspect.side_effect = [
            OSError("receipt temporarily unavailable"),
            {"outstanding": [], "used_gpu_seconds": 100},
        ]
        waits = []

        def finish(*args):
            (self.root / "control/STOP").touch()
            return {"status": "ready"}

        self.runner.run_one.side_effect = finish
        result = SERVER.drive(
            self.root, self.runner, self.backend, clock=lambda: 0, wait=waits.append
        )
        self.assertEqual(result, "user_stop")
        self.assertEqual(waits, [5])
        self.runner.run_one.assert_called_once()
        status = SERVER.read(self.root / "control/supervisor-status.json")
        self.assertEqual(status["status"], "running")
        self.assertIn("receipt temporarily unavailable", status["first_error"])

    def test_rejected_readonly_response_uses_locked_core_retry(self):
        for phase in ("selection", "plan", "review"):
            with self.subTest(phase=phase):
                state = {
                    "phase": phase,
                    "status": "needs_attention",
                    "error": "ValueError: invalid completed response",
                    "call_count": 7,
                    "elapsed_seconds": 21,
                    "active_attempt": "retained-output",
                }
                ready = {**state, "status": "ready", "active_attempt": None}
                self.write("study/state.json", state)

                def retry(**kwargs):
                    self.assertEqual(SERVER.read(self.root / "study/state.json"), state)
                    self.write("study/state.json", ready)

                def finish(*args):
                    self.assertEqual(SERVER.read(self.root / "study/state.json"), ready)
                    (self.root / "control/STOP").touch()
                    return {"status": "ready"}

                self.runner.run_one.reset_mock()
                self.runner.run_one.side_effect = finish
                with patch("research_loop.core.Study") as study:
                    study.return_value.retry.side_effect = retry
                    result = SERVER.drive(
                        self.root, self.runner, self.backend, clock=lambda: 0
                    )
                    study.return_value.retry.assert_called_once_with(readonly=True)
                    study.return_value._can_resume.assert_not_called()
                self.assertEqual(result, "user_stop")
                self.runner.run_one.assert_called_once()
                (self.root / "control/STOP").unlink()

    def test_readonly_retry_refusal_stays_blocked_without_agent_calls(self):
        state = {"phase": "plan", "status": "needs_attention"}
        self.write("study/state.json", state)

        def stop(seconds):
            self.assertEqual(seconds, 5)
            (self.root / "control/STOP").touch()

        with patch("research_loop.core.Study") as study:
            study.return_value.retry.side_effect = RuntimeError(
                "retained agent is live"
            )
            result = SERVER.drive(
                self.root, self.runner, self.backend, clock=lambda: 0, wait=stop
            )
            study.return_value.retry.assert_called_once_with(readonly=True)
        self.assertEqual(result, "user_stop")
        self.assertEqual(SERVER.read(self.root / "study/state.json"), state)
        self.runner.run_one.assert_not_called()
        self.assertIn(
            "retained agent is live",
            SERVER.read(self.root / "control/supervisor-status.json")["error"],
        )

    def test_execute_and_debug_never_request_automatic_retry(self):
        with patch("research_loop.core.Study") as study:
            for state in (
                {"phase": "execute", "status": "needs_attention"},
                {"phase": "plan", "status": "needs_attention", "debugging": "owned"},
            ):
                self.assertFalse(SERVER.readonly_resume(self.root, state))
            study.assert_not_called()

    def test_readonly_publication_retry_rechecks_fresh_allocation_budget(self):
        self.write("study/state.json", {"phase": "review", "status": "needs_attention"})

        def publish(**kwargs):
            self.write("study/state.json", {"phase": "done", "status": "ready"})

        with patch("research_loop.core.Study") as study:
            study.return_value.retry.side_effect = publish
            result = SERVER.drive(
                self.root, self.runner, self.backend, clock=lambda: 9000
            )
            study.return_value.retry.assert_called_once_with(readonly=True)
        self.assertEqual(result, "budget")
        self.runner.run_one.assert_not_called()

    def test_failed_execute_remains_blocked_across_supervisor_restart(self):
        state = {"phase": "execute", "status": "ready"}
        self.write("study/state.json", state)
        self.runner.run_one.side_effect = RuntimeError("unknown external effects")

        def stop(*args):
            (self.root / "control/STOP").touch()

        for restart in range(2):
            with self.subTest(restart=restart):
                SERVER.drive(
                    self.root, self.runner, self.backend, clock=lambda: 0, wait=stop
                )
                self.assertEqual(self.runner.run_one.call_count, 1)
                self.assertEqual(SERVER.read(self.root / "study/state.json"), state)
                (self.root / "control/STOP").unlink()
        status = SERVER.read(self.root / "control/supervisor-status.json")
        self.assertIn("unknown external effects", status["first_error"])

    def test_blocked_inspection_obeys_original_deadline_with_capped_backoff(self):
        self.backend.inspect.side_effect = RuntimeError("accounting conflict")
        now = [0]
        waits = []

        def wait(seconds):
            waits.append(seconds)
            now[0] += seconds
            lease = SERVER.read(self.root / "control/continuation.json")
            lease["deadline"] = 999999
            self.write("control/continuation.json", lease)

        self.assertEqual(
            SERVER.drive(
                self.root, self.runner, self.backend, clock=lambda: now[0], wait=wait
            ),
            "deadline",
        )
        self.assertEqual(now[0], 10000)
        self.assertEqual(waits[:5], [5, 10, 20, 40, 60])
        self.assertLessEqual(max(waits), 60)
        self.runner.run_one.assert_not_called()

    def test_ownership_expiry_stops_blocked_process(self):
        self.write("control/ownership.json", {"expires_at": 3})
        self.backend.inspect.side_effect = OSError("temporary read error")
        now = [0]

        def wait(seconds):
            now[0] += seconds

        self.assertEqual(
            SERVER.drive(
                self.root, self.runner, self.backend, clock=lambda: now[0], wait=wait
            ),
            "deadline",
        )
        self.assertEqual(now[0], 3)
        self.runner.run_one.assert_not_called()

    def test_cleanup_inspect_failure_writes_unknown_and_preserves_first_failure(self):
        self.write(
            "control/supervisor-status.json",
            {"status": "blocked", "first_error": "original execution failure"},
        )
        self.backend.inspect.side_effect = RuntimeError("accounting unavailable")
        SERVER.final_summary(
            self.root,
            self.runner,
            self.backend,
            "user_stop",
            first_error="KeyboardInterrupt",
        )
        summary = SERVER.read(self.root / "control/final-summary.json")
        self.assertEqual(summary["first_error"], "original execution failure")
        self.assertEqual(summary["accounting_status"], "unknown")
        for key in (
            "used_gpu_seconds",
            "additional_gpu_seconds_used",
            "outstanding",
            "research_outstanding",
        ):
            self.assertIsNone(summary[key])
        self.assertIn("accounting unavailable", summary["cleanup_errors"][0])

    def test_main_cleanup_inspect_and_scout_failure_cannot_skip_summary(self):
        self.write(
            "control/ownership.json", {"project": "/project", "owner_token": "owned"}
        )
        self.write("study/study.json", {"exploration": True})
        self.runner.ProTokBackend = Mock(return_value=self.backend)
        self.backend.inspect.side_effect = RuntimeError("accounting unavailable")
        spec = SimpleNamespace(loader=SimpleNamespace(exec_module=Mock()))
        scout = Mock()
        scout.shutdown.side_effect = RuntimeError("scout shutdown failed")
        with (
            patch.object(
                SERVER.sys,
                "argv",
                ["server", "--root", str(self.root), "--runner", "/runner.py"],
            ),
            patch.object(SERVER.signal, "signal"),
            patch.object(
                SERVER.importlib.util, "spec_from_file_location", return_value=spec
            ),
            patch.object(
                SERVER.importlib.util, "module_from_spec", return_value=self.runner
            ),
            patch.object(SERVER, "ScoutScheduler", return_value=scout),
            patch.object(SERVER, "drive", side_effect=RuntimeError("first failure")),
            patch.object(
                SERVER, "recover_owned", side_effect=RuntimeError("cannot reconcile")
            ) as recovery,
        ):
            SERVER.main()
        recovery.assert_called_once_with(self.root.resolve(), self.runner, self.backend)
        summary = SERVER.read(self.root / "control/final-summary.json")
        self.assertEqual(summary["first_error"], "RuntimeError: first failure")
        self.assertEqual(summary["accounting_status"], "unknown")
        self.assertIn("accounting unavailable", summary["cleanup_errors"][0])
        self.assertTrue((self.root / "control/scout-shutdown-error.json").exists())
        self.assertTrue((self.root / "control/recovery-error.json").exists())

    def test_blocked_outstanding_deadline_recovers_and_summarizes_failure(self):
        self.write(
            "control/ownership.json",
            {"project": "/project", "owner_token": "owned", "expires_at": 3},
        )
        outstanding = [{"fingerprint": "owned"}]
        self.backend.inspect.return_value["outstanding"] = outstanding
        self.runner.ProTokBackend = Mock(return_value=self.backend)
        spec = SimpleNamespace(loader=SimpleNamespace(exec_module=Mock()))
        now = [0]
        drive = SERVER.drive

        def wait(seconds):
            now[0] += seconds

        def recovery(*args):
            self.assertTrue((self.root / "control/STOP").exists())
            raise RuntimeError("cannot settle owned allocation")

        with (
            patch.object(
                SERVER.sys,
                "argv",
                ["server", "--root", str(self.root), "--runner", "/runner.py"],
            ),
            patch.object(SERVER.signal, "signal"),
            patch.object(
                SERVER.importlib.util, "spec_from_file_location", return_value=spec
            ),
            patch.object(
                SERVER.importlib.util, "module_from_spec", return_value=self.runner
            ),
            patch.object(
                SERVER,
                "drive",
                side_effect=lambda *args: drive(*args, clock=lambda: now[0], wait=wait),
            ),
            patch.object(SERVER, "recover_owned", side_effect=recovery) as recover,
        ):
            SERVER.main()
        recover.assert_called_once_with(self.root.resolve(), self.runner, self.backend)
        self.runner.run_one.assert_not_called()
        summary = SERVER.read(self.root / "control/final-summary.json")
        self.assertEqual(summary["reason"], "deadline")
        self.assertIn("Outstanding job", summary["first_error"])
        self.assertEqual(summary["used_gpu_seconds"], 100)
        self.assertEqual(summary["outstanding"], outstanding)
        self.assertIn(
            "cannot settle owned allocation",
            SERVER.read(self.root / "control/recovery-error.json")["error"],
        )

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

    def test_scout_does_not_launch_search_while_supervisor_is_blocked(self):
        self.write("study/state.json", {"iteration": 7})
        self.write("control/supervisor-status.json", {"status": "blocked"})
        job = self.root / "control/jobs/0007/job.json"
        job.parent.mkdir(parents=True)
        job.write_text(json.dumps({"status": "submitted"}))
        manager = SimpleNamespace(launch_if_due=Mock(), close=Mock())
        scheduler = SERVER.ScoutScheduler(self.root, manager=manager)
        with patch.object(
            scheduler.stopped, "wait", side_effect=lambda _: scheduler.stopped.set()
        ):
            scheduler._run()
        manager.launch_if_due.assert_not_called()

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
