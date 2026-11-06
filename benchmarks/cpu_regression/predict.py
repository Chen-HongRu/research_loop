"""Run the candidate against public training labels and unlabeled features."""

import json
import sys
from pathlib import Path

from candidate import fit_predict


def main():
    inputs, output = map(Path, sys.argv[1:])
    train = json.loads((inputs / "train.json").read_text())
    dev = json.loads((inputs / "dev_features.json").read_text())
    predictions = fit_predict(train, dev)
    output.write_text(json.dumps(predictions, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
