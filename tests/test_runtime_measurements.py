"""Trusted receipt registration on the real context-bound SQLite execution path."""

from contextlib import contextmanager
from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research_loop.runtime.context import ContextSnapshot, build_context
from research_loop.runtime.files import publish_artifact
from research_loop.runtime.measurements import MeasurementRegistry
from research_loop.runtime.store import ConflictError, InvalidTransition, StateStore
from research_loop.runtime.updates import CallIdentity, Updater


def evaluation(name="global"):
    return dict(
        id=name,
        comparison_scope=f"scope/{name}",
        protocol="fixed protocol",
        data_role="development",
        allow_feedback=True,
        objectives=[dict(name="score", direction="maximize", unit="fraction")],
        constraints=[],
    )


def permissive_identity_validator(output, context):
    """Tests exercise controller binding; scientific role validation is explicit."""
    return deepcopy(output)


class RuntimeMeasurementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = StateStore.create(
            self.root,
            config=dict(goal="Synthetic receipt test", global_evaluation=evaluation()),
            clock=lambda: 10,
        )
        self.session = self.store.coordinator()
        self.owner = self.session.__enter__()
        self.addCleanup(lambda: self.session.__exit__(None, None, None))
        self.updater = Updater(
            self.owner,
            validators={
                role: permissive_identity_validator
                for role in ("plan", "execute", "review")
            },
        )
        self.registry = MeasurementRegistry(self.owner)
        self.serial = 0

    def call(self, iteration, role):
        current = next(
            row
            for row in self.store.snapshot()["iterations"]
            if row["id"] == iteration["id"]
        )
        attempt = self.owner.begin_attempt(
            role,
            request_id=f"{iteration['id']}-{role}",
            iteration_id=iteration["id"],
            expected_revision=current["revision"],
        )
        return CallIdentity.from_attempt(self.store.snapshot()["study_id"], attempt)

    def accept(self, iteration, role, output, *, bound=True):
        call = self.call(iteration, role)
        if not bound:
            self.owner.accept_attempt(
                call.attempt_id,
                call.worker_token,
                publish_artifact(self.root, output),
                expected_revision=call.expected_revision,
            )
            return call
        record = self.owner._feedback_snapshot(call.to_dict())
        config = self.store.snapshot()["config"]
        inputs = dict(
            goal=config["goal"],
            global_evaluation=config["global_evaluation"],
            permissions={},
            task=record["task"],
            budget=self.store.snapshot()["limits"],
        )
        if role in ("execute", "review"):
            inputs["plan"] = json.loads(
                (self.root / record["plan"]["output_ref"]).read_text()
            )["normalized_output"]
        captured = ContextSnapshot.capture(
            study_id=call.study_id,
            revisions=dict(
                study=1,
                task=record["task"]["revision"],
                iteration=call.expected_revision,
                knowledge=0,
            ),
            inputs=inputs,
            sources=[
                dict(
                    id=key,
                    key=key,
                    ref=f"control:{key}",
                    revision=1,
                    reason="Synthetic controller binding",
                    allow_feedback=True,
                )
                for key in inputs
            ],
            protected_keys=list(inputs),
            allowed_evidence=[],
        )
        package = build_context(captured, role, record["task"], "test/policy", {})
        self.updater.bind_context(call, package)
        proposal = self.updater.validate_output(call, output, [])
        self.updater.apply_update(proposal, proposal.revisions)
        return call

    def executed(self, *, local=None, route="route-a", bound=True):
        self.serial += 1
        specification = {} if route is None else dict(route_id=route)
        self.owner.create_task(f"task-{self.serial}", specification)
        iteration = self.owner.claim_task(f"task-{self.serial}")
        plan = {} if local is None else dict(local_evaluation=local)
        self.accept(iteration, "plan", plan, bound=bound)
        artifact = dict(path=f"models/{self.serial}", version="candidate-v1")
        execute = self.accept(
            iteration,
            "execute",
            dict(artifacts=[artifact], metrics=dict(score=999)),
            bound=bound,
        )
        return iteration, execute, artifact

    def receipt(self, iteration, execute, declared_artifact, *, local=None, **changes):
        definition = local or evaluation()
        value = dict(
            version=1,
            iteration_id=iteration["id"],
            execute_attempt_id=execute.attempt_id,
            candidate_id="candidate",
            artifact=declared_artifact,
            evaluation_id=definition["id"],
            comparison_scope=definition["comparison_scope"],
            evaluator=dict(id="trusted-evaluator", version="v1"),
            metrics=dict(score=0.5),
        )
        value.update(changes)
        return value

    def publish(self, value):
        # Nonfinite JSON values model malformed external evaluator receipts. All
        # other cases use the production immutable artifact publisher unchanged.
        try:
            return publish_artifact(self.root, value)
        except ValueError:
            reference = publish_artifact(self.root, {})
            path = self.root / reference
            path.chmod(0o644)
            path.write_text(json.dumps(value))
            path.chmod(0o444)
            return reference

    def registered(self):
        with self.store._connection(readonly=True) as conn:
            return conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]

    def test_backend_values_and_incomplete_observations_ignore_execute_claims(self):
        iteration, execute, artifact = self.executed()
        receipt = self.receipt(iteration, execute, artifact)
        reference = self.publish(receipt)
        row = self.registry.register(iteration["id"], reference)
        self.assertEqual(row["metrics"], {"score": 0.5})
        self.assertEqual(row["artifact"], artifact)
        self.assertEqual(row["evaluator"], receipt["evaluator"])
        self.assertEqual((row["level"], row["route_id"]), ("global", "route-a"))
        row["metrics"]["score"] = -1
        self.assertEqual(
            self.registry.register(iteration["id"], reference)["metrics"],
            {"score": 0.5},
        )
        incomplete = self.receipt(
            iteration,
            execute,
            artifact,
            candidate_id="incomplete",
            metrics={"diagnostic": 2},
        )
        self.assertEqual(
            self.registry.register(iteration["id"], self.publish(incomplete))[
                "metrics"
            ],
            {"diagnostic": 2},
        )
        self.assertEqual(self.registered(), 2)

    def test_rejects_nonfinite_boolean_and_nonnumeric_metrics(self):
        iteration, execute, artifact = self.executed()
        for value in (True, False, math.nan, math.inf, -math.inf, "0.5", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.registry.register(
                    iteration["id"],
                    self.publish(
                        self.receipt(
                            iteration, execute, artifact, metrics={"score": value}
                        )
                    ),
                )
        self.assertEqual(self.registered(), 0)

    def test_receipt_binding_and_exact_shape_are_required(self):
        iteration, execute, artifact = self.executed()
        for changes in (
            dict(iteration_id="other"),
            dict(execute_attempt_id="other"),
            dict(artifact=dict(artifact, version="different")),
            dict(evaluation_id="unknown"),
            dict(comparison_scope="different"),
        ):
            with self.subTest(changes=changes), self.assertRaises(ConflictError):
                self.registry.register(
                    iteration["id"],
                    self.publish(self.receipt(iteration, execute, artifact, **changes)),
                )
        for changes in (
            dict(version=True),
            dict(evaluator={"id": "missing-version"}),
            dict(metrics=[]),
            dict(candidate_id=" "),
            dict(extra="unknown"),
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.registry.register(
                    iteration["id"],
                    self.publish(self.receipt(iteration, execute, artifact, **changes)),
                )
        self.assertEqual(self.registered(), 0)

    def test_unbound_legacy_acceptance_does_not_qualify(self):
        iteration, execute, artifact = self.executed(bound=False)
        with self.assertRaises(InvalidTransition):
            self.registry.register(
                iteration["id"],
                self.publish(self.receipt(iteration, execute, artifact)),
            )
        self.assertEqual(self.registered(), 0)

    def test_registration_freezes_at_review_but_exact_replay_survives_restart(self):
        iteration, execute, artifact = self.executed()
        reference = self.publish(self.receipt(iteration, execute, artifact))
        original = self.registry.register(iteration["id"], reference)
        self.call(iteration, "review")
        with self.assertRaises(InvalidTransition):
            self.registry.register(
                iteration["id"],
                self.publish(
                    self.receipt(iteration, execute, artifact, candidate_id="late")
                ),
            )
        self.assertEqual(self.registry.register(iteration["id"], reference), original)
        self.session.__exit__(None, None, None)
        self.session = StateStore(self.root, clock=lambda: 10).coordinator()
        self.owner = self.session.__enter__()
        self.registry = MeasurementRegistry(self.owner)
        self.assertEqual(self.registry.register(iteration["id"], reference), original)
        with self.assertRaises(ConflictError):
            self.registry.register("other-iteration", reference)
        self.assertEqual(self.registered(), 1)

    def test_unique_candidate_evaluation_and_receipt_identity(self):
        iteration, execute, artifact = self.executed()
        receipt = self.receipt(iteration, execute, artifact)
        reference = self.publish(receipt)
        self.registry.register(iteration["id"], reference)
        with self.assertRaises(ConflictError):
            self.registry.register(iteration["id"], self.publish(receipt))
        path = self.root / reference
        path.chmod(0o644)
        path.write_text(json.dumps(dict(receipt, metrics={"score": 0.9})))
        path.chmod(0o444)
        with self.assertRaises(ConflictError):
            self.registry.register(iteration["id"], reference)
        self.assertEqual(self.registered(), 1)

    def test_local_route_protocol_is_frozen_with_separate_routes(self):
        local = evaluation("local")
        iteration, execute, artifact = self.executed(local=local)
        row = self.registry.register(
            iteration["id"],
            self.publish(self.receipt(iteration, execute, artifact, local=local)),
        )
        self.assertEqual(
            (row["level"], row["route_id"], row["definition"]),
            ("local", "route-a", local),
        )
        changed = dict(local, protocol="changed protocol")
        iteration2, execute2, artifact2 = self.executed(local=changed)
        with self.assertRaises(ConflictError):
            self.registry.register(
                iteration2["id"],
                self.publish(
                    self.receipt(iteration2, execute2, artifact2, local=changed)
                ),
            )
        iteration3, execute3, artifact3 = self.executed(local=changed, route="route-b")
        other = self.registry.register(
            iteration3["id"],
            self.publish(self.receipt(iteration3, execute3, artifact3, local=changed)),
        )
        self.assertEqual((other["route_id"], other["definition"]), ("route-b", changed))

    def test_local_requires_route_and_cannot_alias_global_or_publish_final_evidence(
        self,
    ):
        cases = [
            (evaluation(), "route-a", ConflictError),
            (evaluation("local"), None, ValueError),
            (
                dict(evaluation("local"), data_role="final", allow_feedback=False),
                "route-a",
                ValueError,
            ),
            (dict(evaluation("local"), allow_feedback=False), "route-a", ValueError),
        ]
        for local, route, error in cases:
            iteration, execute, artifact = self.executed(local=local, route=route)
            with self.subTest(local=local, route=route), self.assertRaises(error):
                self.registry.register(
                    iteration["id"],
                    self.publish(
                        self.receipt(iteration, execute, artifact, local=local)
                    ),
                )
        self.assertEqual(self.registered(), 0)

    def test_stop_does_not_discard_existing_evaluator_work(self):
        iteration, execute, artifact = self.executed()
        reference = self.publish(self.receipt(iteration, execute, artifact))
        self.owner.set_stop("Operator stop")
        self.assertEqual(
            self.registry.register(iteration["id"], reference)["receipt_ref"], reference
        )
        self.assertEqual(self.registered(), 1)

    def test_receipt_and_control_bodies_are_read_outside_write_transaction(self):
        iteration, execute, artifact = self.executed()
        reference = self.publish(self.receipt(iteration, execute, artifact))
        original_transaction = self.owner._transaction
        original_read = Path.read_text
        inside = False
        reads = []

        @contextmanager
        def transaction(*args, **kwargs):
            nonlocal inside
            with original_transaction(*args, **kwargs) as value:
                inside = True
                try:
                    yield value
                finally:
                    inside = False

        def read(path, *args, **kwargs):
            self.assertFalse(
                inside, "Artifact body read under SQLite write transaction"
            )
            reads.append(path)
            return original_read(path, *args, **kwargs)

        with (
            patch.object(self.owner, "_transaction", transaction),
            patch.object(Path, "read_text", read),
        ):
            self.registry.register(iteration["id"], reference)
        self.assertEqual(len(reads), 3)
        self.assertIn((self.root / reference).resolve(), reads)

    def test_receipt_identity_change_during_prepare_is_rechecked_atomically(self):
        iteration, execute, artifact = self.executed()
        receipt = self.receipt(iteration, execute, artifact)
        reference = self.publish(receipt)
        original = self.registry._prepare

        def prepare(*args):
            record = original(*args)
            path = self.root / reference
            path.chmod(0o644)
            path.write_text(json.dumps(dict(receipt, metrics={"score": 0.123456})))
            path.chmod(0o444)
            return record

        with (
            patch.object(self.registry, "_prepare", prepare),
            self.assertRaises(ConflictError),
        ):
            self.registry.register(iteration["id"], reference)
        self.assertEqual(self.registered(), 0)

    def test_review_reservation_during_prepare_is_rechecked_atomically(self):
        iteration, execute, artifact = self.executed()
        reference = self.publish(self.receipt(iteration, execute, artifact))
        original = self.registry._prepare

        def prepare(*args):
            record = original(*args)
            self.call(iteration, "review")
            return record

        with (
            patch.object(self.registry, "_prepare", prepare),
            self.assertRaises(InvalidTransition),
        ):
            self.registry.register(iteration["id"], reference)
        self.assertEqual(self.registered(), 0)


if __name__ == "__main__":
    unittest.main()
