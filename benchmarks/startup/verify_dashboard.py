"""Check trusted smoke scalars through the existing dashboard telemetry view."""

import math
from pathlib import Path

from integrations.protok.dashboard import telemetry_view


TERM_NAMES = (
    "geom_dist_loss",
    "geom_dir_loss",
    "binned_dist_loss",
    "binned_dir_loss",
    "inverse_folding_loss",
)


def verify(root, receipt):
    """Verify actual dashboard projection; ``root`` contains the Study directory."""
    if receipt.get("synthetic") is not True or receipt.get("completed_steps") != 3:
        raise ValueError("A completed three-step synthetic smoke receipt is required")
    final = receipt["losses"][-1]
    if final.get("step") != 3:
        raise ValueError("Trusted smoke receipt has no final step 3")
    expected = {
        "train/loss": final["loss"],
        **{"train/" + name: final[name] for name in TERM_NAMES},
        "diagnostic/ncac_bbrmsd": receipt["synthetic_denoising_ncac_bbrmsd_mean_A"],
        **{
            "perf/" + name: receipt[name]
            for name in (
                "initialization_seconds",
                "first_step_latency_seconds",
                "steady_state_proteins_per_second",
                "peak_memory_bytes",
            )
        },
    }
    projected = telemetry_view(Path(root), source_id="active", experiment_id="0001")
    visible = {}
    streams = []
    for stream in projected.get("streams", []):
        matched = False
        if stream.get("error_count") or stream.get("sync_error"):
            raise ValueError("Dashboard telemetry stream reports an error")
        for event in stream.get("events", []):
            name = event.get("name")
            if name not in expected or event.get("step") != 3:
                continue
            context = event.get("context", {})
            if (
                context.get("synthetic") is not True
                or context.get("seed") != receipt["seed"]
            ):
                raise ValueError("Dashboard scalar has a different synthetic context")
            value = event.get("value")
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("Dashboard scalar must be finite and numeric")
            trusted = expected[name]
            if (
                type(trusted) not in (int, float)
                or not math.isfinite(trusted)
                or not math.isclose(value, trusted, rel_tol=1e-6, abs_tol=1e-7)
            ):
                raise ValueError(
                    f"Dashboard scalar differs from trusted receipt: {name}"
                )
            if name in visible:
                raise ValueError(f"Ambiguous duplicate final dashboard scalar: {name}")
            visible[name] = value
            matched = True
        if matched:
            streams.append(stream["stream_id"])
    missing = expected.keys() - visible.keys()
    if missing:
        raise ValueError(
            f"Final smoke scalars absent from dashboard: {sorted(missing)}"
        )
    return dict(
        ok=True,
        verified=True,
        source_id="active",
        experiment_id="0001",
        step=3,
        synthetic=True,
        visible_scalars=visible,
        streams=streams,
    )
