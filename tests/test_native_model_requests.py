"""Actual SQLite request admission and retrieval receipts without model calls."""

from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from research_loop.runtime.invocations import _identity
from research_loop.runtime.model_requests import (
    call_trace,
    check_active,
    consulted_refs,
    record_consulted,
    record_retrieval,
    register_trace,
    reserve_request,
    retrieved_evidence,
)
from research_loop.runtime.store import (
    BudgetExceeded,
    ConflictError,
    OwnershipError,
    StateStore,
)


def _reserve(capability, ordinal):
    try:
        reserve_request(capability, ordinal)
        return "admitted"
    except BudgetExceeded:
        return "budget"


class ModelRequestTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def capability(self, owner, request):
        attempt = owner.begin_attempt("scout", request_id=request)
        return {
            "study_root": str(self.root),
            "attempt_id": attempt["id"],
            "worker_token": attempt["worker_token"],
            "owner_epoch": attempt["owner_epoch"],
        }

    def test_first_request_uses_existing_reservation_and_replay_is_rejected(self):
        store = StateStore.create(self.root, config={}, max_calls=2)
        with store.coordinator() as owner:
            capability = self.capability(owner, "one")
            reserve_request(capability, 1)
            self.assertEqual(store.snapshot()["counters"]["calls"], 1)
            reserve_request(capability, 2)
            self.assertEqual(store.snapshot()["counters"]["calls"], 2)
            with self.assertRaises(ConflictError):
                reserve_request(capability, 2)
            with self.assertRaises(BudgetExceeded):
                reserve_request(capability, 3)
            self.assertEqual(store.snapshot()["counters"]["calls"], 2)

    def test_cross_process_admission_cannot_overspend_last_call(self):
        store = StateStore.create(self.root, config={}, max_calls=3)
        with store.coordinator() as owner:
            capabilities = [self.capability(owner, request) for request in ("a", "b")]
            for capability in capabilities:
                reserve_request(capability, 1)
            with ProcessPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(_reserve, capabilities, (2, 2)))
            self.assertCountEqual(outcomes, ["admitted", "budget"])
            self.assertEqual(store.snapshot()["counters"]["calls"], 3)

    def test_stop_timeout_and_owner_fence_apply_to_followup_requests(self):
        store = StateStore.create(self.root, config={})
        with store.coordinator() as owner:
            capability = self.capability(owner, "one")
            reserve_request(capability, 1)
            with self.assertRaises(BudgetExceeded):
                reserve_request({**capability, "deadline": time.time() - 1}, 2)
            with self.assertRaises(OwnershipError):
                reserve_request({**capability, "worker_token": "forged"}, 2)
            register_trace(capability, self.root / "private-call")
            self.assertEqual(
                call_trace(self.root, capability["attempt_id"]),
                str((self.root / "private-call").resolve()),
            )
            owner.set_stop("stop test")
            with self.assertRaises(BudgetExceeded):
                check_active(capability)
            with self.assertRaises(BudgetExceeded):
                reserve_request(capability, 2)
        with self.assertRaises(OwnershipError):
            reserve_request(capability, 2)

    def test_retrieval_receipts_are_immutable_and_scoped_to_attempt(self):
        store = StateStore.create(self.root, config={})
        with store.coordinator() as owner:
            capability = self.capability(owner, "one")
            reference = record_retrieval(
                capability,
                "read",
                {"path": "page.md"},
                {"fixed_ref": "study:K1@1", "snapshot": 1, "content": "# Synthetic"},
            )
            self.assertEqual(
                retrieved_evidence(self.root, capability["attempt_id"]),
                {reference: _identity(self.root, reference)},
            )
            self.assertEqual(retrieved_evidence(self.root, "other-attempt"), {})
            self.assertEqual((self.root / reference).stat().st_mode & 0o222, 0)

    def test_interrupted_tool_cannot_publish_a_late_retrieval(self):
        import json
        import socket
        from threading import Event, Thread

        from integrations.native_runtime.tool_bridge import serve

        store = StateStore.create(self.root, config={})
        started, release = Event(), Event()
        reply = []
        history = []
        directory = self.root / "trace"
        directory.mkdir()

        def handler(name, arguments):
            started.set()
            self.assertTrue(release.wait(5))
            return {"content": "# Synthetic", "fixed_ref": "study:K1@1"}

        def request(path):
            with socket.socket(socket.AF_UNIX) as client:
                client.connect(path)
                client.sendall(
                    b'{"action":"tool","id":"read","name":"read","arguments":{}}\n'
                )
                reply.append(json.loads(client.makefile("rb").readline()))

        with store.coordinator() as owner:
            capability = self.capability(owner, "interrupted")
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                with serve(
                    directory,
                    {"read": {}},
                    handler,
                    capability,
                    time.monotonic() + 10,
                    history,
                ) as (path, state):
                    client = Thread(target=request, args=(path,))
                    client.start()
                    self.assertTrue(started.wait(5))
                    raise RuntimeError("interrupted")
            release.set()
            client.join(timeout=5)
            self.assertFalse(client.is_alive())
            self.assertFalse(reply[0]["ok"])
            self.assertEqual(
                retrieved_evidence(self.root, capability["attempt_id"]), {}
            )
            self.assertEqual(history[-1]["event"], "failure")
            self.assertIn("interrupted", (directory / "tool-history.jsonl").read_text())

    def test_index_search_and_read_disclosures_all_record_typed_dependencies(self):
        import json
        import socket

        from integrations.native_runtime.tool_bridge import serve

        refs = [
            {"library_id": "synthetic", "kind": "knowledge", "id": name, "revision": 1}
            for name in ("from-index", "from-search", "from-read")
        ]
        results = {
            "read_index": {"content": "# Index", "dependencies": [refs[0]]},
            "search": [
                {
                    "title": "Synthetic",
                    "excerpt": "Synthetic snippet",
                    "reference": refs[1],
                }
            ],
            "read": {
                "content": "# Read",
                "fixed_ref": refs[2],
                "dependencies": [refs[0]],
            },
        }
        store = StateStore.create(self.root, config={})
        directory = self.root / "trace"
        directory.mkdir()
        with store.coordinator() as owner:
            capability = self.capability(owner, "disclosures")
            with serve(
                directory,
                {name: {} for name in results},
                lambda name, args: results[name],
                capability,
                time.monotonic() + 10,
                [],
            ) as (path, state):
                for name in results:
                    with socket.socket(socket.AF_UNIX) as client:
                        client.connect(path)
                        client.sendall(
                            (
                                json.dumps(
                                    {
                                        "action": "tool",
                                        "id": name,
                                        "name": name,
                                        "arguments": {"path": "index.md"}
                                        if name == "read"
                                        else {},
                                    }
                                )
                                + "\n"
                            ).encode()
                        )
                        self.assertTrue(
                            json.loads(client.makefile("rb").readline())["ok"]
                        )
            self.assertCountEqual(
                consulted_refs(self.root, capability["attempt_id"]), refs
            )
            self.assertEqual(
                len(retrieved_evidence(self.root, capability["attempt_id"])), 1
            )
            self.assertEqual(consulted_refs(self.root, "unrelated"), [])
            with self.assertRaises(ValueError):
                record_consulted(capability, ["untyped-reference"])
