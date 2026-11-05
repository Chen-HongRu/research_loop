"""Durable dispatch intents, positive retirement proofs and narrow recovery grants.

The invoker runs outside this ledger. Persisted intents never authorize redispatch;
only a newly created current-owner intent may be launched. Retirement is about the
local callback sender, never proof that an external backend operation had no effect.
"""

import json
import math
import re
import uuid

from .context import ContextPackage
from .operation_contract import json_value
from .store import (
    BudgetExceeded,
    ConflictError,
    InvalidTransition,
    OwnershipError,
    _decode,
    _json,
    _text,
)
from .updates import CallIdentity, Updater, _identity, _same

DISPATCH_SCHEMA = """
CREATE TABLE dispatch_runs (
 token TEXT PRIMARY KEY, category TEXT NOT NULL,
 attempt_id TEXT UNIQUE REFERENCES attempts(id), operation_id TEXT REFERENCES operations(id),
 action_kind TEXT, operation_revision INTEGER, submission_token TEXT,
 owner_epoch INTEGER NOT NULL, kind TEXT NOT NULL, input_ref TEXT NOT NULL,
 input_identity_json TEXT NOT NULL, adapter_json TEXT NOT NULL,
 context_ref TEXT, context_identity_json TEXT, deadline REAL NOT NULL,
 state TEXT NOT NULL, worker_retired INTEGER NOT NULL DEFAULT 0,
 completion_ref TEXT, completion_identity_json TEXT, status TEXT, completed_at REAL,
 worker_identity_json TEXT, settled INTEGER NOT NULL DEFAULT 0,
 settlement_status TEXT, settlement_ref TEXT, settlement_identity_json TEXT, error TEXT
);
CREATE UNIQUE INDEX one_unresolved_backend_action ON dispatch_runs(operation_id)
 WHERE category='backend' AND settled=0;
CREATE INDEX unretired_dispatch_runs ON dispatch_runs(worker_retired) WHERE worker_retired=0;
CREATE UNIQUE INDEX unique_operation_submission_token ON operations(submission_token) WHERE submission_token IS NOT NULL;
CREATE TABLE reconciliation_grants (
 attempt_id TEXT NOT NULL REFERENCES attempts(id), current_owner_epoch INTEGER NOT NULL,
 token TEXT NOT NULL REFERENCES dispatch_runs(token), completion_ref TEXT NOT NULL,
 completion_identity_json TEXT NOT NULL, input_ref TEXT NOT NULL, input_identity_json TEXT NOT NULL,
 context_ref TEXT, context_identity_json TEXT, request_id TEXT NOT NULL UNIQUE,
 PRIMARY KEY(attempt_id,current_owner_epoch)
);
CREATE TABLE planning_reconciliations (
 round_id TEXT NOT NULL REFERENCES planning_rounds(id), current_owner_epoch INTEGER NOT NULL,
 request_id TEXT NOT NULL UNIQUE, PRIMARY KEY(round_id,current_owner_epoch)
);
CREATE TABLE phase_retries (
 request_id TEXT PRIMARY KEY, intent_json TEXT NOT NULL, receipt_json TEXT NOT NULL,
 new_attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id),
 source_attempt_id TEXT NOT NULL REFERENCES attempts(id),
 source_context_ref TEXT, source_context_identity_json TEXT,
 source_evidence_identities_json TEXT NOT NULL, proof_ref TEXT NOT NULL, proof_identity_json TEXT NOT NULL
);
CREATE TABLE runtime_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, producer_key TEXT NOT NULL UNIQUE,
 kind TEXT NOT NULL, subject TEXT NOT NULL, payload_ref TEXT,
 payload_identity_json TEXT, payload_json TEXT NOT NULL
);
CREATE TABLE runtime_event_acks (
 consumer TEXT NOT NULL, event_id INTEGER NOT NULL REFERENCES runtime_events(id),
 PRIMARY KEY(consumer,event_id)
);
"""

_TERMINAL = {"completed", "failed", "cancelled", "timed_out", "not_started"}
_ACTIONS = {
    "prepare",
    "submit",
    "inspection",
    "cancellation",
    "collection",
    "settlement",
}
_COMPLETION_FIELDS = {
    "version",
    "launch_token",
    "kind",
    "input_ref",
    "adapter",
    "status",
    "result",
    "error",
    "sender_retired",
    "exit_code",
}


class RuntimeEvents:
    def __init__(self, coordinator):
        self.owner = coordinator

    @staticmethod
    def emit(
        conn,
        producer_key,
        kind,
        subject,
        *,
        payload_ref=None,
        identity=None,
        payload=None,
    ):
        """Append compact control metadata; callers already own a short transaction."""
        for value, name in (
            (producer_key, "producer_key"),
            (kind, "event kind"),
            (subject, "event subject"),
        ):
            _text(value, name)
        value = {} if payload is None else json_value(payload)
        if not isinstance(value, dict):
            raise ValueError("Runtime event metadata must be an object")
        if (payload_ref is None) != (identity is None):
            raise ValueError("Event artifact reference and identity must be paired")
        previous = _decode(
            conn.execute(
                "SELECT * FROM runtime_events WHERE producer_key=?", (producer_key,)
            ).fetchone()
        )
        if previous is not None:
            if not _same(
                (
                    previous["kind"],
                    previous["subject"],
                    previous["payload_ref"],
                    previous["payload_identity"],
                    previous["payload"],
                ),
                (kind, subject, payload_ref, identity, value),
            ):
                raise ConflictError(
                    "Runtime event producer key reused with different intent"
                )
            return previous
        event_id = conn.execute(
            "INSERT INTO runtime_events(producer_key,kind,subject,payload_ref,payload_identity_json,payload_json) VALUES(?,?,?,?,?,?)",
            (
                producer_key,
                kind,
                subject,
                payload_ref,
                _json(identity) if identity is not None else None,
                _json(value),
            ),
        ).lastrowid
        return _decode(
            conn.execute(
                "SELECT * FROM runtime_events WHERE id=?", (event_id,)
            ).fetchone()
        )

    def pending(self, consumer):
        _text(consumer, "consumer")
        self.owner._live()
        with self.owner.store._connection(readonly=True) as conn:
            return [
                _decode(row)
                for row in conn.execute(
                    "SELECT e.* FROM runtime_events e LEFT JOIN runtime_event_acks a ON a.event_id=e.id AND a.consumer=? WHERE a.event_id IS NULL ORDER BY e.id",
                    (consumer,),
                )
            ]

    def ack(self, consumer, event_id):
        _text(consumer, "consumer")
        if type(event_id) is not int or event_id < 1:
            raise ValueError("Invalid event ID")
        with self.owner._transaction() as (conn, _):
            if (
                conn.execute(
                    "SELECT 1 FROM runtime_events WHERE id=?", (event_id,)
                ).fetchone()
                is None
            ):
                raise ConflictError("Unknown runtime event")
            conn.execute(
                "INSERT OR IGNORE INTO runtime_event_acks VALUES(?,?)",
                (consumer, event_id),
            )


class DispatchLedger:
    def __init__(self, coordinator, max_slots=None):
        self.owner = coordinator
        self.reader = Updater(coordinator, validators={})
        runtime = coordinator.snapshot()["config"].get("runtime")
        if not isinstance(runtime, dict):
            raise ValueError("Dispatch requires immutable config.runtime")
        slots = runtime.get("worker_slots")
        adapter = runtime.get("worker_adapter")
        if type(slots) is not int or slots < 1:
            raise ValueError("runtime.worker_slots must be a positive integer")
        if max_slots is not None and (type(max_slots) is not int or max_slots != slots):
            raise ValueError(
                "Explicit worker limit must equal immutable runtime configuration"
            )
        if not isinstance(adapter, dict) or set(adapter) != {"id", "version"}:
            raise ValueError("runtime.worker_adapter requires id and version")
        for value in adapter.values():
            _text(value, "worker adapter binding")
        self.max_slots = slots
        self.adapter = json.loads(_json(adapter))

    @staticmethod
    def call_token(attempt_id):
        return "call-" + attempt_id

    @staticmethod
    def action_token():
        return "backend-" + uuid.uuid4().hex

    @staticmethod
    def _get(conn, token):
        row = _decode(
            conn.execute(
                "SELECT * FROM dispatch_runs WHERE token=?", (token,)
            ).fetchone()
        )
        if row is None:
            raise ConflictError("Unknown dispatch token")
        row["worker_retired"] = bool(row["worker_retired"])
        row["settled"] = bool(row["settled"])
        return row

    def get(self, token):
        self.owner._live()
        with self.owner.store._connection(readonly=True) as conn:
            return self._get(conn, token)

    def list(self, category=None):
        self.owner._live()
        with self.owner.store._connection(readonly=True) as conn:
            conn.execute("BEGIN")
            return [
                self._get(conn, row[0])
                for row in conn.execute(
                    "SELECT token FROM dispatch_runs WHERE (? IS NULL OR category=?) ORDER BY rowid",
                    (category, category),
                )
            ]

    def unsettled(self, category=None):
        return [run for run in self.list(category) if not run["settled"]]

    def active_count(self):
        self.owner._live()
        with self.owner.store._connection(readonly=True) as conn:
            return conn.execute(
                "SELECT count(*) FROM dispatch_runs WHERE worker_retired=0"
            ).fetchone()[0]

    def _slot(self, conn, control, *, admit=True):
        if admit:
            self.owner._admit(control)
        if (
            conn.execute(
                "SELECT count(*) FROM dispatch_runs WHERE worker_retired=0"
            ).fetchone()[0]
            >= self.max_slots
        ):
            raise BudgetExceeded(
                "All bounded worker slots are occupied, including unknown prior senders"
            )

    def _capture_input(self, input_ref, adapter, deadline):
        if not _same(adapter, self.adapter):
            raise ConflictError("Worker adapter differs from immutable configuration")
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError("Worker deadline must be a finite UTC timestamp")
        identity = _identity(self.owner.store.root, input_ref)
        payload = self.reader._read_record(input_ref, identity)
        if not isinstance(payload, dict):
            raise ValueError("Invocation input must be an object")
        return identity, payload

    def _check_refs(self, run):
        self.reader._check_identity(run["input_ref"], run["input_identity"])
        if run["context_ref"] is not None:
            self.reader._check_identity(run["context_ref"], run["context_identity"])
        if run["completion_ref"] is not None:
            self.reader._check_identity(
                run["completion_ref"], run["completion_identity"]
            )

    def register_call(self, call, input_ref, adapter, deadline):
        if not isinstance(call, CallIdentity):
            raise TypeError("Expected CallIdentity")
        identity, payload = self._capture_input(input_ref, adapter, deadline)
        captured = self.owner._feedback_snapshot(call.to_dict())
        binding = captured["binding"]
        if captured["attempt"]["status"] != "active" or binding is None:
            raise InvalidTransition("Dispatch requires an active context-bound attempt")
        if captured["attempt"]["owner_epoch"] != self.owner.owner_epoch:
            raise OwnershipError("Prior-owner intent cannot be launched by a new owner")
        original = self.reader._read_record(binding["context_ref"], binding["identity"])
        if (
            not _same(payload.get("call"), call.to_dict())
            or payload.get("context_ref") != binding["context_ref"]
            or json.dumps(payload.get("package"), ensure_ascii=False, allow_nan=False)
            != json.dumps(original, ensure_ascii=False, allow_nan=False)
        ):
            raise ConflictError(
                "Invocation payload differs from the frozen call/context"
            )
        token = self.call_token(call.attempt_id)
        with self.owner._transaction() as (conn, control):
            existing = conn.execute(
                "SELECT token FROM dispatch_runs WHERE attempt_id=?", (call.attempt_id,)
            ).fetchone()
            if existing:
                run = self._get(conn, existing[0])
                if not _same(
                    (
                        run["input_ref"],
                        run["input_identity"],
                        run["adapter"],
                        run["deadline"],
                    ),
                    (input_ref, identity, adapter, deadline),
                ):
                    raise ConflictError(
                        "Attempt already has a different immutable dispatch binding"
                    )
                return dict(run=run, created=False)
            self._slot(conn, control)
            current = self.owner._feedback_record(
                conn, call.to_dict(), metadata_only=True
            )
            if (
                current["attempt"]["status"] != "active"
                or current["attempt"]["owner_epoch"] != self.owner.owner_epoch
                or not _same(current["binding"], binding)
            ):
                raise ConflictError(
                    "Attempt/context changed before dispatch registration"
                )
            self.reader._check_identity(input_ref, identity)
            self.reader._check_frozen_refs(current, captured)
            conn.execute(
                "INSERT INTO dispatch_runs(token,category,attempt_id,owner_epoch,kind,input_ref,input_identity_json,adapter_json,context_ref,context_identity_json,deadline,state) VALUES(?,'call',?,?,'agent',?,?,?,?,?,?,'intent')",
                (
                    token,
                    call.attempt_id,
                    self.owner.owner_epoch,
                    input_ref,
                    _json(identity),
                    _json(adapter),
                    binding["context_ref"],
                    _json(binding["identity"]),
                    deadline,
                ),
            )
            return dict(run=self._get(conn, token), created=True)

    def register_orphan_for_retirement(self, attempt_id, input_ref, adapter, deadline):
        identity, _ = self._capture_input(input_ref, adapter, deadline)
        token = self.call_token(attempt_id)
        with self.owner._transaction() as (conn, _):
            previous = conn.execute(
                "SELECT token FROM dispatch_runs WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if previous:
                return dict(run=self._get(conn, previous[0]), created=False)
            attempt = conn.execute(
                "SELECT * FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            if attempt is None or attempt["status"] != "active":
                raise InvalidTransition(
                    "Only an active untracked reservation can be retired"
                )
            binding = _decode(
                conn.execute(
                    "SELECT * FROM contexts WHERE attempt_id=?", (attempt_id,)
                ).fetchone()
            )
            self.reader._check_identity(input_ref, identity)
            conn.execute(
                "INSERT INTO dispatch_runs(token,category,attempt_id,owner_epoch,kind,input_ref,input_identity_json,adapter_json,context_ref,context_identity_json,deadline,state) VALUES(?,'call',?,?,'agent',?,?,?,?,?,?,'retiring')",
                (
                    token,
                    attempt_id,
                    attempt["owner_epoch"],
                    input_ref,
                    _json(identity),
                    _json(adapter),
                    binding["context_ref"] if binding else None,
                    _json(binding["identity"]) if binding else None,
                    deadline,
                ),
            )
            return dict(run=self._get(conn, token), created=True)

    def register_action(
        self,
        operation_id,
        expected_revision,
        kind,
        input_ref,
        adapter,
        deadline,
        *,
        launch_token,
        submission_token=None,
    ):
        from .operations import Operations

        if kind not in _ACTIONS:
            raise ValueError("Unknown backend action")
        _text(launch_token, "launch token")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", launch_token):
            raise ValueError("Launch token must be a safe invoker identity")
        identity, payload = self._capture_input(input_ref, adapter, deadline)
        operations = Operations(self.owner)
        op = operations.get(operation_id)
        if op["revision"] != expected_revision:
            raise ConflictError("Operation revision mismatch")
        if (
            payload.get("operation_id") != operation_id
            or payload.get("action") != kind
            or not _same(payload.get("request"), operations.request(operation_id))
        ):
            raise ConflictError(
                "Backend invocation differs from its original operation/request"
            )
        detached = payload.get("operation")
        if not isinstance(detached, dict) or any(
            not _same(detached.get(key), op[key])
            for key in (
                "id",
                "backend",
                "backend_version",
                "environment",
                "execute_attempt_id",
                "plan_attempt_id",
                "deadline",
            )
        ):
            raise ConflictError(
                "Backend invocation immutable operation identity differs"
            )
        if kind == "submit":
            _text(submission_token, "preallocated submission token")
            expected = {
                **Operations._binding(op),
                "submission_token": submission_token,
                "owner_epoch": self.owner.owner_epoch,
                "deadline": op["deadline"],
                "operation_revision": expected_revision + 1,
                "request_ref": op["request_ref"],
            }
            if not _same(payload.get("permit"), expected):
                raise ConflictError(
                    "Backend submit payload differs from anticipated atomic permit"
                )
        elif payload.get("permit") is not None:
            raise ValueError("Only submit carries a submission permit")
        with self.owner._transaction() as (conn, control):
            previous = conn.execute(
                "SELECT token FROM dispatch_runs WHERE token=?", (launch_token,)
            ).fetchone()
            if previous:
                run = self._get(conn, launch_token)
                if not _same(
                    (
                        run["operation_id"],
                        run["action_kind"],
                        run["input_ref"],
                        run["input_identity"],
                        run["adapter"],
                        run["deadline"],
                    ),
                    (operation_id, kind, input_ref, identity, adapter, deadline),
                ):
                    raise ConflictError("Action token reused with different intent")
                return dict(run=run, created=False)
            self._slot(conn, control, admit=kind in {"prepare", "submit"})
            if conn.execute(
                "SELECT 1 FROM dispatch_runs WHERE operation_id=? AND settled=0",
                (operation_id,),
            ).fetchone():
                raise InvalidTransition("Operation has an unresolved backend action")
            self.reader._check_identity(input_ref, identity)
            current = Operations._get(conn, operation_id)
            if current["revision"] != expected_revision:
                raise ConflictError("Operation changed before action registration")
            if kind == "submit":
                permit = operations._reserve_submit(
                    conn,
                    control,
                    operation_id,
                    expected_revision,
                    submission_token=submission_token,
                )
                operation_revision = permit["operation_revision"]
            else:
                operation_revision = current["revision"]
            conn.execute(
                "INSERT INTO dispatch_runs(token,category,operation_id,action_kind,operation_revision,submission_token,owner_epoch,kind,input_ref,input_identity_json,adapter_json,deadline,state) VALUES(?,'backend',?,?,?,?,?,'backend',?,?,?,?,'intent')",
                (
                    launch_token,
                    operation_id,
                    kind,
                    operation_revision,
                    submission_token
                    if kind == "submit"
                    else current["submission_token"],
                    self.owner.owner_epoch,
                    input_ref,
                    _json(identity),
                    _json(adapter),
                    deadline,
                ),
            )
            return dict(run=self._get(conn, launch_token), created=True)

    def _completion(self, run, reference):
        identity = _identity(self.owner.store.root, reference)
        payload = self.reader._read_record(reference, identity)
        if (
            not isinstance(payload, dict)
            or set(payload) != _COMPLETION_FIELDS
            or type(payload["version"]) is not int
            or payload["version"] != 1
        ):
            raise ValueError("Invalid canonical worker completion")
        if not _same(
            (
                payload["launch_token"],
                payload["kind"],
                payload["input_ref"],
                payload["adapter"],
            ),
            (run["token"], run["kind"], run["input_ref"], run["adapter"]),
        ):
            raise ConflictError(
                "Worker completion differs from immutable dispatch binding"
            )
        if payload["status"] not in _TERMINAL or payload["sender_retired"] is not True:
            raise ValueError(
                "A terminal completion requires positive sender retirement"
            )
        if payload["exit_code"] is not None and type(payload["exit_code"]) is not int:
            raise ValueError("Invalid actual worker exit code")
        if payload["error"] is not None and not isinstance(payload["error"], str):
            raise ValueError("Invalid worker error")
        if payload["status"] == "completed" and (
            payload["exit_code"] != 0 or payload["error"] is not None
        ):
            raise ValueError("Completed callback needs a successful actual exit")
        if payload["status"] != "completed" and payload["result"] is not None:
            raise ValueError(
                "Non-completed callbacks cannot provide an accepted result"
            )
        json_value(payload["result"])
        return payload, identity

    def observe(self, token, observation):
        if not isinstance(observation, dict) or set(observation) != {
            "status",
            "completion_ref",
            "proof_ref",
            "worker_identity",
        }:
            raise ValueError("Invalid invoker observation")
        run = self.get(token)
        status = observation["status"]
        if status not in _TERMINAL | {"running", "unknown"}:
            raise ValueError("Unknown invoker status")
        payload = identity = None
        if status in _TERMINAL:
            if (
                observation["completion_ref"] is None
                or observation["proof_ref"] != observation["completion_ref"]
            ):
                raise ValueError(
                    "Terminal observation requires the exact canonical completion proof"
                )
            payload, identity = self._completion(run, observation["completion_ref"])
            if payload["status"] != status:
                raise ConflictError("Observation differs from completion status")
        elif (
            observation["completion_ref"] is not None
            or observation["proof_ref"] is not None
        ):
            raise ValueError("Unknown/running observations cannot retire a sender")
        worker = json_value(observation["worker_identity"])
        with self.owner._transaction() as (conn, control):
            current = self._get(conn, token)
            self._check_refs(current)
            if current["worker_retired"]:
                if (
                    status not in _TERMINAL
                    or current["completion_ref"] != observation["completion_ref"]
                    or not _same(current["completion_identity"], identity)
                ):
                    raise ConflictError(
                        "Positive retirement proof cannot be replaced or regressed"
                    )
                return current
            if payload is not None:
                self.reader._check_identity(observation["completion_ref"], identity)
                conn.execute(
                    "UPDATE dispatch_runs SET state='terminal',worker_retired=1,completion_ref=?,completion_identity_json=?,status=?,worker_identity_json=?,error=?,completed_at=? WHERE token=?",
                    (
                        observation["completion_ref"],
                        _json(identity),
                        status,
                        _json(worker),
                        payload["error"],
                        control["anchor"],
                        token,
                    ),
                )
                RuntimeEvents.emit(
                    conn,
                    "dispatch-terminal:" + token,
                    "worker_terminal",
                    token,
                    payload_ref=observation["completion_ref"],
                    identity=identity,
                    payload={"status": status, "category": current["category"]},
                )
            else:
                conn.execute(
                    "UPDATE dispatch_runs SET state=?,worker_identity_json=? WHERE token=?",
                    (status, _json(worker), token),
                )
            return self._get(conn, token)

    @staticmethod
    def can_reconcile(conn, attempt_id, owner_epoch):
        grant = _decode(
            conn.execute(
                "SELECT * FROM reconciliation_grants WHERE attempt_id=? AND current_owner_epoch=?",
                (attempt_id, owner_epoch),
            ).fetchone()
        )
        if grant is None:
            return False
        run = _decode(
            conn.execute(
                "SELECT * FROM dispatch_runs WHERE token=?", (grant["token"],)
            ).fetchone()
        )
        binding = _decode(
            conn.execute(
                "SELECT * FROM contexts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
        )
        return bool(
            run
            and run["worker_retired"]
            and run["attempt_id"] == attempt_id
            and run["completion_ref"] == grant["completion_ref"]
            and _same(run["completion_identity"], grant["completion_identity"])
            and (
                (binding is None and grant["context_ref"] is None)
                or (
                    binding is not None
                    and binding["context_ref"] == grant["context_ref"]
                    and _same(binding["identity"], grant["context_identity"])
                )
            )
        )

    def grant_reconciliation(self, token, request_id):
        _text(request_id, "reconciliation request ID")
        run = self.get(token)
        if run["category"] != "call" or not run["worker_retired"]:
            raise InvalidTransition(
                "Only an individually retired call can receive a recovery grant"
            )
        self._completion(run, run["completion_ref"])
        with self.owner._transaction() as (conn, _):
            current = self._get(conn, token)
            self._check_refs(current)
            existing = _decode(
                conn.execute(
                    "SELECT * FROM reconciliation_grants WHERE request_id=?",
                    (request_id,),
                ).fetchone()
            )
            if existing:
                if (
                    existing["token"] != token
                    or existing["current_owner_epoch"] != self.owner.owner_epoch
                ):
                    raise ConflictError("Reconciliation request reused")
                return existing
            prior = _decode(
                conn.execute(
                    "SELECT * FROM reconciliation_grants WHERE attempt_id=? AND current_owner_epoch=?",
                    (run["attempt_id"], self.owner.owner_epoch),
                ).fetchone()
            )
            if prior:
                return prior
            conn.execute(
                "INSERT INTO reconciliation_grants VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    run["attempt_id"],
                    self.owner.owner_epoch,
                    token,
                    run["completion_ref"],
                    _json(run["completion_identity"]),
                    run["input_ref"],
                    _json(run["input_identity"]),
                    run["context_ref"],
                    _json(run["context_identity"])
                    if run["context_identity"] is not None
                    else None,
                    request_id,
                ),
            )
            return _decode(
                conn.execute(
                    "SELECT * FROM reconciliation_grants WHERE attempt_id=? AND current_owner_epoch=?",
                    (run["attempt_id"], self.owner.owner_epoch),
                ).fetchone()
            )

    def result(self, token):
        run = self.get(token)
        if not run["worker_retired"] or run["status"] != "completed":
            raise InvalidTransition("Call has no completed retired result")
        payload, _ = self._completion(run, run["completion_ref"])
        return payload["result"]

    def settle(self, token, status, error=None, receipt_ref=None):
        if status not in {"accepted", "failed", "recorded", "ignored"}:
            raise ValueError("Invalid dispatch settlement")
        identity = (
            _identity(self.owner.store.root, receipt_ref)
            if receipt_ref is not None
            else None
        )
        with self.owner._transaction() as (conn, _):
            run = self._get(conn, token)
            if not run["worker_retired"]:
                raise InvalidTransition(
                    "Unknown sender cannot settle or free its action"
                )
            self._check_refs(run)
            if run["settled"]:
                if not _same(
                    (run["settlement_status"], run["error"], run["settlement_ref"]),
                    (status, error, receipt_ref),
                ):
                    raise ConflictError("Dispatch already settled differently")
                return run
            if receipt_ref is not None:
                self.reader._check_identity(receipt_ref, identity)
            conn.execute(
                "UPDATE dispatch_runs SET settled=1,settlement_status=?,settlement_ref=?,settlement_identity_json=?,error=? WHERE token=?",
                (
                    status,
                    receipt_ref,
                    _json(identity) if identity is not None else None,
                    error,
                    token,
                ),
            )
            return self._get(conn, token)

    @staticmethod
    def can_use_round(conn, round_id, owner_epoch):
        row = conn.execute(
            "SELECT owner_epoch FROM planning_rounds WHERE id=?", (round_id,)
        ).fetchone()
        if row is None:
            return False
        if row[0] == owner_epoch:
            return True
        return (
            conn.execute(
                "SELECT 1 FROM planning_reconciliations WHERE round_id=? AND current_owner_epoch=?",
                (round_id, owner_epoch),
            ).fetchone()
            is not None
        )

    def authorize_planning(self, round_id, request_id):
        _text(request_id, "planning reconciliation request ID")
        with self.owner.store._connection(readonly=True) as conn:
            calls = [
                dict(row)
                for row in conn.execute(
                    "SELECT a.id,a.status,d.token,d.worker_retired,d.completion_ref,d.completion_identity_json FROM planning_calls c JOIN attempts a ON a.id=c.attempt_id LEFT JOIN dispatch_runs d ON d.attempt_id=a.id WHERE c.round_id=?",
                    (round_id,),
                )
            ]
        if not calls:
            raise ConflictError("Unknown or empty planning round")
        for call in calls:
            if call["status"] not in {"accepted", "failed"} or (
                call["token"] is not None and not call["worker_retired"]
            ):
                raise InvalidTransition("Planning has an unresolved prior sender")
            if call["token"] is not None:
                run = self.get(call["token"])
                self._completion(run, run["completion_ref"])
        with self.owner._transaction() as (conn, _):
            current = [
                dict(row)
                for row in conn.execute(
                    "SELECT a.id,a.status,d.token,d.worker_retired,d.completion_ref,d.completion_identity_json FROM planning_calls c JOIN attempts a ON a.id=c.attempt_id LEFT JOIN dispatch_runs d ON d.attempt_id=a.id WHERE c.round_id=?",
                    (round_id,),
                )
            ]
            if not _same(current, calls):
                raise ConflictError("Planning senders changed during reconciliation")
            for call in calls:
                if call["token"] is not None:
                    self._check_refs(self._get(conn, call["token"]))
            previous = conn.execute(
                "SELECT * FROM planning_reconciliations WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if previous and (
                previous["round_id"] != round_id
                or previous["current_owner_epoch"] != self.owner.owner_epoch
            ):
                raise ConflictError("Planning grant request reused")
            conn.execute(
                "INSERT OR IGNORE INTO planning_reconciliations VALUES(?,?,?)",
                (round_id, self.owner.owner_epoch, request_id),
            )
            return dict(round_id=round_id, current_owner_epoch=self.owner.owner_epoch)

    def retry_phase(
        self, iteration_id, expected_revision, reason, proof_ref, request_id
    ):
        _text(reason, "explicit retry reason")
        _text(request_id, "retry request ID")
        intent = dict(
            iteration_id=iteration_id,
            expected_revision=expected_revision,
            reason=reason,
            proof_ref=proof_ref,
        )
        with self.owner.store._connection(readonly=True) as conn:
            conn.execute("BEGIN")
            previous = _decode(
                conn.execute(
                    "SELECT * FROM phase_retries WHERE request_id=?", (request_id,)
                ).fetchone()
            )
            if previous:
                if not _same(previous["intent"], intent):
                    raise ConflictError("Retry request reused with different intent")
                return previous["receipt"]
            iteration = _decode(
                conn.execute(
                    "SELECT * FROM iterations WHERE id=?", (iteration_id,)
                ).fetchone()
            )
            if (
                iteration is None
                or iteration["revision"] != expected_revision
                or iteration["phase"] != "review"
                or iteration["status"] != "failed"
            ):
                raise InvalidTransition(
                    "Only the current failed Review phase can be explicitly retried"
                )
            source = _decode(
                conn.execute(
                    "SELECT a.* FROM attempts a WHERE iteration_id=? AND role='review' AND status='failed' ORDER BY rowid DESC LIMIT 1",
                    (iteration_id,),
                ).fetchone()
            )
            run = (
                _decode(
                    conn.execute(
                        "SELECT * FROM dispatch_runs WHERE attempt_id=?",
                        (source["id"],),
                    ).fetchone()
                )
                if source
                else None
            )
            binding = (
                _decode(
                    conn.execute(
                        "SELECT * FROM contexts WHERE attempt_id=?", (source["id"],)
                    ).fetchone()
                )
                if source
                else None
            )
        if (
            run is None
            or not run["worker_retired"]
            or (binding is None and run["status"] != "not_started")
            or run["completion_ref"] != proof_ref
        ):
            raise InvalidTransition(
                "Retry requires the original context and exact sender retirement proof"
            )
        self._completion(run, proof_ref)
        if binding is not None:
            self.reader._read_record(binding["context_ref"], binding["identity"])
        evidence = binding["evidence_identities"] if binding is not None else {}
        for ref, identity in evidence.items():
            self.reader._check_identity(ref, identity)
        with self.owner._transaction() as (conn, control):
            old = conn.execute(
                "SELECT * FROM iterations WHERE id=?", (iteration_id,)
            ).fetchone()
            if not _same(dict(old), iteration):
                raise ConflictError("Review phase changed before retry")
            self._check_refs(self._get(conn, run["token"]))
            for ref, identity in evidence.items():
                self.reader._check_identity(ref, identity)
            conn.execute(
                "UPDATE iterations SET status='active',revision=revision+1 WHERE id=?",
                (iteration_id,),
            )
            attempt = self.owner._reserve_attempt(
                conn,
                control,
                "review",
                request_id="retry:" + request_id,
                iteration_id=iteration_id,
                expected_revision=expected_revision + 1,
            )
            conn.execute("UPDATE control SET pool_version=pool_version+1")
            conn.execute(
                "INSERT INTO phase_retries VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    _json(intent),
                    _json(attempt),
                    attempt["id"],
                    source["id"],
                    binding["context_ref"] if binding is not None else None,
                    _json(binding["identity"]) if binding is not None else None,
                    _json(evidence),
                    proof_ref,
                    _json(run["completion_identity"]),
                ),
            )
            RuntimeEvents.emit(
                conn,
                "phase-retry:" + request_id,
                "phase_retry",
                iteration_id,
                payload_ref=proof_ref,
                identity=run["completion_identity"],
                payload={
                    "attempt_id": attempt["id"],
                    "source_attempt_id": source["id"],
                },
            )
            return attempt

    def retry_context(self, attempt_id):
        with self.owner.store._connection(readonly=True) as conn:
            row = _decode(
                conn.execute(
                    "SELECT * FROM phase_retries WHERE new_attempt_id=?", (attempt_id,)
                ).fetchone()
            )
        if row is None or row["source_context_ref"] is None:
            return None
        for ref, identity in row["source_evidence_identities"].items():
            self.reader._check_identity(ref, identity)
        return ContextPackage.from_dict(
            self.reader._read_record(
                row["source_context_ref"], row["source_context_identity"]
            )
        )
