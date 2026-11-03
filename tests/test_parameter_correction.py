import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from research_loop import Study, StudyError, initialize
from research_loop.correction import ParameterError, safe_errors
from research_loop.harness import HarnessConfig, HarnessError
from research_loop.protocol import SCHEMAS
from test_core import ProcessLost, RecordedHarness, evaluation, read


class ParameterCorrectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "study"

    def study(self, **options):
        initialize(
            self.root,
            goal="Preserve the frozen research task.",
            strategy={"selection": "direct", "review": "full"},
            **options,
        )
        harness = RecordedHarness()
        return Study(self.root, harness), harness

    def test_invalid_plan_is_corrected_with_same_context_and_retained_reply(self):
        study, harness = self.study(max_calls=12)
        original = harness.response
        invalid = [True]

        def response(phase, context, kwargs):
            result = original(phase, context, kwargs)
            if phase == "plan" and invalid:
                invalid.pop()
                result["timeout_seconds"] = 0
            return result

        harness.response = response
        state = study.run()
        plans = [call for call in harness.calls if call["phase"] == "plan"]
        self.assertEqual(len(plans), 2)
        self.assertEqual(plans[0]["context"], plans[1]["context"])
        self.assertEqual(plans[0]["schema"], plans[1]["schema"])
        rejected = read(plans[0]["run_dir"] / "rejection.json")
        self.assertEqual(
            rejected["response"], read(plans[0]["run_dir"] / "response.json")
        )
        self.assertIn('\\"timeout_seconds\\": 0', plans[1]["prompt"])
        self.assertIn("greater_than_equal", plans[1]["prompt"])
        self.assertNotIn("input", rejected["errors"][0])
        self.assertNotIn("ctx", rejected["errors"][0])
        self.assertEqual(state["call_count"], len(harness.calls))
        self.assertEqual(harness.count("execute"), 1)
        self.assertTrue((self.root / "iterations/0001/publication.json").exists())

    def test_repeated_rejection_exhausts_existing_call_budget(self):
        study, harness = self.study(max_calls=6)
        original = harness.response

        def response(phase, context, kwargs):
            result = original(phase, context, kwargs)
            if phase == "plan":
                result["markdown"] = " "
            return result

        harness.response = response
        state = study.run()
        self.assertEqual(
            (state["status"], state["stop_reason"]), ("stopped", "call_budget")
        )
        self.assertEqual(state["call_count"], 6)
        self.assertEqual(harness.count("plan"), 3)
        self.assertEqual(harness.count("execute"), 0)
        self.assertEqual(
            len(list(self.root.glob("iterations/0001/plan/attempt-*/rejection.json"))),
            3,
        )
        retained = self.root / state["calls"][state["last_call_key"]]
        self.assertTrue((retained / "rejection.json").exists())

    def test_rejection_does_not_reset_elapsed_time(self):
        study, harness = self.study(max_seconds=5)
        clock = [0.0]
        original = harness.response

        def response(phase, context, kwargs):
            result = original(phase, context, kwargs)
            if phase == "plan":
                result["markdown"] = " "
                clock[0] += 6
            return result

        harness.response = response
        with patch("research_loop.core.time.monotonic", side_effect=lambda: clock[0]):
            state = study.run()
        self.assertEqual(state["stop_reason"], "time_budget")
        self.assertEqual(state["elapsed_seconds"], 6)
        self.assertEqual(harness.count("plan"), 1)
        self.assertEqual(harness.count("execute"), 0)

    def test_restart_reuses_durable_rejection_and_call_budget(self):
        study, harness = self.study(max_calls=4)
        original = harness.response

        def response(phase, context, kwargs):
            result = original(phase, context, kwargs)
            if phase == "plan":
                result["markdown"] = " "
            return result

        harness.response = response
        original_save = study._save
        rejection_saves = [0]

        def crash_after_rejection(state):
            original_save(state)
            key = state.get("last_call_key")
            if (
                key in state["calls"]
                and (self.root / state["calls"][key] / "rejection.json").exists()
            ):
                rejection_saves[0] += 1
                if rejection_saves[0] == 1:
                    raise ProcessLost()

        with patch.object(study, "_save", side_effect=crash_after_rejection):
            with self.assertRaises(ProcessLost):
                study.run()
        before = study.status()
        retained = self.root / before["calls"][before["last_call_key"]]
        raw = (retained / "response.json").read_bytes()
        state = Study(self.root, harness).run()
        self.assertEqual(state["stop_reason"], "call_budget")
        self.assertEqual(state["call_count"], before["call_count"])
        self.assertEqual(len(harness.calls), 4)
        self.assertEqual((retained / "response.json").read_bytes(), raw)

    def test_restart_includes_feedback_without_changing_scientific_context(self):
        study, harness = self.study(max_calls=12)
        original = harness.response
        first = [True]

        def response(phase, context, kwargs):
            result = original(phase, context, kwargs)
            if phase == "plan" and first:
                first.pop()
                result["markdown"] = " "
            return result

        harness.response = response
        original_save = study._save

        def crash_after_successor_commit(state):
            original_save(state)
            key = state.get("last_call_key")
            if (
                key in state["calls"]
                and (self.root / state["calls"][key] / "feedback.json").exists()
                and not (self.root / state["calls"][key] / "response.json").exists()
            ):
                raise ProcessLost()

        with patch.object(study, "_save", side_effect=crash_after_successor_commit):
            with self.assertRaises(ProcessLost):
                study.run()
        before = study.status()
        Study(self.root, harness).run()
        plans = [call for call in harness.calls if call["phase"] == "plan"]
        self.assertEqual(len(plans), 2)
        self.assertIn("Stage report must be nonempty", plans[1]["prompt"])
        self.assertEqual(plans[0]["context"], plans[1]["context"])
        self.assertGreater(study.status()["call_count"], before["call_count"])
        self.assertEqual(study.status()["call_count"], len(harness.calls) + 1)
        self.assertEqual(harness.count("execute"), 1)

    def test_adapter_allowance_cannot_renew_original_correction_deadline(self):
        study, harness = self.study(
            max_calls=None, max_seconds=1, harness=HarnessConfig(timeout_seconds=3)
        )
        harness.call_allowance = lambda remaining: 3600
        clock = [1000.0]
        original = harness.response

        def response(phase, context, kwargs):
            result = original(phase, context, kwargs)
            result["markdown"] = " "
            clock[0] += 2
            return result

        harness.response = response
        with (
            patch("research_loop.core.time.time", side_effect=lambda: clock[0]),
            patch("research_loop.core.time.monotonic", side_effect=lambda: clock[0]),
        ):
            state = study.run()
        self.assertEqual(state["stop_reason"], "time_budget")
        self.assertEqual(state["call_count"], 2)
        self.assertEqual([call["timeout_seconds"] for call in harness.calls], [3, 1])
        deadlines = [
            read(call["run_dir"] / "parameter-budget.json")["deadline"]
            for call in harness.calls
        ]
        self.assertEqual(deadlines, [1003.0, 1003.0])
        self.assertEqual(harness.count("execute"), 0)

    def test_incomplete_review_coverage_is_corrected_without_execute_replay(self):
        study, harness = self.study(global_evaluation=evaluation("global"))
        harness.measure = True
        (study.workspace / "result.json").write_text("{}")
        original = harness.response
        first = [True]

        def response(phase, context, kwargs):
            result = original(phase, context, kwargs)
            if phase == "review" and first:
                first.pop()
                result["candidate_reviews"] = []
            return result

        harness.response = response
        study.run()
        reviews = [call for call in harness.calls if call["phase"] == "review"]
        self.assertEqual(len(reviews), 2)
        self.assertIn("match current measurements exactly", reviews[1]["prompt"])
        self.assertEqual(harness.count("execute"), 1)
        self.assertTrue((self.root / "iterations/0001/publication.json").exists())

    def test_accepted_output_is_never_replaced_when_later_revalidation_fails(self):
        study, harness = self.study()
        study.run()
        state = study.status()
        plan = next(call for call in harness.calls if call["phase"] == "plan")
        attempt = plan["run_dir"]
        output = read(attempt / "response.json")
        output["references"] = ["invalid-reference"]
        (attempt / "response.json").write_text(json.dumps(output))
        count = len(harness.calls)
        with self.assertRaises(ValidationError):
            study._call(state, "plan", "plan-0", plan["context"], attempt.parent)
        self.assertEqual(len(harness.calls), count)
        self.assertFalse((attempt / "rejection.json").exists())

    def test_adapter_errors_are_never_parameter_feedback(self):
        for index, error in enumerate(
            (ValueError("pipeline"), HarnessError("adapter"), OSError("disk"))
        ):
            with self.subTest(error=type(error).__name__):
                self.root = self.root.parent / f"failure-{index}"
                study, harness = self.study()
                harness.before = lambda call: (_ for _ in ()).throw(error)
                with self.assertRaises(type(error)):
                    study.run()
                self.assertEqual(len(harness.calls), 1)
                self.assertFalse(
                    list(self.root.glob("iterations/*/*/attempt-*/rejection.json"))
                )

    def test_schema_hook_validation_error_is_not_caught(self):
        study, harness = self.study()
        try:
            SCHEMAS["critique"].validate({"markdown": 1})
        except ValidationError as error:
            failure = error
        harness.response_schema = lambda *args: (_ for _ in ()).throw(failure)
        with self.assertRaises(ValidationError):
            study.run()
        self.assertEqual(harness.calls, [])

    def test_execute_output_rejection_never_replays_execution(self):
        study, harness = self.study()
        original = harness.response

        def response(phase, context, kwargs):
            result = original(phase, context, kwargs)
            if phase == "execute":
                result["markdown"] = " "
            return result

        harness.response = response
        with self.assertRaises(ParameterError):
            study.run()
        with self.assertRaises(StudyError):
            Study(self.root, harness).run()
        self.assertEqual(harness.count("execute"), 1)
        self.assertEqual(harness.count("review"), 0)
        self.assertFalse(
            list(self.root.glob("iterations/*/execute/attempt-*/rejection.json"))
        )

    def test_missing_failed_or_live_receipt_blocks_correction(self):
        for status in (None, "failed", "running"):
            with self.subTest(status=status):
                self.root = self.root.parent / f"receipt-{status}"
                study, harness = self.study()
                original = harness.response

                def response(phase, context, kwargs):
                    result = original(phase, context, kwargs)
                    result["markdown"] = " "
                    return result

                def corrupt(call):
                    path = call["run_dir"] / "process.json"
                    if status is None:
                        path.unlink()
                    else:
                        path.write_text(json.dumps({"status": status}))

                harness.response, harness.after = response, corrupt
                with self.assertRaisesRegex(
                    StudyError, "successful stopped call receipt"
                ):
                    study.run()
                self.assertEqual(len(harness.calls), 1)

    def test_safe_errors_exclude_input_and_ctx_and_redact_known_secrets(self):
        try:
            SCHEMAS["plan"].validate({"markdown": "private-token"})
        except ValidationError as error:
            errors = safe_errors(error, secrets=("private-token",))
        serialized = json.dumps(errors)
        self.assertNotIn("private-token", serialized)
        self.assertNotIn('"input"', serialized)
        self.assertNotIn('"ctx"', serialized)
        self.assertEqual(
            safe_errors(ParameterError("secret=value"))[0]["message"],
            "secret=[REDACTED]",
        )


if __name__ == "__main__":
    unittest.main()
