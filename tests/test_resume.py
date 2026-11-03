"""An operator may reopen a reviewed stop without resetting its authorization."""

import json
from pathlib import Path
import tempfile
import unittest

from research_loop import Study, StudyError, initialize
from test_core import RecordedHarness


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "study"
        initialize(
            self.root,
            goal="Bounded diagnostic",
            max_iterations=3,
            max_calls=30,
            max_seconds=600,
        )
        self.harness = RecordedHarness()
        self.study = Study(self.root, self.harness)
        self.study.run()

    def test_resume_preserves_evidence_and_budget_then_runs_next_iteration(self):
        before = self.study.status()
        # Fixture stops after Review with an unmet goal.
        self.assertEqual(before["stop_reason"], "review")
        publication = (self.root / "iterations/0001/publication.json").read_bytes()
        calls = len(self.harness.calls)
        after = self.study.resume(reason="User requested a further diagnostic")
        self.assertEqual(after["status"], "ready")
        for key in (
            "iteration",
            "call_count",
            "elapsed_seconds",
            "calls",
            "goal_status",
        ):
            self.assertEqual(after[key], before[key])
        self.assertEqual(len(self.harness.calls), calls)
        self.assertEqual(after["resumptions"][-1]["review_ref"], before["review_ref"])
        self.assertEqual(
            (self.root / "iterations/0001/publication.json").read_bytes(), publication
        )
        self.assertEqual(self.study.run()["iteration"], 2)

    def test_invalid_stops_and_exhausted_limits_are_not_reopened(self):
        before = self.study.status()
        for updates in (
            {"goal_status": "met"},
            {"status": "needs_attention", "phase": "execute"},
            {"stop_reason": "iteration_budget"},
            {"phase": "review"},
            {"active_attempt": "some-attempt"},
            {"call_count": 30},
            {"elapsed_seconds": 600},
            {"iteration": 3},
        ):
            with self.subTest(updates=updates):
                state = before | updates
                path = self.root / "state.json"
                path.write_text(json.dumps(state))
                with self.assertRaises(StudyError):
                    self.study.resume(reason="Continue")
                self.assertEqual(json.loads(path.read_text()), state)
        (self.root / "state.json").write_text(json.dumps(before))
        with self.assertRaises(ValueError):
            self.study.resume(reason=" ")

    def test_missing_publication_blocks_resume(self):
        (self.root / "iterations/0001/publication.json").unlink()
        with self.assertRaises(StudyError):
            self.study.resume(reason="Continue")


if __name__ == "__main__":
    unittest.main()
