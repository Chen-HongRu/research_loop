"""Settled script failures reach real Study Review without another allocation."""

import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from integrations.protok import deploy_runtime
from integrations.protok.research_execution import research_usage
from research_loop.harness import HarnessError, completed_response
from research_loop.submission import submit, status as submission_status
from test_core import RecordedHarness
from test_research_contract import contract
import test_formal_timeout as formal

RUNNER = formal.RUNNER


class ScriptDelegate(RecordedHarness):
    fail_reporting = False
    repair_missing_source = False
    source_corrections = 0
    before_correction = None

    def response(self, phase, context, kwargs):
        if phase == "execute":
            workspace = Path(kwargs["workspace"])
            source = workspace / "research/main.py"
            source.parent.mkdir(exist_ok=True)
            if not self.repair_missing_source:
                source.write_text('print("bounded experiment")\n')
            submit(workspace, "research")
            deadline = time.monotonic() + 5
            while not (workspace / "evaluation-result.json").exists():
                if submission_status(workspace)["status"] == "parameter_error":
                    if self.before_correction:
                        self.before_correction(workspace)
                    self.source_corrections += 1
                    source.write_text('print("bounded experiment")\n')
                    submit(workspace, "research")
                if time.monotonic() >= deadline:
                    raise AssertionError("Controller did not return")
                time.sleep(0.01)
            trusted = json.loads((workspace / "evaluation-result.json").read_text())
            if self.fail_reporting:
                raise HarnessError("Agent stopped after the allocation settled")
            return {
                "outcome": "incomplete",
                "markdown": "An accounted failure is useful evidence.",
                "artifacts": [trusted["artifact"]] if "artifact" in trusted else [],
                "measurements": [],
            }
        result = super().response(phase, context, kwargs)
        if phase == "plan":
            design = contract()
            design["execution"].update(
                entrypoint="research/main.py",
                gpu=True,
                time_limit_seconds=1200,
                inputs=[],
                outputs=["result.json"],
            )
            result.update(design)
        return result


class ResearchFailureTests(unittest.TestCase):
    setUp = formal.FormalTimeoutTests.setUp
    command = formal.FormalTimeoutTests.command
    run_one = formal.FormalTimeoutTests.run_one

    def initialize(self):
        formal.FormalTimeoutTests.initialize(self)
        config = self.study.config
        config["research_execution"] = {"version": 1}
        RUNNER.write(self.study.root / "study.json", config)
        RUNNER.write(
            self.control / "research-policy.json",
            {
                "version": 1,
                "authorization": "test bounded scripts",
                "max_time_limit_seconds": 1800,
            },
        )
        self.delegate = ScriptDelegate()
        self.delegate.base = "0001"
        self.delegate.stop_after = 100
        self.allocations = 0
        self.enterContext(
            patch(
                "integrations.protok.research_backend.prepare_script",
                return_value=object(),
            )
        )
        self.enterContext(
            patch(
                "integrations.protok.research_backend.run_script",
                side_effect=self.script,
            )
        )

    def script(self, backend, *, root, iteration, **kwargs):
        self.allocations += 1
        directory = root / "control/research-jobs" / iteration
        identity = {
            "source_identity": "source-" + iteration,
            "execution_identity": "execution-" + iteration,
        }
        RUNNER.write(directory / "job.json", {**identity, "allocated_seconds": 1200})
        receipt = {
            **identity,
            "allocated_seconds": 1200,
            "gpu_seconds": 1222,
            "purpose": "research",
            "quality_score_eligible": False,
            "status": "failed",
            "terminal_state": "TIMEOUT",
            "owned_job_id": "791012",
            "outputs": [],
            "error": "Slurm TIMEOUT",
            "evidence": [str(directory / "state.json")],
        }
        RUNNER.write(directory / "receipt.json", receipt)
        return receipt

    def assert_reviewed(self, state):
        self.assertEqual(
            (state["iteration"], state["phase"], state["status"]), (2, "done", "ready")
        )
        self.assertEqual(self.allocations, 1)
        self.assertFalse((self.control / "STOP").exists())
        response = completed_response(self.study.root / state["calls"]["execute"])
        self.assertEqual(response["outcome"], "incomplete")
        self.assertEqual(response["measurements"], [])
        self.assertEqual(research_usage(self.control)["gpu_seconds"], 1222)
        self.assertTrue((self.study.root / "iterations/0002/publication.json").exists())
        frontier = self.study.frontiers()["global"][RUNNER.PROTOCOL]["candidates"]
        self.assertEqual([row["experiment_id"] for row in frontier], ["0001"])

    def test_missing_entrypoint_corrects_in_same_execute_then_reaches_review(self):
        self.initialize()
        self.delegate.repair_missing_source = True
        frozen = []

        def before_correction(workspace):
            self.assertEqual(self.allocations, 0)
            self.assertFalse((workspace / "evaluation-submission.json").exists())
            self.assertFalse((self.control / "research-sources").exists())
            state = self.study.status()
            frozen.append((self.study.root / state["plan_ref"]).read_bytes())

        self.delegate.before_correction = before_correction
        state = self.run_one()
        self.assert_reviewed(state)
        self.assertEqual(self.delegate.source_corrections, 1)
        self.assertEqual(self.delegate.count("execute"), 1)
        self.assertEqual(self.delegate.count("review"), 1)
        self.assertEqual((self.study.root / state["plan_ref"]).read_bytes(), frozen[0])
        workspace = Path(
            json.loads(
                (
                    self.study.root / state["calls"]["execute"] / "context.json"
                ).read_text()
            )["workspace"]
        )
        self.assertTrue((workspace / "evaluation-parameter-errors.json").exists())

    def test_timeout_is_reviewed_and_next_investigation_can_start(self):
        self.initialize()
        self.assert_reviewed(self.run_one())
        state = self.run_one()
        self.assertEqual((state["iteration"], state["phase"]), (3, "done"))
        self.assertEqual(self.allocations, 2)

    def test_agent_reporting_failure_does_not_lose_settled_experiment(self):
        self.initialize()
        self.delegate.fail_reporting = True
        self.assert_reviewed(self.run_one())
        state = self.study.status()
        self.assertIn(
            "Agent reporting ended",
            completed_response(self.study.root / state["calls"]["execute"])["markdown"],
        )

    def test_explicit_recovery_preserves_original_attempt_and_does_not_reexecute(self):
        self.initialize()
        with patch(
            "integrations.protok.research_execution.completed_script",
            side_effect=RUNNER.ProTokError("old accounting error"),
        ):
            with self.assertRaises(HarnessError):
                self.run_one()
        before = self.study.status()
        previous = self.study.root / before["calls"]["execute"]
        original = (previous / "agent/response.json").read_bytes()
        plan = (self.study.root / before["plan_ref"]).read_bytes()
        (self.control / "STOP").write_text("Supervisor stopped: attention\n")
        state = RUNNER.reconcile_research_execute(
            self.root,
            self.backend,
            authorization="User authorized restoring the settled failure",
        )
        self.assertEqual(state["call_count"], before["call_count"])
        self.assertEqual(state["elapsed_seconds"], before["elapsed_seconds"])
        self.assertEqual((previous / "agent/response.json").read_bytes(), original)
        self.assertEqual((self.study.root / before["plan_ref"]).read_bytes(), plan)
        self.assertEqual(
            deploy_runtime.check_timeout_boundary(
                self.root, self.backend, research=True
            ),
            state,
        )
        self.assertEqual(
            RUNNER.reconcile_research_execute(
                self.root, self.backend, authorization="same authorization"
            ),
            state,
        )
        (self.control / "STOP").unlink()
        self.assert_reviewed(self.run_one())
        self.assertEqual(self.delegate.count("execute"), 1)


if __name__ == "__main__":
    unittest.main()
