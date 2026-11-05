"""Frozen-context validation and atomic feedback on the real SQLite path."""

from dataclasses import FrozenInstanceError, replace
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from research_loop.runtime.context import ContextPackage, ContextSnapshot, build_context
from research_loop.runtime.files import publish_artifact
from research_loop.runtime.store import (
    ConflictError,
    InvalidTransition,
    OwnershipError,
    StateStore,
    StateStoreError,
)
from research_loop.runtime.updates import CallIdentity, UpdateProposal, Updater


def normalize(output, context):
    if set(output) != {"answer"} or not isinstance(output["answer"], str):
        raise ValueError("Expected an answer string")
    if not output["answer"].strip():
        raise ValueError("Answer must not be empty")
    return {"answer": output["answer"].strip()}


class RuntimeUpdatesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = StateStore.create(
            self.root,
            config={
                "goal": "Synthetic validation",
                "global_evaluation": {"id": "fixed"},
                "permissions": {"allow_changes": False},
                "context_budget": {"tokens": 2048},
            },
            clock=lambda: 10,
        )
        self.session = self.store.coordinator()
        self.owner = self.session.__enter__()
        self.addCleanup(lambda: self.session.__exit__(None, None, None))
        self.updater = Updater(
            self.owner,
            validators={
                role: normalize
                for role in ("scout", "advocate", "judge", "plan", "execute", "review")
            },
        )
        self.owner.create_task(
            "T1", {"instruction": "Operator task", "allow_changes": False}
        )
        self.iteration = self.owner.claim_task("T1")
        self.evidence = publish_artifact(self.root, {"observation": "fixed evidence"})

    def call(self, role="plan", request_id=None):
        if role in {"plan", "execute", "review"}:
            iteration = self.store.snapshot()["iterations"][0]
            attempt = self.owner.begin_attempt(
                role,
                request_id=request_id or role,
                iteration_id=iteration["id"],
                expected_revision=iteration["revision"],
            )
        else:
            attempt = self.owner.begin_attempt(role, request_id=request_id or role)
        return CallIdentity.from_attempt(self.store.snapshot()["study_id"], attempt)

    def package(
        self,
        call,
        *,
        inputs_override=None,
        revisions_override=None,
        task_override=False,
        manifest_budget=None,
        allowed=True,
    ):
        record = self.owner._feedback_snapshot(call.to_dict())
        config = self.store.snapshot()["config"]
        inputs = {
            key: config[key] for key in ("goal", "global_evaluation", "permissions")
        }
        inputs.update(task=record["task"], budget=self.store.snapshot()["limits"])
        if call.role in {"execute", "review"}:
            accepted = self.root / record["plan"]["output_ref"]
            inputs["plan"] = json.loads(accepted.read_text())["normalized_output"]
        required = list(inputs)
        if inputs_override:
            inputs.update(inputs_override)
        inputs["evidence"] = "Previously published observation"
        sources = [
            {
                "id": key,
                "key": key,
                "ref": self.evidence if key == "evidence" else f"control:{key}",
                "revision": 1,
                "reason": "Trusted controller input",
                "allow_feedback": True,
            }
            for key in inputs
        ]
        revisions = dict(
            study=1,
            task=record["task"]["revision"] if record["task"] else None,
            iteration=call.expected_revision,
            knowledge=0,
        )
        if revisions_override:
            revisions.update(revisions_override)
        snapshot = ContextSnapshot.capture(
            study_id=call.study_id,
            revisions=revisions,
            inputs=inputs,
            sources=sources,
            protected_keys=required,
            allowed_evidence=[self.evidence] if allowed else [],
        )
        return build_context(
            snapshot,
            call.role,
            {} if task_override else record["task"],
            "policy:v1",
            config["context_budget"] if manifest_budget is None else manifest_budget,
        )

    def bind(self, call, **kwargs):
        package = self.package(call, **kwargs)
        reference = self.updater.bind_context(call, package)
        return package, reference

    def propose(self, call, output=None, evidence_refs=None):
        return self.updater.validate_output(
            call,
            {"answer": " accepted "} if output is None else output,
            [self.evidence] if evidence_refs is None else evidence_refs,
        )

    def test_bind_validate_apply_advances_plan_once(self):
        call = self.call()
        package, reference = self.bind(call)
        self.assertEqual(self.updater.bind_context(call, package), reference)
        before = self.store.snapshot()
        files_before = set((self.root / "artifacts").iterdir())
        proposal = self.propose(call)
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(set((self.root / "artifacts").iterdir()), files_before)
        self.assertEqual(proposal.normalized_output, {"answer": "accepted"})
        receipt = self.updater.apply_update(proposal, package.manifest["revisions"])
        after = self.store.snapshot()
        self.assertEqual(
            (after["iterations"][0]["phase"], after["iterations"][0]["revision"]),
            ("execute", 2),
        )
        self.assertEqual(after["counters"]["calls"], 1)
        self.assertEqual(after["attempts"][0]["receipt"], receipt.to_dict())
        files_after = set((self.root / "artifacts").iterdir())
        self.assertEqual(
            self.updater.apply_update(proposal, proposal.revisions).to_dict(),
            receipt.to_dict(),
        )
        self.assertEqual(set((self.root / "artifacts").iterdir()), files_after)
        self.assertEqual(self.store.snapshot(), after)

    def test_committed_proposal_replays_after_reopen_without_new_artifact(self):
        call = self.call()
        self.bind(call)
        proposal = self.propose(call)
        receipt = self.updater.apply_update(proposal, proposal.revisions)
        self.session.__exit__(None, None, None)
        self.session = StateStore(self.root, clock=lambda: 10).coordinator()
        self.owner = self.session.__enter__()
        self.updater = Updater(self.owner, validators={"plan": normalize})
        before = self.store.snapshot()
        files = set((self.root / "artifacts").iterdir())
        replay = UpdateProposal.from_dict(proposal.to_dict())
        self.assertEqual(
            self.updater.apply_update(replay, replay.revisions).to_dict(),
            receipt.to_dict(),
        )
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(set((self.root / "artifacts").iterdir()), files)
        self.assertLess(receipt.to_dict()["owner_epoch"], self.owner.owner_epoch)
        different = self.propose(call, {"answer": "different"})
        with self.assertRaises(ConflictError):
            self.updater.apply_update(different, different.revisions)

    def test_stale_unresolved_worker_cannot_bind_validate_or_apply(self):
        call = self.call()
        package, _ = self.bind(call)
        proposal = self.propose(call)
        self.session.__exit__(None, None, None)
        self.session = self.store.coordinator()
        self.owner = self.session.__enter__()
        self.updater = Updater(self.owner, validators={"plan": normalize})
        before = self.store.snapshot()
        for operation in (
            lambda: self.updater.bind_context(call, package),
            lambda: self.propose(call),
            lambda: self.updater.apply_update(proposal, proposal.revisions),
        ):
            with self.assertRaises(OwnershipError):
                operation()
        self.assertEqual(self.store.snapshot(), before)

    def test_committed_replay_does_not_run_changed_or_removed_validator(self):
        call = self.call()
        self.bind(call)
        proposal = self.propose(call)
        receipt = self.updater.apply_update(proposal, proposal.revisions)
        self.session.__exit__(None, None, None)
        self.session = self.store.coordinator()
        self.owner = self.session.__enter__()
        for validators in (
            {},
            {
                "plan": lambda *_: (_ for _ in ()).throw(
                    AssertionError("validator rerun")
                )
            },
        ):
            updater = Updater(self.owner, validators=validators)
            self.assertEqual(
                updater.apply_update(proposal, proposal.revisions).to_dict(),
                receipt.to_dict(),
            )

    def test_bound_context_prompt_order_survives_reload_and_reorder_is_conflict(self):
        call = self.call()
        package, _ = self.bind(call)
        observed = []

        def validator(output, context):
            observed.append(list(context))
            return normalize(output, context)

        updater = Updater(self.owner, validators={"plan": validator})
        updater.validate_output(call, {"answer": "ok"}, [])
        self.assertEqual(observed, [list(package.context)])
        changed = package.to_dict()
        changed["context"] = dict(reversed(list(changed["context"].items())))
        with self.assertRaises(ConflictError):
            updater.bind_context(call, ContextPackage.from_dict(changed))

    def test_payload_reads_and_validator_never_run_in_write_transaction(self):
        call = self.call()
        package = self.package(call)
        in_write = [False]
        original_transaction = self.owner._transaction
        original_read = Path.read_text
        original_context = ContextPackage.context.fget
        original_manifest = ContextPackage.manifest.fget

        @contextmanager
        def tracked_transaction():
            with original_transaction() as transaction:
                in_write[0] = True
                try:
                    yield transaction
                finally:
                    in_write[0] = False

        def read_outside(path, *args, **kwargs):
            self.assertFalse(in_write[0], "file payload read inside write transaction")
            return original_read(path, *args, **kwargs)

        def validator(output, context):
            self.assertFalse(in_write[0], "validator inside write transaction")
            return normalize(output, context)

        def context_outside(package):
            self.assertFalse(
                in_write[0], "context payload parsed inside write transaction"
            )
            return original_context(package)

        def manifest_outside(package):
            self.assertFalse(
                in_write[0], "manifest payload parsed inside write transaction"
            )
            return original_manifest(package)

        updater = Updater(self.owner, validators={"plan": validator})
        with (
            patch.object(self.owner, "_transaction", tracked_transaction),
            patch.object(Path, "read_text", read_outside),
            patch.object(ContextPackage, "context", property(context_outside)),
            patch.object(ContextPackage, "manifest", property(manifest_outside)),
        ):
            updater.bind_context(call, package)
            proposal = updater.validate_output(call, {"answer": "ok"}, [])
            updater.apply_update(proposal, proposal.revisions)
            updater.apply_update(proposal, proposal.revisions)

    def test_exact_identity_role_epoch_token_and_revision_checked(self):
        call = self.call()
        package, _ = self.bind(call)
        for changed, error in (
            (replace(call, worker_token="forged"), OwnershipError),
            (replace(call, owner_epoch=call.owner_epoch + 1), ConflictError),
            (replace(call, role="review"), ConflictError),
            (replace(call, iteration_id="wrong"), ConflictError),
            (replace(call, study_id="wrong"), ConflictError),
            (replace(call, expected_revision=2), ConflictError),
            (replace(call, attempt_id="unknown"), ConflictError),
        ):
            with self.subTest(changed=changed), self.assertRaises(error):
                self.updater.bind_context(changed, package)

    def test_required_binding_fields_cannot_be_changed_or_rebound(self):
        call = self.call()
        for field, value in (
            ("goal", "replace goal"),
            ("global_evaluation", {"id": "changed"}),
            ("permissions", {"allow_changes": True}),
            ("task", None),
            ("budget", {"max_calls": 999}),
        ):
            with self.subTest(field=field), self.assertRaises(ConflictError):
                self.updater.bind_context(
                    call, self.package(call, inputs_override={field: value})
                )
        for package in (
            self.package(call, task_override=True),
            self.package(call, revisions_override={"knowledge": 1}),
            self.package(call, manifest_budget={}),
        ):
            with self.assertRaises(ConflictError):
                self.updater.bind_context(call, package)
        package, reference = self.bind(call)
        changed = package.to_dict()
        changed["manifest"]["policy_ref"] = "different-policy"
        with self.assertRaises(ConflictError):
            self.updater.bind_context(call, ContextPackage.from_dict(changed))
        self.assertEqual(self.updater.bind_context(call, package), reference)

    def test_context_and_compute_budgets_are_distinct(self):
        call = self.call()
        package, _ = self.bind(call)
        self.assertEqual(package.manifest["budget"], {"tokens": 2048})
        self.assertEqual(package.context["budget"], self.store.snapshot()["limits"])

    def test_validation_requires_binding_allowlist_and_registered_validator(self):
        call = self.call()
        with self.assertRaises(InvalidTransition):
            self.propose(call)
        self.bind(call)
        before = self.store.snapshot()
        no_validator = Updater(self.owner, validators={})
        with self.assertRaises(InvalidTransition):
            no_validator.validate_output(call, {"answer": "ok"}, [])
        for refs in (["unknown"], [self.evidence, self.evidence], "not-a-list"):
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                self.propose(call, evidence_refs=refs)
        for output in (
            {"answer": ""},
            {"state_after": {}},
            {"authorization": {}},
            {"answer": "x", "extra": 1},
        ):
            with self.subTest(output=output), self.assertRaises(ValueError):
                self.propose(call, output)
        self.assertEqual(self.store.snapshot(), before)

    def test_forged_proposal_reruns_validator_and_rejects_modified_normalization(self):
        call = self.call()
        self.bind(call)
        invoked = []

        def validator(output, context):
            invoked.append((output.copy(), context.copy()))
            return normalize(output, context)

        self.updater = Updater(self.owner, validators={"plan": validator})
        proposal = self.propose(call)
        forged = proposal.to_dict()
        forged["normalized_output"] = {"answer": "forged"}
        before = self.store.snapshot()
        with self.assertRaises(ConflictError):
            self.updater.apply_update(
                UpdateProposal.from_dict(forged), proposal.revisions
            )
        self.assertEqual(len(invoked), 2)
        self.assertEqual(self.store.snapshot(), before)
        forged["output"] = {"answer": "", "state_after": {}}
        with self.assertRaises(ValueError):
            self.updater.apply_update(
                UpdateProposal.from_dict(forged), proposal.revisions
            )
        with self.assertRaises(ConflictError):
            self.updater.apply_update(proposal, {**proposal.revisions, "iteration": 2})
        self.assertEqual(self.store.snapshot(), before)

    def test_proposal_and_receipt_values_are_detached_and_frozen(self):
        call = self.call()
        self.bind(call)
        output = {"answer": "ok"}
        proposal = self.propose(call, output)
        output["answer"] = "changed"
        detached = proposal.to_dict()
        detached["output"]["answer"] = "changed"
        self.assertEqual(proposal.to_dict()["output"]["answer"], "ok")
        with self.assertRaises(FrozenInstanceError):
            proposal._serialized = "{}"
        receipt = self.updater.apply_update(proposal, proposal.revisions)
        data = receipt.to_dict()
        data["evidence_refs"].clear()
        self.assertEqual(receipt.to_dict()["evidence_refs"], [self.evidence])
        with self.assertRaises(FrozenInstanceError):
            receipt.new_field = 1

    def test_raw_acceptance_cannot_bypass_bound_updater(self):
        call = self.call()
        self.bind(call)
        reference = publish_artifact(self.root, {"answer": "unvalidated"})
        before = self.store.snapshot()
        with self.assertRaises(InvalidTransition):
            self.owner.accept_attempt(
                call.attempt_id,
                call.worker_token,
                reference,
                expected_revision=call.expected_revision,
            )
        self.assertEqual(self.store.snapshot(), before)
        proposal = self.propose(call)
        receipt = self.updater.apply_update(proposal, proposal.revisions)
        with self.assertRaises(InvalidTransition):
            self.owner.accept_attempt(
                call.attempt_id,
                call.worker_token,
                receipt.output_ref,
                expected_revision=call.expected_revision,
            )

    def test_stop_allows_finishing_admitted_work(self):
        call = self.call()
        self.bind(call)
        proposal = self.propose(call)
        self.owner.set_stop("finish admitted work")
        receipt = self.updater.apply_update(proposal, proposal.revisions)
        self.assertEqual(receipt.revision, 2)
        self.assertTrue(self.store.snapshot()["stop"]["requested"])

    def test_execute_and_review_bind_accepted_normalized_plan(self):
        plan = self.call()
        self.bind(plan)
        proposal = self.propose(plan, {"answer": " immutable plan "})
        self.updater.apply_update(proposal, proposal.revisions)
        execute = self.call("execute")
        package = self.package(execute)
        self.assertEqual(package.context["plan"], {"answer": "immutable plan"})
        with self.assertRaises(ConflictError):
            self.updater.bind_context(
                execute,
                self.package(execute, inputs_override={"plan": {"answer": "changed"}}),
            )
        self.updater.bind_context(execute, package)
        execution = self.propose(execute)
        self.updater.apply_update(execution, execution.revisions)
        review = self.call("review")
        package, _ = self.bind(review)
        self.assertEqual(package.context["plan"], {"answer": "immutable plan"})
        reviewed = self.propose(review)
        self.updater.apply_update(reviewed, reviewed.revisions)
        self.assertEqual(self.store.snapshot()["iterations"][0]["phase"], "publication")

    def test_standalone_call_has_no_task_or_iteration_revision(self):
        call = self.call("scout")
        package, _ = self.bind(call)
        self.assertIsNone(package.context["task"])
        self.assertIsNone(package.manifest["task"])
        self.assertEqual(
            package.manifest["revisions"],
            {"study": 1, "task": None, "iteration": None, "knowledge": 0},
        )
        proposal = self.propose(call)
        receipt = self.updater.apply_update(proposal, proposal.revisions)
        self.assertIsNone(receipt.revision)
        self.assertEqual(self.store.snapshot()["iterations"][0]["phase"], "plan")

    def test_changed_context_or_evidence_metadata_rejected(self):
        call = self.call()
        _, context_ref = self.bind(call)
        proposal = self.propose(call)
        path = self.root / self.evidence
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.updater.apply_update(proposal, proposal.revisions)
        path.chmod(0o444)
        context = self.root / context_ref
        context.unlink()
        context.write_text("substituted context")
        context.chmod(0o444)
        with self.assertRaises(ConflictError):
            self.propose(call)
        self.assertEqual(self.store.snapshot()["iterations"][0]["phase"], "plan")

    def test_evidence_replacement_after_binding_before_first_proposal_is_rejected(self):
        call = self.call()
        package, _ = self.bind(call)
        before = self.store.snapshot()
        path = self.root / self.evidence
        path.chmod(0o644)
        path.write_text('{"observation": "changed after source revision was frozen"}')
        path.chmod(0o444)
        with self.assertRaises(ConflictError):
            self.propose(call)
        with self.assertRaises(ConflictError):
            self.updater.bind_context(call, package)
        self.assertEqual(self.store.snapshot(), before)

    def test_binding_rejects_missing_or_noncanonical_allowed_artifacts(self):
        call = self.call()
        package = self.package(call)
        before = self.store.snapshot()
        artifacts_before = set((self.root / "artifacts").iterdir())
        for reference, error in (
            ("control:goal", ValueError),
            ("artifacts/" + "0" * 32 + ".json", FileNotFoundError),
        ):
            data = package.to_dict()
            data["manifest"]["allowed_evidence"] = [reference]
            for source in data["manifest"]["sources"]:
                if source["key"] == "evidence":
                    source["ref"] = reference
            with self.subTest(reference=reference), self.assertRaises(error):
                self.updater.bind_context(call, ContextPackage.from_dict(data))
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(set((self.root / "artifacts").iterdir()), artifacts_before)
        # Symbolic configuration provenance is valid when it grants no file access.
        self.updater.bind_context(call, package)

    def test_committed_replay_rejects_changed_original_bound_evidence(self):
        call = self.call()
        self.bind(call)
        proposal = self.propose(call)
        self.updater.apply_update(proposal, proposal.revisions)
        before = self.store.snapshot()
        path = self.root / self.evidence
        path.chmod(0o644)
        path.write_text('{"observation": "substituted after acceptance"}')
        path.chmod(0o444)
        with self.assertRaises(ConflictError):
            self.updater.apply_update(proposal, proposal.revisions)
        self.assertEqual(self.store.snapshot(), before)

    def test_failed_acceptance_transaction_rolls_back_phase_and_proposal(self):
        call = self.call()
        self.bind(call)
        proposal = self.propose(call)
        original = self.owner._accept_record

        def fail_after_accept(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected failure after phase update")

        before = self.store.snapshot()
        with patch.object(self.owner, "_accept_record", side_effect=fail_after_accept):
            with self.assertRaises(RuntimeError):
                self.updater.apply_update(proposal, proposal.revisions)
        self.assertEqual(self.store.snapshot(), before)
        self.assertIsNone(self.store.snapshot()["attempts"][0]["output_ref"])
        self.updater.apply_update(proposal, proposal.revisions)
        self.assertEqual(self.store.snapshot()["iterations"][0]["revision"], 2)

    def test_stage_one_schema_is_explicitly_rejected(self):
        self.session.__exit__(None, None, None)
        with sqlite3.connect(self.store.path) as conn:
            conn.execute("PRAGMA user_version=1")
        with self.assertRaisesRegex(StateStoreError, "schema version: 1"):
            StateStore(self.root)


if __name__ == "__main__":
    unittest.main()
