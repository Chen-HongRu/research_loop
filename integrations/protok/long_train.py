"""Trusted fresh 10k lane; imported training mathematics belong to frozen source."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

STEPS = [2000, 4000, 6000, 8000, 10000]


def progress_record(config, step, phase, points, checkpoints, training=None):
    """Small journal; only fully evaluated checkpoints enter points."""
    return {
        "status": "incomplete",
        "run_id": config["run_id"],
        "protocol": config["protocol"],
        "source_identity": config["source_identity"],
        "recipe": config["recipe"],
        "scheduler_horizon": 50000,
        "optimizer_step": step,
        "phase": phase,
        "points": list(points),
        "checkpoints": list(checkpoints),
        "last_training": training,
    }


def run(config):
    source = Path(config["source"])
    sys.path[:0] = [str(source), str(source / "scripts")]
    import torch
    from research_loop_events import emit
    import train_autoresearch_se3 as frozen
    from train_ca_h_flow import FrozenHiddenDataset, check_cache_identity, optimizer_for
    from protok.model.autoresearch_se3 import build_model
    from protok.model.autoresearch_se3_loss import requires_backbone_targets
    from protok.utils.research import atomic_json, log_training_row
    from check_autoresearch_se3 import run_gate

    argv = [
        "--fixed-config",
        str(source / config["fixed_config"]),
        "--candidate-config",
        str(source / config["candidate_config"]),
        "--cache-root",
        config["cache_root"],
        "--output",
        config["output"],
        "--seed",
        "42",
        "--workers",
        str(config["workers"]),
    ]
    if config.get("target_root"):
        argv += ["--target-root", config["target_root"]]
    args, fixed, candidate = frozen.parse_args(argv)
    # Fixed recipe parsing stays unchanged; checkpoints identify this long lane.
    frozen.PROTOCOL = config["protocol"]
    args.max_steps = 10000
    if args.output.exists() and any(args.output.iterdir()):
        raise FileExistsError("Preserving existing long-training output")
    args.output.mkdir(parents=True, exist_ok=True)
    curve, checkpoints = [], []
    last_training = None

    def publish(step, phase):
        atomic_json(
            args.output / "long-curve.json",
            progress_record(config, step, phase, curve, checkpoints, last_training),
        )

    def observe(name, value, step, scope, tags=("dashboard", "unreviewed")):
        try:
            emit(name, value, tags=tags, step=step, context={"scope": scope})
        except (OSError, ValueError, TypeError):
            pass

    publish(0, "initializing")
    torch.set_float32_matmul_precision("highest")
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    device = torch.device("cuda")
    if torch.cuda.device_count() != 1:
        raise RuntimeError("One visible GPU required")
    gpu = torch.cuda.get_device_properties(device)
    if "H100" not in gpu.name.upper() or gpu.total_memory < 75 * 1024**3:
        raise RuntimeError("Frozen recipe requires H100 80GB")
    backbone = requires_backbone_targets(candidate)
    if backbone:
        from ca_h_esm3_objective import PackedESM3Dataset

        train = PackedESM3Dataset(args.cache_root, args.target_root, "train")
    else:
        train = FrozenHiddenDataset(
            args.cache_root, "train", hidden_dtype=torch.float16
        )
    val = FrozenHiddenDataset(args.cache_root, "val", hidden_dtype=torch.float16)
    check_cache_identity(train, fixed)
    if (
        not train.complete
        or not val.complete
        or train.identity != val.identity
        or (len(train), len(val)) != (449771, 4491)
    ):
        raise RuntimeError("Complete frozen train/full-validation cache required")
    model = build_model(candidate, 42).to(device)
    optimizer, scheduler = optimizer_for(model, fixed)
    atomic_json(args.output / "config.json", fixed)
    atomic_json(args.output / "candidate_config.json", candidate)
    atomic_json(
        args.output / "gate-initial.json", run_gate(model, seed=42, device=device)
    )
    stream = iter(frozen.make_train_loader(train, args, fixed["data_seed"], backbone))
    frozen.save_checkpoint(
        args.output, model, optimizer, scheduler, 0, fixed, candidate, args
    )
    publish(0, "compiling")
    forward = torch.compile(model, dynamic=True) if args.compile else model
    adapter = frozen.FixedSamplingAdapter(model, forward)
    model.train()
    for step in range(10000):
        values, _ = frozen.update(
            model,
            forward,
            optimizer,
            scheduler,
            stream,
            fixed,
            candidate,
            args,
            step,
            device,
        )
        completed = step + 1
        if completed % 10 == 0:
            last_training = {
                "optimizer_step": completed,
                **{f"train/{key}": float(value) for key, value in values.items()},
            }
            log_training_row(args.output, last_training)
            publish(completed, "training")
            observe("training/progress", completed, completed, "training")
            for name, value in last_training.items():
                if name != "optimizer_step":
                    observe(
                        name,
                        value,
                        completed,
                        "training",
                        ("dashboard", "unreviewed")
                        if "loss" in name
                        else ("internal",),
                    )
        if completed in STEPS:
            publish(completed, "validation")
            checkpoint = frozen.save_checkpoint(
                args.output,
                model,
                optimizer,
                scheduler,
                completed,
                fixed,
                candidate,
                args,
            )
            point = frozen.evaluate_scoped(
                adapter, val, fixed, args, completed, "full", device
            )
            if point["proteins"] != 4491:
                raise RuntimeError("Full validation coverage differs")
            curve.append(point)
            checkpoints.append(
                {
                    "optimizer_step": completed,
                    "path": config["output_reference"] + "/" + checkpoint.name,
                }
            )
            publish(completed, "training")
            # Only the already complete, coverage-checked full point is emitted.
            for name, value in point.items():
                if name not in {"optimizer_step", "step", "scope"}:
                    observe("validation/" + name, value, completed, "full")
    publish(10000, "final-gate")
    adam = sorted({int(state["step"]) for state in optimizer.state.values()})
    if adam != [10000] or scheduler.last_epoch != 10000:
        raise RuntimeError("Optimizer/scheduler completion clocks differ")
    atomic_json(
        args.output / "gate-final.json",
        run_gate(model, seed=42, device=device, optimizer_step=10000),
    )
    atomic_json(
        args.output / "long-completion.json",
        {
            "status": "complete",
            "run_id": config["run_id"],
            "protocol": config["protocol"],
            "source_identity": config["source_identity"],
            "recipe": config["recipe"],
            "optimizer_step": 10000,
            "scheduler_horizon": 50000,
            "points": curve,
            "checkpoints": checkpoints,
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "config", type=Path, nargs="?", default=Path("/workspace/long-config.json")
    )
    run(json.loads(parser.parse_args().config.read_text()))
