"""Opt-in, durable admission of all ProTok GPU channels against one allowance.

Concurrent allocations reserve their full scheduler bounds and safety margins. This is an
admission cap: Slurm cancellation or scheduler overruns cannot be guaranteed by
the controller. Unknown effects retain their reservation; overruns stop admission.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

CHANNELS = frozenset({"formal", "research", "debug", "long", "manual"})
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
POLICY = "shared-gpu-budget-policy.json"
LEDGER = "shared-gpu-budget.json"


class SharedBudgetRefusal(ValueError):
    """Admission denied before the native submit function was called."""


def _integer(value, *, positive=False):
    return type(value) is int and value >= int(positive)


def _policy(value):
    if not isinstance(value, dict):
        raise ValueError("Shared GPU budget requires a policy object")
    anchor = value.get("historical_gpu_seconds")
    times = [value.get(name) for name in ("started_at", "deadline")]
    if (
        value.get("schema") != 1
        or not isinstance(value.get("budget_id"), str)
        or not value["budget_id"].strip()
        or not isinstance(value.get("authorization"), str)
        or not value["authorization"].strip()
        or value.get("baseline_settled") is not True
        or not isinstance(anchor, dict)
        or set(anchor) != CHANNELS
        or any(not _integer(item) for item in anchor.values())
        or not _integer(value.get("limit_gpu_seconds"), positive=True)
        or value["limit_gpu_seconds"] > 86400
        or value.get("overrun_reserve_seconds") != 300
        or any(
            type(item) not in (int, float) or not math.isfinite(item) for item in times
        )
        or not 0 < times[1] - times[0] <= 86400
    ):
        raise ValueError("Invalid shared GPU budget policy or unsettled baseline")
    return value


def _write(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w") as handle:
            json.dump(value, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _lock(control):
    with (control / "shared-gpu-budget.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def initialize_shared_budget(control, policy):
    """Activate only after the operator has reconciled all historical jobs.

    Historical totals are an immutable, excluded baseline; the new allowance
    begins at zero. The operator supplies these small accounting fields and must
    stop old controllers first. No existing ledger may be reset or rebound.
    """
    control = Path(control)
    policy = _policy(policy)
    control.mkdir(parents=True, exist_ok=True)
    with _lock(control):
        if (control / POLICY).exists() or (control / LEDGER).exists():
            raise ValueError("Shared GPU budget already exists; cannot reset allowance")
        _write(
            control / LEDGER,
            {"schema": 1, "policy": policy, "attempts": {}, "breached": False},
        )
        # A crash between publications leaves an incomplete activation that blocks.
        _write(control / POLICY, policy)


class SharedBudget:
    def __init__(self, control):
        self.control = Path(control)

    def _load(self):
        policy = _policy(json.loads((self.control / POLICY).read_text()))
        state = json.loads((self.control / LEDGER).read_text())
        if (
            state.get("schema") != 1
            or state.get("policy") != policy
            or type(state.get("breached")) is not bool
            or not isinstance(state.get("attempts"), dict)
        ):
            raise ValueError("Shared GPU budget policy/ledger changed or invalid")
        for key, attempt in state["attempts"].items():
            if (
                not isinstance(key, str)
                or not isinstance(attempt, dict)
                or attempt.get("channel") not in CHANNELS
                or not _integer(attempt.get("allocated_seconds"), positive=True)
                or not _integer(attempt.get("reserved_seconds"), positive=True)
                or attempt["reserved_seconds"]
                != attempt["allocated_seconds"] + policy["overrun_reserve_seconds"]
                or attempt.get("phase") not in {"submitting", "submitted", "settled"}
            ):
                raise ValueError("Malformed shared GPU reservation")
            if "no_allocation_certificate" in attempt:
                from integrations.protok.research_rejection import (
                    validate_budget_certificate,
                )

                validate_budget_certificate(self.control, key, attempt)
            elif attempt["phase"] == "settled":
                self._receipt(attempt["receipt"])
        return policy, state

    @staticmethod
    def _receipt(attempt):
        if (
            not _integer(attempt.get("gpu_seconds"))
            or attempt.get("terminal_state") not in TERMINAL_STATES
            or not re.fullmatch(r"[1-9][0-9]*", str(attempt.get("owned_job_id", "")))
            or not isinstance(attempt.get("exit_code"), str)
            or not re.fullmatch(r"\d+:\d+", attempt["exit_code"])
        ):
            raise ValueError(
                "Shared GPU settlement requires authoritative terminal accounting"
            )
        return {
            key: attempt[key]
            for key in ("gpu_seconds", "terminal_state", "exit_code", "owned_job_id")
        }

    @staticmethod
    def _status(policy, state):
        used = sum(
            item["receipt"]["gpu_seconds"]
            for item in state["attempts"].values()
            if item["phase"] == "settled" and "no_allocation_certificate" not in item
        )
        reserved = sum(
            item["reserved_seconds"]
            for item in state["attempts"].values()
            if item["phase"] != "settled" and "no_allocation_certificate" not in item
        )
        return {
            "budget_id": policy["budget_id"],
            "used_gpu_seconds": used,
            "reserved_gpu_seconds": reserved,
            "remaining_gpu_seconds": max(
                0, policy["limit_gpu_seconds"] - used - reserved
            ),
            "breached": state["breached"],
            "deadline": policy["deadline"],
            "started_at": policy["started_at"],
            "limit_gpu_seconds": policy["limit_gpu_seconds"],
            "overrun_reserve_seconds": policy["overrun_reserve_seconds"],
            "unresolved_submission": any(
                item["phase"] == "submitting"
                and "no_allocation_certificate" not in item
                for item in state["attempts"].values()
            ),
        }

    def status(self):
        with _lock(self.control):
            policy, state = self._load()
            return self._status(policy, state)

    def reserve(self, key, channel, seconds):
        if channel not in CHANNELS or not _integer(seconds, positive=True):
            raise ValueError(
                "Shared GPU reservation requires a channel and full allocation"
            )
        with _lock(self.control):
            policy, state = self._load()
            status = self._status(policy, state)
            reserved = seconds + policy["overrun_reserve_seconds"]
            if key in state["attempts"]:
                raise ValueError("Shared GPU allocation cannot be submitted twice")
            self._check_admission(policy, status, seconds)
            state["attempts"][key] = {
                "channel": channel,
                "allocated_seconds": seconds,
                "reserved_seconds": reserved,
                "phase": "submitting",
                "job_id": None,
            }
            _write(self.control / LEDGER, state)

    def _check_admission(self, policy, status, seconds):
        if not _integer(seconds, positive=True):
            raise ValueError("Full positive scheduler allocation required")
        reserved = seconds + policy["overrun_reserve_seconds"]
        now = time.time()
        if (
            status["breached"]
            or status["unresolved_submission"]
            or status["remaining_gpu_seconds"] < reserved
            or not policy["started_at"] <= now
            or now + reserved > policy["deadline"]
            or (self.control / "STOP").exists()
        ):
            raise SharedBudgetRefusal(
                "Shared GPU budget exhausted, cutoff reached, or unresolved allocation"
            )

    def check_admission(self, seconds):
        """Early refusal before staging; atomic reservation repeats at submit."""
        with _lock(self.control):
            policy, state = self._load()
            self._check_admission(policy, self._status(policy, state), seconds)

    def certify_no_allocation(self, key, certificate_path):
        """Project certified scheduler rejection without changing raw accounting."""
        from integrations.protok.research_rejection import validate_budget_certificate

        with _lock(self.control):
            _, state = self._load()
            saved = state["attempts"].get(key)
            if (
                saved is None
                or saved["phase"] != "submitting"
                or saved.get("job_id") is not None
                or "receipt" in saved
            ):
                raise ValueError(
                    "No-allocation certificate requires an unresolved reservation"
                )
            certificate = str(certificate_path)
            if "no_allocation_certificate" in saved:
                if saved["no_allocation_certificate"] != certificate:
                    raise ValueError("No-allocation certificate cannot be replaced")
                return
            projected = saved | {"no_allocation_certificate": certificate}
            validate_budget_certificate(self.control, key, projected)
            state["attempts"][key] = projected
            _write(self.control / LEDGER, state)

    def reconcile(self, key, attempt):
        with _lock(self.control):
            policy, state = self._load()
            saved = state["attempts"].get(key)
            if saved is None:
                raise ValueError(
                    "Allocation has no shared GPU reservation; cannot import unknown effects"
                )
            if "no_allocation_certificate" in saved:
                if (
                    attempt.get("status") != "submitting"
                    or attempt.get("owned_job_id") is not None
                    or attempt.get("job_id") is not None
                    or any(
                        name in attempt
                        for name in (
                            "gpu_seconds",
                            "terminal_state",
                            "exit_code",
                            "receipt",
                        )
                    )
                    or attempt.get("phase", "submitting") != "submitting"
                ):
                    raise ValueError(
                        "Certified no-allocation reservation cannot acquire accounting"
                    )
                return
            job = attempt.get("owned_job_id")
            if job is not None:
                if saved["job_id"] not in (None, job):
                    raise ValueError("Shared GPU reservation job identity changed")
                saved["job_id"] = job
                if saved["phase"] != "settled":
                    saved["phase"] = "submitted"
            if all(
                name in attempt
                for name in (
                    "gpu_seconds",
                    "terminal_state",
                    "exit_code",
                    "owned_job_id",
                )
            ):
                receipt = self._receipt(attempt)
                if saved["phase"] == "settled" and saved["receipt"] != receipt:
                    raise ValueError("Shared GPU terminal accounting changed")
                saved.update(phase="settled", receipt=receipt)
                state["breached"] |= (
                    receipt["gpu_seconds"] > saved["reserved_seconds"]
                    or self._status(policy, state)["used_gpu_seconds"]
                    > policy["limit_gpu_seconds"]
                )
            _write(self.control / LEDGER, state)


def shared_budget_enabled(control):
    control = Path(control)
    return (control / POLICY).exists() or (control / LEDGER).exists()


def check_shared_admission(control, seconds):
    if shared_budget_enabled(control):
        SharedBudget(control).check_admission(seconds)


def install_shared_budget(backend, control_dir, *, channel):
    """Wrap the trusted submission and accounting boundary, including recovery."""
    control = Path(control_dir)
    if not shared_budget_enabled(control):
        return
    if channel not in CHANNELS:
        raise ValueError("Unknown shared GPU budget channel")
    budget = SharedBudget(control)
    budget.status()  # Incomplete activation and policy mutation fail closed.
    submit, wait = backend.submit, backend.wait_for_job

    def identity(record, attempt):
        directory = attempt.get("directory", attempt.get("remote"))
        if not isinstance(directory, str) or not directory:
            raise ValueError(
                "Shared GPU reservation requires an immutable attempt directory"
            )
        return json.dumps([channel, str(Path(record).resolve()), directory])

    def submit_shared(config, record, state, attempt):
        key = identity(record, attempt)
        try:
            budget.reserve(key, channel, attempt.get("budget_seconds"))
        except SharedBudgetRefusal as error:
            # Trusted evidence of this invocation's non-effect. A prior identical
            # key is rejected separately and must never receive this marker.
            Path(record).mkdir(parents=True, exist_ok=True)
            _write(
                Path(record) / "shared-budget-refused.json",
                {
                    "schema": 1,
                    "key": key,
                    "channel": channel,
                    "attempt_directory": attempt.get("directory"),
                    "remote": attempt.get("remote"),
                    "budget_seconds": attempt.get("budget_seconds"),
                    "attempt_status": attempt.get("status"),
                    "native_submit_called": False,
                    "reserved": False,
                    "reason": str(error),
                    "at": time.time(),
                },
            )
            raise
        try:
            return submit(config, record, state, attempt)
        finally:
            budget.reconcile(key, attempt)

    def wait_shared(config, session, record, state, attempt):
        key = identity(record, attempt)
        status = budget.status()
        try:
            return wait(
                config,
                session | {"deadline": min(session["deadline"], status["deadline"])},
                record,
                state,
                attempt,
            )
        finally:
            budget.reconcile(key, attempt)

    backend.submit = submit_shared
    backend.wait_for_job = wait_shared
