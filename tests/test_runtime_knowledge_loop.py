"""Real Pi, SQLite and loopback provider: publish while the same Plan is reading."""

import json
from contextlib import redirect_stdout
from io import StringIO
import os
import re
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import patch
from uuid import uuid4

from examples.concurrent_runtime import configuration
from integrations.native_runtime import application
from research_loop.runtime.application import load_application, prepare_application
from research_loop.runtime.call_context import preview, read_at_call
from research_loop.runtime.cli import main as runtime_cli
from research_loop.runtime.coordinator import ConcurrentCoordinator
from research_loop.runtime.controls import ControlInbox, submit_command
from research_loop.runtime.knowledge import KnowledgeBase
from research_loop.runtime.model_requests import format_decision_reference
from research_loop.runtime.status import status_snapshot
from research_loop.runtime.store import StateStore


@unittest.skipUnless(
    os.environ.get("NATIVE_PI_CLI"), "Requires installed Pi and loopback"
)
class KnowledgeLoopTests(unittest.TestCase):
    def test_publish_during_plan_and_complete_approved_experiment(self):
        self.first_read = Event()
        self.published = Event()
        self.errors = []
        self.requests = []
        self.plan_reads = []
        self.evidence = None
        self.retry_manager = False
        self.plan_refs = []
        self.task = dict(
            id="replicate",
            priority=1,
            specification={"instruction": "Replicate the observation."},
            dependencies=[],
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = self

            class Endpoint(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    try:
                        packet = json.loads(
                            self.rfile.read(int(self.headers["Content-Length"]))
                        )
                        name, args = case.reply(packet)
                    except Exception as exc:
                        case.errors.append(repr(exc))
                        self.send_error(400)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    call = {
                        "index": 0,
                        "id": "call_" + uuid4().hex,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                    for delta, finish in (
                        ({"tool_calls": [call]}, None),
                        ({}, "tool_calls"),
                    ):
                        chunk = {
                            "id": "synthetic",
                            "object": "chat.completion.chunk",
                            "created": 0,
                            "model": "fixture-model",
                            "choices": [
                                {"index": 0, "delta": delta, "finish_reason": finish}
                            ],
                        }
                        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")

            with ThreadingHTTPServer(("127.0.0.1", 0), Endpoint) as server:
                agent_dir = root / "pi-profile"
                agent_dir.mkdir()
                (agent_dir / "models.json").write_text(
                    json.dumps(
                        {
                            "providers": {
                                "fixture": {
                                    "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                                    "api": "openai-completions",
                                    "apiKey": "synthetic",
                                    "models": [
                                        {"id": "fixture-model", "reasoning": False}
                                    ],
                                }
                            }
                        }
                    )
                )
                (agent_dir / "settings.json").write_text(
                    json.dumps(
                        {
                            "retry": {"enabled": False, "provider": {"maxRetries": 0}},
                            "compaction": {"enabled": False},
                        }
                    )
                )
                serving = Thread(target=server.serve_forever, daemon=True)
                serving.start()
                try:
                    with patch.dict(
                        os.environ, {"PI_CODING_AGENT_DIR": str(agent_dir)}
                    ):
                        self.exercise(root)
                finally:
                    self.published.set()
                    server.shutdown()
                    serving.join()

    def reply(self, packet):
        messages = packet["messages"]
        user = next(m["content"] for m in messages if m["role"] == "user")
        prompt = user if isinstance(user, str) else user[0]["text"]
        package = json.loads(
            prompt.split("ContextPackage:\n", 1)[1].split("\nResponse contract:", 1)[0]
        )
        role = package["manifest"]["role"]
        tools = {item["function"]["name"] for item in packet["tools"]}
        expected = {"ls", "find", "grep", "read", "submit_result"}
        if role == "knowledge_manager":
            expected |= {"edit", "write"}
        self.assertEqual(tools, expected)
        self.requests.append(role)
        results = [m["content"] for m in messages if m["role"] == "tool"]
        turn = len(results)
        refs = []
        if role == "advocate":
            output = {"proposals": [self.task | {"reason": "Synthetic observation"}]}
        elif role == "judge":
            existing = package["context"]["planning"]["pool"]["tasks"]
            output = {"changes": [] if existing else [self.task | {"action": "create"}]}
        elif role == "plan":
            base = "knowledge/study"
            if turn == 0:
                return "read", {"path": f"{base}/files/pages/finding.md"}
            text = (
                results[-1] if isinstance(results[-1], str) else results[-1][0]["text"]
            )
            self.plan_reads.append(text)
            if turn == 1:
                self.assertIn("observation-seven", text)
                return "read", {"path": f"{base}/revisions.json"}
            if turn == 2:
                citation = re.search(r"kb:[A-Za-z0-9_-]+:knowledge:finding@7", text)
                self.assertIsNotNone(citation)
                self.plan_refs.append(citation.group())
                self.first_read.set()
                if not self.published.wait(45):
                    raise TimeoutError(
                        "Manager did not publish while Plan remained active"
                    )
                return "grep", {
                    "pattern": "observation-eight",
                    "path": f"{base}/files",
                }
            if turn == 3:
                self.assertIn("observation-eight", text)
                return "read", {"path": f"{base}/files/notes/rough/observation.txt"}
            if turn == 4:
                self.assertIn("observation-eight", text)
                return "read", {"path": f"{base}/revisions.json"}
            citation = re.search(
                r"kb:[A-Za-z0-9_-]+:file:notes/rough/observation\.txt@1", text
            )
            self.assertIsNotNone(citation)
            refs = [*self.plan_refs, citation.group()]
            output = {
                "timeout_seconds": 10,
                "local_evaluation": None,
                "experiment_definition": "Inspect the two observed revisions.",
            }
        elif role == "knowledge_manager":
            batch = package["context"]["maintenance"]
            evidence = batch["evidence"][0]
            observation = evidence["content"]["content"]["observation"]
            if turn == 0:
                return "write", {
                    "path": "draft/notes/rough/observation.txt",
                    "content": f"observation-{observation}\n",
                }
            refs = [format_decision_reference(evidence["reference"])]
            output = {
                "remove_paths": ["index.md"],
                "processed_through": batch["evidence_through"]
                + int(not self.retry_manager),
            }
        elif role == "execute":
            self.assertIn("plan", package["context"])
            output = {"operations": [], "artifacts": []}
        elif role in {"scout", "review"}:
            if role == "review":
                self.assertIn("execute", package["context"])
            output = {"knowledge": [], "reviews": [], "corrections": []}
        else:
            raise AssertionError(role)
        return "submit_result", {"output": output, "evidence_refs": refs}

    def exercise(self, root):
        cfg = configuration(
            worker_adapter=application.APPLICATION_IDENTITY,
            planning=True,
            worker_slots=3,
            call_timeout_seconds=80,
        )
        cfg["runtime"]["application"] = prepare_application(
            application.__file__,
            {
                "backend_handlers": {},
                "transport": {
                    "backend": "pi",
                    "executable": os.environ["NATIVE_PI_CLI"],
                    "provider": "fixture",
                    "model": "fixture-model",
                    "reasoning": "off",
                    "timeout_seconds": 75,
                },
            },
            application.APPLICATION_IDENTITY,
        )
        study = root / "study"
        store = StateStore.create(
            study, config=cfg, max_calls=40, max_iterations=1, max_seconds=180
        )
        kb = KnowledgeBase(study, config=cfg)
        for n in range(1, 8):
            body = "observation-seven" if n == 7 else f"seed-{n}"
            kb.commit(
                n - 1,
                [dict(id="finding", title="Finding", markdown=body, dependencies=[])],
                "# Index\n",
                0,
                f"seed-{n}",
            )
        with store.coordinator() as owner:
            app = load_application(owner)
            runner = ConcurrentCoordinator(owner, app.invoker, app.validators)
            runner.planning.trigger("start", "manual", {"reason": "synthetic study"})
            runner.request_scout("observe")
            deadline = time.monotonic() + 130
            while time.monotonic() < deadline:
                state = runner.tick()
                self.assertFalse(self.errors, self.errors)
                failed = [
                    (a["role"], a["failure_reason"])
                    for a in owner.snapshot()["attempts"]
                    if a["status"] == "failed"
                ]
                if failed:
                    self.assertEqual(
                        [role for role, _ in failed], ["knowledge_manager"], failed
                    )
                    if not self.retry_manager:
                        self.assertIn("advance", failed[0][1])
                        self.assertEqual(kb.published_version(), 7)
                        self.assertEqual(kb.status()["processed_through"], 0)
                        entry = kb.live(["study"], role="plan")["entries"][0]
                        files = Path(entry["path"]) / "files"
                        self.assertEqual(
                            (files / "pages/finding.md").read_text(),
                            "observation-seven",
                        )
                        self.assertTrue((files / "index.md").is_file())
                        self.assertFalse(
                            (files / "notes/rough/observation.txt").exists()
                        )
                        self.retry_manager = True
                        submit_command(
                            study, "maintain", {}, "retry-after-rejected-draft"
                        )
                        ControlInbox(owner).drain(admit_calls=True)
                if self.first_read.is_set() and self.evidence is None:
                    self.evidence = kb.publish_evidence(
                        "E8", "experiment_receipt", {"observation": "eight"}
                    )
                if (
                    self.evidence
                    and not self.published.is_set()
                    and kb.published_version() == 8
                ):
                    self.published.set()
                if not state["waiting"] and not state["progress"]:
                    break
                time.sleep(0.03)
            else:
                self.fail(f"Timed out: {state}")
            final = owner.snapshot()
            self.assertTrue(self.retry_manager)
            self.assertEqual(
                [row["status"] for row in final["iterations"]], ["published"]
            )
            self.assertEqual(set(self.requests), set(application.policy.MODELS))
            self.assertEqual(final["counters"]["calls"], len(self.requests))
            plans = [a for a in final["attempts"] if a["role"] == "plan"]
            self.assertEqual(len(plans), 1)
            info = preview(store, plans[0]["id"])
            manager = next(
                a
                for a in final["attempts"]
                if a["role"] == "knowledge_manager" and a["status"] == "accepted"
            )
            self.assertEqual(
                preview(store, manager["id"])["decision_refs"],
                [f"kb:{kb.library_id}:evidence:E8@1"],
            )
            self.assertEqual(info["knowledge_endpoints"]["start_version"], 7)
            self.assertEqual(info["knowledge_endpoints"]["end_version"], 8)
            self.assertEqual(
                set(info["decision_refs"]),
                {
                    f"kb:{kb.library_id}:knowledge:finding@7",
                    f"kb:{kb.library_id}:file:notes/rough/observation.txt@1",
                },
            )
            capture = StringIO()
            with redirect_stdout(capture):
                self.assertEqual(
                    runtime_cli(["context", str(study), plans[0]["id"], "--expand"]), 0
                )
            decision = json.loads(capture.getvalue())
            self.assertEqual(
                decision["accepted_output"]["experiment_definition"],
                "Inspect the two observed revisions.",
            )
            self.assertEqual(decision["task_id"], self.task["id"])
            self.assertTrue(
                decision["accepted_output_ref"] and decision["invocation_id"]
            )
            self.assertEqual(
                read_at_call(store, plans[0]["id"], "study", "finding", version=7),
                "observation-seven",
            )
            capture = StringIO()
            with redirect_stdout(capture):
                self.assertEqual(
                    runtime_cli(
                        [
                            "knowledge-read",
                            str(study),
                            plans[0]["id"],
                            "notes/rough/observation.txt",
                            "--version",
                            "8",
                        ]
                    ),
                    0,
                )
            self.assertEqual(capture.getvalue(), "observation-eight\n")
            self.assertFalse((files / "index.md").exists())
            self.assertEqual(status_snapshot(study)["knowledge"]["live_version"], 8)
