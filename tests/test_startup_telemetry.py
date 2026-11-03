"""Synthetic scalar transport through the canonical store and real dashboard view."""

from pathlib import Path
import tempfile
import unittest

from benchmarks.startup.gpu_smoke import read_smoke_events
from benchmarks.startup.verify_dashboard import verify
from research_loop import telemetry


class StartupTelemetryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.events = self.root / "generated/events.jsonl"
        self.receipt = {
            "synthetic": True,
            "completed_steps": 3,
            "seed": 43,
            "losses": [
                {
                    "step": step,
                    "loss": loss,
                    "geom_dist_loss": 10.0,
                    "geom_dir_loss": 7.0,
                    "binned_dist_loss": 8.0,
                    "binned_dir_loss": 7.0,
                    "inverse_folding_loss": 4.0,
                }
                for step, loss in ((1, 40.0), (2, 38.0), (3, 36.0))
            ],
            "synthetic_denoising_ncac_bbrmsd_A": [2.0, 4.0],
            "synthetic_denoising_ncac_bbrmsd_mean_A": 3.0,
            "initialization_seconds": 0.3,
            "first_step_latency_seconds": 0.4,
            "steady_state_proteins_per_second": 5.0,
            "peak_memory_bytes": 1024,
        }

    def publish(self, *, private_final_loss=False):
        context = {"synthetic": True, "seed": 43}
        for record in self.receipt["losses"]:
            for name, value in record.items():
                if name == "step":
                    continue
                private = private_final_loss and name == "loss" and record["step"] == 3
                telemetry.emit(
                    "train/" + name,
                    value,
                    step=record["step"],
                    tags=("internal",) if private else ("dashboard", "training"),
                    context=context,
                    path=self.events,
                )
        for name, value in {
            "diagnostic/ncac_bbrmsd": 3.0,
            "perf/initialization_seconds": 0.3,
            "perf/first_step_latency_seconds": 0.4,
            "perf/steady_state_proteins_per_second": 5.0,
            "perf/peak_memory_bytes": 1024,
        }.items():
            telemetry.emit(
                name,
                value,
                step=3,
                tags=("dashboard", "performance"),
                context=context,
                path=self.events,
            )
        source = {
            "kind": "startup-smoke",
            "experiment_id": "0001",
            "source_identity": "synthetic-source",
            "execution_identity": "synthetic-execution",
        }
        telemetry.register(self.root / "study", "startup", source)
        telemetry.append_chunk(
            self.root / "study", "startup", source, self.events.read_bytes(), offset=0
        )

    def test_canonical_events_reach_actual_dashboard_and_match_final_receipt(self):
        self.publish()
        events, count = read_smoke_events(self.events)
        self.assertEqual(len(events), 23)
        self.assertLess(count, 131072)
        actual = verify(self.root, self.receipt)
        self.assertIs(actual["ok"], True)
        self.assertIs(actual["verified"], True)
        self.assertEqual(actual["visible_scalars"]["train/loss"], 36.0)
        self.assertEqual(actual["visible_scalars"]["diagnostic/ncac_bbrmsd"], 3.0)
        self.assertEqual(actual["streams"], ["startup"])
        self.assertEqual(len(actual["visible_scalars"]), 11)

    def test_private_final_loss_cannot_pass_using_an_earlier_visible_step(self):
        self.publish(private_final_loss=True)
        with self.assertRaisesRegex(ValueError, "absent from dashboard"):
            verify(self.root, self.receipt)

    def test_dashboard_value_must_match_the_trusted_diagnostic(self):
        self.publish()
        self.receipt["synthetic_denoising_ncac_bbrmsd_mean_A"] = 0.01
        with self.assertRaisesRegex(ValueError, "differs from trusted receipt"):
            verify(self.root, self.receipt)

    def test_generated_stream_has_a_hard_size_limit(self):
        self.events.parent.mkdir(parents=True)
        self.events.write_bytes(b"x" * 131073)
        with self.assertRaisesRegex(ValueError, "exceeds 128 KiB"):
            read_smoke_events(self.events)


if __name__ == "__main__":
    unittest.main()
