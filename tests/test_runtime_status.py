"""Coherent, allowlisted runtime monitoring without owner or artifact access."""

from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from examples.backend_lifecycle import CONFIG, REQUEST
from examples.runtime_publication import SyntheticSession
from research_loop.runtime.contexts import RuntimeContexts
from research_loop.runtime.dispatch import DispatchLedger
from research_loop.runtime.files import publish_artifact
from research_loop.runtime.operations import Operations
from research_loop.runtime.publication import Publisher
from research_loop.runtime.status import status_snapshot
from research_loop.runtime.store import StateStore
from research_loop.runtime.updates import CallIdentity


class StatusFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = deepcopy(CONFIG)
        config["operation_authorizations"]["fixture-grant"]["max_operations"] = 10
        config["operation_authorizations"]["fixture-grant"]["cost_limits"] = {
            "fixture-unit": 10
        }
        config.update(
            private_adapter_config={"secret": "PRIVATE_CONFIG"},
            runtime={
                "worker_slots": 4,
                "worker_adapter": {"id": "fixture", "version": "1"},
            },
        )
        self.store = StateStore.create(self.root, config=config, clock=lambda: 10)
        self.session = self.store.coordinator()
        self.owner = self.session.__enter__()
        self.addCleanup(lambda: self.session.__exit__(None, None, None))
        self.driver = SyntheticSession(self.owner)
        self.operations = Operations(self.owner)

    def trial(self, identity, *, operation=False):
        self.owner.create_task(identity, {"instruction": "PRIVATE_REQUIREMENT"})
        iteration = self.owner.claim_task(identity)
        self.driver.accept("plan", {"timeout_seconds": 15}, iteration=iteration)
        execute = self.driver.accept(
            "execute",
            {"artifacts": [], "operations": [REQUEST] if operation else []},
            iteration=self.driver.iteration(iteration["id"]),
        )
        return self.driver.iteration(iteration["id"]), execute

    def event(self, operation_id, kind, data):
        return self.operations.record_receipt(
            operation_id,
            self.operations.get(operation_id)["revision"],
            kind,
            f"{operation_id}:{kind}",
            self.operations.envelope(operation_id, data),
        )

    def submitted(self, identity, *, unknown=False):
        iteration, execute = self.trial(identity, operation=True)
        operation_id = self.operations.list(execute_attempt_id=execute.attempt_id)[0][
            "id"
        ]
        self.event(
            operation_id,
            "prepare",
            {
                "ready": True,
                "reason": "PRIVATE_PREPARATION",
                "effective_parameters": {},
            },
        )
        self.operations.reserve_submit(
            operation_id, self.operations.get(operation_id)["revision"]
        )
        self.event(
            operation_id,
            "submission",
            {
                "outcome": "unknown" if unknown else "accepted",
                "job_id": None if unknown else "PRIVATE_JOB",
                "proof": None,
                "reason": "PRIVATE_RECEIPT",
            },
        )
        return iteration, execute, operation_id

    def reviewed(self, identity, *, operation=False, publish=False):
        if operation:
            iteration, execute, operation_id = self.submitted(identity)
            self.event(
                operation_id,
                "inspection",
                {
                    "job_id": "PRIVATE_JOB",
                    "terminal": True,
                    "outcome": "succeeded",
                    "physical_released": True,
                    "not_submitted_proof": None,
                },
            )
            self.event(
                operation_id,
                "collection",
                {"artifacts": [], "evaluations": [], "unavailable_reason": None},
            )
            self.operations.finalize_feedback(execute.attempt_id)
        else:
            iteration, execute = self.trial(identity)
        review = self.driver.accept(
            "review",
            {"knowledge": [], "reviews": [], "corrections": []},
            iteration=self.driver.iteration(iteration["id"]),
        )
        if publish:
            Publisher(self.owner).publish(review)
        return iteration, execute


class RuntimeStatusTests(StatusFixture, unittest.TestCase):
    def test_no_owner_mutation_artifact_reads_or_private_fields(self):
        self.reviewed("published", publish=True)
        self.owner.create_task("queued", {"instruction": "PRIVATE_REQUIREMENT"})
        self.owner.set_stop("PRIVATE_STOP_REASON")
        before = self.store.snapshot()
        with (
            patch.object(
                StateStore, "coordinator", side_effect=AssertionError("Owner acquired")
            ),
            patch.object(
                Path, "read_text", side_effect=AssertionError("Artifact body read")
            ),
            patch.object(
                Operations, "list", side_effect=AssertionError("Owner API called")
            ),
        ):
            result = status_snapshot(self.root)
        self.assertEqual(self.store.snapshot(), before)
        serialized = json.dumps(result)
        for private in (
            "PRIVATE_",
            "worker_token",
            "specification",
            "config",
            "output_ref",
            "context_ref",
            "submission_token",
            "failure_reason",
        ):
            self.assertNotIn(private, serialized)
        self.assertEqual(result["stop"], {"requested": True})
        self.assertEqual(result["counters"], before["counters"])
        self.assertTrue(result["owner_open"])
        self.assertEqual(result["publication_version"], 1)
        result["tasks"][0]["priority"] = -999
        self.assertNotEqual(status_snapshot(self.root)["tasks"][0]["priority"], -999)

    def test_unknown_operation_and_review_before_fees_remain_distinct(self):
        self.reviewed("reviewed", operation=True)
        self.reviewed("published", publish=True)
        self.submitted("unresolved", unknown=True)
        self.owner.create_task("queued", {})
        self.owner.create_task("blocked", {}, dependencies=["unresolved"])
        result = status_snapshot(self.root)
        tasks = {row["id"]: row for row in result["tasks"]}
        self.assertEqual(tasks["reviewed"]["status"], "reviewed")
        self.assertEqual(tasks["reviewed"]["execution"], "terminal")
        self.assertTrue(tasks["reviewed"]["physical_released"])
        self.assertTrue(tasks["reviewed"]["feedback_sealed"])
        self.assertFalse(tasks["reviewed"]["fees_settled"])
        self.assertEqual(tasks["published"]["status"], "published")
        self.assertEqual(tasks["unresolved"]["status"], "unresolved")
        self.assertFalse(tasks["unresolved"]["physical_released"])
        self.assertTrue(tasks["queued"]["ready"])
        self.assertTrue(tasks["blocked"]["dependency_blocked"])
        self.assertFalse(tasks["blocked"]["ready"])
        self.assertEqual(len(result["iterations"]), 3)
        self.assertNotIn("PRIVATE_", json.dumps(result))
        operation = next(
            row for row in result["operations"] if row["state"] == "unknown"
        )
        self.assertTrue(operation["held_resources"])
        self.assertFalse(operation["physical_released"])
        self.assertFalse(operation["costs_final"])

    def test_recorded_callback_running_and_unknown_do_not_infer_retirement(self):
        self.owner.create_task("callback", {})
        iteration = self.owner.claim_task("callback")
        attempt = self.owner.begin_attempt(
            "plan",
            request_id="callback-plan",
            iteration_id=iteration["id"],
            expected_revision=iteration["revision"],
        )
        call = CallIdentity.from_attempt(self.store.snapshot()["study_id"], attempt)
        package = RuntimeContexts(self.owner).build(call)
        reference = self.driver.updater.bind_context(call, package)
        invocation = publish_artifact(
            self.root,
            {
                "call": call.to_dict(),
                "context_ref": reference,
                "package": package.to_dict(),
            },
        )
        ledger = DispatchLedger(self.owner)
        run = ledger.register_call(
            call, invocation, {"id": "fixture", "version": "1"}, 100
        )["run"]
        for observed in ("running", "unknown"):
            ledger.observe(
                run["token"],
                dict(
                    status=observed,
                    completion_ref=None,
                    proof_ref=None,
                    worker_identity=None,
                ),
            )
            result = status_snapshot(self.root)
            self.assertEqual(
                result["tasks"][0]["status"],
                observed if observed == "running" else "unresolved",
            )
            self.assertFalse(result["attempts"][0]["dispatch"]["worker_retired"])
            self.assertFalse(result["attempts"][0]["dispatch"]["settled"])

    def test_failed_execute_without_operations_is_execution_failure(self):
        self.owner.create_task("failed-execute", {})
        iteration = self.owner.claim_task("failed-execute")
        self.driver.accept("plan", {"timeout_seconds": 15}, iteration=iteration)
        iteration = self.driver.iteration(iteration["id"])
        attempt = self.owner.begin_attempt(
            "execute",
            request_id="failed-execute-call",
            iteration_id=iteration["id"],
            expected_revision=iteration["revision"],
        )
        self.owner.fail_attempt(
            attempt["id"],
            attempt["worker_token"],
            "Synthetic Execute failure",
            expected_revision=iteration["revision"],
        )
        result = status_snapshot(self.root)
        self.assertEqual(result["tasks"][0]["status"], "failed")
        self.assertEqual(result["tasks"][0]["execution"], "failed")
        self.assertIsNone(result["tasks"][0]["physical_released"])
        self.assertIsNone(result["tasks"][0]["fees_settled"])
        self.assertEqual(result["operations"], [])

    def test_one_read_transaction_keeps_pool_and_tasks_coherent(self):
        self.owner.create_task("initial", {})
        before = self.store.snapshot()
        original = StateStore._connection
        statements = []
        added = False

        def trace(statement):
            nonlocal added
            statements.append(statement)
            if "SELECT id,revision,source" in statement and not added:
                added = True
                self.owner.create_task("arrived-during-read", {})

        @contextmanager
        def connection(store, *args, **kwargs):
            with original(store, *args, **kwargs) as conn:
                if kwargs.get("readonly"):
                    conn.set_trace_callback(trace)
                yield conn

        with patch.object(StateStore, "_connection", connection):
            result = status_snapshot(self.root)
        self.assertEqual(sum(statement == "BEGIN" for statement in statements), 1)
        self.assertTrue(added)
        self.assertEqual(result["pool_version"], before["pool_version"])
        self.assertEqual([row["id"] for row in result["tasks"]], ["initial"])
        self.assertIn(
            "arrived-during-read", [row["id"] for row in self.store.snapshot()["tasks"]]
        )


if __name__ == "__main__":
    unittest.main()
