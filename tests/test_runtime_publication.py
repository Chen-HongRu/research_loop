"""Accepted feedback publication on SQLite, including real registry receipts."""

from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research_loop.runtime.context import ContextSnapshot, build_context
from research_loop.runtime.files import publish_artifact
from research_loop.runtime.publication import Publisher
from research_loop.runtime.store import (
    ConflictError,
    InvalidTransition,
    OwnershipError,
    StateStore,
)
from research_loop.runtime.updates import CallIdentity, Updater


def definition():
    return dict(
        id="global-v1",
        comparison_scope="fixed-v1",
        protocol="Fixed development evaluator",
        data_role="development",
        allow_feedback=True,
        objectives=[dict(name="score", direction="maximize", unit="points")],
        constraints=[],
    )


def knowledge(identity, **changes):
    return (
        dict(
            id=identity,
            expected_revision=0,
            kind="external",
            statement="Observed external claim",
            conditions=["Under the reported protocol"],
            source_refs=["https://example.com/paper"],
            measurement_ids=[],
            interpretation="",
            limitations=["Not locally reproduced"],
        )
        | changes
    )


def payload(*entries, reviews=None, corrections=None):
    return dict(
        knowledge=list(entries), reviews=reviews or [], corrections=corrections or []
    )


class RuntimePublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = StateStore.create(
            self.root,
            config=dict(
                goal="Synthetic publication",
                permissions={},
                global_evaluation=definition(),
            ),
            clock=lambda: 10,
            max_calls=100,
        )
        self.session = self.store.coordinator()
        self.owner = self.session.__enter__()
        self.addCleanup(lambda: self.session.__exit__(None, None, None))
        self.updater = Updater(
            self.owner,
            validators={
                role: lambda output, _: output
                for role in ("scout", "plan", "execute", "review", "judge")
            },
        )
        self.publisher = Publisher(self.owner)
        self.serial = 0

    def identity(self, attempt):
        return CallIdentity.from_attempt(self.store.snapshot()["study_id"], attempt)

    def begin(self, role, iteration=None):
        self.serial += 1
        kwargs = (
            {}
            if iteration is None
            else dict(
                iteration_id=iteration["id"], expected_revision=iteration["revision"]
            )
        )
        return self.identity(
            self.owner.begin_attempt(role, request_id=f"call-{self.serial}", **kwargs)
        )

    def package(self, call, refs):
        record = self.owner._feedback_snapshot(call.to_dict())
        config = self.store.snapshot()["config"]
        inputs = {
            key: config[key] for key in ("goal", "permissions", "global_evaluation")
        }
        inputs.update(task=record["task"], budget=self.store.snapshot()["limits"])
        if call.role in {"execute", "review"}:
            inputs["plan"] = json.loads(
                (self.root / record["plan"]["output_ref"]).read_text()
            )["normalized_output"]
        protected = list(inputs)
        sources = [
            dict(
                id=key,
                key=key,
                ref=f"control:{key}",
                revision=1,
                reason="Frozen authority",
                allow_feedback=True,
            )
            for key in inputs
        ]
        for index, ref in enumerate(refs):
            key = f"proof-{index}"
            inputs[key] = "Trusted immutable evidence"
            sources.append(
                dict(
                    id=key,
                    key=key,
                    ref=ref,
                    revision=1,
                    reason="Evaluator receipt",
                    allow_feedback=True,
                )
            )
        snapshot = ContextSnapshot.capture(
            study_id=call.study_id,
            revisions=dict(
                study=1,
                task=record["task"]["revision"] if record["task"] else None,
                iteration=call.expected_revision,
                knowledge=record["knowledge_version"],
            ),
            inputs=inputs,
            sources=sources,
            protected_keys=protected,
            allowed_evidence=list(refs),
        )
        return build_context(snapshot, call.role, record["task"], "policy:v1", {})

    def accept(self, call, output, refs=()):
        self.updater.bind_context(call, self.package(call, refs))
        proposal = self.updater.validate_output(call, output, list(refs))
        self.updater.apply_update(proposal, proposal.revisions)
        return call

    def scout(self, output, refs=()):
        return self.accept(self.begin("scout"), output, refs)

    def iteration(self, identity):
        return next(
            row for row in self.store.snapshot()["iterations"] if row["id"] == identity
        )

    def trial(self, task_id, *, measured=False):
        self.owner.create_task(task_id, dict(instruction=task_id, route_id="route-a"))
        iteration = self.owner.claim_task(task_id)
        plan = self.accept(self.begin("plan", iteration), {"local_evaluation": None})
        artifact = dict(path=f"candidates/{task_id}.bin", version="v1")
        execute = self.accept(
            self.begin("execute", self.iteration(iteration["id"])),
            {"artifacts": [artifact]},
        )
        measurement = None
        if measured:
            from research_loop.runtime.measurements import MeasurementRegistry

            receipt_ref = publish_artifact(
                self.root,
                dict(
                    version=1,
                    iteration_id=iteration["id"],
                    execute_attempt_id=execute.attempt_id,
                    candidate_id=f"candidate-{task_id}",
                    artifact=artifact,
                    evaluation_id="global-v1",
                    comparison_scope="fixed-v1",
                    evaluator=dict(id="fixed", version="1"),
                    metrics={"score": 9.0},
                ),
            )
            measurement = MeasurementRegistry(self.owner).register(
                iteration["id"], receipt_ref
            )
        return self.iteration(iteration["id"]), measurement, plan

    def review(self, iteration, output, refs=()):
        return self.accept(self.begin("review", iteration), output, refs)

    def measured_publication(self, task_id="measured"):
        iteration, measurement, _ = self.trial(task_id, measured=True)
        decision = dict(
            measurement_id=measurement["id"],
            valid=True,
            verified=True,
            reason="Evaluator record matches artifact",
        )
        call = self.review(
            iteration, payload(reviews=[decision]), [measurement["receipt_ref"]]
        )
        receipt = self.publisher.publish(call)
        return measurement, call, receipt

    def test_scout_review_out_of_order_append_retains_both(self):
        scout = self.scout(payload(knowledge("external")))
        iteration, _, _ = self.trial("qualitative")
        review = self.review(
            iteration, payload(knowledge("interpretation", kind="interpretation"))
        )
        review_receipt = self.publisher.publish(review)
        scout_receipt = self.publisher.publish(scout)
        self.assertEqual(
            (review_receipt["publication_id"], scout_receipt["publication_id"]), (1, 2)
        )
        snapshot = self.publisher.snapshot()
        self.assertEqual(snapshot["version"], 2)
        self.assertEqual(
            {row["id"] for row in snapshot["knowledge"]}, {"external", "interpretation"}
        )
        self.assertEqual(
            {row["source_role"] for row in snapshot["knowledge"]}, {"scout", "review"}
        )
        current = self.iteration(iteration["id"])
        self.assertEqual(
            (current["status"], current["phase"], current["revision"]),
            ("published", "done", 5),
        )
        self.assertEqual(self.store.snapshot()["counters"]["iterations"], 1)

    def test_duplicate_reopen_is_same_receipt_without_files_or_advancement(self):
        call = self.scout(payload(knowledge("one")))
        original = self.publisher.publish(call)
        before = self.publisher.snapshot()
        files = set((self.root / "artifacts").iterdir())
        self.session.__exit__(None, None, None)
        self.session = self.store.coordinator()
        self.owner = self.session.__enter__()
        self.publisher = Publisher(self.owner)
        self.assertEqual(self.publisher.publish(call), original)
        self.assertEqual(self.publisher.snapshot(), before)
        self.assertEqual(set((self.root / "artifacts").iterdir()), files)
        with self.assertRaises(OwnershipError):
            self.publisher.publish(replace(call, worker_token="forged"))

    def test_frozen_zero_knowledge_context_can_finish_after_new_publication(self):
        call = self.begin("scout")
        package = self.package(call, [])
        self.assertEqual(package.manifest["revisions"]["knowledge"], 0)
        self.updater.bind_context(call, package)
        first = self.publisher.publish(self.scout(payload(knowledge("first"))))
        self.assertEqual(first["publication_id"], 1)
        proposal = self.updater.validate_output(call, payload(knowledge("late")), [])
        self.assertEqual(proposal.revisions["knowledge"], 0)
        self.updater.apply_update(proposal, proposal.revisions)
        self.assertEqual(self.publisher.publish(call)["publication_id"], 2)
        self.assertEqual(
            {row["id"] for row in self.publisher.snapshot()["knowledge"]},
            {"first", "late"},
        )

    def test_duplicate_receipt_rechecks_accepted_source_metadata(self):
        reference = publish_artifact(self.root, {"source": "original"})
        call = self.scout(
            payload(knowledge("source", source_refs=[reference])), [reference]
        )
        self.publisher.publish(call)
        before = self.publisher.snapshot()
        path = self.root / reference
        path.chmod(0o644)
        path.write_text('{"source": "substituted after publication"}')
        path.chmod(0o444)
        with self.assertRaises(ConflictError):
            self.publisher.publish(call)
        self.assertEqual(self.publisher.snapshot(), before)

    def test_target_conflicts_are_durable_while_unaffected_entries_append(self):
        self.publisher.publish(self.scout(payload(knowledge("shared"))))
        first = self.scout(
            payload(knowledge("shared", expected_revision=1), knowledge("first"))
        )
        second = self.scout(
            payload(knowledge("shared", expected_revision=1), knowledge("second"))
        )
        self.publisher.publish(first)
        result = self.publisher.publish(second)
        self.assertEqual(result["knowledge"], [{"id": "second", "revision": 1}])
        self.assertEqual(
            (
                result["conflicts"][0]["target_id"],
                result["conflicts"][0]["actual_revision"],
            ),
            ("shared", 2),
        )
        change_kind = self.publisher.publish(
            self.scout(
                payload(
                    knowledge("shared", kind="interpretation", expected_revision=2),
                    knowledge("third"),
                )
            )
        )
        self.assertEqual(change_kind["conflicts"][0]["reason"], "kind_change")
        collision = self.publisher.publish(self.scout(payload(knowledge("shared"))))
        self.assertEqual(collision["conflicts"][0]["actual_revision"], 2)
        snapshot = self.publisher.snapshot()
        self.assertEqual(len(snapshot["conflicts"]), 3)
        self.assertEqual(
            [
                (row["revision"], row["kind"])
                for row in snapshot["knowledge"]
                if row["id"] == "shared"
            ],
            [(1, "external"), (2, "external")],
        )

    def test_guessed_unseen_predecessors_conflict_without_dropping_independent_knowledge(
        self,
    ):
        measurement, _, _ = self.measured_publication()
        self.publisher.publish(self.scout(payload(knowledge("shared"))))
        pending = []
        for revision, name in ((1, "observed-correction"), (2, "guessed-correction")):
            iteration, _, _ = self.trial(name)
            correction = dict(
                measurement_id=measurement["id"],
                expected_revision=revision,
                valid=revision == 2,
                verified=revision == 2,
                reason=name,
                evidence_refs=[measurement["receipt_ref"]],
            )
            pending.append(
                self.review(
                    iteration,
                    payload(
                        knowledge("shared", expected_revision=revision, statement=name),
                        knowledge(name),
                        corrections=[correction],
                    ),
                    [measurement["receipt_ref"]],
                )
            )
        for call in pending:
            self.assertEqual(
                self.owner._feedback_snapshot(call.to_dict())["binding"]["revisions"][
                    "knowledge"
                ],
                2,
            )
        first = self.publisher.publish(pending[0])
        self.assertEqual(first["conflicts"], [])
        result = self.publisher.publish(pending[1])
        self.assertEqual(
            result["knowledge"], [{"id": "guessed-correction", "revision": 1}]
        )
        self.assertEqual(result["eligibility"], [])
        self.assertEqual(
            {row["target_kind"] for row in result["conflicts"]},
            {"knowledge", "corrections"},
        )
        for conflict in result["conflicts"]:
            self.assertEqual(
                (conflict["expected_revision"], conflict["actual_revision"]), (2, 2)
            )
            self.assertIn("snapshot_revision_mismatch", conflict["reason"])
            self.assertIn("knowledge version 2 exposed revision 1", conflict["reason"])
        snapshot = self.publisher.snapshot()
        self.assertEqual(
            [
                (row["revision"], row["valid"], row["verified"])
                for row in snapshot["eligibility"]
            ],
            [(1, 1, 1), (2, 0, 0)],
        )
        self.assertEqual(
            [row["revision"] for row in snapshot["knowledge"] if row["id"] == "shared"],
            [1, 2],
        )
        self.assertEqual(self.publisher.publish(pending[1]), result)
        self.assertEqual(self.publisher.snapshot(), snapshot)

    def test_guessed_future_knowledge_target_conflicts_even_when_current(self):
        call = self.begin("scout")
        self.updater.bind_context(call, self.package(call, []))
        self.publisher.publish(self.scout(payload(knowledge("future-target"))))
        proposal = self.updater.validate_output(
            call,
            payload(
                knowledge("future-target", expected_revision=1),
                knowledge("independent"),
            ),
            [],
        )
        self.updater.apply_update(proposal, proposal.revisions)
        result = self.publisher.publish(call)
        self.assertEqual(result["knowledge"], [{"id": "independent", "revision": 1}])
        self.assertEqual(result["conflicts"][0]["actual_revision"], 1)
        self.assertIn(
            "knowledge version 0 exposed revision 0", result["conflicts"][0]["reason"]
        )
        self.assertEqual(
            [
                row["revision"]
                for row in self.publisher.snapshot()["knowledge"]
                if row["id"] == "future-target"
            ],
            [1],
        )

    def test_historical_measurement_published_after_binding_is_entry_conflict(self):
        iteration, measurement, _ = self.trial("late-qualification", measured=True)
        call = self.begin("scout")
        refs = [measurement["receipt_ref"]]
        self.updater.bind_context(call, self.package(call, refs))
        decision = dict(
            measurement_id=measurement["id"],
            valid=True,
            verified=True,
            reason="Qualified after Scout context was captured",
        )
        self.publisher.publish(
            self.review(iteration, payload(reviews=[decision]), refs)
        )
        entry = knowledge(
            "unseen-measurement",
            kind="interpretation",
            measurement_ids=[measurement["id"]],
        )
        proposal = self.updater.validate_output(
            call, payload(entry, knowledge("independent")), refs
        )
        self.updater.apply_update(proposal, proposal.revisions)
        result = self.publisher.publish(call)
        self.assertEqual(result["knowledge"], [{"id": "independent", "revision": 1}])
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(result["conflicts"][0]["target_id"], "unseen-measurement")
        self.assertIn(
            "snapshot_measurement_unavailable", result["conflicts"][0]["reason"]
        )
        self.assertIn("knowledge version 0", result["conflicts"][0]["reason"])
        self.assertEqual(len(self.publisher.snapshot()["eligibility"]), 1)

    def test_publication_preserves_authority_budget_and_stop(self):
        call = self.scout(payload(knowledge("permitted")))
        self.owner.set_stop("no new work")
        before = self.store.snapshot()
        self.publisher.publish(call)
        after = self.store.snapshot()
        for key in ("config", "limits", "counters", "stop", "tasks", "attempts"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(self.publisher.snapshot()["version"], 1)

    def test_registered_review_measurement_publishes_eligibility_and_local_knowledge(
        self,
    ):
        iteration, measurement, _ = self.trial("numeric", measured=True)
        entry = knowledge(
            "local",
            kind="local_measurement",
            measurement_ids=[measurement["id"]],
            source_refs=[],
        )
        decision = dict(
            measurement_id=measurement["id"],
            valid=True,
            verified=True,
            reason="Checked evaluator receipt",
        )
        call = self.review(
            iteration, payload(entry, reviews=[decision]), [measurement["receipt_ref"]]
        )
        result = self.publisher.publish(call)
        self.assertEqual(
            result["eligibility"],
            [{"measurement_id": measurement["id"], "revision": 1}],
        )
        snapshot = self.publisher.snapshot()
        self.assertEqual(snapshot["measurements"][0]["metrics"], {"score": 9.0})
        self.assertEqual(snapshot["knowledge"][0]["kind"], "local_measurement")
        self.assertEqual(snapshot["eligibility"][0]["decision_kind"], "reviews")

    def test_historical_corrections_append_eligibility_and_preserve_numeric_records(
        self,
    ):
        measurement, _, _ = self.measured_publication()
        before = self.publisher.snapshot()["measurements"]
        correction = dict(
            measurement_id=measurement["id"],
            expected_revision=1,
            valid=False,
            verified=False,
            reason="Artifact attribution disputed",
            evidence_refs=[measurement["receipt_ref"]],
        )
        pending = []
        for name in ("correction-a", "correction-b"):
            iteration, _, _ = self.trial(name)
            pending.append(
                self.review(
                    iteration,
                    payload(knowledge(name), corrections=[correction]),
                    [measurement["receipt_ref"]],
                )
            )
        self.publisher.publish(pending[0])
        result = self.publisher.publish(pending[1])
        self.assertEqual(result["knowledge"], [{"id": "correction-b", "revision": 1}])
        self.assertEqual(result["conflicts"][0]["target_kind"], "corrections")
        snapshot = self.publisher.snapshot()
        self.assertEqual(snapshot["measurements"], before)
        self.assertEqual(
            [
                (row["revision"], row["valid"], row["verified"])
                for row in snapshot["eligibility"]
            ],
            [(1, 1, 1), (2, 0, 0)],
        )

    def test_unregistered_unreviewed_or_uncited_measurements_rejected(self):
        iteration, measurement, _ = self.trial("unreviewed", measured=True)
        for entry, refs in (
            (
                knowledge(
                    "scout-local",
                    kind="local_measurement",
                    measurement_ids=[measurement["id"]],
                ),
                [measurement["receipt_ref"]],
            ),
            (
                knowledge(
                    "unreviewed",
                    kind="interpretation",
                    measurement_ids=[measurement["id"]],
                ),
                [measurement["receipt_ref"]],
            ),
            (
                knowledge(
                    "unknown", kind="interpretation", measurement_ids=["unknown"]
                ),
                [],
            ),
        ):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                self.publisher.publish(self.scout(payload(entry), refs))
        decision = dict(
            measurement_id=measurement["id"],
            valid=True,
            verified=True,
            reason="Narrative alone",
        )
        call = self.review(iteration, payload(reviews=[decision]))
        with self.assertRaises(ValueError):
            self.publisher.publish(call)
        self.assertEqual(self.publisher.snapshot()["version"], 0)
        self.assertEqual(self.iteration(iteration["id"])["phase"], "publication")

    def test_review_must_cover_all_registered_measurements_and_cannot_edit_metrics(
        self,
    ):
        iteration, _, _ = self.trial("missing-review", measured=True)
        call = self.review(iteration, payload())
        with self.assertRaises(ValueError):
            self.publisher.publish(call)
        self.assertEqual(self.publisher.snapshot()["eligibility"], [])
        self.assertEqual(self.publisher.snapshot()["version"], 0)

    def test_failed_or_wrong_role_feedback_cannot_publish(self):
        iteration, _, plan = self.trial("failed")
        call = self.begin("review", iteration)
        self.updater.bind_context(call, self.package(call, []))
        self.owner.fail_attempt(
            call.attempt_id,
            call.worker_token,
            "failed Review",
            expected_revision=call.expected_revision,
        )
        for identity in (call, plan):
            with self.assertRaises(InvalidTransition):
                self.publisher.publish(identity)
        self.assertEqual(self.publisher.snapshot()["version"], 0)

    def test_external_sources_and_explicit_uncertainty_are_required(self):
        entries = [
            knowledge("no-source", source_refs=[]),
            knowledge("no-conditions", conditions=[]),
            knowledge("no-limitations", limitations=[]),
            knowledge(
                "credentials", source_refs=["https://user:secret@example.com/paper"]
            ),
            knowledge("unknown-ref", source_refs=["artifacts/" + "0" * 32 + ".json"]),
        ]
        for entry in entries:
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                self.publisher.publish(self.scout(payload(entry)))
        self.assertEqual(self.publisher.snapshot()["version"], 0)

    def test_initial_eligibility_conflict_does_not_overwrite_or_drop_knowledge(self):
        measurement, _, first = self.measured_publication()
        # A second initial decision cannot arise through normal one-attempt
        # Review admission; exercising the primitive here proves CAS isolation.
        with self.owner._transaction() as (conn, _):
            receipt = {"eligibility": [], "conflicts": []}
            self.publisher._eligibility(
                conn,
                first["publication_id"],
                "reviews",
                [
                    dict(
                        measurement_id=measurement["id"],
                        valid=False,
                        verified=False,
                        reason="duplicate",
                    )
                ],
                receipt,
                knowledge_version=first["publication_id"],
            )
        self.assertEqual(receipt["eligibility"], [])
        self.assertEqual(receipt["conflicts"][0]["actual_revision"], 1)
        self.assertEqual(self.publisher.snapshot()["eligibility"][0]["valid"], 1)

    def test_publication_payload_reads_remain_outside_write_transactions(self):
        call = self.scout(payload(knowledge("outside-lock")))
        inside = [False]
        original_transaction = self.owner._transaction
        original_read = Path.read_text

        @contextmanager
        def tracked():
            with original_transaction() as transaction:
                inside[0] = True
                try:
                    yield transaction
                finally:
                    inside[0] = False

        def checked_read(path, *args, **kwargs):
            self.assertFalse(inside[0], "publication payload read inside transaction")
            return original_read(path, *args, **kwargs)

        with (
            patch.object(self.owner, "_transaction", tracked),
            patch.object(Path, "read_text", checked_read),
        ):
            self.publisher.publish(call)
            self.publisher.publish(call)
        with patch.object(
            Path, "read_text", side_effect=AssertionError("snapshot read payload")
        ):
            self.publisher.snapshot()

    def test_failed_transaction_leaves_review_pending_and_can_retry_once(self):
        iteration, measurement, _ = self.trial("atomic", measured=True)
        decision = dict(
            measurement_id=measurement["id"],
            valid=True,
            verified=True,
            reason="receipt checked",
        )
        call = self.review(
            iteration,
            payload(knowledge("atomic"), reviews=[decision]),
            [measurement["receipt_ref"]],
        )
        original = self.publisher._eligibility

        def fail_after_decision(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected publication failure")

        with patch.object(
            self.publisher, "_eligibility", side_effect=fail_after_decision
        ):
            with self.assertRaises(RuntimeError):
                self.publisher.publish(call)
        snapshot = self.publisher.snapshot()
        self.assertEqual(
            (snapshot["version"], snapshot["knowledge"], snapshot["eligibility"]),
            (0, [], []),
        )
        self.assertEqual(self.iteration(iteration["id"])["phase"], "publication")
        self.assertEqual(self.publisher.publish(call)["publication_id"], 1)
        self.assertEqual(self.iteration(iteration["id"])["revision"], 5)


if __name__ == "__main__":
    unittest.main()
