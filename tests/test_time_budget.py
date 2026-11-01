import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_loop import Study, initialize
from test_core import RecordedHarness, read


class TimeBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "study"

    def study(self, **options):
        initialize(
            self.root,
            goal="A continuing bounded investigation.",
            strategy={"selection": "direct", "review": "full"},
            **options,
        )
        harness = RecordedHarness()
        harness.stop_after = 1000
        return Study(self.root, harness), harness

    def test_uncapped_counts_persist_and_each_run_still_honors_requested_iterations(
        self,
    ):
        study, harness = self.study(max_iterations=None, max_calls=None)
        self.assertIsNone(read(self.root / "study.json")["max_iterations"])
        self.assertIsNone(read(self.root / "study.json")["max_calls"])
        first = study.run()
        self.assertEqual(
            (first["iteration"], first["status"], first["stop_reason"]),
            (1, "ready", None),
        )
        self.assertEqual(harness.count("execute"), 1)
        second = study.run()
        self.assertEqual(
            (second["iteration"], second["status"], second["stop_reason"]),
            (2, "ready", None),
        )
        self.assertEqual(second["call_count"], 12)
        self.assertEqual(harness.count("execute"), 2)
        self.assertEqual(
            [node["status"] for node in study.graph()], ["reviewed", "reviewed"]
        )
        selection_calls = [
            call for call in harness.calls if call["phase"] in ("proposal", "judge")
        ]
        self.assertTrue(
            all(
                call["context"]["budget"]["calls_left"] is None
                for call in selection_calls
            )
        )

    def test_time_budget_stops_next_call_with_uncapped_counts(self):
        study, harness = self.study(max_iterations=None, max_calls=None, max_seconds=10)
        clock = [0.0]

        def consume_time(call):
            clock[0] = 11.0

        harness.after = consume_time
        with patch("research_loop.core.time.monotonic", side_effect=lambda: clock[0]):
            state = study.run(iterations=2)
        self.assertEqual(
            (state["status"], state["stop_reason"]), ("stopped", "time_budget")
        )
        self.assertEqual(state["call_count"], 1)
        self.assertEqual(harness.count("proposal"), 1)
        self.assertEqual(harness.count("execute"), 0)
        self.assertGreaterEqual(state["elapsed_seconds"], 10)

    def test_uncapped_iterations_preserve_explicit_call_cap(self):
        study, harness = self.study(max_iterations=None, max_calls=1)
        state = study.run()
        self.assertEqual(state["stop_reason"], "call_budget")
        self.assertEqual(len(harness.calls), 1)

    def test_uncapped_calls_preserve_explicit_iteration_cap(self):
        study, harness = self.study(max_iterations=1, max_calls=None)
        state = study.run(iterations=2)
        self.assertEqual(state["stop_reason"], "iteration_budget")
        self.assertEqual(harness.count("execute"), 1)

    def test_uncapped_counts_preserve_review_stop_decision(self):
        study, harness = self.study(max_iterations=None, max_calls=None)
        harness.stop_after = 1
        state = study.run(iterations=2)
        self.assertEqual(state["stop_reason"], "review")
        self.assertEqual(harness.count("review"), 1)

    def test_none_is_only_valid_for_iteration_and_call_limits(self):
        for options in (
            {"max_seconds": None},
            {"max_seconds": 0},
            {"max_seconds": True},
            {"max_iterations": 0},
            {"max_calls": False},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                initialize(self.root, goal="A valid goal.", **options)


if __name__ == "__main__":
    unittest.main()
