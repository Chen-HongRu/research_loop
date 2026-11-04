"""Public SQLite CLI with real local callbacks and concurrent control ingress."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from examples import coordinator_fixture
from examples.concurrent_runtime import configuration
from research_loop.cli import main
from research_loop.runtime.controls import receipt
from research_loop.runtime.store import StateStore


class RuntimeCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "study"
        self.module = self.base / "application.py"
        self.module.write_text(Path(coordinator_fixture.__file__).read_text())
        self.configuration = self.base / "config.json"
        self.configuration.write_text(json.dumps(configuration()))
        self.application = self.base / "application.json"
        self.spec = self.base / "task.json"
        self.spec.write_text(json.dumps({"instruction": "Synthetic command path"}))

    def cli(self, *arguments, expected=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["runtime", *map(str, arguments)])
        self.assertEqual(code, expected, stderr.getvalue() + stdout.getvalue())
        return json.loads(stdout.getvalue()) if stdout.getvalue() else stderr.getvalue()

    def initialize(self, **fixture):
        self.application.write_text(json.dumps({"root": str(self.root), **fixture}))
        return self.cli(
            "init",
            self.root,
            "--config",
            self.configuration,
            "--application",
            self.module,
            "--application-config",
            self.application,
            "--max-iterations",
            "5",
            "--max-calls",
            "20",
            "--max-seconds",
            "60",
        )

    def task(self, identity="one", *extra):
        return self.cli(
            "task",
            "create",
            self.root,
            identity,
            "--specification",
            self.spec,
            "--request-id",
            "task-" + identity,
            *extra,
        )

    def test_initialize_task_run_publication_and_readonly_status(self):
        initialized = self.initialize(
            backend_tasks=["one"], lost_ack=["one"], fail_collection_once=["one"]
        )
        self.assertEqual(initialized["callbacks_invoked"], 0)
        self.assertFalse((self.root / "fixture").exists())
        created = self.task()
        self.assertEqual(created["status"], "applied")
        self.assertEqual(self.task(), created)
        result = self.cli("run", self.root)
        self.assertEqual(result["state"], "idle")
        self.assertEqual(result["iterations"][0]["status"], "published")
        before = StateStore(self.root).snapshot()
        status = self.cli("status", self.root)
        self.assertEqual(status["publication_version"], 1)
        self.assertEqual(StateStore(self.root).snapshot(), before)
        actions = [
            json.loads(path.read_text())["action"]
            for path in (self.root / "fixture").glob("action-*.json")
        ]
        self.assertEqual(actions.count("submit"), 1)
        self.assertEqual(actions.count("collection"), 2)

    def test_mutations_and_stop_work_without_loading_changed_application(self):
        self.initialize()
        self.module.write_text(
            "raise AssertionError('Changed module must not import for status or STOP')\n"
        )
        self.task()
        self.assertEqual(
            self.cli(
                "stop",
                self.root,
                "--reason",
                "Operator stop",
                "--request-id",
                "stop-one",
            )["status"],
            "applied",
        )
        self.assertTrue(self.cli("status", self.root)["stop"]["requested"])
        self.assertEqual(
            self.cli("request-status", self.root, "stop-one")["status"], "applied"
        )
        self.assertIn("changed", self.cli("run", self.root, expected=1).lower())
        self.assertFalse((self.root / "fixture").exists())

    def test_events_are_readonly_until_explicit_ack_request(self):
        self.initialize()
        self.cli(
            "stop", self.root, "--reason", "Synthetic stop", "--request-id", "stop"
        )
        before = StateStore(self.root).snapshot()
        events = self.cli("events", self.root, "--consumer", "test-consumer")
        self.assertEqual(StateStore(self.root).snapshot(), before)
        event = next(row for row in events if row["kind"] == "stop")
        self.cli(
            "ack-event",
            self.root,
            event["id"],
            "--consumer",
            "test-consumer",
            "--request-id",
            "ack",
        )
        self.assertNotIn(
            event["id"],
            [
                row["id"]
                for row in self.cli("events", self.root, "--consumer", "test-consumer")
            ],
        )

    def test_idle_scout_request_is_admitted_by_the_actual_runner(self):
        self.initialize()
        requested = self.cli("scout", self.root, "--request-id", "scout-once")
        self.assertEqual(requested["status"], "pending")
        self.assertEqual(
            self.cli("status", self.root)["control_requests"],
            [{"request_id": "scout-once", "kind": "scout", "status": "pending"}],
        )
        self.assertEqual(StateStore(self.root).snapshot()["counters"]["calls"], 0)
        self.cli("run", self.root)
        state = StateStore(self.root).snapshot()
        self.assertEqual(state["knowledge_version"], 1)
        self.assertEqual(
            [(row["role"], row["status"]) for row in state["attempts"]],
            [("scout", "accepted")],
        )
        self.assertEqual(
            self.cli("scout", self.root, "--request-id", "scout-once")["status"],
            "applied",
        )
        self.cli("run", self.root)
        self.assertEqual(StateStore(self.root).snapshot()["counters"]["calls"], 1)

    def test_idle_review_retry_runs_only_review_and_preserves_operation(self):
        self.initialize(backend_tasks=["one"], fail_review_once=["one"])
        self.task()
        self.cli("run", self.root)
        store = StateStore(self.root)
        state = store.snapshot()
        iteration = state["iterations"][0]
        self.assertEqual(
            (iteration["phase"], iteration["status"]), ("review", "failed")
        )
        with store._connection(readonly=True) as conn:
            proof = conn.execute(
                "SELECT completion_ref FROM dispatch_runs WHERE category='call' AND settlement_status='failed'"
            ).fetchone()[0]
        requested = self.cli(
            "retry-review",
            self.root,
            iteration["id"],
            "--expected-revision",
            iteration["revision"],
            "--proof-ref",
            proof,
            "--reason",
            "Explicit fixture review retry",
            "--request-id",
            "review-once",
        )
        self.assertEqual(requested["status"], "pending")
        self.assertEqual(store.snapshot()["counters"]["calls"], 3)
        self.cli("run", self.root)
        self.assertEqual(store.snapshot()["iterations"][0]["status"], "published")
        self.assertEqual(store.snapshot()["counters"]["calls"], 4)
        self.assertEqual(
            [row["role"] for row in store.snapshot()["attempts"]].count("execute"), 1
        )
        actions = [
            json.loads(path.read_text())["action"]
            for path in (self.root / "fixture").glob("action-*.json")
        ]
        self.assertEqual(actions.count("submit"), 1)

    def test_watch_owner_accepts_other_cli_task_and_stop_requests(self):
        self.initialize(role_delays={"plan": 0.5})
        output = self.base / "run-output.json"
        errors = self.base / "run-errors.txt"
        with output.open("w") as stdout, errors.open("w") as stderr:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "research_loop",
                    "runtime",
                    "run",
                    str(self.root),
                    "--watch",
                    "--poll-interval",
                    "0.02",
                ],
                stdout=stdout,
                stderr=stderr,
            )
            try:
                deadline = time.monotonic() + 5
                while (
                    not StateStore(self.root).snapshot()["owner_open"]
                    and time.monotonic() < deadline
                ):
                    if process.poll() is not None:
                        self.fail(errors.read_text())
                    time.sleep(0.01)
                self.assertTrue(StateStore(self.root).snapshot()["owner_open"])
                self.assertEqual(self.task()["status"], "queued")
                while (
                    not list((self.root / "fixture").glob("call-*.json"))
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                stopped = self.cli(
                    "stop",
                    self.root,
                    "--reason",
                    "Concurrent stop",
                    "--request-id",
                    "live-stop",
                )
                self.assertEqual(stopped["status"], "queued")
                process.wait(timeout=8)
                self.assertEqual(process.returncode, 0, errors.read_text())
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
        self.assertTrue(json.loads(output.read_text())["stop"]["requested"])
        self.assertEqual(receipt(self.root, "live-stop")["status"], "applied")
        self.assertEqual(
            [row["role"] for row in StateStore(self.root).snapshot()["attempts"]],
            ["plan"],
        )

    def test_invalid_settings_fail_before_creating_study(self):
        config = configuration(worker_slots=0)
        self.configuration.write_text(json.dumps(config))
        self.application.write_text("{}")
        self.cli(
            "init",
            self.root,
            "--config",
            self.configuration,
            "--application",
            self.module,
            "--max-iterations",
            "1",
            "--max-calls",
            "3",
            "--max-seconds",
            "60",
            expected=1,
        )
        self.assertFalse((self.root / "runtime.sqlite3").exists())

    def test_legacy_entry_still_rejects_sqlite_and_task_conflict_is_visible(self):
        self.initialize()
        self.task()
        self.spec.write_text(json.dumps({"instruction": "Different task"}))
        self.cli(
            "task",
            "create",
            self.root,
            "one",
            "--specification",
            self.spec,
            "--request-id",
            "task-one",
            expected=1,
        )
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["status", str(self.root)]), 1)


if __name__ == "__main__":
    unittest.main()
