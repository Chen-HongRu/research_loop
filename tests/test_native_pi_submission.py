"""Opt-in real Pi native tools against a synthetic loopback provider."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch

from integrations.native_runtime import transport
from research_loop.harness import HarnessError

SCHEMA = {
    "type": "object",
    "properties": {
        "output": {"type": "object"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["output", "evidence_refs"],
    "additionalProperties": False,
}
ANSWER = {"output": {}, "evidence_refs": []}


@contextmanager
def loopback(root, steps, *, before_request=None):
    requests = []

    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            ordinal = len(requests)
            if before_request:
                before_request(ordinal, request)
            name, args = steps[min(ordinal - 1, len(steps) - 1)]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            call = {
                "index": 0,
                "id": f"call_{ordinal}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for delta, finish in (({"tool_calls": [call]}, None), ({}, "tool_calls")):
                chunk = {
                    "id": "local",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "fixture-model",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")

    with ThreadingHTTPServer(("127.0.0.1", 0), Endpoint) as server:
        agent = root / "host-agent"
        agent.mkdir()
        (agent / "models.json").write_text(
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
                                        "response_format": {"type": "json_object"}
                                    },
                                }
                            ],
                        }
                    }
                }
            )
        )
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = dict(
            backend="pi",
            executable=os.environ["NATIVE_PI_CLI"],
            provider="fixture",
            model="fixture-model",
            reasoning="off",
            timeout_seconds=20,
            private_calls_dir=str(root / "calls"),
        )
        try:
            with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(agent)}):
                yield config, requests, agent
        finally:
            server.shutdown()
            thread.join()


def result_text(request, tool_id):
    return next(
        message["content"]
        for message in request["messages"]
        if message.get("tool_call_id") == tool_id
    )


@unittest.skipUnless(
    os.environ.get("NATIVE_PI_CLI"), "Optional real Pi native-tool smoke"
)
class InstalledPiSubmissionTests(unittest.TestCase):
    def test_terminating_tool_with_local_validation(self):
        for valid in (True, False):
            result = dict(ANSWER) if valid else {**ANSWER, "unknown": True}
            with self.subTest(valid=valid), TemporaryDirectory() as tmp:
                root = Path(tmp)
                with loopback(root, [("submit_result", result)]) as (
                    config,
                    requests,
                    agent,
                ):
                    original = (agent / "models.json").read_bytes()
                    if valid:
                        self.assertEqual(
                            transport.run(
                                "Call submit_result.", config, response_schema=SCHEMA
                            ),
                            ANSWER,
                        )
                    else:
                        with self.assertRaises(HarnessError):
                            transport.run(
                                "Call submit_result.", config, response_schema=SCHEMA
                            )
                    self.assertEqual(len(requests), 1)
                    self.assertNotIn("response_format", requests[0])
                    self.assertEqual(
                        [item["function"]["name"] for item in requests[0]["tools"]],
                        ["submit_result"],
                    )
                    self.assertEqual((agent / "models.json").read_bytes(), original)
                self.assertEqual(
                    {path.name for path in (root / "calls").glob("*/*")},
                    {"outcome.json"},
                )

    def test_native_live_reads_and_absolute_symlink_denial(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            from research_loop.runtime.knowledge import KnowledgeBase

            kb = KnowledgeBase(root / "study")
            for version in range(1, 8):
                kb.commit(
                    version - 1,
                    [],
                    None,
                    0,
                    f"generation-{version}",
                    files={"notes/rough/observation.txt": f"K{version} live content"},
                )
            entry = kb.live(["study"], role="scout")["entries"][0]
            scope = Path(entry["isolation_root"])
            current = Path(entry["path"])
            root_inode = scope.stat().st_ino
            current_inode = current.stat().st_ino
            unauthorized = root / "unauthorized.md"
            unauthorized.write_text("SYNTHETIC_UNAUTHORIZED_LIBRARY")
            private = root / "other-draft.md"
            private.write_text("SYNTHETIC_OTHER_PRIVATE_DRAFT")
            steps = [
                ("ls", {"path": "knowledge/study/files/notes/rough"}),
                (
                    "find",
                    {"pattern": "observation.txt", "path": "knowledge/study/files"},
                ),
                (
                    "grep",
                    {
                        "pattern": "K7",
                        "path": "knowledge/study/files/notes/rough/observation.txt",
                    },
                ),
                ("read", {"path": "knowledge/study/files/notes/rough/observation.txt"}),
                ("read", {"path": "knowledge/study/files/notes/rough/observation.txt"}),
                ("read", {"path": str(unauthorized)}),
                ("read", {"path": "knowledge/study/files/notes/rough/escape.txt"}),
                ("read", {"path": str(private)}),
                ("read", {"path": str(root / "host-agent/models.json")}),
                (
                    "read",
                    {"path": "/System/Volumes/Data" + str(unauthorized.resolve())},
                ),
                ("submit_result", ANSWER),
            ]

            def advance(ordinal, request):
                if ordinal == 5:
                    kb.commit(
                        7,
                        [],
                        None,
                        0,
                        "generation-8",
                        files={"notes/rough/observation.txt": "K8 live content"},
                    )
                    (current / "files/notes/rough/escape.txt").symlink_to(unauthorized)

            with loopback(root, steps, before_request=advance) as (config, requests, _):
                result = transport.run(
                    "Read authorized live knowledge then submit.",
                    config,
                    response_schema=SCHEMA,
                    tools=["ls", "find", "grep", "read"],
                    filesystem={
                        "read_roots": [str(scope)],
                        "mounts": {"study": str(current)},
                    },
                )
                self.assertEqual(result, ANSWER)
                self.assertEqual(len(requests), 11)
                self.assertEqual(scope.stat().st_ino, root_inode)
                self.assertNotEqual(current.stat().st_ino, current_inode)
                self.assertIn("observation.txt", result_text(requests[1], "call_1"))
                self.assertIn("observation.txt", result_text(requests[2], "call_2"))
                self.assertIn("K7", result_text(requests[3], "call_3"))
                self.assertIn("K7", result_text(requests[4], "call_4"))
                self.assertIn("K8", result_text(requests[5], "call_5"))
                for ordinal in (6, 7, 8, 9, 10):
                    denied = result_text(requests[ordinal], f"call_{ordinal}")
                    self.assertRegex(denied, "EPERM|EACCES|ENOENT")
                    self.assertNotIn("SYNTHETIC_UNAUTHORIZED_LIBRARY", denied)
                    self.assertNotIn("SYNTHETIC_OTHER_PRIVATE_DRAFT", denied)
                    self.assertNotIn("synthetic-local-key", denied)
                self.assertEqual(
                    {item["function"]["name"] for item in requests[0]["tools"]},
                    {"ls", "find", "grep", "read", "submit_result"},
                )
            self.assertEqual(
                {path.name for path in (root / "calls").glob("*/*")}, {"outcome.json"}
            )

    def test_manager_writes_private_draft_only(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / "library"
            library.mkdir()
            (library / "page.md").write_text("original library")
            draft = root / "private-draft"
            draft.mkdir()
            (draft / "index.md").write_text("old draft")
            outside = root / "outside.md"
            outside.write_text("outside original")
            steps = [
                ("write", {"path": "draft/new.md", "content": "private new"}),
                (
                    "edit",
                    {
                        "path": "draft/index.md",
                        "oldText": "old draft",
                        "newText": "updated draft",
                    },
                ),
                ("write", {"path": "knowledge/alpha/page.md", "content": "forbidden"}),
                ("write", {"path": str(outside), "content": "forbidden"}),
                ("submit_result", ANSWER),
            ]
            with loopback(root, steps) as (config, requests, _):
                self.assertEqual(
                    transport.run(
                        "Edit the private draft then submit.",
                        config,
                        response_schema=SCHEMA,
                        tools=["read", "edit", "write"],
                        filesystem={
                            "read_roots": [str(library)],
                            "write_root": str(draft),
                            "mounts": {"alpha": str(library)},
                        },
                    ),
                    ANSWER,
                )
                self.assertEqual(len(requests), 5)
                self.assertRegex(
                    result_text(requests[3], "call_3"), "EPERM|EACCES|EROFS"
                )
                self.assertRegex(
                    result_text(requests[4], "call_4"), "EPERM|EACCES|EROFS|ENOENT"
                )
            self.assertEqual((draft / "new.md").read_text(), "private new")
            self.assertEqual((draft / "index.md").read_text(), "updated draft")
            self.assertEqual((library / "page.md").read_text(), "original library")
            self.assertEqual(outside.read_text(), "outside original")

    def test_manager_cannot_modify_sealed_profile_or_extension(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            draft = root / "private-draft"
            draft.mkdir()
            steps = [("write", {}) for _ in range(5)] + [("submit_result", ANSWER)]
            captured = {}
            launch = transport.host_filesystem.command

            def capture(*args):
                captured["state"], captured["control"] = args[3], args[2]
                return launch(*args)

            def target_paths(ordinal, request):
                if ordinal == 1:
                    state = captured["state"]
                    captured["sealed_mode"] = state.stat().st_mode & 0o777
                    captured["profile_removed"] = not (state / "models.json").exists()
                    paths = [
                        state / "settings.json",
                        state / "models.json",
                        captured["control"] / "submit_result.ts",
                        state / "extensions/injected.ts",
                        root / "host-agent/models.json",
                    ]
                    for index, path in enumerate(paths):
                        steps[index] = (
                            "write",
                            {"path": str(path), "content": "forbidden mutation"},
                        )

            with loopback(root, steps, before_request=target_paths) as (
                config,
                requests,
                agent,
            ):
                original = (agent / "models.json").read_bytes()
                with patch.object(
                    transport.host_filesystem, "command", side_effect=capture
                ):
                    self.assertEqual(
                        transport.run(
                            "Try the supplied file operations then submit.",
                            config,
                            response_schema=SCHEMA,
                            tools=["write"],
                            filesystem={"write_root": str(draft)},
                        ),
                        ANSWER,
                    )
                self.assertEqual(captured["sealed_mode"], 0o500)
                self.assertTrue(captured["profile_removed"])
                self.assertEqual(len(requests), 6)
                for ordinal in range(1, 6):
                    self.assertRegex(
                        result_text(requests[ordinal], f"call_{ordinal}"),
                        "EPERM|EACCES|EROFS",
                    )
                self.assertEqual((agent / "models.json").read_bytes(), original)
            self.assertFalse(captured["state"].exists())

    def test_wrong_model_never_sends_provider_http(self):
        with TemporaryDirectory() as tmp:
            with loopback(Path(tmp), [("submit_result", ANSWER)]) as (
                config,
                requests,
                _,
            ):
                config["model"] = "missing-model"
                with self.assertRaises(HarnessError):
                    transport.run("Submit.", config, response_schema=SCHEMA)
                self.assertEqual(requests, [])

    def test_native_rounds_consume_provider_budget(self):
        from research_loop.runtime.store import StateStore

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / "library"
            library.mkdir()
            (library / "page.md").write_text("Synthetic budget fixture")
            steps = [("read", {"path": "knowledge/alpha/page.md"})] * 3 + [
                ("submit_result", ANSWER)
            ]
            with loopback(root, steps) as (config, requests, _):
                study = StateStore.create(root / "study", config={}, max_calls=2)
                with study.coordinator() as owner:
                    attempt = owner.begin_attempt("scout", request_id="synthetic")
                    budget = {
                        "study_root": str(root / "study"),
                        "attempt_id": attempt["id"],
                        "worker_token": attempt["worker_token"],
                        "owner_epoch": attempt["owner_epoch"],
                    }
                    with self.assertRaises(
                        (HarnessError, TimeoutError, subprocess.TimeoutExpired)
                    ):
                        transport.run(
                            "Read then submit.",
                            config,
                            response_schema=SCHEMA,
                            tools=["read"],
                            filesystem={
                                "read_roots": [str(library)],
                                "mounts": {"alpha": str(library)},
                            },
                            request_budget=budget,
                        )
                self.assertEqual(len(requests), 2)
                self.assertEqual(study.snapshot()["counters"]["calls"], 2)
