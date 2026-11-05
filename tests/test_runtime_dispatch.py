"""Durable dispatch/recovery contracts on real SQLite and local fixture workers."""

from contextlib import contextmanager
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import test_runtime_operations as operation_fixtures
from research_loop.runtime.contexts import RuntimeContexts
from research_loop.runtime.dispatch import DispatchLedger, RuntimeEvents
from research_loop.runtime.files import publish_artifact
from research_loop.runtime.invocations import LocalProcessInvoker
from research_loop.runtime.operations import Operations
from research_loop.runtime.planning_state import PlanningLedger
from research_loop.runtime.store import (
    BudgetExceeded,
    ConflictError,
    InvalidTransition,
    OwnershipError,
    StateStore,
)
from research_loop.runtime.updates import CallIdentity, Updater


ADAPTER = {"id": "local-test", "version": "1"}


class RuntimeDispatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = operation_fixtures.configuration()
        config["runtime"] = {"worker_slots": 1, "worker_adapter": ADAPTER}
        self.now = 10
        self.store = StateStore.create(self.root, config=config, clock=lambda: self.now)
        self.context = self.store.coordinator()
        self.owner = self.context.__enter__()
        self.addCleanup(lambda: self.context.__exit__(None, None, None))
        self._components()
        self.serial = 0

    def _components(self):
        self.ledger = DispatchLedger(self.owner)
        self.events = RuntimeEvents(self.owner)
        self.updater = Updater(
            self.owner,
            validators={
                role: lambda output, _: output
                for role in ("scout", "plan", "execute", "review", "advocate", "judge")
            },
        )
        self.builder = RuntimeContexts(self.owner)

    def reopen(self):
        self.context.__exit__(None, None, None)
        self.context = self.store.coordinator()
        self.owner = self.context.__enter__()
        self._components()

    def reserve(self, role="scout", iteration=None):
        self.serial += 1
        scope = (
            {}
            if iteration is None
            else dict(
                iteration_id=iteration["id"], expected_revision=iteration["revision"]
            )
        )
        attempt = self.owner.begin_attempt(
            role, request_id=f"runtime:test:{self.serial}", **scope
        )
        return CallIdentity.from_attempt(self.store.snapshot()["study_id"], attempt)

    def bind(self, call):
        package = self.builder.build(call)
        context_ref = self.updater.bind_context(call, package)
        input_ref = publish_artifact(
            self.root,
            dict(
                call=call.to_dict(), context_ref=context_ref, package=package.to_dict()
            ),
        )
        return input_ref, package

    def register(self, role="scout", iteration=None, deadline=1000):
        call = self.reserve(role, iteration)
        ref, package = self.bind(call)
        registered = self.ledger.register_call(call, ref, ADAPTER, deadline)
        return call, registered["run"], package

    def test_live_endpoints_follow_dispatch_and_terminal_observation(self):
        from research_loop.runtime.knowledge import KnowledgeBase

        kb = KnowledgeBase(self.root)
        kb.live(["study"], role="scout")
        call, run, _ = self.register()
        self.assertIsNone(run["kb_start_version"])
        self.ledger.record_start(run["token"])
        self.assertEqual(self.ledger.get(run["token"])["kb_start_version"], kb.head())
        kb.commit(0, [], "# First", 0, "first-publication")
        observation = self.completion(run, result={"observation": "synthetic"})
        self.ledger.observe(run["token"], observation)
        self.assertEqual(self.ledger.get(run["token"])["kb_end_version"], 1)
        kb.commit(1, [], "# Second", 0, "second-publication")
        proposal = self.updater.validate_output(call, {"observation": "synthetic"}, [])
        receipt = self.updater.apply_update(proposal, proposal.revisions)
        recorded = self.ledger.get(run["token"])
        self.assertEqual(recorded["kb_end_version"], 1)
        self.assertEqual(recorded["kb_start_version"], 0)
        self.assertEqual(
            receipt.output_ref, self.owner.snapshot()["attempts"][-1]["output_ref"]
        )

    def test_explicit_knowledge_citation_is_accepted_without_a_read_receipt(self):
        from research_loop.runtime.knowledge import KnowledgeBase
        from research_loop.runtime.model_requests import format_decision_reference

        kb = KnowledgeBase(self.root)
        fixed = kb.publish_evidence("E1", "scout", {"observation": "synthetic"})
        citation = format_decision_reference(fixed)
        call = self.reserve()
        package = self.builder.build(call)
        context_ref = self.updater.bind_context(call, package)
        reference = publish_artifact(
            self.root,
            dict(
                call=call.to_dict(),
                context_ref=context_ref,
                package=package.to_dict(),
                knowledge_config={},
                assembly=dict(
                    role="scout",
                    knowledge_libraries=[
                        {"alias": "study", "library_id": kb.library_id}
                    ],
                    source_scope=[],
                ),
            ),
        )
        run = self.ledger.register_call(call, reference, ADAPTER, 1000)["run"]
        observation = self.completion(
            run, result={"observation": "cites"}, evidence_refs=[citation]
        )
        self.ledger.observe(run["token"], observation)
        proposal = self.updater.validate_output(
            call, {"observation": "cites"}, [citation]
        )
        receipt = self.updater.apply_update(proposal, proposal.revisions)
        self.assertEqual(receipt.to_dict()["evidence_refs"], [citation])
        self.assertEqual(
            proposal.to_dict()["evidence_identities"][citation],
            {"knowledge_reference": fixed},
        )
        with self.store._connection(readonly=True) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertFalse({"model_retrievals", "model_consulted"} & tables)
        foreign = dict(fixed, library_id="unauthorized")
        with self.assertRaises((PermissionError, ValueError)):
            self.updater._knowledge_evidence(
                self.owner._feedback_snapshot(call.to_dict())["binding"],
                format_decision_reference(foreign),
            )

    def completion(
        self,
        run,
        *,
        status="completed",
        result=None,
        sender_retired=True,
        evidence_refs=None,
    ):
        if run["category"] == "call" and status == "completed":
            result = {
                "output": result,
                "evidence_refs": [] if evidence_refs is None else evidence_refs,
            }
        ref = publish_artifact(
            self.root,
            dict(
                version=1,
                launch_token=run["token"],
                kind=run["kind"],
                input_ref=run["input_ref"],
                adapter=ADAPTER,
                status=status,
                result=result,
                error=None if status == "completed" else "synthetic retired fixture",
                sender_retired=sender_retired,
                exit_code=0 if status == "completed" else None,
            ),
        )
        return dict(
            status=status,
            completion_ref=ref,
            proof_ref=ref,
            worker_identity={"pid": 123, "started_at": 10},
        )

    def accept_result(self, call, result):
        proposal = self.updater.validate_output(call, result, [])
        return self.updater.apply_update(proposal, proposal.revisions)

    def manual(self, role, output, iteration):
        call = self.reserve(role, iteration)
        self.bind(call)
        return call, self.accept_result(call, output)

    def iteration(self, identity):
        return next(
            row for row in self.store.snapshot()["iterations"] if row["id"] == identity
        )

    def execution(self, requests=None):
        self.owner.create_task("task-" + str(self.serial), {})
        iteration = self.owner.claim_next()
        self.manual(
            "plan", {"timeout_seconds": 60, "local_evaluation": None}, iteration
        )
        call, receipt = self.manual(
            "execute",
            {
                "artifacts": [],
                "operations": [operation_fixtures.request()]
                if requests is None
                else requests,
            },
            self.iteration(iteration["id"]),
        )
        return call, receipt.to_dict().get("operation_ids", [])

    def action_input(self, operation_id, kind, *, token=None, submission_token=None):
        operations = Operations(self.owner)
        op = operations.get(operation_id)
        permit = None
        if kind == "submit":
            permit = {
                **operations._binding(op),
                "submission_token": submission_token,
                "owner_epoch": self.owner.owner_epoch,
                "deadline": op["deadline"],
                "operation_revision": op["revision"] + 1,
                "request_ref": op["request_ref"],
            }
        ref = publish_artifact(
            self.root,
            dict(
                operation_id=operation_id,
                action=kind,
                operation=op,
                request=operations.request(operation_id),
                permit=permit,
            ),
        )
        return self.ledger.register_action(
            operation_id,
            op["revision"],
            kind,
            ref,
            ADAPTER,
            5000,
            launch_token=token or self.ledger.action_token(),
            submission_token=submission_token,
        )

    def prepare_operation(self, operation_id):
        operations = Operations(self.owner)
        op = operations.get(operation_id)
        body = operations.envelope(
            operation_id,
            dict(
                ready=True, reason="Synthetic free preparation", effective_parameters={}
            ),
        )
        operations.record_receipt(
            operation_id, op["revision"], "prepare", "prepared-" + operation_id, body
        )

    def test_reserved_call_registration_is_idempotent_and_never_spends_again(self):
        call, run, package = self.register()
        before = self.store.snapshot()["counters"]["calls"]
        repeated = self.ledger.register_call(
            call, run["input_ref"], ADAPTER, run["deadline"]
        )
        self.assertFalse(repeated["created"])
        self.assertEqual(repeated["run"]["token"], "call-" + call.attempt_id)
        self.assertEqual(self.store.snapshot()["counters"]["calls"], before)
        changed = package.to_dict()
        changed["context"] = dict(reversed(list(changed["context"].items())))
        ref = publish_artifact(
            self.root,
            dict(call=call.to_dict(), context_ref=run["context_ref"], package=changed),
        )
        with self.assertRaises(ConflictError):
            self.ledger.register_call(call, ref, ADAPTER, run["deadline"])

    def test_unknown_sender_keeps_capacity_even_past_deadline_and_across_owner(self):
        call, run, _ = self.register()
        self.ledger.observe(
            run["token"],
            dict(
                status="unknown",
                completion_ref=None,
                proof_ref=None,
                worker_identity=None,
            ),
        )
        second = self.reserve()
        ref, _ = self.bind(second)
        with self.assertRaises(BudgetExceeded):
            self.ledger.register_call(second, ref, ADAPTER, 1000)
        self.now = 1100
        self.reopen()
        self.assertEqual(self.ledger.active_count(), 1)
        with self.assertRaises(InvalidTransition):
            self.ledger.grant_reconciliation(run["token"], "unknown")
        with self.assertRaises(OwnershipError):
            self.ledger.register_call(call, run["input_ref"], ADAPTER, 1000)
        self.assertTrue(
            any(
                event["kind"] == "recovery_needed"
                for event in self.events.pending("diagnostics")
            )
        )

    def test_terminal_claim_without_positive_sender_retirement_does_not_free_slot(self):
        _, run, _ = self.register()
        observation = self.completion(
            run, result={"answer": "bad"}, sender_retired=False
        )
        with self.assertRaises(ValueError):
            self.ledger.observe(run["token"], observation)
        self.assertEqual(self.ledger.active_count(), 1)
        with self.assertRaises(InvalidTransition):
            self.ledger.settle(run["token"], "failed")

    def test_recovery_event_is_stable_across_later_retirement_and_owner_reopen(self):
        _, run, _ = self.register()
        self.reopen()
        self.ledger.observe(
            run["token"], self.completion(run, result={"answer": "retired later"})
        )
        self.reopen()
        events = [
            event
            for event in self.events.pending("diagnostic")
            if event["kind"] == "recovery_needed" and event["subject"] == run["token"]
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(self.ledger.active_count(), 0)

    def test_old_completed_call_requires_exact_grant_and_real_updater_result(self):
        call, run, _ = self.register()
        original_epoch = call.owner_epoch
        unrelated = self.reserve()
        self.reopen()
        with self.assertRaises(OwnershipError):
            self.owner._feedback_snapshot(call.to_dict())
        observation = self.completion(run, result={"answer": "actual"})
        self.ledger.observe(run["token"], observation)
        grant = self.ledger.grant_reconciliation(run["token"], "reconcile-exact")
        self.assertEqual(grant["current_owner_epoch"], self.owner.owner_epoch)
        self.assertEqual(
            self.owner._feedback_snapshot(call.to_dict())["attempt"]["owner_epoch"],
            original_epoch,
        )
        with self.assertRaises(OwnershipError):
            self.owner._feedback_snapshot(unrelated.to_dict())
        with self.assertRaises(ConflictError):
            self.updater.validate_output(call, {"answer": "forged"}, [])
        self.accept_result(call, {"answer": "actual"})
        self.assertEqual(self.store.snapshot()["counters"]["calls"], 2)
        self.assertEqual(self.ledger.active_count(), 0)

    def test_substituted_completion_cannot_authorize_old_owner_failure(self):
        call, run, _ = self.register()
        self.reopen()
        observation = self.completion(run, status="failed")
        self.ledger.observe(run["token"], observation)
        self.ledger.grant_reconciliation(run["token"], "grant")
        path = self.root / observation["proof_ref"]
        path.chmod(0o644)
        path.write_text("replaced proof")
        path.chmod(0o444)
        with self.assertRaises(OwnershipError):
            self.owner.fail_attempt(
                call.attempt_id,
                call.worker_token,
                "stale proof",
                expected_revision=None,
            )
        self.assertEqual(self.store.snapshot()["attempts"][0]["status"], "active")

    def test_untracked_retirement_under_stop_fences_every_late_launch(self):
        call = self.reserve()
        self.owner.set_stop("no dispatch")
        ref = publish_artifact(self.root, {"retirement_only": call.attempt_id})
        registered = self.ledger.register_orphan_for_retirement(
            call.attempt_id, ref, ADAPTER, time.time() + 5
        )
        module = self.root / "fixture.py"
        module.write_text(
            "def invoke(kind,payload,config): return {'answer':'should never run'}\n"
        )
        invoker = LocalProcessInvoker(self.root, module, ADAPTER)
        run = registered["run"]
        observation = invoker.retire_unlaunched(
            run["token"], dict(kind="agent", input_ref=ref, deadline=run["deadline"])
        )
        self.ledger.observe(run["token"], observation)
        with patch(
            "research_loop.runtime.invocations.subprocess.Popen",
            side_effect=AssertionError("late launch"),
        ):
            self.assertEqual(
                invoker.launch(run["token"], "agent", ref, run["deadline"])["status"],
                "not_started",
            )
            other = publish_artifact(self.root, {"old_different_input": True})
            with self.assertRaises(ValueError):
                invoker.launch(run["token"], "agent", other, run["deadline"])
        self.assertEqual(self.ledger.active_count(), 0)
        self.assertEqual(self.store.snapshot()["counters"]["calls"], 1)
        with self.assertRaises(InvalidTransition):
            self.owner.accept_attempt(
                call.attempt_id,
                call.worker_token,
                publish_artifact(self.root, {}),
                expected_revision=None,
            )

    def test_planning_reservations_adopt_without_double_call_charge(self):
        self.owner.create_task("operator", {})
        planning = PlanningLedger(self.owner)
        round_record = planning.begin()
        calls = planning.calls(round_record["id"])
        before = self.store.snapshot()["counters"]["calls"]
        attempt = calls["advocate_a"]
        call = CallIdentity.from_attempt(self.store.snapshot()["study_id"], attempt)
        ref, _ = self.bind(call)
        self.ledger.register_call(call, ref, ADAPTER, 1000)
        self.assertEqual(self.store.snapshot()["counters"]["calls"], before)
        self.assertEqual(before, 2)

    def test_backend_submit_intent_and_action_are_atomic_and_unknown_is_not_absence(
        self,
    ):
        _, [opid] = self.execution()
        self.prepare_operation(opid)
        result = self.action_input(
            opid, "submit", submission_token="stable-submit-token"
        )
        run = result["run"]
        operation = Operations(self.owner).get(opid)
        self.assertEqual(run["operation_revision"], operation["revision"])
        self.assertEqual(operation["submission_token"], "stable-submit-token")
        self.assertEqual(operation["held_resources"], {"cpu": 1})
        self.ledger.observe(run["token"], self.completion(run, status="failed"))
        with self.assertRaises(InvalidTransition):
            self.action_input(opid, "inspection")
        self.ledger.settle(
            run["token"], "failed", error="sender retired, remote side effects unknown"
        )
        self.assertEqual(Operations(self.owner).get(opid)["state"], "submitting")
        with self.assertRaises(InvalidTransition):
            self.action_input(opid, "submit", submission_token="must-not-submit-again")
        self.owner.set_stop("cleanup only")
        self.now = 4000
        cleanup = self.action_input(opid, "inspection")
        self.assertTrue(cleanup["created"])
        self.assertEqual(self.store.snapshot()["counters"]["calls"], 2)

    def test_failed_action_registration_rolls_back_submit_holds_and_count(self):
        _, [opid] = self.execution()
        self.prepare_operation(opid)
        original = Operations._reserve_submit

        def failed(operations, *args, **kwargs):
            original(operations, *args, **kwargs)
            raise RuntimeError("atomic insert failure")

        with patch.object(Operations, "_reserve_submit", failed):
            with self.assertRaises(RuntimeError):
                self.action_input(opid, "submit", submission_token="rolled-back-token")
        self.assertIsNone(Operations(self.owner).get(opid)["submission_token"])
        self.assertEqual(Operations(self.owner).get(opid)["held_resources"], {})
        self.assertEqual(self.ledger.list("backend"), [])
        with self.store._connection(readonly=True) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT admitted_operations FROM operation_authorities WHERE id='a'"
                ).fetchone()[0],
                0,
            )

    def test_explicit_failed_review_retry_charges_once_and_preserves_frozen_input(self):
        execute, _ = self.execution([])
        call, run, package = self.register(
            "review", self.iteration(execute.iteration_id)
        )
        observation = self.completion(run, status="failed")
        self.ledger.observe(run["token"], observation)
        self.owner.fail_attempt(
            call.attempt_id,
            call.worker_token,
            "failed fixture Review",
            expected_revision=call.expected_revision,
        )
        failed = self.iteration(execute.iteration_id)
        before = self.store.snapshot()["counters"]["calls"]
        attempt = self.ledger.retry_phase(
            failed["id"],
            failed["revision"],
            "explicit user retry",
            observation["proof_ref"],
            "retry-1",
        )
        self.assertEqual(self.store.snapshot()["counters"]["calls"], before + 1)
        self.assertEqual(
            attempt,
            self.ledger.retry_phase(
                failed["id"],
                failed["revision"],
                "explicit user retry",
                observation["proof_ref"],
                "retry-1",
            ),
        )
        self.assertEqual(
            self.ledger.retry_context(attempt["id"]).context, package.context
        )
        retry_call = CallIdentity.from_attempt(call.study_id, attempt)
        source = self.owner._feedback_snapshot(retry_call.to_dict())["retry"]
        retry_package = self.builder.retry_review(
            retry_call, source["source_context_ref"], source["source_context_identity"]
        )
        self.assertEqual(retry_package.context, package.context)
        self.updater.bind_context(retry_call, retry_package)
        self.assertEqual(self.iteration(failed["id"])["phase"], "review")
        with self.assertRaises(InvalidTransition):
            self.ledger.retry_phase(
                failed["id"],
                self.iteration(failed["id"])["revision"],
                "again",
                observation["proof_ref"],
                "retry-2",
            )

    def test_unknown_failed_review_cannot_retry_or_reopen_execute(self):
        self.owner.create_task("plan-failure", {})
        iteration = self.owner.claim_next()
        call, run, _ = self.register("plan", iteration)
        with self.assertRaises(InvalidTransition):
            self.owner.fail_attempt(
                call.attempt_id,
                call.worker_token,
                "unproven death",
                expected_revision=1,
            )
        self.ledger.observe(run["token"], self.completion(run, status="failed"))
        self.owner.fail_attempt(
            call.attempt_id,
            call.worker_token,
            "actual failed plan",
            expected_revision=1,
        )
        with self.assertRaises(InvalidTransition):
            self.ledger.retry_phase(
                iteration["id"],
                2,
                "not execute retry",
                self.ledger.get(run["token"])["completion_ref"],
                "bad-retry",
            )

    def test_review_never_started_before_context_can_explicitly_retry_without_clone(
        self,
    ):
        execute, _ = self.execution([])
        call = self.reserve("review", self.iteration(execute.iteration_id))
        placeholder = publish_artifact(
            self.root, {"retire_unbound_review": call.attempt_id}
        )
        run = self.ledger.register_orphan_for_retirement(
            call.attempt_id, placeholder, ADAPTER, 1000
        )["run"]
        observation = self.completion(run, status="not_started")
        self.ledger.observe(run["token"], observation)
        self.owner.fail_attempt(
            call.attempt_id,
            call.worker_token,
            "crash before choosing input",
            expected_revision=call.expected_revision,
        )
        failed = self.iteration(execute.iteration_id)
        before = self.store.snapshot()["counters"]["calls"]
        attempt = self.ledger.retry_phase(
            failed["id"],
            failed["revision"],
            "explicit retry with no prior selected input",
            observation["proof_ref"],
            "unbound-review",
        )
        self.assertIsNone(self.ledger.retry_context(attempt["id"]))
        self.assertEqual(self.store.snapshot()["counters"]["calls"], before + 1)
        self.assertEqual(self.iteration(failed["id"])["phase"], "review")
        retry = CallIdentity.from_attempt(call.study_id, attempt)
        self.assertIsNone(
            self.owner._feedback_snapshot(retry.to_dict())["retry"][
                "source_context_ref"
            ]
        )

    def test_runtime_event_acknowledgement_is_durable_per_consumer(self):
        call = self.reserve()
        self.owner.fail_attempt(
            call.attempt_id,
            call.worker_token,
            "manual terminal",
            expected_revision=None,
        )
        self.owner.set_stop("operator")
        pending = self.events.pending("one")
        self.assertTrue(pending)
        self.events.ack("one", pending[0]["id"])
        self.events.ack("one", pending[0]["id"])
        self.assertNotIn(
            pending[0]["id"], [event["id"] for event in self.events.pending("one")]
        )
        self.assertIn(
            pending[0]["id"], [event["id"] for event in self.events.pending("two")]
        )
        self.reopen()
        self.assertNotIn(
            pending[0]["id"], [event["id"] for event in self.events.pending("one")]
        )
        with self.owner._transaction() as (conn, _):
            RuntimeEvents.emit(
                conn, "same-key", "fixture", "subject", payload={"version": 1}
            )
        with self.assertRaises(ConflictError):
            with self.owner._transaction() as (conn, _):
                RuntimeEvents.emit(
                    conn, "same-key", "fixture", "subject", payload={"version": 2}
                )

    def test_dispatch_body_reads_and_reconciliation_never_run_under_writer(self):
        call, run, _ = self.register()
        inside = [False]
        original = self.owner._transaction
        read = Path.read_text

        @contextmanager
        def tracked():
            with original() as transaction:
                inside[0] = True
                try:
                    yield transaction
                finally:
                    inside[0] = False

        def outside(path, *args, **kwargs):
            self.assertFalse(inside[0])
            return read(path, *args, **kwargs)

        observation = self.completion(run, result={"answer": "outside"})
        with (
            patch.object(self.owner, "_transaction", tracked),
            patch.object(Path, "read_text", outside),
        ):
            self.ledger.observe(run["token"], observation)
            self.ledger.grant_reconciliation(run["token"], "outside-proof")
            self.accept_result(call, {"answer": "outside"})
            self.ledger.settle(run["token"], "accepted")

    def test_actual_local_callback_exit_supplies_grant_without_another_invocation(self):
        call, run, _ = self.register(deadline=time.time() + 10)
        module = self.root / "local_fixture.py"
        module.write_text(
            "def invoke(kind,payload,config): return {'output':{'answer':'actual child'},'evidence_refs':[]}\n"
        )
        invoker = LocalProcessInvoker(self.root, module, ADAPTER)
        invoker.launch(run["token"], run["kind"], run["input_ref"], run["deadline"])
        limit = time.monotonic() + 8
        observation = invoker.inspect(run["token"])
        while (
            observation["status"]
            not in {"completed", "failed", "cancelled", "timed_out"}
            and time.monotonic() < limit
        ):
            time.sleep(0.02)
            observation = invoker.inspect(run["token"])
        self.assertEqual(observation["status"], "completed")
        self.reopen()
        self.ledger.observe(run["token"], observation)
        self.ledger.grant_reconciliation(run["token"], "actual-child")
        self.accept_result(call, {"answer": "actual child"})
        self.assertEqual(self.store.snapshot()["counters"]["calls"], 1)
        self.assertEqual(self.ledger.active_count(), 0)


if __name__ == "__main__":
    unittest.main()
