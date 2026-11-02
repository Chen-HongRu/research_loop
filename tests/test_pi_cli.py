"""Opt-in real Pi CLI smoke with a local deterministic model endpoint.

PI_HARNESS_EXECUTABLE=/absolute/path/to/pi uv run python -m unittest discover \
    -s tests -p test_pi_cli.py -v
No subscription, real model request, or user authentication files are used.
"""

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from research_loop.harness import HarnessConfig, completed_response, make_harness


@unittest.skipUnless(
    os.environ.get("PI_HARNESS_EXECUTABLE"), "opt-in real Pi CLI smoke"
)
class PiCliTests(unittest.TestCase):
    def test_real_cli_tools_stdin_events_and_receipts(self):
        for writable, reasoning in ((False, "medium"), (True, "max")):
            with self.subTest(writable=writable, reasoning=reasoning):
                self.run_cli(writable, reasoning)

    def run_cli(self, writable, reasoning):
        requests = []
        answer = {"text": "中文\u2028line\u2029paragraph", "ok": True}
        content = "local tool evidence\n"

        class Endpoint(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                turn = len(requests)
                tool = "write" if writable and turn == 1 else "read"
                if turn <= (2 if writable else 1):
                    arguments = {"path": "artifact.txt"}
                    if tool == "write":
                        arguments["content"] = content
                    delta = {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": f"call_{turn}",
                                "type": "function",
                                "function": {
                                    "name": tool,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ]
                    }
                    finish = "tool_calls"
                else:
                    delta = {"content": json.dumps(answer, ensure_ascii=False)}
                    finish = "stop"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for payload, reason in ((delta, None), ({}, finish)):
                    chunk = {
                        "id": f"local_{turn}",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "local-fixture",
                        "choices": [
                            {"index": 0, "delta": payload, "finish_reason": reason}
                        ],
                    }
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

        with (
            tempfile.TemporaryDirectory() as temporary,
            ThreadingHTTPServer(("127.0.0.1", 0), Endpoint) as server,
        ):
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            run_dir = root / "run"
            run_dir.mkdir()
            agent_dir = root / "agent"
            agent_dir.mkdir()
            (agent_dir / "models.json").write_text(
                json.dumps(
                    {
                        "providers": {
                            "openai-codex": {
                                "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                                "api": "openai-completions",
                                "apiKey": "local-fixture-only",
                                "models": [
                                    {
                                        "id": "gpt-6.1-sol",
                                        "reasoning": True,
                                        "thinkingLevelMap": {"max": "max"},
                                    }
                                ],
                            }
                        }
                    }
                )
            )
            if not writable:
                (workspace / "artifact.txt").write_text(content)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(agent_dir)}):
                    harness = make_harness(
                        HarnessConfig(
                            backend="pi",
                            executable=os.environ["PI_HARNESS_EXECUTABLE"],
                            reasoning=reasoning,
                            timeout_seconds=30,
                        )
                    )
                    result = harness.run(
                        prompt="Inspect local tool evidence.\n" + "context " * 30000,
                        schema={"type": "object"},
                        workspace=workspace,
                        run_dir=run_dir,
                        writable=writable,
                    )
            finally:
                server.shutdown()
                worker.join()
            self.assertEqual(result, answer)
            self.assertEqual(completed_response(run_dir), answer)
            self.assertEqual((workspace / "artifact.txt").read_text(), content)
            self.assertEqual(len(requests), 3 if writable else 2)
            for request in requests:
                self.assertEqual(request["model"], "gpt-6.1-sol")
                self.assertEqual(request["reasoning_effort"], reasoning)
                tools = {tool["function"]["name"] for tool in request["tools"]}
                self.assertEqual(
                    tools,
                    {"read", "grep", "find", "ls"}
                    | ({"write", "edit", "bash"} if writable else set()),
                )
            self.assertTrue(
                any(
                    message.get("role") == "tool"
                    and content.strip() in message["content"]
                    for message in requests[-1]["messages"]
                )
            )
            process = json.loads((run_dir / "process.json").read_text())
            self.assertEqual(process["status"], "succeeded")
            self.assertEqual(process["requested_reasoning"], reasoning)
            self.assertEqual(
                process["reasoning_verification"]["thinkingLevel"], reasoning
            )
            self.assertNotIn("context ", json.dumps(process["argv"]))
