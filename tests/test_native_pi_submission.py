"""Opt-in real Pi CLI test with a synthetic loopback model endpoint."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch

from integrations.native_runtime import policy, transport
from research_loop.harness import HarnessError


@unittest.skipUnless(os.environ.get("NATIVE_PI_CLI"), "Optional real Pi tool smoke")
class InstalledPiSubmissionTests(unittest.TestCase):
    def test_terminating_tool_with_local_validation(self):
        schema = policy.response_schema(policy.Advocate)
        for valid in (True, False):
            result = {"output": {"proposals": []}, "evidence_refs": []}
            if not valid:
                result["output"]["unknown"] = True
            requests = []

            class Endpoint(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    requests.append(
                        json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    call = {
                        "index": 0,
                        "id": "call_one",
                        "type": "function",
                        "function": {
                            "name": "submit_result",
                            "arguments": json.dumps(result),
                        },
                    }
                    for delta, finish in (
                        ({"tool_calls": [call]}, None),
                        ({}, "tool_calls"),
                    ):
                        chunk = {
                            "id": "local",
                            "object": "chat.completion.chunk",
                            "created": 0,
                            "model": "fixture-model",
                            "choices": [
                                {"index": 0, "delta": delta, "finish_reason": finish}
                            ],
                        }
                        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")

            with (
                self.subTest(valid=valid),
                TemporaryDirectory() as tmp,
                ThreadingHTTPServer(("127.0.0.1", 0), Endpoint) as server,
            ):
                root = Path(tmp)
                (root / "models.json").write_text(
                    json.dumps(
                        {
                            "providers": {
                                "fixture": {
                                    "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                                    "api": "openai-completions",
                                    "apiKey": "synthetic-local-key",
                                    "models": [
                                        {
                                            "id": "fixture-model",
                                            "reasoning": False,
                                            "samplingParams": {
                                                "response_format": {
                                                    "type": "json_object"
                                                }
                                            },
                                        }
                                    ],
                                }
                            }
                        }
                    )
                )
                (root / "settings.json").write_text(
                    json.dumps(
                        {
                            "retry": {"enabled": False, "provider": {"maxRetries": 0}},
                            "compaction": {"enabled": False},
                        }
                    )
                )
                config = dict(
                    backend="pi",
                    executable=os.environ["NATIVE_PI_CLI"],
                    provider="fixture",
                    model="fixture-model",
                    reasoning="off",
                    timeout_seconds=30,
                    private_calls_dir=str(root / "calls"),
                )
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": tmp}):
                        if valid:
                            self.assertEqual(
                                transport.run(
                                    "Call submit_result.",
                                    config,
                                    response_schema=schema,
                                ),
                                result,
                            )
                        else:
                            with self.assertRaises(HarnessError):
                                transport.run(
                                    "Call submit_result.",
                                    config,
                                    response_schema=schema,
                                )
                finally:
                    server.shutdown()
                    thread.join()
                self.assertEqual(len(requests), 1)
                self.assertNotIn("response_format", requests[0])
                tools = requests[0]["tools"]
                self.assertEqual(
                    [t["function"]["name"] for t in tools], ["submit_result"]
                )
                self.assertEqual(tools[0]["function"]["parameters"], schema)
                self.assertFalse(tools[0]["function"].get("strict", False))
