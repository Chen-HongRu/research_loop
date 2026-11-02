"""Durable admission of one short investigation before its entry cutoff."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import time
import uuid


def _read(path):
    return json.loads(path.read_text())


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def active_round(root, *, owner_token=None):
    """Return only an admitted, committed, unfinished round with unchanged control.

    An orphan record/iteration file does not authorize entry after a crash: the
    atomically published Study state must commit the same admission token.
    """
    root = Path(root)
    try:
        if (root / "control/STOP").exists():
            return None
        state = _read(root / "study/state.json")
        if state["phase"] == "done" or state["status"] == "stopped":
            return None
        iteration = f"{state['iteration']:04d}"
        record = _read(root / "control/round-admissions" / f"{iteration}.json")
        metadata = _read(root / "study/iterations" / iteration / "iteration.json")
        owner = _read(root / "control/ownership.json")
        lease = _read(root / "control/continuation.json")
        token = record["token"]
        if (
            record["schema"] != 1
            or record["iteration"] != iteration
            or not isinstance(token, str)
            or not token
            or state.get("round_admission") != token
            or metadata.get("round_admission") != token
            or state.get("round_admitted_at") != record.get("committed_at")
            or type(record.get("committed_at")) not in (int, float)
            or not record["admitted_at"] <= record["committed_at"] < record["cutoff"]
            or record["owner"] != owner
            or record["continuation"] != lease
            or (owner_token is not None and owner.get("owner_token") != owner_token)
            or type(record["admitted_at"]) not in (int, float)
            or not math.isfinite(record["admitted_at"])
            or not lease["started_at"] <= record["admitted_at"] < record["cutoff"]
            or record["cutoff"] != min(lease["deadline"], owner["expires_at"])
        ):
            return None
        return record
    except (OSError, ValueError, TypeError, KeyError):
        return None


def admit(root, iteration, *, owner_token):
    """Prepare admission; caller commits token to iteration metadata and state."""
    root = Path(root)
    existing = active_round(root, owner_token=owner_token)
    if existing and existing["iteration"] == iteration:
        return existing["token"]
    prior = root / "control/round-admissions" / f"{iteration}.json"
    if prior.exists() and _read(prior).get("committed_at") is not None:
        raise ValueError(
            "Committed short round cannot be rebound after control/state change"
        )
    owner = _read(root / "control/ownership.json")
    lease = _read(root / "control/continuation.json")
    now = time.time()
    cutoff = min(lease["deadline"], owner["expires_at"])
    if (
        (root / "control/STOP").exists()
        or (root / "control/HANDOFF").exists()
        or owner.get("owner_token") != owner_token
        or lease.get("owner_token") != owner_token
        or owner.get("handoff_acknowledged") is not True
        or not lease["started_at"] <= now < cutoff
    ):
        raise ValueError(
            "Short investigation entry cutoff reached or ownership changed"
        )
    # Never reuse a prepared-but-uncommitted token across a crash.
    token = uuid.uuid4().hex
    _write(
        root / "control/round-admissions" / f"{iteration}.json",
        {
            "schema": 1,
            "iteration": iteration,
            "token": token,
            "admitted_at": now,
            "cutoff": cutoff,
            "owner": owner,
            "continuation": lease,
        },
    )
    return token


def commit(root, iteration, token, *, owner_token):
    """Revalidate immediately before Study publishes entry to the new round."""
    root = Path(root)
    existing = active_round(root, owner_token=owner_token)
    if existing and existing["iteration"] == iteration and existing["token"] == token:
        return existing["committed_at"]
    path = root / "control/round-admissions" / f"{iteration}.json"
    record = _read(path)
    now = time.time()
    if (
        record["token"] != token
        or record["owner"] != _read(root / "control/ownership.json")
        or record["continuation"] != _read(root / "control/continuation.json")
        or record["owner"].get("owner_token") != owner_token
        or (root / "control/STOP").exists()
        or (root / "control/HANDOFF").exists()
        or not record["admitted_at"] <= now < record["cutoff"]
    ):
        raise ValueError("Short round entry cutoff or control changed before commit")
    _write(path, record | {"committed_at": now})
    return now


def _window_path(root, admitted, attempt):
    key = hashlib.sha256(str(attempt).encode()).hexdigest()
    return root / "control/round-admissions" / admitted["iteration"] / (key + ".json")


def frozen_execution_deadline(root, *, owner_token, attempt):
    """Read an existing window bound to this active round and exact attempt."""
    root = Path(root)
    admitted = active_round(root, owner_token=owner_token)
    if not admitted:
        raise ValueError("Execution window requires a committed short round")
    saved = _read(_window_path(root, admitted, attempt))
    if (
        saved.get("token") != admitted["token"]
        or saved.get("attempt") != str(attempt)
        or type(saved.get("deadline")) not in (int, float)
        or not math.isfinite(saved["deadline"])
    ):
        raise ValueError("Execution window belongs to another admitted attempt")
    return saved["deadline"]


def freeze_execution_deadline(root, deadline, *, owner_token, attempt):
    """Persist an independently bounded attempt deadline without renewing it."""
    root = Path(root)
    admitted = active_round(root, owner_token=owner_token)
    if not admitted:
        raise ValueError("Execution window requires a committed short round")
    path = _window_path(root, admitted, attempt)
    if path.exists():
        return frozen_execution_deadline(root, owner_token=owner_token, attempt=attempt)
    if (
        type(deadline) not in (int, float)
        or not math.isfinite(deadline)
        or deadline > time.time() + 3600
    ):
        raise ValueError("Execution window exceeds the per-attempt timeout")
    _write(
        path,
        {"token": admitted["token"], "attempt": str(attempt), "deadline": deadline},
    )
    return deadline


def execution_deadline(root, original, seconds, *, owner_token=None, attempt):
    """Freeze one attempt's runtime bound; recovery never renews this window."""
    root = Path(root)
    admitted = active_round(root, owner_token=owner_token)
    if not admitted or original != admitted["cutoff"]:
        return original
    path = _window_path(root, admitted, attempt)
    if path.exists():
        return frozen_execution_deadline(root, owner_token=owner_token, attempt=attempt)
    if (
        type(seconds) not in (int, float)
        or not math.isfinite(seconds)
        or not 0 < seconds <= 3600
    ):
        raise ValueError(
            "A finite per-attempt timeout of at most 3600 seconds is required"
        )
    return freeze_execution_deadline(
        root, time.time() + seconds, owner_token=owner_token, attempt=attempt
    )
