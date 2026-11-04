"""One bounded planning round with concurrent calls and serialized state updates.

Callbacks receive only their identity and detached context package. A callback
exception records a local failure; it is not proof that a remote job stopped.
Interrupted rounds require attention and are never automatically redispatched.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Mapping

from .planning_state import PlanningLedger
from .store import BudgetExceeded, InvalidTransition
from .task_pool import TaskPool
from .updates import CallIdentity, Updater


def _reason(error):
    return f"{type(error).__name__}: {error}"[:1000]


class PlanningRunner:
    def __init__(self, coordinator, invoke, validators, policy=None):
        if not callable(invoke):
            raise TypeError("invoke must be callable")
        if not isinstance(validators, Mapping) or any(
            not callable(validators.get(role)) for role in ("advocate", "judge")
        ):
            raise ValueError("Explicit Advocate and Judge validators are required")
        self.coordinator = coordinator
        self.invoke = invoke
        self.policy = policy
        self.ledger = PlanningLedger(coordinator)
        self.pool = TaskPool(coordinator)
        judge_validator = validators["judge"]

        def validate_judge(output, context):
            normalized = judge_validator(output, context)
            return {"changes": TaskPool._changes(normalized)}

        self.updater = Updater(
            coordinator, validators=dict(validators) | {"judge": validate_judge}
        )

    def _identity(self, attempt):
        return CallIdentity.from_attempt(
            self.coordinator.store.snapshot()["study_id"], attempt
        )

    def _active(self, call, round_id, slot):
        attempt = self.coordinator._feedback_snapshot(call.to_dict())["attempt"]
        if (
            attempt["status"] != "active"
            or call.owner_epoch != self.coordinator.owner_epoch
            or attempt["request_id"] != f"planning:{round_id}:{slot}"
        ):
            raise InvalidTransition(
                "Planning call is not an active reservation of this coordinator"
            )

    def _fail(self, call, error):
        attempt = self.coordinator._feedback_snapshot(call.to_dict())["attempt"]
        if attempt["status"] == "active":
            self.coordinator.fail_attempt(
                call.attempt_id,
                call.worker_token,
                _reason(error),
                expected_revision=None,
            )
        elif attempt["status"] != "failed":
            # An acceptance may already have committed before an interruption.
            # Preserve it for deterministic settlement; never label it failed.
            raise InvalidTransition(
                "Accepted planning feedback requires settlement"
            ) from error

    def _accept(self, call, result):
        if (
            not isinstance(result, dict)
            or set(result) != {"output", "evidence_refs"}
            or not isinstance(result["output"], dict)
            or not isinstance(result["evidence_refs"], list)
        ):
            raise ValueError("invoke must return exactly output and evidence_refs")
        proposal = self.updater.validate_output(
            call, result["output"], result["evidence_refs"]
        )
        self.updater.apply_update(proposal, proposal.revisions)

    def _failed(self, round_id, error):
        return self.ledger.finish(round_id, status="failed", error=_reason(error))

    def run_once(self):
        """Process one new round, or return None without calls when idle."""
        round_record = self.ledger.begin()
        if round_record is None:
            return None
        round_id = round_record["id"]
        attempts = self.ledger.calls(round_id)
        if set(attempts) != {"advocate_a", "advocate_b"}:
            raise InvalidTransition(
                "A new planning round requires exactly two Advocate reservations"
            )
        calls = {slot: self._identity(attempt) for slot, attempt in attempts.items()}
        packages = {}
        try:
            for slot, call in calls.items():
                self._active(call, round_id, slot)
                package = self.ledger.context(call, policy=self.policy)
                self.updater.bind_context(call, package)
                packages[slot] = package
        except Exception as error:
            for call in calls.values():
                self._fail(call, error)
            return self._failed(round_id, error)

        errors = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {}
            for slot, call in calls.items():
                self._active(call, round_id, slot)
                futures[executor.submit(self.invoke, call, packages[slot])] = call
            for future in as_completed(futures):
                call = futures[future]
                try:
                    self._accept(call, future.result())
                except Exception as error:
                    self._fail(call, error)
                    errors.append(error)
        terminal = self.ledger.calls(round_id)
        if not any(attempt["status"] == "accepted" for attempt in terminal.values()):
            return self._failed(
                round_id, errors[0] if errors else RuntimeError("Both Advocates failed")
            )
        if "judge" in terminal:
            raise InvalidTransition(
                "An existing Judge reservation must not be redispatched"
            )
        try:
            attempt = self.ledger.reserve_judge(round_id)
        except BudgetExceeded as error:
            return self._failed(round_id, error)
        call = self._identity(attempt)
        try:
            self._active(call, round_id, "judge")
            package = self.ledger.context(call, policy=self.policy)
            self.updater.bind_context(call, package)
            self._accept(call, self.invoke(call, package))
        except Exception as error:
            self._fail(call, error)
            return self._failed(round_id, error)
        receipt = self.pool.apply_judge(call)
        return self.ledger.finish(round_id, status="applied", receipt=receipt)

    def settle_accepted_judge(self, round_id):
        """Apply already accepted Judge feedback without invoking or reserving calls."""
        attempt = self.ledger.calls(round_id).get("judge")
        if attempt is None or attempt["status"] != "accepted":
            raise InvalidTransition("Settlement requires an accepted Judge")
        receipt = self.pool.apply_judge(self._identity(attempt))
        return self.ledger.finish(round_id, status="applied", receipt=receipt)
