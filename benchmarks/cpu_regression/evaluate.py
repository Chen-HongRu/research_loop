"""Trusted host scorer. Never import or execute candidate code."""

import json
import math
import sys
from pathlib import Path


def evaluate(predictions_path, labels_path, baseline_path):
    labels = json.loads(Path(labels_path).read_text())
    predictions = json.loads(Path(predictions_path).read_text())
    baseline = json.loads(Path(baseline_path).read_text())["prediction"]
    if (
        isinstance(baseline, bool)
        or not isinstance(baseline, (int, float))
        or not math.isfinite(baseline)
    ):
        raise ValueError("Trusted training-mean baseline must be finite")
    if not isinstance(predictions, list):
        raise ValueError("Predictions must be a list")
    expected = {row["id"]: row["target"] for row in labels}
    actual = {}
    for row in predictions:
        if not isinstance(row, dict) or set(row) != {"id", "prediction"}:
            raise ValueError("Each prediction requires exactly id and prediction")
        identifier, value = row["id"], row["prediction"]
        if not isinstance(identifier, str) or identifier in actual:
            raise ValueError("Prediction IDs must be unique strings")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Predictions must be finite numbers")
        if not math.isfinite(value):
            raise ValueError("Predictions must be finite numbers")
        actual[identifier] = value
    if actual.keys() != expected.keys():
        raise ValueError("Prediction IDs must exactly match development IDs")
    residuals = [actual[key] - target for key, target in expected.items()]
    mean = sum(expected.values()) / len(expected)
    squared_error = sum(error**2 for error in residuals)
    total = sum((target - mean) ** 2 for target in expected.values())
    metrics = {
        "mse": squared_error / len(expected),
        "mae": sum(abs(error) for error in residuals) / len(expected),
        "r2": 1 - squared_error / total,
        "baseline_mse": sum((baseline - target) ** 2 for target in expected.values())
        / len(expected),
        "baseline_mae": sum(abs(baseline - target) for target in expected.values())
        / len(expected),
    }
    if not all(math.isfinite(value) for value in metrics.values()):
        raise ValueError("Computed metrics must be finite")
    return {
        "valid": True,
        "metrics": metrics,
        "observations": [
            f"Scored {len(expected)} exact, unique development IDs",
            "baseline_mae/mse score the frozen training mean on these same development labels; r2 uses the development-mean denominator",
            "Development scores are adaptive feedback; no final evaluation was run",
        ],
    }


def main():
    try:
        result = evaluate(*sys.argv[1:])
    except (ValueError, KeyError, TypeError, OSError, OverflowError) as error:
        # Report only a validation category, never hidden labels or row contents.
        result = {
            "valid": False,
            "metrics": {},
            "observations": [f"Prediction validation failed: {type(error).__name__}"],
        }
    print(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    main()
