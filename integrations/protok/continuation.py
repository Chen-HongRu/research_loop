"""Trusted, leased continuation of ProTok's immutable scored backend.

Only operator admission, incremental budget and deadline hooks change. Scoring
configuration, source identity, historical records and GPU receipts remain fixed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time
from types import ModuleType

# The evaluator launches this trusted file with Python -I, outside the release
# directory. Resolve sibling controller packages from this file, never the CWD.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _read(path):
    return json.loads(Path(path).read_text())


def _finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _fresh_id(fresh_attempt):
    if fresh_attempt is not None and (
        not isinstance(fresh_attempt, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", fresh_attempt)
    ):
        raise ValueError("Fresh attempt requires a safe explicit allocation ID")
    return fresh_attempt


def _record_path(records, identity, fresh_attempt):
    return records / (
        identity if fresh_attempt is None else f"{identity}--{fresh_attempt}"
    )


def _fresh_state(record, identity, fresh_attempt, owner_token):
    if record.is_symlink() or (record / "state.json").is_symlink():
        raise ValueError("Fresh allocation record cannot alias another record")
    state = _read(record / "state.json")
    if (
        state.get("fingerprint") != identity
        or state.get("fresh_attempt") != fresh_attempt
        or state.get("mode") != "scored"
        or state.get("baseline") is not False
        or state.get("controller_lease", {}).get("owner_token") != owner_token
    ):
        raise ValueError("Fresh allocation identity or owning lease does not match")
    return state


def _validate_incremental_anchor(control, states):
    """Check small receipt metadata with the pinned backend's accounting rules."""
    anchor = control.get("initial_record_gpu_seconds")
    initial = control.get("initial_gpu_seconds")
    count = control.get("initial_scored_records")
    if (
        not isinstance(anchor, dict)
        or not anchor
        or not _finite_number(initial)
        or initial < 0
        or type(count) is not int
        or count < 0
        or any(not _finite_number(value) or value < 0 for value in anchor.values())
        or sum(anchor.values()) != initial
    ):
        raise ValueError("Continuation historical accounting anchor mismatch")
    by_name = {path.name: state for path, state in states}
    if (
        not anchor.keys() <= by_name.keys()
        or sum(by_name[name].get("mode") == "scored" for name in anchor) != count
    ):
        raise ValueError("Continuation historical record anchor mismatch")
    for name, state in by_name.items():
        if name not in anchor:
            if (
                state.get("controller_lease", {}).get("owner_token")
                != control["owner_token"]
            ):
                raise ValueError("Unanchored record belongs to another continuation")
            continue
        # Match used_gpu_seconds in the SHA-pinned backend. Prepared attempts have
        # no allocation; every other attempt requires a terminal accounting receipt.
        used = 0
        for attempt in state.get("attempts", []):
            if attempt.get("status") == "prepared":
                continue
            seconds = attempt.get("gpu_seconds")
            if (
                not _finite_number(seconds)
                or seconds < 0
                or not attempt.get("terminal_state")
            ):
                raise ValueError("Historical allocation receipt is unreconciled")
            used += seconds
        if used < anchor[name]:
            raise ValueError("Historical record GPU accounting decreased below anchor")


def _original_session(backend, config, control):
    relocation = control.get("relocation")
    if control["schema"] != 2 or relocation is None:
        return backend.session_state(config)
    records = Path(config["records_root"])
    if (
        not isinstance(relocation, dict)
        or set(relocation) != {"previous_records_root", "current_records_root"}
        or not isinstance(relocation["previous_records_root"], str)
        or not Path(relocation["previous_records_root"]).is_absolute()
        or relocation["current_records_root"] != str(records.resolve())
        or str(records) != str(records.resolve())
        or relocation["previous_records_root"] == relocation["current_records_root"]
    ):
        raise ValueError("Invalid explicit continuation records relocation")
    session = _read(records / "session.json")
    if session != control.get("original_session") or not session.get(
        "formal_config_fingerprint"
    ):
        raise ValueError("Relocated original session changed or was not formalized")
    # Bind the declared old location to the preserved amendment metadata. Their
    # path-sensitive backend checks cannot run against a relocated records root.
    for name in ("protocol-amendment.json", "resource-amendment.json"):
        amendment = _read(records / name)
        if (
            amendment.get("original_session") != session
            or amendment.get("previous_config", {}).get("records_root")
            != relocation["previous_records_root"]
            or (
                name == "protocol-amendment.json"
                and amendment.get("approved_config_fingerprint")
                != control["config_identity"]
            )
        ):
            raise ValueError("Relocation differs from preserved amendment metadata")
    return session


def _load_backend(project, control):
    path = project / "scripts/autoresearch_se3_eval.py"
    source = path.read_bytes()
    expected = control["evaluator_sha256"]
    canonical = _read(project / "autoresearch-run.json")
    if (
        hashlib.sha256(source).hexdigest() != expected
        or canonical["locked_files"]["scripts/autoresearch_se3_eval.py"] != expected
    ):
        raise ValueError("Protected evaluator SHA does not match continuation")
    backend = ModuleType("_protok_fixed_backend")
    backend.__file__ = str(path)
    exec(compile(source, str(path), "exec"), backend.__dict__)
    return backend, canonical


def validate_continuation(
    project,
    control_dir,
    owner_token,
    config_path,
    *,
    now=None,
    recover=False,
    fresh_attempt=None,
    execution_deadline=None,
):
    """Validate the lease and unchanged original scientific/budget contract."""
    fresh_attempt = _fresh_id(fresh_attempt)
    project, control_dir = Path(project).resolve(), Path(control_dir).resolve()
    control = _read(control_dir / "continuation.json")
    owner = _read(control_dir / "ownership.json")
    if (
        type(control.get("schema")) is not int
        or control["schema"] not in (1, 2)
        or owner.get("schema") != 1
        or control.get("project") != str(project)
        or owner.get("project") != str(project)
        or not owner_token
        or control.get("owner_token") != owner_token
        or owner.get("owner_token") != owner_token
        or owner.get("handoff_acknowledged") is not True
        or not owner.get("acknowledged_at")
        or not owner.get("previous_owner")
        or not owner.get("evidence")
        or type(owner.get("expires_at")) not in (int, float)
        or not math.isfinite(owner["expires_at"])
    ):
        raise ValueError("Continuation ownership lease is not active or does not match")
    if not recover and (control_dir / "STOP").exists():
        raise ValueError("Continuation is paused")
    start, end = control.get("started_at"), control.get("deadline")
    incremental = control["schema"] == 2
    seconds = 86400 if incremental else 21600
    if (
        type(start) not in (int, float)
        or type(end) not in (int, float)
        or not math.isfinite(start)
        or not math.isfinite(end)
        or not 0 < end - start <= seconds
        or control.get("seconds") != seconds
        or control.get("max_new_candidates", "missing") is not None
        or not control.get("authorization")
        or not control.get("previous_study")
    ):
        raise ValueError(
            "Continuation must authorize its bounded wall window without a count cap"
        )
    if incremental and (
        not isinstance(control.get("author"), str)
        or not control["author"].strip()
        or not _finite_number(control.get("additional_gpu_seconds"))
        or not 0 < control["additional_gpu_seconds"] <= 86400
    ):
        raise ValueError(
            "Incremental continuation requires author and bounded GPU grant"
        )
    now = time.time() if now is None else now
    if not _finite_number(now) or now < start:
        raise ValueError("Continuation has not started")
    backend, canonical = _load_backend(project, control)
    config = backend.load_config(config_path)
    # Only workspace may change, so no alternate ledger, transport or launch switch
    # can be smuggled through the backend's broader operational exclusions.
    if {k: v for k, v in config.items() if k != "workspace"} != {
        k: v for k, v in canonical.items() if k != "workspace"
    }:
        raise ValueError("Runtime config may retarget only the candidate workspace")
    workspace = Path(config["workspace"]).resolve()
    if control_dir == workspace or control_dir.is_relative_to(workspace):
        raise ValueError(
            "Trusted continuation control must be outside candidate workspace"
        )
    if backend.config_identity(config) != control.get("config_identity"):
        raise ValueError("Continuation scoring configuration identity mismatch")
    session = _original_session(backend, config, control)
    if session != control.get("original_session") or (
        not incremental and end > session["deadline"]
    ):
        raise ValueError(
            "Original session changed or continuation exceeds original deadline"
        )
    off = project / ".autoresearch-off"
    if not recover and (
        "prior_off" not in control
        or (_read(off) if off.exists() else None) != control["prior_off"]
    ):
        raise ValueError(
            "Project stop marker differs from acknowledged historical marker"
        )
    stat = off.stat() if off.exists() else None
    observed_stat = (
        {name: getattr(stat, "st_" + name) for name in ("mtime_ns", "size", "ino")}
        if stat
        else None
    )
    if not recover and observed_stat != control.get("prior_off_stat"):
        raise ValueError("Historical stop marker metadata changed")
    deadline = min(end, owner["expires_at"])
    if not incremental:
        deadline = min(deadline, session["deadline"])
    from integrations.protok.round_admission import active_round

    admitted = active_round(control_dir.parent, owner_token=owner_token)
    if incremental and not recover and now >= deadline and not admitted:
        raise ValueError("Incremental continuation lease has expired")
    identity = None
    if execution_deadline is not None:
        if (
            not admitted
            or not _finite_number(execution_deadline)
            or execution_deadline > now + 3600
        ):
            raise ValueError("Execution deadline requires a committed short round")
        from integrations.protok.backend import allocation_record
        from integrations.protok.round_admission import frozen_execution_deadline

        identity = backend.fingerprint(config, backend.source_boundary(config))
        attempt = "formal/" + allocation_record(identity, fresh_attempt)
        try:
            frozen_deadline = frozen_execution_deadline(
                control_dir.parent, owner_token=owner_token, attempt=attempt
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ValueError(
                "Formal execution requires its frozen attempt window"
            ) from error
        if execution_deadline != frozen_deadline:
            raise ValueError(
                "Formal execution deadline differs from its frozen attempt window"
            )
        deadline = frozen_deadline
    control = control | {"effective_deadline": deadline}
    records = Path(config["records_root"])
    states = backend.records_state(records)
    scored = [state for _, state in states if state.get("mode") == "scored"]
    if incremental:
        _validate_incremental_anchor(control, states)
    elif (
        type(control.get("initial_scored_records")) is not int
        or control["initial_scored_records"] != 12
        or len(scored) < control["initial_scored_records"]
        or control.get("initial_gpu_seconds") != 11660
    ):
        raise ValueError("Continuation historical accounting anchor mismatch")
    if recover or fresh_attempt is not None:
        if identity is None:
            identity = backend.fingerprint(config, backend.source_boundary(config))
        record = _record_path(records, identity, fresh_attempt)
        path = record / "state.json"
        state = (
            _fresh_state(record, identity, fresh_attempt, owner_token)
            if fresh_attempt is not None and record.exists()
            else _read(path)
            if path.exists()
            else {}
        )
    if recover:
        if state.get("controller_lease", {}).get(
            "owner_token"
        ) != owner_token or not any(
            attempt.get("status") in {"submitting", "running", "finished"}
            for attempt in state.get("attempts", [])
        ):
            raise ValueError(
                "Recovery requires this lease's outstanding matching fingerprint"
            )
    return backend, config, control


def install_hooks(
    backend,
    config,
    control,
    validate_lease,
    *,
    validate_poll=None,
    recover=False,
    fresh_attempt=None,
    control_dir=None,
):
    """Install narrow hooks; backend.evaluate acquires its original global lock."""
    fresh_attempt = _fresh_id(fresh_attempt)
    reserve, budget, wait, submit = (
        backend.reserve_record,
        backend.budget_check,
        backend.wait_for_job,
        backend.submit,
    )
    original_ssh = backend.ssh
    records = Path(config["records_root"])
    deadline = control["effective_deadline"]
    incremental = control["schema"] == 2
    accounted = control["initial_gpu_seconds"]
    waiting = False
    from integrations.protok.round_admission import active_round

    admitted = (
        active_round(Path(control_dir).parent, owner_token=control["owner_token"])
        if control_dir
        else None
    )
    validate_poll = validate_poll or validate_lease

    def check_owned_wait():
        try:
            validate_poll()
        except Exception as error:
            # The original evaluator catches this signal and cancels only its
            # receipted job. Ordinary validation errors would leave it running.
            raise KeyboardInterrupt(
                "Continuation ownership or stop state changed"
            ) from error

    def ssh(current, command, *, timeout=120):
        if (
            waiting
            and (not recover or admitted)
            and re.fullmatch(r"squeue -h -j [1-9][0-9]* -o %T", command)
        ):
            check_owned_wait()
        return original_ssh(current, command, timeout=timeout)

    def check_lease():
        validate_lease()

    def session_state(current, *, create=False, now=None, started_at=None):
        check_lease()
        if (
            current != config
            or create
            or (
                started_at is not None
                and started_at != control["original_session"]["started_at"]
            )
        ):
            raise ValueError("Relocated continuation cannot create or change a session")
        original = _read(records / "session.json")
        if original != control["original_session"]:
            raise ValueError("Relocated original session changed")
        return original | {"deadline": deadline}

    def reserve_record(current, identity, baseline_identity, *, smoke=False):
        check_lease()
        if smoke:
            raise ValueError(
                "Continuation authorizes scored candidates, not separate smokes"
            )
        record = _record_path(records, identity, fresh_attempt)
        if recover and not record.exists():
            raise ValueError("Receipt recovery cannot reserve a new candidate")
        states = backend.records_state(records)
        for path, state in states:
            if path == record:
                continue
            if state.get("status") in {"reserved", "incomplete"} or any(
                attempt.get("status")
                in {"prepared", "submitting", "running", "finished"}
                for attempt in state.get("attempts", [])
            ):
                raise RuntimeError(
                    "Recover the outstanding original fingerprint before another candidate"
                )
        if record.exists():
            # Completed caches and same-fingerprint receipt recovery need no new
            # admission and remain available after the continuation deadline.
            if fresh_attempt is not None:
                state = _fresh_state(
                    record, identity, fresh_attempt, control["owner_token"]
                )
                if recover and not any(
                    attempt.get("status") in {"submitting", "running", "finished"}
                    for attempt in state.get("attempts", [])
                ):
                    raise ValueError(
                        "Fresh recovery requires this allocation's outstanding attempt"
                    )
                return record, state
            return reserve(current, identity, baseline_identity, smoke=False)
        session = backend.session_state(current)
        budget_check(current, session, records, 1)
        count = sum(state.get("mode") == "scored" for _, state in states)
        if fresh_attempt is not None:
            if identity != baseline_identity:
                baseline = records / baseline_identity / "state.json"
                if not baseline.exists() or _read(baseline).get("status") != "complete":
                    raise RuntimeError(
                        "Fresh protected baseline must complete before candidates"
                    )
            record.mkdir()
            state = {
                "status": "reserved",
                "fingerprint": identity,
                "fresh_attempt": fresh_attempt,
                "mode": "scored",
                "baseline": False,
                "attempts": [],
                "created_at": time.time(),
            }
        else:
            record, state = reserve(
                current | {"max_evaluations": count + 1},
                identity,
                baseline_identity,
                smoke=False,
            )
        state["controller_lease"] = {
            "owner_token": control["owner_token"],
            "started_at": control["started_at"],
            "deadline": min(
                control["deadline"], control["original_session"]["deadline"]
            )
            if control["schema"] == 1
            else control["deadline"],
            "cumulative_scored_records": count + 1,
        }
        backend.save_state(record, state)
        return record, state

    def budget_check(
        current, session, record_root, allocations, *, now=None, smoke=False
    ):
        nonlocal accounted
        check_lease()
        if smoke:
            raise ValueError("Separate continuation smokes are not authorized")
        if incremental:
            # Config and immutable session retain their original <=24h limits.
            # This trusted hook alone admits the explicitly granted increment.
            now = time.time() if now is None else now
            if type(allocations) is not int or allocations <= 0:
                raise ValueError("Continuation requires positive whole allocations")
            bound = allocations * backend.duration(current["time_limit"])
            if not _finite_number(now) or deadline - now < bound:
                raise RuntimeError("Insufficient wall deadline for whole allocation")
            used = backend.used_gpu_seconds(record_root)
            from integrations.protok.research_execution import research_usage

            research = research_usage(control_dir) if control_dir is not None else None
            if research is not None:
                if research["outstanding"]:
                    raise RuntimeError(
                        "Unsettled research allocation prevents formal submission"
                    )
                used += research["gpu_seconds"]
            if not _finite_number(used) or used < accounted:
                raise RuntimeError("Cumulative GPU accounting decreased below anchor")
            accounted = used
            if (
                used
                - control["initial_gpu_seconds"]
                + bound
                + current["reserve_gpu_seconds"]
                > control["additional_gpu_seconds"]
            ):
                raise RuntimeError("Insufficient incremental GPU budget plus reserve")
            return used
        used = budget(
            current,
            session
            | {
                "deadline": deadline if admitted else min(session["deadline"], deadline)
            },
            record_root,
            allocations,
            now=now,
            smoke=False,
        )
        if used < control["initial_gpu_seconds"]:
            raise RuntimeError(
                "Cumulative GPU accounting decreased below historical anchor"
            )
        return used

    def wait_for_job(current, session, record, state, attempt):
        nonlocal waiting
        if recover:
            check_lease()
        else:
            check_owned_wait()
        wait_deadline = (
            deadline if admitted or incremental else min(session["deadline"], deadline)
        )
        if recover and not admitted:
            # Receipt-only recovery must reach accounting even after STOP/expiry.
            # The original wait polls first: terminal jobs are charged directly;
            # still-active owned jobs are cancelled immediately, then reconciled.
            wait_deadline = min(wait_deadline, time.time())
        waiting = True
        try:
            return wait(
                current,
                session | {"deadline": wait_deadline},
                record,
                state,
                attempt,
            )
        finally:
            # Original cancel_interrupted polls the same command during cleanup;
            # that path must remain available after stop/lease invalidation.
            waiting = False

    def submit_owned(current, record, state, attempt):
        if recover:
            raise ValueError("Receipt recovery cannot submit an allocation")
        check_lease()
        budget_check(current, backend.session_state(current), records, 1)
        return submit(current, record, state, attempt)

    backend.ssh = ssh
    backend.submit = submit_owned
    backend.reserve_record = reserve_record
    backend.budget_check = budget_check
    backend.wait_for_job = wait_for_job
    if incremental and control.get("relocation") is not None:
        backend.session_state = session_state


def launch(
    project,
    control_dir,
    owner_token,
    config_path,
    *,
    recover=False,
    fresh_attempt=None,
    execution_deadline=None,
):
    backend, config, control = validate_continuation(
        project,
        control_dir,
        owner_token,
        config_path,
        recover=recover,
        fresh_attempt=fresh_attempt,
        execution_deadline=execution_deadline,
    )

    def validate_lease(*, receipt_recovery=recover):
        # Lease changes are observed again while holding the backend's global lock.
        fresh_backend, fresh_config, fresh_control = validate_continuation(
            project,
            control_dir,
            owner_token,
            config_path,
            recover=receipt_recovery,
            fresh_attempt=fresh_attempt,
            execution_deadline=execution_deadline,
        )
        del fresh_backend
        if fresh_config != config or fresh_control != control:
            raise RuntimeError("Continuation control changed after admission")

    install_hooks(
        backend,
        config,
        control,
        validate_lease,
        validate_poll=lambda: validate_lease(receipt_recovery=False),
        recover=recover,
        fresh_attempt=fresh_attempt,
        control_dir=control_dir,
    )
    return backend.main(["--config", str(config_path), "--evaluate"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("project", "control", "owner-token", "config"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--evaluate", action="store_true", required=True)
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--fresh-attempt")
    parser.add_argument("--execution-deadline", type=float)
    args = parser.parse_args(argv)
    try:
        return launch(
            args.project,
            args.control,
            args.owner_token,
            args.config,
            recover=args.recover,
            fresh_attempt=args.fresh_attempt,
            execution_deadline=args.execution_deadline,
        )
    except (Exception, KeyboardInterrupt) as error:
        # Do not print exception payloads: trusted receipts may contain owner tokens.
        print(f"continuation_failed: {type(error).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
