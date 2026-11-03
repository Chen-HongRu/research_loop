import importlib.util
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from research_loop import StudyError, initialize

SPEC = importlib.util.spec_from_file_location(
    "server", Path(__file__).resolve().parents[3] / "integrations/protok/server.py"
)
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


class ServerTests(unittest.TestCase):
    def test_main_rejects_older_parent_before_startup_or_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize(root / "study", goal="Parent")
            (root / "control").mkdir()
            config_path = root / "study/study.json"
            for version in (1, 2):
                with self.subTest(version=version):
                    config = SERVER.read(config_path)
                    config["version"] = version
                    SERVER.write(config_path, config)
                    with (
                        patch.object(
                            SERVER.sys,
                            "argv",
                            ["server", "--root", str(root), "--runner", "/runner.py"],
                        ),
                        patch.object(
                            SERVER.importlib.util, "spec_from_file_location"
                        ) as load_runner,
                        patch.object(SERVER.signal, "signal") as signals,
                        patch.object(SERVER, "ScoutScheduler") as scout,
                        patch(
                            "integrations.protok.tasks.ExperimentScheduler"
                        ) as scheduler,
                        patch.object(SERVER, "recover_owned") as recovery,
                        self.assertRaisesRegex(StudyError, "Unsupported study format"),
                    ):
                        SERVER.main()
                    load_runner.assert_not_called()
                    signals.assert_not_called()
                    scout.assert_not_called()
                    scheduler.assert_not_called()
                    recovery.assert_not_called()
                    self.assertEqual(list((root / "control").iterdir()), [])
                    self.assertEqual(SERVER.read(config_path), config)

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
            (19084, 1799, 1),
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
        initialize(self.root / "study", goal="Parent")
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

    def test_pipeline_failure_keeps_trace_and_deduplicates_polling_diagnostics(self):
        secret = "operator-owner-value"
        self.backend.owner_token = secret
        self.write(
            "control/ownership.json",
            {"owner_token": secret, "handoff_acknowledged": True, "expires_at": 10000},
        )
        lease = SERVER.read(self.root / "control/continuation.json")
        self.write("control/continuation.json", lease | {"owner_token": secret})
        self.backend.inspect.side_effect = RuntimeError(
            "pipeline failed while using " + secret
        )
        waits = []

        def finish_polling(seconds):
            waits.append(seconds)
            if len(waits) == 3:
                (self.root / "control/STOP").touch()

        SERVER.drive(
            self.root,
            self.runner,
            self.backend,
            clock=lambda: 0,
            wait=finish_polling,
        )
        status = SERVER.read(self.root / "control/supervisor-status.json")
        diagnostic = status["diagnostics"]
        self.assertEqual(status["status"], "blocked")
        self.assertEqual(diagnostic["owner"], "pipeline")
        self.assertEqual(diagnostic["failure_count"], 1)
        self.assertNotIn(secret, json.dumps(diagnostic))
        self.assertTrue(diagnostic["cause_chain"][0]["frames"])
        self.assertEqual(len(waits), 3)
        self.runner.run_one.assert_not_called()

    def test_identical_blocked_polls_escalate_locally_without_shutdown(self):
        self.backend.inspect.side_effect = RuntimeError("receipts unavailable")
        waits = []

        def wait(seconds):
            self.assertFalse((self.root / "control/STOP").exists())
            waits.append(seconds)
            if len(waits) == 7:
                (self.root / "control/HANDOFF").touch()

        result = SERVER.drive(
            self.root,
            self.runner,
            self.backend,
            clock=lambda: 0,
            wait=wait,
        )
        self.assertEqual(result, "handoff")
        self.assertEqual(waits, [5, 10, 20, 40, 60, 60, 60])
        status = SERVER.read(self.root / "control/supervisor-status.json")
        self.assertEqual(status["status"], "blocked")
        self.assertEqual(status["consecutive_failures"], 7)
        self.assertEqual(status["blocked_since"], 0)
        self.assertTrue(status["escalated"])
        self.assertEqual(status["diagnostics"]["failure_count"], 1)
        self.assertNotIn("escalated", SERVER.read(self.root / "study/state.json"))
        self.runner.run_one.assert_not_called()

    def test_successful_poll_resets_the_identical_failure_count(self):
        self.backend.inspect.side_effect = [
            RuntimeError("receipts unavailable"),
            RuntimeError("receipts unavailable"),
            {"outstanding": [], "used_gpu_seconds": 100},
        ]

        def finish(*args):
            (self.root / "control/STOP").touch()
            return {"status": "ready"}

        self.runner.run_one.side_effect = finish
        waits = []
        result = SERVER.drive(
            self.root,
            self.runner,
            self.backend,
            clock=lambda: 0,
            wait=waits.append,
        )
        self.assertEqual(result, "user_stop")
        self.assertEqual(waits, [5, 10])
        status = SERVER.read(self.root / "control/supervisor-status.json")
        self.assertEqual(status["status"], "running")
        self.assertNotIn("consecutive_failures", status)
        self.runner.run_one.assert_called_once()

    def test_restart_continues_an_identical_failure_count(self):
        self.write(
            "control/supervisor-status.json",
            {
                "status": "blocked",
                "stage": "inspection",
                "error": "RuntimeError: receipts unavailable",
                "first_error": "RuntimeError: receipts unavailable",
                "consecutive_failures": 4,
                "blocked_since": 123,
            },
        )
        self.backend.inspect.side_effect = RuntimeError("receipts unavailable")
        waits = []

        def wait(seconds):
            waits.append(seconds)
            (self.root / "control/HANDOFF").touch()

        result = SERVER.drive(
            self.root,
            self.runner,
            self.backend,
            clock=lambda: 0,
            wait=wait,
        )
        self.assertEqual(result, "handoff")
        self.assertEqual(waits, [5])
        status = SERVER.read(self.root / "control/supervisor-status.json")
        self.assertEqual(status["consecutive_failures"], 5)
        self.assertEqual(status["blocked_since"], 123)
        self.runner.run_one.assert_not_called()

    def test_missing_required_state_counts_seven_observations_without_execution(self):
        (self.root / "study/state.json").unlink()
        waits = []

        def wait(seconds):
            waits.append(seconds)
            self.assertFalse((self.root / "control/STOP").exists())
            if len(waits) == 7:
                (self.root / "control/STOP").touch()

        SERVER.drive(self.root, self.runner, self.backend, clock=lambda: 0, wait=wait)
        status = SERVER.read(self.root / "control/supervisor-status.json")
        self.assertEqual(status["stage"], "readiness")
        self.assertEqual(status["consecutive_failures"], 7)
        self.assertEqual(status["attention_status"], "needs_attention")
        self.assertIn("state.json", status["first_error"])
        self.runner.run_one.assert_not_called()
        self.runner.repair_completed_control.assert_not_called()

    def test_readonly_retry_stops_locally_at_threshold_and_survives_restart(self):
        self.write("study/state.json", {"phase": "review", "status": "needs_attention"})
        with patch("research_loop.core.Study") as study:
            study.return_value.retry.side_effect = RuntimeError(
                "same retained read-only failure"
            )
            for count in (7, 9):
                waits = []

                def wait(seconds):
                    waits.append(seconds)
                    if len(waits) == (7 if count == 7 else 2):
                        (self.root / "control/STOP").touch()

                SERVER.drive(
                    self.root, self.runner, self.backend, clock=lambda: 0, wait=wait
                )
                status = SERVER.read(self.root / "control/supervisor-status.json")
                self.assertEqual(status["consecutive_failures"], count)
                self.assertEqual(study.return_value.retry.call_count, 5)
                self.assertEqual(status["diagnostics"]["failure_count"], 1)
                (self.root / "control/STOP").unlink()
        self.runner.run_one.assert_not_called()

    def test_repeated_readonly_run_error_reaches_threshold_not_count_one(self):
        ready = {"phase": "review", "status": "ready"}
        failed = {"phase": "review", "status": "needs_attention"}
        self.write("study/state.json", ready)

        def run(*args):
            self.write("study/state.json", failed)
            raise RuntimeError("same Review operation failed")

        waits = []

        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 7:
                (self.root / "control/STOP").touch()

        self.runner.run_one.side_effect = run
        with patch("research_loop.core.Study") as study:
            study.return_value.retry.side_effect = lambda **kwargs: self.write(
                "study/state.json", ready
            )
            SERVER.drive(
                self.root, self.runner, self.backend, clock=lambda: 0, wait=wait
            )
            self.assertEqual(study.return_value.retry.call_count, 4)
        self.assertEqual(self.runner.run_one.call_count, 5)
        status = SERVER.read(self.root / "control/supervisor-status.json")
        self.assertEqual(status["stage"], "iteration")
        self.assertEqual(status["consecutive_failures"], 7)
        self.assertEqual(status["diagnostics"]["failure_count"], 1)

    def test_explicit_reconciliation_resets_operation_before_renewed_failure(self):
        failed = {"phase": "review", "status": "needs_attention"}
        self.write("study/state.json", failed)
        message = "RuntimeError: retained read-only error"
        self.write(
            "control/supervisor-status.json",
            {
                "first_error": message,
                "failed_state": failed,
                "failed_operation": {"stage": "readonly_retry", "error": message},
                "failures": [
                    {
                        "scope": "parent",
                        "target": str(self.root / "study"),
                        "stage": "readonly_retry",
                        "error": message,
                        "consecutive_failures": 5,
                        "blocked_since": 0,
                    }
                ],
            },
        )
        self.write("study/state.json", failed | {"status": "ready"})

        def finish(*args):
            (self.root / "control/STOP").touch()
            return {"status": "ready"}

        self.runner.run_one.side_effect = finish
        SERVER.drive(self.root, self.runner, self.backend, clock=lambda: 10)
        status = SERVER.read(self.root / "control/supervisor-status.json")
        self.assertEqual(status["failures"][0]["consecutive_failures"], 0)
        (self.root / "control/STOP").unlink()
        self.write("study/state.json", failed)
        with patch("research_loop.core.Study") as study:
            study.return_value.retry.side_effect = RuntimeError(
                "retained read-only error"
            )
            SERVER.drive(
                self.root,
                self.runner,
                self.backend,
                clock=lambda: 20,
                wait=lambda seconds: (self.root / "control/STOP").touch(),
            )
        status = SERVER.read(self.root / "control/supervisor-status.json")
        self.assertEqual(status["consecutive_failures"], 1)
        self.assertEqual(status["blocked_since"], 20)
        self.assertEqual(status["first_error"], message)

    def test_count_reset_is_operation_local_and_recovery_then_renewed_failure(self):
        from integrations.protok.supervisor_state import FailureCounts

        counts = FailureCounts({}, self.root / "study")
        for number in range(7):
            row, _ = counts.failure("iteration", "same error", number)
            counts.success("inspection")
        self.assertEqual(row["consecutive_failures"], 7)
        restarted = FailureCounts({"failures": counts.rows}, self.root / "study")
        row, first = restarted.failure("iteration", "same error", 10)
        self.assertEqual(row["consecutive_failures"], 8)
        self.assertFalse(first)
        restarted.success("iteration")
        row, first = restarted.failure("iteration", "same error", 20)
        self.assertEqual(row["consecutive_failures"], 1)
        self.assertEqual(row["blocked_since"], 20)
        self.assertTrue(first)
        other = FailureCounts({"failures": counts.rows}, self.root / "independent")
        other.failure("iteration", "same error", 21)
        self.assertEqual(row["consecutive_failures"], 1)

    def test_shared_ownership_invalidity_publishes_stop_and_nonzero(self):
        self.backend.owner_token = "expected-owner"
        self.write(
            "control/ownership.json",
            {
                "owner_token": "wrong-owner",
                "expires_at": 10000,
                "handoff_acknowledged": True,
            },
        )
        result = SERVER.drive(self.root, self.runner, self.backend, clock=lambda: 0)
        self.assertEqual(result, "global_attention")
        self.assertEqual(SERVER.exit_status(result), 2)
        self.assertTrue((self.root / "control/STOP").exists())
        self.backend.inspect.assert_not_called()
        self.runner.run_one.assert_not_called()
        SERVER.final_summary(self.root, self.runner, self.backend, result)
        self.assertEqual(
            SERVER.read(self.root / "control/final-summary.json")["reason"], result
        )

    def test_parent_review_can_read_partial_short_report_without_allocation(self):
        self.write("study/state.json", {"phase": "review", "status": "ready"})
        self.backend.inspect.return_value.update(
            used_gpu_seconds=None,
            research_outstanding=["unknown"],
            accounting_status="partial",
        )

        def review(*args):
            (self.root / "control/STOP").touch()
            return {"status": "ready"}

        self.runner.run_one.side_effect = review
        self.assertEqual(
            SERVER.drive(self.root, self.runner, self.backend, clock=lambda: 0),
            "user_stop",
        )
        self.runner.run_one.assert_called_once()

    def test_attention_exit_status_differs_from_a_normal_end(self):
        self.assertEqual(SERVER.exit_status("global_attention"), 2)
        self.assertEqual(SERVER.exit_status("attention"), 0)
        for reason in ("budget", "deadline", "review_stop", "user_stop", "handoff"):
            self.assertEqual(SERVER.exit_status(reason), 0)

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
        self.backend.inspect.return_value["used_gpu_seconds"] = 86500

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

    def test_admitted_round_can_finish_after_cutoff_but_no_next_round_starts(self):
        lease = SERVER.read(self.root / "control/continuation.json")
        lease.update(owner_token="owner", started_at=0)
        self.write("control/continuation.json", lease)
        owner = {
            "owner_token": "owner",
            "expires_at": 10000,
            "handoff_acknowledged": True,
        }
        self.write("control/ownership.json", owner)
        self.backend.owner_token = "owner"
        (self.root / "control/round-admissions").mkdir()
        (self.root / "study/iterations/0007").mkdir(parents=True)
        self.write(
            "control/round-admissions/0007.json",
            {
                "schema": 1,
                "iteration": "0007",
                "token": "admitted",
                "admitted_at": 100,
                "committed_at": 100,
                "cutoff": 10000,
                "owner": owner,
                "continuation": lease,
            },
        )
        self.write(
            "study/iterations/0007/iteration.json", {"round_admission": "admitted"}
        )
        self.write(
            "study/state.json",
            {
                "iteration": 7,
                "status": "ready",
                "phase": "review",
                "round_admission": "admitted",
                "round_admitted_at": 100,
            },
        )

        def finish(root, backend):
            self.write(
                "study/state.json", {"iteration": 7, "status": "ready", "phase": "done"}
            )
            return {"iteration": 7, "status": "ready", "phase": "done"}

        self.runner.run_one.side_effect = finish
        self.assertEqual(
            SERVER.drive(self.root, self.runner, self.backend, clock=lambda: 10001),
            "deadline",
        )
        self.runner.run_one.assert_called_once()

    def test_handoff_takes_precedence_after_finished_round_passes_cutoff(self):
        (self.root / "control/HANDOFF").write_text("switch at boundary")
        self.assertEqual(
            SERVER.drive(self.root, self.runner, self.backend, clock=lambda: 10001),
            "handoff",
        )
        self.runner.run_one.assert_not_called()

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
                self.root,
                self.runner,
                self.backend,
                clock=lambda: now[0],
                wait=wait,
                # Isolate backoff/deadline behavior from blocked escalation.
                escalate_after=10**6,
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
            "long_training_gpu_seconds",
            "long_training_outstanding",
            "total_gpu_seconds",
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
        long_training = Mock()
        long_training.shutdown.side_effect = RuntimeError("long shutdown failed")
        with (
            patch.object(SERVER, "Study"),
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
            patch(
                "integrations.protok.tasks.ExperimentScheduler",
                return_value=long_training,
            ),
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
        self.assertTrue(
            (self.root / "control/long-training-shutdown-error.json").exists()
        )
        self.assertTrue((self.root / "control/recovery-error.json").exists())

    def test_missing_shared_owner_startup_stops_and_summarizes_without_workers(self):
        with (
            patch.object(
                SERVER.sys,
                "argv",
                ["server", "--root", str(self.root), "--runner", "/runner.py"],
            ),
            patch("integrations.protok.tasks.ExperimentScheduler") as scheduler,
        ):
            self.assertEqual(SERVER.main(), 2)
        scheduler.assert_not_called()
        self.assertTrue((self.root / "control/STOP").exists())
        summary = SERVER.read(self.root / "control/final-summary.json")
        self.assertEqual(summary["reason"], "global_attention")
        self.assertEqual(summary["accounting_status"], "unknown")
        self.assertIn("ownership.json", summary["first_error"])

    def test_failed_duplicate_startup_cannot_stop_existing_supervisor(self):
        with (self.root / "control/supervisor.lock").open("a") as lock:
            SERVER.fcntl.flock(lock, SERVER.fcntl.LOCK_EX | SERVER.fcntl.LOCK_NB)
            with (
                patch.object(
                    SERVER.sys,
                    "argv",
                    ["server", "--root", str(self.root), "--runner", "/runner.py"],
                ),
                self.assertRaises(BlockingIOError),
            ):
                SERVER.main()
        self.assertFalse((self.root / "control/STOP").exists())
        self.assertFalse((self.root / "control/final-summary.json").exists())

    def test_invalid_shared_owner_stops_before_worker_join_and_main_summarizes(self):
        self.backend.owner_token = "expected"
        self.write(
            "control/ownership.json",
            {
                "project": "/synthetic/project",
                "owner_token": "changed",
                "expires_at": 10000,
                "handoff_acknowledged": True,
            },
        )
        self.runner.ProTokBackend = Mock(return_value=self.backend)
        spec = SimpleNamespace(loader=SimpleNamespace(exec_module=Mock()))
        scheduler = Mock()
        joined = []

        def shutdown():
            self.assertTrue((self.root / "control/STOP").exists())
            joined.append(True)

        scheduler.shutdown.side_effect = shutdown
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
            patch(
                "integrations.protok.tasks.ExperimentScheduler", return_value=scheduler
            ),
            patch.object(SERVER, "recover_owned"),
        ):
            self.assertEqual(SERVER.main(), 2)
        self.assertEqual(joined, [True])
        scheduler.finish_nominations.assert_not_called()
        summary = SERVER.read(self.root / "control/final-summary.json")
        self.assertEqual(summary["reason"], "global_attention")
        self.assertIn("authorization", summary["first_error"])

    def test_long_training_drains_before_stop_and_accounting_is_published(self):
        self.write(
            "control/ownership.json", {"project": "/project", "owner_token": "owned"}
        )
        self.runner.ProTokBackend = Mock(return_value=self.backend)
        self.backend.inspect.return_value.update(
            long_training_gpu_seconds=80,
            long_training_outstanding=[],
            manual_training_gpu_seconds=40,
            manual_training_outstanding=[],
            total_gpu_seconds=220,
        )
        spec = SimpleNamespace(loader=SimpleNamespace(exec_module=Mock()))
        for reason in ("handoff", "budget"):
            with self.subTest(reason=reason):
                (self.root / "control/STOP").unlink(missing_ok=True)
                scheduler = Mock()
                scheduler.status.return_value = {
                    "status": "finished",
                    "reason": "no_pending",
                }

                def drain():
                    self.assertFalse((self.root / "control/STOP").exists())

                scheduler.shutdown.side_effect = drain
                with (
                    patch.object(
                        SERVER.sys,
                        "argv",
                        ["server", "--root", str(self.root), "--runner", "/runner.py"],
                    ),
                    patch.object(SERVER.signal, "signal"),
                    patch.object(
                        SERVER.importlib.util,
                        "spec_from_file_location",
                        return_value=spec,
                    ),
                    patch.object(
                        SERVER.importlib.util,
                        "module_from_spec",
                        return_value=self.runner,
                    ),
                    patch(
                        "integrations.protok.tasks.ExperimentScheduler",
                        return_value=scheduler,
                    ),
                    patch.object(SERVER, "drive", return_value=reason),
                    patch.object(SERVER, "recover_owned") as recovery,
                ):
                    SERVER.main()
                scheduler.start.assert_called_once()
                scheduler.shutdown.assert_called_once()
                self.assertEqual(
                    scheduler.finish_nominations.call_count, int(reason == "budget")
                )
                self.assertEqual(recovery.call_count, int(reason != "handoff"))
                self.assertEqual(
                    (self.root / "control/STOP").exists(), reason == "budget"
                )
                summary = SERVER.read(self.root / "control/final-summary.json")
                self.assertEqual(summary["long_training_gpu_seconds"], 80)
                self.assertEqual(summary["long_training_outstanding"], [])
                self.assertEqual(summary["manual_training_gpu_seconds"], 40)
                self.assertEqual(summary["manual_training_outstanding"], [])
                self.assertEqual(summary["total_gpu_seconds"], 220)

    def test_long_training_continues_after_short_budget_and_honors_handoff(self):
        scheduler = Mock()
        scheduler.status.side_effect = [
            {"status": "running", "reason": None},
            {"status": "finished", "reason": "no_pending"},
        ]
        waits = []

        def wait(seconds):
            self.assertFalse((self.root / "control/STOP").exists())
            waits.append(seconds)

        self.assertEqual(
            SERVER.finish_long_training(self.root, scheduler, "budget", wait=wait),
            ("budget", None),
        )
        scheduler.finish_nominations.assert_called_once()
        self.assertEqual(waits, [3])

        scheduler.reset_mock()
        scheduler.status.side_effect = None
        scheduler.status.return_value = {"status": "running", "reason": None}

        def request_handoff(seconds):
            (self.root / "control/HANDOFF").touch()

        self.assertEqual(
            SERVER.finish_long_training(
                self.root, scheduler, "budget", wait=request_handoff
            ),
            ("handoff", None),
        )
        self.assertFalse((self.root / "control/STOP").exists())

    def test_blocked_long_training_is_reported_after_short_lane_stops(self):
        scheduler = Mock()
        scheduler.status.return_value = {"status": "blocked", "reason": "unknown job"}
        self.assertEqual(
            SERVER.finish_long_training(self.root, scheduler, "budget"),
            ("attention", "unknown job"),
        )

    def test_drain_waits_for_independent_lane_before_reporting_blocked_sibling(self):
        from integrations.protok.tasks import ExperimentScheduler

        automatic = Mock()
        automatic.status.return_value = {
            "status": "blocked",
            "reason": "retained child",
        }
        manual = Mock()
        manual.status.side_effect = [
            {"status": "running", "active": ["independent-child"]},
            {"status": "finished", "reason": "no_pending"},
        ]
        scheduler = ExperimentScheduler(
            self.root, self.backend, automatic=automatic, manual=manual
        )
        waits = []
        self.assertEqual(
            SERVER.finish_long_training(
                self.root, scheduler, "budget", wait=waits.append
            ),
            ("attention", "retained child"),
        )
        self.assertEqual(waits, [3])
        self.assertFalse((self.root / "control/STOP").exists())

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
        self.backend.inspect.return_value["used_gpu_seconds"] = 86500
        self.assertEqual(
            SERVER.drive(self.root, self.runner, self.backend, clock=lambda: 9000),
            "budget",
        )
        self.runner.run_one.assert_not_called()

    def test_completed_control_repair_precedes_budget_gate(self):
        self.backend.inspect.return_value["used_gpu_seconds"] = 86500
        self.assertEqual(
            SERVER.drive(self.root, self.runner, self.backend, clock=lambda: 9000),
            "budget",
        )
        self.runner.repair_completed_control.assert_called_once_with(
            self.root, self.backend
        )

    def test_existing_review_can_finish_without_budget_for_new_allocation(self):
        self.backend.inspect.return_value["used_gpu_seconds"] = 86500
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
        self.write(
            "study/state.json",
            {"iteration": 7, "status": "running", "phase": "selection"},
        )

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

    def test_scout_polls_without_a_formal_job_during_active_research(self):
        for phase in ("selection", "plan", "execute", "review"):
            for supervisor in (None, "running"):
                with self.subTest(phase=phase, supervisor=supervisor):
                    self.write(
                        "study/state.json",
                        {"iteration": 7, "status": "running", "phase": phase},
                    )
                    status = self.root / "control/supervisor-status.json"
                    status.unlink(missing_ok=True)
                    if supervisor:
                        self.write(status, {"status": supervisor})
                    manager = SimpleNamespace(launch_if_due=Mock(), close=Mock())
                    scheduler = SERVER.ScoutScheduler(self.root, manager=manager)
                    with patch.object(
                        scheduler.stopped,
                        "wait",
                        side_effect=lambda _: scheduler.stopped.set(),
                    ):
                        scheduler._run()
                    manager.launch_if_due.assert_called_once_with()

    def test_scout_suppresses_new_search_when_blocked_failed_or_handing_off(self):
        for supervisor, handoff in (
            ("blocked", False),
            ("failed", False),
            ("running", True),
        ):
            with self.subTest(supervisor=supervisor, handoff=handoff):
                self.write("control/supervisor-status.json", {"status": supervisor})
                if handoff:
                    (self.root / "control/HANDOFF").touch()
                manager = SimpleNamespace(
                    launch_if_due=Mock(), close=Mock(), shutdown=Mock()
                )
                scheduler = SERVER.ScoutScheduler(self.root, manager=manager)
                with patch.object(
                    scheduler.stopped,
                    "wait",
                    side_effect=lambda _: scheduler.stopped.set(),
                ):
                    scheduler._run()
                manager.launch_if_due.assert_not_called()
                manager.close.assert_not_called()
                manager.shutdown.assert_not_called()

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
