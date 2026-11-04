"""Trusted backend callbacks outside SQLite transactions.

An adapter owns durable external identity, execution isolation and deadlines.
This driver does not infer proof from an exception, an empty queue or a missing
job ID. It never retries a submission. The concurrent scheduler is a separate
layer; these methods perform one explicit lifecycle action each.
"""

from typing import Protocol

from .operations import Operations
from .store import ConflictError, InvalidTransition, _json, _text


class OperationBackend(Protocol):
    """A configured, trusted adapter; callbacks return ``{event_key, data}``.

    ``prepare`` must allocate no compute and incur no charge. All callbacks must
    be bounded by the frozen operation deadline or an adapter cleanup deadline.
    An adapter that cannot prepare for free must include that work in ``submit``.
    Parameters and input artifacts are opaque to the engine; the adapter checks
    their task meaning and exact checkpoint/environment bindings before effects.

    ``submit`` receives a durable one-use permit. Even when its response is lost,
    ``inspect`` and ``cancel`` address that original operation and token. A new
    coordinator epoch never authorizes another external submission. Positive
    no-submit proof must also retire/fence any old sender. Cancellation ACK is
    not proof of physical release. Receipts and stable event keys must come from
    the adapter's journal, not from Agent narrative.
    """

    version: str
    environment: dict

    def prepare(self, request: dict, operation: dict) -> dict: ...

    def submit(self, request: dict, permit: dict) -> dict: ...

    def inspect(self, request: dict, operation: dict) -> dict: ...

    def cancel(self, request: dict, operation: dict) -> dict: ...

    def collect(self, request: dict, operation: dict) -> dict: ...

    def settle(self, request: dict, operation: dict) -> dict: ...


class BackendDriver:
    """Bind configured backend identities to trusted implementations."""

    def __init__(self, coordinator, backends: dict[str, OperationBackend]):
        self.coordinator = coordinator
        self.operations = Operations(coordinator)
        self.backends = dict(backends)

    def _inputs(self, operation_id):
        operation = self.operations.get(operation_id)
        backend = self.backends.get(operation["backend"])
        if backend is None:
            raise InvalidTransition("No trusted adapter registered for this backend")
        if backend.version != operation["backend_version"] or _json(
            backend.environment
        ) != _json(operation["environment"]):
            raise ConflictError("Registered backend version or environment changed")
        request = self.operations.request(operation_id)
        return operation, backend, request

    @staticmethod
    def _event(result):
        if not isinstance(result, dict) or set(result) != {"event_key", "data"}:
            raise ValueError("Backend must return exactly event_key and data")
        _text(result["event_key"], "backend event key")
        if not isinstance(result["data"], dict):
            raise ValueError("Backend receipt data must be an object")
        return result

    def _record(self, operation, kind, result):
        event = self._event(result)
        return self.operations.record_receipt(
            operation["id"],
            operation["revision"],
            kind,
            event["event_key"],
            self.operations.envelope(operation["id"], event["data"]),
        )

    def prepare(self, operation_id):
        operation, backend, request = self._inputs(operation_id)
        if operation["state"] != "pending":
            raise InvalidTransition("Only an unprepared operation may prepare")
        return self._record(operation, "prepare", backend.prepare(request, operation))

    def submit(self, operation_id):
        # Resolve the adapter and read the frozen request before reservation. No
        # external effect is permitted until reserve_submit has durably committed.
        operation, backend, request = self._inputs(operation_id)
        permit = self.operations.reserve_submit(operation_id, operation["revision"])
        reserved = self.operations.get(operation_id)
        try:
            result = self._event(backend.submit(request, permit))
        except Exception as exc:
            # The callback may already have sent a request. Preserve its identity
            # and holds; no cleanup or resubmission is inferred from this failure.
            result = {
                "event_key": f"driver-submit-unknown:{reserved['submission_token']}",
                "data": {
                    "outcome": "unknown",
                    "job_id": None,
                    "proof": None,
                    "reason": f"Submission callback failed: {type(exc).__name__}",
                },
            }
        return self._record(reserved, "submission", result)

    def _action(self, operation_id, kind, method):
        operation, backend, request = self._inputs(operation_id)
        observed_quarantine = operation["inconsistency"] is not None and (
            operation["job_id"] is not None or operation.get("unexpected_job_ids")
        )
        if (
            kind in {"inspection", "cancellation", "settlement"}
            and not operation["submission_token"]
            and not observed_quarantine
        ):
            raise InvalidTransition("This operation has no external submission intent")
        if kind == "collection" and (
            operation["state"] != "terminal"
            or operation["inconsistency"] is not None
            or operation["collection_state"] != "pending"
        ):
            raise InvalidTransition("Operation is not ready for collection")
        result = getattr(backend, method)(request, operation)
        return self._record(operation, kind, result)

    def inspect(self, operation_id):
        return self._action(operation_id, "inspection", "inspect")

    def cancel(self, operation_id):
        return self._action(operation_id, "cancellation", "cancel")

    def collect(self, operation_id):
        return self._action(operation_id, "collection", "collect")

    def settle(self, operation_id):
        return self._action(operation_id, "settlement", "settle")
