"""Opt-in real Pi CLI test with a synthetic loopback model endpoint."""

import base64
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

    def test_multiturn_tools_are_bound_budgeted_and_terminal(self):
        from research_loop.runtime.model_requests import retrieved_evidence
        from research_loop.runtime.store import StateStore

        schema = {
            "type": "object",
            "properties": {
                "output": {"type": "object"},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["output", "evidence_refs"],
            "additionalProperties": False,
        }
        empty = {"type": "object", "properties": {}, "additionalProperties": False}
        tools = {
            name: {"description": f"Synthetic {name}", "parameters": empty}
            for name in ("read_index", "search", "read")
        }
        for scenario in (
            "multiturn",
            "budget",
            "denied",
            "unknown",
            "http_retry",
            "retry_profile",
            "codex_profile",
        ):
            requests, history, executed = [], [], []
            steps = ["read_index", "search", "read", "submit_result"]
            if scenario == "unknown":
                steps = ["unregistered", "submit_result"]

            class Endpoint(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    request = json.loads(
                        self.rfile.read(int(self.headers["Content-Length"]))
                    )
                    requests.append(request)
                    if scenario == "http_retry":
                        self.send_response(429)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Retry-After", "0")
                        self.end_headers()
                        self.wfile.write(
                            b'{"error":{"message":"synthetic rate limit"}}'
                        )
                        return
                    name = steps[min(len(requests) - 1, len(steps) - 1)]
                    refs = [
                        item["result"]["evidence_ref"]
                        for item in history
                        if item["event"] == "success"
                        and item.get("request", {}).get("name") == "read"
                    ]
                    args = (
                        {"output": {}, "evidence_refs": refs}
                        if name == "submit_result"
                        else {}
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for delta, finish in (
                        (
                            {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": f"call_{len(requests)}",
                                        "type": "function",
                                        "function": {
                                            "name": name,
                                            "arguments": json.dumps(args),
                                        },
                                    }
                                ]
                            },
                            None,
                        ),
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
                self.subTest(scenario=scenario),
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
                                    "api": "openai-codex-responses"
                                    if scenario == "codex_profile"
                                    else "openai-completions",
                                    "apiKey": "synthetic."
                                    + base64.b64encode(
                                        json.dumps(
                                            {
                                                "https://api.openai.com/auth": {
                                                    "chatgpt_account_id": "synthetic-account"
                                                }
                                            }
                                        ).encode()
                                    ).decode()
                                    + ".synthetic"
                                    if scenario == "codex_profile"
                                    else "synthetic",
                                    "models": [
                                        {"id": "fixture-model", "reasoning": False}
                                    ],
                                }
                            }
                        }
                    )
                )
                (root / "settings.json").write_text(
                    json.dumps(
                        {
                            "retry": {
                                "enabled": False,
                                "provider": {
                                    "maxRetries": 3
                                    if scenario == "retry_profile"
                                    else 0
                                },
                            },
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
                study = StateStore.create(
                    root / "study",
                    config={},
                    max_calls=3
                    if scenario == "budget"
                    else (1 if scenario == "http_retry" else 8),
                )

                def handler(name, args):
                    executed.append(name)
                    if scenario == "denied":
                        raise ValueError("Alias outside frozen authorization")
                    return {
                        "content": "# Synthetic",
                        "fixed_ref": {
                            "library_id": "synthetic",
                            "kind": "knowledge",
                            "id": "K1",
                            "revision": 1,
                        },
                        "snapshot": 1,
                    }

                original_settings = (root / "settings.json").read_bytes()
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with (
                        study.coordinator() as owner,
                        patch.dict(os.environ, {"PI_CODING_AGENT_DIR": tmp}),
                    ):
                        attempt = owner.begin_attempt("scout", request_id="synthetic")
                        capability = {
                            "study_root": str(root / "study"),
                            "attempt_id": attempt["id"],
                            "worker_token": attempt["worker_token"],
                            "owner_epoch": attempt["owner_epoch"],
                        }
                        if scenario == "multiturn":
                            response = transport.run(
                                "Retrieve then submit.",
                                config,
                                response_schema=schema,
                                tools=tools,
                                tool_handler=handler,
                                request_budget=capability,
                                history=history,
                            )
                            self.assertEqual(executed, ["read_index", "search", "read"])
                            self.assertEqual(len(requests), 4)
                            self.assertEqual(
                                [
                                    entry["request"]["payload"]
                                    for entry in history
                                    if entry["event"] == "request"
                                    and entry["request"].get("action")
                                    == "provider_request"
                                ],
                                requests,
                            )
                            self.assertTrue(
                                all("recorded_at" in entry for entry in history)
                            )
                            self.assertEqual(study.snapshot()["counters"]["calls"], 4)
                            self.assertEqual(
                                set(response["evidence_refs"]),
                                set(retrieved_evidence(root / "study", attempt["id"])),
                            )
                            self.assertTrue(
                                any(
                                    message.get("role") == "tool"
                                    for message in requests[-1]["messages"]
                                )
                            )
                        else:
                            with self.assertRaises(
                                (
                                    HarnessError,
                                    TimeoutError,
                                    __import__("subprocess").TimeoutExpired,
                                )
                            ):
                                transport.run(
                                    "Retrieve then submit.",
                                    config,
                                    response_schema=schema,
                                    tools=tools,
                                    tool_handler=handler,
                                    request_budget=capability,
                                    history=history,
                                )
                            self.assertLessEqual(
                                len(requests), 3 if scenario == "budget" else 1
                            )
                        self.assertEqual(
                            study.snapshot()["counters"]["calls"], max(1, len(requests))
                        )
                finally:
                    server.shutdown()
                    thread.join()
                self.assertEqual(
                    (root / "settings.json").read_bytes(), original_settings
                )
                self.assertFalse((root / "trust.json").exists())
                if scenario in {"retry_profile", "codex_profile"}:
                    self.assertEqual(len(requests), 0)
                if scenario == "http_retry":
                    self.assertEqual(len(requests), 1)
                traces = list((root / "calls").glob("*/tool-history.jsonl"))
                self.assertTrue(traces)
                self.assertTrue(traces[0].read_text())
                if scenario == "codex_profile":
                    self.assertIn("Set Pi transport to sse", traces[0].read_text())
