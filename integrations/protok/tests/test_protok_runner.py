import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from integrations.protok.backend import CANDIDATE_FILES, METRIC, PROTOCOL, ProTokError
from research_loop.harness import HarnessError
from research_loop.correction import ParameterError
from research_loop.submission import (
    RECEIPT,
    REJECTIONS,
    REQUEST,
    claim_request,
    status as submission_status,
    submit,
)
from test_core import RecordedHarness

SPEC = importlib.util.spec_from_file_location(
    "protok_runner", Path(__file__).resolve().parents[3] / "integrations/protok/run.py"
)
RUNNER = importlib.util.module_from_spec(SPEC)
# Synthetic receipt identity; never depend on the operator's real experiment.
with patch.dict(
    os.environ,
    {
        "PROTOK_BASE_COMMIT": "b" * 40,
        "PROTOK_BASE_FINGERPRINT": "a" * 64,
        "PROTOK_BASE_VALUE": "3.0",
    },
):
    SPEC.loader.exec_module(RUNNER)


class FakeBackend:
    def __init__(self, project, control):
        self.project, self.control, self.owner_token = project, control, "test-owner"
        self.initial = RUNNER.signatures(RUNNER.candidate_bytes(project))
        self.evaluations = 0
        self.completed = {}
        self.row = {
            "fingerprint": RUNNER.BEST_FINGERPRINT,
            "status": "complete",
            "summary": {
                "fingerprint": RUNNER.BEST_FINGERPRINT,
                "protocol": PROTOCOL,
                "objective": METRIC,
                "quality_score_eligible": True,
                "value": RUNNER.BEST_VALUE,
            },
            "evidence": [
                str(control / "original/state.json"),
                str(control / "original/summary.json"),
            ],
        }

    def _config(self):
        return {"reserve_gpu_seconds": 3600}

    def inspect(self):
        return {
            "outstanding": [],
            "records": [self.row],
            "record_count": 12,
            "scored_records": 12,
            "used_gpu_seconds": 11660,
        }

    def prepare_config(self, workspace, output):
        signatures = RUNNER.signatures(RUNNER.candidate_bytes(workspace))
        fingerprint = (
            RUNNER.BEST_FINGERPRINT
            if signatures == self.initial
            else hashlib.sha256(
                json.dumps(signatures, sort_keys=True).encode()
            ).hexdigest()
        )
        RUNNER.write(output, {"workspace": str(workspace), "protocol": PROTOCOL})
        return {
            "fingerprint": fingerprint,
            "config_path": str(output),
            "workspace": str(workspace),
        }

    def evaluate(self, workspace, output, timeout_seconds, *, cancel_event):
        assert not cancel_event.is_set()
        assert timeout_seconds > 0
        self.evaluations += 1
        prepared = self.prepare_config(workspace, output)
        result = {
            **prepared,
            "metric": METRIC,
            "value": 2.5,
            "verified": True,
            "evidence": [str(self.control / "new/summary.json")],
        }
        self.completed[prepared["fingerprint"]] = result
        return result

    def settled_failure(self, workspace, config_path, fingerprint, *, record_name=None):
        raise ProTokError("No certified candidate failure")

    def timed_out_receipt(
        self, workspace, config_path, fingerprint, *, record_name=None
    ):
        raise ProTokError("No settled allocation timeout")

    def completed_receipt(self, workspace, config_path, fingerprint):
        prepared = self.prepare_config(workspace, config_path)
        if prepared["fingerprint"] != fingerprint or self.inspect()["outstanding"]:
            raise ProTokError("No complete, settled, candidate-bound backend receipt")
        if fingerprint == RUNNER.BEST_FINGERPRINT:
            return RUNNER.import_best(self, prepared)
        if fingerprint not in self.completed:
            raise ProTokError("No complete, settled, candidate-bound backend receipt")
        return self.completed[fingerprint]


class ProTokRunnerTests(unittest.TestCase):
    def test_missing_baseline_stops_before_admission_or_receipt_access(self):
        with (
            patch.object(RUNNER, "BEST_FINGERPRINT", ""),
            patch.object(RUNNER, "admission") as admission,
        ):
            backend = Mock()
            with self.assertRaisesRegex(ProTokError, "PROTOK_BASE_FINGERPRINT"):
                RUNNER.init_study(self.root, backend)
            with self.assertRaisesRegex(ProTokError, "PROTOK_BASE_FINGERPRINT"):
                RUNNER.import_best(backend, {})
            admission.assert_not_called()
            backend.inspect.assert_not_called()

    def test_baseline_requires_exact_identities_and_a_finite_metric(self):
        for field, value in (
            ("BEST_COMMIT", ""),
            ("BEST_COMMIT", "not-a-commit"),
            ("BEST_FINGERPRINT", "not-a-fingerprint"),
            ("BEST_VALUE", float("nan")),
            ("BEST_VALUE", float("inf")),
        ):
            with (
                self.subTest(field=field, value=value),
                patch.object(RUNNER, field, value),
            ):
                with self.assertRaises(ProTokError):
                    RUNNER.require_baseline()
        RUNNER.require_baseline()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.project = self.directory / "canonical"
        self.project.mkdir()
        for name in CANDIDATE_FILES:
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "{}\n" if name.endswith(".json") else "# original candidate\n"
            )
        (self.project / "README.md").write_text("Protected source.\n")
        self.command("git", "-C", str(self.project), "init", "--quiet")
        self.command(
            "git", "-C", str(self.project), "add", "--", *CANDIDATE_FILES, "README.md"
        )
        self.command(
            "git",
            "-C",
            str(self.project),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "--quiet",
            "-m",
            "Best source",
        )
        self.baseline = RUNNER.git(self.project, "rev-parse", "HEAD")
        self.root = self.directory / "takeover"
        self.control = self.root / "control"
        self.control.mkdir(parents=True)
        self.workspace = self.directory / "agent"
        self.command(
            "git",
            "clone",
            "--shared",
            "--quiet",
            str(self.project),
            str(self.workspace),
        )
        self.backend = FakeBackend(self.project, self.control)
        now = time.time()
        RUNNER.write(
            self.control / "ownership.json",
            {
                "owner_token": "test-owner",
                "project": str(self.project),
                "handoff_acknowledged": True,
                "expires_at": now + 21600,
            },
        )
        RUNNER.write(
            self.control / "continuation.json",
            {
                "owner_token": "test-owner",
                "max_new_candidates": None,
                "seconds": 21600,
                "authorization": "6h, no candidate count cap",
                "started_at": now - 1,
                "deadline": now + 21599,
            },
        )
        RUNNER.write(self.root / "study/study.json", {"baseline": self.baseline})
        self.harness = RUNNER.ProTokHarness(self.backend, self.root, Mock(), now + 3600)
        self.stop = threading.Event()
        self.deadline = now + 3600

    def command(self, *args):
        subprocess.run(args, check=True, capture_output=True)

    def test_resumed_runner_publishes_current_status_before_execution(self):
        state = {"iteration": 9, "status": "ready", "phase": "execute"}
        RUNNER.write(self.root / "study/state.json", state)
        RUNNER.write(
            self.control / "status.json",
            {"status": "paused", "error": "previous reconciled failure"},
        )

        def execute(root, backend, study):
            current = RUNNER.read(self.control / "status.json")
            self.assertEqual(current["status"], "running")
            self.assertEqual(current["phase"], "execute")
            self.assertNotIn("error", current)
            return state

        with (
            patch.object(RUNNER, "ensure_study_ready", return_value=Mock()),
            patch.object(RUNNER, "_run_one", side_effect=execute),
        ):
            self.assertEqual(RUNNER.run_one(self.root, self.backend), state)

    def test_old_study_rejected_before_overwriting_runner_evidence(self):
        from research_loop.core import StudyError

        RUNNER.write(self.root / "study/study.json", {"version": 2})
        RUNNER.write(self.control / "runner.json", {"status": "retained"})
        RUNNER.write(self.control / "status.json", {"error": "retained failure"})
        before = {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }
        with (
            patch.object(RUNNER, "admission") as admission,
            self.assertRaisesRegex(StudyError, "Unsupported study format"),
        ):
            RUNNER.run_one(self.root, self.backend)
        admission.assert_not_called()
        self.assertEqual(
            {
                path: path.read_bytes()
                for path in self.root.rglob("*")
                if path.is_file()
            },
            before,
        )

    def request(self, action="import", iteration="0001"):
        return self.harness.evaluate_request(
            self.workspace, iteration, {"action": action}, self.stop, self.deadline
        )

    def test_live_watcher_corrects_action_before_one_claim_with_same_deadline(self):
        submit(self.workspace, "import")
        stop = Mock()

        def waiting(_seconds):
            state = submission_status(self.workspace)
            if state["status"] == "parameter_error":
                self.assertEqual(state["feedback"]["request"], {"action": "import"})
                self.assertFalse((self.workspace / RECEIPT).exists())
                submit(self.workspace, "evaluate")
            return False

        stop.wait.side_effect = waiting
        stop.is_set.return_value = False
        with patch.object(
            self.harness, "evaluate_request", return_value={"status": "completed"}
        ) as evaluate:
            self.harness._watch(self.workspace, "0002", stop, self.deadline)
        evaluate.assert_called_once_with(
            self.workspace, "0002", {"action": "evaluate"}, stop, self.deadline
        )
        self.assertEqual(submission_status(self.workspace)["status"], "completed")
        self.assertTrue((self.workspace / REJECTIONS).exists())
        self.assertEqual(self.backend.evaluations, 0)

    def test_action_preflight_uses_frozen_script_mode_without_changing_plan(self):
        contract = {"execution": {"mode": "script"}}
        with (
            patch.object(RUNNER, "research_policy", return_value={"version": 1}),
            patch(
                "integrations.protok.research_execution.frozen_contract",
                return_value=contract,
            ),
        ):
            with self.assertRaisesRegex(ParameterError, "research"):
                self.harness._preflight_request(
                    "0002", {"action": "evaluate"}, self.stop, self.deadline
                )
            self.harness._preflight_request(
                "0002", {"action": "research"}, self.stop, self.deadline
            )
        self.assertEqual(contract, {"execution": {"mode": "script"}})

    def test_preclaim_deadline_and_unknown_failures_never_allow_requeue(self):
        for error in (ProTokError("unknown policy state"), OSError("read failure")):
            with self.subTest(error=error):
                for name in (REQUEST, RECEIPT, REJECTIONS, "evaluation-result.json"):
                    (self.workspace / name).unlink(missing_ok=True)
                submit(self.workspace)
                with (
                    patch.object(self.harness, "_preflight_request", side_effect=error),
                    patch.object(self.harness, "evaluate_request") as evaluate,
                    patch.object(
                        RUNNER,
                        "certify_candidate_failure",
                        side_effect=ProTokError("none"),
                    ),
                    patch.object(
                        RUNNER, "timed_out_job", side_effect=ProTokError("none")
                    ),
                ):
                    self.harness._watch(
                        self.workspace, "0002", threading.Event(), self.deadline
                    )
                evaluate.assert_not_called()
                self.assertEqual(submission_status(self.workspace)["status"], "failed")
                self.assertEqual(submit(self.workspace)["status"], "failed")
                self.assertFalse((self.workspace / REJECTIONS).exists())
        with self.assertRaisesRegex(ProTokError, "Execute ended"):
            self.harness._preflight_request(
                "0002", {"action": "import"}, self.stop, time.time() - 1
            )

    def test_rejection_only_history_is_not_a_computation_effect(self):
        submit(self.workspace, "import")
        claim_request(
            self.workspace,
            preflight=Mock(side_effect=ParameterError("Use evaluate")),
        )
        response = {
            "outcome": "incomplete",
            "markdown": "No evaluation was run.",
            "artifacts": [],
            "measurements": [],
        }
        RUNNER.local_incomplete(
            self.root, self.backend, self.workspace, "0002", response
        )
        self.assertEqual(self.backend.evaluations, 0)

    def test_parameter_error_after_claim_is_terminal_without_requeue(self):
        submit(self.workspace)
        with (
            patch.object(
                self.harness,
                "evaluate_request",
                side_effect=ParameterError("late error with possible effects"),
            ) as evaluate,
            patch.object(
                RUNNER, "certify_candidate_failure", side_effect=ProTokError("none")
            ),
            patch.object(RUNNER, "timed_out_job", side_effect=ProTokError("none")),
        ):
            self.harness._watch(
                self.workspace, "0002", threading.Event(), self.deadline
            )
        evaluate.assert_called_once()
        self.assertTrue((self.workspace / RECEIPT).exists())
        self.assertFalse((self.workspace / REJECTIONS).exists())
        self.assertEqual(submit(self.workspace)["status"], "failed")

    def test_operator_environment_reaches_readonly_and_writable_delegate_prompts(self):
        environment = {
            "ruff": "/private/tmp/research-loop-uv-tools/ruff/bin/ruff",
            "cpu_python": "/opt/anaconda3/bin/python",
            "constraints": "No installs; at most 4 CPU threads; tiny in-memory checks.",
            "protected_evaluation": "Formal HPC Torch2.9 gates remain mandatory.",
        }
        path = self.control / "execution-environment.json"
        RUNNER.write(path, environment)
        original = "Frozen Plan and scope remain authoritative."
        for writable in (False, True):
            with self.subTest(writable=writable):
                delegate = Mock()
                # End Execute before any request; only forwarding is under test.
                delegate.run.side_effect = HarnessError("fixture preflight finished")
                harness = RUNNER.ProTokHarness(
                    self.backend, self.root, delegate, self.deadline
                )
                with self.assertRaisesRegex(HarnessError, "fixture preflight finished"):
                    harness.run(
                        prompt=original,
                        writable=writable,
                        workspace=self.workspace,
                        timeout_seconds=60,
                    )
                forwarded = delegate.run.call_args.kwargs["prompt"]
                self.assertTrue(forwarded.startswith(original))
                self.assertIn(
                    "Operator-supplied execution environment from trusted control",
                    forwarded,
                )
                self.assertIn(
                    "existing scope, frozen contract and protected evaluation checks remain unchanged",
                    forwarded,
                )
                self.assertIn(path.read_text(), forwarded)
                self.assertEqual(self.backend.evaluations, 0)
                self.assertFalse((self.control / "jobs").exists())

    def test_submission_api_instructions_reach_all_stages(self):
        delegate = Mock()
        delegate.run.return_value = {"markdown": "Read-only response."}
        harness = RUNNER.ProTokHarness(self.backend, self.root, delegate, self.deadline)
        response = harness.run(
            prompt="Original stage instructions.",
            writable=False,
            workspace=self.workspace,
            timeout_seconds=60,
        )
        self.assertEqual(response, delegate.run.return_value)
        prompt = delegate.run.call_args.kwargs["prompt"]
        self.assertTrue(prompt.startswith("Original stage instructions."))
        self.assertIn("submission.py submit --workspace .", prompt)
        self.assertIn("transport instructions replace older", prompt)

    def test_local_incomplete_rejects_any_evaluation_job_or_interface(self):
        response = {
            "outcome": "incomplete",
            "markdown": "Actual local check failed.",
            "artifacts": [],
            "measurements": [],
        }
        job = self.control / "jobs/0002"
        RUNNER.write(job / "job.json", {"status": "prepared"})
        with self.assertRaisesRegex(ProTokError, "existing evaluation job"):
            RUNNER.local_incomplete(
                self.root, self.backend, self.workspace, "0002", response
            )
        (job / "job.json").unlink()
        job.rmdir()
        for name in RUNNER.SUBMISSION_EFFECT_FILES:
            with self.subTest(name=name):
                path = self.workspace / name
                RUNNER.write(path, {"status": "unknown"})
                with self.assertRaisesRegex(ProTokError, "evaluation interface"):
                    RUNNER.local_incomplete(
                        self.root, self.backend, self.workspace, "0002", response
                    )
                path.unlink()
        response["artifacts"] = [{"path": "model.py", "version": "unmeasured"}]
        with self.assertRaisesRegex(ProTokError, "empty artifacts/measurements"):
            RUNNER.local_incomplete(
                self.root, self.backend, self.workspace, "0002", response
            )

    def test_status_lock_does_not_count_as_a_submitted_evaluation(self):
        self.assertEqual(submission_status(self.workspace)["status"], "not_submitted")
        response = {
            "outcome": "incomplete",
            "markdown": "Local check failed.",
            "artifacts": [],
            "measurements": [],
        }
        diagnostic = RUNNER.local_incomplete(
            self.root, self.backend, self.workspace, "0002", response
        )
        self.assertEqual(diagnostic["classification"], "unsubmitted-interruption")
        self.assertEqual(self.backend.evaluations, 0)

    def test_local_static_fault_requires_source_evidence_not_report_words(self):
        response = {
            "outcome": "incomplete",
            "markdown": "SyntaxError: candidate failed its numerical check.",
            "artifacts": [],
            "measurements": [],
        }
        attempt = self.root / "study/iterations/0002/execute/attempt-001"
        diagnostic = RUNNER.local_incomplete(
            self.root,
            self.backend,
            self.workspace,
            "0002",
            response,
            run_dir=attempt,
        )
        self.assertEqual(diagnostic["classification"], "unsubmitted-interruption")
        self.assertIsNone(diagnostic["candidate_fault"])
        self.assertEqual(diagnostic["attempt"], str(attempt))
        self.assertEqual(diagnostic["source_signatures"], self.backend.initial)
        for name, source, error_type in (
            (CANDIDATE_FILES[0], "def broken(:\n", "SyntaxError"),
            (CANDIDATE_FILES[2], '{"broken":', "JSONDecodeError"),
        ):
            with self.subTest(name=name):
                path = self.workspace / name
                original = path.read_bytes()
                path.write_text(source)
                with patch.object(
                    RUNNER, "candidate_bytes", wraps=RUNNER.candidate_bytes
                ) as read_source:
                    diagnostic = RUNNER.local_incomplete(
                        self.root, self.backend, self.workspace, "0002", response
                    )
                self.assertEqual(read_source.call_count, 1)
                self.assertEqual(diagnostic["classification"], "candidate-fault")
                self.assertEqual(diagnostic["candidate_fault"]["type"], error_type)
                self.assertEqual(diagnostic["candidate_fault"]["path"], name)
                path.write_bytes(original)
        (self.workspace / CANDIDATE_FILES[2]).unlink()
        diagnostic = RUNNER.local_incomplete(
            self.root, self.backend, self.workspace, "0002", response
        )
        self.assertEqual(diagnostic["classification"], "unsubmitted-interruption")
        self.assertIsNone(diagnostic["source_signatures"])
        self.assertIn("small regular source file", diagnostic["source_error"])
        self.assertEqual(self.backend.evaluations, 0)

    def test_completed_local_failure_enters_review_without_static_fault(self):
        from research_loop.harness import publish_completed_response

        response = {
            "outcome": "incomplete",
            "markdown": "linkat failed with ENOENT; no request was published.",
            "artifacts": [],
            "measurements": [],
        }
        iteration = self.workspace.parent.name
        legacy = self.control / "diagnostics" / (iteration + ".json")
        RUNNER.write(legacy, {"provenance": "local-preflight-rejection-no-evaluation"})
        previous = legacy.read_bytes()
        for number in (1, 2):
            attempt = self.root / f"study/iterations/0002/execute/attempt-{number:03d}"
            attempt.mkdir(parents=True)
            publish_completed_response(attempt, response)
            self.assertEqual(
                self.harness.validate_completed(
                    response, workspace=self.workspace, run_dir=attempt
                ),
                response,
            )
            diagnostic = RUNNER.read(
                self.control / "diagnostics" / iteration / (attempt.name + ".json")
            )
            self.assertEqual(diagnostic["classification"], "completed-incomplete")
            self.assertTrue(diagnostic["completed_response"])
            self.assertEqual(diagnostic["attempt"], str(attempt))
        self.assertEqual(legacy.read_bytes(), previous)
        self.assertEqual(
            len(list((self.control / "diagnostics" / iteration).glob("*.json"))), 2
        )
        self.assertFalse((self.control / "jobs").exists())
        self.assertEqual(self.backend.evaluations, 0)

    def test_unexpected_certification_error_returns_pipeline_diagnostic_without_fallback(
        self,
    ):
        RUNNER.write(self.workspace / REQUEST, {"action": "evaluate"})
        with (
            patch.object(
                self.harness,
                "evaluate_request",
                side_effect=ProTokError("evaluator rejected"),
            ),
            patch.object(
                RUNNER,
                "certify_candidate_failure",
                side_effect=AttributeError("certifier defect"),
            ),
            patch.object(RUNNER, "timed_out_job") as timeout,
        ):
            self.harness._watch(
                self.workspace, "0002", threading.Event(), self.deadline
            )
        result = RUNNER.read(self.workspace / "evaluation-result.json")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["diagnostics"]["owner"], "pipeline")
        self.assertIn("certifier defect", result["error"])
        self.assertTrue(self.harness.errors)
        self.assertIsNone(self.harness.recoverable_failure)
        timeout.assert_not_called()

    def test_diagnostic_context_finds_this_iterations_research_receipt(self):
        public = {
            "owner": "workspace",
            "logs": [{"log_id": "safe-id", "tail": "traceback detail"}],
        }
        RUNNER.write(
            self.control / "research-jobs/0002/receipt.json",
            {"diagnostics": public, "private": "operator-only"},
        )
        RUNNER.write(
            self.control / "research-jobs/0003/receipt.json",
            {"diagnostics": {"logs": [{"tail": "other iteration"}]}},
        )
        self.assertEqual(self.harness.diagnostic_context("0002"), [public])

    def test_completed_local_response_still_blocks_scope_violation_and_unknown_effects(
        self,
    ):
        from research_loop.harness import publish_completed_response

        response = {
            "outcome": "incomplete",
            "markdown": "No measurement.",
            "artifacts": [],
            "measurements": [],
        }
        attempt = self.root / "study/iterations/0002/execute/attempt-001"
        attempt.mkdir(parents=True)
        publish_completed_response(attempt, response)
        (self.workspace / "README.md").write_text("unauthorized protected change")
        with self.assertRaisesRegex(HarnessError, "frozen candidate scope"):
            self.harness.validate_completed(
                response, workspace=self.workspace, run_dir=attempt
            )
        (self.workspace / "README.md").write_text("Protected source.\n")
        with patch.object(
            self.backend, "inspect", return_value={"outstanding": ["unknown job"]}
        ):
            with self.assertRaisesRegex(ProTokError, "Outstanding backend effects"):
                self.harness.validate_completed(
                    response, workspace=self.workspace, run_dir=attempt
                )
        interrupted = attempt.parent / "attempt-002"
        interrupted.mkdir()
        RUNNER.write(interrupted / "response.json", response)
        RUNNER.write(interrupted / "process.json", {"status": "failed", "exit_code": 1})
        with self.assertRaisesRegex(HarnessError, "no successful completion receipt"):
            self.harness.validate_completed(
                response, workspace=self.workspace, run_dir=interrupted
            )

    def test_parent_interfaces_are_cleared_once_and_current_submission_is_retained(
        self,
    ):
        RUNNER.write(self.workspace / RECEIPT, {"action": "evaluate"})
        RUNNER.write(self.workspace / "evaluation-result.json", {"status": "completed"})
        self.command(
            "git",
            "-C",
            str(self.workspace),
            "add",
            "--",
            RECEIPT,
            "evaluation-result.json",
        )
        self.command(
            "git",
            "-C",
            str(self.workspace),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "--quiet",
            "-m",
            "Parent receipts",
        )
        RUNNER.initialize_submission_workspace(self.root, self.workspace, "0002")
        self.assertEqual(submission_status(self.workspace)["status"], "not_submitted")
        submit(self.workspace)
        claim_request(self.workspace)
        RUNNER.initialize_submission_workspace(self.root, self.workspace, "0002")
        self.assertEqual(submit(self.workspace)["status"], "accepted")
        self.assertTrue((self.workspace / RECEIPT).exists())
        self.assertFalse((self.workspace / REQUEST).exists())

    def test_new_unreconciled_interface_is_never_cleared(self):
        submit(self.workspace)
        with self.assertRaisesRegex(ProTokError, "Unreconciled evaluation interface"):
            RUNNER.initialize_submission_workspace(self.root, self.workspace, "0002")
        self.assertEqual(submission_status(self.workspace)["status"], "pending")
        self.assertFalse((self.control / "evaluation-interfaces/0002.json").exists())

    def test_operator_continuation_rejects_changed_source_before_allocation(self):
        RUNNER.write(
            self.workspace.parent / "iteration.json",
            {"operator_resume": {"source_signatures": {"wrong": "source"}}},
        )
        with self.assertRaisesRegex(ProTokError, "changed the unmeasured candidate"):
            self.request(action="evaluate", iteration="0002")
        self.assertFalse((self.control / "jobs/0002").exists())
        self.assertEqual(self.backend.evaluations, 0)

    def test_repeated_api_calls_through_real_bridge_allocate_once(self):
        workspace = self.root / "study/iterations/0002/workspace"
        workspace.parent.mkdir(parents=True)
        self.command(
            "git", "clone", "--shared", "--quiet", str(self.project), str(workspace)
        )
        (workspace / CANDIDATE_FILES[0]).write_text("# new candidate\n")

        def execute(**kwargs):
            submit(workspace)
            until = time.monotonic() + 5
            while submission_status(workspace)["status"] not in {"completed", "failed"}:
                submit(workspace)
                if time.monotonic() >= until:
                    raise AssertionError("No evaluation result")
                time.sleep(0.01)
            return {"outcome": "completed", "markdown": "Measured using reusable API."}

        harness = RUNNER.ProTokHarness(
            self.backend, self.root, Mock(run=execute), self.deadline
        )
        result = harness.run(
            prompt="Execute", writable=True, workspace=workspace, timeout_seconds=60
        )
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(self.backend.evaluations, 1)
        self.assertEqual(submit(workspace)["status"], "completed")
        with self.assertRaisesRegex(HarnessError, "requires reconciliation"):
            harness.run(
                prompt="Execute", writable=True, workspace=workspace, timeout_seconds=60
            )
        self.assertEqual(self.backend.evaluations, 1)

    def test_first_import_freezes_only_candidate_source_without_allocating(self):
        RUNNER.write(
            self.workspace / "evaluation-result.json", {"old": "interface is allowed"}
        )
        result = self.request()
        source = Path(result["frozen_source"])
        self.assertEqual(self.backend.evaluations, 0)
        self.assertEqual(result["value"], RUNNER.BEST_VALUE)
        self.assertEqual(result["provenance"], "historical-import-no-allocation")
        self.assertEqual(result["fingerprint"], RUNNER.BEST_FINGERPRINT)
        self.assertEqual(result["artifact"]["version"], RUNNER.BEST_FINGERPRINT)
        self.assertFalse((source / "evaluation-result.json").exists())
        self.assertEqual(
            RUNNER.candidate_bytes(source), RUNNER.candidate_bytes(self.workspace)
        )
        self.assertEqual(RUNNER.git(source, "rev-parse", "HEAD"), self.baseline)
        self.assertEqual(RUNNER.git(self.project, "status", "--porcelain"), "")

    def test_completed_job_is_idempotent_and_changed_source_cannot_rewind_it(self):
        first = self.request()
        second = self.request()
        self.assertEqual(first, second)
        self.assertEqual(self.backend.evaluations, 0)
        (self.workspace / CANDIDATE_FILES[0]).write_text("# changed after complete\n")
        with self.assertRaisesRegex(ProTokError, "cannot be replaced or rewound"):
            self.request()
        self.assertEqual(RUNNER.read(self.control / "jobs/0001/result.json"), first)

    def test_prepared_or_submitted_job_is_not_relaunched(self):
        for status in ("prepared", "submitted"):
            with self.subTest(status=status):
                job = self.control / "jobs/0002"
                RUNNER.write(
                    job / "job.json",
                    {
                        "status": status,
                        "request": {"action": "evaluate"},
                        "source_signatures": RUNNER.signatures(
                            RUNNER.candidate_bytes(self.workspace)
                        ),
                    },
                )
                with self.assertRaisesRegex(ProTokError, "unknown effects"):
                    self.request("evaluate", "0002")
                self.assertEqual(self.backend.evaluations, 0)

    def test_evaluate_freezes_new_source_and_allocates_once(self):
        (self.workspace / CANDIDATE_FILES[1]).write_text(
            "# mathematically specified new loss\n"
        )
        result = self.request("evaluate", "0002")
        self.assertEqual(self.backend.evaluations, 1)
        self.assertNotEqual(result["fingerprint"], RUNNER.BEST_FINGERPRINT)
        self.assertNotEqual(result["source_commit"], self.baseline)
        self.assertEqual(self.request("evaluate", "0002"), result)
        self.assertEqual(self.backend.evaluations, 1)
        self.assertEqual(RUNNER.git(self.project, "rev-parse", "HEAD"), self.baseline)

    def test_completed_backend_repairs_result_and_job_publication_without_evaluation(
        self,
    ):
        (self.workspace / CANDIDATE_FILES[1]).write_text("# measured candidate\n")
        result = self.request("evaluate", "0002")
        job = self.control / "jobs/0002"
        record = RUNNER.read(job / "job.json")
        RUNNER.write(job / "job.json", {**record, "status": "submitted"})
        repaired = RUNNER.completed_job(self.root, self.backend, self.workspace, "0002")
        self.assertEqual(repaired, result)
        self.assertEqual(RUNNER.read(job / "job.json")["status"], "completed")
        (job / "result.json").unlink()
        RUNNER.write(job / "job.json", {**record, "status": "submitted"})
        repaired = RUNNER.completed_job(self.root, self.backend, self.workspace, "0002")
        self.assertEqual(repaired["value"], result["value"])
        self.assertEqual(self.backend.evaluations, 1)
        RUNNER.write(job / "result.json", {**repaired, "value": 0.01})
        with self.assertRaisesRegex(ProTokError, "authoritative receipt"):
            RUNNER.completed_job(self.root, self.backend, self.workspace, "0002")
        self.assertEqual(self.backend.evaluations, 1)

    def test_controller_constructs_measurement_and_preserves_raw_report(self):
        from research_loop.harness import completed_response

        workspace = self.root / "study/iterations/0001/workspace"
        workspace.parent.mkdir(parents=True)
        self.command(
            "git", "clone", "--shared", "--quiet", str(self.project), str(workspace)
        )
        outer = self.directory / "attempt-001"
        outer.mkdir()

        def delegate_run(**kwargs):
            self.assertEqual(
                set(kwargs["schema"]["properties"]), {"outcome", "markdown"}
            )
            self.assertIn("return ONLY outcome and markdown", kwargs["prompt"])
            RUNNER.write(workspace / "evaluation-request.json", {"action": "import"})
            until = time.monotonic() + 5
            while not (workspace / "evaluation-result.json").exists():
                if time.monotonic() >= until:
                    raise AssertionError("No backend result")
                time.sleep(0.01)
            report = {
                "outcome": "incomplete",
                "markdown": "Model interpretation; identity and score are controller-owned.",
            }
            RUNNER.write(kwargs["run_dir"] / "response.json", report)
            RUNNER.write(kwargs["run_dir"] / "process.json", {"status": "succeeded"})
            return report

        harness = RUNNER.ProTokHarness(
            self.backend, self.root, Mock(run=delegate_run), self.deadline
        )
        result = harness.run(
            prompt="Execute",
            writable=True,
            workspace=workspace,
            run_dir=outer,
            timeout_seconds=60,
        )
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(
            result["measurements"][0]["candidate_id"], RUNNER.BEST_FINGERPRINT
        )
        self.assertEqual(
            result["measurements"][0]["metrics"][0]["value"], RUNNER.BEST_VALUE
        )
        self.assertEqual(len(result["artifacts"]), 1)
        raw = completed_response(outer / "agent")
        self.assertEqual(set(raw), {"outcome", "markdown"})
        self.assertEqual(raw["outcome"], "incomplete")
        self.assertEqual(completed_response(outer), result)
        result["measurements"][0]["candidate_id"] = "0001:" + RUNNER.BEST_FINGERPRINT
        with self.assertRaisesRegex(HarnessError, "does not match"):
            harness.validate_completed(result, workspace=workspace, run_dir=outer)
        self.assertEqual(self.backend.evaluations, 0)

    def test_scope_rejects_tracked_untracked_and_ignored_extra_files(self):
        for name in ("README.md", "scratch.py", "ignored.tmp"):
            with self.subTest(name=name):
                # The ignored fixture is ignored through local metadata, never a candidate change.
                exclude = Path(
                    RUNNER.git(
                        self.workspace, "rev-parse", "--git-path", "info/exclude"
                    )
                )
                if not exclude.is_absolute():
                    exclude = self.workspace / exclude
                with exclude.open("a") as handle:
                    handle.write("\nignored.tmp\n")
                path = self.workspace / name
                old = path.read_text() if path.exists() else None
                path.write_text("outside allowed source\n")
                with self.assertRaisesRegex(ProTokError, "outside candidate scope"):
                    self.request()
                if old is None:
                    path.unlink()
                else:
                    path.write_text(old)
        self.assertFalse((self.control / "jobs").exists())
        self.assertEqual(self.backend.evaluations, 0)

    def test_import_rejects_changed_candidate_and_ineligible_historical_record(self):
        self.backend.row["summary"]["quality_score_eligible"] = False
        with self.assertRaisesRegex(ProTokError, "complete eligible"):
            self.request()
        self.assertEqual(self.backend.evaluations, 0)

    def test_stop_or_wrong_first_action_never_prepares_a_job(self):
        with self.assertRaisesRegex(ProTokError, "first iteration imports"):
            self.request("evaluate")
        self.stop.set()
        with self.assertRaisesRegex(ProTokError, "Execute ended"):
            self.request()
        self.assertFalse((self.control / "jobs").exists())

    def test_modified_source_during_evaluation_is_rejected(self):
        evaluate = self.backend.evaluate

        def mutate(*args, **kwargs):
            result = evaluate(*args, **kwargs)
            (self.workspace / CANDIDATE_FILES[0]).write_text(
                "# changed while evaluating\n"
            )
            return result

        self.backend.evaluate = mutate
        with self.assertRaisesRegex(ProTokError, "changed after source freeze"):
            self.request("evaluate", "0002")
        self.assertEqual(self.backend.evaluations, 1)
        self.assertEqual(
            RUNNER.read(self.control / "jobs/0002/job.json")["status"], "submitted"
        )

    def test_interrupted_execute_reconciles_original_attempt_then_independent_review(
        self,
    ):
        from research_loop import initialize
        from research_loop.harness import completed_response

        (self.root / "study/study.json").unlink()
        (self.root / "study").rmdir()
        initialize(
            self.root / "study",
            goal=RUNNER.GOAL,
            workspace=self.project,
            allow_write=True,
            max_iterations=None,
            max_calls=None,
            global_evaluation={
                "id": PROTOCOL,
                "comparison_scope": PROTOCOL,
                "protocol": "Fixed development protocol.",
                "data_role": "development",
                "allow_feedback": True,
                "objectives": [
                    {"name": METRIC, "direction": "minimize", "unit": "Angstrom"}
                ],
                "constraints": [],
            },
        )

        self.seed_synthetic_initialization()

        class InterruptedHarness(RecordedHarness):
            stop_after = 10

            def response(delegate, phase, context, kwargs):
                if phase != "execute":
                    return super().response(phase, context, kwargs)
                workspace = Path(kwargs["workspace"])
                RUNNER.write(
                    workspace / "evaluation-request.json", {"action": "import"}
                )
                until = time.monotonic() + 5
                while not (workspace / "evaluation-result.json").exists():
                    if time.monotonic() >= until:
                        raise AssertionError("No backend receipt")
                    time.sleep(0.01)
                raise HarnessError(
                    "Agent transport interrupted after backend completion"
                )

        delegate = InterruptedHarness()
        delegate.stop_after = 10
        with patch.object(RUNNER, "CodexHarness", return_value=delegate):
            with self.assertRaisesRegex(HarnessError, "transport interrupted"):
                RUNNER.run_one(self.root, self.backend)
        study = RUNNER.Study(self.root / "study")
        before = study.status()
        partial = study.root / before["calls"]["execute"]
        self.assertIsNone(completed_response(partial))
        (partial / "agent/partial.json").write_text('{"partial":')
        partial_bytes = (partial / "agent/partial.json").read_bytes()
        calls = len(delegate.calls)
        repaired = RUNNER.reconcile_execute(
            self.root, self.backend, authorization="Restore settled Execute"
        )
        replacement = study.root / repaired["calls"]["execute"]
        self.assertEqual(partial, replacement)
        self.assertIn(
            "Controller reconciliation", completed_response(replacement)["markdown"]
        )
        self.assertFalse((partial / "process.json").exists())
        self.assertEqual((partial / "agent/partial.json").read_bytes(), partial_bytes)
        self.assertEqual(repaired["call_count"], before["call_count"])
        self.assertEqual(repaired["elapsed_seconds"], before["elapsed_seconds"])
        self.assertEqual(len(delegate.calls), calls)
        self.assertFalse((self.control / "STOP").exists())
        with patch.object(RUNNER, "CodexHarness", return_value=delegate):
            after = RUNNER.run_one(self.root, self.backend)
        self.assertEqual(after["phase"], "done")
        self.assertEqual(delegate.count("execute"), 1)
        self.assertEqual(delegate.count("review"), 1)
        self.assertEqual(self.backend.evaluations, 0)
        self.assertEqual(
            RUNNER.read(self.control / "active-best.json")["metrics"][METRIC],
            RUNNER.BEST_VALUE,
        )

        # Crash after independent Review but before adapter control publication.
        (self.control / "active-best.json").unlink()
        (self.control / "journal.jsonl").unlink()
        retained = study.status()
        RUNNER.write(
            study.root / "state.json",
            {**retained, "phase": "review", "status": "needs_attention"},
        )
        (self.control / "STOP").touch()
        owner = RUNNER.read(self.control / "ownership.json")
        RUNNER.write(
            self.control / "ownership.json", {**owner, "expires_at": time.time() - 1}
        )
        stale_runner = {"status": "running", "iteration_before": 0, "pid": 12345}
        RUNNER.write(self.control / "runner.json", stale_runner)
        self.assertTrue(RUNNER.repair_completed_control(self.root, self.backend))
        self.assertEqual(study.status()["phase"], "done")
        self.assertTrue((self.control / "active-best.json").exists())
        self.assertEqual(
            len((self.control / "journal.jsonl").read_text().splitlines()), 1
        )
        self.assertEqual(delegate.count("review"), 1)
        self.assertEqual(self.backend.evaluations, 0)
        repaired_runner = RUNNER.read(self.control / "runner.json")
        self.assertEqual(repaired_runner["status"], "completed")
        self.assertEqual(repaired_runner["iteration"], retained["iteration"])
        self.assertEqual(
            RUNNER.read(Path(repaired_runner["reconciliation"]))["previous_runner"],
            stale_runner,
        )
        self.assertIn("publication-repair", repaired_runner["completion_provenance"])
        self.assertTrue(RUNNER.repair_completed_control(self.root, self.backend))
        self.assertEqual(RUNNER.read(self.control / "runner.json"), repaired_runner)

    def test_initialization_seeds_history_and_routes_without_old_dag_or_count_caps(
        self,
    ):
        (self.project / "autoresearch-report.md").write_text(
            "Historical best: 2.6046197081836633 A; 12 records, 11660 GPU s.\n"
        )
        RUNNER.git(self.project, "add", "--", "autoresearch-report.md")
        RUNNER.git(
            self.project,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "-m",
            "Historical report",
        )
        best = RUNNER.git(self.project, "rev-parse", "HEAD")
        root = self.directory / "initialized"
        control = root / "control"
        for name in ("ownership.json", "continuation.json"):
            RUNNER.write(control / name, RUNNER.read(self.control / name))
        backend = FakeBackend(self.project, control)
        original_owner = (control / "ownership.json").read_text()
        with patch.object(RUNNER, "BEST_COMMIT", best):
            state = RUNNER.init_study(root, backend)
        config = RUNNER.read(root / "study/study.json")
        self.assertEqual(state["iteration"], 0)
        self.assertIsNone(config["max_calls"])
        self.assertIsNone(config["max_iterations"])
        self.assertEqual(config["strategy"], {"selection": "debate", "review": "full"})
        self.assertEqual(config["harness"]["executable"], RUNNER.CODEX)
        self.assertEqual(len(RUNNER.read(root / "study/routes/index.json")), 3)
        self.assertEqual(list((root / "study/iterations").iterdir()), [])
        self.assertIn("Historical best:", (root / "study/research.md").read_text())
        self.assertEqual((control / "ownership.json").read_text(), original_owner)
        self.assertEqual(RUNNER.git(self.project, "status", "--porcelain"), "")
        self.assertEqual(backend.evaluations, 0)

    def seed_synthetic_initialization(self):
        # Valid adapter seeds for core/harness fixtures, not a marker-only bypass.
        from integrations.protok.initialization import COMPLETION, SEEDS, completion

        folder = self.root / "study"
        (folder / "research.md").write_text(
            "# Historical ProTok evidence\n\nSynthetic evidence only.\n"
        )
        routes = {}
        for rid in SEEDS:
            research = f"routes/{rid}/research.md"
            evaluation = f"routes/{rid}/evaluation.json"
            RUNNER.write(folder / evaluation, {})
            (folder / research).write_text("# Synthetic seed\n\nNo measurement.\n")
            routes[rid] = {
                "title": "Synthetic seed",
                "status": "active",
                "reason": "Offline fixture",
                "resume_condition": None,
                "research_ref": research,
                "evaluation_ref": evaluation,
            }
        RUNNER.write(folder / "routes/index.json", routes)
        RUNNER.write(
            folder / COMPLETION,
            completion(RUNNER.read(folder / "study.json")["baseline"]),
        )
        RUNNER.ensure_study_ready(self.root)

    def study_snapshot(self, root):
        return {
            p.relative_to(root): p.read_bytes()
            for p in (root / "study").rglob("*")
            if p.is_file()
        }

    def initialization_root(self):
        (self.project / "autoresearch-report.md").write_text(
            "Historical best: 2.6046197081836633 A; 12 records, 11660 GPU s.\n"
        )
        RUNNER.git(self.project, "add", "--", "autoresearch-report.md")
        RUNNER.git(
            self.project,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "-m",
            "Historical report",
        )
        best = RUNNER.git(self.project, "rev-parse", "HEAD")
        root = self.directory / "initialized"
        control = root / "control"
        for name in ("ownership.json", "continuation.json"):
            RUNNER.write(control / name, RUNNER.read(self.control / name))
        return root, FakeBackend(self.project, control), best

    def test_supervisor_resumes_owned_staging_after_one_seed_write_failure(self):
        from integrations.protok import server

        root, backend, best = self.initialization_root()
        (root / "study").mkdir()  # An empty target is valid for initialization.
        lease = RUNNER.read(root / "control/continuation.json")
        RUNNER.write(
            root / "control/continuation.json",
            {
                **lease,
                "schema": 2,
                "seconds": 86400,
                "initial_gpu_seconds": backend.inspect()["used_gpu_seconds"],
                "additional_gpu_seconds": 86400,
            },
        )
        original = RUNNER.write
        interrupted = False
        waits = []

        def write(path, value):
            nonlocal interrupted
            if not interrupted and str(path).endswith("R0002/evaluation.json"):
                interrupted = True
                raise OSError("synthetic seed write interruption")
            return original(path, value)

        def wait(seconds):
            waits.append(seconds)
            self.assertFalse((root / "study").exists())
            self.assertTrue((root / ".initializing/study/study.json").exists())
            status = RUNNER.read(root / "control/supervisor-status.json")
            self.assertEqual(
                (status["stage"], status["consecutive_failures"]), ("initialization", 1)
            )

        def finish(*args):
            state = RUNNER.ensure_study_ready(root).status()
            self.assertEqual((state["iteration"], state["call_count"]), (0, 0))
            (root / "control/STOP").touch()
            return state

        with (
            patch.object(RUNNER, "BEST_COMMIT", best),
            patch.object(RUNNER, "write", side_effect=write),
            patch.object(RUNNER, "init_study", wraps=RUNNER.init_study) as init,
            patch.object(RUNNER, "run_one", side_effect=finish) as run,
        ):
            self.assertEqual(
                server.drive(root, RUNNER, backend, wait=wait), "user_stop"
            )
        self.assertEqual(init.call_count, 2)
        run.assert_called_once()
        self.assertEqual(waits, [5])
        self.assertEqual(backend.evaluations, 0)
        self.assertEqual(RUNNER.git(root / "baseline", "rev-parse", "HEAD"), best)
        status = RUNNER.read(root / "control/supervisor-status.json")
        self.assertIsNone(
            status["failed_operation"] if "failed_operation" in status else None
        )
        self.assertTrue(
            all(row["consecutive_failures"] == 0 for row in status["failures"])
        )

    def test_initialization_resumes_after_a_retained_baseline(self):
        root, backend, best = self.initialization_root()
        with patch.object(RUNNER, "BEST_COMMIT", best):
            RUNNER.init_study(root, backend)
            # Crash after the clone and before the study was published.
            shutil.rmtree(root / "study")
            with patch.object(
                RUNNER.subprocess, "run", wraps=RUNNER.subprocess.run
            ) as run:
                state = RUNNER.init_study(root, backend)
        clones = [
            call
            for call in run.call_args_list
            if isinstance(call.args[0], list) and "clone" in call.args[0]
        ]
        self.assertEqual(state["iteration"], 0)
        self.assertEqual(clones, [])
        self.assertEqual(RUNNER.git(root / "baseline", "rev-parse", "HEAD"), best)
        self.assertEqual(len(RUNNER.read(root / "study/routes/index.json")), 3)

    def test_initialization_rejects_a_retained_baseline_at_another_commit(self):
        root, backend, best = self.initialization_root()
        with patch.object(RUNNER, "BEST_COMMIT", best):
            RUNNER.init_study(root, backend)
        shutil.rmtree(root / "study")
        with patch.object(RUNNER, "BEST_COMMIT", "0" * 40):
            with self.assertRaisesRegex(ProTokError, "Retained baseline"):
                RUNNER.init_study(root, backend)
        self.assertFalse((root / "study").exists())

    def test_legacy_core_only_initialization_is_rejected_by_all_entry_points(self):
        import importlib.util

        root, backend, best = self.initialization_root()
        # Exact legacy interruption window: core initialized, adapter seeds not
        # written yet. Use real Git and core, not a fabricated ready state.
        RUNNER.initialize(
            root / "study", goal=RUNNER.GOAL, workspace=self.project, allow_write=True
        )
        before = self.study_snapshot(root)
        spec = importlib.util.spec_from_file_location(
            "readiness_server", Path(RUNNER.__file__).with_name("server.py")
        )
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
        lease = RUNNER.read(root / "control/continuation.json")
        RUNNER.write(
            root / "control/continuation.json",
            {
                **lease,
                "schema": 2,
                "seconds": 86400,
                "initial_gpu_seconds": 0,
                "additional_gpu_seconds": 86400,
            },
        )
        with patch.object(RUNNER, "BEST_COMMIT", best):
            for entry in (RUNNER.init_study, RUNNER.run_one):
                with self.assertRaisesRegex(ProTokError, "incomplete"):
                    entry(root, backend)
        with (
            patch.object(
                server.sys,
                "argv",
                ["server", "--root", str(root), "--runner", RUNNER.__file__],
            ),
            patch("integrations.protok.tasks.ExperimentScheduler") as scheduler,
            patch.object(server, "recover_owned") as recovery,
        ):
            with self.assertRaisesRegex(ProTokError, "incomplete"):
                server.main()
            scheduler.assert_not_called()
            recovery.assert_not_called()
        with patch.object(RUNNER, "run_one") as run:

            def stop(_):
                (root / "control/STOP").touch()

            self.assertEqual(
                server.drive(root, RUNNER, backend, wait=stop), "user_stop"
            )
            run.assert_not_called()
        self.assertIn(
            "incomplete", RUNNER.read(root / "control/supervisor-status.json")["error"]
        )
        self.assertEqual(before, self.study_snapshot(root))

    def test_completion_record_does_not_hide_missing_current_state(self):
        from integrations.protok import server

        root, backend, best = self.initialization_root()
        with patch.object(RUNNER, "BEST_COMMIT", best):
            RUNNER.init_study(root, backend)
            (root / "study/state.json").unlink()
            for entry in (RUNNER.init_study, RUNNER.run_one):
                with self.assertRaisesRegex(ProTokError, "incomplete"):
                    entry(root, backend)
        before = self.study_snapshot(root)
        with (
            patch.object(
                server.sys,
                "argv",
                ["server", "--root", str(root), "--runner", RUNNER.__file__],
            ),
            patch("integrations.protok.tasks.ExperimentScheduler") as scheduler,
            self.assertRaisesRegex(ProTokError, "incomplete"),
        ):
            server.main()
        scheduler.assert_not_called()
        lease = RUNNER.read(root / "control/continuation.json")
        RUNNER.write(
            root / "control/continuation.json", {**lease, "schema": 2, "seconds": 86400}
        )
        with (
            patch.object(RUNNER, "run_one") as run,
            patch.object(RUNNER, "repair_completed_control") as repair,
        ):
            server.drive(
                root, RUNNER, backend, wait=lambda _: (root / "control/STOP").touch()
            )
        run.assert_not_called()
        repair.assert_not_called()
        self.assertIn(
            "incomplete", RUNNER.read(root / "control/supervisor-status.json")["error"]
        )
        self.assertEqual(self.study_snapshot(root), before)

    def test_legacy_running_study_does_not_require_unchanged_seed_routes(self):
        from integrations.protok.initialization import COMPLETION

        root, backend, best = self.initialization_root()
        with patch.object(RUNNER, "BEST_COMMIT", best):
            RUNNER.init_study(root, backend)
            (root / "study" / COMPLETION).unlink()
            # Complete legacy zero-round seeds are sufficient without a marker.
            RUNNER.ensure_study_ready(root)
            harness = RecordedHarness()
            harness.stop_after = 10
            study = RUNNER.Study(root / "study", harness=harness)
            state = study.run(iterations=1)
            self.assertEqual(state["iteration"], 1)
            self.assertGreater(state["call_count"], 0)
            self.assertEqual(harness.count("review"), 1)
            # A real reviewed iteration remains evidence after seeds retire.
            RUNNER.write(root / "study/routes/index.json", {})
            shutil.rmtree(root / "study/routes/R0002")
            before = self.study_snapshot(root)
            self.assertEqual(RUNNER.init_study(root, backend), state)
            self.assertEqual(RUNNER.ensure_study_ready(root).status(), state)
            self.assertEqual(self.study_snapshot(root), before)
            self.assertFalse((root / "study" / COMPLETION).exists())

    def test_seed_write_interruption_is_not_published(self):
        root, backend, best = self.initialization_root()
        original = RUNNER.write

        def interrupt(path, value):
            if str(path).endswith("R0002/evaluation.json"):
                raise OSError("synthetic seed write interruption")
            return original(path, value)

        with patch.object(RUNNER, "BEST_COMMIT", best):
            with patch.object(RUNNER, "write", side_effect=interrupt):
                with self.assertRaisesRegex(OSError, "seed write"):
                    RUNNER.init_study(root, backend)
            self.assertFalse((root / "study").exists())
            RUNNER.init_study(root, backend)
            self.assertEqual(RUNNER.ensure_study_ready(root).status()["iteration"], 0)

    def test_initialization_preserves_an_unrecognized_study_directory(self):
        root, backend, best = self.initialization_root()
        (root / "study").mkdir(parents=True)
        (root / "study/notes.md").write_text("operator notes")
        with patch.object(RUNNER, "BEST_COMMIT", best):
            with self.assertRaisesRegex(ProTokError, "without study.json"):
                RUNNER.init_study(root, backend)
        self.assertTrue((root / "study/notes.md").exists())

    def test_initialization_rebuilds_an_interrupted_staging_study(self):
        root, backend, best = self.initialization_root()
        with patch.object(RUNNER, "BEST_COMMIT", best):
            RUNNER.init_study(root, backend)
            shutil.rmtree(root / "study")
            stale = root / ".initializing/study/routes"
            stale.mkdir(parents=True)
            (stale / "stale.json").write_text("{}")
            state = RUNNER.init_study(root, backend)
        self.assertEqual(state["status"], "ready")
        self.assertFalse((root / "study/routes/stale.json").exists())
        self.assertEqual(len(RUNNER.read(root / "study/routes/index.json")), 3)

    def test_initialization_does_not_rewrite_a_complete_study(self):
        root, backend, best = self.initialization_root()
        with patch.object(RUNNER, "BEST_COMMIT", best):
            RUNNER.init_study(root, backend)
            index = RUNNER.read(root / "study/routes/index.json")
            index["R0001"]["status"] = "paused"
            RUNNER.write(root / "study/routes/index.json", index)
            (root / "study/research.md").write_text(
                "Operator's evolving research notes.\n"
            )
            before = self.study_snapshot(root)
            state = RUNNER.init_study(root, backend)
            self.assertEqual(self.study_snapshot(root), before)
        self.assertEqual(state["status"], "ready")
        self.assertEqual(RUNNER.read(root / "study/routes/index.json"), index)

    def test_initialization_publishes_the_study_only_when_complete(self):
        root, backend, best = self.initialization_root()
        original = RUNNER.initialize

        def interrupt(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("crash after core initialization")

        with patch.object(RUNNER, "BEST_COMMIT", best):
            with patch.object(RUNNER, "initialize", side_effect=interrupt):
                with self.assertRaisesRegex(RuntimeError, "crash"):
                    RUNNER.init_study(root, backend)
            self.assertFalse((root / "study").exists())
            self.assertTrue((root / "baseline").is_dir())
            state = RUNNER.init_study(root, backend)
        self.assertEqual(state["iteration"], 0)
        self.assertTrue((root / "study/study.json").exists())

    def test_clone_interruption_retries_only_owned_unpublished_staging(self):
        root, backend, best = self.initialization_root()
        original = RUNNER.subprocess.run

        def interrupt(command, *args, **kwargs):
            if command[:2] == ["git", "clone"]:
                partial = Path(command[-1])
                partial.mkdir()
                (partial / "partial-clone").write_text("interrupted")
                raise RuntimeError("clone interrupted")
            return original(command, *args, **kwargs)

        with patch.object(RUNNER, "BEST_COMMIT", best):
            with patch.object(RUNNER.subprocess, "run", side_effect=interrupt):
                with self.assertRaisesRegex(RuntimeError, "clone interrupted"):
                    RUNNER.init_study(root, backend)
            self.assertFalse((root / "baseline").exists())
            self.assertFalse((root / "study").exists())
            RUNNER.init_study(root, backend)
        self.assertFalse((root / "baseline/partial-clone").exists())
        self.assertEqual(RUNNER.ensure_study_ready(root).status()["iteration"], 0)

    def test_final_rename_interruption_preserves_baseline_and_retries(self):
        from integrations.protok.initialization import COMPLETION

        root, backend, best = self.initialization_root()
        original = Path.rename

        def interrupt(path, target):
            if path == root / ".initializing/study":
                raise OSError("publication interrupted")
            return original(path, target)

        with patch.object(RUNNER, "BEST_COMMIT", best):
            with patch.object(Path, "rename", interrupt):
                with self.assertRaisesRegex(OSError, "publication interrupted"):
                    RUNNER.init_study(root, backend)
            self.assertFalse((root / "study").exists())
            self.assertTrue((root / ".initializing/study" / COMPLETION).exists())
            baseline_inode = (root / "baseline").stat().st_ino
            state = RUNNER.init_study(root, backend)
        self.assertEqual((root / "baseline").stat().st_ino, baseline_inode)
        self.assertEqual(state["iteration"], 0)

    def test_unrecognized_staging_is_preserved(self):
        root, backend, best = self.initialization_root()
        note = root / ".initializing/study/operator-notes.md"
        note.parent.mkdir(parents=True)
        note.write_text("Unknown retained evidence")
        with patch.object(RUNNER, "BEST_COMMIT", best):
            with self.assertRaisesRegex(ProTokError, "Unowned"):
                RUNNER.init_study(root, backend)
        self.assertEqual(note.read_text(), "Unknown retained evidence")
        self.assertFalse((root / "baseline").exists())

    def test_concurrent_initializer_cannot_remove_live_staging(self):
        from integrations.protok.initialization import initialization_lock

        root, backend, best = self.initialization_root()
        with patch.object(RUNNER, "BEST_COMMIT", best):
            with initialization_lock(root):
                staging = RUNNER._initialization_staging(root, "study")
                staging.mkdir()
                note = staging / "in-progress"
                note.write_text("live writer")
                with self.assertRaisesRegex(ProTokError, "initializer is active"):
                    RUNNER.init_study(root, backend)
                self.assertEqual(note.read_text(), "live writer")
            RUNNER.init_study(root, backend)
        RUNNER.ensure_study_ready(root)

    def test_initialization_rejects_symlink_targets_without_touching_them(self):
        root, backend, best = self.initialization_root()
        outside = self.directory / "outside"
        outside.mkdir()
        note = outside / "notes.md"
        note.write_text("Do not touch")
        with patch.object(RUNNER, "BEST_COMMIT", best):
            for relative in (
                "study",
                "baseline",
                ".initializing",
                "control/initialization.lock",
            ):
                with self.subTest(relative=relative):
                    link = root / relative
                    link.unlink(missing_ok=True)
                    link.symlink_to(outside, target_is_directory=True)
                    with self.assertRaises(ProTokError):
                        RUNNER.init_study(root, backend)
                    self.assertTrue(link.is_symlink())
                    self.assertEqual(note.read_text(), "Do not touch")
                    link.unlink()
            # The same protection applies inside an explicitly owned staging root.
            staging = RUNNER._initialization_staging(root, "study")
            staging.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ProTokError):
                RUNNER.init_study(root, backend)
            self.assertEqual(note.read_text(), "Do not touch")

    def test_marker_cannot_hide_missing_or_invalid_required_records(self):
        from integrations.protok.initialization import COMPLETION

        root, backend, best = self.initialization_root()
        with patch.object(RUNNER, "BEST_COMMIT", best):
            RUNNER.init_study(root, backend)
        for relative in (
            "study.json",
            "state.json",
            "routes/index.json",
            "frontiers.json",
            "goal.md",
            "research.md",
            "routes/R0002/evaluation.json",
        ):
            with self.subTest(relative=relative):
                path = root / "study" / relative
                content = path.read_bytes()
                path.unlink()
                with self.assertRaises(ProTokError):
                    RUNNER.ensure_study_ready(root)
                path.write_bytes(content)
        marker = root / "study" / COMPLETION
        original = RUNNER.read(marker)
        for changed in (
            {"version": 2},
            {"status": "initializing"},
            {"baseline": "0" * 40},
        ):
            RUNNER.write(marker, {**original, **changed})
            with self.assertRaises(ProTokError):
                RUNNER.ensure_study_ready(root)
        RUNNER.write(marker, original)
        state = root / "study/state.json"
        outside = self.directory / "retained-state.json"
        state.rename(outside)
        state.symlink_to(outside)
        with self.assertRaises(ProTokError):
            RUNNER.ensure_study_ready(root)
        self.assertTrue(outside.is_file())

    def test_legacy_iteration_counter_without_retained_evidence_is_rejected(self):
        from integrations.protok.initialization import COMPLETION

        root, backend, best = self.initialization_root()
        with patch.object(RUNNER, "BEST_COMMIT", best):
            RUNNER.init_study(root, backend)
        (root / "study" / COMPLETION).unlink()
        state = RUNNER.read(root / "study/state.json")
        RUNNER.write(root / "study/state.json", {**state, "iteration": 1})
        RUNNER.write(root / "study/iterations/0001/iteration.json", {"id": "0001"})
        before = self.study_snapshot(root)
        with self.assertRaises(ProTokError):
            RUNNER.ensure_study_ready(root)
        self.assertEqual(self.study_snapshot(root), before)

    def test_readiness_reads_only_bounded_controller_records(self):
        root, backend, best = self.initialization_root()
        with patch.object(RUNNER, "BEST_COMMIT", best):
            RUNNER.init_study(root, backend)
        # These paths must never be scanned or opened by readiness.
        (root / "study/checkpoints").symlink_to(self.project, target_is_directory=True)
        (root / "study/datasets").symlink_to(self.project, target_is_directory=True)
        opened = []
        original = Path.open

        def tracked(path, *args, **kwargs):
            opened.append(path.relative_to(root / "study").as_posix())
            return original(path, *args, **kwargs)

        with (
            patch.object(Path, "open", tracked),
            patch.object(Path, "rglob", side_effect=AssertionError("No scans")),
            patch.object(Path, "iterdir", side_effect=AssertionError("No scans")),
        ):
            RUNNER.ensure_study_ready(root)
        expected = {
            "study.json",
            "state.json",
            "routes/index.json",
            "frontiers.json",
            "goal.md",
            "research.md",
            "protok-initialization.json",
        }
        expected |= {
            f"routes/R000{i}/{name}"
            for i in (1, 2, 3)
            for name in ("research.md", "evaluation.json")
        }
        self.assertEqual(set(opened), expected)
        self.assertEqual(len(opened), len(expected) + 1)  # Study also reads config.

    def test_run_one_corrects_rejected_plan_and_publishes_import(self):
        from research_loop import initialize
        from research_loop.harness import HarnessConfig

        (self.root / "study/study.json").unlink()
        (self.root / "study").rmdir()
        baseline = self.root / "baseline"
        self.command(
            "git", "clone", "--shared", "--quiet", str(self.project), str(baseline)
        )
        initialize(
            self.root / "study",
            goal=RUNNER.GOAL,
            workspace=baseline,
            allow_write=True,
            harness=HarnessConfig(executable=RUNNER.CODEX, timeout_seconds=3600),
            max_iterations=None,
            max_calls=None,
            global_evaluation={
                "id": PROTOCOL,
                "comparison_scope": PROTOCOL,
                "protocol": "Fixed development protocol with explicit historical import.",
                "data_role": "development",
                "allow_feedback": True,
                "objectives": [
                    {"name": METRIC, "direction": "minimize", "unit": "Angstrom"}
                ],
                "constraints": [],
            },
        )

        self.seed_synthetic_initialization()

        class InterfaceHarness(RecordedHarness):
            bad_plan = True
            reject_locally = False

            def response(self, phase, context, kwargs):
                if phase == "plan" and self.bad_plan:
                    self.bad_plan = False
                    result = super().response(phase, context, kwargs)
                    result["references"] = ["research.md"]
                    return result
                if phase != "execute":
                    result = super().response(phase, context, kwargs)
                    if (
                        phase == "review"
                        and context["execute"]["outcome"] == "incomplete"
                    ):
                        failure = context["execute"]["markdown"]
                        result["knowledge_changes"] = [failure]
                        result["route_research"] += " " + failure
                        result["global_research"] += " " + failure
                    return result
                workspace = Path(kwargs["workspace"])
                if self.reject_locally:
                    (workspace / CANDIDATE_FILES[0]).write_text(
                        "def rejected_fixture(:\n"
                    )
                    return {
                        "outcome": "incomplete",
                        "artifacts": [],
                        "measurements": [],
                        "markdown": "Fixture static syntax precheck failed; no measurement or GPU request; attribution unresolved.",
                    }
                RUNNER.write(
                    workspace / "evaluation-request.json", {"action": "import"}
                )
                until = time.monotonic() + 5
                while not (workspace / "evaluation-result.json").exists():
                    if time.monotonic() >= until:
                        raise AssertionError(
                            "Fixed bridge did not publish its import result"
                        )
                    time.sleep(0.01)
                receipt = RUNNER.read(workspace / "evaluation-result.json")
                if receipt["status"] != "completed":
                    raise AssertionError(receipt)
                return {
                    "outcome": "completed",
                    "markdown": "Audited historical best; no new GPU evidence.",
                    "artifacts": [receipt["artifact"]],
                    "measurements": [
                        {
                            "candidate_id": receipt["fingerprint"],
                            "artifact": receipt["artifact"],
                            "evaluation_id": PROTOCOL,
                            "comparison_scope": PROTOCOL,
                            "metrics": [{"name": METRIC, "value": receipt["value"]}],
                            "evidence": receipt["evidence"],
                        }
                    ],
                }

        delegate = InterfaceHarness()
        delegate.stop_after = 3
        with patch.object(RUNNER, "CodexHarness", return_value=delegate):
            state = RUNNER.run_one(self.root, self.backend)
        plans = [call for call in delegate.calls if call["phase"] == "plan"]
        rejection = RUNNER.read(plans[0]["run_dir"] / "rejection.json")
        self.assertIn("references", json.dumps(rejection["errors"]))
        self.assertIn("previous reply was rejected", plans[1]["prompt"])
        self.assertEqual(plans[0]["context"], plans[1]["context"])
        self.assertEqual(delegate.count("proposal"), 2)
        self.assertEqual(delegate.count("judge"), 1)
        self.assertEqual(delegate.count("plan"), 2)
        self.assertEqual(state["phase"], "done")
        self.assertEqual(delegate.count("execute"), 1)
        self.assertEqual(delegate.count("review"), 1)
        self.assertEqual(self.backend.evaluations, 0)
        self.assertTrue(
            (self.root / "study/iterations/0001/publication.json").is_file()
        )
        best = RUNNER.read(self.control / "active-best.json")
        self.assertEqual(best["metrics"][METRIC], RUNNER.BEST_VALUE)
        self.assertEqual(best["artifact"]["version"], RUNNER.BEST_FINGERPRINT)
        self.assertTrue(best["workspace_ref"])
        self.assertEqual(
            RUNNER.read(self.control / "runner.json")["status"], "completed"
        )
        self.assertEqual(
            len((self.control / "journal.jsonl").read_text().splitlines()), 1
        )
        RUNNER.publish_control(
            self.root, RUNNER.Study(self.root / "study"), self.backend
        )
        self.assertEqual(
            len((self.control / "journal.jsonl").read_text().splitlines()), 1
        )
        self.assertEqual(RUNNER.git(self.project, "status", "--porcelain"), "")

        # A new local precheck rejection reaches ordinary Review without any allocation.
        delegate.base = "0001"
        delegate.reject_locally = True
        with patch.object(RUNNER, "CodexHarness", return_value=delegate):
            second = RUNNER.run_one(self.root, self.backend)
        self.assertEqual((second["iteration"], second["status"]), (2, "ready"))
        review = RUNNER.read(self.root / "study" / second["review_ref"])
        self.assertEqual(review["candidate_reviews"], [])
        self.assertIn("precheck failed", review["knowledge_changes"][0])
        self.assertIn(
            "attribution unresolved", (self.root / "study/research.md").read_text()
        )
        self.assertFalse((self.control / "jobs/0002").exists())
        self.assertEqual(
            RUNNER.read(self.control / "active-best.json")["id"], best["id"]
        )
        self.assertEqual(self.backend.evaluations, 0)

    def test_unsubmitted_interruption_preserves_plan_then_retries_one_evaluation(self):
        from research_loop.harness import completed_response

        (self.project / "autoresearch-report.md").write_text("Historical best.\n")
        RUNNER.git(self.project, "add", "--", "autoresearch-report.md")
        RUNNER.git(
            self.project,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "-m",
            "Historical report",
        )
        (self.root / "study/study.json").unlink()
        with patch.object(
            RUNNER, "BEST_COMMIT", RUNNER.git(self.project, "rev-parse", "HEAD")
        ):
            RUNNER.init_study(self.root, self.backend)

        class InterruptedHarness(RecordedHarness):
            interrupt = True

            def response(self, phase, context, kwargs):
                if phase != "execute":
                    return super().response(phase, context, kwargs)
                workspace = Path(kwargs["workspace"])
                iteration = workspace.parent.name
                if iteration == "0002":
                    (workspace / CANDIDATE_FILES[0]).write_text(
                        "# valid intervention\n"
                    )
                    if self.interrupt:
                        self.interrupt = False
                        (kwargs["run_dir"] / "partial.json").write_text(
                            '{"error":"linkat failed with ENOENT before publication"}'
                        )
                        raise HarnessError("CLI interrupted before submission")
                submit(
                    workspace, action="import" if iteration == "0001" else "evaluate"
                )
                until = time.monotonic() + 5
                while submission_status(workspace)["status"] not in {
                    "completed",
                    "failed",
                }:
                    if time.monotonic() >= until:
                        raise AssertionError("No controlled backend result")
                    time.sleep(0.01)
                receipt = submit(
                    workspace, action="import" if iteration == "0001" else "evaluate"
                )["result"]
                if receipt["status"] != "completed":
                    raise AssertionError(receipt)
                return {
                    "outcome": "completed",
                    "markdown": "Measured through the controlled submission API.",
                    "artifacts": [receipt["artifact"]],
                    "measurements": [
                        {
                            "candidate_id": receipt["fingerprint"],
                            "artifact": receipt["artifact"],
                            "evaluation_id": PROTOCOL,
                            "comparison_scope": PROTOCOL,
                            "metrics": [{"name": METRIC, "value": receipt["value"]}],
                            "evidence": receipt["evidence"],
                        }
                    ],
                }

        delegate = InterruptedHarness()
        delegate.stop_after = 2
        study = RUNNER.Study(self.root / "study")
        with patch.object(RUNNER, "CodexHarness", return_value=delegate):
            RUNNER.run_one(self.root, self.backend)
            delegate.base = "0001"
            with self.assertRaisesRegex(HarnessError, "before submission"):
                RUNNER.run_one(self.root, self.backend)
            paused = study.status()
            self.assertEqual(
                (paused["iteration"], paused["phase"], paused["status"]),
                (2, "execute", "needs_attention"),
            )
            attempt = study.root / paused["calls"]["execute"]
            self.assertIsNone(completed_response(attempt / "agent"))
            self.assertIsNone(completed_response(attempt))
            raw_bytes = (attempt / "agent/partial.json").read_bytes()
            plan_path = study.root / paused["plan_ref"]
            plan_bytes = plan_path.read_bytes()
            workspace = Path(
                RUNNER.read(study.root / "iterations/0002/iteration.json")["workspace"]
            )
            self.assertEqual(submission_status(workspace)["status"], "not_submitted")
            self.assertFalse((self.control / "jobs/0002").exists())
            self.assertFalse((study.root / "iterations/0002/publication.json").exists())
            self.assertEqual(delegate.count("review"), 1)
            self.assertEqual(self.backend.evaluations, 0)
            self.assertEqual(self.backend.inspect()["outstanding"], [])
            plan_count = delegate.count("plan")
            retried = study.retry()
            self.assertEqual((retried["iteration"], retried["phase"]), (2, "execute"))
            self.assertNotIn("execute", retried["calls"])
            completed = RUNNER.run_one(self.root, self.backend)
        self.assertEqual((completed["iteration"], completed["phase"]), (2, "done"))
        self.assertEqual(delegate.count("plan"), plan_count)
        self.assertEqual(delegate.count("review"), 2)
        self.assertEqual(self.backend.evaluations, 1)
        self.assertEqual(plan_path.read_bytes(), plan_bytes)
        self.assertEqual((attempt / "agent/partial.json").read_bytes(), raw_bytes)
        self.assertTrue((study.root / "iterations/0002/publication.json").exists())


if __name__ == "__main__":
    unittest.main()
