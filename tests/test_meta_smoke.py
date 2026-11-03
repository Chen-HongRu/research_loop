import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from research_loop import Study, StudyError, initialize
from test_core import RecordedHarness, read

DIRECTORY = Path(__file__).resolve().parents[1] / "benchmarks/cactus"
SPEC = importlib.util.spec_from_file_location("meta_inner", DIRECTORY / "meta_inner.py")
INNER = importlib.util.module_from_spec(SPEC)
sys.modules["meta_inner"] = INNER
SPEC.loader.exec_module(INNER)
RUNNER_SPEC = importlib.util.spec_from_file_location(
    "run_meta_smoke", DIRECTORY / "run_meta_smoke.py"
)
RUNNER = importlib.util.module_from_spec(RUNNER_SPEC)
RUNNER_SPEC.loader.exec_module(RUNNER)


class MetaSmokeTests(unittest.TestCase):
    def test_meta_bridge_constructs_ratio_from_pair_without_author_copying(self):
        from benchmarks.cactus import bridge

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            harness = RUNNER.MetaRequestHarness.__new__(RUNNER.MetaRequestHarness)
            harness.args = SimpleNamespace()
            harness.errors = []
            harness.result = {
                "candidate": {"ref": "actual-version", "parent_ref": "actual-parent"},
                "report": {
                    "rows": [
                        {"arm": "S", "overhead_seconds": 4},
                        {"arm": "C", "overhead_seconds": 3},
                    ]
                },
            }
            harness._watch = lambda workspace, stop: stop.wait()
            harness.delegate = Mock()
            raw = {"markdown": "Measured pair evidence."}
            harness.delegate.run.return_value = raw
            with patch.dict(sys.modules, {"bridge": bridge}):
                result = harness.run(
                    prompt="execute",
                    writable=True,
                    workspace=root,
                    run_dir=root,
                    timeout_seconds=2,
                )
            self.assertEqual(result["outcome"], "completed")
            self.assertEqual(result["artifacts"][0]["version"], "actual-version")
            measurement = result["measurements"][0]
            self.assertEqual(measurement["comparison_scope"], "actual-parent")
            self.assertEqual(
                measurement["metrics"], [{"name": "mean_rO", "value": 0.75}]
            )
            self.assertEqual(read(root / "agent-report.json"), raw)
            self.assertEqual(
                harness.delegate.run.call_args.kwargs["schema"], bridge.REPORT_SCHEMA
            )
            harness.delegate.run.assert_called_once()

    def test_meta_worker_crash_blocks_retained_completed_pair(self):
        from benchmarks.cactus import bridge

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            harness = RUNNER.MetaRequestHarness.__new__(RUNNER.MetaRequestHarness)
            harness.args = SimpleNamespace()
            harness.errors = []
            harness.result = {"retained": "completed pair"}

            def watch(workspace, stop):
                stop.wait()
                raise RuntimeError("unexpected evaluation worker failure")

            harness._watch = watch
            harness.delegate = Mock()
            harness.delegate.run.return_value = {
                "markdown": "report",
            }
            with patch.dict(sys.modules, {"bridge": bridge}):
                with self.assertRaisesRegex(
                    Exception, "effects require reconciliation"
                ):
                    harness.run(
                        prompt="execute",
                        writable=True,
                        workspace=root,
                        run_dir=root,
                        timeout_seconds=2,
                    )
            self.assertFalse((root / "completed.json").exists())
            harness.delegate.run.assert_called_once()

    def test_meta_live_worker_or_remote_error_blocks_completion(self):
        from benchmarks.cactus import bridge

        for alive, errors in [(True, []), (False, ["remote effects unknown"])]:
            with (
                self.subTest(alive=alive, errors=errors),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                harness = RUNNER.MetaRequestHarness.__new__(RUNNER.MetaRequestHarness)
                harness.args = SimpleNamespace()
                harness.errors = list(errors)
                harness.result = {"retained": "completed pair"}
                harness.delegate = Mock()
                harness.delegate.run.return_value = {"markdown": "report"}
                worker = Mock()
                worker.is_alive.return_value = alive
                with (
                    patch.dict(sys.modules, {"bridge": bridge}),
                    patch.object(RUNNER.threading, "Thread", return_value=worker),
                ):
                    with self.assertRaisesRegex(Exception, "reconciliation required"):
                        harness.run(
                            prompt="execute",
                            writable=True,
                            workspace=root,
                            run_dir=root,
                            timeout_seconds=1,
                        )
                self.assertFalse((root / "completed.json").exists())
                harness.delegate.run.assert_called_once()

    def recovery_fixture(self, receipt_status="succeeded", malformed_reviews=1):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "study"
        initialize(
            root,
            goal="One bounded canary with no training or measurements.",
            strategy={"selection": "direct", "review": "concise"},
            max_calls=12,
        )
        harness = RecordedHarness()
        response = harness.response

        def malformed_first_review(phase, context, kwargs):
            result = response(phase, context, kwargs)
            if phase == "review" and harness.review_count <= malformed_reviews:
                result["candidate_reviews"] = [
                    {
                        "candidate_id": "unmeasured-artifact",
                        "valid": True,
                        "verified": True,
                        "reason": "Artifact exists, but it has no measurement.",
                    }
                ]
            return result

        def change_receipt(call):
            if call["phase"] == "review" and harness.review_count == 1:
                receipt = call["run_dir"] / "process.json"
                if receipt_status is None:
                    receipt.unlink()
                else:
                    receipt.write_text(json.dumps({"status": receipt_status}))

        harness.response = malformed_first_review
        harness.after = change_receipt
        return Study(root, harness), harness

    def test_review_recovery_publishes_without_repeating_execute(self):
        study, harness = self.recovery_fixture()
        state = INNER.run_review_recovery(study)
        self.assertEqual(state["phase"], "done")
        self.assertEqual(harness.count("execute"), 1)
        self.assertEqual(harness.count("review"), 2)
        self.assertEqual(state["call_count"], 7)
        self.assertEqual(study.graph()[0]["status"], "reviewed")
        self.assertTrue((study.root / "iterations/0001/publication.json").is_file())
        self.assertEqual(
            read(study.root / state["review_ref"])["candidate_reviews"], []
        )
        retained = study.root / "iterations/0001/review/attempt-001/response.json"
        self.assertEqual(
            read(retained)["candidate_reviews"][0]["candidate_id"],
            "unmeasured-artifact",
        )
        observations = read(study.root / "observations.json")
        reviews = [r for r in observations["intervals"] if r["phase"] == "review"]
        self.assertEqual([r["status"] for r in reviews], ["failed", "succeeded"])
        self.assertTrue(all(r["wall_seconds"] > 0 for r in reviews))
        self.assertAlmostEqual(
            observations["totals"]["review_wall_seconds"],
            sum(r["wall_seconds"] for r in reviews),
        )
        self.assertEqual(
            [r["status"] for r in observations["runs"]], ["failed", "succeeded"]
        )

    def test_review_recovery_requires_a_successful_process_receipt(self):
        for receipt_status in (None, "failed", "running"):
            with self.subTest(receipt_status=receipt_status):
                study, harness = self.recovery_fixture(receipt_status)
                try:
                    state = INNER.run_review_recovery(study)
                except (StudyError, ValueError, OSError):
                    state = study.status()
                self.assertEqual(harness.count("execute"), 1)
                self.assertEqual(harness.count("review"), 1)
                self.assertEqual(study.status()["call_count"], 6)
                self.assertEqual(state["status"], "needs_attention")
                self.assertIn(
                    "candidate_reviews",
                    state["error"],
                )
                self.assertFalse(
                    (study.root / "iterations/0001/publication.json").exists()
                )

    def test_review_recovery_retries_at_most_once(self):
        study, harness = self.recovery_fixture(malformed_reviews=2)
        with self.assertRaisesRegex(
            (StudyError, ValueError),
            "candidate_reviews|Candidate reviews must match unique current measurements exactly",
        ):
            INNER.run_review_recovery(study)
        self.assertEqual(harness.count("execute"), 1)
        self.assertEqual(harness.count("review"), 2)
        self.assertEqual(study.status()["call_count"], 7)
        self.assertFalse((study.root / "iterations/0001/publication.json").exists())

    def test_review_recovery_does_not_retry_additional_model_errors(self):
        study, harness = self.recovery_fixture()
        response = harness.response

        def additional_error(phase, context, kwargs):
            result = response(phase, context, kwargs)
            if phase == "review":
                result["route_status"] = "invalid"
            return result

        harness.response = additional_error
        with self.assertRaises(ValueError):
            INNER.run_review_recovery(study)
        self.assertEqual(harness.count("execute"), 1)
        self.assertEqual(harness.count("review"), 1)
        self.assertEqual(study.status()["call_count"], 6)

    def outer_fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        state = {
            "phase": "done",
            "execution_ref": "iterations/0001/execute/response.json",
            "review_ref": "iterations/0001/review/response.json",
        }
        candidate = {"ref": "candidate-version", "parent_ref": "supervisor-version"}
        artifact = {
            "path": str(root / "research/iterations/0001/workspace/strategy.json"),
            "version": candidate["ref"],
        }
        executed = {
            "outcome": "completed",
            "artifacts": [artifact],
            "measurements": [
                {
                    "candidate_id": RUNNER.CANDIDATE_ID,
                    "artifact": dict(artifact),
                    "evaluation_id": "meta-development-v1",
                    "comparison_scope": candidate["parent_ref"],
                    "metrics": [{"name": "mean_rO", "value": 0.75}],
                    "evidence": ["evaluation-result.json"],
                }
            ],
        }
        reviewed = {
            "candidate_reviews": [
                {"candidate_id": RUNNER.CANDIDATE_ID, "valid": True, "verified": True}
            ]
        }
        paired = {
            "candidate": candidate,
            "report": {
                "rows": [
                    {"arm": "S", "overhead_seconds": 100.0},
                    {"arm": "C", "overhead_seconds": 75.0},
                ]
            },
        }
        for directory in ("workspace", "execute", "review"):
            (root / "research/iterations/0001" / directory).mkdir(parents=True)
        INNER.write(Path(artifact["path"]), RUNNER.POLICY)
        INNER.write(root / "research" / state["execution_ref"], executed)
        INNER.write(root / "research" / state["review_ref"], reviewed)
        INNER.write(
            root / "research/iterations/0001/iteration.json", {"status": "reviewed"}
        )
        return root, state, paired, executed, reviewed

    def test_outer_accepts_completed_frozen_policy_trusted_ratio_and_published_review(
        self,
    ):
        root, state, paired, _, _ = self.outer_fixture()
        RUNNER.verify_outer(root, state, paired)

    def test_outer_rejects_incomplete_or_unbound_execute(self):
        for defect in (
            "incomplete",
            "duplicate_artifact",
            "artifact_version",
            "measurement_version",
            "wrong_ratio",
            "duplicate_measurement",
            "evaluation_identity",
            "comparison_scope",
            "wrong_policy",
        ):
            with self.subTest(defect=defect):
                root, state, paired, executed, _ = self.outer_fixture()
                measurement = executed["measurements"][0]
                if defect == "incomplete":
                    executed["outcome"] = "incomplete"
                elif defect == "duplicate_artifact":
                    executed["artifacts"] *= 2
                elif defect == "artifact_version":
                    executed["artifacts"][0]["version"] = "unfrozen-version"
                elif defect == "measurement_version":
                    measurement["artifact"]["version"] = "unfrozen-version"
                elif defect == "wrong_ratio":
                    measurement["metrics"][0]["value"] = 0.5
                elif defect == "duplicate_measurement":
                    executed["measurements"] *= 2
                elif defect == "evaluation_identity":
                    measurement["evaluation_id"] = "another-evaluation"
                elif defect == "comparison_scope":
                    measurement["comparison_scope"] = "another-supervisor"
                elif defect == "wrong_policy":
                    INNER.write(
                        Path(executed["artifacts"][0]["path"]), RUNNER.BASELINE_POLICY
                    )
                INNER.write(root / "research" / state["execution_ref"], executed)
                with self.assertRaises(ValueError):
                    RUNNER.verify_outer(root, state, paired)

    def test_outer_rejects_invalid_unverified_or_unpublished_review(self):
        for defect in (
            "active_phase",
            "missing_review",
            "invalid",
            "unverified",
            "duplicate_review",
            "unpublished",
        ):
            with self.subTest(defect=defect):
                root, state, paired, _, reviewed = self.outer_fixture()
                if defect == "active_phase":
                    state["phase"] = "review"
                elif defect == "missing_review":
                    state["review_ref"] = None
                elif defect == "invalid":
                    reviewed["candidate_reviews"][0]["valid"] = False
                elif defect == "unverified":
                    reviewed["candidate_reviews"][0]["verified"] = False
                elif defect == "duplicate_review":
                    reviewed["candidate_reviews"] *= 2
                elif defect == "unpublished":
                    INNER.write(
                        root / "research/iterations/0001/iteration.json",
                        {"status": "planned"},
                    )
                INNER.write(
                    root / "research/iterations/0001/review/response.json", reviewed
                )
                with self.assertRaises(ValueError):
                    RUNNER.verify_outer(root, state, paired)

    def test_cancelled_evaluation_never_reserves_or_launches_job(self):
        stop = threading.Event()
        stop.set()
        args = SimpleNamespace(stop_event=stop)
        meta = Mock()
        with patch.object(RUNNER.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(
                RuntimeError, "no further evaluation may launch"
            ):
                RUNNER.subprocess_run(meta, args, {}, "cancelled-job", "cactus")
        meta.reserve_job.assert_not_called()
        meta.status.assert_not_called()
        launch.assert_not_called()

    def test_request_cannot_change_paired_training_conditions(self):
        self.assertEqual(INNER.fixed_request(INNER.FIXED_REQUEST), INNER.FIXED_REQUEST)
        for change in (
            {"seed": 1},
            {"epochs": 2},
            {"lr": 0.002},
            {"batch_size": 256},
            {"epochs": True},
            {"extra": "path"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                INNER.fixed_request({**INNER.FIXED_REQUEST, **change})

    def test_usage_includes_cached_input_once_and_never_defaults_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attempt = root / "iterations/0001/review/attempt-001"
            attempt.mkdir(parents=True)
            (attempt / "process.json").write_text(json.dumps({"status": "succeeded"}))
            event = {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 80,
                    "output_tokens": 10,
                    "reasoning_output_tokens": 5,
                },
            }
            path = attempt / "stdout.jsonl"
            path.write_text(json.dumps(event) + "\n")
            self.assertEqual(INNER.usage(root)["tokens"], 110)
            self.assertEqual(INNER.usage(root)["calls"], 1)
            del event["usage"]["output_tokens"]
            path.write_text(json.dumps(event) + "\n")
            with self.assertRaisesRegex(ValueError, "output_tokens"):
                INNER.usage(root)
            path.write_text("")
            with self.assertRaisesRegex(ValueError, "usage"):
                INNER.usage(root)


if __name__ == "__main__":
    unittest.main()
