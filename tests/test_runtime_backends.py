"""Trusted dispatch and wall accounting on the actual SQLite operation path."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from examples.backend_lifecycle import CONFIG, REQUEST, accept_fixture, run_smoke
from research_loop.runtime.backends import BackendDriver
from research_loop.runtime.operations import Operations
from research_loop.runtime.store import ConflictError, InvalidTransition, StateStore


class FixtureBackend:
    version = "1"
    environment = {"id": "fixture-python", "version": "1"}

    def __init__(self, owner):
        self.owner = owner
        self.calls = []
        self.finished = False
        self.fail_submission = False

    def event(self, operation, kind, data):
        # A second writable connection must be usable during every callback.
        # This would fail if dispatch were inside the coordinator transaction.
        with self.owner.store._connection() as conn:
            conn.execute("PRAGMA busy_timeout=0")
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
        self.calls.append(kind)
        return {"event_key": f"{operation['id']}:{kind}", "data": data}

    def prepare(self, request, operation):
        return self.event(
            operation,
            "prepare",
            {
                "ready": True,
                "reason": "Fixture",
                "effective_parameters": request["parameters"],
            },
        )

    def submit(self, request, permit):
        operation = Operations(self.owner).get(permit["operation_id"])
        if operation["submission_token"] != permit["submission_token"]:
            raise AssertionError("Submission intent was not committed before dispatch")
        result = self.event(
            operation,
            "submission",
            {
                "outcome": "accepted",
                "job_id": "fixture-job",
                "proof": None,
                "reason": "Fixture accepted",
            },
        )
        if self.fail_submission:
            raise ConnectionError("Fixture response lost")
        return result

    def inspect(self, request, operation):
        return self.event(
            operation,
            "finished" if self.finished else "running",
            {
                "job_id": "fixture-job",
                "terminal": self.finished,
                "outcome": "succeeded" if self.finished else None,
                "physical_released": self.finished,
                "not_submitted_proof": None,
            },
        )


class RuntimeBackendTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = 10
        self.store = StateStore.create(
            self.root, config=deepcopy(CONFIG), clock=lambda: self.now
        )
        self.session = self.store.coordinator()
        self.owner = self.session.__enter__()
        self.addCleanup(lambda: self.session.__exit__(None, None, None))
        self.owner.create_task("fixture", {})
        self.iteration = self.owner.claim_next()
        accept_fixture(
            self.owner, self.iteration["id"], "plan", {"timeout_seconds": 15}
        )
        execute = accept_fixture(
            self.owner, self.iteration["id"], "execute", {"operations": [REQUEST]}
        )
        self.operation_id = f"{execute.attempt_id}:arithmetic"
        self.backend = FixtureBackend(self.owner)
        self.driver = BackendDriver(self.owner, {"local-fixture": self.backend})
        self.operations = Operations(self.owner)

    def test_adapter_identity_checked_before_any_effect_or_reservation(self):
        self.backend.environment = {"id": "different", "version": "1"}
        with self.assertRaises(ConflictError):
            self.driver.prepare(self.operation_id)
        self.assertEqual(self.backend.calls, [])
        self.assertIsNone(self.operations.get(self.operation_id)["submission_token"])

    def test_callbacks_outside_writer_and_lost_response_never_retries(self):
        self.driver.prepare(self.operation_id)
        self.backend.fail_submission = True
        self.driver.submit(self.operation_id)
        unknown = self.operations.get(self.operation_id)
        self.assertEqual(unknown["state"], "unknown")
        self.assertEqual(unknown["held_resources"], {"local-slot": 1})
        with self.assertRaises(InvalidTransition):
            self.driver.submit(self.operation_id)
        self.driver.inspect(self.operation_id)
        revision = self.operations.get(self.operation_id)["revision"]
        self.driver.inspect(self.operation_id)
        self.assertEqual(self.operations.get(self.operation_id)["revision"], revision)
        self.assertEqual(
            self.backend.calls, ["prepare", "submission", "running", "running"]
        )
        self.assertEqual(
            self.operations.get(self.operation_id)["job_id"], "fixture-job"
        )

    def test_invalid_lifecycle_does_not_dispatch_callbacks(self):
        for method in (
            self.driver.inspect,
            self.driver.cancel,
            self.driver.collect,
            self.driver.settle,
        ):
            with (
                self.subTest(method=method.__name__),
                self.assertRaises(InvalidTransition),
            ):
                method(self.operation_id)
        self.assertEqual(self.backend.calls, [])

    def test_cleanup_can_inspect_quarantined_unreserved_job(self):
        data = {
            "job_id": "fixture-job",
            "terminal": True,
            "outcome": "succeeded",
            "physical_released": True,
            "not_submitted_proof": None,
        }
        self.operations.record_receipt(
            self.operation_id,
            1,
            "inspection",
            "unexpected-external-job",
            self.operations.envelope(self.operation_id, data),
        )
        self.backend.finished = True
        self.driver.inspect(self.operation_id)
        operation = self.operations.get(self.operation_id)
        self.assertEqual(self.backend.calls, ["finished"])
        self.assertIsNone(operation["submission_token"])
        self.assertTrue(operation["inconsistency"])
        self.assertTrue(operation["external_open"])

    def test_live_external_gap_charged_once_fee_only_gap_excluded(self):
        self.driver.prepare(self.operation_id)
        self.driver.submit(self.operation_id)
        self.now = 12
        self.session.__exit__(None, None, None)
        self.now = 20
        self.session = self.store.coordinator()
        self.owner = self.session.__enter__()
        self.assertEqual(self.store.snapshot()["counters"]["elapsed_seconds"], 10)
        self.backend.owner = self.owner
        self.backend.finished = True
        self.driver = BackendDriver(self.owner, {"local-fixture": self.backend})
        self.driver.inspect(self.operation_id)
        closed = Operations(self.owner).get(self.operation_id)
        self.assertFalse(closed["external_open"])
        self.assertEqual(closed["held_costs"], {"fixture-unit": 1})
        self.session.__exit__(None, None, None)
        self.now = 40
        self.session = self.store.coordinator()
        self.owner = self.session.__enter__()
        self.assertEqual(self.store.snapshot()["counters"]["elapsed_seconds"], 10)

    def test_real_subprocess_smoke(self):
        result = run_smoke()
        self.assertEqual(result["submissions"], 1)
        self.assertTrue(result["owner_reopened"])
        self.assertTrue(result["review_before_fee_settlement"])
        self.assertEqual(result["score"], 0.6)
        self.assertEqual(result["resource_hold"], {})
        self.assertEqual(result["cost_hold"], {})

    def test_external_gap_counts_even_without_pooled_resources(self):
        store = StateStore.create(
            self.root / "zero-resources", config=CONFIG, clock=lambda: self.now
        )
        request = deepcopy(REQUEST)
        request["resources"] = {}
        with store.coordinator() as owner:
            owner.create_task("zero", {})
            iteration = owner.claim_next()
            accept_fixture(owner, iteration["id"], "plan", {"timeout_seconds": 15})
            execute = accept_fixture(
                owner, iteration["id"], "execute", {"operations": [request]}
            )
            operation_id = f"{execute.attempt_id}:arithmetic"
            driver = BackendDriver(owner, {"local-fixture": FixtureBackend(owner)})
            driver.prepare(operation_id)
            driver.submit(operation_id)
            operation = Operations(owner).get(operation_id)
            self.assertEqual(operation["held_resources"], {})
            self.assertTrue(operation["external_open"])
        self.now = 20
        with store.coordinator():
            self.assertEqual(store.snapshot()["counters"]["elapsed_seconds"], 10)


if __name__ == "__main__":
    unittest.main()
