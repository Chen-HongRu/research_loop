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

    def plan_sources(self, sources):
        RUNNER.write(
            self.project / "autoresearch-run.json", {"cache_root": "/trusted/cache"}
        )
        original = self.delegate.response

        def response(phase, context, kwargs):
            result = original(phase, context, kwargs)
            if phase == "plan":
                source = sources.pop(0) if len(sources) > 1 else sources[0]
                result["execution"]["inputs"] = [{"name": "cache", "source": source}]
            return result

        self.delegate.response = response

    def test_invalid_input_corrects_plan_before_one_execute_and_review(self):
        self.initialize()
        self.plan_sources(["hidden_cach", "hidden_cache"])
        self.initial_calls = self.study.status()["call_count"]
        with patch(
            "integrations.protok.research_backend._input_metadata",
            side_effect=AssertionError("Plan must not probe remote inputs"),
        ):
            state = self.run_one()
        self.assert_reviewed(state)
        plans = [call for call in self.delegate.calls if call["phase"] == "plan"]
        self.assertEqual(len(plans), 2)
        self.assertEqual(plans[0]["context"], plans[1]["context"])
        rejected = RUNNER.read(plans[0]["run_dir"] / "rejection.json")
        self.assertIn("Unknown research input", rejected["errors"][0]["message"])
        self.assertEqual(
            RUNNER.read(plans[1]["run_dir"] / "parameter-budget.json")["deadline"],
            RUNNER.read(plans[0]["run_dir"] / "parameter-budget.json")["deadline"],
        )
        accepted = RUNNER.read(self.study.root / state["plan_ref"])
        self.assertEqual(accepted["execution"]["inputs"][0]["source"], "hidden_cache")
        self.assertEqual(self.delegate.count("execute"), 1)
        self.assertEqual(self.delegate.count("review"), 1)
        self.assertEqual(
            state["call_count"],
            self.initial_calls + len(self.delegate.calls),
        )

    def assert_execution_parameter_correction(self, field, invalid):
        self.initialize()
        original = self.delegate.response
        values = [
            invalid,
            "research/main.py" if field == "entrypoint" else ["result.json"],
        ]

        def response(phase, context, kwargs):
            result = original(phase, context, kwargs)
            if phase == "plan":
                result["execution"][field] = values.pop(0)
            return result

        self.delegate.response = response
        state = self.run_one()
        self.assert_reviewed(state)
        plans = [call for call in self.delegate.calls if call["phase"] == "plan"]
        self.assertEqual(len(plans), 2)
        self.assertEqual(plans[0]["context"], plans[1]["context"])
        self.assertTrue((plans[0]["run_dir"] / "rejection.json").exists())
        self.assertEqual(
            RUNNER.read(plans[0]["run_dir"] / "parameter-budget.json")["deadline"],
            RUNNER.read(plans[1]["run_dir"] / "parameter-budget.json")["deadline"],
        )
        self.assertEqual(self.delegate.count("execute"), 1)
        self.assertEqual(self.delegate.count("review"), 1)

    def test_invalid_entrypoint_corrects_before_one_execute(self):
        self.assert_execution_parameter_correction("entrypoint", "research/a+b.py")

    def test_invalid_output_path_corrects_before_one_execute(self):
        self.assert_execution_parameter_correction("outputs", ["result space.json"])

    def test_policy_limit_corrects_plan_even_without_inputs(self):
        self.initialize()
        policy = RUNNER.read(self.control / "research-policy.json")
        RUNNER.write(
            self.control / "research-policy.json",
            {**policy, "max_time_limit_seconds": 1200},
        )
        original = self.delegate.response
        limits = [1800, 1200]

        def response(phase, context, kwargs):
            result = original(phase, context, kwargs)
            if phase == "plan":
                result["execution"]["time_limit_seconds"] = limits.pop(0)
            return result

        self.delegate.response = response
        self.assert_reviewed(self.run_one())
        plans = [call for call in self.delegate.calls if call["phase"] == "plan"]
        self.assertEqual(len(plans), 2)
        rejection = RUNNER.read(plans[0]["run_dir"] / "rejection.json")
        self.assertIn("authorized", rejection["errors"][0]["message"])
        self.assertEqual(
            RUNNER.read(plans[0]["run_dir"] / "parameter-budget.json")["deadline"],
            RUNNER.read(plans[1]["run_dir"] / "parameter-budget.json")["deadline"],
        )
        self.assertEqual(self.delegate.count("execute"), 1)

    def test_corrupt_trusted_policy_blocks_without_parameter_classification(self):
        self.initialize()
        RUNNER.write(self.control / "research-policy.json", {"version": 1})
        harness = RUNNER.ProTokHarness(
            self.backend, self.root, self.delegate, self.deadline
        )
        with patch("integrations.protok.research_backend._execution") as execution:
            with self.assertRaisesRegex(
                RUNNER.ProTokError, "Invalid research-script authorization"
            ):
                harness.validate_plan(
                    {"decision": "investigate", **contract()}, iteration=2
                )
        execution.assert_not_called()
        self.assertEqual(self.delegate.count("execute"), 0)
        self.assertEqual(self.allocations, 0)

    def test_script_without_authorization_is_a_correctable_plan_choice(self):
        self.initialize()
        (self.control / "research-policy.json").unlink()
        harness = RUNNER.ProTokHarness(
            self.backend, self.root, self.delegate, self.deadline
        )
        with self.assertRaisesRegex(RUNNER.ParameterError, "choose fixed_evaluation"):
            harness.validate_plan(
                {"decision": "investigate", **contract()}, iteration=2
            )
        self.assertEqual(self.delegate.count("execute"), 0)
        self.assertEqual(self.allocations, 0)

    def test_invalid_input_exhausts_original_plan_budget_without_execute(self):
        self.initialize()
        self.plan_sources(["hidden_cach"])
        config = RUNNER.read(self.study.root / "study.json")
        config["max_calls"] = self.study.status()["call_count"] + 12
        RUNNER.write(self.study.root / "study.json", config)
        with self.assertRaisesRegex(
            RUNNER.ProTokError, "no published independent Review"
        ):
            self.run_one()
        state = self.study.status()
        self.assertEqual(state["stop_reason"], "call_budget")
        self.assertEqual(state["call_count"], config["max_calls"])
        self.assertGreater(self.delegate.count("plan"), 1)
        self.assertEqual(self.delegate.count("execute"), 0)
        self.assertEqual(self.allocations, 0)
        self.assertFalse((self.control / "research-sources").exists())

    def test_trusted_plan_input_failure_blocks_without_parameter_retry(self):
        self.initialize()
        self.plan_sources(["teacher"])
        RUNNER.write(self.project / "autoresearch-run.json", {})
        with self.assertRaises(KeyError):
            self.run_one()
        self.assertEqual(self.delegate.count("plan"), 1)
        self.assertEqual(self.delegate.count("execute"), 0)
        self.assertEqual(self.allocations, 0)
        plan_call = next(
            call for call in self.delegate.calls if call["phase"] == "plan"
        )
        self.assertFalse((plan_call["run_dir"] / "rejection.json").exists())

    def script(self, backend, *, root, iteration, **kwargs):
        from integrations.protok.research_settlement import mark_script_submission

        mark_script_submission(root, iteration, kwargs["execution"], kwargs["deadline"])
        self.allocations += 1
        directory = root / "control/research-jobs" / iteration
        identity = {
            "source_identity": "source-" + iteration,
            "execution_identity": "execution-" + iteration,
        }
        RUNNER.write(
            directory / "job.json",
            {**identity, "allocated_seconds": 1200, "submission_intent": True},
        )
        status = getattr(self, "script_status", "failed")
        receipt = {
            **identity,
            "allocated_seconds": 1200,
            "gpu_seconds": 1222,
            "purpose": "research",
            "quality_score_eligible": False,
            "status": status,
            "terminal_state": "TIMEOUT" if status == "failed" else "COMPLETED",
            "exit_code": "0:0",
            "owned_job_id": "791012",
            "outputs": [],
            "error": "Slurm TIMEOUT",
            "evidence": [str(directory / "state.json")],
        }
        RUNNER.write(directory / "receipt.json", receipt)
        RUNNER.write(
            directory / "state.json",
            {
                "attempts": [
                    {
                        name: receipt[name]
                        for name in (
                            "status",
                            "owned_job_id",
                            "terminal_state",
                            "exit_code",
                            "gpu_seconds",
                        )
                    }
                ]
            },
        )
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
            side_effect=RUNNER.ProTokError("controller accounting fault"),
        ):
            with self.assertRaises(HarnessError):
                self.run_one()
        before = self.study.status()
        previous = self.study.root / before["calls"]["execute"]
        original = (previous / "agent/response.json").read_bytes()
        plan = (self.study.root / before["plan_ref"]).read_bytes()
        (self.control / "STOP").write_text("Supervisor stopped: attention\n")
        state = RUNNER.reconcile_execute(
            self.root,
            self.backend,
            authorization="User authorized restoring the settled failure",
        )
        self.assertEqual(state["call_count"], before["call_count"])
        self.assertEqual(state["calls"], before["calls"])
        self.assertEqual(state["elapsed_seconds"], before["elapsed_seconds"])
        self.assertEqual((previous / "agent/response.json").read_bytes(), original)
        self.assertEqual((self.study.root / before["plan_ref"]).read_bytes(), plan)
        self.assertEqual(
            deploy_runtime.check_reconciliation_boundary(self.root, self.backend),
            state,
        )
        self.assertEqual(
            RUNNER.reconcile_execute(
                self.root, self.backend, authorization="same authorization"
            ),
            state,
        )
        (self.control / "STOP").unlink()
        self.assert_reviewed(self.run_one())
        self.assertEqual(self.delegate.count("execute"), 1)
        review = next(
            call for call in reversed(self.delegate.calls) if call["phase"] == "review"
        )
        event = review["context"]["execution_recovery"][0]
        self.assertEqual(event["source_attempt"], before["calls"]["execute"])
        self.assertEqual(event["plan_ref"], before["plan_ref"])
        self.assertEqual((event["action"], event["effect"]), ("settle", "terminal"))
        self.assertIn(str(previous / "reconciliation.json"), event["proof_refs"])
        self.assertIn(event["recovery_id"], review["prompt"])

    def test_live_workspace_artifact_does_not_erase_frozen_settled_receipt(self):
        from integrations.protok.research_execution import completed_script

        self.initialize()

        def strict_result(root, workspace, iteration):
            artifact = Path(workspace) / "research/scratch.bin"
            artifact.write_bytes(b"local diagnostic")
            return completed_script(root, workspace, iteration)

        with patch(
            "integrations.protok.research_execution.completed_script",
            side_effect=strict_result,
        ):
            with self.assertRaisesRegex(HarnessError, "small regular source"):
                self.run_one()
        before = self.study.status()
        self.assertEqual(self.allocations, 1)
        self.assertEqual(self.delegate.count("review"), 0)
        workspace = self.study.root / "iterations/0002/workspace"
        artifact = (workspace / "research/scratch.bin").read_bytes()
        state = RUNNER.reconcile_execute(
            self.root,
            self.backend,
            authorization="Review the settled frozen experiment",
        )
        self.assertEqual(state["calls"], before["calls"])
        self.assertEqual((workspace / "research/scratch.bin").read_bytes(), artifact)
        self.assert_reviewed(self.run_one())
        self.assertEqual(self.delegate.count("execute"), 1)

    def test_completed_research_with_interrupted_report_reconciles_without_score(self):
        self.initialize()
        self.script_status = "completed"
        self.delegate.fail_reporting = True
        with self.assertRaisesRegex(HarnessError, "Agent stopped"):
            self.run_one()
        before = self.study.status()
        state = RUNNER.reconcile_execute(
            self.root,
            self.backend,
            authorization="Review completed retained computation",
        )
        attempt = self.study.root / state["calls"]["execute"]
        self.assertEqual(state["calls"], before["calls"])
        self.assertEqual(completed_response(attempt)["outcome"], "completed")
        self.assertEqual(completed_response(attempt)["measurements"], [])
        reviewed = self.run_one()
        self.assertEqual((reviewed["iteration"], reviewed["phase"]), (2, "done"))
        self.assertEqual(self.allocations, 1)
        self.assertEqual(self.delegate.count("execute"), 1)
        self.assertEqual(self.delegate.count("review"), 1)

    def assert_not_started_refuses_changed_workspace(self, *, fixed):
        from integrations.protok.research_execution import settle_script_execution

        self.initialize()
        original = self.delegate.response

        def response(phase, context, kwargs):
            if phase == "execute":
                workspace = Path(kwargs["workspace"])
                if fixed:
                    RUNNER.write(
                        workspace.parent / "context/task.json",
                        {
                            "allow_changes": False,
                            "source": {
                                "revision": RUNNER.git(workspace, "rev-parse", "HEAD")
                            },
                        },
                    )
                    (workspace / RUNNER.CANDIDATE_FILES[0]).write_text(
                        "# changed fixed source\n"
                    )
                else:
                    (workspace / "README.md").write_text("Changed protected source.\n")
            return original(phase, context, kwargs)

        self.delegate.response = response
        message = (
            "Fixed-candidate manual task changed source"
            if fixed
            else "outside candidate scope"
        )
        with self.assertRaisesRegex(HarnessError, message):
            self.run_one()
        before = self.study.status()
        workspace = self.study.root / "iterations/0002/workspace"
        attempt_ref = before["calls"]["execute"]
        attempt = self.study.root / attempt_ref
        # No-allocation remains true; workspace authorization is a separate
        # condition for the controller to accept this Execute for Review.
        self.assertEqual(
            settle_script_execution(self.root, workspace, "0002", attempt_ref).status,
            "not_started",
        )
        with self.assertRaisesRegex(RUNNER.ProTokError, message):
            RUNNER.reconcile_execute(
                self.root, self.backend, authorization="Inspect the unstarted attempt"
            )
        self.assertEqual(self.study.status(), before)
        self.assertFalse((attempt / "reconciliation.json").exists())
        self.assertIsNone(completed_response(attempt))
        self.assertEqual(self.delegate.count("execute"), 1)
        self.assertEqual(self.delegate.count("review"), 0)
        self.assertEqual(self.allocations, 0)

    def test_not_started_protected_source_change_blocks_reconciliation(self):
        self.assert_not_started_refuses_changed_workspace(fixed=False)

    def test_not_started_fixed_candidate_change_blocks_reconciliation(self):
        self.assert_not_started_refuses_changed_workspace(fixed=True)

    def test_preparation_pipeline_fault_requires_explicit_not_started_settlement(self):
        self.initialize()
        with patch(
            "integrations.protok.research_backend.prepare_script",
            side_effect=RuntimeError("trusted preparation defect"),
        ):
            with self.assertRaisesRegex(HarnessError, "trusted preparation defect"):
                self.run_one()
        before = self.study.status()
        self.assertEqual(
            (before["phase"], before["status"]), ("execute", "needs_attention")
        )
        self.assertEqual(self.allocations, 0)
        self.assertEqual(self.delegate.count("review"), 0)
        attempt = self.study.root / before["calls"]["execute"]
        workspace = self.study.root / "iterations/0002/workspace"
        invalid_source = b"def incomplete_edit(:\n"
        (workspace / "research/main.py").write_bytes(invalid_source)
        agent = {
            path.name: path.read_bytes()
            for path in (attempt / "agent").iterdir()
            if path.is_file()
        }
        (self.control / "STOP").write_text("User pause retained\n")
        with self.assertRaisesRegex(RUNNER.ProTokError, "authorization"):
            RUNNER.reconcile_execute(self.root, self.backend, authorization="")
        with patch.object(
            self.backend, "inspect", return_value={"outstanding": ["unknown"]}
        ):
            with self.assertRaisesRegex(RUNNER.ProTokError, "Unresolved effects"):
                RUNNER.reconcile_execute(
                    self.root, self.backend, authorization="Inspect closure"
                )
        self.assertFalse((attempt / "reconciliation.json").exists())
        state = RUNNER.reconcile_execute(
            self.root,
            self.backend,
            authorization="Settle the confirmed unstarted attempt",
        )
        self.assertEqual(state["calls"], before["calls"])
        self.assertEqual(state["call_count"], before["call_count"])
        self.assertEqual(state["elapsed_seconds"], before["elapsed_seconds"])
        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in (attempt / "agent").iterdir()
                if path.is_file()
            },
            agent,
        )
        result = RUNNER.read(attempt / "reconciliation.json")["trusted_result"]
        self.assertEqual(result["status"], "not_started")
        self.assertEqual((workspace / "research/main.py").read_bytes(), invalid_source)
        self.assertEqual(result["gpu_seconds"], 0)
        self.assertEqual(completed_response(attempt)["measurements"], [])
        self.assertEqual(self.allocations, 0)
        self.assertEqual(
            RUNNER.reconcile_execute(self.root, self.backend, authorization="repeat"),
            state,
        )
        (self.control / "STOP").unlink()
        reviewed = self.run_one()
        self.assertEqual((reviewed["iteration"], reviewed["phase"]), (2, "done"))
        self.assertEqual(self.delegate.count("execute"), 1)
        self.assertEqual(self.delegate.count("review"), 1)
        self.assertEqual(self.allocations, 0)
        review = next(call for call in self.delegate.calls if call["phase"] == "review")
        event = review["context"]["execution_recovery"][0]
        self.assertEqual(
            (event["action"], event["effect"]), ("settle", "never_submitted")
        )
        self.assertEqual(event["source_attempt"], before["calls"]["execute"])
        self.assertIn(event["recovery_id"], review["prompt"])


if __name__ == "__main__":
    unittest.main()
