"""Finite owner-thread scheduling over durable local invocation journals.

Workers receive detached JSON and publish files. This loop alone accepts their
results; callback capacity and external resource holds are independent limits.
"""

import math

from .backend_scheduler import BackendScheduler
from .contexts import RuntimeContexts
from .dispatch import DispatchLedger, RuntimeEvents
from .files import publish_artifact
from .measurements import MeasurementRegistry
from .operations import Operations
from .planning_state import PlanningLedger
from .projections import ProjectionWriter
from .publication import Publisher
from .store import BudgetExceeded, ConflictError, InvalidTransition, _decode, _text
from .task_pool import TaskPool
from .updates import CallIdentity, Updater


def _reason(error):
    return f"{type(error).__name__}: {error}"[:1000]


def validate_settings(settings):
    """Validate frozen scheduling settings before creating a CLI Study."""
    if not isinstance(settings, dict):
        raise ValueError("runtime settings must be an object")
    slots = settings.get("worker_slots")
    if type(slots) is not int or slots < 1:
        raise ValueError("runtime.worker_slots must be positive")
    adapter = settings.get("worker_adapter")
    if not isinstance(adapter, dict) or set(adapter) != {"id", "version"}:
        raise ValueError("runtime.worker_adapter requires id and version")
    for value in adapter.values():
        _text(value, "worker adapter binding")
    for key in (
        "call_timeout_seconds",
        "action_timeout_seconds",
        "inspection_interval_seconds",
    ):
        value = settings.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"runtime.{key} must be finite and positive")
    if (
        type(settings.get("action_retries")) is not int
        or settings["action_retries"] < 0
    ):
        raise ValueError("runtime.action_retries must be a nonnegative integer")
    if type(settings.get("planning")) is not bool:
        raise ValueError("runtime.planning must be explicit")
    if settings["planning"] and slots < 2:
        raise ValueError("Parallel planning needs at least two worker slots")


class ConcurrentCoordinator:
    """One host owner, explicit validators, one configured durable invoker."""

    def __init__(self, owner, invoker, validators, *, policy=None):
        self.owner, self.invoker = owner, invoker
        self.ledger = DispatchLedger(owner)
        self.settings = owner.snapshot()["config"]["runtime"]
        validate_settings(self.settings)
        if invoker.identity != self.ledger.adapter:
            raise ConflictError("Invoker differs from the frozen worker adapter")
        validators = dict(validators)
        required = {"plan", "execute", "review"}
        if self.settings["planning"]:
            required |= {"advocate", "judge"}
        if any(not callable(validators.get(role)) for role in required):
            raise ValueError("Every enabled role requires an explicit validator")
        if "judge" in validators:
            judge_validator = validators["judge"]

            def judge(output, context):
                return {"changes": TaskPool._changes(judge_validator(output, context))}

            validators["judge"] = judge
        self.updater = Updater(owner, validators=validators)
        self.contexts = RuntimeContexts(owner, policy=policy)
        self.operations = Operations(owner)
        self.planning = PlanningLedger(owner)
        self.publisher = Publisher(owner)
        self.backend = BackendScheduler(owner, self.ledger, invoker, self.settings)
        self._projection_version = None

    def _clock(self):
        with self.owner._transaction() as (_, control):
            return control["anchor"]

    def _call(self, attempt):
        return CallIdentity.from_attempt(self.owner.snapshot()["study_id"], attempt)

    def _attention(self, subject, error):
        key = f"runtime-attention:{subject}:{type(error).__name__}"
        with self.owner._transaction() as (conn, _):
            if conn.execute(
                "SELECT 1 FROM runtime_events WHERE producer_key=?", (key,)
            ).fetchone():
                return False
            RuntimeEvents.emit(
                conn,
                key,
                "needs_attention",
                subject,
                payload={"reason": _reason(error)},
            )
        return True

    def request_scout(self, request_id):
        """Reserve one explicit Scout request; idle ticks never manufacture one."""
        _text(request_id, "Scout request ID")
        if "scout" not in self.updater.validators:
            raise ValueError("Scout requires its own explicit validator")
        return self.owner.begin_attempt(
            "scout", request_id="runtime:scout:" + request_id
        )

    def stop(self, reason, *, cancel_running=False):
        self.owner.set_stop(reason)
        if cancel_running:
            for operation in self.operations.list():
                if operation["external_open"]:
                    self.backend.request_cancel(
                        operation["id"], reason, "stop:" + operation["id"]
                    )

    def _launch(self, registration):
        run = registration["run"]
        if not registration["created"]:
            return False
        # STOP can arrive after registration but before the actual launch. The
        # invoker's tombstone fences even a delayed original sender.
        stopped = self.owner.snapshot()["stop"]["requested"]
        if stopped and (
            run["category"] == "call" or run["action_kind"] in {"prepare", "submit"}
        ):
            observation = self.invoker.retire_unlaunched(
                run["token"],
                {key: run[key] for key in ("kind", "input_ref", "deadline")},
            )
        else:
            observation = self.invoker.launch(
                run["token"], run["kind"], run["input_ref"], run["deadline"]
            )
        self.ledger.observe(run["token"], observation)
        return True

    def _retire_orphan(self, attempt, now):
        call = self._call(attempt)
        with self.owner.store._connection(readonly=True) as conn:
            binding = _decode(
                conn.execute(
                    "SELECT * FROM contexts WHERE attempt_id=?", (attempt["id"],)
                ).fetchone()
            )
        package = (
            self.updater._read_record(binding["context_ref"], binding["identity"])
            if binding
            else None
        )
        reference = publish_artifact(
            self.owner.store.root,
            {
                "call": call.to_dict(),
                "context_ref": binding["context_ref"] if binding else None,
                "package": package,
            },
        )
        registration = self.ledger.register_orphan_for_retirement(
            attempt["id"], reference, self.invoker.identity, now
        )
        run = registration["run"]
        observation = self.invoker.retire_unlaunched(
            run["token"], {key: run[key] for key in ("kind", "input_ref", "deadline")}
        )
        self.ledger.observe(run["token"], observation)
        return True

    def _observe(self, now, blocked):
        progress = 0
        tracked = {run["attempt_id"] for run in self.ledger.list("call")}
        state = self.owner.snapshot()
        for attempt in state["attempts"]:
            if (
                attempt["status"] == "active"
                and attempt["id"] not in tracked
                and not attempt["request_id"].startswith(
                    ("runtime:", "planning:", "retry:")
                )
            ):
                blocked.append(
                    {
                        "attempt_id": attempt["id"],
                        "reason": "Unmanaged active attempt requires explicit reconciliation",
                    }
                )
            if (
                attempt["status"] == "active"
                and attempt["id"] not in tracked
                and attempt["request_id"].startswith(
                    ("runtime:", "planning:", "retry:")
                )
                and (
                    attempt["owner_epoch"] != self.owner.owner_epoch
                    or state["stop"]["requested"]
                )
            ):
                try:
                    progress += self._retire_orphan(attempt, now)
                except Exception as error:
                    self._attention(attempt["id"], error)
                    blocked.append(
                        {"attempt_id": attempt["id"], "reason": _reason(error)}
                    )
        for run in self.ledger.unsettled():
            if run["worker_retired"]:
                continue
            try:
                observation = self.invoker.inspect(run["token"])
                if observation["status"] == "unknown" and (
                    run["owner_epoch"] != self.owner.owner_epoch
                    or run["state"] == "retiring"
                    or state["stop"]["requested"]
                ):
                    observation = self.invoker.retire_unlaunched(
                        run["token"],
                        {key: run[key] for key in ("kind", "input_ref", "deadline")},
                    )
                if (
                    observation["status"] != run["state"]
                    or observation["worker_identity"] != run["worker_identity"]
                ):
                    self.ledger.observe(run["token"], observation)
                    progress += 1
                if observation["status"] == "unknown":
                    blocked.append(
                        {
                            "token": run["token"],
                            "reason": "Worker identity is unresolved; no redispatch",
                        }
                    )
            except Exception as error:
                self._attention(run["token"], error)
                blocked.append({"token": run["token"], "reason": _reason(error)})
        return progress

    def _settle_call(self, run):
        attempt = next(
            row
            for row in self.owner.snapshot()["attempts"]
            if row["id"] == run["attempt_id"]
        )
        call = self._call(attempt)
        if (
            attempt["status"] == "active"
            and attempt["owner_epoch"] != self.owner.owner_epoch
        ):
            self.ledger.grant_reconciliation(
                run["token"], f"recover:{self.owner.owner_epoch}:{run['token']}"
            )
        if attempt["status"] == "accepted":
            self.ledger.settle(
                run["token"], "accepted", receipt_ref=attempt["output_ref"]
            )
            return
        if attempt["status"] == "failed":
            self.ledger.settle(run["token"], "failed", error=attempt["failure_reason"])
            return
        try:
            if run["status"] != "completed":
                raise RuntimeError(run["error"] or f"Invocation ended: {run['status']}")
            result = self.ledger.result(run["token"])
            if not isinstance(result, dict) or set(result) != {
                "output",
                "evidence_refs",
            }:
                raise ValueError(
                    "Agent result requires exactly output and evidence_refs"
                )
            proposal = self.updater.validate_output(
                call, result["output"], result["evidence_refs"]
            )
            if call.role in {"review", "scout"}:
                # Reject malformed scientific publication before acceptance, so
                # an invalid Review remains explicitly retryable as Review.
                self.publisher._prepare(
                    self.owner._feedback_snapshot(call.to_dict()), proposal.to_dict()
                )
            receipt = self.updater.apply_update(proposal, proposal.revisions)
        except Exception as error:
            failed = self.owner.fail_attempt(
                call.attempt_id,
                call.worker_token,
                _reason(error),
                expected_revision=call.expected_revision,
            )
            self.ledger.settle(run["token"], "failed", error=failed["failure_reason"])
            return
        self.ledger.settle(run["token"], "accepted", receipt_ref=receipt.output_ref)

    def _settle(self, blocked):
        progress = 0
        for run in self.ledger.unsettled():
            if not run["worker_retired"]:
                continue
            try:
                if run["category"] == "call":
                    self._settle_call(run)
                else:
                    self.backend.settle(run)
                progress += 1
            except Exception as error:
                self._attention(run["token"], error)
                blocked.append({"token": run["token"], "reason": _reason(error)})
        return progress

    def _feedback(self, blocked):
        progress = 0
        with self.owner.store._connection(readonly=True) as conn:
            pending = [
                dict(row)
                for row in conn.execute(
                    "SELECT e.receipt_ref,o.iteration_id FROM evaluation_intake e JOIN operation_groups o ON o.execute_attempt_id=(SELECT execute_attempt_id FROM operations WHERE id=e.operation_id) WHERE e.disposition='pending'"
                )
            ]
            groups = [
                row[0]
                for row in conn.execute(
                    "SELECT execute_attempt_id FROM operation_groups WHERE feedback_sealed=0"
                )
            ]
        registry = MeasurementRegistry(self.owner)
        for item in pending:
            try:
                try:
                    registry.register(item["iteration_id"], item["receipt_ref"])
                except (ValueError, ConflictError) as error:
                    self.operations.reject_evaluation(
                        item["receipt_ref"], _reason(error)
                    )
                progress += 1
            except Exception as error:
                self._attention(item["receipt_ref"], error)
                blocked.append(
                    {"receipt_ref": item["receipt_ref"], "reason": _reason(error)}
                )
        for identity in groups:
            try:
                self.operations.finalize_feedback(identity)
                progress += 1
            except InvalidTransition:
                pass
        return progress

    def _publish(self, blocked):
        progress = 0
        with self.owner.store._connection(readonly=True) as conn:
            attempts = [
                _decode(row)
                for row in conn.execute(
                    "SELECT a.* FROM attempts a LEFT JOIN publications p ON p.attempt_id=a.id WHERE a.status='accepted' AND a.role IN ('review','scout') AND p.id IS NULL"
                )
            ]
        for attempt in attempts:
            try:
                self.publisher.publish(self._call(attempt))
                progress += 1
            except Exception as error:
                self._attention(attempt["id"], error)
                blocked.append({"attempt_id": attempt["id"], "reason": _reason(error)})
        state = self.owner.snapshot()
        version = state["knowledge_version"]
        if self._projection_version != version and isinstance(
            state["config"].get("global_evaluation"), dict
        ):
            try:
                ProjectionWriter(self.owner).rebuild()
                self._projection_version = version
                progress += 1
            except Exception as error:
                self._attention(f"views:{version}", error)
                blocked.append(
                    {"publication_version": version, "reason": _reason(error)}
                )
        return progress

    def _planning_progress(self, blocked):
        progress = 0
        with self.owner.store._connection(readonly=True) as conn:
            rounds = [
                _decode(row)
                for row in conn.execute(
                    "SELECT * FROM planning_rounds WHERE status='active'"
                )
            ]
        for record in rounds:
            identity = record["id"]
            calls = self.planning.calls(identity)
            if any(call["status"] == "active" for call in calls.values()):
                continue
            try:
                if record["owner_epoch"] != self.owner.owner_epoch:
                    self.ledger.authorize_planning(
                        identity,
                        f"planning-recover:{self.owner.owner_epoch}:{identity}",
                    )
                judge = calls.get("judge")
                if judge and judge["status"] == "accepted":
                    receipt = TaskPool(self.owner).apply_judge(self._call(judge))
                    self.planning.finish(identity, "applied", receipt=receipt)
                elif judge or not any(
                    call["status"] == "accepted" for call in calls.values()
                ):
                    self.planning.finish(
                        identity,
                        "failed",
                        error="Planning callbacks did not produce an accepted Judge",
                    )
                elif self.ledger.active_count() < self.ledger.max_slots:
                    try:
                        self.planning.reserve_judge(identity)
                    except BudgetExceeded as error:
                        self.planning.finish(identity, "failed", error=_reason(error))
                else:
                    continue
                progress += 1
            except Exception as error:
                self._attention(identity, error)
                blocked.append({"round_id": identity, "reason": _reason(error)})
        return progress

    def _dispatch_call(self, attempt, now, blocked=None):
        call = self._call(attempt)
        try:
            package = self.contexts.build(call)
            context_ref = self.updater.bind_context(call, package)
            reference = publish_artifact(
                self.owner.store.root,
                {
                    "call": call.to_dict(),
                    "context_ref": context_ref,
                    "package": package.to_dict(),
                },
            )
            registration = self.ledger.register_call(
                call,
                reference,
                self.invoker.identity,
                now + self.settings["call_timeout_seconds"],
            )
            return self._launch(registration)
        except BudgetExceeded as error:
            if blocked is not None:
                blocked.append(
                    {"attempt_id": call.attempt_id, "reason": _reason(error)}
                )
            retired = False
            if not any(
                run["attempt_id"] == call.attempt_id for run in self.ledger.list("call")
            ):
                retired = self._retire_orphan(attempt, now)
            self._attention(call.attempt_id, error)
            return retired
        except Exception as error:
            # If launch already has an intent, its sender may still act. Only an
            # untracked reservation can be deterministically retired here.
            retired = False
            if not any(
                run["attempt_id"] == call.attempt_id for run in self.ledger.list("call")
            ):
                retired = self._retire_orphan(attempt, now)
            self._attention(call.attempt_id, error)
            if blocked is not None:
                blocked.append(
                    {"attempt_id": call.attempt_id, "reason": _reason(error)}
                )
            return retired

    def _schedule_calls(self, now, blocked):
        progress = 0
        state = self.owner.snapshot()
        if state["stop"]["requested"]:
            return progress
        with self.owner.store._connection(readonly=True) as conn:
            planning_active = conn.execute(
                "SELECT 1 FROM planning_rounds WHERE status='active'"
            ).fetchone()
        # Let existing callbacks finish, then retain room for the two Advocates.
        # Otherwise autonomous continuations can fill both slots indefinitely.
        reserve_planning = (
            self.settings["planning"]
            and planning_active is None
            and bool(self.planning.pending())
            and (
                state["limits"]["max_calls"] is None
                or state["limits"]["max_calls"] - state["counters"]["calls"] >= 2
            )
        )
        tasks = {row["id"]: row for row in state["tasks"]}
        iterations = {row["id"]: row for row in state["iterations"]}
        tracked = {run["attempt_id"] for run in self.ledger.list("call")}

        def order(iteration_id):
            if iteration_id is None:
                return (1, 0, "")
            task = tasks[iterations[iteration_id]["task_id"]]
            return (0 if task["source"] == "human" else 2, task["priority"], task["id"])

        pending = [
            a
            for a in state["attempts"]
            if a["status"] == "active"
            and a["id"] not in tracked
            and a["owner_epoch"] == self.owner.owner_epoch
            and a["request_id"].startswith(("runtime:", "planning:", "retry:"))
        ]
        for attempt in sorted(pending, key=lambda row: order(row["iteration_id"])):
            if reserve_planning and order(attempt["iteration_id"])[0] != 0:
                continue
            if self.ledger.active_count() >= self.ledger.max_slots:
                return progress
            progress += self._dispatch_call(attempt, now, blocked)
        for iteration in sorted(iterations.values(), key=lambda row: order(row["id"])):
            if reserve_planning and order(iteration["id"])[0] != 0:
                continue
            if self.ledger.active_count() >= self.ledger.max_slots:
                return progress
            if iteration["status"] != "active" or iteration["phase"] not in {
                "plan",
                "execute",
                "review",
            }:
                continue
            if any(
                a["iteration_id"] == iteration["id"] and a["status"] == "active"
                for a in self.owner.snapshot()["attempts"]
            ):
                continue
            try:
                attempt = self.owner.begin_attempt(
                    iteration["phase"],
                    request_id=f"runtime:{iteration['id']}:{iteration['phase']}:{iteration['revision']}",
                    iteration_id=iteration["id"],
                    expected_revision=iteration["revision"],
                )
                progress += self._dispatch_call(attempt, now, blocked)
            except (BudgetExceeded, InvalidTransition) as error:
                blocked.append(
                    {"iteration_id": iteration["id"], "reason": _reason(error)}
                )
        while self.ledger.active_count() < self.ledger.max_slots:
            if reserve_planning:
                with self.owner.store._connection(readonly=True) as conn:
                    ready = self.owner._ready_tasks(conn)
                if not ready or ready[0]["source"] != "human":
                    break
            state = self.owner.snapshot()
            if (
                state["limits"]["max_calls"] is not None
                and state["counters"]["calls"] >= state["limits"]["max_calls"]
            ):
                break
            try:
                iteration = self.owner.claim_next()
                if iteration is None:
                    break
                attempt = self.owner.begin_attempt(
                    "plan",
                    request_id=f"runtime:{iteration['id']}:plan:1",
                    iteration_id=iteration["id"],
                    expected_revision=iteration["revision"],
                )
                progress += self._dispatch_call(attempt, now, blocked)
            except BudgetExceeded as error:
                blocked.append({"reason": _reason(error)})
                break
        return progress

    def tick(self):
        """Make finite progress and return waiting/blocked identities; never sleep."""
        now, blocked = self._clock(), []
        progress = self._observe(now, blocked)
        progress += self._settle(blocked)
        progress += self._feedback(blocked)
        progress += self._publish(blocked)
        progress += self._planning_progress(blocked)
        backend = self.backend.schedule(now)
        progress += backend["progress"]
        blocked.extend(backend["blocked"])
        progress += self._schedule_calls(now, blocked)
        if (
            self.settings["planning"]
            and self.ledger.max_slots - self.ledger.active_count() >= 2
        ):
            with self.owner.store._connection(readonly=True) as conn:
                active = conn.execute(
                    "SELECT 1 FROM planning_rounds WHERE status='active'"
                ).fetchone()
            if active is None and self.planning.pending():
                try:
                    if self.planning.begin() is not None:
                        progress += 1
                        progress += self._schedule_calls(now, blocked)
                except BudgetExceeded as error:
                    blocked.append({"planning": True, "reason": _reason(error)})
        state = self.owner.snapshot()
        waiting = [
            run["token"] for run in self.ledger.list() if not run["worker_retired"]
        ]
        return {
            "progress": progress,
            "waiting": waiting,
            "blocked": blocked,
            "next_wake": backend["next_wake"],
            "stop": state["stop"],
            "iterations": [
                {key: row[key] for key in ("id", "task_id", "phase", "status")}
                for row in state["iterations"]
            ],
        }

    def run_until_idle(self, *, max_ticks=100):
        """Stop at an external wait; idle is not a claim of Study completion."""
        if type(max_ticks) is not int or max_ticks < 1:
            raise ValueError("max_ticks must be positive")
        for _ in range(max_ticks):
            result = self.tick()
            if not result["progress"]:
                return result
        return result | {"tick_limit_reached": True}
