"""Train a small real CNN without access to held-out labels or grading code."""

import argparse
import hashlib
import importlib.metadata
import json
import random
import shutil
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from PIL import Image
from torch import nn
from torch.utils.data import DataLoader, Dataset


class Images(Dataset):
    def __init__(self, data, split):
        self.frame = pd.read_csv(data / f"{split}.csv")
        self.directory = data / split

    def __len__(self):
        return len(self.frame)

    def __getitem__(self, index):
        row = self.frame.iloc[index]
        with Image.open(self.directory / row.id) as image:
            array = np.asarray(image.convert("RGB"), dtype=np.float32).copy()
        tensor = torch.from_numpy(array).permute(2, 0, 1) / 255.0
        label = float(row.has_cactus) if "has_cactus" in self.frame else -1.0
        return tensor, label


def tiny_cnn():
    return nn.Sequential(
        nn.Conv2d(3, 32, 3, padding=1),
        nn.ReLU(),
        nn.MaxPool2d(2),
        nn.Conv2d(32, 64, 3, padding=1),
        nn.ReLU(),
        nn.MaxPool2d(2),
        nn.Conv2d(64, 64, 3, padding=1),
        nn.ReLU(),
        nn.AdaptiveAvgPool2d(1),
        nn.Flatten(),
        nn.Linear(64, 1),
    )


def main(args):
    started = time.perf_counter()
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.set_num_threads(args.threads)
    device = torch.device(args.device)
    if device.type == "cuda":
        torch.cuda.init()
        torch.cuda.reset_peak_memory_stats(device)

    def sync():
        if device.type == "cuda":
            torch.cuda.synchronize(device)

    args.output.mkdir(parents=True, exist_ok=False)
    source = Path(__file__)
    shutil.copy2(source, args.output / "train.py")
    metadata = json.loads((args.data / "metadata.json").read_text())
    dataset = Images(args.data, "train")
    loader = DataLoader(
        dataset, batch_size=args.batch_size, shuffle=True, num_workers=0
    )
    model = tiny_cnn().to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    criterion = nn.BCEWithLogitsLoss()
    sync()
    initialized = time.perf_counter()
    initialization_seconds = initialized - started
    first_batch_seconds = None
    steady_samples = 0
    steady_started = None
    losses = []
    model.train()
    for _ in range(args.epochs):
        loss_sum = 0.0
        for images, labels in loader:
            images = images.to(device)
            labels = labels.to(device, dtype=torch.float32)
            optimizer.zero_grad(set_to_none=True)
            logits = model(images).squeeze(1)
            loss = criterion(logits, labels)
            loss.backward()
            optimizer.step()
            loss_sum += loss.detach().item() * len(images)
            if first_batch_seconds is None:
                sync()
                steady_started = time.perf_counter()
                first_batch_seconds = steady_started - initialized
            else:
                steady_samples += len(images)
        losses.append(loss_sum / len(dataset))
    sync()
    training_finished = time.perf_counter()
    train_peak = (
        torch.cuda.max_memory_allocated(device) if device.type == "cuda" else None
    )
    config = {
        key: str(value) if isinstance(value, Path) else value
        for key, value in vars(args).items()
    }
    torch.save(
        {"model": model.state_dict(), "config": config}, args.output / "checkpoint.pt"
    )
    model.eval()
    with torch.no_grad():
        for split in ["dev", "test"]:
            inputs = Images(args.data, split)
            prediction_loader = DataLoader(
                inputs, batch_size=args.batch_size, num_workers=0
            )
            predictions = []
            for images, _ in prediction_loader:
                predictions.extend(
                    model(images.to(device)).squeeze(1).sigmoid().cpu().tolist()
                )
            pd.DataFrame({"id": inputs.frame.id, "has_cactus": predictions}).to_csv(
                args.output / f"{split}-predictions.csv", index=False
            )
    report = {
        "config": config,
        "data": metadata,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "versions": {
            name: importlib.metadata.version(name)
            for name in ["torch", "numpy", "pandas", "scikit-learn", "pillow"]
        },
        "gpu": torch.cuda.get_device_name(device) if device.type == "cuda" else None,
        "losses": losses,
        "initialization_seconds_excluding_imports": initialization_seconds,
        "first_batch_seconds_after_initialization": first_batch_seconds,
        "seconds_to_first_batch_excluding_imports": initialization_seconds
        + first_batch_seconds,
        "training_seconds": training_finished - initialized,
        "steady_samples_per_second": steady_samples
        / (training_finished - steady_started)
        if steady_samples
        else None,
        "peak_training_cuda_allocated_bytes": train_peak,
        "startup_validation_seconds": 0.0,
        "startup_validation_bytes_read": 0,
        "metadata_and_csv_bytes_before_first_batch": (args.data / "metadata.json")
        .stat()
        .st_size
        + (args.data / "train.csv").stat().st_size,
        "image_bytes_before_first_batch": "one training batch; compressed bytes not instrumented",
        "gpu_utilization": "not measured internally; sample externally",
        "wall_seconds_excluding_imports": time.perf_counter() - started,
        "checkpoint": "checkpoint.pt",
    }
    (args.output / "run.json").write_text(json.dumps(report, indent=2) + "\n")
    timing_keys = [
        "initialization_seconds_excluding_imports",
        "first_batch_seconds_after_initialization",
        "seconds_to_first_batch_excluding_imports",
        "training_seconds",
        "steady_samples_per_second",
        "peak_training_cuda_allocated_bytes",
        "startup_validation_seconds",
        "startup_validation_bytes_read",
        "metadata_and_csv_bytes_before_first_batch",
        "image_bytes_before_first_batch",
        "gpu_utilization",
        "wall_seconds_excluding_imports",
    ]
    (args.output / "timing.json").write_text(
        json.dumps({key: report[key] for key in timing_keys}, indent=2) + "\n"
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--model", choices=["tiny-cnn"], default="tiny-cnn")
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--lr", type=float, default=0.001)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()
    if args.epochs < 1 or args.batch_size < 1:
        parser.error("epochs and batch-size must be positive")
    main(args)
