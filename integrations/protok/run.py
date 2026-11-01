"""One leased ProTok investigation; fixed evaluation runs outside Agent workspaces."""

from __future__ import annotations

import argparse
import fcntl
import os
import hashlib
import json
import sys
import math
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
    publish_completed_response,
)
from research_loop.protocol import validate_response
from integrations.protok.backend import (
    CANDIDATE_FILES,
    METRIC,
    PROTOCOL,
    ProTokBackend,
    ProTokError,
)

PROJECT = Path(
    os.environ.get(
        "PROTOK_PROJECT",
        "/Users/chenhongru/workspace/hkust/protein/autoresearch-se3-v2-20261101",
    )
)
BEST_COMMIT = os.environ.get(
    "PROTOK_BASE_COMMIT", "0defd2127e60ce1ef8baf392f860d24dc6ee3fcf"
)
BEST_FINGERPRINT = os.environ.get(
    "PROTOK_BASE_FINGERPRINT",
    "b5db45ac63db923c9bbbe7fdfce6cc59a04913bb4e3defb800fe8ff34d5f84de",
)
BEST_VALUE = float(os.environ.get("PROTOK_BASE_VALUE", "2.6046197081836633"))
CODEX = os.environ.get(
    "PROTOK_CODEX",
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
)
INTERFACE_FILES = {"evaluation-request.json", "evaluation-result.json"}


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


def check_scope(workspace, baseline):
    changed = set(
        git(workspace, "diff", "--name-only", "-z", baseline, "--").split("\0")
    )
    changed |= set(git(workspace, "ls-files", "--others", "-z").split("\0"))
    extra = changed - set(CANDIDATE_FILES) - INTERFACE_FILES - {""}
    if extra:
        raise ProTokError(
            "Agent changed files outside candidate scope: " + ", ".join(sorted(extra))
        )
    candidate_bytes(workspace)


def freeze_source(project, workspace, destination, baseline):
    check_scope(workspace, baseline)
    contents = candidate_bytes(workspace)
    subprocess.run(
        ["git", "clone", "--shared", "--quiet", str(project), str(destination)],
        check=True,
    )
    git(destination, "checkout", "--detach", baseline)
    for name, value in contents.items():
        (destination / name).write_bytes(value)
    git(destination, "add", "--", *CANDIDATE_FILES)
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
        or not continuation["started_at"]
        <= now
        < min(continuation["deadline"], owner["expires_at"])
    ):
        raise ProTokError(
            "An acknowledged active bounded continuation lease is required"
        )
    status = backend.inspect()
    if require_idle and status["outstanding"]:
        raise ProTokError(
            "Outstanding original record requires explicit operator reconciliation"
        )
    return min(continuation["deadline"], owner["expires_at"])


def local_incomplete(root, backend, workspace, iteration, result):
    """Establish that a local rejection contains no measured or unknown effects."""
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
    for name in INTERFACE_FILES:
        path = Path(workspace) / name
        if path.exists() or path.is_symlink():
            raise ProTokError(
                "Local rejection cannot retain an evaluation interface file"
            )
    baseline = read(Path(root) / "study/study.json")["baseline"]
    check_scope(workspace, baseline)
    if backend.inspect()["outstanding"]:
        raise ProTokError(
            "Outstanding backend effects cannot be accepted as a local rejection"
        )
    return {
        "iteration": iteration,
        "source_signatures": signatures(candidate_bytes(workspace)),
        "response": result,
        "provenance": "local-preflight-rejection-no-evaluation",
    }


def immutable_record(path, record):
    if path.exists():
        if read(path) != record:
            raise ProTokError("An existing operator evidence record cannot be replaced")
    else:
        write(path, record)


def completed_job(root, backend, workspace, iteration):
    """Repair only publication of a frozen, fully settled backend measurement."""
    root, workspace = Path(root), Path(workspace)
    job = root / "control/jobs" / iteration
    record = read(job / "job.json")
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
        check_scope(candidate, baseline)
        if signatures(candidate_bytes(candidate)) != record["source_signatures"]:
            raise ProTokError("Retained candidate no longer matches frozen job source")
    if git(source, "rev-parse", "HEAD") != record["source_commit"]:
        raise ProTokError("Frozen job source commit changed")
    receipt = backend.completed_receipt(
        source, job / "runtime-config.json", record["fingerprint"]
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


class ProTokHarness:
    def __init__(self, backend, root, delegate, deadline):
        self.backend, self.root, self.delegate, self.deadline = (
            backend,
            Path(root),
            delegate,
            deadline,
        )
        self.errors = []
        self.trusted_result = None

    def evaluate_request(self, workspace, iteration, request, stop, deadline):
        if request not in ({"action": "import"}, {"action": "evaluate"}):
            raise ProTokError("Request must contain exactly action=import or evaluate")
        if (iteration == "0001") != (request["action"] == "import"):
            raise ProTokError(
                "Only the first iteration imports; subsequent iterations evaluate"
            )
        if stop.is_set() or time.time() >= deadline:
            raise ProTokError("Execute ended; no evaluation may launch")
        admission(self.root, self.backend)
        baseline = read(self.root / "study/study.json")["baseline"]
        check_scope(workspace, baseline)
        before = signatures(candidate_bytes(workspace))
        job = self.root / "control/jobs" / iteration
        if job.exists():
            saved = read(job / "job.json")
            if saved["request"] != request or saved["source_signatures"] != before:
                raise ProTokError("Existing job cannot be replaced or rewound")
            if saved["status"] != "completed" and not (job / "result.json").exists():
                raise ProTokError(
                    "Existing unfinished job has unknown effects; explicit recovery required"
                )
            return completed_job(self.root, self.backend, workspace, iteration)
        job.mkdir(parents=True)
        record = {
            "status": "preparing",
            "request": request,
            "source_signatures": before,
            "iteration": iteration,
            "created_at": time.time(),
        }
        write(job / "job.json", record)
        commit, frozen = freeze_source(
            self.backend.project, workspace, job / "source", baseline
        )
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
        check_scope(workspace, baseline)
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
        request_path = workspace / "evaluation-request.json"
        while not stop.wait(0.1):
            if not request_path.exists():
                continue
            try:
                if request_path.is_symlink() or request_path.stat().st_size > 1024:
                    raise ProTokError("Request must be a small regular JSON file")
                request = read(request_path)
                request_path.unlink()
                result = self.evaluate_request(
                    workspace, iteration, request, stop, deadline
                )
                self.trusted_result = result
            except Exception as error:
                result = {
                    "status": "error",
                    "error": f"{type(error).__name__}: {error}",
                    "retry": False,
                }
                self.errors.append(result["error"])
            write(workspace / "evaluation-result.json", result)
            return

    def run(self, **kwargs):
        remaining = int(self.deadline - time.time())
        if remaining <= 0 or (self.root / "control/STOP").exists():
            raise HarnessError("Continuation deadline reached or control/STOP set")
        kwargs["timeout_seconds"] = min(kwargs["timeout_seconds"], remaining, 3600)
        environment = self.root / "control/execution-environment.json"
        if environment.exists():
            kwargs["prompt"] += (
                "\n\nOperator-supplied execution environment from trusted control "
                "(factual capabilities; existing scope, frozen contract and protected "
                "evaluation checks remain unchanged):\n"
                + environment.read_text(encoding="utf-8")
            )
        if not kwargs["writable"]:
            return self.delegate.run(**kwargs)
        self.trusted_result = None
        self.errors = []
        workspace = Path(kwargs["workspace"])
        outer = Path(kwargs["run_dir"]) if "run_dir" in kwargs else None
        if outer is not None:
            agent = outer / "agent"
            agent.mkdir()
            if (outer / "context.json").exists():
                shutil.copyfile(outer / "context.json", agent / "context.json")
            kwargs["run_dir"] = agent
        # Prior interface files cannot trigger a new allocation or supply feedback.
        result_path = workspace / "evaluation-result.json"
        request_path = workspace / "evaluation-request.json"
        for path in (request_path, result_path):
            if path.is_symlink():
                raise ProTokError("Evaluation interface cannot be a symlink")
            path.unlink(missing_ok=True)
        stop = threading.Event()
        deadline = min(self.deadline, time.time() + kwargs["timeout_seconds"] - 5)
        iteration = workspace.parent.name
        thread = threading.Thread(
            target=self._watch, args=(workspace, iteration, stop, deadline)
        )
        thread.start()
        try:
            result = self.delegate.run(**kwargs)
        finally:
            stop.set()
            # evaluate has the same finite deadline; settle effects before returning.
            thread.join()
        if self.errors:
            raise HarnessError(self.errors[-1])
        if result.get("outcome") == "incomplete" and self.trusted_result is None:
            diagnostic = local_incomplete(
                self.root, self.backend, workspace, iteration, result
            )
            immutable_record(
                self.root / "control/diagnostics" / (iteration + ".json"), diagnostic
            )
            if outer is not None:
                publish_completed_response(outer, result)
            return result
        if request_path.exists():
            raise HarnessError("A duplicate evaluation request was left after Execute")
        if not result_path.exists() or read(result_path).get("status") != "completed":
            raise HarnessError("Execute ended without a completed trusted evaluation")
        trusted = self.trusted_result
        if trusted is None or read(result_path) != trusted:
            raise HarnessError("Agent altered the trusted evaluation interface result")
        result = self.validate_completed(result, workspace=workspace, run_dir=outer)
        if outer is not None:
            publish_completed_response(outer, result)
        return result

    def validate_completed(self, result, *, workspace, run_dir):
        """Validate retained process output before accepting an Execute receipt."""
        validate_response("execute", result)
        workspace = Path(workspace)
        iteration = workspace.parent.name
        if result["outcome"] == "incomplete":
            diagnostic = local_incomplete(
                self.root, self.backend, workspace, iteration, result
            )
            immutable_record(
                self.root / "control/diagnostics" / (iteration + ".json"), diagnostic
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


GOAL = f'''Minimize {METRIC} (Angstrom) under {PROTOCOL}; this is adaptive development evidence.
Fixed: 449 proteins, seed42, 2000 updates, Euler100, H100 debug 30min, 8CPU/96G/2workers.
Prefer improvements without increasing parameter count or inference work; scaling is not the primary route; retain the already-kept D4 best. Never change teacher/data/noise/scorer/fixed config,
canonical project, old journal, permissions, hardware, or protocol. No SSH/network/install,
subagents, direct training or other launch paths. Read only relevant source and evidence.
First iteration MUST audit/import historical best {BEST_FINGERPRINT}, {BEST_VALUE} A, without
changing any of the three candidate files or launching GPU work. base_experiment=null;
old run IDs are external evidence, never Study graph references. Subsequent proposals MUST
base on the current Global Frontier best experiment; explain mathematical invariants, masks,
and rationale in Plan before editing. A mechanism hypothesis is optional. Only editable files:
{", ".join(CANDIDATE_FILES)}. No additional code, report or scratch files.
During Execute atomically write evaluation-request.json containing exactly {{"action":"import"}}
(first iteration) or {{"action":"evaluate"}} (later), then wait for evaluation-result.json.
Use a bounded polling loop up to 3600s or the remaining window; timeout_seconds in Plan should
allow this, at most 3600 seconds. Never resubmit. The fixed bridge freezes and verifies source and returns actual
receipt, fingerprint, metric/value, evidence and provenance. Do not modify the two interface
JSON files except publishing the request. Return exactly its artifact and one measurement:
candidate_id=fingerprint, evaluation_id={PROTOCOL}, comparison_scope={PROTOCOL},
metrics=[{{"name":"{METRIC}","value":actual value}}], evidence=all returned evidence paths.
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
For an ordinary-case local candidate precheck failure before any evaluation request, return incomplete
with artifacts=[] and measurements=[] and report the actual failure and uncertainty.
This is an unmeasured candidate rejection for independent Review, not an infrastructure
failure or a fabricated score. Review uses candidate_reviews=[] for no measurements,
retains the existing measured best, records what failed and what remains unattributed,
and should continue with another route while the window remains. Transport
errors and unknown actual evaluation effects require pause and reconciliation.
Independent Review must inspect actual source and receipt, verify measurement, preserve
limitations, update both route/global research, and continue. A worse candidate is useful
evidence, not a reason to stop. Continue until deadline or real execution failure.
'''


def init_study(root, backend):
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
    initialize(
        Path(root) / "study",
        goal=GOAL,
        workspace=baseline,
        allow_write=True,
        harness=config,
        max_iterations=None,
        max_calls=None,
        max_seconds=max(1, int(deadline - time.time())),
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
    deadline = admission(root, backend)
    state = study.status()
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
        (Path(root) / "control/STOP").write_text(
            "Paused after real execution failure; reconcile effects before resuming.\n"
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
        try:
            state = _run_one(root, backend)
            record.update(
                status="completed" if state["status"] == "ready" else "stopped",
                iteration=state["iteration"],
            )
            return state
        except Exception as error:
            record.update(status="paused", error=f"{type(error).__name__}: {error}")
            write(control / "status.json", record)
            (control / "STOP").write_text(
                "Paused; reconcile retained effects before resuming.\n"
            )
            raise
        finally:
            record["ended_at"] = time.time()
            write(control / "runner.json", record)


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
            diagnostic = local_incomplete(
                root, backend, Path(metadata["workspace"]), iteration, response
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
    modes = parser.add_mutually_exclusive_group(required=True)
    for mode in (
        "init",
        "run-one",
        "status",
        "reconcile-local-incomplete",
        "reconcile-completed-execute",
        "repair-completed-control",
    ):
        modes.add_argument("--" + mode, action="store_true")
    args = parser.parse_args(argv)
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
    elif args.init:
        result = init_study(root, backend)
    elif args.reconcile_local_incomplete:
        result = reconcile_local_incomplete(root, backend)
    elif args.reconcile_completed_execute:
        result = reconcile_completed_execute(root, backend)
    elif args.repair_completed_control:
        result = {
            "repaired": repair_completed_control(root, backend),
            "study": Study(root / "study").status(),
        }
    else:
        result = run_one(root, backend)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
