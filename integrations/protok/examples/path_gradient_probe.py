"""Execution smoke on a trained projection subgraph; no training or quality score."""

import errno
import json
import math
import os
from pathlib import Path

import torch
from torch import nn

from protok.data.frozen_hidden import FrozenHiddenDataset
from protok.model.autoresearch_se3 import RMSNorm


def require_read_only(path):
    # No truncation, creation or write: even a failed isolation check cannot alter input.
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CLOEXEC)
    except OSError as error:
        if error.errno not in (errno.EROFS, errno.EACCES, errno.EPERM):
            raise
        return {"path": str(path), "write_open_denied": True}
    os.close(descriptor)
    raise RuntimeError(f"Expected read-only mount: {path}")


def norm(tensors):
    values = [v.detach().float().reshape(-1) for v in tensors if v is not None]
    return float(torch.cat(values).norm()) if values else 0.0


def main():
    if os.getpid() != 1:
        raise RuntimeError("Research process must own the isolated PID namespace")
    if not torch.cuda.is_available():
        # Preserve the driver's precise initialization failure for the next fix.
        torch.cuda.init()
        raise RuntimeError("CUDA unavailable inside the research sandbox")
    torch.set_num_threads(1)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    isolation = [
        require_read_only(Path(__file__).resolve()),
        require_read_only("/inputs/checkpoint"),
    ]
    # Load once for actual computation; no whole-checkpoint validation or hash.
    saved = torch.load("/inputs/checkpoint", map_location="cpu", weights_only=True)
    config = json.loads(
        Path("/workspace/configs/autoresearch_se3_candidate.json").read_text()
    )
    if saved["optimizer_step"] != 2000 or saved["candidate_config"] != config:
        raise ValueError("Expected iteration 0012's frozen step-2000 model")
    if config["model"]["depth"] != 4 or config["model"]["heads"] != 4:
        raise ValueError("This smoke is specified for the four-layer 0012 model")
    row = FrozenHiddenDataset(Path("/inputs/cache"), "val")[0]
    count = min(64, row["length"])
    if count < 2:
        raise ValueError("The selected row needs two residues")
    target = row["target"][:count].to(device="cuda", dtype=torch.float32)
    target -= target.mean(0, keepdim=True)
    generator = torch.Generator(device="cuda").manual_seed(20261102)
    noise = torch.randn(target.shape, generator=generator, device="cuda")
    noise -= noise.mean(0, keepdim=True)
    coordinates = 0.5 * (noise + target)
    radial_input = torch.linalg.vector_norm(
        coordinates[:, None] - coordinates[None, :], dim=-1
    )[None, ..., None]
    edges = ~torch.eye(count, dtype=torch.bool, device="cuda")
    rows = []
    for index in range(4):
        prefix = f"layers.{index}."
        encoder = nn.Sequential(nn.Linear(1, 16), nn.SiLU(), RMSNorm(16, affine=False))
        encoder.load_state_dict(
            {
                name: saved["model"][prefix + "radial_projection." + name]
                for name in encoder.state_dict()
            }
        )
        projection = nn.Linear(16, 12, bias=False)
        projection.load_state_dict(
            {"weight": saved["model"][prefix + "radial_coord_projection.weight"]}
        )
        encoder, projection = encoder.cuda().eval(), projection.cuda().eval()
        cotangent = torch.randn(
            (1, count, count, 12), generator=generator, device="cuda"
        )
        cotangent *= edges[None, ..., None] / math.sqrt(int(edges.sum()) * 12)

        def evaluate(detach):
            encoder.zero_grad(set_to_none=True)
            projection.zero_grad(set_to_none=True)
            features = encoder(radial_input)
            features.retain_grad()
            values = projection(features.detach() if detach else features)
            scalar = (values * cotangent).sum()
            scalar.backward()
            measurements = {
                "scalar": float(scalar.detach()),
                "feature_grad_present": features.grad is not None,
                "feature_grad_norm": norm([features.grad]),
                "encoder_grad_present": any(
                    p.grad is not None for p in encoder.parameters()
                ),
                "encoder_grad_norm": norm([p.grad for p in encoder.parameters()]),
                "projection_grad_norm": norm([projection.weight.grad]),
            }
            return (
                values.detach().clone(),
                projection.weight.grad.detach().clone(),
                measurements,
            )

        full_values, full_grad, full = evaluate(False)
        cut_values, cut_grad, cut = evaluate(True)
        torch.testing.assert_close(full_values, cut_values, atol=1e-6, rtol=1e-6)
        torch.testing.assert_close(full_grad, cut_grad, atol=1e-6, rtol=1e-6)
        if cut["feature_grad_present"] or cut["encoder_grad_present"]:
            raise AssertionError("Detached subgraph still reaches its encoder")
        if not torch.isfinite(full_values).all() or not torch.isfinite(full_grad).all():
            raise AssertionError("Nonfinite projection output/weight gradient")
        rows.append(
            {
                "layer": index,
                "projection_weight_norm": norm([projection.weight]),
                "forward_max_abs_error": float((full_values - cut_values).abs().max()),
                "projection_grad_max_abs_error": float(
                    (full_grad - cut_grad).abs().max()
                ),
                "full": full,
                "detach": cut,
            }
        )
    torch.cuda.synchronize()
    report = {
        "purpose": "execution_and_path_intervention_smoke",
        "quality_score_eligible": False,
        "optimizer_step": saved["optimizer_step"],
        "checkpoint_seed": saved["seed"],
        "compute": {
            "device": torch.cuda.get_device_name(),
            "dtype": "float32",
            "tf32": False,
            "probe_seed": 20261102,
            "time": 0.5,
        },
        "sample": {
            "split": "val",
            "role": "development",
            "dataset_index": 0,
            "row_id": row["row_id"],
            "name": row["name"],
            "original_residues": row["length"],
            "used_residues": count,
            "mask": "selected residues valid; self edges excluded",
            "coordinate_units": "unchanged cached target units",
            "target_centering": "recentered after prefix truncation",
        },
        "method": "For each trained layer: r=RMSNorm(SiLU(Linear(distance))), a=W_X r. Compare full versus detached r at identical weights/input and fixed masked random cotangent; scalar=sum(a*cotangent).",
        "read_only_checks": isolation,
        "layers": rows,
        "limitations": [
            "One protein prefix and one fixed time/noise realization.",
            "All layers use initial-state distances, not full-model activations.",
            "Hidden conditioning, attention, coordinate directions and other branches are omitted.",
            "Random cotangent is not a training-loss gradient; encoder gradients vanish here because other paths are absent.",
            "Nonzero path gradients do not establish usefulness or performance benefit.",
            "No optimizer update, training, sampling, cross-seed comparison or ranking measurement.",
        ],
    }
    Path("probe.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    scale = 300 / max(max(r["full"]["feature_grad_norm"] for r in rows), 1e-30)
    svg = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="660" height="270">',
        '<rect width="660" height="270" fill="white"/>',
        '<g font-family="sans-serif" font-size="14" fill="#172033">',
        '<text x="20" y="28">Projection subgraph: input VJP norm</text>',
        '<text x="20" y="50">Blue: full; detach: absent gradient. Not a quality score.</text>',
    ]
    for row in rows:
        y, value = 85 + 38 * row["layer"], row["full"]["feature_grad_norm"]
        svg += [
            f'<text x="20" y="{y + 15}">Layer {row["layer"]}</text>',
            f'<rect x="90" y="{y}" width="{value * scale:.3f}" height="20" fill="#2878b5"/>',
            f'<text x="410" y="{y + 15}">{value:.5g}; detach: absent</text>',
        ]
    Path("probe.svg").write_text("\n".join(svg + ["</g></svg>"]) + "\n")


if __name__ == "__main__":
    main()
