"""One-time metadata/official-grader checks; never called by training startup."""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split

from evaluate import InvalidSubmissionError, official_grade
from prepare import public_split


def check(root):
    controller = root / "controller"
    raw = controller / "mirror"
    public = pd.read_csv(raw / "public-train.csv")
    private = pd.read_csv(raw / "private-test.csv")
    # Original Kaggle train.csv is ID ordered. Check prepared IDs and labels
    # against the exact official split under that ordering; no image contents read.
    original_order = (
        pd.concat([public, private]).sort_values("id").reset_index(drop=True)
    )
    expected_public, expected_private = train_test_split(
        original_order, test_size=0.19, random_state=0
    )
    pd.testing.assert_frame_equal(public, expected_public.reset_index(drop=True))
    pd.testing.assert_frame_equal(private, expected_private.reset_index(drop=True))
    train, dev = public_split(public)
    permutation = np.random.RandomState(0).permutation(len(public))
    dev_count = int(np.ceil(len(public) * 0.20))
    assert dev.index.tolist() == permutation[:dev_count].tolist()
    assert train.index.tolist() == permutation[dev_count:].tolist()
    assert set(train.id).isdisjoint(dev.id)
    assert set(train.id) | set(dev.id) == set(public.id)
    assert set(train.id).isdisjoint(private.id)
    for split, expected in [("train", train), ("dev", dev), ("test", private)]:
        visible = pd.read_csv(root / "data" / f"{split}.csv")
        assert visible.id.tolist() == expected.id.tolist()
        assert list(visible.columns) == (
            ["id", "has_cactus"] if split == "train" else ["id"]
        )
    # Tiny synthetic frames test only grading semantics, never training data.
    answers = pd.DataFrame({"id": ["a", "b", "c", "d"], "has_cactus": [0, 0, 1, 1]})
    predictions = pd.DataFrame(
        {"id": ["d", "b", "a", "c"], "has_cactus": [0.8, 0.1, 0.2, 0.9]}
    )
    assert official_grade(predictions, answers, controller) == 1.0
    predictions.loc[0, "has_cactus"] = 1.1
    try:
        official_grade(predictions, answers, controller)
    except InvalidSubmissionError:
        pass
    else:
        raise AssertionError("Official grader accepted an out-of-range probability")
    print(
        "PASS: official split exact ID/label parity under original sorted IDs; dev partition and unlabeled views; official grader ordering/range semantics"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    check(parser.parse_args().root)
