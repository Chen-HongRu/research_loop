"""SQLite operation authority tests using synthetic trusted facts, no backend jobs.

Proof assertions below exercise contracts; they do not authenticate a real sender.
The separate local-adapter smoke supplies real process execution evidence.
"""

from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from examples.runtime_publication import EVALUATION
from research_loop.runtime.context import ContextSnapshot, build_context
from research_loop.runtime.files import publish_artifact
from research_loop.runtime.operations import Operations
from research_loop.runtime.store import (
    BudgetExceeded,
    ConflictError,
    InvalidTransition,
    StateStore,
)
from research_loop.runtime.updates import CallIdentity, Updater


def configuration():
    grant = dict(
        backends=["synthetic"],
        max_operations=20,
        resource_limits={"cpu": 1},
        cost_limits={"ticks": 100, "fee": 100},
        deadline=1000,
    )
    return dict(
        goal="Synthetic operation authority",
        permissions={},
        global_evaluation=EVALUATION,
        execution_backends={
            "synthetic": dict(
                version="backend-v1", environment=dict(id="local", version="env-v1")
            )
        },
        resource_pools={"cpu": 1},
        operation_authorizations={"a": grant, "b": deepcopy(grant)},
    )


def request(identity="work", **changes):
    return (
        dict(
            id=identity,
            backend="synthetic",
            authorization_id="a",
            resources={"cpu": 1},
            cost_bounds={"ticks": 4, "fee": 0},
            timeout_seconds=60,
            input_artifacts=[
                dict(path="checkpoint/input.bin", version="checkpoint-v7")
            ],
            parameters={"seed": 1},
        )
        | changes
    )


class RuntimeOperationsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = 10
        self.store = StateStore.create(
            self.root, config=configuration(), clock=lambda: self.now
        )
        self.session = self.store.coordinator()
        self.owner = self.session.__enter__()
        self.addCleanup(lambda: self.session.__exit__(None, None, None))
        self.ops = Operations(self.owner)
        self.updater = Updater(
            self.owner,
            validators={
                role: lambda output, _: output for role in ("plan", "execute", "review")
            },
        )
        self.serial = 0

    def accept(self, role, output, iteration):
        self.serial += 1
        attempt = self.owner.begin_attempt(
            role,
            request_id=f"synthetic-{self.serial}",
            iteration_id=iteration["id"],
            expected_revision=iteration["revision"],
        )
        call = CallIdentity.from_attempt(self.store.snapshot()["study_id"], attempt)
        record = self.owner._feedback_snapshot(call.to_dict())
        config = self.store.snapshot()["config"]
        inputs = {
            key: config[key]
            for key in (
                "goal",
                "permissions",
                "global_evaluation",
                "execution_backends",
                "resource_pools",
                "operation_authorizations",
            )
        }
        inputs.update(task=record["task"], budget=self.store.snapshot()["limits"])
        if role in {"execute", "review"}:
            plan = record["plan"]
            inputs["plan"] = self.updater._read_record(
                plan["output_ref"], plan["output_identity"]
            )["normalized_output"]
        sources = [
            dict(
                id=key,
                key=key,
                ref=f"control:{key}",
                revision=1,
                reason="Frozen fixture",
                allow_feedback=True,
            )
            for key in inputs
        ]
        snapshot = ContextSnapshot.capture(
            study_id=call.study_id,
            revisions=dict(
                study=1,
                task=record["task"]["revision"],
                iteration=call.expected_revision,
                knowledge=record["knowledge_version"],
            ),
            inputs=inputs,
            sources=sources,
            protected_keys=list(inputs),
            allowed_evidence=[],
        )
        package = build_context(snapshot, role, record["task"], "synthetic:v1", {})
        self.updater.bind_context(call, package)
        proposal = self.updater.validate_output(call, output, [])
        receipt = self.updater.apply_update(proposal, proposal.revisions)
        return call, receipt

    def iteration(self, identity):
        return next(
            row for row in self.store.snapshot()["iterations"] if row["id"] == identity
        )

    def execution(self, requests=None):
        name = f"task-{self.serial}"
        self.owner.create_task(name, {"instruction": "Synthetic operation"})
        iteration = self.owner.claim_task(name)
        self.accept("plan", dict(timeout_seconds=60, local_evaluation=None), iteration)
        call, receipt = self.accept(
            "execute",
            dict(
                artifacts=[], operations=[request()] if requests is None else requests
            ),
            self.iteration(iteration["id"]),
        )
        return call, receipt.to_dict().get("operation_ids", [])

    def rows(self, table):
        with self.store._connection(readonly=True) as conn:
            return [
                dict(row)
                for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]

    def event(self, operation_id, kind, data, *, key=None):
        op = self.ops.get(operation_id)
        key = key or f"{operation_id}:{kind}:{op['revision']}"
        envelope = self.ops.envelope(operation_id, data)
        receipt = self.ops.record_receipt(
            operation_id, op["revision"], kind, key, envelope
        )
        return receipt

    def prepare(self, operation_id):
        return self.event(
            operation_id,
            "prepare",
            dict(
                ready=True,
                reason="Synthetic preparation",
                effective_parameters=self.ops.request(operation_id)["parameters"],
            ),
        )

    def submit(self, operation_id):
        if self.ops.get(operation_id)["state"] == "pending":
            self.prepare(operation_id)
        self.ops.reserve_submit(operation_id, self.ops.get(operation_id)["revision"])
        return self.event(
            operation_id,
            "submission",
            dict(
                outcome="accepted",
                job_id="job-" + operation_id,
                proof=None,
                reason="Synthetic backend admission",
            ),
        )

    def inspect(
        self,
        operation_id,
        *,
        release=True,
        terminal=True,
        outcome="succeeded",
        job_id=None,
        proof=None,
    ):
        return self.event(
            operation_id,
            "inspection",
            dict(
                job_id=self.ops.get(operation_id)["job_id"]
                if job_id is None
                else job_id,
                terminal=terminal,
                outcome=outcome if terminal else None,
                physical_released=release,
                not_submitted_proof=proof,
            ),
        )

    def collect(self, operation_id, *, evaluations=(), artifacts=(), reason=None):
        return self.event(
            operation_id,
            "collection",
            dict(
                artifacts=list(artifacts),
                evaluations=list(evaluations),
                unavailable_reason=reason,
            ),
        )

    def test_execute_seals_exact_requests_backend_environment_and_absolute_deadline(
        self,
    ):
        call, ids = self.execution(
            [request("one"), request("two", authorization_id="b")]
        )
        self.assertEqual(len(ids), 2)
        operations = self.ops.list(execute_attempt_id=call.attempt_id)
        self.assertEqual([row["deadline"] for row in operations], [70, 70])
        self.assertEqual(
            operations[0]["environment"], {"id": "local", "version": "env-v1"}
        )
        self.assertEqual(
            self.ops.request(ids[0])["input_artifacts"],
            [{"path": "checkpoint/input.bin", "version": "checkpoint-v7"}],
        )
        returned = self.ops.request(ids[0])
        returned["parameters"]["seed"] = 999
        self.assertEqual(self.ops.request(ids[0])["parameters"], {"seed": 1})
        raw = json.loads((self.root / operations[0]["request_ref"]).read_text())
        self.assertEqual(raw["normalized_output"]["artifacts"], [])
        self.assertEqual(len(self.rows("operation_groups")), 1)
        self.assertEqual(
            self.rows("operation_authorities")[0]["admitted_operations"], 0
        )

    def test_invalid_requests_fail_before_group_or_authority_allocation(self):
        for changes in (
            {"resources": {"cpu": 0}},
            {"backend": "missing"},
            {"timeout_seconds": 61},
            {"authorization_id": "missing"},
            {"cost_bounds": {"new-unit": 1}},
            {"environment": {}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.execution([request(**changes)])
        self.assertEqual(self.rows("operations"), [])
        self.assertEqual(self.rows("operation_groups"), [])
        self.assertEqual(self.rows("operation_authorities"), [])

    def test_empty_operations_keep_direct_review_path(self):
        call, ids = self.execution([])
        self.assertEqual(ids, [])
        self.assertEqual(self.rows("operation_groups"), [])
        iteration = self.iteration(call.iteration_id)
        self.owner.begin_attempt(
            "review",
            request_id="direct-review",
            iteration_id=iteration["id"],
            expected_revision=iteration["revision"],
        )

    def test_last_physical_capacity_race_has_one_admission_and_no_failed_charge(self):
        _, ids = self.execution([request("a"), request("b")])
        for identity in ids:
            self.prepare(identity)
        barrier = threading.Barrier(3)
        results = []

        def reserve(identity):
            barrier.wait()
            try:
                results.append(
                    self.ops.reserve_submit(
                        identity, self.ops.get(identity)["revision"]
                    )
                )
            except BudgetExceeded:
                results.append("held")

        threads = [
            threading.Thread(target=reserve, args=(identity,)) for identity in ids
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results.count("held"), 1)
        self.assertEqual(self.rows("operation_pool_balances")[0]["held"], "1")
        self.assertEqual(
            self.rows("operation_authorities")[0]["admitted_operations"], 1
        )
        self.assertEqual(
            sum(row["submission_token"] is not None for row in self.ops.list()), 1
        )

    def test_unknown_submission_survives_reopen_and_never_resubmits(self):
        _, [identity] = self.execution()
        self.prepare(identity)
        permit = self.ops.reserve_submit(identity, self.ops.get(identity)["revision"])
        self.event(
            identity,
            "submission",
            dict(
                outcome="unknown",
                job_id=None,
                proof=None,
                reason="Connection lost after intent",
            ),
        )
        deadline = self.ops.get(identity)["deadline"]
        self.session.__exit__(None, None, None)
        self.now = 35
        self.session = self.store.coordinator()
        self.owner = self.session.__enter__()
        self.ops = Operations(self.owner)
        op = self.ops.get(identity)
        self.assertEqual(op["submission_token"], permit["submission_token"])
        self.assertEqual(op["deadline"], deadline)
        self.assertTrue(op["external_open"])
        with self.assertRaises(InvalidTransition):
            self.ops.reserve_submit(identity, op["revision"])
        with self.assertRaises(InvalidTransition):
            self.inspect(
                identity,
                outcome="not_submitted",
                proof={
                    "basis": "no_submission_intent",
                    "reference": "new owner epoch is not proof",
                },
            )
        self.inspect(
            identity,
            outcome="not_submitted",
            proof={
                "basis": "sender_retired",
                "reference": "synthetic positively retired sender",
            },
        )
        self.assertEqual(self.ops.get(identity)["terminal_outcome"], "not_submitted")
        self.assertIsNone(self.ops.get(identity)["job_id"])
        self.assertEqual(
            self.rows("operation_authorities")[0]["admitted_operations"], 1
        )
        self.assertEqual(self.rows("operation_pool_balances")[0]["held"], "0")
        self.assertEqual(self.ops.get(identity)["held_costs"], {"ticks": 4, "fee": 0})

    def test_cancellation_ack_and_known_terminal_do_not_infer_release_or_fees(self):
        call, [identity] = self.execution()
        self.submit(identity)
        self.event(
            identity,
            "cancellation",
            dict(
                job_id=self.ops.get(identity)["job_id"],
                acknowledged=True,
                reason="Cancellation accepted, completion unknown",
            ),
        )
        self.assertEqual(self.ops.get(identity)["held_resources"], {"cpu": 1})
        self.inspect(identity, release=False)
        self.assertTrue(self.ops.get(identity)["external_open"])
        self.collect(identity)
        self.ops.finalize_feedback(call.attempt_id)
        iteration = self.iteration(call.iteration_id)
        self.owner.begin_attempt(
            "review",
            request_id="review-with-fees-unknown",
            iteration_id=iteration["id"],
            expected_revision=iteration["revision"],
        )
        self.assertEqual(self.ops.get(identity)["held_costs"], {"ticks": 4, "fee": 0})
        self.assertEqual(self.ops.get(identity)["held_resources"], {"cpu": 1})

    def test_per_unit_final_cost_zero_closes_unknown_and_replay_allocates_nothing(self):
        _, [identity] = self.execution()
        self.submit(identity)
        op = self.ops.get(identity)
        key = "cost-partial"
        payload = self.ops.envelope(identity, {"final_costs": {"ticks": 2}})
        receipt = self.ops.record_receipt(
            identity, op["revision"], "settlement", key, payload
        )
        self.assertFalse(self.ops.get(identity)["costs_final"])
        self.assertEqual(self.ops.get(identity)["held_costs"], {"fee": 0})
        self.event(identity, "settlement", {"final_costs": {"fee": 0}})
        self.assertTrue(self.ops.get(identity)["costs_final"])
        files = set((self.root / "artifacts").iterdir())
        before = self.ops.get(identity)
        self.assertEqual(
            self.ops.record_receipt(
                identity, op["revision"], "settlement", key, payload
            ),
            receipt,
        )
        self.assertEqual(self.ops.get(identity), before)
        self.assertEqual(set((self.root / "artifacts").iterdir()), files)
        self.assertEqual(
            self.ops.record_receipt(
                identity, before["revision"], "settlement", key, payload
            ),
            receipt,
        )
        self.assertEqual(self.ops.get(identity), before)
        self.assertEqual(set((self.root / "artifacts").iterdir()), files)
        recorded = next(
            row for row in self.rows("operation_events") if row["event_key"] == key
        )
        self.assertEqual(recorded["expected_revision"], op["revision"])
        self.event(identity, "settlement", {"final_costs": {"ticks": 3}})
        cost = next(
            row for row in self.rows("operation_scope_costs") if row["unit"] == "ticks"
        )
        self.assertEqual(cost["settled"], "3")

    def test_actual_cost_overrun_and_unexpected_unit_record_truth_and_block_scope(self):
        _, ids = self.execution(
            [request("one"), request("two"), request("other", authorization_id="b")]
        )
        self.submit(ids[0])
        self.inspect(ids[0])
        self.event(
            ids[0], "settlement", {"final_costs": {"ticks": 10**50, "new-fee": 1}}
        )
        self.assertEqual(self.ops.get(ids[0])["actual_costs"]["ticks"], 10**50)
        self.prepare(ids[1])
        with self.assertRaises(BudgetExceeded):
            self.ops.reserve_submit(ids[1], self.ops.get(ids[1])["revision"])
        self.prepare(ids[2])
        self.ops.reserve_submit(ids[2], self.ops.get(ids[2])["revision"])
        self.assertEqual(self.ops.get(ids[2])["authorization_id"], "b")

    def test_late_job_quarantine_restores_exposure_without_reopening_science(self):
        call, ids = self.execution(
            [request("absent"), request("other", authorization_id="b")]
        )
        self.prepare(ids[0])
        self.ops.reserve_submit(ids[0], self.ops.get(ids[0])["revision"])
        self.inspect(
            ids[0],
            outcome="not_submitted",
            proof={
                "basis": "backend_no_late_create",
                "reference": "synthetic backend absence proof",
            },
        )
        self.event(ids[0], "settlement", {"final_costs": {"ticks": 0, "fee": 0}})
        self.collect(ids[0], reason="No submitted job")
        self.prepare(ids[1])
        self.ops.reserve_submit(ids[1], self.ops.get(ids[1])["revision"])
        self.event(
            ids[0],
            "submission",
            dict(
                outcome="accepted",
                job_id="unexpected-late-job",
                proof=None,
                reason="Contradictory positive backend observation",
            ),
        )
        op = self.ops.get(ids[0])
        self.assertEqual(
            (op["state"], op["terminal_outcome"]), ("terminal", "not_submitted")
        )
        self.assertEqual(op["job_id"], "unexpected-late-job")
        self.assertIsNotNone(op["inconsistency"])
        self.assertEqual(op["collection_state"], "unavailable")
        self.assertEqual(self.rows("operation_pool_balances")[0]["held"], "2")
        self.assertEqual(op["held_costs"], {"ticks": 4, "fee": 0})
        with self.assertRaises(InvalidTransition):
            self.ops.finalize_feedback(call.attempt_id)
        self.inspect(ids[0], outcome="failed")
        self.assertFalse(self.ops.get(ids[0])["external_open"])
        self.assertEqual(self.ops.get(ids[0])["terminal_outcome"], "not_submitted")
        self.assertEqual(self.rows("operation_pool_balances")[0]["held"], "1")
        self.event(ids[0], "settlement", {"final_costs": {"ticks": 3, "fee": 0}})
        self.assertEqual(self.ops.get(ids[0])["actual_costs"], {"ticks": 3, "fee": 0})
        self.assertIsNotNone(self.ops.get(ids[0])["inconsistency"])

    def test_receipts_bind_original_backend_environment_request_and_actual_job_once(
        self,
    ):
        _, [identity] = self.execution()
        self.submit(identity)
        op = self.ops.get(identity)
        for field, value in (
            ("backend_version", "wrong"),
            ("environment", {"id": "other", "version": "env-v1"}),
            ("request_id", "other"),
            ("owner_epoch", 999),
        ):
            body = self.ops.envelope(identity, {"final_costs": {"ticks": 0}})
            body[field] = value
            with self.subTest(field=field), self.assertRaises(ConflictError):
                self.ops.record_receipt(
                    identity, op["revision"], "settlement", field, body
                )
        self.assertEqual(self.ops.get(identity), op)
        self.inspect(identity, job_id="another-job")
        contradictory = self.ops.get(identity)
        self.assertEqual(contradictory["job_id"], op["job_id"])
        self.assertEqual(contradictory["unexpected_job_ids"], ["another-job"])
        self.assertIsNotNone(contradictory["inconsistency"])
        self.assertTrue(contradictory["external_open"])

    def test_new_live_fact_after_terminal_restores_holds_even_after_prior_cleanup(self):
        _, [identity] = self.execution()
        self.submit(identity)
        self.inspect(identity)
        self.event(identity, "settlement", {"final_costs": {"ticks": 2, "fee": 0}})
        original_outcome = self.ops.get(identity)["terminal_outcome"]
        for _ in range(2):
            self.inspect(identity, terminal=False, release=False)
            current = self.ops.get(identity)
            self.assertEqual(current["state"], "terminal")
            self.assertEqual(current["terminal_outcome"], original_outcome)
            self.assertEqual(current["held_resources"], {"cpu": 1})
            self.assertEqual(current["held_costs"], {"ticks": 4, "fee": 0})
            self.assertTrue(current["external_open"])
            self.assertIsNotNone(current["inconsistency"])
            self.inspect(identity)
            self.event(identity, "settlement", {"final_costs": {"ticks": 2, "fee": 0}})
            self.assertEqual(self.ops.get(identity)["held_resources"], {})
            self.assertFalse(self.ops.get(identity)["external_open"])

    def test_different_job_quarantines_shared_pool_and_keeps_unknown_fee_exposure(self):
        _, ids = self.execution(
            [request("first"), request("other", authorization_id="b")]
        )
        self.submit(ids[0])
        self.inspect(ids[0])
        self.event(ids[0], "settlement", {"final_costs": {"ticks": 1, "fee": 0}})
        self.inspect(ids[0], job_id="unexpected-job", terminal=False, release=False)
        original = self.ops.get(ids[0])["job_id"]
        self.inspect(ids[0])
        self.event(ids[0], "settlement", {"final_costs": {"ticks": 2, "fee": 0}})
        op = self.ops.get(ids[0])
        self.assertEqual(op["job_id"], original)
        self.assertEqual(op["unexpected_job_ids"], ["unexpected-job"])
        self.assertFalse(op["physical_released"])
        self.assertTrue(op["external_open"])
        self.assertFalse(op["costs_final"])
        self.assertEqual(op["actual_costs"], {"ticks": 2, "fee": 0})
        self.assertEqual(op["held_costs"], {"ticks": 4, "fee": 0})
        self.assertEqual(self.rows("operation_pool_balances")[0]["held"], "1")
        self.prepare(ids[1])
        with self.assertRaises(BudgetExceeded):
            self.ops.reserve_submit(ids[1], self.ops.get(ids[1])["revision"])

    def test_tokenless_positive_job_is_quarantined_without_authorized_admission(self):
        call, [identity] = self.execution()
        self.inspect(identity, job_id="unreserved-job")
        op = self.ops.get(identity)
        self.assertEqual(op["state"], "pending")
        self.assertEqual(op["job_id"], "unreserved-job")
        self.assertEqual(op["unexpected_job_ids"], ["unreserved-job"])
        self.assertIsNone(op["submission_token"])
        self.assertIsNotNone(op["inconsistency"])
        self.assertTrue(op["external_open"])
        self.assertFalse(op["costs_final"])
        self.assertEqual(
            self.rows("operation_authorities")[0]["admitted_operations"], 0
        )
        with self.assertRaises(InvalidTransition):
            self.ops.finalize_feedback(call.attempt_id)
        with self.assertRaises(InvalidTransition):
            self.collect(identity)

    def test_new_terminal_disagreement_preserves_original_outcome_and_retains_fact(
        self,
    ):
        _, [identity] = self.execution()
        self.submit(identity)
        self.inspect(identity)
        original = self.ops.get(identity)["terminal_outcome"]
        receipt = self.inspect(identity, outcome="failed")
        op = self.ops.get(identity)
        self.assertEqual(op["terminal_outcome"], original)
        self.assertIsNotNone(op["inconsistency"])
        self.assertFalse(op["external_open"])
        self.assertEqual(
            self.rows("operation_events")[-1]["event_key"], receipt["event_key"]
        )

    def test_feedback_intake_seal_blocks_review_until_explicit_dispositions(self):
        call, [identity] = self.execution()
        self.submit(identity)
        self.inspect(identity, release=False)
        allowed = publish_artifact(
            self.root, {"synthetic_evaluator": "not auto registered"}
        )
        denied = publish_artifact(self.root, {"final": "must never be opened"})
        original_read = Path.read_text

        def no_denied_body(path, *args, **kwargs):
            if path == self.root / denied:
                raise AssertionError("denied evaluator body opened")
            return original_read(path, *args, **kwargs)

        with patch.object(Path, "read_text", no_denied_body):
            self.collect(
                identity,
                evaluations=[
                    dict(receipt_ref=allowed, allow_feedback=True),
                    dict(receipt_ref=denied, allow_feedback=False),
                ],
            )
        self.assertEqual(
            {
                row["receipt_ref"]: row["disposition"]
                for row in self.rows("evaluation_intake")
            },
            {allowed: "pending", denied: "rejected"},
        )
        with self.assertRaises(InvalidTransition):
            self.ops.finalize_feedback(call.attempt_id)
        iteration = self.iteration(call.iteration_id)
        with self.assertRaises(InvalidTransition):
            self.owner.begin_attempt(
                "review",
                request_id="premature-review",
                iteration_id=iteration["id"],
                expected_revision=iteration["revision"],
            )
        self.ops.reject_evaluation(allowed, "Explicit invalid synthetic evaluator")
        self.ops.finalize_feedback(call.attempt_id)
        self.owner.begin_attempt(
            "review",
            request_id="sealed-review",
            iteration_id=iteration["id"],
            expected_revision=iteration["revision"],
        )

    def test_free_prepare_failure_and_failed_collection_can_finish_without_fake_job(
        self,
    ):
        call, [identity] = self.execution()
        self.event(
            identity,
            "prepare",
            dict(
                ready=False,
                reason="Synthetic prepare failed before any intent",
                effective_parameters={},
            ),
        )
        self.assertEqual(self.ops.get(identity)["terminal_outcome"], "not_submitted")
        self.assertIsNone(self.ops.get(identity)["job_id"])
        self.collect(identity, reason="No job/artifacts were created")
        self.ops.finalize_feedback(call.attempt_id)
        self.assertEqual(
            self.rows("operation_authorities")[0]["admitted_operations"], 0
        )

    def test_resource_update_failure_rolls_back_intent_holds_and_count(self):
        _, [identity] = self.execution()
        self.prepare(identity)
        original = self.ops._add_resources

        def fail_after_hold(*args):
            original(*args)
            raise RuntimeError("hold failure")

        with patch.object(self.ops, "_add_resources", side_effect=fail_after_hold):
            with self.assertRaises(RuntimeError):
                self.ops.reserve_submit(identity, self.ops.get(identity)["revision"])
        self.assertEqual(self.rows("operation_pool_balances")[0]["held"], "0")
        self.assertEqual(
            self.rows("operation_authorities")[0]["admitted_operations"], 0
        )
        self.assertIsNone(self.ops.get(identity)["submission_token"])
        self.ops.reserve_submit(identity, self.ops.get(identity)["revision"])

    def test_receipt_payload_reads_never_run_inside_write_transactions(self):
        _, [identity] = self.execution()
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

        def outside(path, *args, **kwargs):
            self.assertFalse(inside[0], "operation payload body read inside writer")
            return original_read(path, *args, **kwargs)

        with (
            patch.object(self.owner, "_transaction", tracked),
            patch.object(Path, "read_text", outside),
        ):
            self.prepare(identity)
            self.ops.reserve_submit(identity, self.ops.get(identity)["revision"])
            self.event(
                identity,
                "submission",
                dict(
                    outcome="unknown",
                    job_id=None,
                    proof=None,
                    reason="Synthetic ambiguous transport",
                ),
            )
            op = self.ops.get(identity)
            body = self.ops.envelope(identity, {"final_costs": {"fee": 0}})
            self.ops.record_receipt(
                identity, op["revision"], "settlement", "repeat", body
            )
            self.ops.record_receipt(
                identity, op["revision"], "settlement", "repeat", body
            )


if __name__ == "__main__":
    unittest.main()
