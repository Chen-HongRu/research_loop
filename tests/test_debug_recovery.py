import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_loop import Study, StudyError, initialize
from research_loop.harness import (
    HarnessError,
    RecoverableExecutionError,
    publish_completed_response,
)
from test_core import ProcessLost, RecordedHarness, evaluation, read


class RepairHarness:
    def __init__(self):
        self.calls = []
        self.interrupt = False

    def run(self, **kwargs):
        self.calls.append(kwargs)
        (kwargs["workspace"] / "repaired.py").write_text("repaired = True\n")
        result = {
            "outcome": "incomplete",
            "markdown": "Repaired the observed candidate failure; no formal measurement.",
            "artifacts": [],
            "measurements": [],
        }
        from research_loop.core import _write

        _write(kwargs["run_dir"] / "response.json", result)
        publish_completed_response(kwargs["run_dir"], result)
        if self.interrupt:
            self.interrupt = False
            raise ProcessLost()
        return result


class RecoverableHarness(RecordedHarness):
    def __init__(self):
        super().__init__()
        self.repair = RepairHarness()
        self.recoveries = []
        self.validations = []
        self.exhausted = False
        self.unknown = False

    def response(self, phase, context, kwargs):
        if phase == "execute":
            if self.unknown:
                raise HarnessError("Unknown external execution state")
            raise RecoverableExecutionError(
                "Known terminal candidate failure",
                details={"cause": "candidate failure", "terminal_receipt": "job-1"},
            )
        return super().response(phase, context, kwargs)

    def recover_execution(self, failure, *, workspace, run_dir, context, invoke):
        self.recoveries.append({"failure": failure, "context": context})
        if self.exhausted:
            return {
                "outcome": "incomplete",
                "markdown": "Recovery budget exhausted; terminal failure evidence retained.",
                "artifacts": [],
                "measurements": [],
            }
        invoke(
            "repair-1",
            {"debug": {"purpose": "Repair the known failure", "failure": failure}},
            run_dir / "repair-1",
            self.repair,
        )
        result_path = workspace / "result.json"
        result_path.write_text('{"quality": 0.9}\n')
        return {
            "outcome": "completed",
            "markdown": "Formal fixed-protocol result verified after recovery.",
            "artifacts": [{"path": "result.json", "version": "v1"}],
            "measurements": [
                {
                    "candidate_id": "fixed",
                    "artifact": {"path": "result.json", "version": "v1"},
                    "evaluation_id": "fixed",
                    "comparison_scope": "fixed-split-v1",
                    "metrics": [
                        {"name": "quality", "value": 0.9},
                        {"name": "cost", "value": 1},
                    ],
                    "evidence": [str(result_path)],
                }
            ],
        }

    def validate_completed(self, result, *, workspace, run_dir):
        self.validations.append(run_dir)
        if (
            result["outcome"] == "completed"
            and not (workspace / "result.json").exists()
        ):
            raise HarnessError("Missing formal result evidence")
        return result


class DebugRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "study"

    def study(self, **options):
        initialize(
            self.root,
            goal="Preserve frozen investigation through bounded candidate recovery.",
            strategy={"selection": "direct", "review": "full"},
            allow_write=True,
            global_evaluation=evaluation("fixed"),
            **options,
        )
        harness = RecoverableHarness()
        return Study(self.root, harness), harness

    def assert_completed(self, study, harness):
        state = study.status()
        self.assertEqual((state["status"], state["phase"]), ("stopped", "done"))
        self.assertEqual(harness.count("execute"), 1)
        self.assertEqual(len(harness.repair.calls), 1)
        self.assertEqual(harness.count("review"), 1)
        self.assertEqual(state["call_count"], 7)
        self.assertEqual(study.graph()[0]["status"], "reviewed")
        self.assertIsNone(state["debugging"])

    def test_known_failure_repairs_then_publishes_verified_formal_result(self):
        study, harness = self.study()
        study.run()
        self.assert_completed(study, harness)
        original = self.root / "iterations/0001/execute/attempt-001"
        recovery = original.parent / "recovery"
        self.assertEqual(
            read(original / "failure.json")["details"]["cause"], "candidate failure"
        )
        self.assertFalse((original / "completed.json").exists())
        self.assertEqual(
            read(recovery / "returned.json")["failure_ref"],
            str((original / "failure.json").relative_to(self.root)),
        )
        self.assertEqual(
            study.status()["calls"]["execute"], str(recovery.relative_to(self.root))
        )
        self.assertEqual(harness.validations, [recovery])
        self.assertEqual(harness.recoveries[0]["context"]["budget"]["calls_left"], 95)
        repair_context = read(harness.repair.calls[0]["run_dir"] / "context.json")
        self.assertEqual(
            repair_context["plan"], read(self.root / study.status()["plan_ref"])
        )
        self.assertEqual(
            repair_context["debug"]["failure"], harness.recoveries[0]["failure"]
        )
        self.assertEqual(
            study.frontiers()["global"]["fixed"]["candidate_ids"], ["0001:fixed"]
        )

    def test_repair_and_review_receive_the_original_failure_diagnostics(self):
        study, harness = self.study()
        harness.diagnostic_context = lambda iteration: {
            "iteration": iteration,
            "owner": "workspace",
            "logs": [{"log_id": "registered-native-log", "tail": "actual failure"}],
        }
        study.run()
        self.assert_completed(study, harness)
        repair_context = read(harness.repair.calls[0]["run_dir"] / "context.json")
        review_context = next(
            call["context"] for call in harness.calls if call["phase"] == "review"
        )
        for context in (repair_context, review_context):
            diagnostic = context["diagnostics"]
            self.assertEqual(diagnostic["server"]["iteration"], "0001")
            self.assertEqual(diagnostic["server"]["logs"][0]["tail"], "actual failure")
            original = diagnostic["attempts"][0]["record"]
            self.assertEqual(original["owner"], "workspace")
            self.assertEqual(original["effects"], "reconciled")
            self.assertIn(
                "Known terminal candidate failure", str(original["cause_chain"])
            )

    def test_adapter_budget_exhaustion_returns_incomplete_for_review(self):
        study, harness = self.study()
        harness.exhausted = True
        state = study.run()
        result = read(self.root / state["execution_ref"])
        self.assertEqual(
            (result["outcome"], result["measurements"]), ("incomplete", [])
        )
        self.assertEqual(harness.count("execute"), 1)
        self.assertEqual(harness.count("review"), 1)
        self.assertEqual(len(harness.repair.calls), 0)
        self.assertEqual(state["call_count"], 6)
        self.assertEqual(study.frontiers()["global"]["fixed"]["candidate_ids"], [])

    def test_interrupted_debug_replays_completed_repair_without_new_call(self):
        study, harness = self.study()
        harness.repair.interrupt = True
        with self.assertRaises(ProcessLost):
            study.run()
        self.assertTrue(study.status()["debugging"])
        Study(self.root, harness).run()
        self.assert_completed(study, harness)
        self.assertEqual(len(harness.recoveries), 2)

    def test_returned_handler_response_recovers_without_rerunning_handler(self):
        from research_loop import core

        study, harness = self.study()
        write = core._write

        def interrupt(path, value):
            write(path, value)
            if path.name == "returned.json":
                raise ProcessLost()

        with patch.object(core, "_write", side_effect=interrupt):
            with self.assertRaises(ProcessLost):
                study.run()
        Study(self.root, harness).run()
        self.assert_completed(study, harness)
        self.assertEqual(len(harness.recoveries), 1)

    def test_completed_recovery_recovers_before_state_binding(self):
        study, harness = self.study()
        save = study._save

        def interrupt(state):
            if state["calls"].get("execute", "").endswith("/recovery"):
                raise ProcessLost()
            save(state)

        with patch.object(study, "_save", side_effect=interrupt):
            with self.assertRaises(ProcessLost):
                study.run()
        Study(self.root, harness).run()
        self.assert_completed(study, harness)
        self.assertEqual(len(harness.recoveries), 1)
        self.assertEqual(len(harness.validations), 2)

    def test_cached_recovery_revalidates_before_review(self):
        study, harness = self.study()
        save = study._save

        def interrupt(state):
            if state["calls"].get("execute", "").endswith("/recovery"):
                raise ProcessLost()
            save(state)

        with patch.object(study, "_save", side_effect=interrupt):
            with self.assertRaises(ProcessLost):
                study.run()
        (self.root / "iterations/0001/workspace/result.json").unlink()
        with self.assertRaisesRegex(HarnessError, "Missing formal result"):
            Study(self.root, harness).run()
        self.assertEqual(harness.count("review"), 0)
        self.assertEqual(len(harness.recoveries), 1)

    def test_failure_receipt_recovers_before_debug_state_binding(self):
        study, harness = self.study()
        save = study._save

        def interrupt(state):
            if state.get("debugging"):
                raise ProcessLost()
            save(state)

        with patch.object(study, "_save", side_effect=interrupt):
            with self.assertRaises(ProcessLost):
                study.run()
        Study(self.root, harness).run()
        self.assert_completed(study, harness)

    def test_final_recovery_validation_failure_requires_explicit_reconciliation(self):
        study, harness = self.study()
        validate = harness.validate_completed

        def reject(*args, **kwargs):
            raise HarnessError("Formal recovery evidence requires reconciliation")

        harness.validate_completed = reject
        with self.assertRaisesRegex(HarnessError, "requires reconciliation"):
            study.run()
        self.assertFalse(
            (self.root / "iterations/0001/execute/recovery/completed.json").exists()
        )
        with self.assertRaisesRegex(StudyError, "explicitly retry"):
            study.run()
        self.assertEqual(harness.count("review"), 0)
        harness.validate_completed = validate
        study.retry()
        study.run()
        self.assert_completed(study, harness)
        self.assertEqual(len(harness.recoveries), 1)

    def test_repair_agent_call_consumes_existing_call_budget(self):
        study, harness = self.study(max_calls=5)
        state = study.run()
        self.assertEqual(
            (state["stop_reason"], state["call_count"]), ("call_budget", 5)
        )
        self.assertTrue(state["debugging"])
        self.assertEqual(len(harness.repair.calls), 0)
        self.assertEqual(harness.count("review"), 0)

    def test_every_repair_refreshes_budget_and_reserves_one_review_call(self):
        from research_loop.core import BudgetExhausted

        study, harness = self.study(max_calls=7)
        remaining_calls = []

        def recover(failure, *, workspace, run_dir, context, invoke):
            for index in range(2):
                remaining_calls.append(invoke.budget_status()["calls_left"])
                try:
                    invoke(
                        f"repair-{index}",
                        {},
                        run_dir / f"repair-{index}",
                        harness.repair,
                    )
                except BudgetExhausted:
                    return {
                        "outcome": "incomplete",
                        "markdown": "Retain failed evidence for Review.",
                        "artifacts": [],
                        "measurements": [],
                    }
            self.fail("A second repair consumed the reserved Review call")

        harness.recover_execution = recover
        state = study.run()
        self.assertEqual(remaining_calls, [2, 1])
        self.assertEqual(len(harness.repair.calls), 1)
        self.assertEqual(harness.count("review"), 1)
        self.assertEqual(state["call_count"], 7)
        context = read(harness.repair.calls[0]["run_dir"] / "context.json")
        self.assertEqual(context["budget"]["calls_left"], 2)
        self.assertIsNone(state["debugging"])

    def test_repair_agent_call_obeys_existing_wall_budget(self):
        study, harness = self.study(max_seconds=10)
        clock = [0.0]
        recover = harness.recover_execution

        def expire(*args, **kwargs):
            clock[0] = 10.0
            return recover(*args, **kwargs)

        harness.recover_execution = expire
        with patch("research_loop.core.time.monotonic", side_effect=lambda: clock[0]):
            state = study.run()
        self.assertEqual(state["stop_reason"], "time_budget")
        self.assertEqual(len(harness.repair.calls), 0)
        self.assertEqual(harness.count("review"), 0)

    def test_unknown_execution_failure_does_not_enter_recovery(self):
        study, harness = self.study()
        harness.unknown = True
        with self.assertRaisesRegex(HarnessError, "Unknown external"):
            study.run()
        failure = read(self.root / study.status()["diagnostic_ref"])
        self.assertEqual(failure["owner"], "pipeline")
        self.assertEqual(failure["effects"], "unknown")
        self.assertEqual(failure["iteration"], 1)
        with self.assertRaisesRegex(StudyError, "explicitly retry"):
            study.run()
        self.assertEqual(len(harness.recoveries), 0)
        self.assertEqual(harness.count("execute"), 1)


if __name__ == "__main__":
    unittest.main()
