"""Reject malformed prediction artifacts before calling the fixed official grader."""

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from evaluate import InvalidSubmissionError, official_grade


class PredictionBoundaryTest(unittest.TestCase):
    def test_invalid_ids_and_probabilities_never_reach_grading_source(self):
        answers = pd.DataFrame({"id": ["a.jpg", "b.jpg"], "has_cactus": [0, 1]})
        invalid = [
            {"id": ["a.jpg", "a.jpg"], "has_cactus": [0.1, 0.9]},
            {"id": ["a.jpg", "other.jpg"], "has_cactus": [0.1, 0.9]},
            {"id": ["a.jpg", "b.jpg"], "has_cactus": [float("nan"), 0.9]},
            {"id": ["a.jpg", "b.jpg"], "has_cactus": [float("inf"), 0.9]},
            {"id": ["a.jpg", "b.jpg"], "has_cactus": [-0.1, 0.9]},
        ]
        with tempfile.TemporaryDirectory() as root:
            for values in invalid:
                with (
                    self.subTest(values=values),
                    self.assertRaises(InvalidSubmissionError),
                ):
                    official_grade(pd.DataFrame(values), answers, Path(root))


if __name__ == "__main__":
    unittest.main()
