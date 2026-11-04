"""Bounded real Study campaign measuring fixed from-zero GPU/dashboard startup.

Only instruction Markdown is editable. The trusted controller, scorer, Python
runtime, original model/loss, Review and three-case success constraints stay fixed.
This file prepares and runs the Study only when its CLI is explicitly invoked.
"""

# ruff: noqa: E402

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import threading
import time

FIXED_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(FIXED_ROOT), str(FIXED_ROOT / "src")]

from benchmarks.cactus.bridge import _publish, publish_execution, report_arguments
from research_loop import Study, initialize, submission
from research_loop.core import BudgetExhausted
from research_loop.harness import CodexHarness, HarnessConfig, HarnessError
from research_loop.tasks import TaskQueue


EDITABLE = ("SKILL.md", "references/operations.md")
SCOPE = "startup-e2e-v3"
CASE_SECONDS = 420
REPEATS = 3
EXECUTE_SECONDS = 1500
PLAN_SECONDS = 240
REVIEW_SECONDS = 420
ROUND_SECONDS = EXECUTE_SECONDS + PLAN_SECONDS + REVIEW_SECONDS + 30
MAX_BUDGET_SECONDS = 36000
MAX_ITERATIONS = 60
ARTIFACT = "startup-evaluation.json"
HOST_FILES = submission.INTERFACE_FILES | {ARTIFACT}
EVALUATION = {
    "id": SCOPE,
    "comparison_scope": SCOPE,
    "protocol": (
        "Three serial fresh Terra/medium cases per candidate; actual fresh remote "
        "backend setup, real CUDA smoke, full independent Review and existing "
        "dashboard scalar verification. Elapsed launch-to-display_ready_seconds; "
        "failed cases cost 420 seconds. Keep all cases. Fixed scorer and Python."
    ),
    "data_role": "development",
    "allow_feedback": True,
    "objectives": [
        {"name": "mean_startup_seconds", "direction": "minimize", "unit": "seconds"}
    ],
    "constraints": [
        {"name": "success_rate", "operator": "ge", "value": 1, "unit": "fraction"},
        {
            "name": "scalar_visibility_rate",
            "operator": "ge",
            "value": 1,
            "unit": "fraction",
        },
    ],
}
GOAL = """Research the fastest reliable from-zero startup of the fixed ProTok synthetic
GPU smoke through the existing research-loop interfaces. This is a bounded campaign
of dynamically queued investigations, not a two-investigation completion goal.
Continue instruction-only optimization and scheduled baseline controls until the
controller's wall-clock admission budget or iteration cap prevents another round.
An improvement, completed baseline or finished candidate alone never meets this
continuing goal. Prior completed Studies remain complete; this is a new linked
Study with a new explicit authorization, not a resumption of their old goals.

The controller runs three serial fresh Terra/medium cases for each submission.
Each case includes fresh remote environment/source setup, a real Study with full
Plan/Execute/Review, one unchanged synthetic B2/L16/K4 seed43 three-update CUDA job,
and final training/diagnostic/performance scalars in the existing dashboard.
Outer research roles use Sol/medium. Model CLIs run locally; GPU/backend artifacts
run on the configured AutoDL backend through the fixed evaluator, never your shell.
The campaign limits and prior evidence are specified below. Do not launch model/GPU/SSH/network commands,
install dependencies, or modify the scorer, backend, dashboard, tests or Python.
Allowed candidate edits are ONLY SKILL.md and references/operations.md. Preserve
model/loss/inputs/metrics, permission and unknown-effect gates, validation and full
independent Review. Optimize concrete instruction/context/setup-sequence overhead;
do not gain speed by omitting required work or weakening success requirements.

During Execute inspect the actual source and previous trusted evidence. Submit
exactly once through the existing interface:
python3 src/research_loop/submission.py submit --workspace . --action evaluate
Poll status with the same script's status --workspace . command until terminal.
The host owns the scorer and publishes evaluation-result.json and
startup-evaluation.json; accepted is never permission to resubmit. Evaluation can
take about 23 minutes; Plan must grant Execute 1500 seconds within Study budget.
Use short polling waits. Do not create alternate snapshots, graders or launchers.
Final Execute response is only schema-conforming markdown; the host constructs
the artifact and measurements. Report all three cases, failures, actual timings,
calls, visible scalar evidence, and limitations without inventing evidence.

The global development objective is mean_startup_seconds (lower), with
success_rate>=1 and scalar_visibility_rate>=1. Every failed case contributes420s;
missing or failed cases never become zero or disappear. Unresolved effects stop
the entire Study for operator reconciliation, without retries. Synthetic bbRMSD
is a denoising diagnostic, not formal scientific model performance.
Review must inspect the actual receipt and give explicit candidate valid/verified
judgments. Every evaluated candidate requires the full independent Review before
another task is admitted. While another round fits, Review should continue with
goal_status not_met or undetermined. Stop for unsafe or unresolved effects, never
just because a candidate improved. Historical research claiming its old goal was
met does not complete this campaign. Compare all failed candidates and controls;
timing changes across the campaign may reflect changing conditions.
These three development cases do not establish general speed improvement.
"""


def git(root, *words, binary=False):
    result = subprocess.run(
        ["git", "-C", str(root), *words],
        capture_output=True,
        check=True,
        text=not binary,
        timeout=30,
    )
    return result.stdout if binary else result.stdout.strip()


def archive(source, revision, destination):
    destination.mkdir(parents=True, exist_ok=False)
    data = git(source, "archive", "--format=tar", revision, binary=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as bundle:
        bundle.extractall(destination, filter="data")


def changed_files(workspace, baseline):
    return set(
        filter(
            None,
            git(workspace, "diff", "--name-only", "-z", baseline, "--").split("\0"),
        )
    )


def freeze_candidate(
    source,
    revision,
    workspace,
    baseline,
    destination,
    *,
    unchanged,
    change_base=None,
    require_change=True,
):
    changed = changed_files(workspace, baseline) - HOST_FILES
    allowed = set() if unchanged else set(EDITABLE)
    if changed - allowed:
        raise ValueError(
            f"Candidate changed protected tracked files: {sorted(changed - allowed)}"
        )
    if (
        not unchanged
        and require_change
        and not (changed_files(workspace, change_base or baseline) - HOST_FILES)
    ):
        raise ValueError("Optimization must make one instruction-only source change")
    if (
        not unchanged
        and not require_change
        and (changed_files(workspace, change_base or baseline) - HOST_FILES)
    ):
        raise ValueError(
            "Prior-best reproduction must preserve its starting instructions"
        )
    for name in git(
        workspace, "ls-files", "--others", "--exclude-standard", "-z"
    ).split("\0"):
        if (
            name
            and name not in submission.INTERFACE_FILES | {ARTIFACT}
            and not name.startswith(".tmp/")
        ):
            raise ValueError(f"Unauthorized untracked candidate file: {name}")
    archive(source, revision, destination)
    identity = hashlib.sha256(revision.encode())
    for name in EDITABLE:
        path = workspace / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 131072:
            raise ValueError(
                f"Editable instruction must be a regular file <=128KiB: {name}"
            )
        content = path.read_bytes()
        identity.update(name.encode() + b"\0" + content + b"\0")
        (destination / name).write_bytes(content)
    return {
        "id": "sha256:" + identity.hexdigest(),
        "baseline_revision": revision,
        "runtime_snapshot": str(destination),
        "changed_files": sorted(changed),
        "editable_scope": list(EDITABLE),
        "python_tests": "not repeated for instruction-only candidates; fixed baseline checks owned by operator",
    }


def reviewed_iteration(study_root, identity):
    """Use the canonical publication boundary, including explicit Review judgments."""
    folder = study_root / "iterations" / identity
    metadata = json.loads((folder / "iteration.json").read_text())
    publication = json.loads((folder / "publication.json").read_text())
    state = json.loads((study_root / "state.json").read_text())
    if (
        metadata.get("status") != "reviewed"
        or publication.get("metadata") != metadata
        or not (
            int(identity) < state["iteration"]
            or (int(identity) == state["iteration"] and state["phase"] == "done")
        )
    ):
        raise ValueError(f"Iteration {identity} has no complete published Review")
    review_ref = publication["state_after"]["review_ref"]
    review = json.loads((study_root / review_ref).read_text())
    return metadata, review


def eligible(receipt, review, identity):
    metrics = receipt.get("metrics", {})
    mean = metrics.get("mean_startup_seconds")
    return (
        receipt.get("status") == "completed"
        and receipt.get("comparison_scope") == SCOPE
        and metrics.get("success_rate") == 1
        and metrics.get("scalar_visibility_rate") == 1
        and type(mean) in (int, float)
        and math.isfinite(mean)
        and 0 < mean <= CASE_SECONDS
        and len(receipt.get("cases", [])) == REPEATS
        and any(
            row.get("candidate_id") == f"startup-{identity}"
            and row.get("valid") is True
            and row.get("verified") is True
            for row in review.get("candidate_reviews", [])
        )
    )


def prior_evidence(root, revision):
    """Link published prior evidence; import only its best reviewed instructions."""
    summary = json.loads((root / "summary.json").read_text())
    if (
        summary.get("status") != "completed"
        or summary.get("comparison_scope") != SCOPE
        or summary.get("source_revision") != revision
        or summary.get("unresolved_errors")
    ):
        raise ValueError("Prior Study must be completed with the same frozen baseline")
    rows = []
    for number, receipt in summary.get("results", {}).items():
        identity = f"{int(number):04d}"
        metadata, review = reviewed_iteration(root / "study", identity)
        if receipt["candidate"]["baseline_revision"] != revision:
            raise ValueError("Prior candidate has a different frozen baseline")
        artifact = Path(metadata["workspace"]) / ARTIFACT
        if json.loads(artifact.read_text()) != receipt:
            raise ValueError(
                "Prior summary differs from its published candidate receipt"
            )
        rows.append(
            {
                "iteration": identity,
                "metrics": receipt["metrics"],
                "eligible": eligible(receipt, review, identity),
                "candidate": receipt["candidate"],
                "receipt": str(artifact),
                "research": str(
                    root / "study/iterations" / identity / "route.after.md"
                ),
                "review": str(
                    root
                    / "study"
                    / json.loads(
                        (
                            root / "study/iterations" / identity / "publication.json"
                        ).read_text()
                    )["state_after"]["review_ref"]
                ),
            }
        )
    frontier = json.loads((root / "study/frontiers.json").read_text())
    current_ids = set(frontier["global"].get(SCOPE, {}).get("candidate_ids", []))
    choices = [
        row
        for row in rows
        if row["eligible"]
        and f"{row['iteration']}:startup-{row['iteration']}" in current_ids
    ]
    if not choices:
        raise ValueError("Prior Study has no valid, verified eligible candidate")
    best = min(choices, key=lambda row: row["metrics"]["mean_startup_seconds"])
    return {
        "study": str(root / "study"),
        "summary": str(root / "summary.json"),
        "research": str(root / "study/research.md"),
        "source_revision": revision,
        "results": rows,
        "best": best,
        "link_type": "new_follow_on_study; prior state and goal remain unchanged",
    }


def commit_workspace(workspace, message):
    git(workspace, "add", *EDITABLE)
    git(
        workspace,
        "-c",
        "user.name=Startup Research",
        "-c",
        "user.email=benchmark@example.invalid",
        "commit",
        "--allow-empty",
        "-qm",
        message,
    )


def round_task(number, *, baseline, prior, best):
    control = (not prior and number == 1) or number % baseline["every"] == 0
    reproduction = bool(prior) and number == 1
    if reproduction:
        instruction = (
            "Reproduce the prior best reviewed candidate, already seeded in this "
            "task's source. Make no tracked source changes. Read its linked receipt "
            "and research rather than rediscovering the previous optimization."
        )
    elif control:
        instruction = (
            "Measure an original-baseline control. Restore ONLY SKILL.md and "
            "references/operations.md to the original frozen workspace commit "
            f"{baseline['revision']} (git restore --source REV -- those paths). "
            "Do not optimize the control. Its trusted receipt must have no source "
            "changes relative to that original baseline."
        )
    else:
        instruction = (
            "Read the prior evidence link in the goal, published receipts and current "
            "research first; inspect the selected starting instructions. Make one "
            "concrete new instruction-only startup optimization relative to this "
            "task's selected starting revision in SKILL.md and/or "
            "references/operations.md. Preserve useful previous optimizations; do "
            "not rediscover them from scratch."
        )
    return {
        "id": f"round-{number:04d}",
        "title": "prior-best-reproduction"
        if reproduction
        else "baseline-control"
        if control
        else "optimize",
        "instruction": instruction
        + " Submit once for the same three-case protocol and complete full Review. "
        "Python, scorer, model/loss, scalar verification and every success "
        "requirement are fixed. This round alone does not finish the campaign.",
        "priority": number,
        "allow_changes": not reproduction,
        "route_id": "new",
        "base_experiment": None if control else best,
    }, control


class StartupResearchHarness:
    def __init__(self, args, config, baseline, *, deadline):
        self.args, self.config, self.baseline = args, config, baseline
        self.deadline = deadline
        self.delegate = CodexHarness(config)
        self.results = {}
        self.errors = []
        self.claimed = set()
        self.started_workspaces = set()
        self.case_count = 0
        self.rounds = {}
        self.admissions = {}
        self.stage_deadlines = {}

    def admit_iteration(self, identity):
        if identity in self.admissions:
            return self.admissions[identity]
        if int(identity) > self.args.max_iterations:
            raise BudgetExhausted("iteration_budget")
        remaining = self.deadline - time.monotonic()
        if remaining < ROUND_SECONDS:
            raise BudgetExhausted("campaign_budget")
        token = {"iteration": identity, "remaining_seconds": remaining}
        self.admissions[identity] = token
        return token

    def call_allowance(self, remaining):
        return min(remaining, max(0, self.deadline - time.monotonic()))

    def evaluate(self, workspace, iteration, stop, deadline):
        if (
            iteration not in self.rounds
            or self.case_count + REPEATS > self.args.max_iterations * REPEATS
        ):
            raise HarnessError("Authorized campaign iteration/case budget exceeded")
        deadline = min(deadline, self.deadline)
        if (
            stop.is_set()
            or time.monotonic() + REPEATS * (CASE_SECONDS + 15) + 7 >= deadline
        ):
            raise HarnessError("Insufficient Execute budget for all three fixed cases")
        current = self.rounds[iteration]
        controller = self.args.root / "controller" / f"{iteration:04d}"
        candidate = freeze_candidate(
            self.args.source,
            self.args.revision,
            workspace,
            self.baseline,
            controller / "runtime",
            unchanged=current["control"],
            change_base=current["source_revision"],
            require_change=current["kind"] == "optimize",
        )
        rows = []
        for number in range(1, REPEATS + 1):
            if stop.is_set() or time.monotonic() + CASE_SECONDS + 15 >= deadline:
                raise HarnessError(
                    "Insufficient Execute budget; no additional case launched"
                )
            case_root = controller / f"case-{number:02d}"
            command = [
                sys.executable,
                str(Path(__file__).with_name("evaluate.py")),
                "--root",
                str(case_root),
                "--runtime-root",
                candidate["runtime_snapshot"],
                "--prepared-source",
                str(self.args.prepared_source),
                "--host",
                self.args.host,
                "--remote-root",
                self.args.remote_root,
                "--remote-python",
                self.args.remote_python,
                "--codex-wrapper",
                str(self.args.codex_wrapper),
                "--gpu",
                self.args.gpu,
                "--source-revision",
                candidate["id"],
                "--timeout-seconds",
                str(CASE_SECONDS),
            ]
            self.case_count += 1
            with (
                (controller / f"case-{number:02d}.stdout").open("w") as stdout,
                (controller / f"case-{number:02d}.stderr").open("w") as stderr,
            ):
                process = subprocess.Popen(
                    command, stdout=stdout, stderr=stderr, start_new_session=True
                )
                try:
                    process.wait(timeout=CASE_SECONDS + 15)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    os.killpg(process.pid, signal.SIGINT)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    raise HarnessError(
                        "Evaluator effects unresolved after interruption; stop and reconcile, never retry"
                    ) from None
            result_path = case_root / "benchmark-result.json"
            if not result_path.is_file():
                raise HarnessError(
                    f"Evaluator has no terminal receipt: {case_root}; reconcile before retry"
                )
            result = json.loads(result_path.read_text())
            execution_path, setup_path = (
                case_root / "backend/execution.json",
                case_root / "backend/setup.json",
            )
            retained = [
                json.loads(path.read_text())
                for path in (execution_path, setup_path)
                if path.exists()
            ]
            if result.get("host_worker_unresolved") or any(
                item.get("status") == "accepted" for item in retained
            ):
                raise HarnessError(
                    f"Retained backend effects unresolved: {case_root}; no further cases"
                )
            outer = result.get("outer", {})
            display = result.get("display_verification", {}).get("ok") is True
            elapsed = result.get("display_ready_seconds")
            success = (
                process.returncode == 0
                and outer.get("exit_code") == 0
                and outer.get("timed_out") is False
                and outer.get("launch_error") is None
                and display
                and type(elapsed) in (int, float)
                and math.isfinite(elapsed)
                and 0 < elapsed <= CASE_SECONDS
                and result.get("host_worker_unresolved") is False
            )
            rows.append(
                {
                    "case": number,
                    "success": success,
                    "scalar_visible": display,
                    "startup_seconds": elapsed if success else CASE_SECONDS,
                    "observed_display_ready_seconds": elapsed,
                    "receipt": str(result_path),
                    "exit_code": process.returncode,
                    "inner_calls": result.get("inner_state", {}).get("call_count"),
                    "outer_tool_commands": outer.get("tool_command_count"),
                    "outer_token_usage": outer.get("token_usage"),
                    "error": result.get("error"),
                }
            )
            _publish(controller / "cases.json", {"candidate": candidate, "cases": rows})
        metrics = {
            "mean_startup_seconds": sum(row["startup_seconds"] for row in rows)
            / REPEATS,
            "success_rate": sum(row["success"] for row in rows) / REPEATS,
            "scalar_visibility_rate": sum(row["scalar_visible"] for row in rows)
            / REPEATS,
        }
        receipt = {
            "status": "completed",
            "candidate": candidate,
            "cases": rows,
            "metrics": metrics,
            "comparison_scope": SCOPE,
            "baseline_control": current["control"],
            "base_experiment": current["base_experiment"],
            "limitations": [
                "instruction-only optimization; fixed executable code",
                "three development contexts per candidate; timing noise unestimated",
                "synthetic denoising diagnostic, not scientific model performance",
            ],
        }
        _publish(controller / "receipt.json", receipt)
        return receipt

    def run(self, **kwargs):
        if self.errors:
            raise HarnessError(
                "Research has unresolved effects; operator reconciliation required"
            )
        phase = Path(kwargs["run_dir"]).parent.name
        iteration = int(
            json.loads((self.args.root / "study/state.json").read_text())["iteration"]
        )
        stage_key = (iteration, phase)
        allowance = {
            "plan": PLAN_SECONDS,
            "execute": EXECUTE_SECONDS,
            "review": REVIEW_SECONDS,
        }.get(phase, PLAN_SECONDS)
        deadline = self.stage_deadlines.setdefault(
            stage_key, min(self.deadline, time.monotonic() + allowance)
        )
        remaining = min(
            kwargs.get("timeout_seconds", allowance), deadline - time.monotonic()
        )
        if remaining < 1:
            raise HarnessError(f"Campaign {phase} deadline exhausted; no call launched")
        kwargs["timeout_seconds"] = int(remaining)
        kwargs["prompt"] += (
            f"\n\nCampaign round {iteration}/{self.args.max_iterations}; "
            f"overall wall time remaining {max(0, self.deadline - time.monotonic()):.0f}s. "
            "This is a new continuing campaign; old completed goals do not apply. "
            "Plan must allocate 1500 seconds to Execute. Full independent Review "
            "is required for every measured round. Continue while another round "
            "fits; stop for unsafe or unresolved effects."
        )
        if not kwargs.get("writable", False):
            # Keep full independent judgments, but a single improvement cannot
            # truthfully mark a still-admissible continuing campaign goal met.
            if (
                phase == "review"
                and iteration < self.args.max_iterations
                and self.deadline - time.monotonic() >= ROUND_SECONDS
            ):
                schema = json.loads(json.dumps(kwargs["schema"]))
                schema["properties"]["goal_status"] = {
                    "type": "string",
                    "enum": ["not_met", "undetermined"],
                }
                kwargs["schema"] = schema
                kwargs["prompt"] += (
                    " Another complete round still fits; the continuing campaign "
                    "goal is not met. Use decision continue unless an actual "
                    "safety or unresolved-effect concern requires stopping."
                )
            return self.delegate.run(**kwargs)
        workspace = Path(kwargs["workspace"]).resolve()
        expected_workspace = (
            self.args.root / "study/iterations" / f"{iteration:04d}" / "workspace"
        )
        if workspace != expected_workspace:
            raise HarnessError("Execute must use its new canonical iteration workspace")
        key = str(workspace)
        if key in self.started_workspaces:
            raise HarnessError(
                "Execute already claimed; inspect retained result without replay"
            )
        self.started_workspaces.add(key)
        # A canonical base_experiment checkpoint includes its terminal host
        # artifact and submission files. This new, never-executed worktree needs
        # its own submission lifetime; preserve the original workspace evidence.
        for name in HOST_FILES:
            path = workspace / name
            if path.exists() or path.is_symlink():
                if path.is_symlink() or not path.is_file():
                    raise HarnessError(
                        f"Invalid inherited host coordination file: {name}"
                    )
                path.unlink()
        stop = threading.Event()
        errors = []

        def service():
            try:
                request = workspace / submission.REQUEST
                while time.monotonic() < deadline:
                    if request.exists() or request.is_symlink():
                        self.claimed.add(key)

                        def preflight(value):
                            if value != {"action": "evaluate"}:
                                raise ValueError(
                                    "Only the fixed evaluate action is authorized"
                                )

                        if (
                            submission.claim_request(workspace, preflight=preflight)
                            is None
                        ):
                            raise HarnessError(
                                "Submission already consumed; never replay"
                            )
                        receipt = self.evaluate(workspace, iteration, stop, deadline)
                        self.results[iteration] = receipt
                        _publish(workspace / ARTIFACT, receipt)
                        _publish(workspace / submission.RESULT, receipt)
                        return
                    if stop.wait(0.2):
                        return
            except BaseException as error:
                errors.append(f"{type(error).__name__}: {error}")
                _publish(
                    workspace / submission.RESULT,
                    {"status": "error", "error": errors[-1], "retry": False},
                )

        worker = threading.Thread(
            target=service, name="trusted-startup-research-evaluator"
        )
        worker.start()
        try:
            report = self.delegate.run(**report_arguments(kwargs))
        finally:
            stop.set()
            worker.join(timeout=max(0, deadline - time.monotonic()) + 7)
            self.errors.extend(errors)
            if worker.is_alive():
                self.errors.append("Evaluation worker unresolved; stop and reconcile")
        if self.errors:
            raise HarnessError("; ".join(self.errors))
        receipt = self.results.get(iteration)
        if receipt is None:
            raise HarnessError(
                "Execute did not complete its canonical evaluation submission"
            )
        _publish(workspace / ARTIFACT, receipt)
        _publish(workspace / submission.RESULT, receipt)
        artifact = {
            "path": str(workspace / ARTIFACT),
            "version": receipt["candidate"]["id"],
        }
        measurement = {
            "candidate_id": f"startup-{iteration:04d}",
            "artifact": artifact,
            "evaluation_id": SCOPE,
            "comparison_scope": SCOPE,
            "metrics": [
                {"name": name, "value": value}
                for name, value in receipt["metrics"].items()
            ],
            "evidence": [row["receipt"] for row in receipt["cases"]],
        }
        return publish_execution(
            kwargs["run_dir"], report, [artifact], [measurement], True
        )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "source", "prepared-source", "codex-wrapper"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("host", "remote-root", "remote-python"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--gpu", default="0")
    parser.add_argument("--prior-study", type=Path)
    parser.add_argument("--budget-seconds", type=int, default=3600)
    parser.add_argument("--max-iterations", type=int, default=2)
    parser.add_argument("--baseline-every", type=int, default=5)
    args = parser.parse_args(argv)
    for name in ("root", "source", "prepared_source", "codex_wrapper"):
        setattr(args, name, getattr(args, name).resolve())
    if not args.gpu.isdecimal() or args.host.startswith("-"):
        parser.error("Invalid GPU or SSH host")
    if not 1 <= args.budget_seconds <= MAX_BUDGET_SECONDS:
        parser.error(f"--budget-seconds must be in 1..{MAX_BUDGET_SECONDS}")
    if not 1 <= args.max_iterations <= MAX_ITERATIONS:
        parser.error(f"--max-iterations must be in 1..{MAX_ITERATIONS}")
    if args.baseline_every < 2:
        parser.error("--baseline-every must be at least 2")
    if args.prior_study:
        args.prior_study = args.prior_study.resolve()
    return args


def main(argv=None):
    args = parse_args(argv)
    started = time.monotonic()
    deadline = started + args.budget_seconds
    args.root.mkdir(mode=0o700, parents=True, exist_ok=False)
    summary = {
        "status": "error",
        "comparison_scope": SCOPE,
        "max_gpu_jobs": args.max_iterations * REPEATS,
        "budget_seconds": args.budget_seconds,
        "max_iterations": args.max_iterations,
        "baseline_every": args.baseline_every,
        "minimum_round_seconds": ROUND_SECONDS,
        "prior_study": str(args.prior_study) if args.prior_study else None,
    }
    harness = None
    try:
        if git(args.source, "status", "--porcelain"):
            raise ValueError("Source baseline must be a clean committed repository")
        args.revision = git(args.source, "rev-parse", "HEAD")
        workspace = args.root / "workspace"
        archive(args.source, args.revision, workspace)
        git(workspace, "init", "-q")
        git(workspace, "add", ".")
        git(
            workspace,
            "-c",
            "user.name=Startup Research",
            "-c",
            "user.email=benchmark@example.invalid",
            "commit",
            "-qm",
            "Frozen startup research baseline",
        )
        baseline = git(workspace, "rev-parse", "HEAD")
        prior = (
            prior_evidence(args.prior_study, args.revision)
            if args.prior_study
            else None
        )
        if prior:
            snapshot = Path(prior["best"]["candidate"]["runtime_snapshot"])
            for name in EDITABLE:
                path = snapshot / name
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.stat().st_size > 131072
                ):
                    raise ValueError(f"Invalid prior instruction snapshot: {name}")
                (workspace / name).write_bytes(path.read_bytes())
            commit_workspace(workspace, "Seed prior best reviewed startup instructions")
            _publish(args.root / "prior-evidence.json", prior)
        goal = GOAL + (
            f"\nCampaign authorization: at most {args.budget_seconds} wall seconds, "
            f"{args.max_iterations} reviewed rounds and {args.max_iterations * REPEATS} "
            f"fresh GPU cases; original baseline control every {args.baseline_every}th round. "
            f"A round needs {ROUND_SECONDS}s remaining before admission.\n"
            f"Original frozen workspace baseline commit: {baseline}.\n"
        )
        if prior:
            goal += (
                f"Linked completed prior Study: {prior['study']}.\n"
                f"Read concise prior evidence: {args.root / 'prior-evidence.json'}.\n"
                f"Prior canonical research: {prior['research']}.\n"
                f"Prior best receipt: {prior['best']['receipt']}; mean "
                f"{prior['best']['metrics']['mean_startup_seconds']:.6f}s. Its exact "
                "reviewed instruction files seed this Study's starting workspace. "
                "First reproduce them unchanged; then optimize from the best "
                "eligible reviewed local experiment. Prior documents describe "
                "their completed authorization; this new campaign supersedes "
                "those historical stop instructions only for this new budget.\n"
            )
        config = HarnessConfig(
            model="gpt-6.1-sol",
            reasoning="medium",
            executable=str(args.codex_wrapper),
            timeout_seconds=EXECUTE_SECONDS,
        )
        initialize(
            args.root / "study",
            goal=goal,
            harness=config,
            workspace=workspace,
            allow_write=True,
            global_evaluation=EVALUATION,
            max_iterations=args.max_iterations,
            max_calls=None,
            max_seconds=args.budget_seconds,
            strategy={"selection": "direct", "review": "full"},
        )
        queue = TaskQueue(args.root / "study")
        harness = StartupResearchHarness(args, config, baseline, deadline=deadline)
        study = Study(args.root / "study", harness=harness)
        best = None
        stop_reason = "iteration_budget"
        for number in range(1, args.max_iterations + 1):
            if deadline - time.monotonic() < ROUND_SECONDS:
                stop_reason = "campaign_budget"
                break
            request, control = round_task(
                number,
                baseline={"revision": baseline, "every": args.baseline_every},
                prior=prior,
                best=best,
            )
            submitted = queue.submit(request)
            harness.rounds[number] = {
                "kind": request["title"],
                "control": control,
                "source_revision": submitted["source"]["revision"],
                "base_experiment": request["base_experiment"],
            }
            state = study.run(iterations=1)
            statuses = {row["id"]: row["status"] for row in queue.snapshot()}
            if (
                statuses.get(request["id"]) != "reviewed"
                or number not in harness.results
                or harness.errors
                or state.get("phase") != "done"
                or state.get("status") == "needs_attention"
            ):
                raise HarnessError(
                    "Admitted round did not complete evaluation and full Review"
                )
            reviewed_iteration(args.root / "study", f"{number:04d}")
            candidates = (
                study.frontiers()["global"].get(SCOPE, {}).get("candidates", [])
            )
            if candidates:
                best = min(
                    candidates, key=lambda row: row["metrics"]["mean_startup_seconds"]
                )["experiment_id"]
            else:
                best = None
            summary.update(
                status="running",
                results=harness.results,
                rounds=harness.rounds,
                completed_rounds=number,
                task_statuses=statuses,
                best_experiment=best,
                elapsed_seconds=time.monotonic() - started,
                source_revision=args.revision,
                instruction_only=True,
            )
            _publish(args.root / "summary.json", summary)
            if state.get("status") == "stopped" and number < args.max_iterations:
                if deadline - time.monotonic() < ROUND_SECONDS:
                    stop_reason = "campaign_budget"
                    break
                raise HarnessError(
                    "Review stopped before the continuing campaign budget; inspect its retained judgment"
                )
        summary.update(
            status="completed",
            results=harness.results,
            source_revision=args.revision,
            instruction_only=True,
            task_statuses={row["id"]: row["status"] for row in queue.snapshot()},
            completed_rounds=len(harness.results),
            stop_reason=stop_reason,
            best_experiment=best,
            rounds=harness.rounds,
        )
    except Exception as error:
        summary.update(status="error", error=f"{type(error).__name__}: {error}")
    finally:
        state_path = args.root / "study/state.json"
        if state_path.is_file():
            summary["state"] = json.loads(state_path.read_text())
        summary.update(
            elapsed_seconds=time.monotonic() - started,
            results=harness.results if harness else {},
            evaluation_cases_started=harness.case_count if harness else 0,
            unresolved_errors=harness.errors if harness else [],
        )
        _publish(args.root / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False), flush=True)
    return 0 if summary["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
