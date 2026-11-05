"""Fenced per-provider-request reservations and immutable retrieval receipts.

The attempt's existing reservation pays for ordinal one. Every later provider
request consumes another Study call before network admission, across processes.
"""

import json
from pathlib import Path
import sqlite3
import time

from .files import publish_artifact
from .invocations import _identity
from .store import BudgetExceeded, ConflictError, DATABASE, OwnershipError


def _connect(root):
    connection = sqlite3.connect(Path(root) / DATABASE, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _fence(connection, capability, now):
    control = connection.execute("SELECT * FROM control WHERE singleton=1").fetchone()
    attempt = connection.execute(
        "SELECT * FROM attempts WHERE id=?", (capability["attempt_id"],)
    ).fetchone()
    if (
        control is None
        or attempt is None
        or not control["owner_open"]
        or control["owner_epoch"] != capability["owner_epoch"]
        or attempt["owner_epoch"] != capability["owner_epoch"]
        or attempt["worker_token"] != capability["worker_token"]
        or attempt["status"] != "active"
    ):
        raise OwnershipError("Model request belongs to an inactive or fenced attempt")
    if control["stop_reason"] is not None or control["admission_mode"] != "active":
        raise BudgetExceeded("Study no longer admits model requests")
    if (
        control["elapsed_seconds"] + max(0, now - control["anchor"])
        >= control["max_seconds"]
    ):
        raise BudgetExceeded("Study wall-time budget exhausted")
    config = json.loads(control["config_json"])
    if (
        config.get("budget", {}).get("window_seconds") is not None
        and control["admission_deadline"] is not None
        and now >= control["admission_deadline"]
    ):
        raise BudgetExceeded("Study admission window ended")
    if capability.get("deadline") is not None and now >= capability["deadline"]:
        raise BudgetExceeded("Original invocation deadline reached")
    return control


def reserve_request(capability, ordinal, *, now=None):
    """Reserve one unique request ordinal; replay must never issue another request."""
    if type(ordinal) is not int or ordinal < 1:
        raise ValueError("Request ordinal must be a positive integer")
    with _connect(capability["study_root"]) as connection:
        connection.execute("BEGIN IMMEDIATE")
        now = time.time() if now is None else now
        control = _fence(connection, capability, now)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS model_requests (attempt_id TEXT NOT NULL, "
            "ordinal INTEGER NOT NULL, recorded_at REAL NOT NULL, "
            "PRIMARY KEY(attempt_id,ordinal))"
        )
        rows = connection.execute(
            "SELECT ordinal FROM model_requests WHERE attempt_id=? ORDER BY ordinal",
            (capability["attempt_id"],),
        ).fetchall()
        ordinals = [row["ordinal"] for row in rows]
        if ordinal in ordinals:
            raise ConflictError("Model request ordinal was already admitted")
        if ordinal != len(ordinals) + 1:
            raise ConflictError("Model request ordinals must be contiguous")
        if ordinal > 1:
            if (
                control["max_calls"] is not None
                and control["calls"] >= control["max_calls"]
            ):
                raise BudgetExceeded("Study call budget exhausted")
            connection.execute("UPDATE control SET calls=calls+1 WHERE singleton=1")
        connection.execute(
            "INSERT INTO model_requests VALUES(?,?,?)",
            (capability["attempt_id"], ordinal, now),
        )


def record_retrieval(capability, name, arguments, result):
    """Publish an authorized successful read and bind its identity to this attempt."""
    with _connect(capability["study_root"]) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _fence(connection, capability, time.time())
        connection.execute(
            "CREATE TABLE IF NOT EXISTS model_retrievals (attempt_id TEXT NOT NULL, "
            "artifact_ref TEXT PRIMARY KEY, identity_json TEXT NOT NULL)"
        )
        reference = publish_artifact(
            Path(capability["study_root"]),
            {
                "attempt_id": capability["attempt_id"],
                "tool": name,
                "arguments": arguments,
                "result": result,
            },
        )
        identity = _identity(capability["study_root"], reference)
        connection.execute(
            "INSERT INTO model_retrievals VALUES(?,?,?)",
            (capability["attempt_id"], reference, json.dumps(identity)),
        )
        return reference


def retrieved_evidence(root, attempt_id):
    """Return recorded immutable identities; readers still check current metadata."""
    with _connect(root) as connection:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_retrievals'"
        ).fetchone():
            return {}
        return {
            row["artifact_ref"]: json.loads(row["identity_json"])
            for row in connection.execute(
                "SELECT artifact_ref,identity_json FROM model_retrievals WHERE attempt_id=?",
                (attempt_id,),
            )
        }


def check_active(capability):
    """Fence tool execution and terminal submission without charging another call."""
    with _connect(capability["study_root"]) as connection:
        _fence(connection, capability, time.time())


def register_trace(capability, directory):
    """Retain the private per-dispatch trace location even when a call fails."""
    with _connect(capability["study_root"]) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _fence(connection, capability, time.time())
        connection.execute(
            "CREATE TABLE IF NOT EXISTS model_call_traces (attempt_id TEXT PRIMARY KEY, directory TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO model_call_traces VALUES(?,?)",
            (capability["attempt_id"], str(Path(directory).resolve())),
        )


def call_trace(root, attempt_id):
    """Return the retained trace pointer; transcript expansion is caller-controlled."""
    with _connect(root) as connection:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_call_traces'"
        ).fetchone():
            return None
        row = connection.execute(
            "SELECT directory FROM model_call_traces WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        return row["directory"] if row else None


def record_consulted(capability, references):
    """Retain typed knowledge dependencies disclosed by a successful tool result."""
    from .knowledge import _reference

    if not isinstance(references, (list, tuple)):
        raise ValueError("Consulted references must be typed knowledge references")
    references = [
        json.dumps(_reference(reference), sort_keys=True, allow_nan=False)
        for reference in references
    ]
    with _connect(capability["study_root"]) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _fence(connection, capability, time.time())
        connection.execute(
            "CREATE TABLE IF NOT EXISTS model_consulted (attempt_id TEXT NOT NULL, reference TEXT NOT NULL, PRIMARY KEY(attempt_id,reference))"
        )
        connection.executemany(
            "INSERT OR IGNORE INTO model_consulted VALUES(?,?)",
            [(capability["attempt_id"], reference) for reference in references],
        )


def consulted_refs(root, attempt_id):
    """Return disclosed fixed references without rereading the private transcript."""
    with _connect(root) as connection:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_consulted'"
        ).fetchone():
            return []
        return [
            json.loads(row["reference"])
            for row in connection.execute(
                "SELECT reference FROM model_consulted WHERE attempt_id=? ORDER BY reference",
                (attempt_id,),
            )
        ]
