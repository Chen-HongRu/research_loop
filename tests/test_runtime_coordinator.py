"""End-to-end local workers, SQLite owner recovery and independent task progress."""

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from examples import coordinator_fixture
from examples.concurrent_runtime import ADAPTER, configuration
from examples.runtime_publication import SyntheticSession
from research_loop.runtime.coordinator import ConcurrentCoordinator
from research_loop.runtime.invocations import LocalProcessInvoker
from research_loop.runtime.store import StateStore
from research_loop.runtime.updates import CallIdentity


class RuntimeCoordinatorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = None

    def start(self, *, settings=None, fixture=None, max_seconds=60):
        self.store = StateStore.create(
            self.root,
            config=configuration(**(settings or {})),
            max_calls=40,
            max_iterations=10,
            max_seconds=max_seconds,
        )
        self.fixture = {"root": str(self.root), **(fixture or {})}
        self.session = self.store.coordinator()
        self.owner = self.session.__enter__()
        self.addCleanup(lambda: self.session.__exit__(None, None, None))
        self.components()

    def components(self):
        self.invoker = LocalProcessInvoker(
            self.root, coordinator_fixture.__file__, ADAPTER, self.fixture
        )
        self.runner = ConcurrentCoordinator(
            self.owner,
            self.invoker,
            {
                role: lambda output, _: output
                for role in ("plan", "execute", "review", "scout", "advocate", "judge")
            },
        )

    def reopen(self):
        self.session.__exit__(None, None, None)
        self.session = self.store.coordinator()
        self.owner = self.session.__enter__()
        self.components()

    def pump(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        result = None
        while time.monotonic() < deadline:
            result = self.runner.tick()
            if predicate():
                return result
            time.sleep(0.02)
        self.fail(
            f"No terminal progress: {result}; {self.owner.snapshot()['attempts']}"
        )

    def iterations(self):
        return {row["task_id"]: row for row in self.owner.snapshot()["iterations"]}

    def records(self, prefix):
        return [
            json.loads(path.read_text())
            for path in (self.root / "fixture").glob(prefix + "-*.json")
        ]

    def test_parallel_plans_failure_isolation_and_published_dependency(self):
        self.start(fixture={"plan_barrier": ["a", "b"], "fail_execute": ["a"]})
        self.owner.create_task("a", {}, priority=1)
        self.owner.create_task("b", {}, priority=2)
        self.owner.create_task("c", {}, dependencies=["b"])
        self.owner.create_task("blocked-by-a", {}, dependencies=["a"])
        self.pump(lambda: self.iterations().get("c", {}).get("status") == "published")
        state = self.iterations()
        self.assertEqual(state["a"]["status"], "failed")
        self.assertEqual(state["b"]["status"], "published")
        self.assertEqual(state["c"]["status"], "published")
        self.assertNotIn("blocked-by-a", state)
        self.assertEqual(self.owner.snapshot()["counters"]["calls"], 8)
        self.assertEqual(self.runner.ledger.active_count(), 0)
        self.assertTrue(all(run["settled"] for run in self.runner.ledger.list()))

    def test_lost_submit_ack_restart_collection_retry_and_review_only_retry(self):
        self.start(
            fixture={
                "backend_tasks": ["b"],
                "lost_ack": ["b"],
                "fail_collection_once": ["b"],
                "fail_review_once": ["b"],
            }
        )
        self.owner.create_task("b", {})
        self.pump(
            lambda: any(
                row["action_kind"] == "submit" for row in self.runner.ledger.list()
            )
        )
        self.reopen()
        self.pump(lambda: self.iterations()["b"]["status"] == "failed")
        iteration = self.iterations()["b"]
        self.assertEqual(iteration["phase"], "review")
        failed = next(
            row
            for row in self.runner.ledger.list("call")
            if row["settlement_status"] == "failed"
        )
        self.runner.ledger.retry_phase(
            iteration["id"],
            iteration["revision"],
            "Explicit fixture Review retry",
            failed["completion_ref"],
            "review-retry",
        )
        self.pump(
            lambda: (
                self.iterations()["b"]["status"] == "published"
                and all(op["costs_final"] for op in self.runner.operations.list())
            )
        )
        calls, actions = self.records("call"), self.records("action")
        self.assertEqual([row["role"] for row in calls].count("plan"), 1)
        self.assertEqual([row["role"] for row in calls].count("execute"), 1)
        self.assertEqual([row["role"] for row in calls].count("review"), 2)
        self.assertEqual([row["action"] for row in actions].count("submit"), 1)
        self.assertEqual([row["action"] for row in actions].count("collection"), 2)
        operation = self.runner.operations.list()[0]
        self.assertEqual(operation["job_id"], "fixture-job-b")
        self.assertEqual(operation["held_resources"], {})
        self.assertTrue(operation["feedback_sealed"])

    def test_old_completed_agent_is_accepted_without_redispatch(self):
        self.start(fixture={"role_delays": {"plan": 0.1}})
        self.owner.create_task("one", {})
        self.runner.tick()
        token = self.runner.ledger.list("call")[0]["token"]
        self.reopen()
        self.pump(lambda: self.iterations()["one"]["status"] == "published")
        run = self.runner.ledger.get(token)
        self.assertLess(run["owner_epoch"], self.owner.owner_epoch)
        self.assertEqual(run["settlement_status"], "accepted")
        self.assertEqual(len(self.records("call")), 3)

    def test_unmanaged_active_attempt_remains_visible_without_invented_proof(self):
        self.start()
        self.owner.create_task("one", {})
        iteration = self.owner.claim_next()
        attempt = self.owner.begin_attempt(
            "plan",
            request_id="manual-external",
            iteration_id=iteration["id"],
            expected_revision=iteration["revision"],
        )
        self.reopen()
        result = self.runner.run_until_idle()
        self.assertTrue(
            any(
                row.get("attempt_id") == attempt["id"] and "Unmanaged" in row["reason"]
                for row in result["blocked"]
            )
        )
        self.assertEqual(self.runner.ledger.list(), [])
        self.assertEqual(self.owner.snapshot()["attempts"][0]["status"], "active")

    def test_budget_expires_during_context_build_retires_without_launch_or_refund(self):
        self.start(max_seconds=1)
        self.owner.create_task("one", {})
        now = [time.time()]
        self.store._clock = lambda: now[0]
        build = self.runner.contexts.build

        def expire(call):
            package = build(call)
            now[0] += 2
            return package

        with patch.object(self.runner.contexts, "build", side_effect=expire):
            self.runner.run_until_idle()
        self.assertEqual(self.iterations()["one"]["status"], "failed")
        self.assertEqual(self.owner.snapshot()["counters"]["calls"], 1)
        self.assertEqual(self.runner.ledger.list()[0]["status"], "not_started")
        self.assertEqual(self.records("call"), [])

    def test_unbound_review_reservation_can_retry_after_positive_retirement(self):
        self.start()
        iteration, _ = SyntheticSession(self.owner).trial("one")
        self.owner.begin_attempt(
            "review",
            request_id="runtime:review-before-context",
            iteration_id=iteration["id"],
            expected_revision=iteration["revision"],
        )
        self.reopen()
        self.runner.run_until_idle()
        iteration = self.iterations()["one"]
        retired = self.runner.ledger.list("call")[0]
        self.assertEqual(retired["status"], "not_started")
        retry = self.runner.ledger.retry_phase(
            iteration["id"],
            iteration["revision"],
            "Explicit retry before initial context existed",
            retired["completion_ref"],
            "unbound-review",
        )
        self.assertIsNone(self.runner.ledger.retry_context(retry["id"]))
        self.pump(lambda: self.iterations()["one"]["status"] == "published")
        self.assertEqual([row["role"] for row in self.records("call")], ["review"])

    def test_pending_planning_precedes_autonomous_claims_but_not_human_work(self):
        self.start(settings={"planning": True})
        for index in range(4):
            self.owner.create_task(f"auto-{index}", {}, source="autonomous")
        self.owner.create_task("human", {}, priority=1000)
        self.runner.planning.trigger(
            "fair-plan", "manual", {"reason": "Synthetic pending planning"}
        )
        self.runner.tick()
        self.assertEqual(set(self.iterations()), {"human"})
        self.pump(
            lambda: any(
                row["role"] == "advocate" for row in self.owner.snapshot()["attempts"]
            )
        )
        self.assertEqual(set(self.iterations()), {"human"})
        self.pump(
            lambda: (
                len(self.iterations()) == 5
                and all(
                    row["status"] == "published" for row in self.iterations().values()
                )
            )
        )

    def test_stop_retires_reserved_and_registered_but_unlaunched_calls(self):
        self.start()
        reserved = self.runner.request_scout("reserved")
        registered = self.runner.request_scout("registered")
        call = CallIdentity.from_attempt(self.owner.snapshot()["study_id"], registered)
        with patch.object(self.runner, "_launch", return_value=False):
            self.runner._dispatch_call(registered, time.time())
        self.runner.stop("Synthetic STOP before launch")
        result = self.runner.run_until_idle()
        self.assertEqual(result["waiting"], [])
        self.assertEqual(self.records("call"), [])
        self.assertEqual(self.owner.snapshot()["counters"]["calls"], 2)
        self.assertEqual(
            {row["id"] for row in self.owner.snapshot()["attempts"]},
            {reserved["id"], call.attempt_id},
        )
        self.assertTrue(
            all(row["status"] == "failed" for row in self.owner.snapshot()["attempts"])
        )
        self.assertTrue(
            all(row["status"] == "not_started" for row in self.runner.ledger.list())
        )

    def test_planning_recovery_and_scout_publish_without_idle_repetition(self):
        self.start(
            settings={"planning": True}, fixture={"role_delays": {"advocate": 0.1}}
        )
        self.runner.planning.trigger(
            "manual-plan", "manual", {"reason": "Synthetic planning"}
        )
        self.runner.tick()
        self.reopen()

        def applied():
            with self.store._connection(readonly=True) as conn:
                return (
                    conn.execute(
                        "SELECT COUNT(*) FROM planning_rounds WHERE status='applied'"
                    ).fetchone()[0]
                    == 1
                )

        self.pump(applied)
        self.assertEqual(self.owner.snapshot()["counters"]["calls"], 3)
        self.runner.request_scout("once")
        self.pump(lambda: self.owner.snapshot()["knowledge_version"] == 1)
        before = self.owner.snapshot()["counters"]["calls"]
        for _ in range(3):
            self.runner.tick()
        self.assertEqual(self.owner.snapshot()["counters"]["calls"], before)


if __name__ == "__main__":
    unittest.main()
