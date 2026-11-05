"""Fenced per-provider-request reservations and explicit decision references.

The attempt's existing reservation pays for ordinal one. Every later provider
request consumes another Study call before network admission, across processes.
"""

import json
from pathlib import Path
import sqlite3
import time

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


def parse_decision_reference(value):
    """Decode a worker's explicit fixed citation, never a tool-read receipt."""
    from .knowledge import _reference

    if not isinstance(value, str) or not value.startswith("kb:"):
        raise ValueError("Expected a fixed kb: decision reference")
    try:
        _, library_id, kind, tail = value.split(":", 3)
        identity, revision = tail.rsplit("@", 1)
        if kind not in {"evidence", "knowledge", "historical", "index", "file"}:
            raise ValueError("Unsupported decision reference kind")
        if not revision.isdecimal():
            raise ValueError("Decision reference revision must be an integer")
        return _reference(
            dict(library_id=library_id, kind=kind, id=identity, revision=int(revision))
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Expected kb:<library_id>:<kind>:<id>@<revision>") from error


def format_decision_reference(reference):
    """Encode a fixed citation for the existing evidence_refs output field."""
    from .knowledge import _reference

    ref = _reference(reference)
    if ref["kind"] not in {"evidence", "knowledge", "historical", "index", "file"}:
        raise ValueError("Unsupported decision reference kind")
    if ":" in ref["library_id"]:
        raise ValueError("Decision reference library ID cannot contain a colon")
    return f"kb:{ref['library_id']}:{ref['kind']}:{ref['id']}@{ref['revision']}"
