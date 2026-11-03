"""Actual TensorBoard event-reader checks for the CPU-only derived mirror."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from integrations.protok import tensorboard as mirror
from integrations.protok import tensorboard_scope
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
            (file.parent.relative_to(self.logdir / "events-v2").as_posix(), event)
            for file in sorted(
                (self.logdir / "events-v2").rglob("events.out.tfevents.*")
            )
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
        self.assertTrue(
            any(p[2].startswith("observations/train/loss/a~1b~0c [") for p in points)
        )
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
        self.assertEqual(len({(point[0], point[2]) for point in points}), 7)
        self.assertEqual(len({point[0] for point in points}), 4)
        self.assertTrue(any(run.startswith("history-1/") for run, *_ in points))
        self.assertTrue(any("L" + "a" * 16 in run for run, *_ in points))
        order = [p for p in points if "event-order;" in p[2]]
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
            if path.suffix == ".json" and path.name != "layout.json":
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
        self.emit(2, step=2**64, unit="seconds")
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
        state_path = next(
            p for p in exporter.state_dir.glob("*.json") if p.name != "layout.json"
        )
        state = json.loads(state_path.read_text())
        self.assertEqual(len(state["errors"]), 1)
        boundary = state["offset"]
        exporter.poll()
        self.assertEqual(len(self.scalars()), 1)
        state = json.loads(state_path.read_text())
        self.assertEqual(state["offset"], boundary)
        self.emit(2, step=2)
        exporter.poll()
        self.assertEqual(len(self.scalars()), 2)

    def test_derived_logdir_cannot_overlap_source_study(self):
        with self.assertRaises(ValueError):
            mirror.Exporter(self.root, self.study / "derived")
        with self.assertRaises(ValueError):
            mirror.Exporter(self.root, self.base)

    def scalar_app(self):
        """Exercise TensorBoard's real reader, provider and HTTP scalar routes."""
        from tensorboard.backend.application import TensorBoardWSGI
        from tensorboard.backend.event_processing.data_provider import (
            MultiplexerDataProvider,
        )
        from tensorboard.backend.event_processing.plugin_event_multiplexer import (
            EventMultiplexer,
        )
        from tensorboard.plugins.base_plugin import TBContext
        from tensorboard.plugins.metrics.metrics_plugin import MetricsPlugin
        from tensorboard.plugins.scalar.scalars_plugin import ScalarsPlugin
        from werkzeug.test import Client

        multiplexer = EventMultiplexer(size_guidance={"tensors": 0})
        provider = MultiplexerDataProvider(multiplexer, str(self.logdir / "events-v2"))
        plugin = ScalarsPlugin(TBContext(data_provider=provider))
        app = TensorBoardWSGI(
            [plugin, MetricsPlugin(TBContext(data_provider=provider))],
            path_prefix="/tensorboard",
            data_provider=provider,
        )
        return multiplexer, Client(app)

    def reload_scalars(self, multiplexer, client):
        multiplexer.AddRunsFromDirectory(str(self.logdir / "events-v2"))
        multiplexer.Reload()
        response = client.get("/tensorboard/data/plugin/scalars/tags")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_many_conditions_share_dashboard_run_and_real_scalar_api_tracks_curve(self):
        for seed in range(60):
            self.emit(seed + 0.25, step=10, unit="loss", context={"seed": seed})
        self.emit(0.125, step=20, unit="loss", context={"seed": 0})
        exporter = mirror.Exporter(self.root, self.logdir)
        exporter.poll()
        multiplexer, client = self.scalar_app()
        tags = self.reload_scalars(multiplexer, client)
        self.assertEqual(list(tags), ["active/#0001"])
        self.assertEqual(len(tags["active/#0001"]), 60)
        tag = next(
            t for t, m in tags["active/#0001"].items() if "seed=0;" in m["displayName"]
        )
        response = client.get(
            "/tensorboard/data/plugin/scalars/scalars",
            query_string={"run": "active/#0001", "tag": tag},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row[1:] for row in response.get_json()], [[10, 0.25], [20, 0.125]]
        )

    def test_real_task_scope_reduces_tags_and_retains_live_curves_and_attempts(self):
        for seed in range(33):
            self.emit(seed + 0.25, step=10, unit="loss", context={"seed": seed})
        self.emit(0.125, step=20, unit="loss", context={"seed": 0})
        telemetry.register(
            self.study, "retry", self.source | {"execution_identity": "retry-job"}
        )
        self.emit(0.5, stream="retry", step=10, unit="loss", context={"seed": 0})
        telemetry.register(
            self.study, "unrelated", self.source | {"experiment_id": "0002"}
        )
        for start in range(0, 1001, 250):
            self.emit(
                {
                    f"field-{index:04d}": index
                    for index in range(start, min(start + 250, 1001))
                },
                stream="unrelated",
                step=10,
            )
        archive = self.base / "archive"
        telemetry.register(archive / "study", "live", self.source)
        self.emit(
            99, study=archive / "study", step=10, unit="loss", context={"seed": 0}
        )
        exporter = mirror.Exporter(self.root, self.logdir, [archive])
        self.assertEqual(exporter.poll()["errors"], [])
        multiplexer, client = self.scalar_app()
        global_tags = self.reload_scalars(multiplexer, client)
        self.assertGreater(sum(len(tags) for tags in global_tags.values()), 1000)
        self.assertEqual(len(global_tags["active/#0002"]), 1001)
        _, scope = tensorboard_scope.parse_request(
            '/tensorboard?dashboard_task={"source":"active","experiment":"0001"}',
            {"active", "history-1"},
        )
        selected = "active/#0001"
        retry = selected + " (attempt 2)"
        filtered_tags = tensorboard_scope.filter_response(
            "/data/plugin/scalars/tags", global_tags, scope
        )
        self.assertEqual(set(filtered_tags), {selected, retry})
        self.assertEqual(sum(len(tags) for tags in filtered_tags.values()), 34)
        self.assertEqual(filtered_tags[selected], global_tags[selected])
        self.assertEqual(filtered_tags[retry], global_tags[retry])
        tag = next(
            tag
            for tag, metadata in filtered_tags[selected].items()
            if "seed=0;" in metadata["displayName"]
        )
        retry_tag = next(iter(filtered_tags[retry]))

        def scalar_curve():
            response = client.get(
                "/tensorboard/data/plugin/scalars/scalars",
                query_string={"run": selected, "tag": tag},
            )
            self.assertEqual(response.status_code, 200)
            return response.get_json()

        def timeseries_curve():
            response = client.get("/tensorboard/data/plugin/timeseries/tags")
            self.assertEqual(response.status_code, 200)
            metadata = tensorboard_scope.filter_response(
                "/data/plugin/timeseries/tags", response.get_json(), scope
            )
            self.assertEqual(
                metadata["scalars"]["runTagInfo"],
                {run: sorted(tags) for run, tags in filtered_tags.items()},
            )
            self.assertEqual(metadata["scalars"]["tagDescriptions"], {})
            response = client.get(
                "/tensorboard/data/plugin/timeseries/timeSeries",
                query_string={
                    "requests": json.dumps(
                        [
                            {"plugin": "scalars", "tag": tag},
                            {"plugin": "scalars", "tag": retry_tag, "run": retry},
                            {"plugin": "scalars", "tag": tag, "run": "history-1/#0001"},
                            {"plugin": "invalid", "tag": tag},
                        ]
                    )
                },
            )
            self.assertEqual(response.status_code, 200)
            global_series = response.get_json()
            self.assertEqual(
                set(global_series[0]["runToSeries"]),
                {selected, "history-1/#0001"},
            )
            series = tensorboard_scope.filter_response(
                "/data/plugin/timeseries/timeSeries", global_series, scope
            )
            self.assertEqual(len(series), 4)
            self.assertEqual(set(series[0]["runToSeries"]), {selected})
            self.assertEqual(series[1], global_series[1])
            self.assertEqual(series[2], {**global_series[2], "runToSeries": {}})
            self.assertEqual(series[3], global_series[3])
            self.assertEqual(series[3]["error"], "Invalid plugin")
            return series[0]["runToSeries"][selected]

        curve = scalar_curve()
        self.assertEqual([row[1:] for row in curve], [[10, 0.25], [20, 0.125]])
        self.assertEqual(
            timeseries_curve(),
            [
                {"wallTime": wall, "step": step, "value": value}
                for wall, step, value in curve
            ],
        )
        self.emit(0.0625, step=30, unit="loss", context={"seed": 0})
        self.assertEqual(exporter.poll()["exported"], 1)
        self.assertEqual(exporter.poll()["exported"], 0)
        self.assertEqual(self.reload_scalars(multiplexer, client), global_tags)
        updated_curve = scalar_curve()
        self.assertEqual(updated_curve[:2], curve)
        self.assertEqual(
            [row[1:] for row in updated_curve],
            [[10, 0.25], [20, 0.125], [30, 0.0625]],
        )
        self.assertEqual(
            timeseries_curve(),
            [
                {"wallTime": wall, "step": step, "value": value}
                for wall, step, value in updated_curve
            ],
        )

    def test_shared_run_streams_append_and_crash_replay_reach_existing_reader(self):
        telemetry.register(self.study, "evaluation", self.source)
        self.emit(1, step=10)
        self.emit(2, step=10, stream="evaluation")
        exporter = mirror.Exporter(self.root, self.logdir)
        self.assertEqual(exporter.poll()["exported"], 2)
        multiplexer, client = self.scalar_app()
        self.assertEqual(
            list(self.reload_scalars(multiplexer, client)), ["active/#0001"]
        )
        self.emit(3, step=20)
        self.emit(4, step=20, stream="evaluation")
        original = mirror.atomic_json

        def fail_cursor(path, value):
            if path.suffix == ".json" and path.name != "layout.json":
                raise OSError("cursor crash")
            return original(path, value)

        with patch.object(mirror, "atomic_json", side_effect=fail_cursor):
            self.assertEqual(len(exporter.poll()["errors"]), 1)
        self.reload_scalars(multiplexer, client)
        mirror.Exporter(self.root, self.logdir).poll()
        mirror.Exporter(self.root, self.logdir).poll()
        tags = self.reload_scalars(multiplexer, client)
        values = []
        for tag in tags["active/#0001"]:
            response = client.get(
                "/tensorboard/data/plugin/scalars/scalars",
                query_string={"run": "active/#0001", "tag": tag},
            )
            self.assertEqual(response.status_code, 200)
            values.extend(row[1:] for row in response.get_json())
        self.assertEqual(sorted(values), [[10, 1], [10, 2], [20, 3], [20, 4]])

    def test_layout_upgrade_preserves_old_mirror_and_rejects_rebound_study(self):
        old = self.logdir / "events/old-run/events.out.tfevents.old"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"retained old mirror")
        self.emit(1, step=1)
        mirror.Exporter(self.root, self.logdir).poll()
        self.assertEqual(old.read_bytes(), b"retained old mirror")
        with self.assertRaisesRegex(ValueError, "different source Studies"):
            mirror.Exporter(self.base / "other-study", self.logdir).poll()

    def test_scalar_object_name_aliases_keep_separate_series(self):
        self.emit({"mean": 1}, step=1)
        telemetry.emit(
            "train/loss/mean",
            2,
            step=1,
            path=self.study / "telemetry/live/events.jsonl",
        )
        mirror.Exporter(self.root, self.logdir).poll()
        self.assertEqual(len({p[2] for p in self.scalars()}), 2)

    def test_shared_run_keeps_nonnumeric_stream_provenance(self):
        other = self.source | {"native_log": "other.log"}
        telemetry.register(self.study, "other", other)
        self.emit("first", step=1)
        self.emit("second", stream="other", step=1)
        mirror.Exporter(self.root, self.logdir).poll()
        evidence = {e["stream_id"]: e for e in self.evidence()}
        self.assertEqual(evidence["live"]["source"], self.source)
        self.assertEqual(evidence["other"]["source"], other)
        self.assertEqual({run for run, _ in self.reader()}, {"active/#0001"})
        self.assertEqual(
            len({v.tag for _, e in self.reader() for v in e.summary.value}), 2
        )


if __name__ == "__main__":
    unittest.main()
