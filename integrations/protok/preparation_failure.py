"""Operator closure of a failed input-metadata probe before native execution."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time

from integrations.protok.backend import ProTokError
from integrations.protok.no_allocation import _bytes, _json, _publish

LEGACY_RELEASE = "/releases/7950256442d5bb0c5dacae049e4936d7d9ad9a39/"


def read(path):
    return json.loads(_bytes(path))


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _error(interface):
    diagnostic = interface.get("diagnostics", {})
    frames = [
        frame
        for cause in diagnostic.get("cause_chain", [])
        for frame in cause.get("frames", [])
    ]
    if (
        interface.get("status") != "error"
        or diagnostic.get("stage") not in {"formal", "evaluation_controller"}
        or diagnostic.get("returncode") != 255
        or any(name in interface for name in ("value", "metric", "measurements"))
        or not {"execute_script", "run_script", "_input_metadata"}.issubset(
            {frame.get("function") for frame in frames}
        )
        or not any(
            frame.get("function") == "_input_metadata"
            and str(frame.get("file", "")).endswith("/research_backend.py")
            for frame in frames
        )
    ):
        raise ProTokError("Only the retained input-metadata SSH failure may certify")


def _absence(control, iteration, *, published=False):
    for name in ("jobs", "debug", "research-jobs"):
        path = control / name / iteration
        if name == "research-jobs" and published:
            continue
        if path.exists() or path.is_symlink():
            raise ProTokError(
                "Native execution evidence prevents preparation certification"
            )
    ledger = control / "shared-gpu-budget.json"
    if ledger.exists():
        for key in read(ledger)["attempts"]:
            try:
                identity = json.loads(key)
            except ValueError:
                continue
            if (
                isinstance(identity, list)
                and len(identity) == 3
                and isinstance(identity[1], str)
                and Path(identity[1]).name == iteration
            ):
                raise ProTokError(
                    "A shared reservation prevents preparation certification"
                )


def _controller_error(interface, outer):
    causes = outer.get("cause_chain", [])
    top = causes[0] if causes else {}
    if (
        outer.get("owner") != "pipeline"
        or outer.get("stage") != "execute"
        or outer.get("effects") != "unknown"
        or top.get("type") != "HarnessError"
        or top.get("message") != interface.get("error")
        or interface.get("error") != "Command exited with status 255"
        or "server" not in outer
    ):
        raise ProTokError(
            "Original controller failure does not bind this preparation error"
        )
    if outer["server"] is not None:
        if outer["server"] != interface["diagnostics"]:
            raise ProTokError("Controller and workspace failure diagnostics differ")
        return "matched_server_diagnostics"
    required = {
        ("_call", "src/research_loop/core.py"),
        ("run", "integrations/protok/run.py"),
    }
    frames = {
        (frame.get("function"), suffix)
        for frame in top.get("frames", [])
        for _, suffix in required
        if LEGACY_RELEASE in frame.get("file", "")
        and frame["file"].endswith(LEGACY_RELEASE + suffix)
    }
    if (
        "returncode" not in outer
        or outer["returncode"] is not None
        or not required.issubset(frames)
    ):
        raise ProTokError(
            "Missing server diagnostics require the exact legacy controller release"
        )
    return "pinned_legacy_write_ahead_order"


def records(root, iteration, certificate):
    root = Path(root)
    folder = root / "control/research-sources" / iteration
    path = root / "control/research-jobs" / iteration / "preparation-failure.json"
    identity = "preparation:" + digest(certificate)
    common = {
        "record_kind": "research_preparation_failure",
        "iteration": iteration,
        "execution": certificate["source_record"]["contract"]["execution"],
        "identity_kind": "preparation_certificate",
        "execution_identity": identity,
        "native_execution_identity": None,
        "source_identity": "preparation-source:"
        + digest(certificate["source_record"]["source_signatures"]),
        "requested_seconds": certificate["source_record"]["contract"]["execution"][
            "time_limit_seconds"
        ],
        "allocated_seconds": 0,
        "purpose": "research",
        "quality_score_eligible": False,
        "status": "failed",
        "failure_kind": "pre_backend_transport",
        "preparation_certificate": str(path),
    }
    receipt = {
        **common,
        "gpu_seconds": 0,
        "cpu_seconds": 0,
        "outputs": [],
        "error": "Pre-backend preparation transport failed; no native execution or scientific result.",
        "evidence": [str(path)],
    }
    result = {
        **receipt,
        "source_commit": certificate["source_commit"],
        "frozen_source": str(folder / "source"),
        "contract": certificate["source_record"]["contract"],
        "artifact": {"path": str(folder / "result.json"), "version": identity},
    }
    return common, receipt, result


def _context(root, workspace, iteration):
    from integrations.protok import run
    from integrations.protok.research_backend import _execution
    from integrations.protok.research_execution import (
        frozen_contract,
        policy,
        research_files,
    )
    from integrations.protok.round_admission import (
        active_round,
        frozen_execution_deadline,
    )

    control = root / "control"
    study = run.Study(root / "study")
    state = study.status()
    if f"{state['iteration']:04d}" != iteration:
        raise ProTokError("Preparation certificate belongs to another iteration")
    owner, lease = read(control / "ownership.json"), read(control / "continuation.json")
    contract = frozen_contract(root, iteration)
    _execution(contract["execution"], policy(root))
    folder = control / "research-sources" / iteration
    saved = read(folder / "record.json")
    if saved["contract"] != contract:
        raise ProTokError("Frozen research contract changed")
    baseline = read(study.root / "study.json")["baseline"]
    for source in (workspace, folder / "source"):
        run.check_fixed_candidate(root, source, iteration, folder / "source")
        run.check_scope(source, baseline, allow_research=True)
        if (
            run.signatures({**run.candidate_bytes(source), **research_files(source)})
            != saved["source_signatures"]
        ):
            raise ProTokError("Research source changed after preparation failed")
    if read(workspace / "evaluation-submission.json") != {"action": "research"} or any(
        (workspace / name).exists() or (workspace / name).is_symlink()
        for name in ("evaluation-request.json", "evaluation-request.json.tmp")
    ):
        raise ProTokError("Preparation submission interface changed")
    admitted = active_round(root, owner_token=owner["owner_token"])
    if admitted is None:
        raise ProTokError("Preparation requires the original admitted investigation")
    deadline = frozen_execution_deadline(
        root, owner_token=owner["owner_token"], attempt=f"research/{iteration}"
    )
    return {
        "source_record": saved,
        "source_commit": run.git(folder / "source", "rev-parse", "HEAD"),
        "owner": owner,
        "continuation": lease,
        "plan_ref": state["plan_ref"],
        "plan": read(study.root / state["plan_ref"]),
        "round_admission": admitted,
        "research_deadline": deadline,
    }


def validate(root, workspace, iteration):
    """Validate only retained controller metadata and bounded frozen source."""
    try:
        return _validate(Path(root), Path(workspace), iteration)
    except (KeyError, TypeError, ValueError, IndexError, OSError) as error:
        raise ProTokError(
            "Malformed or changed preparation-failure evidence"
        ) from error


def _validate(root, workspace, iteration):
    from integrations.protok import run

    control = root / "control"
    directory = control / "research-jobs" / iteration
    if directory.is_symlink():
        raise ProTokError("Preparation directory must remain inside the controller")
    certificate = read(directory / "preparation-failure.json")
    if (
        certificate.get("schema") != 1
        or certificate.get("classification")
        != "operator-certified-research-preparation-failure"
        or certificate.get("iteration") != iteration
        or certificate.get("workspace") != str(workspace)
        or not isinstance(certificate.get("authorization"), str)
        or not certificate["authorization"].strip()
        or any(
            certificate.get(k) != v
            for k, v in _context(root, workspace, iteration).items()
        )
    ):
        raise ProTokError("Preparation certificate identity or frozen context changed")
    _error(certificate["original_interface"])
    if certificate.get("controller_error_binding") != _controller_error(
        certificate["original_interface"],
        certificate["attempt_files"]["failure-diagnostics.json"],
    ):
        raise ProTokError("Preparation controller evidence binding changed")
    previous = Path(certificate["previous_attempt"])
    if previous.is_absolute() or ".." in previous.parts:
        raise ProTokError("Preparation attempt reference leaves the Study")
    for name, value in certificate["attempt_files"].items():
        if name not in {
            "response.json",
            "failure.json",
            "failure-diagnostics.json",
            "agent/response.json",
            "agent/failure.json",
        }:
            raise ProTokError("Unknown retained preparation attempt evidence")
        if read(root / "study" / previous / name) != value:
            raise ProTokError("Original failed Agent evidence changed")
    _absence(control, iteration, published=True)
    job, receipt, result = records(root, iteration, certificate)
    if (
        read(directory / "job.json") != job
        or read(directory / "receipt.json") != receipt
        or set(path.name for path in directory.iterdir())
        - {
            "job.json",
            "receipt.json",
            "preparation-failure.json",
            "recovery.previous-interface.json",
        }
    ):
        raise ProTokError("Preparation records acquired native effects or changed")
    result_path = control / "research-sources" / iteration / "result.json"
    if result_path.exists() and read(result_path) != result:
        raise ProTokError("Preparation trusted result changed")
    interface = read(workspace / "evaluation-result.json")
    if interface != certificate["original_interface"] and (
        interface != result
        or read(directory / "recovery.previous-interface.json")
        != certificate["original_interface"]
    ):
        raise ProTokError("Original preparation error interface changed")
    for path in (root / "study" / previous, root / "study" / previous / "agent"):
        run.Study(root / "study")._check_attempt_stopped(path)
    return result


def certify(root, backend, iteration, *, authorization):
    """Publish a complete terminal metadata directory without native admission."""
    from integrations.protok import run

    root = Path(root).resolve()
    control = root / "control"
    if not isinstance(iteration, str) or not re.fullmatch(r"[0-9]{4,}", iteration):
        raise ProTokError("Invalid preparation iteration")
    if not isinstance(authorization, str) or not authorization.strip():
        raise ProTokError("Explicit preparation certification authorization required")
    with (
        (control / "runner.lock").open("a") as runner,
        (control / "protok.lock").open("a") as allocation,
        (control / "shared-gpu-budget.lock").open("a") as budget,
    ):
        for lock in (runner, allocation, budget):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        study = run.Study(root / "study")
        state = study.status()
        if (
            (state["phase"], state["status"]) != ("execute", "needs_attention")
            or state.get("active_attempt") != state["calls"].get("execute")
            or any((control / name).exists() for name in ("STOP", "HANDOFF"))
        ):
            raise ProTokError(
                "Only the unchanged interrupted Execute may certify preparation"
            )
        previous = study.root / state["active_attempt"]
        for path in (previous, previous / "agent"):
            study._check_attempt_stopped(path)
        workspace = Path(
            read(study.root / "iterations" / iteration / "iteration.json")["workspace"]
        )
        destination = control / "research-jobs" / iteration
        context = _context(root, workspace, iteration)
        if (
            context["owner"]["owner_token"] != backend.owner_token
            or time.time() < context["research_deadline"]
        ):
            raise ProTokError(
                "Original preparation owner or expired execution window required"
            )
        if destination.exists() or destination.is_symlink():
            result = validate(root, workspace, iteration)
            _publish(
                control / "research-sources" / iteration / "result.json", _json(result)
            )
            return result
        result_path = control / "research-sources" / iteration / "result.json"
        if result_path.exists() or result_path.is_symlink():
            raise ProTokError(
                "An existing research result prevents preparation certification"
            )
        interface = read(workspace / "evaluation-result.json")
        _error(interface)
        outer = read(previous / "failure-diagnostics.json")
        binding = _controller_error(interface, outer)
        _absence(control, iteration)
        certificate = {
            "schema": 1,
            "classification": "operator-certified-research-preparation-failure",
            "authorization": authorization,
            "iteration": iteration,
            "workspace": str(workspace),
            "previous_attempt": state["active_attempt"],
            "original_study_state": state,
            "original_interface": interface,
            "controller_error_binding": binding,
            "attempt_files": {
                name: read(previous / name)
                for name in (
                    "response.json",
                    "failure.json",
                    "failure-diagnostics.json",
                    "agent/response.json",
                    "agent/failure.json",
                )
                if (previous / name).exists()
            },
            **context,
        }
        job, receipt, result = records(root, iteration, certificate)
        staging = (
            control
            / "reconciliations"
            / ("preparation-" + iteration + "-" + digest(certificate))
            / "staging"
        )
        staging.mkdir(parents=True, exist_ok=True)
        if any(
            path.is_symlink()
            for path in (staging, staging.parent, staging.parent.parent)
        ):
            raise ProTokError("Preparation staging must remain inside the controller")
        for name, value in (
            ("preparation-failure.json", certificate),
            ("job.json", job),
            ("receipt.json", receipt),
        ):
            _publish(staging / name, _json(value))
        if (
            study.status() != state
            or _context(root, workspace, iteration) != context
            or read(workspace / "evaluation-result.json") != interface
            or read(previous / "failure-diagnostics.json") != outer
            or any((control / name).exists() for name in ("STOP", "HANDOFF"))
            or set(path.name for path in staging.iterdir())
            != {"preparation-failure.json", "job.json", "receipt.json"}
        ):
            raise ProTokError(
                "Study or operator control changed during preparation certification"
            )
        _absence(control, iteration)
        if result_path.exists() or result_path.is_symlink():
            raise ProTokError(
                "An existing research result prevents preparation certification"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(staging, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        staging.rename(destination)
        descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _publish(
            control / "research-sources" / iteration / "result.json", _json(result)
        )
        return validate(root, workspace, iteration)
