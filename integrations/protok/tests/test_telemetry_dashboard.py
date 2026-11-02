"""Read-only dashboard telemetry projection, with internal payload isolation."""

import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

from integrations.protok import dashboard as dash


class TelemetryDashboardTests(unittest.TestCase):
    def test_real_local_store_shows_an_event_before_settlement_without_internal_values(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            study = root / "study"
            source = {
                "kind": "training",
                "experiment_id": "0001",
                "source_identity": "source",
                "execution_identity": "execution",
            }
            dash.telemetry.register(study, "live", source)
            events = [
                {
                    "version": 1,
                    "id": str(index),
                    "time": 1,
                    "name": "loss",
                    "value": value,
                    "step": 12,
                    "unit": None,
                    "tags": tags,
                    "context": {"seed": 42},
                }
                for index, (value, tags) in enumerate(
                    [(0.25, ["dashboard"]), ("PRIVATE_VALUE", ["review"])]
                )
            ]
            chunk = ("\n".join(json.dumps(event) for event in events) + "\n").encode()
            dash.telemetry.append_chunk(study, "live", source, chunk, offset=0)
            value = dash.telemetry_view(root)
            self.assertEqual(value["streams"][0]["events"][0]["value"], 0.25)
            self.assertNotIn("PRIVATE_VALUE", json.dumps(value))
            page = dash.telemetry_view(
                root, source_id="active", stream_id="live", after=0
            )
            self.assertEqual(page["streams"][0]["next_offset"], len(chunk))

    def fixture(self):
        return {
            "streams": [
                {
                    "stream_id": "stream1",
                    "source": {"kind": "training", "experiment_id": "0001"},
                    "status": "running",
                    "error": "SECRET",
                    "errors": [{"error": "SECRET"}],
                    "events": [
                        {"name": "loss", "value": 2, "step": 10, "tags": ["dashboard"]},
                        {"name": "private", "value": "SECRET", "tags": ["review"]},
                    ],
                }
            ]
        }

    def test_only_dashboard_events_and_safe_metadata_cross_the_api_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(
                dash.telemetry, "snapshot", return_value=self.fixture()
            ) as snapshot:
                value = dash.telemetry_view(root)
            snapshot.assert_called_once_with(root / "study", tag="dashboard", limit=32)
            self.assertNotIn("SECRET", json.dumps(value))
            self.assertEqual(value["streams"][0]["events"][0]["step"], 10)
            self.assertEqual(value["streams"][0]["status"], "running")

    def test_namespaces_and_long_lanes_keep_colliding_streams_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history"
            lane = root / "study/long-runs/L0123456789abcdef/study"
            lane.mkdir(parents=True)
            with patch.object(dash.telemetry, "snapshot", return_value=self.fixture()):
                value = dash.telemetry_view(root, history_roots=[history])
            keys = [
                (item["source_id"], item["run_id"], item["stream_id"])
                for item in value["streams"]
            ]
            self.assertEqual(len(set(keys)), 3)
            with (
                patch.object(
                    dash.telemetry, "info", return_value=self.fixture()["streams"][0]
                ),
                patch.object(
                    dash.telemetry,
                    "read_events",
                    return_value={"events": [], "next_offset": 100, "byte_end": 200},
                ) as reader,
            ):
                value = dash.telemetry_view(
                    root,
                    history_roots=[history],
                    source_id="history-1",
                    stream_id="stream1",
                    after=20,
                )
            reader.assert_called_once_with(
                history / "study",
                "stream1",
                after=20,
                tag="dashboard",
                max_bytes=131072,
                limit=200,
            )
            self.assertEqual(value["streams"][0]["next_offset"], 100)

    def test_http_all_tag_request_remains_filtered_and_local(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(dash, "ThreadingHTTPServer") as factory:
                factory.return_value.server_port = 0
                dash.serve(root, 0)
                handler = factory.call_args.args[1]
            connection = Mock()
            connection.makefile.return_value = BytesIO(
                b"GET /api/telemetry?tag=all HTTP/1.0\r\n\r\n"
            )
            response = bytearray()
            connection.sendall.side_effect = response.extend
            with (
                patch.object(
                    dash.telemetry, "snapshot", return_value=self.fixture()
                ) as snapshot,
                patch.object(
                    dash,
                    "ProTokBackend",
                    side_effect=AssertionError("no backend calls"),
                ),
            ):
                handler(connection, ("127.0.0.1", 1), Mock())
            self.assertIn(b"200 OK", response)
            self.assertNotIn(b"SECRET", response)
            self.assertEqual(snapshot.call_args.kwargs["tag"], "dashboard")

    def test_explicit_long_run_paging_is_not_limited_to_the_first_32_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(33):
                (root / "study/long-runs" / f"L{index:016x}" / "study").mkdir(
                    parents=True
                )
            run_id = f"L{32:016x}"
            with (
                patch.object(
                    dash.telemetry, "info", return_value=self.fixture()["streams"][0]
                ),
                patch.object(
                    dash.telemetry,
                    "read_events",
                    return_value={"events": [], "next_offset": 40, "byte_end": 80},
                ) as reader,
            ):
                result = dash.telemetry_view(
                    root,
                    source_id="active",
                    run_id=run_id,
                    stream_id="stream1",
                    after=20,
                )
            reader.assert_called_once_with(
                root / "study/long-runs" / run_id / "study",
                "stream1",
                after=20,
                tag="dashboard",
                max_bytes=131072,
                limit=200,
            )
            self.assertEqual(result["streams"][0]["run_id"], run_id)
            self.assertEqual(result["streams"][0]["next_offset"], 40)
