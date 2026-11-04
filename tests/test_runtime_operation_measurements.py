"""Operation-collected evaluator intake on real SQLite and immutable receipts."""

from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from examples.runtime_publication import EVALUATION, SyntheticSession
from research_loop.runtime.files import publish_artifact
from research_loop.runtime.measurements import MeasurementRegistry
from research_loop.runtime.operations import Operations
from research_loop.runtime.store import ConflictError, InvalidTransition, StateStore


def request(identity):
    return dict(
        id=identity,
        backend="synthetic",
        authorization_id="fixture",
        resources={"cpu": 1},
        cost_bounds={"ticks": 10},
        timeout_seconds=60,
        input_artifacts=[],
        parameters={},
    )


class RuntimeOperationMeasurementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = StateStore.create(
            self.root,
            clock=lambda: 10,
            config=dict(
                goal="Synthetic operation intake",
                permissions={},
                global_evaluation=EVALUATION,
                execution_backends={
                    "synthetic": {
                        "version": "v1",
                        "environment": {"id": "local", "version": "v1"},
                    }
                },
                resource_pools={"cpu": 2},
                operation_authorizations={
                    "fixture": {
                        "backends": ["synthetic"],
                        "max_operations": 20,
                        "resource_limits": {"cpu": 2},
                        "cost_limits": {"ticks": 1000},
                        "deadline": 1000,
                    }
                },
            ),
        )
        self.session = self.store.coordinator()
        self.owner = self.session.__enter__()
        self.addCleanup(lambda: self.session.__exit__(None, None, None))
        self.driver = SyntheticSession(self.owner)
        self.operations = Operations(self.owner)
        self.registry = MeasurementRegistry(self.owner)
        self.serial = 0

    def rows(self, table):
        with self.store._connection(readonly=True) as conn:
            return [
                dict(row)
                for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]

    def executed(self, count=1, *, direct=False):
        self.serial += 1
        task_id = f"task-{self.serial}"
        self.owner.create_task(
            task_id,
            {"instruction": "Synthetic evaluation", "route_id": "local-fixture"},
        )
        iteration = self.owner.claim_task(task_id)
        self.driver.accept(
            "plan",
            {"local_evaluation": None, "timeout_seconds": 60},
            iteration=iteration,
        )
        iteration = self.driver.iteration(iteration["id"])
        artifact = {"path": f"outputs/{task_id}", "version": "v1"}
        output = {
            "artifacts": [artifact] if direct else [],
            "operations": []
            if direct
            else [request(f"operation-{i}") for i in range(count)],
        }
        execute = self.driver.accept("execute", output, iteration=iteration)
        operations = [
            row["id"]
            for row in self.rows("operations")
            if row["execute_attempt_id"] == execute.attempt_id
        ]
        return self.driver.iteration(iteration["id"]), execute, artifact, operations

    def event(self, operation, kind, data, key=None):
        row = self.operations.get(operation)
        return self.operations.record_receipt(
            operation,
            expected_revision=row["revision"],
            kind=kind,
            event_key=key or f"{operation}:{kind}",
            payload=self.operations.envelope(operation, data),
        )

    def terminal(self, operation):
        self.event(
            operation,
            "prepare",
            dict(ready=True, reason="Synthetic preparation", effective_parameters={}),
        )
        self.operations.reserve_submit(
            operation, expected_revision=self.operations.get(operation)["revision"]
        )
        self.event(
            operation,
            "submission",
            dict(
                outcome="accepted",
                job_id=f"local-{operation}",
                proof=None,
                reason="Synthetic submission",
            ),
        )
        self.event(
            operation,
            "inspection",
            dict(
                job_id=f"local-{operation}",
                terminal=True,
                outcome="succeeded",
                physical_released=True,
                not_submitted_proof=None,
            ),
        )
        self.event(operation, "settlement", dict(final_costs={"ticks": 1}))

    def receipt(
        self, iteration, execute, artifact, operation=None, *, event_key=None, **changes
    ):
        value = dict(
            version=1 if operation is None else 2,
            iteration_id=iteration["id"],
            execute_attempt_id=execute.attempt_id,
            candidate_id="candidate",
            artifact=artifact,
            evaluation_id=EVALUATION["id"],
            comparison_scope=EVALUATION["comparison_scope"],
            evaluator={"id": "trusted-fixture", "version": "v1"},
            metrics={"score": 0.5},
        )
        if operation is not None:
            value.update(
                operation_id=operation,
                collection_event_key=event_key or f"{operation}:collection",
            )
        return value | changes

    def collect(
        self,
        operation,
        artifact,
        reference=None,
        *,
        allow_feedback=True,
        unavailable_reason=None,
    ):
        self.terminal(operation)
        self.event(
            operation,
            "collection",
            dict(
                artifacts=[artifact],
                evaluations=[]
                if reference is None
                else [{"receipt_ref": reference, "allow_feedback": allow_feedback}],
                unavailable_reason=unavailable_reason,
            ),
        )

    def registered_fixture(self):
        iteration, execute, artifact, operation_ids = self.executed()
        operation = operation_ids[0]
        reference = publish_artifact(
            self.root, self.receipt(iteration, execute, artifact, operation)
        )
        self.collect(operation, artifact, reference)
        return iteration, execute, artifact, operation, reference

    def test_v2_intake_registration_seal_review_and_restart_replay(self):
        iteration, execute, artifact, operation, reference = self.registered_fixture()
        with self.assertRaises(InvalidTransition):
            self.owner.begin_attempt(
                "review",
                request_id="too-early",
                iteration_id=iteration["id"],
                expected_revision=iteration["revision"],
            )
        with self.assertRaises(InvalidTransition):
            self.operations.finalize_feedback(execute.attempt_id)
        before = self.rows("attempts")
        registered = self.registry.register(iteration["id"], reference)
        self.assertEqual(registered["artifact"], artifact)
        self.assertEqual(registered["metrics"], {"score": 0.5})
        self.assertEqual(self.rows("attempts"), before)
        intake = self.rows("evaluation_intake")[0]
        self.assertEqual(
            (intake["disposition"], intake["measurement_id"]),
            ("registered", registered["id"]),
        )
        self.operations.finalize_feedback(execute.attempt_id)
        self.owner.begin_attempt(
            "review",
            request_id="review",
            iteration_id=iteration["id"],
            expected_revision=iteration["revision"],
        )
        self.assertEqual(self.registry.register(iteration["id"], reference), registered)
        self.session.__exit__(None, None, None)
        self.session = self.store.coordinator()
        self.owner = self.session.__enter__()
        self.registry = MeasurementRegistry(self.owner)
        self.assertEqual(self.registry.register(iteration["id"], reference), registered)

    def test_denied_intake_is_rejected_without_opening_evaluator_body(self):
        iteration, execute, artifact, ids = self.executed()
        reference = publish_artifact(self.root, {"private": "Denied evaluator content"})
        original = Path.read_text

        def read(path, *args, **kwargs):
            if path.resolve() == (self.root / reference).resolve():
                self.fail("Denied evaluator body was opened")
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", read):
            self.collect(ids[0], artifact, reference, allow_feedback=False)
            with self.assertRaises(InvalidTransition):
                self.registry.register(iteration["id"], reference)
        self.assertEqual(self.rows("evaluation_intake")[0]["disposition"], "rejected")
        self.assertEqual(self.rows("measurements"), [])

    def test_v2_must_match_its_operation_collection_and_artifact(self):
        for mismatch in ("operation", "event", "artifact", "v1"):
            iteration, execute, artifact, ids = self.executed(count=2)
            receipt = self.receipt(iteration, execute, artifact, ids[0])
            if mismatch == "operation":
                receipt["operation_id"] = ids[1]
            elif mismatch == "event":
                receipt["collection_event_key"] = "unrelated-collection"
            elif mismatch == "artifact":
                receipt["artifact"] = dict(artifact, version="undeclared")
            else:
                receipt = self.receipt(iteration, execute, artifact)
            reference = publish_artifact(self.root, receipt)
            self.collect(ids[0], artifact, reference)
            with self.subTest(mismatch=mismatch), self.assertRaises(ConflictError):
                self.registry.register(iteration["id"], reference)
        self.assertEqual(self.rows("measurements"), [])
        self.assertTrue(
            all(
                row["disposition"] == "pending"
                for row in self.rows("evaluation_intake")
            )
        )

    def test_partial_collection_registers_only_actually_collected_artifacts(self):
        for available in (True, False):
            iteration, execute, artifact, ids = self.executed()
            measured_artifact = (
                artifact if available else dict(artifact, path="outputs/missing")
            )
            reference = publish_artifact(
                self.root, self.receipt(iteration, execute, measured_artifact, ids[0])
            )
            self.collect(
                ids[0],
                artifact,
                reference,
                unavailable_reason="Secondary output was unavailable",
            )
            self.assertEqual(
                self.operations.get(ids[0])["collection_state"], "unavailable"
            )
            with self.subTest(available=available):
                if available:
                    row = self.registry.register(iteration["id"], reference)
                    self.assertEqual(row["artifact"], artifact)
                    self.assertEqual(row["metrics"], {"score": 0.5})
                else:
                    with self.assertRaises(ConflictError):
                        self.registry.register(iteration["id"], reference)
        self.assertEqual(len(self.rows("measurements")), 1)

    def test_v2_without_collection_intake_is_rejected(self):
        iteration, execute, artifact, ids = self.executed()
        self.collect(ids[0], artifact)
        reference = publish_artifact(
            self.root, self.receipt(iteration, execute, artifact, ids[0])
        )
        with self.assertRaises(ConflictError):
            self.registry.register(iteration["id"], reference)
        self.assertEqual(self.rows("measurements"), [])

    def test_receipt_identity_substitution_rejected_before_body_read(self):
        iteration, execute, artifact, operation, reference = self.registered_fixture()
        path = self.root / reference
        path.chmod(0o644)
        path.write_text('{"substituted": true}')
        path.chmod(0o444)
        with patch.object(
            self.registry.reader,
            "_read_record",
            side_effect=AssertionError("substituted body read"),
        ):
            with self.assertRaises(ConflictError):
                self.registry.register(iteration["id"], reference)
        self.assertEqual(self.rows("measurements"), [])

    def test_replay_rechecks_immutable_collection_metadata(self):
        iteration, execute, artifact, operation, reference = self.registered_fixture()
        self.registry.register(iteration["id"], reference)
        path = self.root / self.operations.get(operation)["collection_ref"]
        path.chmod(0o644)
        path.write_text('{"substituted": true}')
        path.chmod(0o444)
        with self.assertRaises(ConflictError):
            self.registry.register(iteration["id"], reference)
        self.assertEqual(len(self.rows("measurements")), 1)

    def quarantine(self, operation):
        result = self.event(
            operation,
            "inspection",
            dict(
                job_id=f"contradictory-{operation}",
                terminal=False,
                outcome=None,
                physical_released=False,
                not_submitted_proof=None,
            ),
            key=f"{operation}:contradictory-job",
        )
        self.assertIsNotNone(result["inconsistency"])

    def test_quarantined_collection_blocks_new_measurement_without_body_read(self):
        iteration, execute, artifact, operation, reference = self.registered_fixture()
        self.quarantine(operation)
        with patch.object(
            self.registry.reader,
            "_read_record",
            side_effect=AssertionError("quarantined evaluator body read"),
        ):
            with self.assertRaises(InvalidTransition):
                self.registry.register(iteration["id"], reference)
        self.assertEqual(self.rows("measurements"), [])
        self.assertEqual(self.rows("evaluation_intake")[0]["disposition"], "pending")

    def test_later_quarantine_preserves_exact_registered_replay(self):
        iteration, execute, artifact, operation, reference = self.registered_fixture()
        original = self.registry.register(iteration["id"], reference)
        self.quarantine(operation)
        self.assertEqual(self.registry.register(iteration["id"], reference), original)
        self.assertEqual(len(self.rows("measurements")), 1)
        self.assertEqual(
            self.rows("evaluation_intake")[0]["measurement_id"], original["id"]
        )

    def test_quarantine_during_prepare_blocks_commit(self):
        iteration, execute, artifact, operation, reference = self.registered_fixture()
        original = self.registry._prepare

        def prepare(*args):
            record = original(*args)
            self.quarantine(operation)
            return record

        with (
            patch.object(self.registry, "_prepare", prepare),
            self.assertRaises(InvalidTransition),
        ):
            self.registry.register(iteration["id"], reference)
        self.assertEqual(self.rows("measurements"), [])
        self.assertEqual(self.rows("evaluation_intake")[0]["disposition"], "pending")

    def test_measurement_and_intake_disposition_commit_atomically(self):
        iteration, execute, artifact, operation, reference = self.registered_fixture()
        original = self.owner._transaction

        @contextmanager
        def fail_commit():
            with original() as transaction:
                yield transaction
                raise RuntimeError("Synthetic failure before commit")

        with (
            patch.object(self.owner, "_transaction", fail_commit),
            self.assertRaises(RuntimeError),
        ):
            self.registry.register(iteration["id"], reference)
        self.assertEqual(self.rows("measurements"), [])
        self.assertEqual(self.rows("evaluation_intake")[0]["disposition"], "pending")
        row = self.registry.register(iteration["id"], reference)
        self.assertEqual(self.rows("evaluation_intake")[0]["measurement_id"], row["id"])

    def test_rejection_seal_and_review_during_prepare_win_atomically(self):
        iteration, execute, artifact, operation, reference = self.registered_fixture()
        original = self.registry._prepare

        def prepare(*args):
            record = original(*args)
            self.operations.reject_evaluation(
                reference, "Controller rejected this evaluator receipt"
            )
            self.operations.finalize_feedback(execute.attempt_id)
            self.owner.begin_attempt(
                "review",
                request_id="review-won",
                iteration_id=iteration["id"],
                expected_revision=iteration["revision"],
            )
            return record

        with (
            patch.object(self.registry, "_prepare", prepare),
            self.assertRaises(InvalidTransition),
        ):
            self.registry.register(iteration["id"], reference)
        self.assertEqual(self.rows("measurements"), [])
        self.assertEqual(self.rows("evaluation_intake")[0]["disposition"], "rejected")
        self.assertEqual(self.rows("operation_groups")[0]["feedback_sealed"], 1)
        self.assertEqual(self.rows("attempts")[-1]["role"], "review")

    def test_seal_blocks_new_registration_before_receipt_body_read(self):
        iteration, execute, artifact, operation, reference = self.registered_fixture()
        self.registry.register(iteration["id"], reference)
        self.operations.finalize_feedback(execute.attempt_id)
        other = publish_artifact(self.root, self.receipt(iteration, execute, artifact))
        with patch.object(
            self.registry.reader,
            "_read_record",
            side_effect=AssertionError("sealed body read"),
        ):
            with self.assertRaises(InvalidTransition):
                self.registry.register(iteration["id"], other)
        self.assertEqual(len(self.rows("measurements")), 1)

    def test_evaluator_and_accepted_output_reads_stay_outside_write_transaction(self):
        iteration, execute, artifact, operation, reference = self.registered_fixture()
        original_transaction = self.owner._transaction
        original_read = Path.read_text
        inside = False
        reads = []

        @contextmanager
        def transaction():
            nonlocal inside
            with original_transaction() as value:
                inside = True
                try:
                    yield value
                finally:
                    inside = False

        def read(path, *args, **kwargs):
            self.assertFalse(
                inside, "Control body read inside measurement write transaction"
            )
            reads.append(path)
            return original_read(path, *args, **kwargs)

        with (
            patch.object(self.owner, "_transaction", transaction),
            patch.object(Path, "read_text", read),
        ):
            self.registry.register(iteration["id"], reference)
        self.assertEqual(len(reads), 3)
        self.assertIn(self.root / reference, reads)

    def test_direct_v1_execute_artifact_path_is_preserved(self):
        iteration, execute, artifact, ids = self.executed(direct=True)
        self.assertEqual(ids, [])
        reference = publish_artifact(
            self.root, self.receipt(iteration, execute, artifact)
        )
        row = self.registry.register(iteration["id"], reference)
        self.assertEqual(row["artifact"], artifact)
        self.assertEqual(self.rows("evaluation_intake"), [])


if __name__ == "__main__":
    unittest.main()
