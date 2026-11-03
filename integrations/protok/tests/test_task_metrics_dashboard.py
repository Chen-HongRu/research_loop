"""Selected-task telemetry and the protected loopback TensorBoard proxy."""

import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

from integrations.protok import dashboard as dash


class TaskMetricsTests(unittest.TestCase):
    def put_stream(self, study, stream, experiment, value, tags=("dashboard",)):
        source = {
            "kind": "research",
            "experiment_id": experiment,
            "source_identity": "source-" + stream,
            "execution_identity": "execution-" + stream,
        }
        dash.telemetry.register(study, stream, source)
        event = {
            "version": 1,
            "id": stream,
            "time": 100,
            "name": "train/loss",
            "value": value,
            "step": 10,
            "unit": "loss",
            "tags": list(tags),
            "context": {"seed": 42},
        }
        dash.telemetry.append_chunk(
            study, stream, source, (json.dumps(event) + "\n").encode(), offset=0
        )

    def handler(self, root, **kwargs):
        with patch.object(dash, "ThreadingHTTPServer") as server:
            server.return_value.server_port = 0
            dash.serve(root, 0, **kwargs)
            return server.call_args.args[1]

    def request(self, handler, path, method="GET", body=b"", headers=""):
        connection, response = Mock(), bytearray()
        connection.makefile.return_value = BytesIO(
            (
                method
                + " "
                + path
                + " HTTP/1.0\r\nHost: protected.example\r\n"
                + headers
                + "\r\n"
            ).encode()
            + body
        )
        connection.sendall.side_effect = response.extend
        handler(connection, ("127.0.0.1", 1), Mock())
        return bytes(response)

    def test_explicit_experiment_is_filtered_before_recent_stream_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.put_stream(root / "study", "old-task", "0001", 0.123456789)
            self.put_stream(
                root / "study", "hidden", "0001", "INTERNAL_ONLY", tags=("internal",)
            )
            for index in range(33):
                self.put_stream(
                    root / "study", "other-" + str(index), "0002", 1000 + index
                )
            self.put_stream(
                root / "study/long-runs/L0123456789abcdef/study", "child", "0001", 9999
            )
            result = dash.telemetry_view(root, source_id="active", experiment_id="0001")
            self.assertEqual(
                {stream["stream_id"] for stream in result["streams"]},
                {"old-task", "hidden"},
            )
            shown = [
                event["value"]
                for stream in result["streams"]
                for event in stream["events"]
            ]
            self.assertEqual(shown, [0.123456789])
            self.assertNotIn("INTERNAL_ONLY", json.dumps(result))

    def test_task_query_rejects_ambiguous_scope_and_supports_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, history = Path(temporary) / "active", Path(temporary) / "history"
            self.put_stream(history / "study", "same-id", "0001", 0.5)
            handler = self.handler(root, history_roots=[history])
            response = self.request(
                handler, "/api/telemetry?source=history-1&experiment=0001"
            )
            self.assertIn(b"200 OK", response)
            self.assertEqual(
                json.loads(response.split(b"\r\n\r\n")[1])["streams"][0]["source_id"],
                "history-1",
            )
            for query in (
                "experiment=0001",
                "source=active&experiment=../state",
                "source=active&experiment=0001&run=L0123456789abcdef",
            ):
                self.assertIn(
                    b"400 Bad Request", self.request(handler, "/api/telemetry?" + query)
                )

    def test_proxy_preserves_get_head_post_queries_and_strips_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            handler = self.handler(Path(temporary), tensorboard_port=6006)
            for method, body in (
                ("GET", b""),
                ("HEAD", b""),
                ("POST", b'{"runs":["active"]}'),
            ):
                response = Mock(status=200)
                response.getheader.return_value = "close"
                response.getheaders.return_value = [
                    ("Content-Type", "application/json"),
                    ("Content-Length", "2"),
                ]
                response.read.side_effect = [b"{}", b""]
                with patch.object(dash.http.client, "HTTPConnection") as factory:
                    factory.return_value.getresponse.return_value = response
                    value = self.request(
                        handler,
                        "/tensorboard/data/plugin/scalars/tags?run=active",
                        method,
                        body,
                        "Authorization: Basic PRIVATE\r\nContent-Length: "
                        + str(len(body))
                        + "\r\n",
                    )
                    self.assertIn(b"200 OK", value)
                    factory.assert_called_once_with("127.0.0.1", 6006, timeout=30)
                    call = factory.return_value.request.call_args
                    self.assertEqual(
                        call.args[:2],
                        (method, "/tensorboard/data/plugin/scalars/tags?run=active"),
                    )
                    self.assertNotIn("Authorization", call.kwargs["headers"])
                    self.assertEqual(
                        call.kwargs["body"], body if method == "POST" else None
                    )
                    if method == "HEAD":
                        response.read.assert_not_called()

    def test_unconfigured_or_failed_tensorboard_does_not_break_dashboard(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertIn(
                b" 503 ",
                self.request(self.handler(root), "/tensorboard/"),
            )
            handler = self.handler(root, tensorboard_port=6006)
            with patch.object(dash.http.client, "HTTPConnection") as factory:
                factory.return_value.request.side_effect = OSError("not listening")
                self.assertIn(b" 503 ", self.request(handler, "/tensorboard/"))
            self.assertIn(b"200 OK", self.request(handler, "/"))
            redirect = self.request(handler, "/tensorboard?test=1")
            self.assertIn(b"Location: /tensorboard/?test=1", redirect)
            self.assertIn(
                b"405 Method Not Allowed",
                self.request(handler, "/api/telemetry", "POST"),
            )

    def test_proxy_scopes_metadata_per_request_and_regenerates_headers(self):
        from urllib.parse import parse_qs, urlencode, urlparse

        payload = {
            "active/#0001": {f"selected/{i}": {} for i in range(34)},
            "active/#0002": {f"other/{i}": {} for i in range(1000)},
        }
        with tempfile.TemporaryDirectory() as temporary:
            handler = self.handler(Path(temporary), tensorboard_port=6006)
            for experiment in ("0001", "0002", None):
                query = (
                    "?"
                    + urlencode(
                        {
                            "dashboard_task": json.dumps(
                                {"source": "active", "experiment": experiment}
                            )
                        }
                    )
                    if experiment
                    else ""
                )
                response = Mock(status=200)
                response.getheader.side_effect = lambda key: (
                    "application/json" if key == "Content-Type" else None
                )
                response.getheaders.return_value = [
                    ("Content-Type", "application/json"),
                    ("Content-Length", "999"),
                    ("ETag", "global"),
                ]
                response.read.side_effect = [json.dumps(payload).encode(), b""]
                with patch.object(dash.http.client, "HTTPConnection") as factory:
                    factory.return_value.getresponse.return_value = response
                    shown = self.request(
                        handler,
                        "/tensorboard/experiment/default/data/plugin/scalars/tags"
                        + query,
                    )
                    sent = factory.return_value.request.call_args
                    self.assertEqual(
                        urlparse(sent.args[1]).path,
                        "/tensorboard/experiment/default/data/plugin/scalars/tags",
                    )
                    forwarded = parse_qs(urlparse(sent.args[1]).query)
                    self.assertEqual(
                        json.loads(forwarded["dashboard_task"][0])
                        if experiment
                        else forwarded,
                        {"source": "active", "experiment": experiment}
                        if experiment
                        else {},
                    )
                    header, body = shown.split(b"\r\n\r\n", 1)
                    parsed = json.loads(body)
                    self.assertEqual(
                        parsed,
                        {"active/#" + experiment: payload["active/#" + experiment]}
                        if experiment
                        else payload,
                    )
                    if experiment:
                        self.assertIn(
                            ("Content-Length: " + str(len(body))).encode(), header
                        )
                        self.assertIn(b"Cache-Control: no-store", header)
                        self.assertNotIn(b"ETag:", header)
                        self.assertEqual(
                            sent.kwargs["headers"]["Accept-Encoding"], "identity"
                        )

    def test_proxy_rejects_invalid_scope_and_foreign_runs_before_upstream(self):
        from urllib.parse import urlencode

        with tempfile.TemporaryDirectory() as temporary:
            handler = self.handler(Path(temporary), tensorboard_port=6006)
            queries = [
                {"dashboard_task": "invalid"},
                {
                    "dashboard_task": json.dumps(
                        {"source": "active", "experiment": "0001"}
                    ),
                    "run": "active/#0002",
                },
            ]
            with patch.object(dash.http.client, "HTTPConnection") as factory:
                for query in queries:
                    shown = self.request(
                        handler,
                        "/tensorboard/data/plugin/scalars/scalars?" + urlencode(query),
                    )
                    self.assertIn(b" 400 ", shown)
                factory.assert_not_called()

    def test_tensorboard_index_initializes_missing_reload_preference_before_app(self):
        with tempfile.TemporaryDirectory() as temporary:
            handler = self.handler(Path(temporary), tensorboard_port=6006)
            original = b'<html><head></head><body><script src="index.js"></script></body></html>'
            response = Mock(status=200)
            response.getheader.side_effect = lambda key: (
                "text/html" if key == "Content-Type" else None
            )
            response.getheaders.return_value = [
                ("Content-Type", "text/html"),
                ("Content-Length", str(len(original))),
                ("ETag", "old"),
            ]
            response.read.return_value = original
            with patch.object(dash.http.client, "HTTPConnection") as factory:
                factory.return_value.getresponse.return_value = response
                shown = self.request(
                    handler,
                    "/tensorboard/",
                    headers="Accept-Encoding: gzip\r\nIf-None-Match: old\r\n",
                )
                sent = factory.return_value.request.call_args.kwargs["headers"]
                self.assertEqual(sent["Accept-Encoding"], "identity")
                self.assertNotIn("If-None-Match", sent)
            header, body = shown.split(b"\r\n\r\n", 1)
            self.assertLess(
                body.index(b"/assets/tensorboard-live.js"), body.index(b"index.js")
            )
            self.assertIn(("Content-Length: " + str(len(body))).encode(), header)
            self.assertNotIn(b"ETag:", header)
