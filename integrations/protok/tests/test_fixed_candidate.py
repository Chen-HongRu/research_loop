"""Fixed manual candidates across settled failure, restart and publication boundaries."""

import threading
import time
import unittest
from unittest.mock import Mock, patch

from integrations.protok import run
from integrations.protok.backend import CANDIDATE_FILES, ProTokError
from research_loop.harness import HarnessError
from research_loop import Study, initialize
from research_loop.tasks import TaskQueue
from research_loop.submission import submit
from test_core import RecordedHarness
import test_debug_flow
import test_protok_runner


class FixedFailureTests(unittest.TestCase):
    invoke = test_debug_flow.DebugFlowTests.invoke
    recover = test_debug_flow.DebugFlowTests.recover

    def setUp(self):
        test_debug_flow.DebugFlowTests.setUp(self)
        self.baseline = run.git(self.workspace, "rev-parse", "HEAD")
        run.write(
            self.workspace.parent / "context/task.json",
            {"allow_changes": False, "source": {"revision": self.baseline}},
        )
        self.job = self.root / "control/jobs/0009"
        run.freeze_source(
            self.project, self.workspace, self.job / "source", self.baseline
        )
        run.write(
            self.job / "job.json",
            {
                "iteration": "0009",
                "request": {"action": "evaluate"},
                "status": "submitted",
                "source_signatures": self.failure["source_signatures"],
                "source_commit": self.baseline,
                "fingerprint": self.failure["fingerprint"],
                "execution_deadline": time.time() + 120,
            },
        )
        self.backend.settled_failure = Mock(
            return_value={
                "kind": "candidate_rejected",
                "cause": self.failure["cause"],
                "fingerprint": self.failure["fingerprint"],
                "purpose": "formal",
                "gpu_seconds": 39,
                "quality_score_eligible": False,
                "diagnostics": {"owner": "workspace", "logs": ["original.log"]},
                "evidence": ["original-terminal.log"],
            }
        )
        self.failure = run.certify_candidate_failure(
            self.root, self.backend, self.workspace, "0009"
        )
        self.validator = run.ProTokHarness(
            self.backend, self.root, self.delegate, self.harness.deadline
        )

    def assert_no_repair(self):
        self.assertEqual(self.delegate.calls, [])
        self.assertEqual(self.invocations, [])
        self.assertEqual(self.backend.smoke_calls, [])
        self.assertEqual(self.backend.formal_calls, [])

    def test_fixed_failure_with_and_without_gpu_grant_never_repairs(self):
        for grant in (True, False):
            with self.subTest(grant=grant):
                if not grant:
                    # A new invocation of the fixture supplies a separate ledger.
                    self.setUp()
                    (self.root / "control/debug-policy.json").unlink()
                original = run.candidate_bytes(self.workspace)
                result = self.recover()
                self.assertEqual(result["outcome"], "incomplete")
                self.assertEqual(result["measurements"], [])
                self.assertIn("39 seconds", result["markdown"])
                self.assertIn("separate task", result["markdown"])
                self.assertEqual(run.candidate_bytes(self.workspace), original)
                saved = run.read(self.root / "control/debug/0009/decision.json")
                self.assertEqual(saved["failures"], [self.failure])
                # Restart may read existing receipts after expiry; no new work.
                (self.root / "control/STOP").write_text("stop")
                self.assertEqual(self.recover(), result)
                self.assertEqual(
                    self.validator.validate_completed(
                        result, workspace=self.workspace, run_dir=None
                    ),
                    result,
                )
                self.assert_no_repair()

    def test_changed_workspace_and_job_together_cannot_be_certified_as_A(self):
        for source in (self.workspace, self.job / "source"):
            path = source / CANDIDATE_FILES[0]
            path.write_text(path.read_text() + "# B\n")
        with self.assertRaisesRegex(ProTokError, "Fixed-candidate"):
            self.recover()
        self.assertIn("# B", (self.workspace / CANDIDATE_FILES[0]).read_text())
        self.assert_no_repair()

    def test_allow_changes_true_retains_repair_smoke_and_formal_flow(self):
        path = self.workspace.parent / "context/task.json"
        run.write(path, run.read(path) | {"allow_changes": True})
        self.assertEqual(self.recover()["outcome"], "completed")
        self.assertEqual(len(self.delegate.calls), 2)
        self.assertEqual(len(self.backend.smoke_calls), 2)
        self.assertEqual(len(self.backend.formal_calls), 1)

    def test_restoring_workspace_does_not_accept_changed_job_snapshot(self):
        path = self.job / "source" / CANDIDATE_FILES[0]
        path.write_text(path.read_text() + "# B evaluated\n")
        with self.assertRaisesRegex(ProTokError, "Fixed-candidate"):
            self.validator.validate_completed(
                {
                    "outcome": "incomplete",
                    "markdown": "old result",
                    "artifacts": [],
                    "measurements": [],
                },
                workspace=self.workspace,
                run_dir=None,
            )
        self.assertIn("# B evaluated", path.read_text())
        self.assert_no_repair()

    def test_cached_decision_still_requires_original_accounting(self):
        result = self.recover()
        self.backend.settled_failure.side_effect = ProTokError(
            "unknown allocation cost"
        )
        for resume in (
            self.recover,
            lambda: self.validator.validate_completed(
                result, workspace=self.workspace, run_dir=None
            ),
        ):
            with self.assertRaisesRegex(ProTokError, "unknown allocation cost"):
                resume()
        self.assert_no_repair()

    def test_unknown_effects_do_not_publish_zero_cost_failure(self):
        self.backend.inspect = lambda: {"outstanding": ["original-job"]}
        with self.assertRaisesRegex(HarnessError, "unresolved"):
            self.recover()
        self.assertFalse((self.root / "control/debug/0009/decision.json").exists())
        self.assert_no_repair()

    def test_old_modified_recovery_pointer_is_blocked_without_replay(self):
        repaired = self.root / "control/debug/0009/formal-001"
        run.freeze_source(
            self.project, self.workspace, repaired / "source", self.baseline
        )
        path = repaired / "source" / CANDIDATE_FILES[0]
        path.write_text(path.read_text() + "# historical B\n")
        run.write(repaired / "job.json", run.read(self.job / "job.json"))
        run.write(self.job / "recovery.json", {"job_directory": str(repaired)})
        with self.assertRaisesRegex(ProTokError, "Fixed-candidate"):
            self.recover()
        self.assert_no_repair()

    def test_same_candidate_replacement_job_is_also_blocked(self):
        repaired = self.root / "control/debug/0009/formal-001"
        run.freeze_source(
            self.project, self.workspace, repaired / "source", self.baseline
        )
        run.write(repaired / "job.json", run.read(self.job / "job.json"))
        run.write(self.job / "recovery.json", {"job_directory": str(repaired)})
        with self.assertRaisesRegex(ProTokError, "replacement job"):
            self.recover()
        with self.assertRaisesRegex(ProTokError, "replacement job"):
            run.completed_job(self.root, self.backend, self.workspace, "0009")
        self.assert_no_repair()

    def test_interrupted_old_repair_is_not_published_after_expiry(self):
        self.recover()
        debug = self.root / "control/debug/0009"
        (debug / "decision.json").unlink()
        flow = run.read(debug / "flow.json")
        run.write(debug / "flow.json", flow | {"phase": "repair"})
        attempt = (
            self.workspace.parent
            / "execute/recovery/repairs/repair-001-try-001/attempt-001"
        )
        run.write(attempt / "process.json", {"status": "interrupted"})
        with self.assertRaisesRegex(HarnessError, "Interrupted repair"):
            self.recover()
        self.assertFalse((debug / "decision.json").exists())
        self.assert_no_repair()


class FixedEvaluationTests(unittest.TestCase):
    command = test_protok_runner.ProTokRunnerTests.command

    def setUp(self):
        test_protok_runner.ProTokRunnerTests.setUp(self)
        self.backend.initial = {}  # Exercise new formal receipts rather than historical import.

    def fixed(self):
        run.write(
            self.root / "study/iterations/0002/context/task.json",
            {"allow_changes": False, "source": {"revision": self.baseline}},
        )

    def evaluate(self):
        return self.harness.evaluate_request(
            self.workspace,
            "0002",
            {"action": "evaluate"},
            threading.Event(),
            time.time() + 120,
        )

    def test_transport_and_outputs_remain_writable_for_fixed_candidate(self):
        self.fixed()
        run.write(self.workspace / "evaluation-submission.json", {"action": "evaluate"})
        self.assertEqual(self.evaluate()["status"], "completed")
        self.assertEqual(self.backend.evaluations, 1)
        self.assertEqual(self.evaluate()["status"], "completed")
        self.assertEqual(self.backend.evaluations, 1)

    def test_ordinary_evaluation_blocks_changed_candidate_before_submit(self):
        self.fixed()
        path = self.workspace / CANDIDATE_FILES[0]
        path.write_text(path.read_text() + "# B\n")
        with self.assertRaisesRegex(ProTokError, "Fixed-candidate"):
            self.evaluate()
        self.assertEqual(self.backend.evaluations, 0)

    def test_completed_job_cannot_reuse_B_as_A_even_when_snapshots_match(self):
        path = self.workspace / CANDIDATE_FILES[0]
        path.write_text(path.read_text() + "# B\n")
        self.evaluate()  # Historical evaluation occurred before protection was enforced.
        self.fixed()
        with self.assertRaisesRegex(ProTokError, "Fixed-candidate"):
            run.completed_job(self.root, self.backend, self.workspace, "0002")
        self.assertEqual(self.backend.evaluations, 1)

    def test_source_change_during_evaluation_is_not_accepted_after_workspace_restore(
        self,
    ):
        self.fixed()
        evaluate = self.backend.evaluate

        def changed_source(source, *args, **kwargs):
            path = source / CANDIDATE_FILES[0]
            path.write_text(path.read_text() + "# evaluated B\n")
            return evaluate(source, *args, **kwargs)

        with patch.object(self.backend, "evaluate", side_effect=changed_source):
            with self.assertRaisesRegex(ProTokError, "Fixed-candidate"):
                self.evaluate()
        self.assertFalse((self.root / "control/jobs/0002/result.json").exists())

    def test_submitted_unknown_job_is_not_replayed(self):
        self.fixed()
        self.evaluate()
        job = self.root / "control/jobs/0002"
        (job / "result.json").unlink()
        run.write(
            job / "job.json", run.read(job / "job.json") | {"status": "submitted"}
        )
        with self.assertRaisesRegex(ProTokError, "unknown effects"):
            self.evaluate()
        self.assertEqual(self.backend.evaluations, 1)

    def test_real_study_fixed_failure_reaches_independent_review_without_repair(self):
        (self.root / "study/study.json").unlink()
        (self.root / "study").rmdir()
        initialize(
            self.root / "study",
            goal="Measure the requested fixed candidate",
            workspace=self.project,
            allow_write=True,
            max_iterations=None,
            max_calls=None,
        )
        initial = RecordedHarness()
        initial.stop_after = 99
        Study(self.root / "study", initial).run(iterations=1)
        queue = TaskQueue(self.root / "study")
        request = queue.submit(
            {
                "id": "fixed-failure",
                "instruction": "Evaluate exactly A",
                "allow_changes": False,
            }
        )

        class Submitter(RecordedHarness):
            def response(delegate, phase, context, kwargs):
                if phase != "execute":
                    return super().response(phase, context, kwargs)
                self.assertTrue(kwargs["writable"])
                workspace = kwargs["workspace"]
                submit(workspace, "evaluate")
                until = time.monotonic() + 5
                while not (workspace / "evaluation-result.json").exists():
                    if time.monotonic() >= until:
                        raise AssertionError("No settled controller failure")
                    time.sleep(0.01)
                return {
                    "outcome": "incomplete",
                    "markdown": "Candidate failed",
                    "artifacts": [],
                    "measurements": [],
                }

        self.backend.evaluate = Mock(side_effect=ProTokError("actual candidate failed"))
        self.backend.settled_failure = Mock(
            side_effect=lambda source, config, fingerprint: {
                "kind": "candidate_rejected",
                "purpose": "formal",
                "fingerprint": fingerprint,
                "cause": "RuntimeError: actual candidate failed",
                "gpu_seconds": 39,
                "quality_score_eligible": False,
                "diagnostics": {"owner": "workspace", "logs": ["original.log"]},
                "evidence": [str(config)],
            }
        )
        delegate = Submitter()
        harness = run.ProTokHarness(self.backend, self.root, delegate, self.deadline)
        state = Study(self.root / "study", harness).run(iterations=1)
        self.assertEqual(state["phase"], "done")
        self.assertEqual(delegate.count("execute"), 1)
        self.assertEqual(delegate.count("review"), 1)
        self.backend.evaluate.assert_called_once()
        result = run.read(self.root / "study" / state["execution_ref"])
        self.assertEqual(
            (result["outcome"], result["measurements"]), ("incomplete", [])
        )
        self.assertIn("39 seconds", result["markdown"])
        self.assertEqual(queue.snapshot()[0]["status"], "reviewed")
        workspace = self.root / "study/iterations/0002/workspace"
        self.assertEqual(
            run.candidate_bytes(workspace), run.candidate_bytes(self.project)
        )
        self.assertEqual(request["source"]["revision"], self.baseline)
