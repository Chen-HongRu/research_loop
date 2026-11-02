"""Actual TensorBoard event-reader checks for the CPU-only derived mirror."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from integrations.protok import tensorboard as mirror
from research_loop import telemetry

try:
    from tensorboard.backend.event_processing.event_file_loader import EventFileLoader
except ImportError:
    EventFileLoader = None


@unittest.skipIf(EventFileLoader is None, "install the dashboard extra")
class TensorBoardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "run"
        self.study = self.root / "study"
        self.logdir = self.base / "derived"
        self.source = {
            "kind": "fixed-short",
            "experiment_id": "0001",
            "source_identity": "snapshot",
            "execution_identity": "job-1",
        }
        telemetry.register(self.study, "live", self.source)

    def emit(self, value, *, study=None, stream="live", **kwargs):
        return telemetry.emit(
            "train/loss",
            value,
            path=(study or self.study) / "telemetry" / stream / "events.jsonl",
            **kwargs,
        )

    def reader(self):
        return [
            (file.parent.relative_to(self.logdir / "events").as_posix(), event)
            for file in sorted((self.logdir / "events").rglob("events.out.tfevents.*"))
            for event in EventFileLoader(str(file)).Load()
            if event.HasField("summary")
        ]

    def scalars(self):
        return [
            (run, event.step, value.tag, value.tensor.double_val[0])
            for run, event in self.reader()
            for value in event.summary.value
            if value.metadata.plugin_data.plugin_name == "scalars"
        ]

    def evidence(self):
        return [
            json.loads(value.tensor.string_val[0])
            for _, event in self.reader()
            for value in event.summary.value
            if value.metadata.plugin_data.plugin_name == "text"
        ]

    def test_float64_object_fields_and_all_tag_text_evidence(self):
        precise = 1.00000000000001
        self.emit(precise, step=7, context={"seed": 42})
        self.emit({"mean": precise, "a/b~c": 2, "array": [1, 2]}, step=8)
        self.emit([1, 2], tags=["diagnostic"])
        self.emit(float("nan"), tags=["internal"])
        self.emit("a text observation", tags=["review"])
        self.assertEqual(mirror.Exporter(self.root, self.logdir).poll()["exported"], 5)
        points = self.scalars()
        self.assertEqual(len(points), 3)
        self.assertEqual(points[0][3], precise)
        self.assertIn("observations/train/loss/a~1b~0c", [p[2] for p in points])
        evidence = self.evidence()
        self.assertEqual(len(evidence), 5)
        self.assertTrue(any(e["tags"] == ["internal"] for e in evidence))
        self.assertTrue(any(e["value"] == {"nonfinite": "NaN"} for e in evidence))
        self.assertTrue(any(e["value"] == [1, 2] for e in evidence))
        self.assertIsNone(importlib.util.find_spec("tensorflow"))

    def test_source_execution_unit_conditions_and_axis_isolation(self):
        for unit, seed in [("loss", 1), ("loss", 2), ("seconds", 1)]:
            self.emit(1, step=1, unit=unit, context={"seed": seed})
        self.emit(2, step=2, unit="loss", context={"seed": 1, "remote_end_offset": 99})
        self.emit(2, unit="loss", context={"seed": 1})
        other_source = self.source | {"execution_identity": "job-2"}
        telemetry.register(self.study, "second", other_source)
        self.emit(3, stream="second", step=1)
        archive = self.base / "archive"
        telemetry.register(archive / "study", "live", self.source)
        self.emit(4, study=archive / "study", step=1)
        child = self.study / "long-runs" / ("L" + "a" * 16) / "study"
        telemetry.register(child, "live", self.source)
        self.emit(5, study=child, step=1)
        mirror.Exporter(self.root, self.logdir, [archive]).poll()
        points = self.scalars()
        self.assertEqual(len(points), 8)
        self.assertEqual(len({point[0] for point in points}), 7)
        self.assertTrue(any(run.startswith("history-1/") for run, *_ in points))
        self.assertTrue(any("L" + "a" * 16 in run for run, *_ in points))
        order = [p for p in points if p[0].endswith("/event-order")]
        self.assertEqual(len(order), 1)
        self.assertEqual(order[0][1], 5)
        self.assertIsNone(next(e for e in self.evidence() if e["step"] is None)["step"])

    def test_append_restart_reads_only_cursor_without_duplicates(self):
        self.emit(1, step=1)
        exporter = mirror.Exporter(self.root, self.logdir)
        exporter.poll()
        boundary = telemetry.info(self.study, "live")["byte_end"]
        self.emit(2, step=2)
        with patch.object(
            telemetry, "read_events", wraps=telemetry.read_events
        ) as read:
            mirror.Exporter(self.root, self.logdir).poll()
        self.assertEqual(read.call_args.kwargs["after"], boundary)
        mirror.Exporter(self.root, self.logdir).poll()
        self.assertEqual(len(self.scalars()), 2)

    def test_crash_after_publish_before_cursor_replays_exact_pending_page(self):
        self.emit(1, step=1)
        exporter = mirror.Exporter(self.root, self.logdir)
        original = mirror.atomic_json

        def fail_state(path, value):
            if path.suffix == ".json":
                raise OSError("simulated cursor publication crash")
            return original(path, value)

        with patch.object(mirror, "atomic_json", side_effect=fail_state):
            self.assertEqual(len(exporter.poll()["errors"]), 1)
        self.assertEqual(len(self.scalars()), 1)
        self.emit(2, step=2)
        restarted = mirror.Exporter(self.root, self.logdir)
        restarted.poll()
        self.assertEqual(len(self.scalars()), 1)
        restarted.poll()
        self.assertEqual(len(self.scalars()), 2)
        self.assertFalse(list(exporter.state_dir.glob("*.pending")))

    def test_partial_page_publication_restarts_without_duplicate_points(self):
        self.emit(1, step=1, unit="loss")
        self.emit(2, step=2, unit="seconds")
        exporter = mirror.Exporter(self.root, self.logdir)
        original = Path.replace
        published = 0

        def interrupt_second_publish(path, target):
            nonlocal published
            if Path(target).name.startswith("events.out.tfevents."):
                published += 1
                if published == 2:
                    raise OSError("simulated partial page publication")
            return original(path, target)

        with patch.object(Path, "replace", interrupt_second_publish):
            self.assertEqual(len(exporter.poll()["errors"]), 1)
        self.assertEqual(len(self.scalars()), 1)
        mirror.Exporter(self.root, self.logdir).poll()
        self.assertEqual(len(self.scalars()), 2)

    def test_malformed_partial_and_bad_stream_do_not_block_healthy_stream(self):
        log = self.study / "telemetry" / "live" / "events.jsonl"
        log.write_bytes(b"not-json\n")
        self.emit(1, step=1)
        with log.open("ab") as handle:
            handle.write(b'{"partial":')
        broken = self.study / "telemetry" / "broken"
        broken.mkdir()
        (broken / "meta.json").write_text("bad json")
        exporter = mirror.Exporter(self.root, self.logdir)
        result = exporter.poll()
        self.assertEqual(result["exported"], 1)
        self.assertEqual(len(result["errors"]), 1)
        state = json.loads(next(exporter.state_dir.glob("*.json")).read_text())
        self.assertEqual(len(state["errors"]), 1)
        boundary = state["offset"]
        exporter.poll()
        self.assertEqual(len(self.scalars()), 1)
        state = json.loads(next(exporter.state_dir.glob("*.json")).read_text())
        self.assertEqual(state["offset"], boundary)
        self.emit(2, step=2)
        exporter.poll()
        self.assertEqual(len(self.scalars()), 2)

    def test_derived_logdir_cannot_overlap_source_study(self):
        with self.assertRaises(ValueError):
            mirror.Exporter(self.root, self.study / "derived")
        with self.assertRaises(ValueError):
            mirror.Exporter(self.root, self.base)


if __name__ == "__main__":
    unittest.main()
