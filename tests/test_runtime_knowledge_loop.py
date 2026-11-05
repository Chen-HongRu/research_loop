"""Optional end-to-end knowledge Study using a real Pi CLI and loopback provider."""

from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest
from unittest.mock import patch

from examples.concurrent_runtime import configuration
from integrations.native_runtime import application
from research_loop.runtime.application import load_application, prepare_application
from research_loop.runtime.call_context import preview
from research_loop.runtime.cli import main as runtime_cli
from research_loop.runtime.controls import ControlInbox, submit_command
from research_loop.runtime.coordinator import ConcurrentCoordinator
from research_loop.runtime.knowledge import KnowledgeBase
from research_loop.runtime.model_requests import retrieved_evidence
from research_loop.runtime.store import StateStore


@unittest.skipUnless(
    os.environ.get("NATIVE_PI_CLI"), "Requires installed Pi and loopback"
)
class KnowledgeLoopTests(unittest.TestCase):
    def test_manager_revisions_role_config_and_frozen_pi_calls(self):
        """Exercise dispatch, read tools, model admission, acceptance and historical preview."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.requests = []
            self.fail_edit = False
            self.evidence = {}
            self.replies = []
            self.errors = []

            class Endpoint(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(endpoint):
                    packet = json.loads(
                        endpoint.rfile.read(int(endpoint.headers["Content-Length"]))
                    )
                    try:
                        name, args = self.next_tool(packet)
                    except Exception as exc:
                        self.errors.append(repr(exc))
                        endpoint.send_error(400)
                        return
                    endpoint.send_response(200)
                    endpoint.send_header("Content-Type", "text/event-stream")
                    endpoint.end_headers()
                    call = {
                        "index": 0,
                        "id": f"tool_{len(self.requests)}",
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
                        endpoint.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    endpoint.wfile.write(b"data: [DONE]\n\n")

            with ThreadingHTTPServer(("127.0.0.1", 0), Endpoint) as server:
                (root / "models.json").write_text(
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
                (root / "settings.json").write_text(
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
                    with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(root)}):
                        self.exercise_study(root)
                finally:
                    server.shutdown()
                    serving.join()

    def next_tool(self, packet):
        tools = [t["function"]["name"] for t in packet["tools"]]
        messages = packet["messages"]
        user = next(m["content"] for m in messages if m["role"] == "user")
        prompt = user if isinstance(user, str) else user[0]["text"]
        package = json.loads(
            prompt.split("ContextPackage:\n", 1)[1].split("\nResponse contract:", 1)[0]
        )
        role = package["manifest"]["role"]
        self.requests.append(
            (
                role,
                {
                    t["function"]["name"]: t["function"]["parameters"]
                    for t in packet["tools"]
                },
                prompt,
            )
        )
        turn = sum(m["role"] == "tool" for m in messages)
        if role == "knowledge_manager":
            seq = package["context"]["maintenance"]["evidence_through"]
            if seq == 3:
                if turn == 0:
                    return "read_index", {"alias": "literature"}
                # The submitted prose omits its dependency. The controller must
                # retain the disclosure boundary of the returned Index.
                return "submit_result", {
                    "output": {
                        "pages": [
                            {
                                "id": "copied",
                                "title": "Indigo",
                                "markdown": "Indigo copied from a restricted index.",
                                "dependencies": [],
                            }
                        ],
                        "index_markdown": "# Indigo",
                        "processed_through": seq,
                    },
                    "evidence_refs": [],
                }
            evidence = self.evidence[seq]
            if turn == 0:
                return "read", {"alias": "study", "path": evidence}
            if turn == 1:
                return "edit_page", {
                    "page": {
                        "id": "K1",
                        "title": "Finding",
                        "markdown": f"# Finding\n\nResult {seq}.",
                        "dependencies": [evidence],
                    }
                }
            if turn == 2:
                return "edit_index", {
                    "markdown": f"# Index\n\n[K1](K1) — result {seq}."
                }
            output = {
                "pages": [],
                "index_markdown": None,
                "processed_through": seq + int(self.fail_edit),
            }
        elif role == "plan":
            if turn == 0:
                return "read_index", {"alias": "study"}
            if turn == 1 and "search" in tools:
                return "search", {"query": "Finding"}
            if messages[-1].get("role") == "tool":
                last = messages[-1]["content"]
                result = json.loads(last if isinstance(last, str) else last[0]["text"])
                if not isinstance(result, dict) or "evidence_ref" not in result:
                    return "read", {"alias": "study", "path": "K1"}
                if isinstance(result["content"], str):
                    self.replies.append(result)
                    source = next(
                        r for r in result["dependencies"] if r["kind"] == "evidence"
                    )
                    return "read", {"alias": "study", "path": source}
                self.assertEqual(result["content"]["reference"], result["fixed_ref"])
            output = {
                "timeout_seconds": 1,
                "local_evaluation": None,
                "experiment_definition": "Inspect evidence.",
            }
            return "submit_result", {
                "output": output,
                "evidence_refs": [
                    self.replies[-1]["evidence_ref"],
                    result["evidence_ref"],
                ],
            }
        elif role == "scout":
            if turn == 0:
                return "search", {"query": "Indigo"}
            output = {
                "knowledge": [
                    {
                        "id": "observation",
                        "kind": "interpretation",
                        "expected_revision": 0,
                        "statement": "Indigo copied from a restricted snippet.",
                        "conditions": ["Synthetic fixture"],
                        "source_refs": [],
                        "measurement_ids": [],
                        "interpretation": "Indigo",
                        "limitations": ["One observation"],
                    }
                ],
                "reviews": [],
                "corrections": [],
            }
        elif role == "execute":
            output = {"operations": [], "artifacts": []}
        elif role == "review":
            output = {"knowledge": [], "reviews": [], "corrections": []}
        else:
            raise AssertionError(role)
        return "submit_result", {"output": output, "evidence_refs": []}

    def exercise_study(self, root):
        study = root / "study"
        peer = KnowledgeBase(root / "peer")
        peer.commit(
            0,
            [
                {
                    "id": "peer",
                    "title": "Indigo",
                    "markdown": "Indigo reference.",
                    "dependencies": [],
                }
            ],
            "# Indigo literature",
            0,
            "peer-initial",
        )
        cfg = configuration(
            worker_adapter=application.APPLICATION_IDENTITY, call_timeout_seconds=40
        )
        cfg["knowledge_libraries"] = [
            {"alias": "literature", "path": str(peer.root), "version": 1}
        ]
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
                    "timeout_seconds": 30,
                    "private_calls_dir": str(root / "calls"),
                },
            },
            application.APPLICATION_IDENTITY,
        )
        store = StateStore.create(
            study, config=cfg, max_calls=40, max_iterations=2, max_seconds=180
        )
        kb = KnowledgeBase(study, config=cfg)
        with store.coordinator() as owner:
            app = load_application(owner)
            runner = ConcurrentCoordinator(owner, app.invoker, app.validators)

            def drain():
                deadline = time.monotonic() + 75
                while time.monotonic() < deadline:
                    state = runner.tick()
                    if not state["waiting"] and not state["progress"]:
                        self.assertFalse(self.errors, self.errors)
                        return
                    time.sleep(0.03)
                self.fail(str(state))

            def attempts(role):
                return [a for a in owner.snapshot()["attempts"] if a["role"] == role]

            self.evidence[1] = kb.publish_evidence(
                "E1", "experiment_receipt", {"observed": 1}
            )
            drain()
            self.assertEqual(kb.status()["version"], 1, attempts("knowledge_manager"))
            owner.create_task("first", {"instruction": "read the finding"})
            drain()
            plan = attempts("plan")[0]
            self.assertEqual(plan["status"], "accepted", plan["failure_reason"])
            frozen = preview(store, plan["id"], expand=True)
            self.assertEqual(frozen["knowledge_libraries"][0]["version"], 1)
            self.assertEqual(
                self.replies[-1]["content"],
                kb.read(frozen["knowledge_libraries"], "study", "K1"),
            )
            self.assertIn(
                self.replies[-1]["evidence_ref"], retrieved_evidence(study, plan["id"])
            )
            self.evidence[2] = kb.publish_evidence(
                "E2", "run_report", {"counterexample": 2}
            )
            self.fail_edit = True
            drain()
            self.assertEqual(attempts("knowledge_manager")[-1]["status"], "failed")
            self.assertEqual(kb.status()["version"], 1)
            self.assertEqual(kb.status()["processed_through"], 1)
            self.fail_edit = False
            submit_command(study, "maintain", {}, "explicit-retry")
            ControlInbox(owner).drain(admit_calls=True)
            drain()
            self.assertEqual(kb.status()["version"], 2)
            submit_command(
                study,
                "role_config",
                {
                    "role": "plan",
                    "definition": {
                        "knowledge_bases": ["study", "literature"],
                        "tools": ["read_index", "read", "submit_result"],
                    },
                },
                "adjust-plan",
            )
            result = ControlInbox(owner).drain()
            self.assertEqual(result[-1]["status"], "applied", result)
            owner.create_task("second", {"instruction": "read the revised finding"})
            drain()
            second = attempts("plan")[-1]
            self.assertEqual(second["status"], "accepted", second["failure_reason"])
            revised = preview(store, second["id"], expand=True)
            self.assertEqual(
                [e["alias"] for e in revised["knowledge_libraries"]],
                ["study", "literature"],
            )
            self.assertEqual(revised["knowledge_libraries"][0]["version"], 2)
            self.assertEqual(
                kb.read(frozen["knowledge_libraries"], "study", "K1"),
                "# Finding\n\nResult 1.",
            )
            self.assertEqual(self.replies[-1]["content"], "# Finding\n\nResult 2.")
            self.assertEqual(preview(store, plan["id"], expand=True), frozen)
            audit = StringIO()
            with redirect_stdout(audit):
                self.assertEqual(
                    runtime_cli(["context", str(study), plan["id"], "--expand"]), 0
                )
            self.assertEqual(json.loads(audit.getvalue()), frozen)
            human = StringIO()
            with redirect_stdout(human):
                self.assertEqual(
                    runtime_cli(
                        [
                            "knowledge-read",
                            str(study),
                            plan["id"],
                            "K1",
                            "--with-sources",
                        ]
                    ),
                    0,
                )
            human_page = json.loads(human.getvalue())
            self.assertEqual(human_page["content"], self.replies[0]["content"])
            self.assertIn(self.evidence[1], human_page["dependencies"])
            for assembly in (frozen, revised):
                actual = [
                    tools
                    for role, tools, prompt in self.requests
                    if role == "plan" and prompt == assembly["prompt"]
                ]
                self.assertTrue(actual)
                expected = {
                    name: spec["parameters"] for name, spec in assembly["tools"].items()
                }
                for schemas in actual:
                    self.assertEqual(schemas, expected)
            self.assertNotIn("search", revised["tools"])
            for role in ("scout", "knowledge_manager"):
                submit_command(
                    study,
                    "role_config",
                    {
                        "role": role,
                        "definition": {"knowledge_bases": ["study", "literature"]},
                    },
                    "grant-" + role,
                )
            ControlInbox(owner).drain()
            runner.request_scout("considers-literature")
            drain()
            self.assertEqual(
                attempts("scout")[-1]["status"],
                "accepted",
                attempts("scout")[-1]["failure_reason"],
            )
            self.assertEqual(
                kb.status()["processed_through"],
                3,
                attempts("knowledge_manager")[-1]["failure_reason"],
            )
            main = kb.freeze(cfg, ["study"])
            full = kb.freeze(cfg, ["study", "literature"])
            self.assertIn("Indigo", kb.read(full, "study", "copied"))
            self.assertIn("Indigo", kb.read_index(full))
            self.assertTrue(
                any(
                    h["reference"]["kind"] == "evidence"
                    for h in kb.search(full, "Indigo")
                )
            )
            self.assertEqual(kb.search(main, "Indigo"), [])
            self.assertNotIn("Indigo", kb.read_index(main))
            with self.assertRaises(PermissionError):
                kb.read(main, "study", "copied")
            self.assertEqual(owner.snapshot()["counters"]["calls"], len(self.requests))
