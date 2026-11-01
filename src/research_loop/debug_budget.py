"""Persistent accounting for one-GPU debug allocations within one investigation."""

from __future__ import annotations

import fcntl
import json
import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path


TERMINAL_STATES = frozenset(
    {
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "TIMEOUT",
        "OUT_OF_MEMORY",
        "NODE_FAIL",
        "PREEMPTED",
        "BOOT_FAIL",
        "DEADLINE",
        "REVOKED",
        "SPECIAL_EXIT",
    }
)


def _name(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _seconds(value, label, *, positive=False):
    if type(value) is not int or value < (1 if positive else 0):
        raise ValueError(
            f"{label} must be a {'positive' if positive else 'nonnegative'} integer"
        )
    return value


class DebugBudget:
    """Charge actual GPU seconds monotonically; unresolved allocations block reuse.

    Every retry of an investigation must reopen the same directory and scope.
    Reserving does not submit a job. The caller must persist mark_submitting before
    crossing that boundary, and reconcile its job ID and terminal accounting before
    settling. This ledger does not alter the caller's original wall deadline.
    """

    def __init__(self, directory: Path, *, scope_id: str, limit_gpu_seconds: int):
        self.directory = Path(directory)
        self.scope_id = _name(scope_id, "scope_id")
        self.limit = _seconds(limit_gpu_seconds, "limit_gpu_seconds", positive=True)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "debug-budget.json"
        with self._lock():
            if self.path.exists():
                self._load()
            else:
                self._save(
                    {
                        "version": 1,
                        "scope_id": self.scope_id,
                        "limit_gpu_seconds": self.limit,
                        "used_gpu_seconds": 0,
                        "breached": False,
                        "active_attempt": None,
                        "attempts": {},
                    }
                )

    @contextmanager
    def _lock(self):
        with (self.directory / "debug-budget.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def _load(self):
        state = json.loads(self.path.read_text(encoding="utf-8"))
        if (
            not isinstance(state, dict)
            or state.get("version") != 1
            or state.get("scope_id") != self.scope_id
            or state.get("limit_gpu_seconds") != self.limit
        ):
            raise ValueError("Debug budget configuration differs from persisted state")
        _seconds(state.get("used_gpu_seconds"), "persisted used_gpu_seconds")
        if type(state.get("breached")) is not bool or not isinstance(
            state.get("attempts"), dict
        ):
            raise ValueError("Invalid debug budget state")
        active = state.get("active_attempt")
        if active is not None:
            attempt = state["attempts"].get(active)
            if not isinstance(attempt, dict) or attempt.get("phase") not in {
                "reserved",
                "submitting",
                "submitted",
            }:
                raise ValueError("Invalid outstanding debug allocation")
            _seconds(
                attempt.get("allocated_gpu_seconds"),
                "persisted allocation",
                positive=True,
            )
        return state

    def _save(self, state):
        temporary = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
            directory = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def _status(self, state):
        active = state["active_attempt"]
        reserved = state["attempts"][active]["allocated_gpu_seconds"] if active else 0
        remaining = (
            0
            if state["breached"]
            else max(0, self.limit - state["used_gpu_seconds"] - reserved)
        )
        return {
            **state,
            "reserved_gpu_seconds": reserved,
            "remaining_gpu_seconds": remaining,
            "exhausted": state["breached"] or state["used_gpu_seconds"] >= self.limit,
        }

    def status(self) -> dict:
        with self._lock():
            return self._status(self._load())

    def remaining(self) -> int:
        return self.status()["remaining_gpu_seconds"]

    def reserve(self, attempt_id: str, requested_seconds: int | None = None) -> int:
        _name(attempt_id, "attempt_id")
        if requested_seconds is not None:
            _seconds(requested_seconds, "requested_seconds", positive=True)
        with self._lock():
            state = self._load()
            prior = state["attempts"].get(attempt_id)
            if prior is not None:
                if prior["phase"] != "reserved":
                    raise ValueError(
                        "Attempt already crossed submission or closed; it cannot be reserved again"
                    )
                if prior["requested_seconds"] != requested_seconds:
                    raise ValueError("Conflicting reservation for the same attempt")
                return prior["allocated_gpu_seconds"]
            if state["active_attempt"] is not None:
                raise ValueError(
                    "Outstanding debug submission/accounting must be resolved first"
                )
            remaining = self._status(state)["remaining_gpu_seconds"]
            if remaining <= 0:
                raise ValueError("Debug GPU budget exhausted")
            allocated = min(remaining, requested_seconds or remaining)
            state["attempts"][attempt_id] = {
                "phase": "reserved",
                "requested_seconds": requested_seconds,
                "allocated_gpu_seconds": allocated,
                "job_id": None,
            }
            state["active_attempt"] = attempt_id
            self._save(state)
            return allocated

    def _active(self, state, attempt_id):
        _name(attempt_id, "attempt_id")
        if state["active_attempt"] != attempt_id:
            raise ValueError("Attempt does not own the outstanding debug allocation")
        return state["attempts"][attempt_id]

    def mark_submitting(self, attempt_id: str):
        with self._lock():
            state = self._load()
            attempt = self._active(state, attempt_id)
            if attempt["phase"] == "reserved":
                attempt["phase"] = "submitting"
                self._save(state)

    def mark_submitted(self, attempt_id: str, job_id: str):
        _name(job_id, "job_id")
        with self._lock():
            state = self._load()
            attempt = self._active(state, attempt_id)
            if attempt["phase"] == "reserved":
                raise ValueError(
                    "Persist mark_submitting before submitting a debug job"
                )
            if attempt["job_id"] not in (None, job_id):
                raise ValueError("Conflicting job ID for the same attempt")
            attempt.update(phase="submitted", job_id=job_id)
            self._save(state)

    def release(self, attempt_id: str) -> dict:
        """Close a known-unsubmitted reservation; forbidden after mark_submitting."""
        _name(attempt_id, "attempt_id")
        with self._lock():
            state = self._load()
            prior = state["attempts"].get(attempt_id)
            if prior is not None and prior["phase"] == "released":
                return self._status(state)
            attempt = self._active(state, attempt_id)
            if attempt["phase"] != "reserved":
                raise ValueError("Submission may have occurred; release is forbidden")
            attempt["phase"] = "released"
            state["active_attempt"] = None
            self._save(state)
            return self._status(state)

    def settle(
        self, attempt_id: str, *, gpu_seconds: int, terminal_state: str, exit_code: str
    ) -> dict:
        _name(attempt_id, "attempt_id")
        _seconds(gpu_seconds, "gpu_seconds")
        terminal_state = _name(terminal_state, "terminal_state").upper()
        if terminal_state not in TERMINAL_STATES:
            raise ValueError("Debug accounting requires a terminal job state")
        if not isinstance(exit_code, str) or not re.fullmatch(
            r"\d+(?::\d+)?", exit_code
        ):
            raise ValueError("Debug accounting requires a numeric exit code")
        if terminal_state == "COMPLETED" and any(
            int(part) for part in exit_code.split(":")
        ):
            raise ValueError("COMPLETED accounting cannot have a nonzero exit code")
        receipt = {
            "gpu_seconds": gpu_seconds,
            "terminal_state": terminal_state,
            "exit_code": exit_code,
        }
        with self._lock():
            state = self._load()
            prior = state["attempts"].get(attempt_id)
            if prior is not None and prior["phase"] == "settled":
                if prior["receipt"] != receipt:
                    raise ValueError(
                        "Conflicting terminal accounting for the same attempt"
                    )
                return self._status(state)
            attempt = self._active(state, attempt_id)
            if attempt["phase"] != "submitted":
                raise ValueError(
                    "Submission job ID and terminal accounting must be reconciled before settlement"
                )
            state["used_gpu_seconds"] += gpu_seconds
            state["breached"] |= (
                gpu_seconds > attempt["allocated_gpu_seconds"]
                or state["used_gpu_seconds"] > self.limit
            )
            attempt.update(phase="settled", receipt=receipt)
            state["active_attempt"] = None
            self._save(state)
            return self._status(state)
