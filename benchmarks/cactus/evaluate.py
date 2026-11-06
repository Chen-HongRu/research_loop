"""Controller-only execution of the pinned official Cactus ROC-AUC grader."""

import argparse
import ast
import json
import math
from pathlib import Path

import pandas as pd
from sklearn.metrics import roc_auc_score


class InvalidSubmissionError(Exception):
    """Official helper's exception interface (no change to scoring semantics)."""


def official_grade(submission, answers, controller):
    if set(submission.columns) != {"id", "has_cactus"}:
        raise InvalidSubmissionError("Expected exactly id and has_cactus columns")
    if (
        not submission.id.is_unique
        or not answers.id.is_unique
        or len(submission) != len(answers)
        or set(submission.id) != set(answers.id)
    ):
        raise InvalidSubmissionError(
            "Prediction IDs must exactly match the fixed split"
        )
    probabilities = pd.to_numeric(submission.has_cactus, errors="raise")
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in probabilities):
        raise InvalidSubmissionError(
            "Predictions must be finite probabilities in [0,1]"
        )
    namespace = {
        "pd": pd,
        "roc_auc_score": roc_auc_score,
        "InvalidSubmissionError": InvalidSubmissionError,
    }
    helper_source = ast.parse((controller / "utils.py").read_text())
    helper = next(
        node
        for node in helper_source.body
        if isinstance(node, ast.FunctionDef) and node.name == "prepare_for_auroc_metric"
    )
    exec(
        compile(ast.Module(body=[helper], type_ignores=[]), "official-utils", "exec"),
        namespace,
    )
    grade_source = ast.parse((controller / "grade.py").read_text())
    # Imports are supplied above; retain the complete official grade function.
    grade = next(
        node for node in grade_source.body if isinstance(node, ast.FunctionDef)
    )
    exec(
        compile(ast.Module(body=[grade], type_ignores=[]), "official-grade", "exec"),
        namespace,
    )
    return float(namespace["grade"](submission, answers))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controller", type=Path, required=True)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--split", choices=["dev", "test"], required=True)
    args = parser.parse_args()
    score = official_grade(
        pd.read_csv(args.submission),
        pd.read_csv(args.controller / f"{args.split}-labels.csv"),
        args.controller,
    )
    print(json.dumps({"split": args.split, "metric": "roc_auc", "score": score}))
