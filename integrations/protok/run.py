"""One leased ProTok investigation; fixed evaluation runs outside Agent workspaces."""

from __future__ import annotations

import argparse
import ast
import copy
import fcntl
import os
import hashlib
import json
import sys
import math
import re
import shlex
import subprocess
import shutil
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from research_loop import Study, initialize
from research_loop.harness import (
    CodexHarness,
    HarnessConfig,
    HarnessError,
    RecoverableExecutionError,
    completed_response,
    publish_completed_response,
)
from research_loop.diagnostics import diagnostic_secrets, diagnostic_view
from research_loop.protocol import validate_response
from research_loop.submission import (
    INTERFACE_FILES,
    LOCK,
    RECEIPT,
    claim_request,
    status as submission_status,
)
from integrations.protok.backend import (
    CANDIDATE_FILES,
    METRIC,
    PROTOCOL,
    ProTokBackend,
    ProTokError,
)
from integrations.protok.research_execution import policy as research_policy
from integrations.protok.agent_output import (
    REPORT_INSTRUCTIONS,
    REPORT_SCHEMA,
    receipt_response,
    report_response,
)

# Site paths and historical candidate identity belong to the operator's environment.
PROJECT = (
    Path(os.environ["PROTOK_PROJECT"]) if os.environ.get("PROTOK_PROJECT") else None
)
BEST_COMMIT = os.environ.get("PROTOK_BASE_COMMIT", "")
BEST_FINGERPRINT = os.environ.get("PROTOK_BASE_FINGERPRINT", "")
BEST_VALUE = float(os.environ.get("PROTOK_BASE_VALUE", "nan"))
CODEX = os.environ.get("PROTOK_CODEX", "codex")


def require_baseline():
    """Refuse historical import until the operator supplies the exact baseline."""
    if (
        not re.fullmatch(r"[0-9a-f]{40}", BEST_COMMIT)
        or not re.fullmatch(r"[0-9a-f]{64}", BEST_FINGERPRINT)
        or not math.isfinite(BEST_VALUE)
    ):
        raise ProTokError(
            "Set PROTOK_BASE_COMMIT, PROTOK_BASE_FINGERPRINT and PROTOK_BASE_VALUE "
            "from the acknowledged fixed-protocol baseline before initialization/import"
        )


SUBMISSION_EFFECT_FILES = INTERFACE_FILES - {LOCK}


def submission_instructions():
    client = Path(__file__).resolve().parents[2] / "src/research_loop/submission.py"
    command = shlex.join([sys.executable, "-B", str(client)])
    return f"""Controller evaluation submission API (transport instructions replace older
instructions to hand-write interface files; scientific scope and budget are unchanged):
During Execute, use `{command} submit --workspace . --action evaluate` (import only
for the first historical-import iteration). Use `{command} status --workspace .`
to inspect the same submission. The API's named temporary file, lock and retained
submission receipt are authorized transport files, not candidate modifications.
Never implement publication yourself, edit/delete these files, or use O_TMPFILE/linkat.
Repeated API submit calls return the same submission; they do not launch a second job.
not_submitted means nothing was published; after a local publication failure you may
retry the API once only if status confirms not_submitted. pending means published;
accepted means the controller claimed it, not that GPU execution has started. For
pending/accepted wait quietly for evaluation-result.json within the frozen timeout.
completed/failed return the original result. An unknown state requires reconciliation,
not a new submission. The controller constructs structured artifacts and measurements
from its verified result; your final response is the execution report described below.
"""


def initialize_submission_workspace(root, workspace, iteration):
    """Remove only unchanged parent-checkpoint interfaces, once before any Agent."""
    root, workspace = Path(root), Path(workspace)
    marker = root / "control/evaluation-interfaces" / f"{iteration}.json"
    if marker.exists():
        record = read(marker)
        if record["workspace"] != str(workspace):
            raise ProTokError("Submission workspace initialization identity changed")
        if record["status"] == "ready":
            return
    else:
        if (root / "control/jobs" / iteration).exists():
            raise ProTokError("Existing evaluation job requires reconciliation")
        tracked = set(
            git(workspace, "ls-files", "--", *sorted(INTERFACE_FILES)).splitlines()
        )
        record = {
            "workspace": str(workspace),
            "head": git(workspace, "rev-parse", "HEAD"),
            "inherited": sorted(tracked),
            "status": "initializing",
        }
    if (
        record["head"] != git(workspace, "rev-parse", "HEAD")
        or (root / "control/jobs" / iteration).exists()
    ):
        raise ProTokError("Submission initialization has unresolved effects")
    for name in INTERFACE_FILES:
        path = workspace / name
        if not path.exists() and not path.is_symlink():
            continue
        if (
            name not in record["inherited"]
            or path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > 1024 * 1024
            or subprocess.run(
                ["git", "-C", str(workspace), "show", f"HEAD:{name}"],
                check=True,
                capture_output=True,
            ).stdout
            != path.read_bytes()
        ):
            raise ProTokError("Unreconciled evaluation interface: " + name)
    if not marker.exists():
        write(marker, record)
    for name in record["inherited"]:
        (workspace / name).unlink(missing_ok=True)
    write(marker, {**record, "status": "ready"})


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    )
    temporary.replace(path)


def git(path, *args):
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True
    )
    if result.returncode:
        raise ProTokError("Git boundary operation failed: " + result.stderr.strip())
    return result.stdout.strip()


def candidate_bytes(workspace):
    workspace = Path(workspace)
    result = {}
    for name in CANDIDATE_FILES:
        path = workspace / name
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size > 4 * 1024 * 1024
            or any(
                parent.is_symlink()
                for parent in path.parents
                if parent != workspace and parent.is_relative_to(workspace)
            )
        ):
            raise ProTokError("Candidate must be a small regular source file: " + name)
        result[name] = path.read_bytes()
    return result


def signatures(contents):
    return {name: hashlib.sha256(value).hexdigest() for name, value in contents.items()}


def check_scope(workspace, baseline, *, allow_research=False):
    changed = set(
        git(workspace, "diff", "--name-only", "-z", baseline, "--").split("\0")
    )
    changed |= set(git(workspace, "ls-files", "--others", "-z").split("\0"))
    allowed = set(CANDIDATE_FILES) | INTERFACE_FILES | {""}
    if allow_research:
        from integrations.protok.research_execution import research_files

        allowed.update(research_files(workspace))
    extra = changed - allowed
    if extra:
        raise ProTokError(
            "Agent changed files outside candidate scope: " + ", ".join(sorted(extra))
        )
    return candidate_bytes(workspace)


def freeze_source(
    project,
    workspace,
    destination,
    baseline,
    *,
    allow_research=False,
    include_research=False,
):
    check_scope(workspace, baseline, allow_research=allow_research)
    contents = candidate_bytes(workspace)
    if include_research:
        from integrations.protok.research_execution import research_files

        contents.update(research_files(workspace))
    subprocess.run(
        ["git", "clone", "--shared", "--quiet", str(project), str(destination)],
        check=True,
    )
    git(destination, "checkout", "--detach", baseline)
    for name, value in contents.items():
        (destination / name).parent.mkdir(parents=True, exist_ok=True)
        (destination / name).write_bytes(value)
    git(destination, "add", "--", *contents)
    if git(destination, "diff", "--cached", "--name-only"):
        git(
            destination,
            "-c",
            "user.name=Research Loop",
            "-c",
            "user.email=research-loop@localhost",
            "commit",
            "-m",
            "Frozen ProTok candidate",
        )
    return git(destination, "rev-parse", "HEAD"), signatures(contents)


def import_best(backend, prepared):
    require_baseline()
    if prepared["fingerprint"] != BEST_FINGERPRINT:
        raise ProTokError("First audit source is not the historical best fingerprint")
    row = next(
        (
            r
            for r in backend.inspect()["records"]
            if r["fingerprint"] == BEST_FINGERPRINT
        ),
        None,
    )
    if row is None:
        raise ProTokError("Historical best record is missing")
    summary = row["summary"]
    if (
        row["status"] != "complete"
        or summary.get("fingerprint") != BEST_FINGERPRINT
        or summary.get("protocol") != PROTOCOL
        or summary.get("objective") != METRIC
        or summary.get("quality_score_eligible") is not True
        or type(summary.get("value")) not in (int, float)
        or not math.isfinite(summary["value"])
        or summary["value"] != BEST_VALUE
    ):
        raise ProTokError(
            "Historical best is not a complete eligible fixed-protocol record"
        )
    # The backend exposes only small authoritative receipt summaries; no model/data reread.
    return {
        **prepared,
        "metric": METRIC,
        "value": summary["value"],
        "verified": True,
        "evidence": row["evidence"],
        "provenance": "historical-import-no-allocation",
    }


def admission(root, backend, *, require_idle=True):
    root = Path(root)
    control = root / "control"
    if (control / "STOP").exists():
        raise ProTokError("ProTok Study is paused by control/STOP")
    owner, continuation = (
        read(control / "ownership.json"),
        read(control / "continuation.json"),
    )
    now = time.time()
    from integrations.protok.round_admission import active_round

    admitted = active_round(root, owner_token=backend.owner_token)
    duration = continuation.get("seconds")
    supported_lease = (continuation.get("schema", 1) == 1 and duration == 21600) or (
        continuation.get("schema") == 2
        and duration == 86400
        and type(continuation.get("additional_gpu_seconds")) in (int, float)
        and math.isfinite(continuation["additional_gpu_seconds"])
        and 0 < continuation["additional_gpu_seconds"] <= 86400
    )
    if (
        owner.get("owner_token") != backend.owner_token
        or owner.get("project") != str(backend.project)
        or owner.get("handoff_acknowledged") is not True
        or continuation.get("owner_token") != backend.owner_token
        or continuation.get("max_new_candidates", "missing") is not None
        or not supported_lease
        or not continuation.get("authorization")
        or (
            not continuation["started_at"]
            <= now
            < min(continuation["deadline"], owner["expires_at"])
            and admitted is None
        )
    ):
        raise ProTokError(
            "An acknowledged active bounded continuation lease is required"
        )
    status = backend.inspect()
    if require_idle and (status["outstanding"] or status.get("research_outstanding")):
        raise ProTokError(
            "Outstanding original record requires explicit operator reconciliation"
        )
    return min(continuation["deadline"], owner["expires_at"])


def static_candidate_fault(contents):
    """Return a demonstrated static source fault; propagate unverified parsing."""
    for name, value in contents.items():
        try:
            if name.endswith(".py"):
                ast.parse(value, filename=name)
            elif name.endswith(".json"):
                json.loads(value)
        except (SyntaxError, json.JSONDecodeError) as error:
            return {
                "path": name,
                "type": type(error).__name__,
                "message": error.msg,
                "line": error.lineno,
                "column": error.offset
                if isinstance(error, SyntaxError)
                else error.colno,
            }
    return None


def local_incomplete(
    root, backend, workspace, iteration, result, *, run_dir=None, completed=False
):
    """Check a completed local response against scope and absence of effects."""
    validate_response("execute", result)
    if (
        result["outcome"] != "incomplete"
        or result["artifacts"]
        or result["measurements"]
    ):
        raise ProTokError(
            "Local rejection requires incomplete and empty artifacts/measurements"
        )
    if (Path(root) / "control/jobs" / iteration).exists():
        raise ProTokError(
            "An existing evaluation job requires explicit backend reconciliation"
        )
    for name in SUBMISSION_EFFECT_FILES:
        path = Path(workspace) / name
        if path.exists() or path.is_symlink():
            raise ProTokError(
                "Local rejection cannot retain an evaluation interface file"
            )
    baseline = read(Path(root) / "study/study.json")["baseline"]
    contents, source_error = None, None
    try:
        contents = check_scope(
            workspace, baseline, allow_research=research_policy(root) is not None
        )
    except (ProTokError, OSError) as error:
        source_error = f"{type(error).__name__}: {error}"
    effects = backend.inspect()
    if (
        effects["outstanding"]
        or effects.get("debug_outstanding")
        or effects.get("research_outstanding")
    ):
        raise ProTokError(
            "Outstanding backend effects cannot be accepted as a local rejection"
        )
    fault = None
    if contents is not None:
        try:
            fault = static_candidate_fault(contents)
        except (UnicodeError, ValueError) as error:
            source_error = f"{type(error).__name__}: {error}"
    if not completed and run_dir is not None:
        completed = any(
            completed_response(directory) == result
            for directory in (Path(run_dir), Path(run_dir) / "agent")
        )
    return {
        "iteration": iteration,
        "attempt": str(Path(run_dir).resolve()) if run_dir is not None else None,
        "completed_response": completed,
        "source_signatures": signatures(contents) if contents is not None else None,
        "source_error": source_error,
        "candidate_fault": fault,
        "classification": "candidate-fault"
        if fault
        else ("completed-incomplete" if completed else "unsubmitted-interruption"),
        "response": result,
        "provenance": "local-scope-and-effects-check-no-evaluation",
    }


def retain_local_incomplete(
    root, backend, workspace, iteration, result, *, run_dir, completed=False
):
    diagnostic = local_incomplete(
        root,
        backend,
        workspace,
        iteration,
        result,
        run_dir=run_dir,
        completed=completed,
    )
    attempt = Path(run_dir).name if run_dir is not None else "unbound"
    immutable_record(
        Path(root) / "control/diagnostics" / iteration / (attempt + ".json"),
        diagnostic,
    )
    if (
        diagnostic["source_error"] is not None
        or diagnostic["source_signatures"] is None
    ):
        raise HarnessError("Local incomplete violates the frozen candidate scope")
    if not diagnostic["completed_response"]:
        raise HarnessError(
            "Execute interrupted before submission; no successful completion receipt. "
            "Preserve this Execute and Plan for explicit reconciliation and retry"
        )
    return diagnostic


def immutable_record(path, record):
    if path.exists():
        if read(path) != record:
            raise ProTokError("An existing operator evidence record cannot be replaced")
    else:
        write(path, record)


def debug_policy(root):
    policy = read(Path(root) / "control/debug-policy.json")
    if (
        policy.get("schema") != 1
        or policy.get("gpu_seconds_per_investigation") != 1800
        or policy.get("independent_of_formal_budget") is not True
        or not policy.get("authorization")
    ):
        raise ProTokError(
            "Explicit independent per-investigation debug authorization required"
        )
    return policy


def job_path(root, iteration):
    root = Path(root).resolve()
    original = root / "control/jobs" / iteration
    pointer = original / "recovery.json"
    if not pointer.exists():
        return original
    target = Path(read(pointer)["job_directory"]).resolve()
    expected = root / "control/debug" / iteration
    if target.parent != expected or not re.fullmatch(r"formal-[0-9]{3}", target.name):
        raise ProTokError("Invalid recovered formal job pointer")
    return target


def check_fixed_candidate(root, workspace, iteration, *sources):
    """Compare only the three candidate files with the submitted Git revision."""
    manual_path = Path(root) / "study/iterations" / iteration / "context/task.json"
    manual = read(manual_path) if manual_path.exists() else None
    if not manual or manual["allow_changes"]:
        return None
    if (Path(root) / "control/jobs" / iteration / "recovery.json").exists():
        raise ProTokError(
            "Fixed-candidate task retained a replacement job; explicit reconciliation required"
        )
    frozen = {
        name: subprocess.check_output(
            [
                "git",
                "-C",
                str(workspace),
                "show",
                manual["source"]["revision"] + ":" + name,
            ]
        )
        for name in CANDIDATE_FILES
    }
    for source in (workspace, *sources):
        if candidate_bytes(source) != frozen:
            raise ProTokError(
                "Fixed-candidate manual task changed source: " + str(source)
            )
    return signatures(frozen)


def certify_candidate_failure(root, backend, workspace, iteration):
    """Persist the distinction between a settled code rejection and unknown effects."""
    root, workspace = Path(root), Path(workspace)
    job = job_path(root, iteration)
    record = read(job / "job.json")
    check_fixed_candidate(root, workspace, iteration, job / "source")
    baseline = read(root / "study/study.json")["baseline"]
    for source in (workspace, job / "source"):
        check_scope(source, baseline, allow_research=research_policy(root) is not None)
        if signatures(candidate_bytes(source)) != record["source_signatures"]:
            raise ProTokError("Failed candidate changed after its source was frozen")
    if git(job / "source", "rev-parse", "HEAD") != record["source_commit"]:
        raise ProTokError("Failed candidate source commit changed")
    details = backend.settled_failure(
        job / "source",
        job / "runtime-config.json",
        record["fingerprint"],
        **({"record_name": record["record_name"]} if record.get("record_name") else {}),
    )
    details.update(
        iteration=iteration,
        job_directory=str(job.resolve()),
        source_signatures=record["source_signatures"],
        source_commit=record["source_commit"],
        execution_deadline=record.get("execution_deadline"),
    )
    submission = Path(workspace) / RECEIPT
    if submission.is_symlink() or (
        submission.exists() and read(submission) != record["request"]
    ):
        raise ProTokError("Claimed request differs from the failed evaluation")
    if submission.exists():
        details["submission"] = read(submission)
    debug_root = root / "control/debug" / iteration
    request = workspace / "evaluation-request.json"
    result = workspace / "evaluation-result.json"
    if request.exists() or request.is_symlink() or result.is_symlink():
        raise ProTokError("Pending or symlinked evaluation interface prevents debug")
    if result.exists():
        value = read(result)
        if value.get("status") not in {"error", "candidate_failed"} or any(
            name in value for name in ("value", "metric", "measurements")
        ):
            raise ProTokError(
                "Unexpected evaluation result cannot be cleared for debug"
            )
        archived = (
            debug_root / "original-interface" / (job.name + "-evaluation-result.json")
        )
        archived.parent.mkdir(parents=True, exist_ok=True)
        if archived.exists() and archived.read_bytes() != result.read_bytes():
            raise ProTokError("An earlier failed interface record differs")
        if not archived.exists():
            archived.write_bytes(result.read_bytes())
        result.unlink()
    failure = job / "failure.json"
    if failure.exists():
        if read(failure).get("details") != details:
            raise ProTokError("Retained candidate failure evidence changed")
    else:
        immutable_record(
            failure,
            {"version": 1, "details": details, "previous_status": record["status"]},
        )
    if record.get("status") != "failed":
        record.update(status="failed", failure_ref=str(failure), failed_at=time.time())
        write(job / "job.json", record)
    return details


def completed_job(root, backend, workspace, iteration):
    """Repair only publication of a frozen, fully settled backend measurement."""
    root, workspace = Path(root), Path(workspace)
    job = job_path(root, iteration)
    record = read(job / "job.json")
    check_fixed_candidate(root, workspace, iteration, job / "source")
    expected_action = "import" if iteration == "0001" else "evaluate"
    if (
        record.get("iteration") != iteration
        or record.get("request") != {"action": expected_action}
        or record.get("status") not in {"prepared", "submitted", "completed"}
        or not record.get("fingerprint")
        or not record.get("source_commit")
    ):
        raise ProTokError("Job has no recoverable frozen candidate identity")
    baseline = read(root / "study/study.json")["baseline"]
    source = job / "source"
    for candidate in (workspace, source):
        check_scope(
            candidate, baseline, allow_research=research_policy(root) is not None
        )
        if signatures(candidate_bytes(candidate)) != record["source_signatures"]:
            raise ProTokError("Retained candidate no longer matches frozen job source")
    if git(source, "rev-parse", "HEAD") != record["source_commit"]:
        raise ProTokError("Frozen job source commit changed")
    receipt = backend.completed_receipt(
        source,
        job / "runtime-config.json",
        record["fingerprint"],
        **({"record_name": record["record_name"]} if record.get("record_name") else {}),
    )
    if expected_action == "import":
        imported = import_best(backend, receipt)
        if imported["value"] != receipt["value"]:
            raise ProTokError("Historical import disagrees with backend receipt")
    result = {
        **receipt,
        "status": "completed",
        "source_commit": record["source_commit"],
        "artifact": {
            "path": str(workspace / CANDIDATE_FILES[2]),
            "version": record["fingerprint"],
        },
        "config_snapshot": str(job / "runtime-config.json"),
        "frozen_source": str(source),
    }
    path = job / "result.json"
    if path.exists():
        saved = read(path)
        # Keep original process/log evidence; revalidate its measured identity.
        for name in (
            "status",
            "source_commit",
            "artifact",
            "config_snapshot",
            "frozen_source",
            "fingerprint",
            "metric",
            "value",
            "verified",
            "evidence",
        ):
            if saved.get(name) != result[name]:
                raise ProTokError(
                    "Published job result does not match authoritative receipt"
                )
        result = saved
    else:
        write(path, result)
    if record["status"] != "completed":
        record.update(status="completed", completed_at=time.time())
        write(job / "job.json", record)
    return result


def timed_out_job(root, backend, workspace, iteration):
    """Retain a settled formal time limit as an unmeasured investigation result."""
    return _failed_formal_job(root, backend, workspace, iteration)


def no_allocation_job(
    root, backend, workspace, iteration, *, evidence_dir=None, authorization=None
):
    """Retain an operator-certified submission failure with no allocation or score."""
    return _failed_formal_job(
        root,
        backend,
        workspace,
        iteration,
        no_allocation=True,
        evidence_dir=evidence_dir,
        authorization=authorization,
    )


def _failed_formal_job(
    root,
    backend,
    workspace,
    iteration,
    *,
    no_allocation=False,
    evidence_dir=None,
    authorization=None,
):
    root, workspace = Path(root), Path(workspace)
    job = job_path(root, iteration)
    record = read(job / "job.json")
    check_fixed_candidate(root, workspace, iteration, job / "source")
    if (
        record.get("iteration") != iteration
        or record.get("request") != {"action": "evaluate"}
        or record.get("status") not in {"prepared", "submitted", "failed"}
        or not record.get("fingerprint")
        or not record.get("source_commit")
    ):
        raise ProTokError("Job has no retained formal timeout identity")
    baseline = read(root / "study/study.json")["baseline"]
    for source in (workspace, job / "source"):
        check_scope(source, baseline, allow_research=research_policy(root) is not None)
        if signatures(candidate_bytes(source)) != record["source_signatures"]:
            raise ProTokError("Timed-out candidate changed after source freeze")
    if git(job / "source", "rev-parse", "HEAD") != record["source_commit"]:
        raise ProTokError("Timed-out source commit changed")
    submission = workspace / RECEIPT
    request = workspace / "evaluation-request.json"
    if (
        submission.is_symlink()
        or not submission.exists()
        or read(submission) != record["request"]
        or request.exists()
        or request.is_symlink()
        or (workspace / "evaluation-result.json").is_symlink()
    ):
        raise ProTokError("Timed-out submission has changed or pending effects")
    if no_allocation:
        from integrations.protok.no_allocation import receipt as no_allocation_receipt

        def receipt_reader(*args, **kwargs):
            return no_allocation_receipt(
                backend,
                *args,
                **kwargs,
                evidence_dir=evidence_dir,
                authorization=authorization,
            )
    else:
        receipt_reader = backend.timed_out_receipt
    receipt = receipt_reader(
        job / "source",
        job / "runtime-config.json",
        record["fingerprint"],
        **({"record_name": record["record_name"]} if record.get("record_name") else {}),
    )
    marker = job / ("no-allocation.json" if no_allocation else "timeout.json")
    if marker.exists():
        if read(marker).get("receipt") != receipt:
            raise ProTokError("Retained timeout evidence changed")
    else:
        immutable_record(
            marker,
            {"version": 1, "previous_job": record, "receipt": receipt},
        )
    result = {
        **receipt,
        "source_commit": record["source_commit"],
        "config_snapshot": str(job / "runtime-config.json"),
        "frozen_source": str(job / "source"),
        "artifact": {"path": str(marker), "version": receipt["record_name"]},
    }
    immutable_record(job / "result.json", result)
    if record["status"] != "failed":
        record.update(status="failed", failure_ref=str(marker), failed_at=time.time())
        write(job / "job.json", record)
    return result


def settled_failure_response(trusted, *, agent_error=None):
    """An accounted failed allocation is evidence, without a quality measurement."""
    if trusted.get("status") != "failed":
        raise ProTokError("A settled failed execution receipt is required")
    if trusted.get("failure_kind") == "operator_certified_no_allocation":
        return {
            "outcome": "incomplete",
            "markdown": (
                "Operator-certified submission failure with no allocation. Successful "
                "empty scheduler account queries covering submission, a working known-job "
                "control, and missing job-ID evidence support this certification; no "
                "Slurm terminal state is asserted. Zero GPU seconds are charged under "
                "this certification. Original failed submission, source, Plan and Agent "
                "attempt remain evidence. No quality measurement exists. Independent "
                "Review must assess this incomplete experiment and its limitations. "
                "The candidate is not resubmitted."
            ),
            "artifacts": [trusted["artifact"]],
            "measurements": [],
        }
    return {
        "outcome": "incomplete",
        "markdown": (
            "Controller-recorded settled execution failure: "
            + str(trusted.get("terminal_state") or trusted.get("error") or "failed")
            + "; actual GPU allocation charge "
            + str(trusted["gpu_seconds"])
            + " seconds. Original source, Plan, Agent records and collected artifacts "
            "remain evidence. No quality measurement was produced. Independent Review "
            "must assess the incomplete experiment and decide the next investigation "
            "within the remaining original budget; this allocation is not resubmitted."
            + (
                " Agent reporting ended with "
                + type(agent_error).__name__
                + ": "
                + str(agent_error)
                if agent_error is not None
                else ""
            )
        ),
        "artifacts": [trusted["artifact"]],
        "measurements": [],
    }


class ProTokHarness:
    def __init__(self, backend, root, delegate, deadline):
        self.backend, self.root, self.delegate, self.deadline = (
            backend,
            Path(root),
            delegate,
            deadline,
        )
        existing_secrets = getattr(delegate, "diagnostic_secrets", ())
        if not isinstance(existing_secrets, (tuple, list)):
            existing_secrets = ()
        self.diagnostic_secrets = diagnostic_secrets(
            (getattr(backend, "owner_token", None), *existing_secrets)
        )
        if hasattr(delegate, "diagnostic_secrets"):
            delegate.diagnostic_secrets = self.diagnostic_secrets
        self.errors = []
        self.trusted_result = None
        self.recoverable_failure = None
        self.pipeline_diagnostics = None

    def admit_iteration(self, iteration):
        from integrations.protok.round_admission import admit

        admission(self.root, self.backend, require_idle=False)
        return admit(self.root, iteration, owner_token=self.backend.owner_token)

    def commit_iteration(self, iteration, token):
        from integrations.protok.round_admission import commit

        return commit(self.root, iteration, token, owner_token=self.backend.owner_token)

    def call_allowance(self, remaining):
        from integrations.protok.round_admission import active_round

        if active_round(self.root, owner_token=self.backend.owner_token):
            return 3600
        return remaining

    def evaluate_request(self, workspace, iteration, request, stop, deadline):
        check_fixed_candidate(self.root, workspace, iteration)
        if request == {"action": "research"}:
            from integrations.protok.research_execution import execute_script

            return execute_script(self, workspace, iteration, stop, deadline)
        if request not in ({"action": "import"}, {"action": "evaluate"}):
            raise ProTokError("Request must contain exactly action=import or evaluate")
        if (iteration == "0001") != (request["action"] == "import"):
            raise ProTokError(
                "Only the first iteration imports; subsequent iterations evaluate"
            )
        if stop.is_set() or time.time() >= deadline:
            raise ProTokError("Execute ended; no evaluation may launch")
        admission(self.root, self.backend)
        research_enabled = research_policy(self.root) is not None
        if research_enabled:
            from integrations.protok.research_execution import frozen_contract

            contract = frozen_contract(self.root, iteration, required=False)
            if contract and contract["execution"]["mode"] != "fixed_evaluation":
                raise ProTokError(
                    "Formal submission differs from the frozen execution mode"
                )
        baseline = read(self.root / "study/study.json")["baseline"]
        check_scope(workspace, baseline, allow_research=research_enabled)
        before = signatures(candidate_bytes(workspace))
        metadata = Path(workspace).parent / "iteration.json"
        recovery_metadata = read(metadata) if metadata.exists() else {}
        recovery = recovery_metadata.get("continuation") or recovery_metadata.get(
            "operator_resume"
        )
        if recovery and before != recovery["source_signatures"]:
            raise ProTokError("Operator continuation changed the unmeasured candidate")
        job = self.root / "control/jobs" / iteration
        if job.exists():
            saved = read(job / "job.json")
            if saved["request"] != request or saved["source_signatures"] != before:
                raise ProTokError("Existing job cannot be replaced or rewound")
            if (job / "no-allocation.json").exists():
                return no_allocation_job(self.root, self.backend, workspace, iteration)
            if (job / "timeout.json").exists():
                return timed_out_job(self.root, self.backend, workspace, iteration)
            if saved["status"] != "completed" and not (job / "result.json").exists():
                try:
                    return timed_out_job(self.root, self.backend, workspace, iteration)
                except (ProTokError, OSError, ValueError, KeyError) as error:
                    raise ProTokError(
                        "Existing unfinished job has unknown effects; explicit recovery required"
                    ) from error
            return completed_job(self.root, self.backend, workspace, iteration)
        job.mkdir(parents=True)
        record = {
            "status": "preparing",
            "request": request,
            "source_signatures": before,
            "iteration": iteration,
            "created_at": time.time(),
            "execution_deadline": deadline,
        }
        write(job / "job.json", record)
        commit, frozen = freeze_source(
            self.backend.project,
            workspace,
            job / "source",
            baseline,
            allow_research=research_enabled,
        )
        check_fixed_candidate(self.root, workspace, iteration, job / "source")
        prepared = self.backend.prepare_config(
            job / "source", job / "runtime-config.json"
        )
        record.update(
            status="prepared", source_commit=commit, fingerprint=prepared["fingerprint"]
        )
        write(job / "job.json", record)
        if stop.is_set() or time.time() >= deadline:
            raise ProTokError("Execute ended before fixed evaluator admission")
        if request["action"] == "import":
            receipt = import_best(self.backend, prepared)
        else:
            record["status"] = "submitted"
            write(job / "job.json", record)
            receipt = self.backend.evaluate(
                job / "source",
                job / "runtime-config.json",
                timeout_seconds=max(0.01, deadline - time.time()),
                cancel_event=stop,
            )
            receipt["provenance"] = "new-fixed-protocol-evaluation"
        check_scope(workspace, baseline, allow_research=research_enabled)
        check_fixed_candidate(self.root, workspace, iteration, job / "source")
        if signatures(candidate_bytes(workspace)) != frozen:
            raise ProTokError("Agent candidate changed after source freeze")
        if (
            receipt.get("verified") is not True
            or receipt.get("metric") != METRIC
            or receipt.get("fingerprint") != prepared["fingerprint"]
            or type(receipt.get("value")) not in (int, float)
            or not math.isfinite(receipt["value"])
        ):
            raise ProTokError(
                "Fixed backend did not return a verified finite candidate-bound metric"
            )
        result = {
            **receipt,
            "status": "completed",
            "source_commit": commit,
            "artifact": {
                "path": str(Path(workspace) / CANDIDATE_FILES[2]),
                "version": prepared["fingerprint"],
            },
            "config_snapshot": str(job / "runtime-config.json"),
            "frozen_source": str(job / "source"),
        }
        write(job / "result.json", result)
        record.update(status="completed", completed_at=time.time())
        write(job / "job.json", record)
        return result

    def _watch(self, workspace, iteration, stop, deadline):
        try:
            self._watch_request(workspace, iteration, stop, deadline)
        except Exception as error:
            # A controller defect is a pipeline failure, never evidence for
            # candidate recovery or an allocation timeout. Report it to both
            # the waiting Agent and the main thread before ending this watcher.
            diagnostics = diagnostic_view(
                error,
                owner="pipeline",
                stage="evaluation_controller",
                secrets=self.diagnostic_secrets,
            )
            message = diagnostics["cause_chain"][0]["message"]
            self.recoverable_failure = None
            self.pipeline_diagnostics = diagnostics
            self.errors.append(message)
            try:
                write(
                    workspace / "evaluation-result.json",
                    {
                        "status": "error",
                        "error": message,
                        "diagnostics": diagnostics,
                        "retry": False,
                    },
                )
            except (OSError, ValueError):
                # The main thread still surfaces the original pipeline failure
                # if the workspace itself cannot accept a diagnostic result.
                pass

    def _watch_request(self, workspace, iteration, stop, deadline):
        request_path = workspace / "evaluation-request.json"
        while not stop.wait(0.1):
            if not request_path.exists():
                continue
            try:
                request = claim_request(workspace)
                if request is None:
                    continue
                result = self.evaluate_request(
                    workspace, iteration, request, stop, deadline
                )
                self.trusted_result = result
            except Exception as error:
                diagnostics = getattr(error, "diagnostics", None) or diagnostic_view(
                    error,
                    stage="formal",
                    secrets=self.diagnostic_secrets,
                )
                result = {
                    "status": "error",
                    "error": diagnostics["cause_chain"][0]["message"],
                    "diagnostics": diagnostics,
                    "retry": False,
                }
                try:
                    self.recoverable_failure = certify_candidate_failure(
                        self.root, self.backend, workspace, iteration
                    )
                except (ProTokError, OSError, ValueError, KeyError):
                    pass
                else:
                    if self.recoverable_failure.get("diagnostics"):
                        result["diagnostics"] = {
                            **self.recoverable_failure["diagnostics"],
                            "cause_chain": diagnostics["cause_chain"],
                            "sources": [
                                diagnostics,
                                self.recoverable_failure["diagnostics"],
                            ],
                            "owner": "workspace",
                        }
                    result.update(
                        status="candidate_failed",
                        error=self.recoverable_failure["cause"],
                        debug_available=(
                            self.root / "control/debug-policy.json"
                        ).exists(),
                    )
                if self.recoverable_failure is None:
                    try:
                        trusted = timed_out_job(
                            self.root, self.backend, workspace, iteration
                        )
                    except (ProTokError, OSError, ValueError, KeyError, AttributeError):
                        self.errors.append(result["error"])
                    else:
                        result = self.trusted_result = trusted
                else:
                    self.errors.append(result["error"])
            if result.get("diagnostics"):
                job = job_path(self.root, iteration)
                if job.is_dir():
                    write(job / "failure-diagnostics.json", result["diagnostics"])
            write(workspace / "evaluation-result.json", result)
            return

    def run(self, **kwargs):
        from integrations.protok.round_admission import execution_deadline

        deadline = execution_deadline(
            self.root,
            self.deadline,
            min(kwargs["timeout_seconds"], 3600),
            owner_token=self.backend.owner_token,
            attempt=kwargs.get("run_dir", "untracked-agent"),
        )
        remaining = int(deadline - time.time())
        if remaining <= 0 or (self.root / "control/STOP").exists():
            raise HarnessError("Continuation deadline reached or control/STOP set")
        kwargs["timeout_seconds"] = min(kwargs["timeout_seconds"], remaining, 3600)
        kwargs["prompt"] += "\n\n" + submission_instructions()
        context_path = (
            Path(kwargs["run_dir"]) / "context.json" if kwargs.get("run_dir") else None
        )
        manual = (
            read(context_path).get("manual_task")
            if context_path is not None and context_path.exists()
            else None
        )
        if manual:
            kwargs["prompt"] += (
                "\nThe user task fixes this investigation's question and exact source. "
                "Its selected source replaces the automatic preference to start from the "
                "current Frontier best. Preserve all protected evaluation/data/resource "
                "rules. Do not change candidate source when allow_changes=false; the "
                "evaluation transport is still permitted.\n"
            )
        kwargs["prompt"] += """
For a controller-certified formal result with status=failed and
failure_kind=allocation_timeout, report the settled incomplete execution. The
controller attaches its exact artifact and no measurements. Preserve the time limit and actual
GPU charge; do not invent a score, shorten the evaluation, or submit again.
A settled time limit ends this investigation's execution and enters independent
Review. It does not establish model quality or mechanism, but is evidence of
execution cost and feasibility under the fixed time budget.
Review should retain the measured best, record the missing evidence, and continue
unless an independent global reason requires stopping. Unresolved effects,
identity/accounting failures and user STOP remain blocking. The short entry cutoff
blocks the next investigation; this admitted investigation may finish through Review.
Individual Agent, frozen Plan and Slurm allocation time limits remain in force.
"""
        from integrations.protok.research_execution import instructions

        kwargs["prompt"] += instructions(self.root)
        environment = self.root / "control/execution-environment.json"
        if environment.exists():
            kwargs["prompt"] += (
                "\n\nOperator-supplied execution environment from trusted control "
                "(factual capabilities; existing scope, frozen contract and protected "
                "evaluation checks remain unchanged):\n"
                + environment.read_text(encoding="utf-8")
            )
        if not kwargs["writable"]:
            schema = kwargs.get("schema", {})
            plan_attempt = None
            if "research_contract" in schema.get("properties", {}):
                kwargs["schema"] = copy.deepcopy(schema)
                contract_schema = kwargs["schema"]["properties"]["research_contract"][
                    "anyOf"
                ][0]
                input_schema = contract_schema["properties"]["execution"]["properties"][
                    "inputs"
                ]["items"]
                input_schema["properties"]["role"]["enum"] = [
                    "development",
                    "exploratory",
                    "model",
                    "reference",
                ]
                if kwargs.get("run_dir") is not None:
                    plan_attempt = Path(kwargs["run_dir"])
                    agent = plan_attempt / "agent"
                    agent.mkdir()
                    if (plan_attempt / "context.json").exists():
                        shutil.copyfile(
                            plan_attempt / "context.json", agent / "context.json"
                        )
                    kwargs["run_dir"] = agent
            result = self.delegate.run(**kwargs)
            contract = result.get("research_contract")
            if (
                result.get("decision") == "investigate"
                and contract
                and contract["execution"]["mode"] == "script"
            ):
                from integrations.protok.research_backend import _execution

                try:
                    _execution(contract["execution"], research_policy(self.root))
                except ProTokError as error:
                    raise HarnessError(str(error)) from error
            if plan_attempt is not None:
                publish_completed_response(plan_attempt, result)
            return result
        kwargs["schema"] = REPORT_SCHEMA
        kwargs["prompt"] += REPORT_INSTRUCTIONS
        self.trusted_result = None
        self.errors = []
        self.recoverable_failure = None
        self.pipeline_diagnostics = None
        workspace = Path(kwargs["workspace"])
        outer = Path(kwargs["run_dir"]) if "run_dir" in kwargs else None
        if outer is not None:
            agent = outer / "agent"
            agent.mkdir()
            if (outer / "context.json").exists():
                shutil.copyfile(outer / "context.json", agent / "context.json")
            kwargs["run_dir"] = agent
        # Parent checkpoints carry receipts; same-iteration receipts must survive reentry.
        result_path = workspace / "evaluation-result.json"
        request_path = workspace / "evaluation-request.json"
        iteration = workspace.parent.name
        initialize_submission_workspace(self.root, workspace, iteration)
        if submission_status(workspace)["status"] != "not_submitted":
            raise HarnessError("Existing evaluation submission requires reconciliation")
        stop = threading.Event()
        deadline = min(deadline, time.time() + kwargs["timeout_seconds"] - 5)
        thread = threading.Thread(
            target=self._watch, args=(workspace, iteration, stop, deadline)
        )
        thread.start()
        delegate_error = None
        try:
            result = report_response(self.delegate.run(**kwargs))
        except (HarnessError, OSError) as error:
            delegate_error = error
            result = None
        finally:
            stop.set()
            # evaluate has the same finite deadline; settle effects before returning.
            thread.join()
        if self.errors:
            if self.recoverable_failure is not None:
                self.recoverable_failure = certify_candidate_failure(
                    self.root, self.backend, workspace, iteration
                )
                raise RecoverableExecutionError(
                    self.recoverable_failure["cause"], details=self.recoverable_failure
                )
            failure = HarnessError(self.errors[-1])
            failure.diagnostics = self.pipeline_diagnostics
            raise failure
        if self.trusted_result and self.trusted_result.get("status") == "failed":
            # The controller result, not Agent availability, defines a settled failure.
            if self.trusted_result.get("purpose") == "research":
                from integrations.protok.research_execution import completed_script

                trusted = completed_script(self.root, workspace, iteration)
            else:
                trusted = timed_out_job(self.root, self.backend, workspace, iteration)
            if trusted != self.trusted_result or read(result_path) != trusted:
                raise HarnessError("Settled failure interface differs from its receipt")
            result = settled_failure_response(trusted, agent_error=delegate_error)
        elif delegate_error is not None:
            raise delegate_error
        if result.get("outcome") == "incomplete" and self.trusted_result is None:
            retain_local_incomplete(
                self.root,
                self.backend,
                workspace,
                iteration,
                result,
                run_dir=outer,
                completed=True,
            )
            if outer is not None:
                publish_completed_response(outer, result)
            return result
        if request_path.exists():
            raise HarnessError("A duplicate evaluation request was left after Execute")
        if not result_path.exists() or (
            read(result_path).get("status") != "completed"
            and not (
                self.trusted_result
                and self.trusted_result.get("purpose") in {"research", "formal"}
                and self.trusted_result.get("status") == "failed"
            )
        ):
            raise HarnessError("Execute ended without a completed trusted evaluation")
        trusted = self.trusted_result
        if trusted is None or read(result_path) != trusted:
            raise HarnessError("Agent altered the trusted evaluation interface result")
        # Model prose is evidence/interpretation; candidate identity and measurements
        # are constructed by the controller and then independently revalidated below.
        result = receipt_response(trusted, result["markdown"])
        result = self.validate_completed(result, workspace=workspace, run_dir=outer)
        if outer is not None:
            publish_completed_response(outer, result)
        return result

    def diagnostic_context(self, iteration):
        """Return retained public diagnostics for this iteration, without execution."""
        if not isinstance(iteration, str) or not re.fullmatch(r"[0-9]{4,}", iteration):
            raise ProTokError("Invalid diagnostic iteration")
        views = []
        job = job_path(self.root, iteration)
        original = self.root / "control/jobs" / iteration
        for directory in dict.fromkeys((original, job)):
            public = directory / "failure-diagnostics.json"
            if public.is_file() and not public.is_symlink():
                views.append(read(public))
            failure = directory / "failure.json"
            if failure.is_file() and not failure.is_symlink():
                view = read(failure).get("details", {}).get("diagnostics")
                if view:
                    views.append(view)
        research = self.root / "control/research-jobs" / iteration
        for name in ("receipt.json", "blocked.json"):
            receipt = research / name
            if receipt.is_file() and not receipt.is_symlink():
                view = read(receipt).get("diagnostics")
                if view:
                    views.append(view)
        debug = self.root / "control/debug" / iteration
        if (debug / "flow.json").is_file():
            for failure in read(debug / "flow.json").get("failures", []):
                if failure.get("diagnostics"):
                    views.append(failure["diagnostics"])
        return views

    def recover_execution(self, failure, *, workspace, run_dir, context, invoke):
        from integrations.protok.debug_flow import recover_execution

        return recover_execution(
            self,
            failure,
            workspace=workspace,
            run_dir=run_dir,
            context=context,
            invoke=invoke,
        )

    def validate_completed(self, result, *, workspace, run_dir):
        """Validate retained process output before accepting an Execute receipt."""
        validate_response("execute", result)
        workspace = Path(workspace)
        iteration = workspace.parent.name
        fixed = check_fixed_candidate(self.root, workspace, iteration)
        if fixed is not None:
            job = job_path(self.root, iteration)
            if (job / "job.json").exists():
                check_fixed_candidate(self.root, workspace, iteration, job / "source")
        submission = workspace / RECEIPT
        if submission.exists() and read(submission) == {"action": "research"}:
            from integrations.protok.research_execution import completed_script

            trusted = completed_script(self.root, workspace, iteration)
            if (
                result["outcome"]
                != ("completed" if trusted["status"] == "completed" else "incomplete")
                or result["artifacts"] != [trusted["artifact"]]
                or result["measurements"]
                or (workspace / "evaluation-request.json").exists()
                or read(workspace / "evaluation-result.json") != trusted
            ):
                raise HarnessError(
                    "Execute response does not match the unscored research receipt"
                )
            return result
        if result["outcome"] == "incomplete":
            decision = self.root / "control/debug" / iteration / "decision.json"
            if decision.exists():
                from research_loop.debug_budget import DebugBudget

                saved = read(decision)
                flow = read(decision.parent / "flow.json")
                if fixed is not None:
                    trusted_failure = certify_candidate_failure(
                        self.root, self.backend, workspace, iteration
                    )
                    if saved.get("source_signatures") != fixed or saved.get(
                        "failures"
                    ) != [trusted_failure]:
                        raise HarnessError("Fixed-candidate recovery evidence changed")
                policy = (
                    debug_policy(self.root)
                    if (self.root / "control/debug-policy.json").exists()
                    else None
                )
                if flow["policy"] != policy:
                    raise HarnessError("Recovery authorization changed after decision")
                budget = (
                    DebugBudget(
                        decision.parent, scope_id=iteration, limit_gpu_seconds=1800
                    ).status()
                    if policy is not None
                    else saved["budget"]
                )
                if policy is None and (
                    budget.get("limit_gpu_seconds") != 0
                    or budget.get("used_gpu_seconds") != 0
                    or (decision.parent / "debug-budget.json").exists()
                ):
                    raise HarnessError("Unapproved recovery retained GPU effects")
                effects = self.backend.inspect()
                if (
                    saved.get("status") != "failed"
                    or saved.get("response") != result
                    or saved.get("source_signatures")
                    != signatures(candidate_bytes(workspace))
                    or result["artifacts"]
                    or result["measurements"]
                    or budget["active_attempt"] is not None
                    or budget["breached"]
                    or effects["outstanding"]
                    or effects.get("debug_outstanding")
                    or effects.get("research_outstanding")
                ):
                    raise HarnessError(
                        "Debug failure has unresolved effects or altered evidence"
                    )
                check_scope(
                    workspace,
                    read(self.root / "study/study.json")["baseline"],
                    allow_research=research_policy(self.root) is not None,
                )
                return result
            if (job_path(self.root, iteration) / "no-allocation.json").exists():
                trusted = no_allocation_job(
                    self.root, self.backend, workspace, iteration
                )
                if (
                    result["artifacts"] != [trusted["artifact"]]
                    or result["measurements"]
                    or read(workspace / "evaluation-result.json") != trusted
                ):
                    raise HarnessError(
                        "Execute response differs from certified no-allocation evidence"
                    )
                return result
            if (job_path(self.root, iteration) / "timeout.json").exists():
                trusted = timed_out_job(self.root, self.backend, workspace, iteration)
                if (
                    result["artifacts"] != [trusted["artifact"]]
                    or result["measurements"]
                    or not (workspace / "evaluation-result.json").exists()
                    or read(workspace / "evaluation-result.json") != trusted
                ):
                    raise HarnessError(
                        "Execute response does not match the settled formal timeout"
                    )
                return result
            retain_local_incomplete(
                self.root, self.backend, workspace, iteration, result, run_dir=run_dir
            )
            return result
        if (workspace / "evaluation-request.json").exists():
            raise HarnessError("A duplicate evaluation request was left after Execute")
        trusted = completed_job(self.root, self.backend, workspace, iteration)
        expected_artifact = trusted["artifact"]
        measurements = result["measurements"]
        if (
            result["outcome"] != "completed"
            or result["artifacts"] != [expected_artifact]
            or len(measurements) != 1
            or measurements[0]["candidate_id"] != trusted["fingerprint"]
            or measurements[0]["artifact"] != expected_artifact
            or measurements[0]["evaluation_id"] != PROTOCOL
            or measurements[0]["comparison_scope"] != PROTOCOL
            or measurements[0]["metrics"]
            != [{"name": METRIC, "value": trusted["value"]}]
            or not set(trusted["evidence"]).issubset(measurements[0]["evidence"])
        ):
            raise HarnessError(
                "Execute response does not match the trusted fixed evaluation"
            )
        return result


GOAL = f"""Minimize {METRIC} (Angstrom) under {PROTOCOL}; this is adaptive development evidence.
Fixed: 449 proteins, seed42, 2000 updates, Euler100, H100 debug 30min, 8CPU/96G/2workers.
Prefer improvements without increasing parameter count or inference work; scaling is not the primary route; retain the acknowledged historical best. Never change teacher/data/noise/scorer/fixed config,
canonical project, old journal, permissions, hardware, or protocol. No SSH/network/install,
subagents, direct training or other launch paths. Read only relevant source and evidence.
First iteration MUST audit/import historical best {BEST_FINGERPRINT}, {BEST_VALUE} A, without
changing any of the three candidate files or launching GPU work. base_experiment=null;
old run IDs are external evidence, never Study graph references. Subsequent proposals MUST
base on the current Global Frontier best experiment; explain mathematical invariants, masks,
and rationale in Plan before editing. A mechanism hypothesis is optional. Only editable files:
{", ".join(CANDIDATE_FILES)}. No additional code, report or scratch files.
During Execute use the controller-provided submission API with action=import
(first iteration) or action=evaluate (later), then wait for its actual result.
Use a bounded polling loop up to 3600s or the remaining window; timeout_seconds in Plan should
allow this, at most 3600 seconds. Repeated API calls observe the same submission.
The API's temporary file, lock and receipt are permitted transport files; never hand-write
or delete interface files. The fixed bridge freezes and verifies source and returns actual
receipt, fingerprint, metric/value, evidence and provenance. Return only the execution
report requested by the adapter; it constructs the artifact and measurement from the
verified receipt, including candidate identity, protocol, metric and evidence paths.
Local prechecks should be minimal and relevant to the changed computation on ordinary,
nondegenerate inputs and padding. The fixed backend already checks actual initialization,
training gradients and final model invariants; do not duplicate an exhaustive CPU suite.
Artificial corner cases, especially manually perturbed weights with exactly collinear
coordinates, are NON-BLOCKING diagnostics. The user explicitly requested that these
cases must not obstruct the normal research path. Retain any observed failures as
warnings with their conditions; do not require diagnosing or fixing them before the
fixed evaluation. Do not rerun known artificial stress failures for unrelated candidates.
A corner warning alone does not make Execute incomplete, disqualify a measured candidate,
pause a route or stop the Study. Review must continue after recording such warnings.
This amendment applies to future Plans, not to rewriting past frozen contracts or results.
Ordinary-case failures, actual training nonfinite values, fixed backend gate failures,
invalid metrics, transport errors and unknown evaluation effects still require stopping
dependent work and preserving receipts. Never weaken or edit those fixed checks.
On such an actual error return incomplete with no measurements; do not fabricate or relaunch.
For a local failure before any evaluation request, return incomplete with artifacts=[]
and measurements=[] and report the actual failure and uncertainty. The controller may
accept an unmeasured candidate rejection for independent Review only after demonstrating
a Python syntax error or invalid candidate JSON in the three authorized candidate files.
Review then uses candidate_reviews=[], retains the measured best, and records the fault.
An unconfirmed local precheck failure, transport error, or unknown evaluation effect
requires pausing the original Execute and preserving its Plan for reconciliation.
Independent Review must inspect actual source and receipt, verify measurement, preserve
limitations, update both route/global research, and continue. A worse candidate is useful
evidence, not a reason to stop. A controller-certified, fully accounted formal
TIMEOUT is an incomplete investigation, with no score; Review records the limitation
and may continue. Stop dependent work on unresolved effects, identity/accounting
failures, user STOP or exhausted authorization, not solely on a settled time limit.
"""


def init_study(root, backend):
    require_baseline()
    deadline = admission(root, backend)
    baseline = Path(root) / "baseline"
    if baseline.exists():
        raise ProTokError(
            "Baseline already exists; initialization is not a reset operation"
        )
    if (
        git(backend.project, "status", "--porcelain")
        or git(backend.project, "rev-parse", "HEAD") != BEST_COMMIT
    ):
        raise ProTokError("Canonical best must be clean and at its acknowledged commit")
    subprocess.run(
        ["git", "clone", "--shared", "--quiet", str(backend.project), str(baseline)],
        check=True,
    )
    config = HarnessConfig(
        executable=CODEX, model="gpt-6.1-sol", reasoning="medium", timeout_seconds=3600
    )
    from integrations.protok.long_training_loop import study_policy as long_study_policy

    initialize(
        Path(root) / "study",
        goal=GOAL,
        workspace=baseline,
        allow_write=True,
        harness=config,
        max_iterations=None,
        max_calls=None,
        max_seconds=max(1, int(deadline - time.time())),
        long_training=long_study_policy(root),
        global_evaluation={
            "id": PROTOCOL,
            "comparison_scope": PROTOCOL,
            "protocol": "Fixed 449 development proteins, seed42, 2k optimizer updates, Euler100; historical import explicitly marked.",
            "data_role": "development",
            "allow_feedback": True,
            "objectives": [
                {"name": METRIC, "direction": "minimize", "unit": "Angstrom"}
            ],
            "constraints": [],
        },
    )
    report = (backend.project / "autoresearch-report.md").read_text()
    history = backend.inspect()
    (Path(root) / "study/research.md").write_text(
        "# Historical ProTok evidence\n\n"
        f"Imported narrative from canonical autoresearch-report.md; prior {history['scored_records']} scored records and {history['used_gpu_seconds']} GPU s remain historical. "
        "No old DAG nodes or new measurements are inferred. First iteration audits the best receipt.\n\n"
        + report
    )
    routes = {}
    for number, (title, question) in enumerate(
        (
            (
                "Loss",
                "Robust residual loss under the fixed mask and equal-protein aggregation.",
            ),
            (
                "Readout",
                "Equivariant readout using the established deployment contract.",
            ),
            (
                "Message geometry",
                "Geometry-aware messages preserving SE3 and padding invariants.",
            ),
        ),
        1,
    ):
        rid = f"R{number:04d}"
        folder = Path(root) / "study/routes" / rid
        folder.mkdir(parents=True)
        (folder / "research.md").write_text(
            f"# {title}\n\nHistorical direction, not a new measured experiment. {question}\nFirst audit/import the unchanged best before investigating new candidates.\n"
        )
        write(folder / "evaluation.json", {})
        routes[rid] = {
            "title": title,
            "status": "active",
            "reason": question,
            "resume_condition": None,
            "research_ref": f"routes/{rid}/research.md",
            "evaluation_ref": f"routes/{rid}/evaluation.json",
        }
    write(Path(root) / "study/routes/index.json", routes)
    return Study(Path(root) / "study").status()


def publish_control(root, study, backend):
    state = study.status()
    folder = Path(root) / "study/iterations" / f"{state['iteration']:04d}"
    if state["phase"] != "done" or not (folder / "publication.json").exists():
        raise ProTokError("Iteration has no published independent Review")
    candidates = study.frontiers()["global"][PROTOCOL]["candidates"]
    if not candidates:
        raise ProTokError(
            "Published iteration has no verified eligible global candidate"
        )
    best = min(candidates, key=lambda c: (c["metrics"][METRIC], c["id"]))
    metadata = read(
        Path(root) / "study/iterations" / best["experiment_id"] / "iteration.json"
    )
    write(
        Path(root) / "control/active-best.json",
        {
            **best,
            "workspace_ref": metadata["workspace_ref"],
            "workspace": metadata["workspace"],
            "canonical_project_unchanged": str(backend.project),
        },
    )
    journal = Path(root) / "control/journal.jsonl"
    existing = (
        [json.loads(line) for line in journal.read_text().splitlines()]
        if journal.exists()
        else []
    )
    identity = f"{state['iteration']:04d}"
    if not any(row["iteration"] == identity for row in existing):
        row = {
            "iteration": identity,
            "publication": str(folder / "publication.json"),
            "best_candidate": best["id"],
            "best_value": best["metrics"][METRIC],
            "old_ledger": str(backend.project / "autoresearch.jsonl"),
            "backend_accounting": backend.inspect(),
            "published_at": time.time(),
        }
        temporary = journal.with_name(journal.name + ".tmp")
        temporary.write_text(
            "".join(
                json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n"
                for item in [*existing, row]
            )
        )
        temporary.replace(journal)
    return state


def _repair_completed_control(root, backend, study):
    with study._lock():
        state = study.status()
        folder = study.root / "iterations" / f"{state['iteration']:04d}"
        publication = folder / "publication.json"
        if state["phase"] == "review" and publication.exists():
            active = state.get("active_attempt")
            if active:
                study._check_attempt_stopped(study.root / active)
            study._publish(state, folder, read(publication))
        if state["phase"] == "done" and publication.exists():
            publish_control(root, study, backend)
            return True
        return False


def repair_completed_control(root, backend):
    """Finish retained Review/control publication, without agents or allocation.

    Supervisors may call this before STOP/deadline/new-allocation admission.
    """
    root = Path(root)
    with (root / "control/runner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        study = Study(root / "study")
        repaired = _repair_completed_control(root, backend, study)
        if repaired:
            state = study.status()
            path = root / "control/runner.json"
            previous = read(path) if path.exists() else None
            if not previous or (
                previous.get("status") != "completed"
                or previous.get("iteration") != state["iteration"]
            ):
                folder = root / "control/reconciliations"
                number = len(list(folder.glob("runner-publication-*.json"))) + 1
                evidence = folder / f"runner-publication-{number:03d}.json"
                immutable_record(
                    evidence,
                    {
                        "operator_action": "repair-retained-review-control-publication",
                        "iteration": state["iteration"],
                        "previous_runner": previous,
                    },
                )
                record = dict(previous or {})
                record.pop("error", None)
                record.update(
                    status="completed",
                    iteration=state["iteration"],
                    ended_at=time.time(),
                    completion_provenance="retained-review-and-control-publication-repair",
                    reconciliation=str(evidence),
                )
                write(path, record)
        return repaired


def _run_one(root, backend):
    study = Study(Path(root) / "study")
    _repair_completed_control(root, backend, study)
    state = study.status()
    deadline = admission(root, backend, require_idle=not state.get("debugging"))
    from integrations.protok.resume_submission import begin_pending

    begin_pending(study, backend)
    config = HarnessConfig(**study.config["harness"])
    study.harness = ProTokHarness(backend, root, CodexHarness(config), deadline)
    try:
        state = study.run(iterations=1)
        publish_control(root, study, backend)
        return state
    except Exception as error:
        write(
            Path(root) / "control/status.json",
            {
                "status": "paused",
                "error": f"{type(error).__name__}: {error}",
                "at": time.time(),
            },
        )
        raise


def run_one(root, backend):
    control = Path(root) / "control"
    with (control / "runner.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ProTokError("Another ProTok runner is active") from error
        record = {
            "status": "running",
            "pid": os.getpid(),
            "started_at": time.time(),
            "iteration_before": read(Path(root) / "study/state.json")["iteration"],
        }
        write(control / "runner.json", record)
        write(
            control / "status.json",
            {**record, "phase": read(Path(root) / "study/state.json")["phase"]},
        )
        try:
            state = _run_one(root, backend)
            record.update(
                status="completed" if state["status"] == "ready" else "stopped",
                iteration=state["iteration"],
            )
            write(control / "status.json", {**record, "phase": state["phase"]})
            return state
        except Exception as error:
            record.update(status="paused", error=f"{type(error).__name__}: {error}")
            write(control / "status.json", record)
            raise
        finally:
            record["ended_at"] = time.time()
            write(control / "runner.json", record)


def begin_debug(root, backend):
    """Opt a retained, certified rejection into authorized bounded recovery."""
    root = Path(root)
    study = Study(root / "study")
    with (root / "control/runner.lock").open("a") as lock, study._lock():
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = study.status()
        if state["phase"] != "execute" or state["status"] not in {
            "needs_attention",
            "running",
            "ready",
        }:
            raise ProTokError("Only an interrupted failed Execute may enter debug")
        relative = state["calls"].get("execute")
        if not relative:
            raise ProTokError("Failed Execute has no retained attempt")
        attempt = study.root / relative
        study._check_attempt_stopped(attempt)
        iteration = f"{state['iteration']:04d}"
        if state.get("debugging"):
            failure = read(study.root / state["debugging"])
            if (
                failure.get("version") != 1
                or failure["details"]["iteration"] != iteration
            ):
                raise ProTokError("Retained debug identity differs")
            return state
        previous = dict(state)
        workspace = Path(read(study._folder(state) / "iteration.json")["workspace"])
        details = certify_candidate_failure(root, backend, workspace, iteration)
        record = {"version": 1, "error": details["cause"], "details": details}
        immutable_record(attempt / "failure.json", record)
        control_record = (
            root / "control/reconciliations" / f"begin-debug-{iteration}.json"
        )
        stop = root / "control/STOP"
        immutable_record(
            control_record,
            {
                "action": "begin-authorized-debug",
                "state_before": previous,
                "failure": record,
                "stop_before": stop.read_text() if stop.exists() else None,
            },
        )
        archive = control_record.with_suffix("")
        archive.mkdir(parents=True, exist_ok=True)
        current_status = root / "control/status.json"
        if current_status.exists() and not (archive / "status.before.json").exists():
            shutil.copyfile(current_status, archive / "status.before.json")
        write(
            current_status,
            {
                "status": "debug_prepared",
                "iteration": iteration,
                "cause": details["cause"],
                "failure_ref": str(attempt / "failure.json"),
                "debug_gpu_seconds_limit": 1800,
            },
        )
        state.update(
            debugging=str((attempt / "failure.json").relative_to(study.root)),
            status="ready",
            active_attempt=None,
            error=None,
        )
        study._save(state)
        return state


def reconcile_debug_allocations(root, backend):
    """Cancel/account existing debug work under STOP or expired deadlines."""
    root = Path(root)
    for path in sorted((root / "control/debug").glob("*/debug-budget.json")):
        active = read(path).get("active_attempt")
        if active is None:
            continue
        debug = path.parent
        saved = read(debug / "attempts" / active / "debug.json")
        source = Path(saved["base_config"]["workspace"])
        config_path = debug / "attempts" / active / "recovery-config.json"
        if config_path.exists() and read(config_path) != saved["base_config"]:
            raise ProTokError("Retained debug recovery configuration differs")
        if not config_path.exists():
            write(config_path, saved["base_config"])
        result = backend.debug_smoke(
            source, config_path, debug, active, saved["deadline"], recover=True
        )
        if result.get("status") == "blocked":
            raise ProTokError("Debug allocation is still unaccounted")


def reconcile_completed_execute(root, backend):
    """Operator reconciliation of an interrupted agent using real backend evidence.

    A new retained attempt explicitly records the operator action. The partial
    agent response/process are never promoted to a successful agent execution.
    Independent Review remains the next ordinary stage; STOP remains set.
    """
    root = Path(root)
    study = Study(root / "study")
    with (root / "control/runner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with study._lock():
            state = study.status()
            relative = state.get("active_attempt")
            if (
                state["phase"] != "execute"
                or state["status"] not in {"running", "needs_attention"}
                or not relative
                or state["calls"].get("execute") != relative
                or state.get("accepted_calls", {}).get("execute")
            ):
                raise ProTokError(
                    "Only an unaccepted interrupted Execute can be reconciled"
                )
            previous = study.root / relative
            for path in (previous, previous / "agent"):
                study._check_attempt_stopped(path)
            iteration = f"{state['iteration']:04d}"
            metadata = read(study.root / "iterations" / iteration / "iteration.json")
            workspace = Path(metadata["workspace"])
            trusted = completed_job(root, backend, workspace, iteration)
            response = {
                "outcome": "completed",
                "markdown": (
                    "Operator reconciliation of an interrupted Execute, using a verified "
                    "completed fixed-backend receipt; no agent completion is asserted. "
                    f"Observed {METRIC}={trusted['value']} under {PROTOCOL}; "
                    f"candidate {trusted['fingerprint']}. No new evaluation or agent call. "
                    "The original partial attempt is retained. Independent Review must "
                    "assess source, evidence and limitations; mechanism remains unverified."
                ),
                "artifacts": [trusted["artifact"]],
                "measurements": [
                    {
                        "candidate_id": trusted["fingerprint"],
                        "artifact": trusted["artifact"],
                        "evaluation_id": PROTOCOL,
                        "comparison_scope": PROTOCOL,
                        "metrics": [{"name": METRIC, "value": trusted["value"]}],
                        "evidence": trusted["evidence"],
                    }
                ],
            }
            ProTokHarness(backend, root, None, 0).validate_completed(
                response, workspace=workspace, run_dir=previous
            )
            directory = previous.parent
            attempt = (
                directory / f"attempt-{len(list(directory.glob('attempt-*'))) + 1:03d}"
            )
            attempt.mkdir()
            if (previous / "context.json").exists():
                shutil.copyfile(previous / "context.json", attempt / "context.json")
            record = {
                "operator_action": "reconcile-completed-backend-without-replay",
                "previous_attempt": relative,
                "backend_result": str(
                    root / "control/jobs" / iteration / "result.json"
                ),
                "source_signatures": signatures(candidate_bytes(workspace)),
                "call_count": state["call_count"],
                "elapsed_seconds": state["elapsed_seconds"],
            }
            write(attempt / "reconciliation.json", record)
            write(attempt / "response.json", response)
            publish_completed_response(attempt, response)
            state["calls"]["execute"] = str(attempt.relative_to(study.root))
            state.get("accepted_calls", {}).pop("execute", None)
            state.update(status="ready", active_attempt=None, error=None)
            study._save(state)
            return state


def reconcile_timed_out_execute(root, backend, *, authorization):
    """Record a settled formal timeout without repeating its Execute or Plan."""
    return _reconcile_settled_execute(root, backend, authorization=authorization)


def reconcile_research_execute(root, backend, *, authorization):
    """Accept the retained settled research failure without resubmitting compute."""
    return _reconcile_settled_execute(
        root, backend, authorization=authorization, research=True
    )


def reconcile_no_allocation_execute(root, backend, *, authorization, evidence_dir=None):
    """Close the retained failed submission for independent Review, without replay."""
    return _reconcile_settled_execute(
        root,
        backend,
        authorization=authorization,
        no_allocation=True,
        evidence_dir=evidence_dir,
    )


def _reconcile_settled_execute(
    root,
    backend,
    *,
    authorization,
    research=False,
    no_allocation=False,
    evidence_dir=None,
):
    action = (
        "reconcile-operator-certified-no-allocation"
        if no_allocation
        else (
            "reconcile-settled-research-result"
            if research
            else "reconcile-settled-formal-timeout"
        )
    )
    if not isinstance(authorization, str) or not authorization.strip():
        raise ProTokError("Explicit timeout reconciliation authorization required")
    root = Path(root)
    study = Study(root / "study")
    with (root / "control/runner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with study._lock():
            state = study.status()
            relative = state["calls"].get("execute")
            if not relative or state.get("accepted_calls", {}).get("execute"):
                raise ProTokError("Timeout has no unaccepted Execute to reconcile")
            previous = study.root / relative
            replay = previous / "reconciliation.json"
            if state["status"] == "ready" and replay.exists():
                saved = read(replay)
                if saved.get("operator_action") != action:
                    raise ProTokError("Execute belongs to another reconciliation")
                if saved.get("prepared_state") != state or any(
                    (root / name).read_text() != value
                    for name, value in saved["protected"].items()
                ):
                    raise ProTokError(
                        "Timeout preparation or protected contract changed"
                    )
                response = completed_response(previous)
                ProTokHarness(backend, root, None, 0).validate_completed(
                    response, workspace=Path(saved["workspace"]), run_dir=previous
                )
                return state
            if (
                state["phase"] != "execute"
                or state["status"] != "needs_attention"
                or state.get("active_attempt") != relative
            ):
                raise ProTokError("Only an interrupted Execute may reconcile a timeout")
            for directory in (previous, previous / "agent"):
                study._check_attempt_stopped(directory)
            iteration = f"{state['iteration']:04d}"
            metadata = read(study.root / "iterations" / iteration / "iteration.json")
            workspace = Path(metadata["workspace"])
            if research:
                from integrations.protok.research_execution import (
                    completed_script,
                    reject_preflight,
                )

                effects = backend.inspect()
                if any(
                    effects.get(key)
                    for key in (
                        "outstanding",
                        "research_outstanding",
                        "debug_outstanding",
                        "long_training_outstanding",
                        "manual_training_outstanding",
                    )
                ):
                    raise ProTokError(
                        "Unresolved effects prevent research reconciliation"
                    )
                source_root = root / "control/research-sources" / iteration
                if not (source_root / "result.json").exists():
                    reject_preflight(
                        root, workspace, iteration, authorization=authorization
                    )

                trusted = completed_script(root, workspace, iteration)
                if trusted["status"] != "failed":
                    raise ProTokError(
                        "Only a settled research failure may be reconciled"
                    )
                record_root = (
                    source_root
                    if trusted.get("failure_kind") == "preflight_rejected"
                    else root / "control/research-jobs" / iteration
                )
                backend_result = (
                    root / "control/research-sources" / iteration / "result.json"
                )
            else:
                trusted = (
                    no_allocation_job(
                        root,
                        backend,
                        workspace,
                        iteration,
                        evidence_dir=evidence_dir,
                        authorization=authorization,
                    )
                    if no_allocation
                    else timed_out_job(root, backend, workspace, iteration)
                )
                record_root = job_path(root, iteration)
                backend_result = record_root / "result.json"
            effects = backend.inspect()
            if any(
                effects.get(key)
                for key in (
                    "outstanding",
                    "research_outstanding",
                    "debug_outstanding",
                    "long_training_outstanding",
                    "manual_training_outstanding",
                )
            ):
                raise ProTokError(
                    "Unresolved effects prevent settled-result reconciliation"
                )
            result_path = workspace / "evaluation-result.json"
            if result_path.exists() and read(result_path) != trusted:
                old = read(result_path)
                if old.get("status") != "error" or any(
                    name in old for name in ("value", "metric", "measurements")
                ):
                    raise ProTokError(
                        "Existing evaluation interface is not the retained failure"
                    )
                immutable_record(record_root / "recovery.previous-interface.json", old)
            write(result_path, trusted)
            response = settled_failure_response(trusted)
            response["markdown"] = (
                "Controller reconciliation of a retained settled result; no new "
                "Execute call or allocation. Original Agent attempt retained.\n\n"
                + response["markdown"]
            )
            ProTokHarness(backend, root, None, 0).validate_completed(
                response, workspace=workspace, run_dir=previous
            )
            directory = previous.parent
            attempt = (
                directory / f"attempt-{len(list(directory.glob('attempt-*'))) + 1:03d}"
            )
            attempt.mkdir()
            if (previous / "context.json").exists():
                shutil.copyfile(previous / "context.json", attempt / "context.json")
            stop = root / "control/STOP"
            prepared = {
                **state,
                "calls": {
                    **state["calls"],
                    "execute": str(attempt.relative_to(study.root)),
                },
                "status": "ready",
                "active_attempt": None,
                "error": None,
            }
            plan = Path("study") / state["plan_ref"]
            if plan.is_absolute() or ".." in plan.parts:
                raise ProTokError("Frozen Plan reference leaves the Study")
            record = {
                "operator_action": action,
                "authorization": authorization,
                "previous_attempt": relative,
                "workspace": str(workspace),
                "backend_result": str(backend_result),
                "source_signatures": signatures(candidate_bytes(workspace)),
                "stop_before": stop.read_text() if stop.exists() else None,
                "call_count": state["call_count"],
                "elapsed_seconds": state["elapsed_seconds"],
                "previous_error": state.get("error"),
                "prepared_state": prepared,
                "protected": {
                    name: (root / name).read_text()
                    for name in (
                        "control/ownership.json",
                        "control/continuation.json",
                        "study/study.json",
                        str(plan),
                    )
                },
            }
            immutable_record(attempt / "reconciliation.json", record)
            publish_completed_response(attempt, response)
            study._save(prepared)
            return prepared


def reconcile_local_incomplete(root, backend):
    """Operator-only acceptance of the retained successful local rejection receipt."""
    root = Path(root)
    study = Study(root / "study")
    with (root / "control/runner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with study._lock():
            state = study.status()
            if (
                state["phase"] != "execute"
                or state["status"] != "needs_attention"
                or "Execute ended without a completed trusted evaluation"
                not in state.get("error", "")
            ):
                raise ProTokError(
                    "Only the known local incomplete wrapper rejection can be reconciled"
                )
            relative = state["calls"].get("execute")
            if not relative or state.get("active_attempt") != relative:
                raise ProTokError("Execute receipt does not match the active attempt")
            attempt = study.root / relative
            if read(attempt / "process.json").get("status") != "succeeded":
                raise ProTokError(
                    "Local reconciliation requires a successful process receipt"
                )
            response = read(attempt / "response.json")
            iteration = f"{state['iteration']:04d}"
            metadata = read(study.root / "iterations" / iteration / "iteration.json")
            diagnostic = retain_local_incomplete(
                root,
                backend,
                Path(metadata["workspace"]),
                iteration,
                response,
                run_dir=attempt,
            )
            record = {
                **diagnostic,
                "process_receipt": str(attempt / "process.json"),
                "response_receipt": str(attempt / "response.json"),
                "call_count": state["call_count"],
                "elapsed_seconds": state["elapsed_seconds"],
                "error": state["error"],
                "operator_action": "accept-successful-incomplete-without-replay",
            }
            immutable_record(
                root
                / "control/reconciliations"
                / f"local-incomplete-{iteration}-{attempt.name}.json",
                record,
            )
            state.setdefault("accepted_calls", {})["execute"] = True
            study._save(state)
        # retry now retains the accepted successful Execute call; it launches nothing.
        return study.retry()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--project", type=Path, default=PROJECT)
    parser.add_argument(
        "--authorization", help="Recorded user authorization for explicit recovery"
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    for mode in (
        "init",
        "run-one",
        "status",
        "reconcile-local-incomplete",
        "reconcile-completed-execute",
        "reconcile-timed-out-execute",
        "reconcile-no-allocation-execute",
        "reconcile-research-execute",
        "repair-completed-control",
        "begin-debug",
        "resume-unsubmitted",
        "enable-long-training",
        "run-long-one",
        "recover-long-run",
    ):
        modes.add_argument("--" + mode, action="store_true")
    parser.add_argument(
        "--no-allocation-evidence",
        type=Path,
        help="Directory containing retained operator no-allocation audit",
    )
    parser.add_argument("--long-run-id", help="Retained long run to reconcile")
    args = parser.parse_args(argv)
    if args.project is None:
        parser.error("--project or PROTOK_PROJECT is required")
    root = args.root.resolve()
    owner = read(root / "control/ownership.json")
    backend = ProTokBackend(args.project, root / "control", owner["owner_token"])
    if args.status:
        result = {
            "study": read(root / "study/state.json")
            if (root / "study/state.json").exists()
            else None,
            "backend": backend.inspect(),
            "paused": (root / "control/STOP").exists(),
        }
    elif args.enable_long_training or args.run_long_one or args.recover_long_run:
        from integrations.protok import long_training_loop

        if args.enable_long_training:
            result = long_training_loop.enable(root, backend)
        elif args.recover_long_run:
            if not args.long_run_id:
                parser.error("--recover-long-run requires --long-run-id")
            result = long_training_loop.recover(root, backend, args.long_run_id)
        else:
            result = long_training_loop.run_one(root, backend)
    elif args.init:
        result = init_study(root, backend)
    elif args.reconcile_local_incomplete:
        result = reconcile_local_incomplete(root, backend)
    elif args.reconcile_completed_execute:
        result = reconcile_completed_execute(root, backend)
    elif args.reconcile_no_allocation_execute:
        result = reconcile_no_allocation_execute(
            root,
            backend,
            authorization=args.authorization or "",
            evidence_dir=args.no_allocation_evidence,
        )
    elif args.reconcile_timed_out_execute:
        result = reconcile_timed_out_execute(
            root, backend, authorization=args.authorization or ""
        )
    elif args.reconcile_research_execute:
        result = reconcile_research_execute(
            root, backend, authorization=args.authorization or ""
        )
    elif args.repair_completed_control:
        result = {
            "repaired": repair_completed_control(root, backend),
            "study": Study(root / "study").status(),
        }
    elif args.begin_debug:
        result = begin_debug(root, backend)
    elif args.resume_unsubmitted:
        from integrations.protok.resume_submission import prepare_resume

        result = prepare_resume(root, backend, authorization=args.authorization or "")
    else:
        result = run_one(root, backend)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
