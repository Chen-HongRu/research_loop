"""A settled formal time limit ends one investigation without a score or replay."""

import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from integrations.protok import deploy_runtime
from integrations.protok.backend import CANDIDATE_FILES, PROTOCOL, ProTokError
from research_loop.harness import HarnessError, completed_response
from research_loop.submission import submit
from test_core import RecordedHarness
import test_protok_runner as fixtures

RUNNER = fixtures.RUNNER


class TimeoutBackend(fixtures.FakeBackend):
    def __init__(self, project, control):
        super().__init__(project, control)
        self.timeouts = {}
        self.unsettled = False

    def evaluate(self, workspace, output, timeout_seconds, *, cancel_event):
        self.evaluations += 1
        prepared = self.prepare_config(workspace, output)
        self.timeouts[prepared["fingerprint"]] = {
            "purpose": "formal",
            "status": "failed",
            "failure_kind": "allocation_timeout",
            "fingerprint": prepared["fingerprint"],
            "record_name": prepared["fingerprint"],
            "job_id": "790195",
            "terminal_state": "TIMEOUT",
            "exit_code": "0:0",
            "gpu_seconds": 1828,
            "quality_score_eligible": False,
            "cause": "Slurm allocation time limit reached.",
            "evidence": [str(output)],
        }
        raise ProTokError("Protected evaluator failed, cancelled, or timed out")

    def timed_out_receipt(self, workspace, config, fingerprint, **kwargs):
        if self.unsettled or fingerprint not in self.timeouts:
            raise ProTokError("Allocation has unknown or unsettled effects")
        if self.prepare_config(workspace, config)["fingerprint"] != fingerprint:
            raise ProTokError("Frozen timeout identity changed")
        return self.timeouts[fingerprint]

    def inspect(self):
        status = super().inspect()
        status["used_gpu_seconds"] += sum(
            row["gpu_seconds"] for row in self.timeouts.values()
        )
        return status


class TimeoutDelegate(RecordedHarness):
    def response(self, phase, context, kwargs):
        if phase != "execute":
            return super().response(phase, context, kwargs)
        workspace = Path(kwargs["workspace"])
        iteration = workspace.parent.name
        if iteration != "0001":
            (workspace / CANDIDATE_FILES[0]).write_text(
                "# bounded intervention " + iteration + "\n"
            )
        action = "import" if iteration == "0001" else "evaluate"
        submit(workspace, action=action)
        deadline = time.monotonic() + 5
        while not (workspace / "evaluation-result.json").exists():
            if time.monotonic() >= deadline:
                raise AssertionError("No retained controller result")
            time.sleep(0.01)
        receipt = submit(workspace, action=action)["result"]
        if receipt["status"] != "completed":
            return {
                "outcome": "incomplete",
                "markdown": "The allocation reached its time limit; no official score.",
                "artifacts": [receipt["artifact"]] if "artifact" in receipt else [],
                "measurements": [],
            }
        return {
            "outcome": "completed",
            "markdown": "Imported the existing trusted best.",
            "artifacts": [receipt["artifact"]],
            "measurements": [
                {
                    "candidate_id": receipt["fingerprint"],
                    "artifact": receipt["artifact"],
                    "evaluation_id": PROTOCOL,
                    "comparison_scope": PROTOCOL,
                    "metrics": [{"name": RUNNER.METRIC, "value": receipt["value"]}],
                    "evidence": receipt["evidence"],
                }
            ],
        }


class FormalTimeoutTests(unittest.TestCase):
    setUp = fixtures.ProTokRunnerTests.setUp
    command = fixtures.ProTokRunnerTests.command

    def initialize(self):
        self.backend = TimeoutBackend(self.project, self.control)
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
        self.delegate = TimeoutDelegate()
        self.delegate.stop_after = 100
        self.study = RUNNER.Study(self.root / "study")
        with patch.object(RUNNER, "CodexHarness", return_value=self.delegate):
            RUNNER.run_one(self.root, self.backend)
        self.delegate.base = "0001"

    def run_one(self):
        with patch.object(RUNNER, "CodexHarness", return_value=self.delegate):
            return RUNNER.run_one(self.root, self.backend)

    def test_timeout_is_reviewed_and_next_investigation_runs_without_score(self):
        self.initialize()
        state = self.run_one()
        self.assertEqual(
            (state["iteration"], state["phase"], state["status"]), (2, "done", "ready")
        )
        self.assertFalse((self.control / "STOP").exists())
        self.assertEqual(self.delegate.count("review"), 2)
        self.assertEqual(self.backend.evaluations, 1)
        execute = completed_response(self.study.root / state["calls"]["execute"])
        self.assertEqual(execute["outcome"], "incomplete")
        self.assertEqual(execute["measurements"], [])
        trusted = RUNNER.read(self.control / "jobs/0002/result.json")
        self.assertEqual(trusted["gpu_seconds"], 1828)
        self.assertEqual(trusted["exit_code"], "0:0")
        self.assertNotIn("value", trusted)
        self.assertFalse(trusted["quality_score_eligible"])
        self.assertEqual(
            deploy_runtime.check_boundary(self.root, self.backend)["iteration"], 2
        )
        original = (self.control / "jobs/0002/timeout.json").read_bytes()
        workspace = self.study.root / "iterations/0002/workspace"
        harness = RUNNER.ProTokHarness(self.backend, self.root, Mock(), self.deadline)
        self.assertEqual(
            harness.evaluate_request(
                workspace,
                "0002",
                {"action": "evaluate"},
                threading.Event(),
                self.deadline,
            ),
            trusted,
        )
        self.assertEqual(self.backend.evaluations, 1)
        self.assertEqual(
            (self.control / "jobs/0002/timeout.json").read_bytes(), original
        )
        state = self.run_one()
        self.assertEqual(
            (state["iteration"], state["phase"], state["status"]), (3, "done", "ready")
        )
        self.assertEqual(self.delegate.count("review"), 3)
        self.assertEqual(self.backend.evaluations, 2)
        frontier = self.study.frontiers()["global"][PROTOCOL]["candidates"]
        self.assertEqual([row["experiment_id"] for row in frontier], ["0001"])

    def test_unsettled_timeout_still_blocks_and_preserves_partial_execute(self):
        self.initialize()
        self.backend.unsettled = True
        with self.assertRaisesRegex(HarnessError, "Protected evaluator failed"):
            self.run_one()
        state = self.study.status()
        self.assertEqual(
            (state["phase"], state["status"]), ("execute", "needs_attention")
        )
        self.assertEqual(self.delegate.count("review"), 1)
        self.assertFalse((self.control / "STOP").exists())
        self.assertIsNone(
            completed_response(self.study.root / state["calls"]["execute"])
        )

    def test_settled_timeout_review_can_coexist_with_independent_training(self):
        self.initialize()
        with patch.object(
            RUNNER, "timed_out_job", side_effect=ProTokError("old wrapper")
        ):
            with self.assertRaises(HarnessError):
                self.run_one()
        before = self.study.status()
        effects = self.backend.inspect()
        for channel in ("outstanding", "research_outstanding", "debug_outstanding"):
            with (
                self.subTest(channel=channel),
                patch.object(
                    self.backend,
                    "inspect",
                    return_value={**effects, channel: ["unknown"]},
                ),
            ):
                with self.assertRaisesRegex(ProTokError, "Unresolved effects"):
                    RUNNER.reconcile_timed_out_execute(
                        self.root, self.backend, authorization="Restore settled timeout"
                    )
                self.assertEqual(self.study.status(), before)
        stop = self.control / "STOP"
        stop.write_text("Supervisor stopped: attention\n")
        independent = {
            **effects,
            "manual_training_outstanding": ["known-independent-job"],
            "long_training_outstanding": ["known-independent-child"],
        }
        with patch.object(self.backend, "inspect", return_value=independent):
            prepared = RUNNER.reconcile_timed_out_execute(
                self.root, self.backend, authorization="Restore settled timeout"
            )
            self.assertEqual(prepared["call_count"], before["call_count"])
            self.assertEqual(prepared["elapsed_seconds"], before["elapsed_seconds"])
            for channel in ("manual_training_outstanding", "long_training_outstanding"):
                with (
                    self.subTest(deployment_channel=channel),
                    patch.object(
                        self.backend,
                        "inspect",
                        return_value={**effects, channel: independent[channel]},
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, "Outstanding effects"):
                        deploy_runtime.check_timeout_boundary(self.root, self.backend)
            stop.unlink()
            state = self.run_one()
            self.assertEqual((state["phase"], state["status"]), ("done", "ready"))
            with self.assertRaisesRegex(RuntimeError, "Outstanding"):
                deploy_runtime.check_boundary(self.root, self.backend)
        self.assertEqual(self.backend.evaluations, 1)
        response = completed_response(self.study.root / state["calls"]["execute"])
        self.assertEqual(response["outcome"], "incomplete")
        self.assertEqual(response["measurements"], [])
        self.assertEqual(
            RUNNER.read(self.control / "jobs/0002/result.json")["gpu_seconds"], 1828
        )

    def test_reconcile_old_failure_preserves_agent_plan_stop_and_usage(self):
        self.initialize()
        with patch.object(
            RUNNER, "timed_out_job", side_effect=ProTokError("old wrapper")
        ):
            with self.assertRaises(HarnessError):
                self.run_one()
        before = self.study.status()
        old = self.study.root / before["calls"]["execute"]
        raw = (old / "agent/response.json").read_bytes()
        plan = (self.study.root / before["plan_ref"]).read_bytes()
        stop = self.control / "STOP"
        stop.write_text("Supervisor stopped: attention\n")
        prepared = RUNNER.reconcile_timed_out_execute(
            self.root,
            self.backend,
            authorization="User authorized task failure Review and continuation",
        )
        self.assertEqual(prepared["call_count"], before["call_count"])
        self.assertEqual(prepared["elapsed_seconds"], before["elapsed_seconds"])
        self.assertEqual(self.backend.evaluations, 1)
        self.assertEqual((old / "agent/response.json").read_bytes(), raw)
        self.assertEqual((self.study.root / before["plan_ref"]).read_bytes(), plan)
        self.assertEqual(stop.read_text(), "Supervisor stopped: attention\n")
        self.assertEqual(
            RUNNER.reconcile_timed_out_execute(
                self.root, self.backend, authorization="Same user authorization"
            ),
            prepared,
        )
        self.assertEqual(
            deploy_runtime.check_timeout_boundary(self.root, self.backend), prepared
        )
        for field in ("call_count", "elapsed_seconds", "plan_ref"):
            with self.subTest(changed_state=field):
                changed = {
                    **prepared,
                    field: 0 if field != "plan_ref" else "other-plan",
                }
                RUNNER.write(self.study.root / "state.json", changed)
                with self.assertRaisesRegex(RuntimeError, "preparation"):
                    deploy_runtime.check_timeout_boundary(self.root, self.backend)
                RUNNER.write(self.study.root / "state.json", prepared)
        for relative in (
            "control/continuation.json",
            "study/study.json",
            "study/" + before["plan_ref"],
        ):
            with self.subTest(changed_contract=relative):
                path = self.root / relative
                original = path.read_text()
                value = RUNNER.read(path)
                if relative == "control/continuation.json":
                    value["deadline"] += 3600
                else:
                    value["changed_after_preparation"] = True
                RUNNER.write(path, value)
                with self.assertRaisesRegex(RuntimeError, "protected contract"):
                    deploy_runtime.check_timeout_boundary(self.root, self.backend)
                path.write_text(original)
        stop.write_text("A newer user stop\n")
        with self.assertRaisesRegex(RuntimeError, "STOP changed"):
            deploy_runtime.check_timeout_boundary(self.root, self.backend)
        stop.write_text("Supervisor stopped: attention\n")
        owner = RUNNER.read(self.control / "ownership.json")
        RUNNER.write(
            self.control / "ownership.json", {**owner, "expires_at": time.time() - 1}
        )
        with self.assertRaisesRegex(RuntimeError, "deadline"):
            deploy_runtime.check_timeout_boundary(self.root, self.backend)
        RUNNER.write(self.control / "ownership.json", owner)
        stop.unlink()
        state = self.run_one()
        self.assertEqual(
            (state["iteration"], state["phase"], state["status"]), (2, "done", "ready")
        )
        self.assertEqual(self.delegate.count("execute"), 2)
        self.assertEqual(self.delegate.count("review"), 2)
        self.assertEqual(self.backend.evaluations, 1)
        self.assertEqual(state["call_count"], before["call_count"] + 1)


if __name__ == "__main__":
    unittest.main()
