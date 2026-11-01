"""Audited continuation of a published, unsubmitted candidate in a new iteration."""

from __future__ import annotations

import copy
import fcntl
import math
import subprocess
import time
from pathlib import Path

from research_loop import Study
from research_loop.protocol import validate_response

from integrations.protok.backend import CANDIDATE_FILES, ProTokError


def _runner():
    # The runner imports this module at its continuation boundary.
    from integrations.protok import run

    return run


def _budget(root, backend, study):
    run = _runner()
    control = root / "control"
    owner = run.read(control / "ownership.json")
    lease = run.read(control / "continuation.json")
    now = time.time()
    if (
        owner.get("owner_token") != backend.owner_token
        or owner.get("project") != str(backend.project)
        or owner.get("handoff_acknowledged") is not True
        or lease.get("owner_token") != backend.owner_token
        or not lease.get("authorization")
        or lease.get("schema") != 2
        or lease.get("seconds") != 86400
        or lease.get("max_new_candidates", "missing") is not None
        or not lease["started_at"] <= now
        or min(lease["deadline"], owner["expires_at"]) - now < 1800
    ):
        raise ProTokError(
            "Resume requires the original active lease and allocation window"
        )
    status = backend.inspect()
    if status["outstanding"] or status.get("debug_outstanding"):
        raise ProTokError("Outstanding or unknown evaluation effects prevent resume")
    spent = status["used_gpu_seconds"] - lease["initial_gpu_seconds"]
    allowance = lease["additional_gpu_seconds"]
    if (
        not all(
            type(v) in (int, float) and math.isfinite(v) for v in (spent, allowance)
        )
        or spent < 0
        or not 0 < allowance <= 86400
        or spent + 1800 + backend._config()["reserve_gpu_seconds"] > allowance
    ):
        raise ProTokError("Original GPU budget cannot fit resume and reserve")
    state = study.status()
    config = study.config
    if config["max_seconds"] - state["elapsed_seconds"] < 1800:
        raise ProTokError("Original Study time budget cannot fit resume")
    if (
        config["max_calls"] is not None
        and config["max_calls"] - state["call_count"] < 2
    ):
        raise ProTokError("Original Study call budget cannot fit Execute and Review")
    if (
        config["max_iterations"] is not None
        and state["iteration"] >= config["max_iterations"]
        and state["phase"] == "done"
    ):
        raise ProTokError("Original Study iteration budget exhausted")


def _identity(root, record):
    run = _runner()
    source = root / "study/iterations" / record["source_iteration"]
    metadata = run.read(source / "iteration.json")
    if metadata["workspace_ref"] != record["source_workspace_ref"]:
        raise ProTokError("Source candidate version changed")
    workspace = Path(metadata["workspace"])
    contents = run.candidate_bytes(workspace)
    for name, content in contents.items():
        result = subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "show",
                f"{metadata['workspace_ref']}:{name}",
            ],
            check=True,
            capture_output=True,
        )
        if result.stdout != content:
            raise ProTokError("Candidate differs from its published Git version")
    if run.signatures(contents) != record["source_signatures"]:
        raise ProTokError("Source candidate identity changed")
    for name in (
        "evaluation-request.json",
        "evaluation-result.json",
        "evaluation-submission.json",
    ):
        path = workspace / name
        if path.exists() or path.is_symlink():
            raise ProTokError("Source has a retained submission/request/result")
    if (root / "control/jobs" / record["source_iteration"]).exists():
        raise ProTokError("Source already has an evaluation job")


def _unchanged(root, record):
    for name, text in record["protected"].items():
        if (root / name).read_text() != text:
            raise ProTokError("Protected protocol, history or budget changed: " + name)
    _identity(root, record)


def validate_prepared(root, backend):
    """Validate an explicitly prepared, still stopped deployment boundary."""
    run = _runner()
    root = Path(root)
    pending = run.read(root / "control/resume-submission.json")
    record = run.read(Path(pending["reconciliation"]))
    if pending != {**record, "status": "prepared"}:
        raise ProTokError("Resume preparation differs from immutable evidence")
    study = Study(root / "study")
    expected = {**record["state_before"], "status": "ready", "stop_reason": None}
    if study.status() != expected:
        raise ProTokError("Prepared live state changed")
    stop = root / "control/STOP"
    if (stop.read_text() if stop.exists() else None) != record["stop_before"]:
        raise ProTokError("STOP changed after resume preparation")
    runner = run.read(root / "control/runner.json")
    if (
        runner.get("status") != "completed"
        or runner.get("iteration") != expected["iteration"]
    ):
        raise ProTokError("Resume control publication is incomplete")
    _unchanged(root, record)
    _budget(root, backend, study)
    return study.status()


def prepare_resume(root, backend, *, authorization: str):
    """Prepare a new investigation; never reopen or rewrite the published source."""
    run = _runner()
    root = Path(root).resolve()
    if not authorization.strip():
        raise ProTokError("Explicit resume authorization is required")
    control = root / "control"
    study = Study(root / "study")
    with (
        (control / "supervisor.lock").open("a") as supervisor,
        (control / "runner.lock").open("a") as runner,
        study._lock(),
    ):
        for lock in (supervisor, runner):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pending_path = control / "resume-submission.json"
        if pending_path.exists():
            pending = run.read(pending_path)
            if pending["authorization"] != authorization:
                raise ProTokError("An existing resume authorization cannot be replaced")
            if pending.get("status") == "begun":
                raise ProTokError("Resume has already begun; use the normal runner")
            record = run.read(Path(pending["reconciliation"]))
        else:
            state = study.status()
            if (state["status"], state["phase"], state.get("stop_reason")) != (
                "stopped",
                "done",
                "review",
            ):
                raise ProTokError("Only a published Review stop can resume submission")
            source_id = f"{state['iteration']:04d}"
            folder = study.root / "iterations" / source_id
            metadata = run.read(folder / "iteration.json")
            publication = run.read(folder / "publication.json")
            if (
                metadata["status"] != "reviewed"
                or {
                    k: v
                    for k, v in publication["state_after"].items()
                    if k != "elapsed_seconds"
                }
                != {k: v for k, v in state.items() if k != "elapsed_seconds"}
                or state["elapsed_seconds"]
                < publication["state_after"]["elapsed_seconds"]
            ):
                raise ProTokError(
                    "Source Review publication does not match the stopped state"
                )
            if not state.get("accepted_calls", {}).get("execute"):
                raise ProTokError("Source Execute was not accepted")
            execute = run.read(study.root / state["execution_ref"])
            review = run.read(study.root / state["review_ref"])
            if (
                execute["outcome"] != "incomplete"
                or execute["artifacts"]
                or execute["measurements"]
                or review["decision"] != "stop"
            ):
                raise ProTokError(
                    "Source must be a reviewed, unmeasured incomplete Execute"
                )
            for relative in state["calls"].values():
                study._check_attempt_stopped(study.root / relative)
            stop = control / "STOP"
            if not stop.exists():
                raise ProTokError("Explicit STOP is required while preparing resume")
            evidence = (
                control / "reconciliations" / f"resume-submission-{source_id}.json"
            )
            names = [
                "control/ownership.json",
                "control/continuation.json",
                "study/study.json",
                "study/goal.md",
            ]
            names += [
                str((folder / name).relative_to(root))
                for name in (
                    "iteration.json",
                    "publication.json",
                    "candidates.json",
                    "review.md",
                    "route.after.md",
                    "global.after.md",
                )
                if (folder / name).exists()
            ]
            names += [
                str((study.root / state[key]).relative_to(root))
                for key in ("plan_ref", "execution_ref", "review_ref")
            ]
            record = {
                "source": "operator",
                "authorization": authorization,
                "source_iteration": source_id,
                "target_iteration": f"{state['iteration'] + 1:04d}",
                "source_workspace_ref": metadata["workspace_ref"],
                "source_signatures": run.signatures(
                    run.candidate_bytes(metadata["workspace"])
                ),
                "route_id": metadata["route_id"],
                "route_title": metadata["route_title"],
                "state_before": state,
                "stop_before": stop.read_text(),
                "runner_before": run.read(control / "runner.json"),
                "protected": {name: (root / name).read_text() for name in names},
                "reconciliation": str(evidence),
            }
            _unchanged(root, record)
            _budget(root, backend, study)
            run.immutable_record(evidence, record)
            run.write(pending_path, {**record, "status": "prepared"})
        _unchanged(root, record)
        _budget(root, backend, study)
        expected = {**record["state_before"], "status": "ready", "stop_reason": None}
        if study.status() not in (record["state_before"], expected):
            raise ProTokError("Live state changed during resume preparation")
        stop = control / "STOP"
        if (stop.read_text() if stop.exists() else None) != record["stop_before"]:
            raise ProTokError("STOP changed during resume preparation")
        study._save(expected)
        run.write(
            control / "runner.json",
            {
                **record["runner_before"],
                "status": "completed",
                "iteration": expected["iteration"],
                "resume_submission": record["reconciliation"],
            },
        )
        validate_prepared(root, backend)
        return run.read(pending_path)


def begin_pending(study, backend):
    """Create the next frozen operator Plan after runner admission (runner lock held)."""
    run = _runner()
    root = study.root.parent
    path = root / "control/resume-submission.json"
    if not path.exists():
        return
    with study._lock():
        pending = run.read(path)
        record = run.read(Path(pending["reconciliation"]))
        if pending != {**record, "status": pending.get("status")} or pending.get(
            "status"
        ) not in {"prepared", "begun"}:
            raise ProTokError("Invalid pending resume evidence")
        state = study.status()
        target = int(record["target_iteration"])
        if pending["status"] == "begun":
            if state["iteration"] < target:
                raise ProTokError("Begun resume lost its target iteration")
            return
        _unchanged(root, record)
        _budget(root, backend, study)
        if (root / "control/STOP").exists():
            raise ProTokError("Resume STOP must be cleared by deployment")
        marker = {
            key: record[key]
            for key in (
                "source_iteration",
                "target_iteration",
                "source_workspace_ref",
                "source_signatures",
                "reconciliation",
            )
        }
        if (
            state["iteration"] == target - 1
            and state["phase"] == "done"
            and state["status"] == "ready"
        ):
            expected = {
                **record["state_before"],
                "status": "ready",
                "stop_reason": None,
            }
            if state != expected:
                raise ProTokError("Prepared live state changed before beginning resume")
            study._begin(state)
        if (
            state["iteration"] != target
            or state["status"] != "ready"
            or state["phase"] not in {"selection", "execute"}
        ):
            raise ProTokError("Resume target is not at the expected Plan boundary")
        folder = study._folder(state)
        metadata = run.read(folder / "iteration.json")
        if metadata.get("operator_resume", marker) != marker:
            raise ProTokError("Conflicting resume target metadata")
        metadata["operator_resume"] = marker
        run.write(folder / "iteration.json", metadata)
        if state["phase"] == "selection":
            message = (
                f"Operator-authorized first fixed-protocol evaluation of the unchanged candidate from {record['source_iteration']}. "
                "The source remains an unmeasured incomplete investigation. Do not edit candidate source or change protocol. "
                "Submit using the runtime evaluation API, collect its actual receipt, then request independent Review. "
                "No experimental success or model call is asserted by this operator Plan."
            )
            plan = copy.deepcopy(
                run.read(study.root / record["state_before"]["plan_ref"])
            )
            plan.update(
                decision="investigate",
                markdown=message,
                base_experiment=record["source_iteration"],
                references=[record["source_iteration"]],
                input_artifacts=[
                    {
                        "path": str(
                            Path(
                                run.read(
                                    root
                                    / "study/iterations"
                                    / record["source_iteration"]
                                    / "iteration.json"
                                )["workspace"]
                            )
                            / name
                        ),
                        "version": record["source_workspace_ref"],
                    }
                    for name in CANDIDATE_FILES
                ],
                stop_reason=None,
            )
            plan["contract"].update(action=message, rationale=message)
            validate_response("plan", plan)
            receipt_dir = folder / "operator-plan"
            receipt_dir.mkdir(exist_ok=True)
            run.immutable_record(
                receipt_dir / "operator.json",
                {
                    "source": "operator",
                    **marker,
                    "authorization": record["authorization"],
                },
            )
            run.immutable_record(receipt_dir / "response.json", plan)
            proposal = {
                "route_id": record["route_id"],
                "route_title": record["route_title"],
                "action": message,
                "base_experiment": record["source_iteration"],
                "references": [record["source_iteration"]],
                "markdown": message,
            }
            state["selected"] = {
                "source": "operator",
                "proposal": proposal,
                "judge": {"source": "operator", "action": message},
            }
            state["calls"][f"plan-{state['selection_round']}"] = str(
                receipt_dir.relative_to(study.root)
            )
            study._save(state)
            study._accept_plan(state, folder, plan)
        workspace = Path(run.read(folder / "iteration.json")["workspace"])
        if (
            run.signatures(run.candidate_bytes(workspace))
            != record["source_signatures"]
        ):
            raise ProTokError(
                "Resume target source differs from the authorized candidate"
            )
        run.write(path, {**record, "status": "begun"})
