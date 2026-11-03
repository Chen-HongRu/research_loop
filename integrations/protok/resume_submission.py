"""Audited continuation of an unsubmitted Execute with its original scientific Plan."""

from __future__ import annotations

import copy
import fcntl
import math
import subprocess
import time
from pathlib import Path

from research_loop import Study
from research_loop.protocol import validate_response
from research_loop.submission import INTERFACE_FILES, LOCK, status as submission_status

from integrations.protok.backend import ProTokError

PUBLISHED_EXECUTION_DIRECTIVE = (
    "Use the controller-provided target workspace and retained source checkpoint. "
    "Candidate source is frozen: do not reapply the original scientific changes or "
    "follow old workspace paths embedded in the original Plan. Submit once through "
    "the controller submission API and collect the actual receipt for independent Review."
)


def _runner():
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
    if (
        status["outstanding"]
        or status.get("debug_outstanding")
        or status.get("research_outstanding")
        or status.get("used_gpu_seconds") is None
    ):
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


def _mode(record):
    return record.get("mode", "published_continuation")


def _prepared_state(record):
    if "prepared_state" in record:
        return record["prepared_state"]
    return {**record["state_before"], "status": "ready", "stop_reason": None}


def _recovery_certificate(record):
    return {
        "source_iteration": record["source_iteration"],
        "source_attempt": record["source_attempt"],
        "plan_ref": record["plan_ref"],
        "action": "retry"
        if _mode(record) == "same_iteration"
        else "prepare_continuation",
        "effect": "never_submitted",
        "summary": "Operator-authorized continuation of the original Plan after adapter verification that the retained Execute did not submit an allocation. Original source, grant, deadlines and usage are preserved.",
        "proof_refs": [record["reconciliation"]],
    }


def _validate_core_resume(root, record):
    _runner()._validate_core_recovery(
        root,
        Path(record["reconciliation"]).stem,
        _recovery_certificate(record),
        record["state_before"],
        _prepared_state(record),
    )


def _require_formal_plan(plan):
    if (plan.get("execution") or {}).get("mode") == "script":
        raise ProTokError(
            "Unsubmitted resume supports only fixed evaluation; "
            "settle the original script Execute with --reconcile-execute"
        )


def _identity(root, record):
    run = _runner()
    if record.get("plan_ref"):
        _require_formal_plan(run.read(root / "study" / record["plan_ref"]))
    source = root / "study/iterations" / record["source_iteration"]
    metadata = run.read(source / "iteration.json")
    if metadata["workspace_ref"] != record["source_workspace_ref"]:
        raise ProTokError("Source candidate version changed")
    workspace = Path(metadata["workspace"])
    contents = run.check_scope(
        workspace,
        run.read(root / "study/study.json")["baseline"],
        allow_research=run.research_policy(root) is not None,
    )
    if _mode(record) != "same_iteration":
        try:
            fault = run.static_candidate_fault(contents)
        except (UnicodeError, ValueError) as error:
            raise ProTokError(
                "Published candidate static parsing is unverified"
            ) from error
        if fault is not None:
            raise ProTokError(
                "Published candidate has a static source fault and cannot resume unchanged: "
                + fault["path"]
                + " ("
                + fault["type"]
                + ")"
            )
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
    if run.git(workspace, "rev-parse", "HEAD") != record["source_workspace_ref"]:
        raise ProTokError("Source workspace HEAD changed")
    for name in INTERFACE_FILES - {LOCK}:
        path = workspace / name
        if path.exists() or path.is_symlink():
            raise ProTokError("Source has a retained submission/request/result")
    if submission_status(workspace)["status"] != "not_submitted":
        raise ProTokError("Source submission is not confirmed not_submitted")
    for namespace in (
        "jobs",
        "debug",
        "research-executions",
        "research-sources",
        "research-jobs",
    ):
        if (root / "control" / namespace / record["source_iteration"]).exists():
            raise ProTokError("Source already has an execution record or debug effects")


def _unchanged(root, record):
    for name, text in record["protected"].items():
        if (root / name).read_text() != text:
            raise ProTokError("Protected protocol, history or budget changed: " + name)
    _identity(root, record)


def _original_plan(study, source_id):
    """Follow published legacy operator continuations to the accepted research Plan."""
    run = _runner()
    current = source_id
    while True:
        folder = study.root / "iterations" / current
        metadata = run.read(folder / "iteration.json")
        publication = folder / "publication.json"
        state = (
            run.read(publication)["state_after"]
            if publication.exists()
            else study.status()
        )
        continuation = metadata.get("continuation")
        legacy = metadata.get("operator_resume")
        if continuation:
            plan_ref = continuation["plan_ref"]
            break
        if legacy:
            parent = legacy["source_iteration"]
            if not parent.isdigit() or not 1 <= int(parent) < int(current):
                raise ProTokError("Invalid legacy continuation parent")
            parent_folder = study.root / "iterations" / parent
            parent_publication = run.read(parent_folder / "publication.json")
            parent_metadata = run.read(parent_folder / "iteration.json")
            parent_state = parent_publication["state_after"]
            if parent_publication[
                "metadata"
            ] != parent_metadata or not parent_state.get("accepted_calls", {}).get(
                "execute"
            ):
                raise ProTokError(
                    "Legacy continuation parent is not a matching publication"
                )
            parent_execute = run.read(study.root / parent_state["execution_ref"])
            if (
                parent_execute.get("outcome") != "incomplete"
                or parent_execute.get("artifacts") != []
                or parent_execute.get("measurements") != []
                or legacy.get("source_workspace_ref", parent_metadata["workspace_ref"])
                != parent_metadata["workspace_ref"]
            ):
                raise ProTokError(
                    "Legacy parent is not the unchanged unmeasured source"
                )
            current = parent
            continue
        plan_ref = state["plan_ref"]
        break
    path = Path(plan_ref)
    if (
        path.is_absolute()
        or ".." in path.parts
        or len(path.parts) < 4
        or path.parts[0] != "iterations"
        or not path.parts[1].isdigit()
        or not 1 <= int(path.parts[1]) <= int(source_id)
    ):
        raise ProTokError("Invalid original Plan reference")
    owner = study.root / "iterations" / path.parts[1]
    publication = owner / "publication.json"
    if publication.exists():
        published = run.read(publication)
        owner_state = published["state_after"]
        if published["metadata"] != run.read(owner / "iteration.json"):
            raise ProTokError("Original Plan publication changed")
    elif path.parts[1] == source_id:
        owner_state = study.status()
    else:
        raise ProTokError("Original Plan owner is not published")
    from integrations.protok.plan_provenance import accepted_plan_response

    plan = accepted_plan_response(study.root.parent, plan_ref, owner_state)
    if owner_state.get("plan_ref") != plan_ref or plan is None:
        raise ProTokError("Original Plan is not an accepted scientific Plan")
    _require_formal_plan(plan)
    validate_response("plan", plan)
    if plan["decision"] != "investigate":
        raise ProTokError("Resume requires an investigating original Plan")
    current_config = run.read(study.root / "study.json")
    original_config = run.read(owner / "context/study.before.json")
    if (
        {
            key: value
            for key, value in current_config.items()
            if key != "research_execution"
        }
        != {
            key: value
            for key, value in original_config.items()
            if key != "research_execution"
        }
        or study.config != current_config
        or (study.root / "goal.md").read_bytes()
        != (owner / "context/goal.md").read_bytes()
    ):
        raise ProTokError("Original frozen scientific protocol changed")
    return plan_ref


def _same_iteration(state, folder):
    return (
        state["phase"] == "execute"
        and state["status"] in {"needs_attention", "running"}
        and not state.get("accepted_calls", {}).get("execute")
        and not (folder / "publication.json").exists()
    )


def _published_source(run, state, folder, metadata):
    publication = run.read(folder / "publication.json")
    if (
        (state["status"], state["phase"], state.get("stop_reason"))
        != ("stopped", "done", "review")
        or metadata["status"] != "reviewed"
        or publication["metadata"] != metadata
        or {
            k: v
            for k, v in publication["state_after"].items()
            if k != "elapsed_seconds"
        }
        != {k: v for k, v in state.items() if k != "elapsed_seconds"}
        or state["elapsed_seconds"] < publication["state_after"]["elapsed_seconds"]
        or not state.get("accepted_calls", {}).get("execute")
    ):
        raise ProTokError("Source Review publication does not match the stopped state")


def validate_prepared(root, backend):
    """Validate an explicitly prepared, still stopped deployment boundary."""
    run = _runner()
    root = Path(root)
    pending = run.read(root / "control/resume-submission.json")
    record = run.read(Path(pending["reconciliation"]))
    if pending != {**record, "status": "prepared"}:
        raise ProTokError("Resume preparation differs from immutable evidence")
    study = Study(root / "study")
    expected = _prepared_state(record)
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
    _validate_core_resume(root, record)
    return study.status()


def prepare_resume(root, backend, *, authorization: str):
    """Retire an unaccepted Execute or prepare an immutable published continuation."""
    run = _runner()
    root = Path(root).resolve()
    if not authorization.strip():
        raise ProTokError("Explicit resume authorization is required")
    control = root / "control"
    study = Study(root / "study")
    with (
        (control / "supervisor.lock").open("a") as supervisor,
        (control / "runner.lock").open("a") as runner,
    ):
        for lock in (supervisor, runner):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        initial = study.status()
        source_id = f"{initial['iteration']:04d}"
        pending_path = control / "resume-submission.json"
        if pending_path.exists() and run.read(pending_path).get("status") == "prepared":
            recovery_id = Path(run.read(pending_path)["reconciliation"]).stem
        elif _same_iteration(initial, study.root / "iterations" / source_id):
            recovery_id = (
                f"resume-execute-{source_id}-{Path(initial['calls']['execute']).name}"
            )
        else:
            recovery_id = f"resume-submission-{source_id}"
        prepared_record = {}

        def certify(state):
            if state != initial:
                raise ProTokError("Study state changed before resume certification")
            pending_path = control / "resume-submission.json"
            source_id = f"{state['iteration']:04d}"
            folder = study.root / "iterations" / source_id
            record = None
            if pending_path.exists():
                pending = run.read(pending_path)
                saved = run.read(Path(pending["reconciliation"]))
                if pending != {**saved, "status": pending.get("status")} or pending.get(
                    "status"
                ) not in {"prepared", "begun"}:
                    raise ProTokError("Invalid pending resume evidence")
                if pending["status"] == "prepared":
                    if pending["authorization"] != authorization:
                        raise ProTokError(
                            "An existing resume authorization cannot be replaced"
                        )
                    record = saved
                elif not (
                    _same_iteration(state, folder)
                    and state["calls"].get("execute") != saved.get("source_attempt")
                ) and not (
                    state["status"] == "stopped"
                    and state["phase"] == "done"
                    and source_id != saved["source_iteration"]
                ):
                    prepared_record["pending"] = pending
                    return None
            if record is None:
                metadata = run.read(folder / "iteration.json")
                same = _same_iteration(state, folder)
                if state.get("debugging"):
                    raise ProTokError(
                        "Active debug recovery prevents unsubmitted resume"
                    )
                if same:
                    attempt = state["calls"].get("execute")
                    if not attempt or state.get("active_attempt") not in (
                        None,
                        attempt,
                    ):
                        raise ProTokError(
                            "Unaccepted Execute attempt identity is unresolved"
                        )
                    if state.get("execution_ref") not in (
                        None,
                        str(Path(attempt) / "response.json"),
                    ):
                        raise ProTokError(
                            "Unaccepted Execute response identity is unresolved"
                        )
                    mode = "same_iteration"
                    evidence_name = (
                        f"resume-execute-{source_id}-{Path(attempt).name}.json"
                    )
                else:
                    _published_source(run, state, folder, metadata)
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
                    attempt = state["calls"]["execute"]
                    mode = "published_continuation"
                    evidence_name = f"resume-submission-{source_id}.json"
                for relative in state["calls"].values():
                    study._check_attempt_stopped(study.root / relative)
                stop = control / "STOP"
                if not stop.exists():
                    raise ProTokError(
                        "Explicit STOP is required while preparing resume"
                    )
                evidence = control / "reconciliations" / evidence_name
                plan_ref = _original_plan(study, source_id)
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
                    if state.get(key)
                ]
                names.append(str((study.root / plan_ref).relative_to(root)))
                names += [
                    str((study.root / attempt / name).relative_to(root))
                    for name in (
                        "context.json",
                        "process.json",
                        "response.json",
                        "agent/process.json",
                        "agent/response.json",
                    )
                    if (study.root / attempt / name).exists()
                ]
                prepared = copy.deepcopy(state)
                prepared.update(
                    status="ready",
                    stop_reason=None,
                    execution_recovery_ref=f"recoveries/{recovery_id}.json",
                )
                if same:
                    prepared["calls"].pop("execute", None)
                    prepared.get("accepted_calls", {}).pop("execute", None)
                    prepared.update(active_attempt=None, error=None, execution_ref=None)
                record = {
                    "source": "operator",
                    "mode": mode,
                    "authorization": authorization,
                    "source_iteration": source_id,
                    "target_iteration": source_id
                    if same
                    else f"{state['iteration'] + 1:04d}",
                    "source_attempt": attempt,
                    "source_workspace_ref": metadata["workspace_ref"],
                    "source_signatures": run.signatures(
                        run.candidate_bytes(metadata["workspace"])
                    ),
                    "plan_ref": plan_ref,
                    "directive": (
                        "Resume the original unsubmitted Execute using the controller submission API; retain its scientific Plan and collect the actual receipt for independent Review."
                        if same
                        else PUBLISHED_EXECUTION_DIRECTIVE
                    ),
                    "route_id": metadata["route_id"],
                    "route_title": metadata["route_title"],
                    "state_before": state,
                    "prepared_state": prepared,
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
            expected = _prepared_state(record)
            if study.status() not in (record["state_before"], expected):
                raise ProTokError("Live state changed during resume preparation")
            stop = control / "STOP"
            if (stop.read_text() if stop.exists() else None) != record["stop_before"]:
                raise ProTokError("STOP changed during resume preparation")
            if Path(record["reconciliation"]).stem != recovery_id:
                raise ProTokError("Resume attempt changed before certification")
            prepared_record["record"] = record
            return _recovery_certificate(record)

        expected = study.reconcile_execution(recovery_id=recovery_id, certify=certify)
        if "pending" in prepared_record:
            return prepared_record["pending"]
        record = prepared_record["record"]
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
    """Start only the prepared execution boundary; never ask for another Plan."""
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
        if pending["status"] == "begun":
            if state["iteration"] < int(record["target_iteration"]):
                raise ProTokError("Begun resume lost its target iteration")
            return
        _unchanged(root, record)
        _budget(root, backend, study)
        _validate_core_resume(root, record)
        if (root / "control/STOP").exists():
            raise ProTokError("Resume STOP must be cleared by deployment")
        if _mode(record) == "same_iteration":
            if state != _prepared_state(record):
                raise ProTokError("Prepared live state changed before beginning resume")
        else:
            continuation = {
                key: record[key]
                for key in (
                    "source_iteration",
                    "source_workspace_ref",
                    "source_signatures",
                    "authorization",
                    "reconciliation",
                )
            }
            continuation.update(
                plan_ref=record.get("plan_ref")
                or _original_plan(study, record["source_iteration"]),
                directive=record.get(
                    "directive",
                    PUBLISHED_EXECUTION_DIRECTIVE,
                ),
            )
            if state["iteration"] == int(
                record["source_iteration"]
            ) and state != _prepared_state(record):
                raise ProTokError("Prepared live state changed before beginning resume")
            study._continue_execution(
                state,
                source_iteration=record["source_iteration"],
                continuation=continuation,
            )
            workspace = Path(
                run.read(study._folder(study.status()) / "iteration.json")["workspace"]
            )
            if (
                run.signatures(run.candidate_bytes(workspace))
                != record["source_signatures"]
            ):
                raise ProTokError(
                    "Resume target source differs from the authorized candidate"
                )
        run.write(path, {**record, "status": "begun"})
