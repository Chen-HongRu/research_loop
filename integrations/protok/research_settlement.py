"""Durable submission boundary and receipt-only closure for script Execute."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import json
from pathlib import Path
import re
from typing import Literal

from integrations.protok.backend import ProTokError
from research_loop.debug_budget import TERMINAL_STATES


def _read(path):
    return json.loads(Path(path).read_text())


def _write(path, value):
    from integrations.protok.research_backend import _write as write

    path.parent.mkdir(parents=True, exist_ok=True)
    write(path, value)


def _path(root, iteration):
    return Path(root) / "control/research-executions" / iteration / "execution.json"


def _configuration_identity(value):
    # Configuration metadata only; never read/hash computational artifacts.
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def _bound(root, workspace, iteration, attempt_ref):
    from integrations.protok.research_execution import frozen_contract

    root = Path(root)
    reference = Path(attempt_ref)
    if reference.is_absolute() or ".." in reference.parts or not reference.parts:
        raise ProTokError("Execution attempt must be a relative Study reference")
    state = _read(root / "study/state.json")
    return {
        "version": 1,
        "iteration": iteration,
        "attempt_ref": reference.as_posix(),
        "workspace": str(Path(workspace).resolve()),
        "plan_ref": state["plan_ref"],
        "contract": frozen_contract(root, iteration),
        "owner_identity": _configuration_identity(
            _read(root / "control/ownership.json")
        ),
        "continuation_identity": _configuration_identity(
            _read(root / "control/continuation.json")
        ),
    }


def begin_script_execution(root, workspace, iteration, attempt_ref, deadline):
    """Bind a fresh Execute before its monitor or author can request compute."""
    from integrations.protok.research_execution import frozen_contract

    contract = frozen_contract(root, iteration, required=False)
    if contract is None or contract["execution"]["mode"] != "script":
        return None
    if attempt_ref is None:
        raise ProTokError("Script Execute requires a retained Study attempt")
    binding = _bound(root, workspace, iteration, attempt_ref)
    root = Path(root)
    path = _path(root, iteration)
    with (root / "control/protok.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists() or any(
            (root / "control" / name / iteration).exists()
            for name in ("research-jobs", "research-sources")
        ):
            raise ProTokError("Existing script execution requires reconciliation")
        value = {
            "binding": binding,
            "deadline": deadline,
            "status": "active",
            "submission_intent": False,
        }
        _write(path, value)
        return value


def mark_script_submission(root, iteration, execution, deadline):
    """Called under protok.lock before invoking the external submission hook."""
    path = _path(root, iteration)
    if not path.exists():
        # Direct backend callers retain the same boundary in job.json instead.
        return
    value = _read(path)
    binding = value["binding"]
    current = _bound(root, binding["workspace"], iteration, binding["attempt_ref"])
    if (
        binding != current
        or binding["contract"]["execution"] != execution
        or value["deadline"] != deadline
        or value["status"] != "active"
        or value["submission_intent"] is not False
    ):
        raise ProTokError("Script submission differs from its active Execute binding")
    value["submission_intent"] = True
    _write(path, value)


def _no_submission(root, iteration):
    job = Path(root) / "control/research-jobs" / iteration
    if not job.exists():
        return
    record = _read(job / "job.json") if (job / "job.json").exists() else None
    state = _read(job / "state.json") if (job / "state.json").exists() else None
    # A crash can interrupt staging metadata publication, before the durable
    # submit boundary. Existing evidence may contradict no-submission; missing
    # staging metadata cannot contradict the fresh journal's explicit boundary.
    if record is None and state is None:
        return
    attempts = state.get("attempts", []) if state is not None else []
    receipt = _read(job / "receipt.json") if (job / "receipt.json").exists() else None
    if receipt is not None and (
        receipt.get("status") not in {"failed", "not_started"}
        or receipt.get("gpu_seconds") != 0
        or receipt.get("cpu_seconds") != 0
        or any(
            receipt.get(key) is not None
            for key in ("owned_job_id", "terminal_state", "exit_code")
        )
    ):
        raise ProTokError("No-submission boundary contradicts a retained receipt")
    if (
        (record is not None and record.get("submission_intent") is not False)
        or (state is not None and len(attempts) != 1)
        or any(
            attempt.get("status") != "prepared"
            or any(
                key in attempt
                for key in (
                    "owned_job_id",
                    "gpu_seconds",
                    "terminal_state",
                    "exit_code",
                )
            )
            for attempt in attempts
        )
    ):
        raise ProTokError("Script allocation may have started; reconcile its owned job")


def _close_prepared_job(root, iteration, journal):
    """Publish no-allocation accounting, never a synthetic Slurm job receipt."""
    job = Path(root) / "control/research-jobs" / iteration
    if not (job / "job.json").exists() or (job / "receipt.json").exists():
        return
    native = _read(job / "job.json")
    if not all(
        key in native
        for key in ("source_identity", "execution_identity", "allocated_seconds")
    ):
        return  # Incomplete pre-staging metadata has no accounting reservation.
    receipt = {
        "purpose": "research",
        "quality_score_eligible": False,
        "status": "not_started",
        "source_identity": native["source_identity"],
        "execution_identity": native["execution_identity"],
        "allocated_seconds": native["allocated_seconds"],
        "gpu_seconds": 0,
        "cpu_seconds": 0,
        "closure": {
            "path": str(_path(root, iteration)),
            "binding_identity": _configuration_identity(journal["binding"]),
        },
    }
    _write(job / "receipt.json", receipt)


def verify_not_started_receipt(root, iteration, receipt):
    """Validate small durable no-allocation metadata for cumulative accounting."""
    journal = _read(_path(root, iteration))
    if (
        journal.get("status") != "closed"
        or journal.get("submission_intent") is not False
        or receipt.get("status") != "not_started"
        or receipt.get("gpu_seconds") != 0
        or receipt.get("cpu_seconds") != 0
        or any(
            key in receipt for key in ("owned_job_id", "terminal_state", "exit_code")
        )
        or receipt.get("closure")
        != {
            "path": str(_path(root, iteration)),
            "binding_identity": _configuration_identity(journal["binding"]),
        }
    ):
        raise ProTokError("No-allocation accounting lacks its bound stopped closure")
    _no_submission(root, iteration)


def close_script_execution(root, workspace, iteration, attempt_ref, *, error=None):
    """Close after the owning Execute monitor has stopped; never submit or poll."""
    root = Path(root)
    path = _path(root, iteration)
    if not path.exists():
        return None
    with (root / "control/protok.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        value = _read(path)
        if value["binding"] != _bound(root, workspace, iteration, attempt_ref):
            raise ProTokError("Script Execute binding changed before closure")
        if value["status"] == "closed":
            if value["submission_intent"] is False:
                _no_submission(root, iteration)
                _close_prepared_job(root, iteration, value)
            return value
        if value["status"] != "active":
            raise ProTokError("Script Execute is not active")
        if value["submission_intent"] is False:
            _no_submission(root, iteration)
        elif value["submission_intent"] is not True:
            raise ProTokError("Script submission boundary is unresolved")
        value["status"] = "closed"
        if error is not None:
            from research_loop.diagnostics import redact_text

            value["failure"] = {
                "stage": "execute",
                "error": redact_text(
                    str(error),
                    secrets=(
                        _read(root / "control/ownership.json").get("owner_token", ""),
                    ),
                ),
            }
        _write(path, value)
        if value["submission_intent"] is False:
            _close_prepared_job(root, iteration, value)
        return value


@dataclass(frozen=True)
class ScriptSettlement:
    status: Literal["not_started", "completed", "failed"]
    result: dict
    proof: dict


def settle_script_execution(root, workspace, iteration, attempt_ref):
    """Verify a closed Execute against frozen receipts and original authored files."""
    from integrations.protok.research_execution import settled_script

    root = Path(root)
    path = _path(root, iteration)
    with (root / "control/protok.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not path.exists():
            raise ProTokError("Script Execute has no durable submission boundary")
        value = _read(path)
        if (
            value["binding"] != _bound(root, workspace, iteration, attempt_ref)
            or value["status"] != "closed"
        ):
            raise ProTokError("Script Execute has no matching stopped closure")
        proof = {"path": str(path), "binding": value["binding"]}
        if value["submission_intent"] is False:
            _no_submission(root, iteration)
            identity = hashlib.sha256(
                json.dumps(value, sort_keys=True, allow_nan=False).encode()
            ).hexdigest()
            result = {
                "status": "not_started",
                "purpose": "research",
                "quality_score_eligible": False,
                "gpu_seconds": 0,
                "cpu_seconds": 0,
                "owned_job_id": None,
                "outputs": [],
                "evidence": [str(path)],
                "artifact": {"path": str(path), "version": identity},
                "error": value.get("failure", {}).get("error"),
            }
            return ScriptSettlement("not_started", result, proof)
        if value["submission_intent"] is not True:
            raise ProTokError("Script submission boundary is unresolved")
        try:
            result = settled_script(root, workspace, iteration)
            job = root / "control/research-jobs" / iteration
            native = _read(job / "job.json")
            attempts = _read(job / "state.json").get("attempts", [])
        except (OSError, KeyError, ValueError) as error:
            raise ProTokError(
                "Script submission has unresolved effects; reconcile its owned job"
            ) from error
        keys = ("owned_job_id", "terminal_state", "exit_code", "gpu_seconds")
        if (
            native.get("submission_intent") is not True
            or len(attempts) != 1
            or any(attempts[0].get(key) != result.get(key) for key in keys)
            or any(result.get(key) is None for key in keys)
            or not re.fullmatch(r"[1-9][0-9]*", str(result["owned_job_id"]))
            or result["terminal_state"] not in TERMINAL_STATES
            or not re.fullmatch(r"[0-9]+:[0-9]+", str(result["exit_code"]))
            or type(result["gpu_seconds"]) is not int
            or result["gpu_seconds"] < 0
            or (
                result["status"] == "completed"
                and (result["terminal_state"], result["exit_code"])
                != ("COMPLETED", "0:0")
            )
        ):
            raise ProTokError("Script receipt lacks matching authoritative accounting")
        return ScriptSettlement(result["status"], result, proof)
