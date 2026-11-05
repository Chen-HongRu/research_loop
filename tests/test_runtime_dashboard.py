"""Loopback HTTP projection, with no control/mutation or private-body route."""

import http.client
import json
import threading
import unittest

from research_loop.runtime.dashboard import make_server
from tests.test_runtime_status import StatusFixture


class RuntimeDashboardTests(StatusFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.root)
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True
        )
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def request(self, path, method="GET", headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=2
        )
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_status_http_is_read_only_and_allowlisted(self):
        self.reviewed("reviewed", operation=True)
        before = self.store.snapshot()
        status, headers, body = self.request("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        result = json.loads(body)
        self.assertEqual(result["tasks"][0]["status"], "reviewed")
        self.assertFalse(result["tasks"][0]["fees_settled"])
        self.assertNotIn(b"PRIVATE_", body)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertEqual(self.request("/api/status", method)[0], 405)
        self.assertEqual(self.request("/artifacts/private.json")[0], 404)
        self.assertEqual(self.store.snapshot(), before)

    def test_html_has_safe_text_rendering_and_separate_status_axes(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        text = body.decode()
        for expected in (
            "api/status",
            "textContent",
            "Physical released",
            "Fees final",
            "Callback retired",
            "persisted elapsed",
        ):
            self.assertIn(expected, text)
        for forbidden in ("innerHTML", "cdn.", "PRIVATE_", "https://", "<script src="):
            self.assertNotIn(forbidden, text)

    def test_research_content_requires_explicit_opt_in(self):
        for path in ("/ledger", "/api/overview"):
            self.assertEqual(self.request(path)[0], 404)
        with make_server(self.root, research=True) as research_server:
            thread = threading.Thread(
                target=lambda: research_server.serve_forever(poll_interval=0.01),
                daemon=True,
            )
            thread.start()
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", research_server.server_port, timeout=2
                )
                for path, term in (
                    ("/", "Frontier"),
                    ("/ledger", "Physical released"),
                    ("/api/overview", '"study_id"'),
                ):
                    connection.request("GET", path)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200, path)
                    self.assertIn(term, response.read().decode())
                connection.close()
            finally:
                research_server.shutdown()
                thread.join(timeout=2)

    def test_host_header_blocks_rebinding_and_preserves_loopback_requests(self):
        for host in (
            "evil.example",
            "localhost.evil.example",
            "127.0.0.1.evil.example",
            "evil@localhost",
            "localhost:bad",
            "localhost:70000",
        ):
            with self.subTest(host=host):
                status, _, body = self.request("/api/status", headers={"Host": host})
                self.assertEqual(status, 403)
                self.assertNotIn(self.store.snapshot()["study_id"].encode(), body)
        for host in (
            "127.0.0.1",
            "localhost",
            "[::1]",
            f"localhost:{self.server.server_port}",
        ):
            with self.subTest(host=host):
                self.assertEqual(
                    self.request("/api/status", headers={"Host": host})[0], 200
                )

    def test_nonloopback_and_invalid_ports_are_rejected_before_binding(self):
        for host in ("0.0.0.0", "192.0.2.1", "example.invalid"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                make_server(self.root, host=host)
        for port in (-1, 65536, True):
            with self.subTest(port=port), self.assertRaises(ValueError):
                make_server(self.root, port=port)

    def test_unavailable_database_has_generic_error_without_paths(self):
        server = make_server(self.root / "PRIVATE_MISSING_PATH")
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
        )
        thread.start()
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=2
        )
        try:
            connection.request("GET", "/api/status")
            response = connection.getresponse()
            self.assertEqual(response.status, 503)
            self.assertNotIn(b"PRIVATE_MISSING_PATH", response.read())
        finally:
            connection.close()
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
