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
from research_loop.diagnostics import (
    CommandDiagnostics,
    diagnostic_secrets,
    fetch_remote_log,
    diagnostic_view,
)


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
        from integrations.protok.round_admission import active_round

        admitted = active_round(control.parent, owner_token=owner_token)
        ends = (
            (deadline,)
            if admitted
            else (deadline, owner["expires_at"], lease["deadline"])
        )
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
    """Use the formal preflight gates without the setup-only baseline test suite.

    Slurm rounds time limits to whole minutes, so admission supplies a whole-minute
    bound no larger than the remaining debug grant or original wall window.
    """
    script = original(config, remote, seed, smoke_only=True, include_smoke=False)
    focused = [
        line
        for line in script.splitlines()
        if line.endswith(" " + shlex.quote(remote + "/focused-tests.json"))
    ]
    if len(focused) != 1:
        raise ValueError("Pinned native setup test layout changed")
    command = shlex.split(focused[0])
    if (
        len(command) != 4
        or command[:2] != [config["remote_python"], "-c"]
        or not all(
            name in command[2]
            for name in (
                "unittest",
                "test_autoresearch_se3_model.py",
                "test_autoresearch_se3_training.py",
            )
        )
    ):
        raise ValueError("Pinned native setup test command changed")
    # This suite compares against the original three-layer initialization. The
    # formal protocol permits architecture changes and does not run this suite.
    script = script.replace(focused[0] + "\n", "")
    hours, rest = divmod(seconds, 3600)
    minutes, seconds_part = divmod(rest, 60)
    directive = "#SBATCH --time=00:30:00"
    wall = " 1800 " + shlex.quote(remote + "/wall-time.json")
    if script.count(directive) != 1 or script.count(wall) != 1:
        raise ValueError("Pinned native smoke operational layout changed")
    return script.replace(
        directive, f"#SBATCH --time={hours:02d}:{minutes:02d}:{seconds_part:02d}"
    ).replace(wall, f" {seconds} " + shlex.quote(remote + "/wall-time.json"))


def collect_smoke(backend, config, record, attempt):
    """Collect the native smoke artifacts without inventing a setup-test pass."""
    root = record / attempt["directory"] / "artifacts"
    names = [
        "wall-time.json",
        "gpu-utilization.csv",
        "smoke/completion.json",
        "smoke/startup.json",
        "smoke/config.json",
        "smoke/args.json",
        "smoke/candidate_config.json",
        "smoke/convergence.json",
        "smoke/train.csv",
        "smoke/smoke-validation/step-3/eval.csv",
        "smoke/gate-initial.json",
        "smoke/gate-final.json",
    ]
    if config["compile"]:
        names.append("smoke/compile-parity.json")
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            backend.ssh(config, "cat " + shlex.quote(attempt["remote"] + "/" + name))
        )
    backend.collect_logs(config, record, attempt)


def summarize_smoke(backend, config, record, attempt):
    """Apply the native formal preflight's wall, real-update and gate contract.

    Debug reads the canonical inputs; formal still performs its own input staging
    and smoke in a fresh allocation. No debug checkpoint or metric is reused.
    """
    root = record / attempt["directory"] / "artifacts"
    wall = read(root / "wall-time.json")
    elapsed = wall.get("elapsed_seconds")
    if (
        wall.get("status") != "complete"
        or wall.get("budget_seconds") != attempt["budget_seconds"]
        or type(elapsed) not in (int, float)
        or not math.isfinite(elapsed)
        or not 0 < elapsed <= attempt["budget_seconds"]
    ):
        raise ValueError("Allocation did not complete within the per-seed cap")
    smoke = read(root / "smoke/completion.json")
    if (
        smoke.get("status") != "smoke_complete"
        or smoke.get("optimizer_step") != 3
        or smoke.get("quality_score_eligible") is not False
    ):
        raise ValueError("Missing ineligible three-update real smoke receipt")
    backend.gate_check(root, "smoke/gate-initial.json", attempt["seed"], 0)
    backend.gate_check(root, "smoke/gate-final.json", attempt["seed"], 3)
    if config["compile"]:
        parity = read(root / "smoke/compile-parity.json")
        expected = dict(
            status="pass",
            protocol=backend.PROTOCOL,
            seed=attempt["seed"],
            optimizer_step=3,
            proteins=2,
            k=4,
            training_and_evaluation_outputs_checked=True,
            declared_loss_checked=True,
            active_parameter_gradients_checked=True,
            rng_reset=True,
            weights_updated=False,
            compile_boundary="torch.compile(model, dynamic=True)",
        )
        if any(parity.get(key) != value for key, value in expected.items()):
            raise ValueError("Missing successful trained compile-parity gate")
    return dict(
        seed=attempt["seed"],
        mode="smoke",
        check_scope="formal_preflight",
        quality_score_eligible=False,
        completion=smoke,
        startup=read(root / "smoke/startup.json"),
        performance=backend.gpu_utilization(root / "gpu-utilization.csv"),
        gpu_seconds=attempt["gpu_seconds"],
        artifacts=str(root),
    )


def _last_exception(text):
    lines = [
        line.strip()
        for line in text.splitlines()
        if re.match(r"(?:[A-Za-z_][\w.]*)?(?:Error|Exception)(?::|$)", line.strip())
    ]
    return lines[-1] if lines else None


def candidate_cause(text, *, workspace=None, stage=None):
    """Bind the final exception to this attempt's frozen editable source.

    A frame in candidate code does not establish ownership of a missing shared
    dependency, dataset, permission or driver. Those failures remain blocking.
    """
    cause = _last_exception(text)
    if not cause:
        return None
    if any(
        marker in cause
        for marker in (
            "ConnectionError:",
            "ConnectionRefusedError:",
            "ConnectionResetError:",
            "TimeoutError:",
            "CUDA driver",
            "NVIDIA driver",
            "driver version is insufficient",
            "Input/output error",
        )
    ):
        return None
    traceback = text.rsplit("Traceback (most recent call last):", 1)[-1]
    frames = re.findall(r'File "([^"\n]+)", line \d+', traceback)
    from .backend import CANDIDATE_FILES

    # No inferred root from arbitrary log text: the caller supplies the exact
    # remote /code root recorded for this owned, already settled allocation.
    paths = (
        {str(Path(workspace) / name) for name in CANDIDATE_FILES}
        if workspace
        else set()
    )
    candidate_leaf = bool(
        frames and frames[-1] in paths and stage in {"formal", "smoke"}
    )
    if any(marker in cause for marker in ("ModuleNotFoundError:", "ImportError:")):
        # A misspelled candidate import can be fixed in the allowed file. An
        # earlier candidate caller does not own a shared library's failed import.
        last_frame = list(re.finditer(r'File "[^"\n]+", line \d+[^\n]*\n', traceback))
        statements = (
            traceback[last_frame[-1].end() :].splitlines() if last_frame else []
        )
        statement = statements[0].strip() if statements else ""
        return (
            cause
            if candidate_leaf and statement.startswith(("import ", "from "))
            else None
        )
    if any(marker in cause for marker in ("FileNotFoundError:", "PermissionError:")):
        # A concrete editable target is in scope; shared data or environment
        # permissions are not made reparable by an earlier candidate caller.
        targets = re.findall(r"['\"]([^'\"]+)['\"]", cause)
        return (
            cause
            if candidate_leaf and any(target in paths for target in targets)
            else None
        )
    if candidate_leaf:
        return cause
    candidate_calls = [index for index, path in enumerate(frames) if path in paths]
    if candidate_calls and stage in {"formal", "smoke"}:
        callees = frames[candidate_calls[-1] + 1 :]
        # Tensor/argument errors often end inside torch or the Python runtime.
        # Attribute those calls to the exact candidate callsite, while retaining
        # the boundary around protected project/data/scorer callbacks and
        # arbitrary shared-environment scripts. No library edits are authorized.
        library_calls = bool(callees) and all(
            ".." not in Path(path).parts
            and re.search(r"/(?:lib|lib64)/python[0-9]+\.[0-9]+/[^\n]+\.py$", path)
            for path in callees
        )
        operational_failure = re.search(
            r"driver|permission denied|no such file|input/output|"
            r"(?:could not|cannot|failed to) (?:load|open)|transport|"
            r"shared (?:data|dependency)|CUDA initialization",
            cause,
            re.IGNORECASE,
        )
        if library_calls and not operational_failure:
            return cause
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
    candidate_config_gate = any(
        marker in cause
        for marker in (
            "Candidate config may contain only architecture/loss fields",
            "Candidate architecture must be a nonempty string",
            "loss_chunk_size must be a positive integer",
            "Candidate loss must be scalar",
            "Candidate clean must have shape [B,L,3]",
        )
    )
    native_training = (
        workspace
        and frames
        and str(Path(workspace) / "scripts/train_autoresearch_se3.py") == frames[-1]
    )
    native_gate = bool(
        workspace
        and frames
        and stage in {"formal", "smoke"}
        and frames[-1]
        in {
            str(Path(workspace) / "scripts/train_autoresearch_se3.py"),
            str(Path(workspace) / "scripts/check_autoresearch_se3.py"),
        }
    )
    return (
        cause
        if (known_gate and native_gate)
        or (candidate_config_gate and native_training and stage in {"formal", "smoke"})
        else None
    )


def failure_detail(log, attempt, error, *, stage="smoke", secrets=()):
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
    remote = attempt.get("remote")
    certified = candidate_cause(
        detail,
        workspace=remote + "/code" if isinstance(remote, str) else None,
        stage=stage,
    )
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
    public = diagnostic_view(RuntimeError(detail), secrets=secrets)
    public_cause = diagnostic_view(RuntimeError(cause), secrets=secrets)
    return {
        "failure_kind": kind,
        "cause": public_cause["cause_chain"][0]["message"],
        "failure_detail": public["cause_chain"][0]["message"],
    }


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
    from integrations.protok.round_admission import execution_deadline

    wall_deadline = execution_deadline(
        control.parent,
        deadline,
        3600,
        owner_token=owner_token,
        attempt=f"debug/{scope}/{attempt_id}",
    )
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
        _live(control, owner_token, wall_deadline)
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
            seconds = (
                int(min(budget.remaining(), wall_deadline - time.time()) // 60) * 60
            )
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
        capture = CommandDiagnostics(
            record / "command-diagnostics", secrets=diagnostic_secrets((owner_token,))
        ).install(backend)
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
                _live(control, owner_token, wall_deadline)
            return original_ssh(current, command, **kwargs)

        backend.save_state, backend.ssh = save, ssh
        error, summary = None, None
        try:
            if not recover:
                backend.prepare(operational, candidates, record, attempt)
                _live(control, owner_token, wall_deadline)
                if wall_deadline - time.time() < bound:
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
                    {
                        "deadline": min(wall_deadline, time.time())
                        if recover and wall_deadline == deadline
                        else wall_deadline
                    },
                    record,
                    state,
                    attempt,
                )
            if attempt["status"] not in {"finished", "complete"}:
                raise RuntimeError("Debug allocation failed or remains unreconciled")
            collect_smoke(backend, operational, record, attempt)
            summary = summarize_smoke(backend, operational, record, attempt)
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
        if (
            settled
            and attempt.get("owned_job_id")
            and attempt.get("gpu_seconds") is not None
            and attempt.get("terminal_state")
        ):
            fetch_remote_log(backend, operational, attempt["remote"])
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
            "error": diagnostic_view(RuntimeError(error), secrets=capture.secrets)[
                "cause_chain"
            ][0]["message"]
            if error is not None
            else None,
            "diagnostics": capture.view(),
            "summary": summary,
            "budget": budget.status(),
            "evidence": [str(record / "state.json"), str(record / "debug.json")],
        }
        log = record / attempt["directory"] / "run.log.tail"
        if capture.view()["logs"]:
            receipt["evidence"].append(
                str(capture.directory / "diagnostic-manifest.json")
            )
        if receipt["status"] != "completed":
            receipt.update(
                failure_detail(
                    log, attempt, error, secrets=diagnostic_secrets((owner_token,))
                )
            )
            if "diagnostics" in receipt:
                receipt["diagnostics"]["owner"] = (
                    "workspace"
                    if receipt["failure_kind"] == "candidate"
                    else "pipeline"
                    if receipt["failure_kind"] == "infrastructure"
                    else "unknown"
                )
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
