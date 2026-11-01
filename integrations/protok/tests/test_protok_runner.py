import hashlib
import importlib.util
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from integrations.protok.backend import CANDIDATE_FILES, METRIC, PROTOCOL, ProTokError
from research_loop.harness import HarnessError
from research_loop.submission import (
    RECEIPT,
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

        def execute(root, backend):
            current = RUNNER.read(self.control / "status.json")
            self.assertEqual(current["status"], "running")
            self.assertEqual(current["phase"], "execute")
            self.assertNotIn("error", current)
            return state

        with patch.object(RUNNER, "_run_one", side_effect=execute):
            self.assertEqual(RUNNER.run_one(self.root, self.backend), state)

    def request(self, action="import", iteration="0001"):
        return self.harness.evaluate_request(
            self.workspace, iteration, {"action": action}, self.stop, self.deadline
        )

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
                self.assertTrue(forwarded.endswith(path.read_text()))
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
        RUNNER.local_incomplete(
            self.root, self.backend, self.workspace, "0002", response
        )
        self.assertEqual(self.backend.evaluations, 0)

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
            receipt = submit(workspace)["result"]
            return {
                "outcome": "completed",
                "markdown": "Measured using reusable API.",
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

    def test_delegate_success_is_not_wrapper_completion_when_metric_check_fails(self):
        from research_loop.harness import completed_response

        workspace = self.root / "study/iterations/0001/workspace"
        workspace.parent.mkdir(parents=True)
        self.command(
            "git", "clone", "--shared", "--quiet", str(self.project), str(workspace)
        )
        outer = self.directory / "attempt-001"
        outer.mkdir()

        def delegate_run(**kwargs):
            agent = kwargs["run_dir"]
            RUNNER.write(workspace / "evaluation-request.json", {"action": "import"})
            until = time.monotonic() + 5
            while not (workspace / "evaluation-result.json").exists():
                if time.monotonic() >= until:
                    raise AssertionError("No backend result")
                time.sleep(0.01)
            receipt = RUNNER.read(workspace / "evaluation-result.json")
            result = {
                "outcome": "completed",
                "markdown": "Incorrect agent claim.",
                "artifacts": [receipt["artifact"]],
                "measurements": [
                    {
                        "candidate_id": receipt["fingerprint"],
                        "artifact": receipt["artifact"],
                        "evaluation_id": PROTOCOL,
                        "comparison_scope": PROTOCOL,
                        "metrics": [{"name": METRIC, "value": 0.001}],
                        "evidence": receipt["evidence"],
                    }
                ],
            }
            RUNNER.write(agent / "response.json", result)
            RUNNER.write(agent / "process.json", {"status": "succeeded"})
            return result

        delegate = Mock()
        delegate.run.side_effect = delegate_run
        harness = RUNNER.ProTokHarness(self.backend, self.root, delegate, self.deadline)
        with self.assertRaisesRegex(HarnessError, "does not match"):
            harness.run(
                prompt="Execute",
                writable=True,
                workspace=workspace,
                run_dir=outer,
                timeout_seconds=60,
            )
        self.assertIsNone(completed_response(outer))
        raw = completed_response(outer / "agent")
        self.assertIsNotNone(raw)
        with self.assertRaisesRegex(HarnessError, "does not match"):
            harness.validate_completed(
                raw, workspace=workspace, run_dir=outer / "agent"
            )
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

    def test_interrupted_execute_reconciles_as_new_operator_attempt_then_independent_review(
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
        repaired = RUNNER.reconcile_completed_execute(self.root, self.backend)
        replacement = study.root / repaired["calls"]["execute"]
        self.assertNotEqual(partial, replacement)
        self.assertIn(
            "Operator reconciliation", completed_response(replacement)["markdown"]
        )
        self.assertFalse((partial / "process.json").exists())
        self.assertEqual((partial / "agent/partial.json").read_bytes(), partial_bytes)
        self.assertEqual(repaired["call_count"], before["call_count"])
        self.assertEqual(repaired["elapsed_seconds"], before["elapsed_seconds"])
        self.assertEqual(len(delegate.calls), calls)
        self.assertTrue((self.control / "STOP").exists())
        (self.control / "STOP").unlink()
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

    def test_run_one_resumes_explicitly_retried_plan_and_publishes_import(self):
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
                        "# rejected fixture candidate\n"
                    )
                    return {
                        "outcome": "incomplete",
                        "artifacts": [],
                        "measurements": [],
                        "markdown": "Fixture tiny-gradient precheck failed; no measurement or GPU request; attribution unresolved.",
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
            with self.assertRaisesRegex(ValueError, "reference format"):
                RUNNER.run_one(self.root, self.backend)
            interrupted = RUNNER.Study(self.root / "study")
            self.assertEqual(interrupted.status()["status"], "needs_attention")
            self.assertEqual(interrupted.status()["phase"], "plan")
            self.assertEqual(delegate.count("execute"), 0)
            self.assertEqual(self.backend.evaluations, 0)
            self.assertTrue((self.control / "STOP").exists())
            # Operator reconciliation is explicit; the runner performs neither action.
            retried = interrupted.retry()
            self.assertEqual((retried["phase"], retried["status"]), ("plan", "ready"))
            (self.control / "STOP").unlink()
            state = RUNNER.run_one(self.root, self.backend)
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

        # Reproduce the old wrapper rejection, then accept only its known local receipt.
        current_run = RUNNER.ProTokHarness.run

        def legacy_run(harness, **kwargs):
            if not kwargs["writable"]:
                return current_run(harness, **kwargs)
            for name in RUNNER.INTERFACE_FILES:
                (Path(kwargs["workspace"]) / name).unlink(missing_ok=True)
            harness.delegate.run(**kwargs)
            raise HarnessError("Execute ended without a completed trusted evaluation")

        with (
            patch.object(RUNNER, "CodexHarness", return_value=delegate),
            patch.object(RUNNER.ProTokHarness, "run", legacy_run),
        ):
            with self.assertRaisesRegex(HarnessError, "without a completed trusted"):
                RUNNER.run_one(self.root, self.backend)
        before = RUNNER.Study(self.root / "study").status()
        count = delegate.count("execute")
        attempt = self.root / "study" / before["calls"]["execute"]
        process = RUNNER.read(attempt / "process.json")
        RUNNER.write(attempt / "process.json", {**process, "status": "failed"})
        with self.assertRaisesRegex(ProTokError, "successful process receipt"):
            RUNNER.reconcile_local_incomplete(self.root, self.backend)
        RUNNER.write(attempt / "process.json", process)
        with patch.object(
            self.backend,
            "inspect",
            return_value={"outstanding": [{"record": "unknown"}]},
        ):
            with self.assertRaisesRegex(ProTokError, "Outstanding backend effects"):
                RUNNER.reconcile_local_incomplete(self.root, self.backend)
        resumed = RUNNER.reconcile_local_incomplete(self.root, self.backend)
        self.assertEqual((resumed["phase"], resumed["status"]), ("execute", "ready"))
        self.assertEqual(resumed["call_count"], before["call_count"])
        self.assertEqual(resumed["elapsed_seconds"], before["elapsed_seconds"])
        self.assertEqual(resumed["calls"]["execute"], before["calls"]["execute"])
        self.assertTrue((self.control / "STOP").exists())
        self.assertTrue(list((self.control / "reconciliations").glob("*.json")))
        (
            self.control / "STOP"
        ).unlink()  # Explicit operator action in this fixture only.
        with patch.object(RUNNER, "CodexHarness", return_value=delegate):
            third = RUNNER.run_one(self.root, self.backend)
        self.assertEqual(third["iteration"], 3)
        self.assertEqual(delegate.count("execute"), count)
        self.assertEqual(delegate.count("review"), 3)
        self.assertEqual(self.backend.evaluations, 0)
        self.assertEqual(
            RUNNER.read(self.control / "active-best.json")["id"], best["id"]
        )
        self.assertEqual(
            [node["status"] for node in RUNNER.Study(self.root / "study").graph()],
            ["reviewed"] * 3,
        )


if __name__ == "__main__":
    unittest.main()
