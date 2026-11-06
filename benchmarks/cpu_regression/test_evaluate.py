"""Focused scorer contract tests with small synthetic rows."""

import json
import tempfile
import unittest
from pathlib import Path

from evaluate import evaluate


class ScorerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.labels = self.root / "labels.json"
        self.predictions = self.root / "predictions.json"
        self.baseline = self.root / "baseline.json"
        self.baseline.write_text(json.dumps({"prediction": 2}))
        self.labels.write_text(
            json.dumps([{"id": "a", "target": 1}, {"id": "b", "target": 3}])
        )

    def score(self, rows):
        self.predictions.write_text(json.dumps(rows))
        return evaluate(self.predictions, self.labels, self.baseline)

    def test_exact_ids_and_trusted_metric(self):
        report = self.score(
            [{"id": "b", "prediction": 3}, {"id": "a", "prediction": 1}]
        )
        self.assertTrue(report["valid"])
        self.assertEqual(
            report["metrics"],
            {"mse": 0, "mae": 0, "r2": 1, "baseline_mse": 1, "baseline_mae": 1},
        )

    def test_training_mean_comparison_is_distinct_from_r2(self):
        self.baseline.write_text(json.dumps({"prediction": 0}))
        report = self.score(
            [{"id": "a", "prediction": 0.5}, {"id": "b", "prediction": 0.5}]
        )
        self.assertLess(report["metrics"]["r2"], 0)
        self.assertLess(report["metrics"]["mse"], report["metrics"]["baseline_mse"])
        self.assertLess(report["metrics"]["mae"], report["metrics"]["baseline_mae"])

    def test_invalid_predictions(self):
        for rows in [
            [{"id": "a", "prediction": 1}],
            [{"id": "a", "prediction": 1}, {"id": "a", "prediction": 3}],
            [{"id": "a", "prediction": 1}, {"id": "other", "prediction": 3}],
            [{"id": "a", "prediction": float("nan")}, {"id": "b", "prediction": 3}],
            [{"id": "a", "prediction": True}, {"id": "b", "prediction": 3}],
            [
                {"id": "a", "prediction": 1, "metrics": {"r2": 100}},
                {"id": "b", "prediction": 3},
            ],
        ]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.score(rows)


if __name__ == "__main__":
    unittest.main()
