"""Bounded owner-loop backend scheduling with real SQLite and local fixture IPC."""

from pathlib import Path
import tempfile
import time
import unittest

import test_runtime_dispatch as dispatch_fixtures
import test_runtime_operations as operation_fixtures
from research_loop.runtime.backend_scheduler import BackendScheduler
from research_loop.runtime.contexts import RuntimeContexts
from research_loop.runtime.dispatch import DispatchLedger
from research_loop.runtime.files import publish_artifact
from research_loop.runtime.invocations import LocalProcessInvoker
from research_loop.runtime.operations import Operations
from research_loop.runtime.store import StateStore
from research_loop.runtime.updates import Updater

ADAPTER = dispatch_fixtures.ADAPTER


class FixtureInvoker:
    """Synthetic trusted completion source; never creates a backend job/process."""

    identity = ADAPTER

    def __init__(self, root):
        self.root = root
        self.calls = []
        self.states = {}
        self.behavior = {}
        self.jobs = {}
        self.running_inspections = False

    def _observation(self, token, kind, input_ref, status, result=None, error=None):
        if status == "unknown":
            return dict(
                status="unknown",
                completion_ref=None,
                proof_ref=None,
                worker_identity=None,
            )
        ref = publish_artifact(
            self.root,
            dict(
                version=1,
                launch_token=token,
                kind=kind,
                input_ref=input_ref,
                adapter=ADAPTER,
                status=status,
                result=result,
                error=error,
                sender_retired=True,
                exit_code=0 if status == "completed" else None,
            ),
        )
        return dict(
            status=status, completion_ref=ref, proof_ref=ref, worker_identity=None
        )

    def launch(self, token, kind, input_ref, deadline):
        if token in self.states:
            return self.states[token]
        import json

        packet = json.loads((self.root / input_ref).read_text())
        action = packet["action"]
        operation = packet["operation"]
        self.calls.append(packet)
        choices = self.behavior.get(action, [])
        status = choices.pop(0) if choices else "completed"
        if action == "submit":
            self.jobs[operation["id"]] = "job-" + operation["logical_id"]
        if status != "completed":
            observation = self._observation(
                token,
                kind,
                input_ref,
                status,
                error=None if status == "unknown" else "synthetic callback failure",
            )
        else:
            if action == "prepare":
                data = dict(
                    ready=True,
                    reason="Synthetic free prepare",
                    effective_parameters=packet["request"]["parameters"],
                )
            elif action == "submit":
                data = dict(
                    outcome="accepted",
                    job_id=self.jobs[operation["id"]],
                    proof=None,
                    reason="Synthetic submit ACK",
                )
            elif action == "inspection":
                data = dict(
                    job_id=self.jobs.get(operation["id"], operation["job_id"]),
                    terminal=not self.running_inspections,
                    outcome=None if self.running_inspections else "succeeded",
                    physical_released=not self.running_inspections,
                    not_submitted_proof=None,
                )
            elif action == "cancellation":
                data = dict(
                    job_id=operation["job_id"],
                    acknowledged=True,
                    reason="ACK only, not release",
                )
            elif action == "collection":
                data = dict(
                    artifacts=[
                        dict(
                            path="actual/" + operation["logical_id"],
                            version="observed-v1",
                        )
                    ],
                    evaluations=[],
                    unavailable_reason=None,
                )
            else:
                data = dict(final_costs={unit: 0 for unit in operation["cost_bounds"]})
            observation = self._observation(
                token,
                kind,
                input_ref,
                status,
                result=dict(event_key="fixture:" + token, data=data),
            )
        self.states[token] = observation
        return observation

    def inspect(self, token):
        return self.states.get(
            token,
            dict(
                status="unknown",
                completion_ref=None,
                proof_ref=None,
                worker_identity=None,
            ),
        )

    def retire_unlaunched(self, token, binding):
        if token in self.states:
            return self.states[token]
        result = self._observation(
            token, binding["kind"], binding["input_ref"], "not_started"
        )
        self.states[token] = result
        return result


class BackendSchedulerTests(unittest.TestCase):
    reserve = dispatch_fixtures.RuntimeDispatchTests.reserve
    bind = dispatch_fixtures.RuntimeDispatchTests.bind
    accept_result = dispatch_fixtures.RuntimeDispatchTests.accept_result
    manual = dispatch_fixtures.RuntimeDispatchTests.manual
    iteration = dispatch_fixtures.RuntimeDispatchTests.iteration

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = 10
        self.serial = 0
        self.config = operation_fixtures.configuration()
        self.config["resource_pools"] = {"cpu": 1, "gpu": 1}
        for grant in self.config["operation_authorizations"].values():
            grant["resource_limits"] = {"cpu": 1, "gpu": 1}
            grant["deadline"] = None
        self.settings = dict(
            worker_slots=2,
            worker_adapter=ADAPTER,
            action_timeout_seconds=5,
            inspection_interval_seconds=10,
            action_retries=1,
        )
        self.config["runtime"] = self.settings
        self.store = StateStore.create(
            self.root, config=self.config, clock=lambda: self.now
        )
        self.context = self.store.coordinator()
        self.owner = self.context.__enter__()
        self.addCleanup(lambda: self.context.__exit__(None, None, None))
        self.invoker = FixtureInvoker(self.root)
        self.components()

    def components(self):
        self.ledger = DispatchLedger(self.owner)
        self.operations = Operations(self.owner)
        self.updater = Updater(
            self.owner,
            validators={
                role: lambda output, _: output for role in ("plan", "execute", "review")
            },
        )
        self.builder = RuntimeContexts(self.owner)
        self.scheduler = BackendScheduler(
            self.owner, self.ledger, self.invoker, self.settings
        )

    def execute(self, name="one", *, source="human", priority=100, pool="cpu"):
        self.owner.create_task(name, {}, source=source, priority=priority)
        iteration = self.owner.claim_task(name)
        self.manual("plan", dict(timeout_seconds=60, local_evaluation=None), iteration)
        call, receipt = self.manual(
            "execute",
            dict(
                artifacts=[],
                operations=[operation_fixtures.request(name, resources={pool: 1})],
            ),
            self.iteration(iteration["id"]),
        )
        return call, receipt.to_dict()["operation_ids"][0]

    def drain(self):
        for run in self.ledger.unsettled("backend"):
            if run["worker_retired"]:
                self.scheduler.settle(run)

    def pass_once(self):
        result = self.scheduler.schedule(self.now)
        self.drain()
        return result

    def submitted(self, name="one"):
        call, opid = self.execute(name)
        self.pass_once()
        self.pass_once()
        return call, opid

    def terminal(self, name="one"):
        call, opid = self.submitted(name)
        self.now += 10
        self.pass_once()
        return call, opid

    def count(self, action):
        return len(
            [packet for packet in self.invoker.calls if packet["action"] == action]
        )

    def test_human_source_gets_last_gpu_and_independent_cpu_continues(self):
        _, auto_gpu = self.execute(
            "auto-gpu", source="autonomous", priority=-1000, pool="gpu"
        )
        _, auto_cpu = self.execute(
            "auto-cpu", source="autonomous", priority=-999, pool="cpu"
        )
        _, human_gpu = self.execute("human-gpu", priority=999, pool="gpu")
        calls = self.store.snapshot()["counters"]["calls"]
        self.pass_once()
        result = self.pass_once()
        self.assertIsNotNone(self.operations.get(human_gpu)["submission_token"])
        self.assertIsNotNone(self.operations.get(auto_cpu)["submission_token"])
        self.assertIsNone(self.operations.get(auto_gpu)["submission_token"])
        self.assertTrue(
            any(item["operation_id"] == auto_gpu for item in result["blocked"])
        )
        self.assertEqual(self.count("submit"), 2)
        self.assertEqual(self.store.snapshot()["counters"]["calls"], calls)

    def test_lost_submit_ack_inspects_original_job_and_never_resubmits(self):
        self.invoker.behavior["submit"] = ["failed"]
        _, opid = self.submitted()
        self.assertEqual(self.operations.get(opid)["state"], "unknown")
        self.assertIsNotNone(self.operations.get(opid)["submission_token"])
        result = self.pass_once()
        self.assertEqual(result["next_wake"], 20)
        self.now = 20
        self.pass_once()
        self.assertEqual(self.operations.get(opid)["state"], "terminal")
        self.assertEqual(self.count("submit"), 1)
        self.assertEqual(
            self.invoker.calls[-1]["operation"]["submission_token"],
            self.operations.get(opid)["submission_token"],
        )

    def test_failed_collection_retries_original_job_after_durable_interval(self):
        _, opid = self.terminal()
        self.invoker.behavior["collection"] = ["failed", "completed"]
        self.pass_once()
        self.assertEqual(self.operations.get(opid)["collection_state"], "pending")
        self.pass_once()  # Billing can progress while failed collection waits.
        self.assertEqual(self.count("collection"), 1)
        self.now = 25
        self.context.__exit__(None, None, None)
        self.context = self.store.coordinator()
        self.owner = self.context.__enter__()
        self.components()
        result = self.pass_once()
        self.assertEqual(self.count("collection"), 1)
        self.assertEqual(result["next_wake"], 30)
        self.now = 30
        self.pass_once()
        self.assertEqual(self.operations.get(opid)["collection_state"], "collected")
        jobs = [
            packet["operation"]["job_id"]
            for packet in self.invoker.calls
            if packet["action"] == "collection"
        ]
        self.assertEqual(len(set(jobs)), 1)
        self.assertEqual(self.count("submit"), 1)

    def test_exhausted_collection_stays_pending_until_explicit_bounded_retry(self):
        _, opid = self.terminal()
        self.invoker.behavior["collection"] = ["failed", "failed", "completed"]
        self.pass_once()
        self.now = 30
        self.pass_once()
        result = self.pass_once()
        self.assertTrue(
            any(
                item["reason"] == "collection:retry_exhausted"
                for item in result["blocked"]
            )
        )
        self.assertEqual(self.operations.get(opid)["collection_state"], "pending")
        before = len(self.invoker.calls)
        self.now = 100
        self.pass_once()
        self.assertEqual(len(self.invoker.calls), before)
        event = self.scheduler.retry_action(
            opid,
            "collection",
            "operator approved another bounded probe",
            "retry-collect",
        )
        self.assertEqual(
            self.scheduler.retry_action(
                opid,
                "collection",
                "operator approved another bounded probe",
                "retry-collect",
            ),
            event,
        )
        self.pass_once()
        self.assertEqual(self.operations.get(opid)["collection_state"], "collected")
        self.assertFalse(
            any(row["id"] == event["id"] for row in self.scheduler._requests())
        )
        self.assertEqual(
            self.scheduler.retry_action(
                opid,
                "collection",
                "operator approved another bounded probe",
                "retry-collect",
            ),
            event,
        )
        with self.assertRaises(ValueError):
            self.scheduler.retry_action(opid, "submit", "never", "retry-submit")
        self.assertEqual(self.count("submit"), 1)

    def test_successful_running_inspections_are_periodic_not_failed_retry_budget(self):
        _, opid = self.submitted()
        self.invoker.running_inspections = True
        for now in (20, 30, 40, 50):
            self.now = now
            self.pass_once()
        self.assertEqual(self.count("inspection"), 4)
        self.assertEqual(self.operations.get(opid)["state"], "submitted")
        self.assertFalse(
            any(
                "retry_exhausted" in item["reason"]
                for item in self.pass_once()["blocked"]
            )
        )
        self.assertEqual(self.store.snapshot()["counters"]["calls"], 2)

    def test_unknown_worker_prevents_overlap_without_freeing_capacity_at_timeout(self):
        self.execute()
        self.invoker.behavior["prepare"] = ["unknown"]
        self.pass_once()
        self.now = 100
        result = self.pass_once()
        self.assertEqual(self.count("prepare"), 1)
        self.assertTrue(
            any(item["reason"] == "unknown_worker" for item in result["blocked"])
        )
        self.assertEqual(self.ledger.active_count(), 1)

    def test_stop_allows_explicit_cancel_cleanup_but_never_starts_pending_compute(self):
        _, opid = self.submitted("active")
        _, pending = self.execute("unsubmitted", pool="gpu")
        event = self.scheduler.request_cancel(opid, "operator cancel", "cancel-1")
        self.assertEqual(self.count("cancellation"), 0)
        self.owner.set_stop("stop new work")
        self.pass_once()
        self.assertEqual(self.count("cancellation"), 1)
        self.assertEqual(self.count("submit"), 1)
        self.assertEqual(self.count("prepare"), 1)
        self.assertEqual(self.operations.get(opid)["held_resources"], {"cpu": 1})
        self.assertEqual(
            self.operations.get(pending)["terminal_outcome"], "not_submitted"
        )
        self.assertEqual(
            self.operations.get(pending)["collection_state"], "unavailable"
        )
        self.assertFalse(
            any(row["id"] == event["id"] for row in self.scheduler._requests())
        )

    def test_cancel_request_failed_retries_are_bounded_and_acknowledged_on_exhaustion(
        self,
    ):
        _, opid = self.submitted()
        self.invoker.behavior["cancellation"] = ["failed", "failed"]
        event = self.scheduler.request_cancel(
            opid, "cancel requested", "cancel-bounded"
        )
        self.pass_once()
        self.now = 20
        self.pass_once()
        self.assertEqual(self.count("cancellation"), 2)
        self.assertFalse(
            any(row["id"] == event["id"] for row in self.scheduler._requests())
        )
        self.assertEqual(self.operations.get(opid)["held_resources"], {"cpu": 1})

    def test_submit_not_started_is_positive_absence_but_after_intent_fees_stay_unknown(
        self,
    ):
        self.execute()
        self.pass_once()
        self.invoker.behavior["submit"] = ["not_started"]
        self.pass_once()
        op = self.operations.list()[0]
        self.assertEqual(op["terminal_outcome"], "not_submitted")
        self.assertFalse(op["costs_final"])
        self.assertEqual(op["held_resources"], {})
        self.assertEqual(op["held_costs"], {"ticks": 4, "fee": 0})
        self.pass_once()
        self.assertEqual(
            self.operations.get(op["id"])["collection_state"], "unavailable"
        )

    def test_actual_local_prepare_callback_exits_before_owner_settlement(self):
        self.context.__exit__(None, None, None)
        self.root = self.root / "actual-local"
        self.now = time.time()
        self.store = StateStore.create(
            self.root, config=self.config, clock=lambda: self.now
        )
        self.context = self.store.coordinator()
        self.owner = self.context.__enter__()
        module = self.root / "fixture.py"
        module.write_text(
            "def invoke(kind,payload,config): return {'event_key':'actual-free-prepare','data':{'ready':True,'reason':'actual local fixture','effective_parameters':payload['request']['parameters']}}\n"
        )
        self.invoker = LocalProcessInvoker(self.root, module, ADAPTER)
        self.components()
        _, opid = self.execute()
        started = time.monotonic()
        self.scheduler.schedule(self.now)
        self.assertLess(time.monotonic() - started, 1)
        run = self.ledger.unsettled("backend")[0]
        until = time.monotonic() + 5
        while (
            not self.ledger.get(run["token"])["worker_retired"]
            and time.monotonic() < until
        ):
            self.ledger.observe(run["token"], self.invoker.inspect(run["token"]))
            time.sleep(0.02)
        self.scheduler.settle(self.ledger.get(run["token"]))
        self.assertEqual(self.operations.get(opid)["state"], "prepared")
        self.assertIsNone(self.operations.get(opid)["submission_token"])


if __name__ == "__main__":
    unittest.main()
