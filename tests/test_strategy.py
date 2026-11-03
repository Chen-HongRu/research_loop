import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_loop import Study, initialize
from research_loop.harness import HarnessError
from research_loop.strategy import validate_strategy
from test_core import RecordedHarness, evaluation, read


class StrategyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "study"

    def study(self, strategy=None, **options):
        initialize(
            self.root,
            goal="Measure one bounded intervention.",
            strategy=strategy,
            **options,
        )
        harness = RecordedHarness()
        return Study(self.root, harness), harness

    def observations(self):
        return read(self.root / "observations.json")

    def test_default_and_strict_configuration(self):
        self.assertEqual(validate_strategy(), {"selection": "debate", "review": "full"})
        for invalid in (
            {},
            {"selection": "direct"},
            {"selection": "other", "review": "full"},
            {"selection": "debate", "review": "other"},
            {"selection": "debate", "review": "full", "extra": True},
            [],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_strategy(invalid)
        study, harness = self.study()
        study.run()
        self.assertEqual(study.config["strategy"], validate_strategy())
        self.assertEqual(len(harness.calls), 10)
        self.assertNotIn("review_guidance", harness.calls[-1]["context"])

    def test_direct_concise_retains_evidence_review_and_publication(self):
        study, harness = self.study(
            {"selection": "direct", "review": "concise"},
            global_evaluation=evaluation("global"),
        )
        (study.workspace / "result.json").write_text("{}")
        harness.measure = True
        state = study.run()
        self.assertEqual(
            [c["phase"] for c in harness.calls],
            ["proposal", "proposal", "judge", "plan", "execute", "review"],
        )
        proposals = [c["context"] for c in harness.calls if c["phase"] == "proposal"]
        self.assertNotIn("proposals", proposals[0])
        self.assertNotIn("proposals", proposals[1])
        review = harness.calls[-1]
        self.assertTrue(review["context"]["plan"]["markdown"])
        self.assertEqual(review["context"]["plan"]["timeout_seconds"], 60)
        self.assertEqual(len(review["context"]["execute"]["measurements"]), 1)
        self.assertIn("route_research", review["context"])
        self.assertIn("global_research", review["context"])
        self.assertIn("Keep the Review concise", review["prompt"])
        self.assertEqual(
            read(self.root / state["review_ref"])["candidate_reviews"][0]["verified"],
            True,
        )
        self.assertEqual(study.graph()[0]["status"], "reviewed")
        self.assertIn(
            "Global observations after review 1",
            (self.root / "research.md").read_text(),
        )
        self.assertTrue(study.frontiers()["global"]["global"])
        observations = self.observations()
        self.assertTrue(observations["totals"]["complete"])
        self.assertEqual(
            [i["phase"] for i in observations["intervals"]], ["selection", "review"]
        )
        self.assertTrue(
            all(
                i["status"] == "succeeded" and i["wall_seconds"] > 0
                for i in observations["intervals"]
            )
        )
        self.assertAlmostEqual(
            observations["totals"]["overhead_wall_seconds"],
            sum(i["wall_seconds"] for i in observations["intervals"]),
        )

    def test_concise_cannot_omit_candidate_verification(self):
        study, harness = self.study(
            {"selection": "direct", "review": "concise"},
            global_evaluation=evaluation("global"),
            max_calls=12,
        )
        (study.workspace / "result.json").write_text("{}")
        harness.measure = True
        response = harness.response

        def missing_review(phase, context, kwargs):
            result = response(phase, context, kwargs)
            if phase == "review":
                result["candidate_reviews"] = []
            return result

        harness.response = missing_review
        self.assertEqual(study.run()["stop_reason"], "call_budget")
        self.assertFalse((self.root / "iterations/0001/publication.json").exists())
        reviews = [call for call in harness.calls if call["phase"] == "review"]
        self.assertGreater(len(reviews), 1)
        self.assertIn("match current measurements exactly", reviews[1]["prompt"])
        self.assertEqual(harness.count("execute"), 1)

    def test_failed_selection_resume_and_multiple_iterations_preserve_intervals(self):
        study, harness = self.study({"selection": "direct", "review": "full"})
        harness.stop_after = 2

        def fail(call):
            raise HarnessError("recorded failure")

        harness.before = fail
        with self.assertRaises(HarnessError):
            study.run()
        harness.before = None
        study.retry()
        study.run(iterations=2)
        observations = self.observations()
        self.assertEqual(
            [r["status"] for r in observations["runs"]], ["failed", "succeeded"]
        )
        self.assertEqual(
            [
                (i["phase"], i["iteration"], i["status"])
                for i in observations["intervals"]
            ],
            [
                ("selection", 1, "failed"),
                ("selection", 1, "succeeded"),
                ("review", 1, "succeeded"),
                ("selection", 2, "succeeded"),
                ("review", 2, "succeeded"),
            ],
        )
        self.assertTrue(observations["totals"]["complete"])

    def test_publication_retry_measures_completion_without_second_review(self):
        study, harness = self.study({"selection": "direct", "review": "concise"})
        publish = study._publish
        with patch.object(
            study, "_publish", side_effect=OSError("publication interrupted")
        ):
            with self.assertRaises(OSError):
                study.run()
        self.assertTrue((self.root / "iterations/0001/publication.json").exists())
        with patch.object(study, "_publish", wraps=publish):
            state = study.retry()
        self.assertEqual(state["phase"], "done")
        self.assertEqual(harness.count("review"), 1)
        review_intervals = [
            i for i in self.observations()["intervals"] if i["phase"] == "review"
        ]
        self.assertEqual(
            [i["status"] for i in review_intervals], ["failed", "succeeded"]
        )
        self.assertEqual(study.graph()[0]["status"], "reviewed")

    def test_unknown_crash_interval_is_not_zero_on_resume(self):
        study, _ = self.study({"selection": "direct", "review": "full"})
        orphan = {
            "id": "interrupted",
            "phase": "selection",
            "iteration": 1,
            "started_at": "2026-01-01T00:00:00+00:00",
            "ended_at": None,
            "wall_seconds": None,
            "status": "running",
        }
        (self.root / "observations.json").write_text(
            json.dumps({"version": 1, "runs": [], "intervals": [orphan]})
        )
        study.run()
        observations = self.observations()
        self.assertEqual(observations["intervals"][0]["status"], "incomplete")
        self.assertIsNone(observations["intervals"][0]["wall_seconds"])
        self.assertIsNone(observations["totals"]["selection_wall_seconds"])
        self.assertIsNone(observations["totals"]["overhead_wall_seconds"])
        self.assertFalse(observations["totals"]["complete"])


if __name__ == "__main__":
    unittest.main()
