"""Manual long telemetry uses its bound iteration, separately from automatic lanes."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from integrations.protok import dashboard as dash
from integrations.protok import tensorboard as mirror
from integrations.protok.task_telemetry import manual_locations
from integrations.protok.tests import test_task_metrics_dashboard as fixtures
from research_loop import telemetry


class ManualMetricsTests(unittest.TestCase):
    put_stream = fixtures.TaskMetricsTests.put_stream
    handler = fixtures.TaskMetricsTests.handler
    request = fixtures.TaskMetricsTests.request

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root, self.history = self.base / "active", self.base / "history"

    def manual(self, root, identity="manual-check"):
        folder = root / "study/tasks" / identity
        location = folder / "study/iterations/0001"
        location.mkdir(parents=True)
        (folder / "request.json").write_text(
            json.dumps({"id": identity, "kind": "long_training"})
        )
        (folder / "binding.json").write_text(
            json.dumps({"study": str(folder / "study"), "iteration": "0001"})
        )
        return location

    def test_running_completed_api_paging_and_source_task_execution_isolation(self):
        manual = self.manual(self.root)
        other = self.manual(self.root, "another-task")
        history = self.manual(self.history)
        auto = self.root / "study/long-runs/L0123456789abcdef/study"
        for location, value in (
            (manual, 1),
            (other, 2),
            (history, 3),
            (auto, 4),
            (self.root / "study", 5),
        ):
            self.put_stream(location, "same-stream", "0001", value)
        self.put_stream(manual, "new-execution", "0001", 6)
        self.put_stream(manual, "hidden", "0001", 777, tags=("internal",))
        handler = self.handler(self.root, history_roots=[self.history])
        query = "/api/telemetry?source=active&task=manual-check"
        response = self.request(handler, query)
        self.assertIn(b"200 OK", response)
        payload = json.loads(response.split(b"\r\n\r\n")[1])
        self.assertEqual(
            {
                event["value"]
                for stream in payload["streams"]
                for event in stream["events"]
            },
            {1, 6},
        )
        self.assertTrue(
            all(
                stream["manual_task_id"] == "manual-check" and stream["run_id"] is None
                for stream in payload["streams"]
            )
        )
        self.assertEqual(
            len(
                {
                    stream["source"]["execution_identity"]
                    for stream in payload["streams"]
                }
            ),
            3,
        )
        boundary = telemetry.info(manual, "same-stream")["byte_end"]
        telemetry.emit(
            "train/loss",
            7,
            step=20,
            unit="loss",
            context={"seed": 42},
            tags=["dashboard"],
            path=manual / "telemetry/same-stream/events.jsonl",
        )
        # Completing a child leaves request/binding and retained telemetry unchanged.
        (manual / "iteration.json").write_text(json.dumps({"status": "reviewed"}))
        (manual.parent.parent / "state.json").write_text(
            json.dumps({"status": "ready", "phase": "done"})
        )
        page = self.request(
            handler, query + "&stream=same-stream&after=" + str(boundary)
        )
        rows = json.loads(page.split(b"\r\n\r\n")[1])["streams"]
        self.assertEqual([event["value"] for event in rows[0]["events"]], [7])
        historical = dash.telemetry_view(
            self.root,
            history_roots=[self.history],
            source_id="history-1",
            manual_task_id="manual-check",
        )
        self.assertEqual(historical["streams"][0]["events"][0]["value"], 3)
        automatic = dash.telemetry_view(self.root, run_id="L0123456789abcdef")
        self.assertEqual(automatic["streams"][0]["events"][0]["value"], 4)
        main = dash.telemetry_view(self.root, source_id="active", experiment_id="0001")
        self.assertEqual(main["streams"][0]["events"][0]["value"], 5)
        for invalid in (
            "task=manual-check",
            "source=active&task=../outside",
            "source=active&task=manual-check&experiment=0001",
            "source=active&task=manual-check&run=L0123456789abcdef",
        ):
            self.assertIn(
                b"400 Bad Request", self.request(handler, "/api/telemetry?" + invalid)
            )
        for absent in (
            "source=history-2&task=manual-check",
            "source=active&task=absent",
        ):
            self.assertEqual(
                json.loads(
                    self.request(handler, "/api/telemetry?" + absent).split(
                        b"\r\n\r\n"
                    )[1]
                )["streams"],
                [],
            )

    def test_locator_uses_only_canonical_bound_long_tasks(self):
        location = self.manual(self.root)
        task = location.parent.parent.parent
        self.assertEqual(
            list(manual_locations(self.root / "study")),
            [("manual-check", "0001", location)],
        )
        (task / "binding.json").write_text(
            json.dumps({"study": str(self.base / "outside"), "iteration": "0001"})
        )
        self.assertEqual(list(manual_locations(self.root / "study")), [])
        (task / "binding.json").write_text(
            json.dumps({"study": str(task / "study"), "iteration": "../outside"})
        )
        self.assertEqual(list(manual_locations(self.root / "study")), [])

    @unittest.skipUnless(
        importlib.util.find_spec("tensorboard"), "install the dashboard extra"
    )
    def test_actual_tensorboard_all_tags_manual_history_auto_and_restart(self):
        from tensorboard.backend.event_processing.event_accumulator import (
            EventAccumulator,
        )

        manual = self.manual(self.root)
        history = self.manual(self.history)
        auto = self.root / "study/long-runs/L0123456789abcdef/study"
        other = self.manual(self.root, "another-task")
        for location, value in ((manual, 1), (history, 2), (auto, 3), (other, 4)):
            self.put_stream(location, "same-stream", "0001", value)
        self.put_stream(manual, "hidden", "0001", 777, tags=("internal",))
        self.put_stream(manual, "new-execution", "0001", 888)
        logdir = self.base / "derived"
        exporter = mirror.Exporter(self.root, logdir, [self.history])
        self.assertEqual(exporter.poll()["exported"], 6)
        boundary = telemetry.info(manual, "same-stream")["byte_end"]
        telemetry.emit(
            "train/loss",
            9,
            step=20,
            unit="loss",
            context={"seed": 42},
            tags=["diagnostic"],
            path=manual / "telemetry/same-stream/events.jsonl",
        )
        from unittest.mock import patch

        with patch.object(
            telemetry, "read_events", wraps=telemetry.read_events
        ) as read:
            self.assertEqual(
                mirror.Exporter(self.root, logdir, [self.history]).poll()["exported"], 1
            )
        selected_reads = [
            call
            for call in read.call_args_list
            if Path(call.args[0]).resolve() == manual.resolve()
            and call.args[1] == "same-stream"
        ]
        self.assertEqual(selected_reads[0].kwargs["after"], boundary)
        self.assertEqual(
            mirror.Exporter(self.root, logdir, [self.history]).poll()["exported"], 0
        )
        runs = sorted(
            {file.parent for file in (logdir / "events").rglob("events.out.tfevents.*")}
        )
        values, evidence = [], []
        for directory in runs:
            accumulator = EventAccumulator(
                str(directory), size_guidance={"tensors": 0}
            ).Reload()
            values.extend(
                (
                    directory.relative_to(logdir / "events").as_posix(),
                    event.tensor_proto.double_val[0],
                )
                for event in accumulator.Tensors("observations/train/loss")
            )
            evidence.extend(
                json.loads(event.tensor_proto.string_val[0])
                for event in accumulator.Tensors("evidence/train/loss")
            )
        self.assertEqual(
            sorted(value for _, value in values), [1, 2, 3, 4, 9, 777, 888]
        )
        self.assertEqual(len({run for run, _ in values}), 6)
        self.assertTrue(
            any(
                run.startswith("history-1/") and "manual:manual-check:0001" in run
                for run, _ in values
            )
        )
        self.assertTrue(any("L0123456789abcdef" in run for run, _ in values))
        self.assertTrue(any(event["tags"] == ["internal"] for event in evidence))
        self.assertTrue(any(event["tags"] == ["diagnostic"] for event in evidence))
