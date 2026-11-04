"""Three CUDA updates of unchanged Frontier0032-H / ESM3 on synthetic inputs.

This exercises the real model and five losses, not scientific performance or
dataset/checkpoint startup. Run in a fresh process with the ProTok source subset
listed in SOURCE_FILES. No teacher, dataset, checkpoint, logger or compiler runs.
"""

import argparse
import json
import math
from pathlib import Path
import sys
import time
import types


SOURCE_FILES = (
    "protok/model/frontier_h_esm3.py",
    "protok/model/autoresearch_se3.py",
    "protok/model/ca_h_esm3.py",
    "protok/model/ca_h_flow.py",
    "protok/model/ca_flow.py",
    "protok/model/cached_ca_flow.py",
    "protok/model/se3_egnn_layer.py",
    "protok/model/loss.py",
    "protok/data/frozen_backbone.py",
    "protok/data/frozen_hidden.py",
    "protok/utils/structure_metrics.py",
    "scripts/ca_h_esm3_objective.py",
    "minesm/models/vqvae.py",
    "minesm/utils/misc.py",
    "minesm/utils/residue_constants.py",
    "minesm/utils/types.py",
    "minesm/utils/constants/esm3.py",
    "minesm/utils/constants/physics.py",
    "minesm/utils/structure/affine3d.py",
    "minesm/utils/structure/protein_chain.py",
    "minesm/utils/structure/aligner.py",
    "minesm/utils/structure/metrics.py",
    "minesm/utils/structure/normalize_coordinates.py",
    "minesm/utils/structure/protein_structure.py",
)


def read_smoke_events(path):
    """Read only this run's small generated stream, with a hard 128 KiB limit."""
    with Path(path).open("rb") as stream:
        if stream.seek(0, 2) > 131072:
            raise ValueError("Synthetic smoke telemetry exceeds 128 KiB")
        stream.seek(0)
        raw = stream.read(131073)
    if len(raw) > 131072 or not raw.endswith(b"\n"):
        raise ValueError("Invalid synthetic smoke telemetry size or boundary")
    return [json.loads(line) for line in raw.splitlines()], len(raw)


def bind_source(root):
    """Load unchanged modules without unrelated Lightning package bootstraps."""
    root = root.resolve()
    missing = [name for name in SOURCE_FILES if not (root / name).is_file()]
    if missing:
        raise FileNotFoundError(f"Missing source files: {missing}")
    # ProTok model/data __init__ imports the full training application. Namespace
    # bindings only bypass those bootstraps; every executed module is original.
    for name in ("protok", "protok.model", "protok.data", "protok.utils"):
        if name in sys.modules:
            raise RuntimeError("Run this benchmark in a fresh Python process")
        package = types.ModuleType(name)
        package.__path__ = [str(root.joinpath(*name.split(".")))]
        sys.modules[name] = package
    sys.path[:0] = [str(root), str(root / "scripts")]


def run(args):
    started = time.perf_counter()
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required; CPU fallback is forbidden")
    try:
        # The remote fixture stages the unchanged canonical module under this
        # name, without requiring installation of the controller package.
        from research_loop_events import emit
    except ModuleNotFoundError as error:
        if error.name != "research_loop_events":
            raise
        from research_loop.telemetry import emit

    events_file = args.events_file or args.output.parent / "events.jsonl"
    events_file.parent.mkdir(parents=True, exist_ok=True)
    # A fresh stream prevents earlier runs' observations entering this receipt.
    with events_file.open("xb"):
        pass
    event_context = {"synthetic": True, "seed": args.seed}
    device = torch.device("cuda:0")
    torch.cuda.set_device(device)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    bind_source(args.source_root)

    from ca_h_esm3_objective import TERM_NAMES, esm3_terms
    from minesm.utils.constants.physics import BB_COORDINATES

    from protok.model.ca_flow import masked_center
    from protok.model.frontier_h_esm3 import build_model
    from protok.utils.structure_metrics import proper_rmsd

    config = dict(
        seed=args.seed,
        model=dict(hidden_dim=1024, dim=128, heads=4, head_dim=16, depth=3),
        sampling_denominator_min=0.01,
        value_mode="node_frame",
        coordinate_gain=4.0,
        pair_dim=16,
    )
    torch.cuda.reset_peak_memory_stats(device)
    model_started = time.perf_counter()
    model = build_model(config).to(device).train()
    optimizer = torch.optim.AdamW(
        [
            {"params": [p for p in model.parameters() if p.ndim >= 2]},
            {
                "params": [p for p in model.parameters() if p.ndim < 2],
                "weight_decay": 0.0,
            },
        ],
        lr=1e-4,
        betas=(0.9, 0.99),
        weight_decay=0.05,
        fused=True,
    )
    torch.cuda.synchronize(device)
    model_initialization_seconds = time.perf_counter() - model_started

    # Complete finite synthetic NCAC frames around a curved CA trace. Labels 4:24
    # are canonical amino acids in the original ESM3 sequence vocabulary.
    batch, length, k = 2, 16, 4
    generator = torch.Generator(device=device).manual_seed(20261022)
    hidden = torch.randn(batch, length, 1024, generator=generator, device=device)
    mask = torch.ones(batch, length, dtype=torch.bool, device=device)
    mask[0, -3:] = False
    index = torch.arange(length, dtype=torch.float32, device=device)
    ca = torch.stack(
        (2.3 * (1.7 * index).cos(), 2.3 * (1.7 * index).sin(), 1.5 * index), -1
    )
    ca = masked_center(ca[None].expand(batch, -1, -1).clone(), mask)
    coords = ca[:, :, None] + torch.tensor(BB_COORDINATES, device=device)
    coords = torch.where(mask[..., None, None], coords, 0.0)
    labels = torch.randint(4, 24, (batch, length), generator=generator, device=device)
    labels = torch.where(mask, labels, 1)
    positions = torch.arange(length, device=device).expand(batch, -1)
    lengths = torch.full((batch * k,), length, dtype=torch.long)
    expanded_mask = mask.repeat_interleave(k, 0)
    expanded_coords = coords.repeat_interleave(k, 0)
    expanded_labels = labels.repeat_interleave(k, 0)
    expanded_positions = positions.repeat_interleave(k, 0)
    target = (ca / 10).repeat_interleave(k, 0)
    before = model.head[-1].weight.detach().clone()
    torch.cuda.synchronize(device)
    initialization_seconds = time.perf_counter() - started
    records = []
    first_step_latency = None
    train_started = time.perf_counter()
    for step in range(3):
        step_started = time.perf_counter()
        optimizer.zero_grad(set_to_none=True)
        noise_generator = torch.Generator(device=device).manual_seed(20261022 + step)
        # Original draw_path RNG order and Proteina .02 U + .98 Beta(1.9, 1).
        noise = torch.randn(target.shape, generator=noise_generator, device=device)
        noise = masked_center(noise, expanded_mask)
        times = torch.rand(batch * k, generator=noise_generator, device=device)
        uniform = torch.rand(batch * k, generator=noise_generator, device=device) < 0.02
        times = torch.where(uniform, times, times.pow(1 / 1.9))
        state = (1 - times[:, None, None]) * noise + times[:, None, None] * target
        condition = model.prepare_condition(hidden, mask).repeat_interleave(k, 0)
        prediction = model.training_prediction(state, times, condition, expanded_mask)
        terms = esm3_terms(
            model.loss_fn,
            prediction,
            expanded_coords,
            expanded_labels,
            expanded_mask,
            expanded_positions,
            lengths,
        )
        if not torch.isfinite(terms).all():
            raise FloatingPointError(f"Nonfinite ESM3 terms at step {step + 1}")
        loss = terms.sum()
        loss.backward()
        missing = {name for name, p in model.named_parameters() if p.grad is None}
        if missing != model.unused_parameter_names:
            raise RuntimeError(f"Unexpected missing gradient set: {sorted(missing)}")
        if any(
            p.grad is not None and not torch.isfinite(p.grad).all()
            for p in model.parameters()
        ):
            raise FloatingPointError(f"Nonfinite gradient at step {step + 1}")
        optimizer.step()
        if any(not torch.isfinite(p).all() for p in model.parameters()):
            raise FloatingPointError(f"Nonfinite parameter at step {step + 1}")
        torch.cuda.synchronize(device)
        record = dict(
            step=step + 1,
            loss=float(loss.detach()),
            **dict(zip(TERM_NAMES, terms.detach().tolist(), strict=True)),
        )
        for name in ("loss", *TERM_NAMES):
            emit(
                "train/" + name,
                record[name],
                step=step + 1,
                tags=("dashboard", "training"),
                context=event_context,
                path=events_file,
            )
        # Step timing includes the small canonical telemetry writes.
        elapsed = time.perf_counter() - step_started
        if step == 0:
            first_step_latency = time.perf_counter() - started
        records.append({**record, "seconds": elapsed})
    training_seconds = time.perf_counter() - train_started
    if torch.equal(before, model.head[-1].weight):
        raise RuntimeError("Optimizer did not change the active CA head")
    model.eval()
    with torch.no_grad():
        prediction = model.training_prediction(
            state,
            times,
            model.prepare_condition(hidden, mask).repeat_interleave(k, 0),
            expanded_mask,
        )
    # One proper SO(3) fit of all valid N/CA/C atoms per protein, no scale or
    # reflection fit. This is a denoising diagnostic, not sampled evaluation.
    bbrmsd = [
        proper_rmsd(
            prediction["bb_pred"][row * k, mask[row]].cpu().numpy(),
            coords[row, mask[row]].cpu().numpy(),
        )
        for row in range(batch)
    ]
    if not all(math.isfinite(value) for value in bbrmsd):
        raise FloatingPointError("Nonfinite synthetic backbone diagnostic")
    torch.cuda.synchronize(device)
    result = dict(
        status="completed",
        synthetic=True,
        completed_steps=3,
        seed=args.seed,
        device=str(device),
        device_name=torch.cuda.get_device_name(device),
        torch_version=torch.__version__,
        cuda_version=torch.version.cuda,
        contract=dict(
            batch=batch,
            length=length,
            hidden_dim=1024,
            k=k,
            depth=3,
            precision="float32",
            tf32=False,
            compile=False,
            objective="equal sum of original five ESM3 terms; no FM loss",
            optimizer="AdamW lr=1e-4 betas=(.9,.99); matrix decay=.05",
            smoke_changes="synthetic B2/L16/K4, accumulation1, no warmup/scheduler",
        ),
        model_initialization_seconds=model_initialization_seconds,
        initialization_seconds=initialization_seconds,
        first_step_latency_seconds=first_step_latency,
        training_seconds=training_seconds,
        elapsed_seconds=time.perf_counter() - started,
        peak_memory_bytes=torch.cuda.max_memory_allocated(device),
        steady_state_proteins_per_second=batch
        * 2
        / sum(record["seconds"] for record in records[1:]),
        gpu_utilization="unmeasured",
        dataset_bytes_read=0,
        checkpoint_bytes_read=0,
        losses=records,
        synthetic_denoising_ncac_bbrmsd_A=bbrmsd,
        synthetic_denoising_ncac_bbrmsd_mean_A=sum(bbrmsd) / len(bbrmsd),
        diagnostic_protocol="proper SO(3) fit over all valid N/CA/C atoms, Angstrom; synthetic denoising only",
        timing_includes="canonical per-update telemetry writes; no dataset/checkpoint I/O",
    )
    for name, value, unit in (
        (
            "diagnostic/ncac_bbrmsd",
            result["synthetic_denoising_ncac_bbrmsd_mean_A"],
            "Angstrom",
        ),
        ("perf/initialization_seconds", initialization_seconds, "seconds"),
        ("perf/first_step_latency_seconds", first_step_latency, "seconds"),
        (
            "perf/steady_state_proteins_per_second",
            result["steady_state_proteins_per_second"],
            "proteins/second",
        ),
        ("perf/peak_memory_bytes", result["peak_memory_bytes"], "bytes"),
    ):
        emit(
            name,
            value,
            step=3,
            tags=("dashboard", "performance"),
            unit=unit,
            context={
                **event_context,
                "diagnostic_scope": "synthetic denoising only; not formal model performance",
            },
            path=events_file,
        )
    result["telemetry_events"], result["telemetry_event_bytes"] = read_smoke_events(
        events_file
    )
    result["elapsed_seconds"] = time.perf_counter() - started
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=43)
    parser.add_argument(
        "--events-file",
        type=Path,
        help="Fresh canonical event stream (default: output sibling events.jsonl)",
    )
    args = parser.parse_args()
    result = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    temporary.replace(args.output)
    print(json.dumps(result, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
