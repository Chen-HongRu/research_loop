"""Prepare one external synthetic CPU study; private labels stay with the host."""

import argparse
import json
import math
import random
import secrets
import shutil
import sys
import time
import uuid
from pathlib import Path


def prepare(root, provider, model, python=None, seconds=900):
    if seconds <= 0:
        raise ValueError("Authorization seconds must be positive")
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    source, inputs, private = (root / name for name in ("source", "inputs", "private"))
    source.mkdir()
    inputs.mkdir()
    private.mkdir(mode=0o700)
    seed = secrets.randbits(64)
    rng = random.Random(seed)

    def examples(prefix, count):
        result = []
        for index in range(count):
            x0, x1, x2 = (rng.uniform(-2, 2) for _ in range(3))
            target = (
                1.5 * math.sin(2 * x0)
                + 0.8 * x1**2
                - 0.6 * x2
                + 0.3 * x0 * x1
                + rng.gauss(0, 0.05)
            )
            result.append(
                {
                    "id": f"{prefix}-{index:03}",
                    "features": [x0, x1, x2],
                    "target": target,
                }
            )
        return result

    train, dev = examples("train", 96), examples("dev", 48)
    (inputs / "train.json").write_text(json.dumps(train) + "\n")
    (inputs / "dev_features.json").write_text(
        json.dumps(
            [
                {key: value for key, value in row.items() if key != "target"}
                for row in dev
            ]
        )
        + "\n"
    )
    labels = private / "dev_labels.json"
    labels.write_text(
        json.dumps([{"id": row["id"], "target": row["target"]} for row in dev]) + "\n"
    )
    (private / "generation.json").write_text(json.dumps({"seed": seed}) + "\n")
    baseline = private / "training_mean.json"
    baseline.write_text(
        json.dumps({"prediction": sum(row["target"] for row in train) / len(train)})
        + "\n"
    )
    here = Path(__file__).resolve().parent
    for name in ("candidate.py", "predict.py"):
        shutil.copyfile(here / name, source / name)
    shutil.copyfile(here / "evaluate.py", private / "evaluate.py")
    interpreter = str(Path(python or sys.executable).resolve())
    identifier = str(uuid.uuid4())
    config = {
        "version": 1,
        "id": identifier,
        "goal": "Investigate whether a small nonlinear regression predictor generalizes better than the training-mean baseline, using two measured rounds",
        "constraints": [
            "Use only Python standard library and the supplied public inputs",
            "Keep fit_predict(train, dev) returning one finite prediction per development ID",
            "Development feedback is adaptive; do not claim final-test performance",
        ],
        "model": {"provider": provider, "id": model, "thinking": "off"},
        "authorization": {
            "id": f"cpu-{identifier}",
            "expiresAt": int((time.time() + seconds) * 1000),
            "slots": ["round-1", "round-2"],
            "jobSeconds": 30,
        },
        "maxRounds": 2,
        "minimumRounds": 2,
        "source": {"root": str(source), "files": ["candidate.py", "predict.py"]},
        "inputs": {
            "root": str(inputs),
            "version": f"synthetic-{identifier}",
            "feedback": "development",
        },
        "evaluation": {
            "id": "cpu-regression-development",
            "version": "2",
            "description": "Candidate mae/mse are scored on the exact development ID set; baseline_mae/baseline_mse score the frozen training-target mean on those same labels. Lower error is better. r2 uses the development-label mean denominator, so r2 <= 0 alone does not determine improvement over the training-mean baseline. Development feedback is adaptive, not a final-test estimate.",
            "predictCommand": [
                "{python}",
                "-B",
                "{candidate}/predict.py",
                "{inputs}",
                "{predictions}",
            ],
            "command": [
                "{python}",
                "-I",
                "-B",
                str(private / "evaluate.py"),
                "{predictions}",
                str(labels),
                str(baseline),
            ],
            "feedback": "development",
        },
        "python": interpreter,
        "tasks": [
            {
                "id": "regression-investigation",
                "source": "human",
                "question": "Choose and test a justified improvement to the baseline. Use the first measured result to decide the second investigation; do not assume improvement.",
                "constraints": [],
                "basedOn": [],
            }
        ],
    }
    (root / "study.json").write_text(json.dumps(config, indent=2) + "\n")
    return config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--python")
    parser.add_argument("--seconds", type=int, default=900)
    args = parser.parse_args()
    prepare(args.root, args.provider, args.model, args.python, args.seconds)
    print("Prepared external CPU study; host configuration is in study.json")


if __name__ == "__main__":
    main()
