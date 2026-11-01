"""Isolated, unscored ProTok GPU debug using the pinned native smoke gates."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
from pathlib import Path
import re
import shlex
import signal
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from integrations.protok.continuation import _load_backend, validate_continuation
from research_loop.debug_budget import DebugBudget


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Debug scope and attempt IDs require safe names")
    return value


def _policy(control):
    policy = read(control / "debug-policy.json")
    if (
        policy.get("schema") != 1
        or policy.get("gpu_seconds_per_investigation") != 1800
        or policy.get("independent_of_formal_budget") is not True
        or not policy.get("authorization")
    ):
        raise ValueError(
            "Explicit independent 1800-second debug authorization required"
        )
    return policy


def _live(control, owner_token, deadline):
    try:
        owner = read(control / "ownership.json")
        lease = read(control / "continuation.json")
        ends = (deadline, owner["expires_at"], lease["deadline"])
        valid = (
            owner.get("owner_token") == owner_token
            and lease.get("owner_token") == owner_token
            and owner.get("handoff_acknowledged") is True
            and all(type(end) in (int, float) and math.isfinite(end) for end in ends)
            and not (control / "STOP").exists()
            and time.time() < min(ends)
        )
    except Exception as error:
        raise KeyboardInterrupt(
            "Debug ownership/deadline control became unreadable"
        ) from error
    if not valid:
        raise KeyboardInterrupt("Debug owner, STOP or original wall deadline changed")


def bounded_smoke_script(original, config, remote, seed, seconds):
    """Retain native smoke/trainer/gates; amend only allocation/time receipt caps.

    Slurm rounds time limits to whole minutes, so admission supplies a whole-minute
    bound no larger than the remaining debug grant or original wall window.
    """
    script = original(config, remote, seed, smoke_only=True, include_smoke=False)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds_part = divmod(rest, 60)
    directive = "#SBATCH --time=00:30:00"
    wall = " 1800 " + shlex.quote(remote + "/wall-time.json")
    if script.count(directive) != 1 or script.count(wall) != 1:
        raise ValueError("Pinned native smoke operational layout changed")
    return script.replace(
        directive, f"#SBATCH --time={hours:02d}:{minutes:02d}:{seconds_part:02d}"
    ).replace(wall, f" {seconds} " + shlex.quote(remote + "/wall-time.json"))


def _last_exception(text):
    lines = [
        line.strip()
        for line in text.splitlines()
        if re.match(r"(?:[A-Za-z_][\w.]*)?(?:Error|Exception)(?::|$)", line.strip())
    ]
    return lines[-1] if lines else None


def candidate_cause(text):
    """Certify only a known native gate failure or an editable-source traceback."""
    cause = _last_exception(text)
    if not cause:
        return None
    known_gate = any(
        marker in cause
        for marker in (
            "Missing first-step active gradients",
            "Nonfinite gradient",
            "Declared inactive parameter has nonzero gradient",
            "Training objective had no nonzero active gradient",
            "Nonfinite deployed",
            "Nonfinite geometry gradient",
            "No trainable deployed geometry gradient",
            "Synthetic parameter update did not change deployed geometry",
            "Compiled parity failed",
        )
    )
    # Earlier handled exceptions are not evidence for the final failure's origin.
    traceback = text.rsplit("Traceback (most recent call last):", 1)[-1]
    editable_trace = (
        re.search(
            r'File "[^"\n]*/protok/model/autoresearch_se3(?:_loss)?\.py", line \d+',
            traceback,
        )
        is not None
    )
    return cause if known_gate or editable_trace else None


def failure_detail(log, attempt, error):
    """Read at most 32 KiB from this attempt's exact native log tail."""
    detail = ""
    try:
        if log.is_symlink():
            raise ValueError("Native debug log tail cannot be a symlink")
        with log.open("rb") as handle:
            handle.seek(max(0, log.stat().st_size - 32768))
            detail = handle.read(32768).decode("utf-8", errors="replace")
        detail = detail.encode("utf-8")[:32768].decode("utf-8", errors="ignore")
    except (OSError, ValueError):
        pass
    log_lines = [line.strip() for line in detail.splitlines() if line.strip()]
    cause = (
        _last_exception(detail)
        or (log_lines[-1] if log_lines else None)
        or error
        or "No exception detail was captured"
    )
    certified = candidate_cause(detail)
    terminal = attempt.get("terminal_state")
    accounted = all(
        name in attempt for name in ("gpu_seconds", "terminal_state", "exit_code")
    )
    if accounted and terminal in {"FAILED", "OUT_OF_MEMORY"} and certified:
        kind = "candidate"
    elif (
        accounted
        and terminal
        in {
            "CANCELLED",
            "TIMEOUT",
            "NODE_FAIL",
            "PREEMPTED",
            "BOOT_FAIL",
            "DEADLINE",
            "REVOKED",
            "SPECIAL_EXIT",
        }
        or any(
            marker in cause
            for marker in (
                "ConnectionError:",
                "ConnectionRefusedError:",
                "ConnectionResetError:",
                "TimeoutError:",
                "ModuleNotFoundError:",
                "ImportError:",
                "FileNotFoundError:",
                "PermissionError:",
                "Input/output error",
                "CUDA driver",
                "Missing accounting",
                "transport lost",
                "slurmstepd: error:",
            )
        )
    ):
        kind = "infrastructure"
    else:
        kind = "unknown"
    return {"failure_kind": kind, "cause": cause, "failure_detail": detail}


def run_debug(
    project,
    control,
    owner_token,
    config_path,
    debug_root,
    attempt_id,
    deadline,
    *,
    recover=False,
):
    """Run/reconcile one immutable debug attempt; never implicitly resubmit.

    The caller reuses debug_root for every retry of the same investigation.
    Completed/failed receipts are idempotent; unresolved submission/accounting
    requires recover=True on this same attempt. No formal session is created.
    """
    project, control, config_path, debug_root = (
        Path(value).resolve() for value in (project, control, config_path, debug_root)
    )
    _name(attempt_id)
    scope = _name(debug_root.name)
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError("Finite original debug wall deadline required")
    policy = _policy(control)
    record = debug_root / "attempts" / attempt_id
    existing = read(record / "debug.json") if (record / "debug.json").exists() else None
    if existing:
        # Initial admission already validated the unchanged formal contract. After
        # STOP/expiry only the pinned source identity and same receipt may resume.
        lease = read(control / "continuation.json")
        backend, canonical = _load_backend(project, lease)
        config = backend.load_config(config_path)
        owner = read(control / "ownership.json")
        if (
            existing["base_config"] != config
            or existing["deadline"] != deadline
            or existing["policy"] != policy
            or existing["owner"] != owner
            or existing["continuation"] != lease
            or owner.get("owner_token") != owner_token
            or owner.get("handoff_acknowledged") is not True
            or {k: v for k, v in config.items() if k != "workspace"}
            != {k: v for k, v in canonical.items() if k != "workspace"}
            or backend.config_identity(config) != lease["config_identity"]
        ):
            raise ValueError("Debug recovery differs from its admitted frozen contract")
    else:
        if recover:
            raise ValueError("No retained debug attempt to recover")
        backend, config, lease = validate_continuation(
            project, control, owner_token, config_path
        )
        if deadline > lease["effective_deadline"]:
            raise ValueError("Debug cannot extend the original owner/wall deadline")
        _live(control, owner_token, deadline)
    formal = Path(config["records_root"]).resolve()
    workspace = Path(config["workspace"]).resolve()
    for protected in (formal, project):
        if debug_root.is_relative_to(protected) or protected.is_relative_to(debug_root):
            raise ValueError(
                "Debug records must be isolated from formal records/source"
            )
    # The controller freezes candidates inside revisions/NNN/source alongside,
    # rather than inside, attempts/. Never place debug effects in source or let
    # an arbitrary workspace alias the records/control files.
    if debug_root.is_relative_to(workspace) or (
        workspace.is_relative_to(debug_root)
        and not (
            workspace.name == "source"
            and re.fullmatch(r"[0-9]{3,}", workspace.parent.name)
            and workspace.parent.parent == debug_root / "revisions"
        )
    ):
        raise ValueError("Debug records overlap the candidate workspace")
    candidates = backend.source_boundary(config)
    identity = backend.fingerprint(config, candidates)
    if existing and existing["fingerprint"] != identity:
        raise ValueError("Debug recovery candidate changed")
    debug_root.mkdir(parents=True, exist_ok=True)
    with (
        (control / "protok.lock").open("a") as allocation_lock,
        (debug_root / "debug.lock").open("a") as lock,
    ):
        fcntl.flock(allocation_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        budget = DebugBudget(debug_root, scope_id=scope, limit_gpu_seconds=1800)
        if (record / "receipt.json").exists():
            return read(record / "receipt.json")
        if existing and not recover:
            raise ValueError(
                "Retained debug effects require explicit same-attempt recovery"
            )
        if not existing:
            if budget.status()["active_attempt"] is not None:
                raise ValueError(
                    "Outstanding debug submission/accounting must be resolved first"
                )
            # Whole-minute allocation avoids Slurm rounding beyond a residual cap.
            seconds = int(min(budget.remaining(), deadline - time.time()) // 60) * 60
            if seconds < 60:
                raise ValueError("No whole Slurm minute remains in debug/wall budget")
            bound = budget.reserve(attempt_id, requested_seconds=seconds)
            record.mkdir(parents=True)
            existing = {
                "purpose": "debug",
                "fingerprint": identity,
                "base_config": config,
                "deadline": deadline,
                "policy": policy,
                "owner": read(control / "ownership.json"),
                "continuation": read(control / "continuation.json"),
                "allocated_gpu_seconds": bound,
            }
            write(record / "debug.json", existing)
            attempt = {
                "seed": 42,
                "directory": "seed-42-attempt-1",
                "mode": "smoke",
                "budget_seconds": bound,
                "status": "prepared",
                "remote": config["remote_root"]
                + "/research-loop-debug/"
                + scope
                + "/"
                + attempt_id,
                "include_smoke": False,
            }
            state = {
                "status": "reserved",
                "purpose": "debug",
                "mode": "smoke",
                "fingerprint": identity,
                "attempts": [attempt],
            }
            write(record / "state.json", state)
        else:
            bound = existing["allocated_gpu_seconds"]
            state = read(record / "state.json")
            attempt = state["attempts"][0]
        operational = config | {
            "records_root": str(debug_root / "attempts"),
            "remote_root": config["remote_root"] + "/research-loop-debug/" + scope,
            "time_limit": f"00:{bound // 60:02d}:00",
        }
        original_script, original_save, original_ssh = (
            backend.batch_script,
            backend.save_state,
            backend.ssh,
        )
        backend.batch_script = lambda current, remote, seed, **kwargs: (
            bounded_smoke_script(original_script, current, remote, seed, bound)
        )

        def save(current_record, current_state):
            phase = budget.status()["attempts"][attempt_id]["phase"]
            if attempt["status"] == "submitting" and phase == "reserved":
                budget.mark_submitting(attempt_id)
            if attempt.get("owned_job_id") and phase in {"reserved", "submitting"}:
                budget.mark_submitted(attempt_id, attempt["owned_job_id"])
            return original_save(current_record, current_state)

        watching = True

        def ssh(current, command, **kwargs):
            if watching and re.fullmatch(r"squeue -h -j [1-9][0-9]* -o %T", command):
                _live(control, owner_token, deadline)
            return original_ssh(current, command, **kwargs)

        backend.save_state, backend.ssh = save, ssh
        error, summary = None, None
        try:
            if not recover:
                backend.prepare(operational, candidates, record, attempt)
                _live(control, owner_token, deadline)
                if deadline - time.time() < bound:
                    raise RuntimeError(
                        "Original wall window no longer fits debug allocation"
                    )
                backend.submit(operational, record, state, attempt)
            elif attempt["status"] == "prepared":
                raise RuntimeError(
                    "Retained preparation cannot be implicitly submitted"
                )
            if attempt["status"] in {"running", "submitting"}:
                if recover:
                    watching = False
                backend.wait_for_job(
                    operational,
                    {"deadline": min(deadline, time.time()) if recover else deadline},
                    record,
                    state,
                    attempt,
                )
            if attempt["status"] not in {"finished", "complete"}:
                raise RuntimeError("Debug allocation failed or remains unreconciled")
            backend.collect(operational, record, attempt)
            summary = backend.summarize_seed(operational, record, attempt)
            if summary.get("quality_score_eligible") is not False:
                raise ValueError("Debug smoke must not be quality-score eligible")
            if backend.fingerprint(config, backend.source_boundary(config)) != identity:
                raise ValueError("Frozen debug source changed during execution")
        except BaseException as failure:
            error = f"{type(failure).__name__}: {failure}"
            if isinstance(failure, (KeyboardInterrupt, TimeoutError)):
                watching = False
                backend.cancel_interrupted(operational, record, state)
                if attempt["status"] in {"running", "submitting"}:
                    try:
                        backend.wait_for_job(
                            operational,
                            {"deadline": time.time()},
                            record,
                            state,
                            attempt,
                        )
                    except BaseException as cancelled:
                        error += (
                            f"; reconciliation: {type(cancelled).__name__}: {cancelled}"
                        )
        finally:
            if error is not None and attempt.get("owned_job_id"):
                try:
                    backend.collect_logs(operational, record, attempt)
                except Exception:
                    pass  # Missing logs never refund or resolve an allocation.
            phase = budget.status()["attempts"][attempt_id]["phase"]
            if attempt.get("owned_job_id") and phase == "submitting":
                budget.mark_submitted(attempt_id, attempt["owned_job_id"])
            if all(
                name in attempt
                for name in ("gpu_seconds", "terminal_state", "exit_code")
            ):
                budget.settle(
                    attempt_id,
                    gpu_seconds=attempt["gpu_seconds"],
                    terminal_state=attempt["terminal_state"],
                    exit_code=attempt["exit_code"],
                )
            elif phase == "reserved" and not recover:
                budget.release(attempt_id)
        settled = budget.status()["attempts"][attempt_id]["phase"] in {
            "settled",
            "released",
        }
        if budget.status()["breached"]:
            error = (
                error + "; " if error else ""
            ) + "Authoritative accounting exceeded debug allocation/grant"
        receipt = {
            "purpose": "debug",
            "quality_score_eligible": False,
            "status": "completed"
            if summary is not None and error is None
            else "failed"
            if settled
            else "blocked",
            "verified": summary is not None and error is None,
            "fingerprint": identity,
            "attempt_id": attempt_id,
            "allocated_gpu_seconds": bound,
            "gpu_seconds": attempt.get("gpu_seconds"),
            "terminal_state": attempt.get("terminal_state"),
            "exit_code": attempt.get("exit_code"),
            "owned_job_id": attempt.get("owned_job_id"),
            "error": error,
            "summary": summary,
            "budget": budget.status(),
            "evidence": [str(record / "state.json"), str(record / "debug.json")],
        }
        log = record / attempt["directory"] / "run.log.tail"
        if log.exists():
            receipt["evidence"].append(str(log))
        if receipt["status"] != "completed":
            receipt.update(failure_detail(log, attempt, error))
            if receipt["cause"] not in (receipt["error"] or ""):
                receipt["error"] = (
                    receipt["error"] + "; " if receipt["error"] else ""
                ) + receipt["cause"]
        write(record / ("receipt.json" if settled else "blocked.json"), receipt)
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("project", "control", "config", "debug-root"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--owner-token", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--deadline", type=float, required=True)
    parser.add_argument("--recover", action="store_true")
    args = parser.parse_args(argv)

    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, interrupted)
    try:
        result = run_debug(
            args.project,
            args.control,
            args.owner_token,
            args.config,
            args.debug_root,
            args.attempt_id,
            args.deadline,
            recover=args.recover,
        )
    except (Exception, KeyboardInterrupt) as error:
        result = {
            "purpose": "debug",
            "quality_score_eligible": False,
            "status": "blocked",
            "verified": False,
            "error": f"{type(error).__name__}: {error}",
            "failure_kind": "unknown",
            "cause": f"{type(error).__name__}: {error}",
        }
    print(json.dumps(result, allow_nan=False))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
