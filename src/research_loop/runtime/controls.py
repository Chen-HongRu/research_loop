"""Durable trusted human intents, applied exclusively by the fenced owner.

Submission publishes immutable files without acquiring SQLite ownership. Neither
submission nor draining invokes a model, launches a process, or calls a backend.
"""

from __future__ import annotations

import hashlib
import json
import math
import stat
from pathlib import Path

from .files import _sync_directory, publish_artifact, publish_json
from .store import (
    BudgetExceeded,
    ConflictError,
    InvalidTransition,
    StateStore,
    _decode,
    _json,
    _text,
)
from .updates import _identity, _same

CONTROL_SCHEMA = """
CREATE TABLE control_requests (
 request_id TEXT PRIMARY KEY, kind TEXT NOT NULL, intent_json TEXT NOT NULL,
 request_ref TEXT NOT NULL, identity_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','applied','rejected')),
 receipt_json TEXT, error TEXT
);
CREATE INDEX pending_control_requests ON control_requests(status) WHERE status='pending';
"""

_FIELDS = {
    "task_create": {"id", "specification", "priority", "dependencies"},
    "task_edit": {
        "id",
        "expected_revision",
        "specification",
        "priority",
        "dependencies",
    },
    "task_cancel": {"id", "expected_revision"},
    "stop": {"reason", "cancel_running"},
    "scout": set(),
    "planning": {"reason"},
    "retry_review": {"iteration_id", "expected_revision", "reason", "proof_ref"},
    "cancel_operation": {"operation_id", "reason"},
    "retry_action": {"operation_id", "action", "reason"},
    "ack_event": {"consumer", "event_id"},
}


def _json_value(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise ValueError("Commands require finite JSON values and string object keys")


def _validate(command):
    _json_value(command)
    if not isinstance(command, dict) or set(command) != {
        "version",
        "request_id",
        "kind",
        "payload",
    }:
        raise ValueError("Expected an exact versioned command envelope")
    if type(command["version"]) is not int or command["version"] != 1:
        raise ValueError("Unsupported command version")
    _text(command["request_id"], "request_id")
    kind, payload = command["kind"], command["payload"]
    if not isinstance(kind, str) or kind not in _FIELDS:
        raise ValueError("Unknown trusted human command")
    if not isinstance(payload, dict) or set(payload) != _FIELDS[kind]:
        raise ValueError("Command payload has missing or unauthorized fields")
    for key in ("reason", "iteration_id", "operation_id", "consumer", "proof_ref"):
        if key in payload:
            _text(payload[key], key)
    if "expected_revision" in payload and (
        type(payload["expected_revision"]) is not int
        or payload["expected_revision"] < 1
    ):
        raise ValueError("Expected revision must be a positive integer")
    if kind.startswith("task_"):
        from .task_pool import _fields, _identifier

        _identifier(payload["id"])
        if kind != "task_cancel":
            _fields(
                payload["id"],
                payload["specification"],
                payload["priority"],
                payload["dependencies"],
            )
    if kind == "stop" and type(payload["cancel_running"]) is not bool:
        raise ValueError("stop.cancel_running must be an explicit boolean")
    if kind == "retry_action" and payload["action"] not in (
        "prepare",
        "inspection",
        "cancellation",
        "collection",
        "settlement",
    ):
        raise ValueError("Unknown or forbidden backend retry action")
    if kind == "ack_event" and (
        type(payload["event_id"]) is not int or payload["event_id"] < 1
    ):
        raise ValueError("Invalid event ID")


def submit_command(root, kind, payload, request_id):
    """Durably queue a human intent without writing SQLite."""
    root = Path(root).resolve()
    store = StateStore(root)  # Existing store/schema check, read-only.
    command = dict(version=1, request_id=request_id, kind=kind, payload=payload)
    _validate(command)
    old = _accepted(store, command)
    if old is not None:
        return old
    reference = publish_artifact(root, command)
    folder = root / "command-inbox"
    folder.mkdir(exist_ok=True)
    if folder.is_symlink():
        raise ValueError("Command inbox must not be a symlink")
    _sync_directory(root)
    # A fixed name serializes simultaneous publications with the same ID. This
    # hashes only the small command ID, never data/artifact contents.
    pointer = folder / f"{hashlib.sha256(request_id.encode()).hexdigest()}.json"
    while True:
        try:
            publish_json(pointer, dict(request_ref=reference))
            break
        except FileExistsError:
            try:
                value = json.loads(pointer.read_text())
            except FileNotFoundError:
                # The owner can finish and remove this pointer between link()
                # and read(). Its accepted receipt now resolves the race.
                old = _accepted(store, command)
                if old is not None:
                    return old
                continue
            if not isinstance(value, dict) or set(value) != {"request_ref"}:
                raise ValueError("Malformed command inbox pointer")
            before = _identity(root, value["request_ref"])
            existing = json.loads((root / value["request_ref"]).read_text())
            if not _same(before, _identity(root, value["request_ref"])):
                raise ConflictError("Queued command artifact identity changed")
            if not _same(existing, command):
                raise ConflictError(
                    "Queued request ID reused with a different intent"
                ) from None
            reference = value["request_ref"]
            break
    # A prior pointer may have been accepted/unlinked after our first SQL read.
    old = _accepted(store, command)
    if old is not None:
        return old
    return dict(
        request_id=request_id, kind=kind, request_ref=reference, status="queued"
    )


def _accepted(store, command):
    with store._connection(readonly=True) as conn:
        row = _decode(
            conn.execute(
                "SELECT * FROM control_requests WHERE request_id=?",
                (command["request_id"],),
            ).fetchone()
        )
    if row is None:
        return None
    if not _same(row["intent"], command):
        raise ConflictError("Request ID reused with a different intent")
    return _descriptor(row)


def _descriptor(row):
    return dict(
        request_id=row["request_id"],
        kind=row["kind"],
        request_ref=row["request_ref"],
        status=row["status"],
        result=row["receipt"],
        error=row["error"],
    )


def receipt(root, request_id):
    """Return owner-accepted state; an unaccepted intent has no receipt."""
    _text(request_id, "request_id")
    store = StateStore(root)
    with store._connection(readonly=True) as conn:
        row = _decode(
            conn.execute(
                "SELECT * FROM control_requests WHERE request_id=?", (request_id,)
            ).fetchone()
        )
    return None if row is None else _descriptor(row)


class ControlInbox:
    def __init__(self, owner):
        self.owner = owner
        self.root = owner.store.root

    def _read(self, reference, identity=None):
        before = _identity(self.root, reference)
        if identity is not None and not _same(before, identity):
            raise ConflictError("Command artifact identity changed")
        value = json.loads((self.root / reference).read_text())
        if not _same(before, _identity(self.root, reference)):
            raise ConflictError("Command artifact changed during read")
        return value, before

    def _finish(self, request_id, status, result=None, error=None):
        with self.owner._transaction() as (conn, _):
            conn.execute(
                "UPDATE control_requests SET status=?,receipt_json=?,error=? WHERE request_id=? AND status='pending'",
                (status, _json(result), error, request_id),
            )
        return receipt(self.root, request_id)

    def _apply(self, command):
        from .backend_scheduler import BackendScheduler
        from .dispatch import DispatchLedger, RuntimeEvents
        from .planning_state import PlanningLedger
        from .task_pool import TaskPool

        kind, p = command["kind"], command["payload"]
        rid = "control:" + command["request_id"]
        if kind == "task_create":
            return TaskPool(self.owner).create_human(
                p["id"],
                p["specification"],
                request_id=rid,
                priority=p["priority"],
                dependencies=p["dependencies"],
            )
        if kind == "task_edit":
            return TaskPool(self.owner).edit_human(
                p["id"],
                p["expected_revision"],
                request_id=rid,
                specification=p["specification"],
                priority=p["priority"],
                dependencies=p["dependencies"],
            )
        if kind == "task_cancel":
            return TaskPool(self.owner).cancel_human(
                p["id"], p["expected_revision"], request_id=rid
            )
        if kind == "scout":
            return self.owner.begin_attempt("scout", request_id="runtime:scout:" + rid)
        if kind == "planning":
            if (
                self.owner.snapshot()["config"].get("runtime", {}).get("planning")
                is False
            ):
                raise InvalidTransition(
                    "Planning is disabled in immutable runtime configuration"
                )
            return PlanningLedger(self.owner).trigger(
                rid, "human_request", {"reason": p["reason"]}
            )
        if kind == "ack_event":
            RuntimeEvents(self.owner).ack(p["consumer"], p["event_id"])
            return dict(consumer=p["consumer"], event_id=p["event_id"])
        if kind == "retry_review":
            return DispatchLedger(self.owner).retry_phase(
                p["iteration_id"],
                p["expected_revision"],
                p["reason"],
                p["proof_ref"],
                rid,
            )
        if kind == "stop":
            self.owner.set_stop(p["reason"])
            cancelled = []
            if p["cancel_running"]:
                scheduler = BackendScheduler(
                    self.owner,
                    DispatchLedger(self.owner),
                    None,
                    self.owner.snapshot()["config"]["runtime"],
                )
                for op in scheduler.operations.list():
                    if scheduler._allowed(op, "cancellation"):
                        scheduler.request_cancel(
                            op["id"], p["reason"], rid + ":" + op["id"]
                        )
                        cancelled.append(op["id"])
            return dict(stopped=True, cancellation_requests=cancelled)
        scheduler = BackendScheduler(
            self.owner,
            DispatchLedger(self.owner),
            None,
            self.owner.snapshot()["config"]["runtime"],
        )
        if kind == "cancel_operation":
            return scheduler.request_cancel(p["operation_id"], p["reason"], rid)
        if kind == "retry_action":
            return scheduler.retry_action(
                p["operation_id"], p["action"], p["reason"], rid
            )
        raise ValueError("Unknown trusted human command")

    def _accept(self, reference, command, identity):
        rid = command.get("request_id") if isinstance(command, dict) else None
        if not isinstance(rid, str) or not rid.strip():
            rid = "invalid:" + reference
        kind = (
            command.get("kind", "invalid") if isinstance(command, dict) else "invalid"
        )
        if not isinstance(kind, str):
            kind = "invalid"
        with self.owner._transaction() as (conn, _):
            if not _same(identity, _identity(self.root, reference)):
                raise ConflictError(
                    "Command artifact identity changed before acceptance"
                )
            old = _decode(
                conn.execute(
                    "SELECT * FROM control_requests WHERE request_id=?", (rid,)
                ).fetchone()
            )
            if old is not None:
                if not _same(old["intent"], command):
                    return dict(
                        request_id=rid,
                        kind=kind,
                        request_ref=reference,
                        status="rejected",
                        result=None,
                        error="Request ID reused with a different intent",
                    )
                return _descriptor(old)
            conn.execute(
                "INSERT INTO control_requests VALUES(?,?,?,?,?,'pending',NULL,NULL)",
                (rid, kind, _json(command), reference, _json(identity)),
            )
        return receipt(self.root, rid)

    def _process(self, row, command, *, admit_calls=False):
        if row["status"] != "pending":
            return row
        try:
            _validate(command)
        except ValueError as exc:
            return self._finish(row["request_id"], "rejected", error=str(exc))
        if command["kind"] in {"scout", "retry_review"} and not admit_calls:
            # A temporary CLI owner cannot execute the reserved attempt. Keep
            # its intent pending until a runner can reserve and tick as owner.
            return row
        try:
            result = self._apply(command)
        except OSError as exc:
            return self._pending_error(row, exc)
        except (ValueError, ConflictError, InvalidTransition, BudgetExceeded) as exc:
            # STOP has multiple commits; an error must not falsely reject an
            # already committed stop or a partial set of cancellation requests.
            if command["kind"] == "stop":
                return self._pending_error(row, exc)
            return self._finish(row["request_id"], "rejected", error=str(exc))
        return self._finish(row["request_id"], "applied", result=result)

    def _pending_error(self, row, exc):
        with self.owner._transaction() as (conn, _):
            conn.execute(
                "UPDATE control_requests SET error=? WHERE request_id=? AND status='pending'",
                (str(exc), row["request_id"]),
            )
        return receipt(self.root, row["request_id"])

    def drain(self, *, admit_calls=False):
        """Accept intents, admitting calls only under an execution-ready owner.

        Read pending bodies outside writes; recheck metadata at acceptance.
        """
        results = []
        with self.owner._mutex:
            self.owner._live()
            with self.owner.store._connection(readonly=True) as conn:
                pending = [
                    _decode(row)
                    for row in conn.execute(
                        "SELECT * FROM control_requests WHERE status='pending' ORDER BY rowid"
                    )
                ]
            processed = set()
            for record in pending:
                row = _descriptor(record)
                try:
                    command, identity = self._read(
                        record["request_ref"], record["identity"]
                    )
                    row = self._accept(record["request_ref"], command, identity)
                    row = self._process(row, command, admit_calls=admit_calls)
                except (OSError, ValueError, ConflictError) as exc:
                    row = self._pending_error(row, exc)
                results.append(row)
                processed.add(record["request_id"])
            folder = self.root / "command-inbox"
            if not folder.exists():
                return results
            if folder.is_symlink():
                raise ValueError("Command inbox must not be a symlink")
            for pointer in sorted(
                folder.glob("*.json"),
                key=lambda path: (path.lstat().st_mtime_ns, path.name),
            ):
                try:
                    metadata = pointer.lstat()
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o222:
                        raise ValueError(
                            "Inbox pointer must be a published read-only regular file"
                        )
                    value = json.loads(pointer.read_text())
                    if not isinstance(value, dict) or set(value) != {"request_ref"}:
                        raise ValueError("Malformed command inbox pointer")
                    command, identity = self._read(value["request_ref"])
                    row = self._accept(value["request_ref"], command, identity)
                    if row["request_id"] not in processed:
                        row = self._process(row, command, admit_calls=admit_calls)
                    if row["status"] != "pending":
                        pointer.unlink()
                        _sync_directory(folder)
                    results.append(row)
                except (OSError, ValueError, ConflictError) as exc:
                    # Broken pointers are not evidence that an effect failed.
                    results.append(
                        dict(
                            status="pending",
                            request_id=None,
                            request_ref=None,
                            result=None,
                            error=str(exc),
                        )
                    )
        return results
