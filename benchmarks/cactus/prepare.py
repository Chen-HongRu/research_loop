"""Controller-only preparation of the pinned MLE-bench mirror and dev split."""

import argparse
import ast
import json
import shutil
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd
from sklearn.model_selection import train_test_split

MLE_COMMIT = "507f92e1138bb6e40dac5c6ee7a6758e6424bf97"
MIRROR_COMMIT = "01a3db85f9245ba64d87ed2a1170d486ee147e1f"
MIRROR = (
    "https://huggingface.co/datasets/TIGER-Lab/mle-bench/resolve/"
    f"{MIRROR_COMMIT}/data/aerial-cactus-identification/prepared"
)
UPSTREAM = f"https://raw.githubusercontent.com/openai/mle-bench/{MLE_COMMIT}"


def public_split(frame):
    """Fixed project dev split, additional to the mirror's official private split."""
    return train_test_split(frame, test_size=0.20, random_state=0)


def download(url, path):
    temporary = path.with_suffix(path.suffix + ".part")
    urllib.request.urlretrieve(url, temporary)
    temporary.replace(path)


def prepare(root):
    # Refuse an existing destination: published labels and views must not be mixed.
    root.mkdir(parents=True, exist_ok=False)
    controller = root / "controller"
    controller.mkdir(mode=0o700)
    public = root / "data"
    public.mkdir()
    raw = controller / "mirror"
    raw.mkdir()
    for scope, name in [
        ("public", "train.csv"),
        ("public", "train.zip"),
        ("public", "test.zip"),
        ("private", "test.csv"),
    ]:
        download(f"{MIRROR}/{scope}/{name}", raw / f"{scope}-{name}")
    labeled = pd.read_csv(raw / "public-train.csv")
    private = pd.read_csv(raw / "private-test.csv")
    assert len(labeled) == 14175 and len(private) == 3325
    assert labeled.id.is_unique and private.id.is_unique
    assert set(labeled.id).isdisjoint(private.id)
    train, dev = public_split(labeled)
    train.to_csv(public / "train.csv", index=False)
    dev[["id"]].to_csv(public / "dev.csv", index=False)
    private[["id"]].to_csv(public / "test.csv", index=False)
    dev.to_csv(controller / "dev-labels.csv", index=False)
    private.to_csv(controller / "test-labels.csv", index=False)
    # Read each image once for its actual publication, never for a full hash audit.
    for archive_name, partitions in [
        ("public-train.zip", [("train", train), ("dev", dev)]),
        ("public-test.zip", [("test", private)]),
    ]:
        with zipfile.ZipFile(raw / archive_name) as archive:
            members = {Path(name).name: name for name in archive.namelist()}
            for split, frame in partitions:
                destination = public / split
                destination.mkdir()
                for name in frame.id:
                    if Path(name).name != name:
                        raise ValueError("Image IDs must be plain file names")
                    with archive.open(members[name]) as source:
                        with (destination / name).open("wb") as target:
                            shutil.copyfileobj(source, target)
    # Retain upstream source unmodified. Evaluation executes only the exact helper
    # AST node and grade module with its one import bound to that helper.
    for name, upstream_path in [
        ("utils.py", "mlebench/competitions/utils.py"),
        ("grade.py", "mlebench/competitions/aerial-cactus-identification/grade.py"),
        ("prepare.py", "mlebench/competitions/aerial-cactus-identification/prepare.py"),
    ]:
        download(f"{UPSTREAM}/{upstream_path}", controller / name)
    helper = ast.parse((controller / "utils.py").read_text())
    assert any(
        isinstance(node, ast.FunctionDef) and node.name == "prepare_for_auroc_metric"
        for node in helper.body
    )
    metadata = {
        "mlebench_commit": MLE_COMMIT,
        "source": MIRROR,
        "provenance": "Third-party prepared mirror; not independently byte-verified against Kaggle",
        "official_split": {"test_size": 0.19, "random_state": 0},
        "dev_split": {"test_size": 0.20, "random_state": 0, "stratify": None},
        "counts": {"train": len(train), "dev": len(dev), "test": len(private)},
    }
    (public / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (root / "READY").write_text("prepared\n")
    return metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    print(json.dumps(prepare(parser.parse_args().root), indent=2))
