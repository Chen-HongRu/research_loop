import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from research_loop import Study, initialize
from research_loop.telemetry import append_chunk, emit, info, read_events, register
from test_core import RecordedHarness, read


class TelemetryReviewTests(unittest.TestCase):
    def test_review_freezes_all_tags_and_excludes_late_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "study"
            initialize(root, goal="Inspect actual observations across all tags.")
            harness = RecordedHarness()
            study = Study(root, harness)
            producer = root.parent / "producer.jsonl"
            stream = "research-0001"
            source = {
                "kind": "research",
                "experiment_id": "0001",
                "source_identity": "source",
                "execution_identity": "execution",
            }

            def publish(name, value, tags):
                offset = producer.stat().st_size if producer.exists() else 0
                emit(name, value, tags=tags, step=10, path=producer)
                with producer.open("rb") as handle:
                    handle.seek(offset)
                    data = handle.read()
                append_chunk(root, stream, source, data, offset=offset)

            frozen_end = None

            def before(call):
                nonlocal frozen_end
                if call["phase"] == "execute":
                    register(root, stream, source)
                    publish("train/loss", 0.3, ["dashboard"])
                    publish(
                        "gradient/diagnostic", {"norm": 7}, ["diagnostic", "internal"]
                    )
                if call["phase"] == "review":
                    manifest = call["context"]["telemetry"]
                    frozen_end = manifest["streams"][0]["byte_end"]
                    self.assertIn("ALL tags", call["prompt"])
                    publish(
                        "later/observation", "not reviewed in this call", ["internal"]
                    )

            harness.before = before
            study.run()
            manifest = read(root / "iterations/0001/context/telemetry.json")
            self.assertEqual(manifest["streams"][0]["byte_end"], frozen_end)
            self.assertGreater(info(root, stream)["byte_end"], frozen_end)
            page = read_events(root, stream, until=frozen_end)
            self.assertEqual(
                [row["name"] for row in page["events"]],
                ["train/loss", "gradient/diagnostic"],
            )
            self.assertEqual(
                len(read_events(root, stream, tag="dashboard")["events"]), 1
            )
            accepted = next(call for call in harness.calls if call["phase"] == "review")
            saved = read(accepted["run_dir"] / "context.json")
            self.assertEqual(saved["telemetry"], manifest)
            # The read-only reviewer CLI can page every tag through the frozen bound.
            module = Path(
                __import__("research_loop.telemetry", fromlist=["__file__"]).__file__
            )
            output = subprocess.check_output(
                [
                    sys.executable,
                    "-B",
                    str(module),
                    "--study",
                    str(root),
                    "--stream",
                    stream,
                    "--until",
                    str(frozen_end),
                ],
                text=True,
            )
            self.assertEqual(len(json.loads(output)["events"]), 2)
            self.assertTrue((root / "iterations/0001/publication.json").is_file())

    def test_no_telemetry_preserves_existing_review_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "study"
            initialize(root, goal="Existing behavior without an event producer.")
            harness = RecordedHarness()
            Study(root, harness).run()
            self.assertFalse((root / "telemetry").exists())
            review = next(call for call in harness.calls if call["phase"] == "review")
            self.assertNotIn("telemetry", review["context"])


if __name__ == "__main__":
    unittest.main()
