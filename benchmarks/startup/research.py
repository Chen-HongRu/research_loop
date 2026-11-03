"""Two real Study iterations measuring fixed from-zero GPU/dashboard startup.

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
from research_loop.harness import CodexHarness, HarnessConfig, HarnessError
from research_loop.tasks import TaskQueue


EDITABLE = ("SKILL.md", "references/operations.md")
SCOPE = "startup-e2e-v3"
CASE_SECONDS = 420
REPEATS = 3
ARTIFACT = "startup-evaluation.json"
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
GPU smoke through the existing research-loop interfaces. Complete exactly the two
queued investigations: measure the unchanged baseline, then make and measure one
instruction-only optimization informed by the published baseline evidence.

The controller runs three serial fresh Terra/medium cases for each submission.
Each case includes fresh remote environment/source setup, a real Study with full
Plan/Execute/Review, one unchanged synthetic B2/L16/K4 seed43 three-update CUDA job,
and final training/diagnostic/performance scalars in the existing dashboard.
Outer research roles use Sol/medium. Model CLIs run locally; GPU/backend artifacts
run on the configured AutoDL backend through the fixed evaluator, never your shell.
There are at most six GPU jobs. Do not launch model/GPU/SSH/network commands,
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
take about 23 minutes; Plan should grant Execute 1500 seconds within Study budget.
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
judgments. Baseline completion alone does not meet this two-investigation goal;
do not stop the Study before the queued optimization and its full Review.
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


def freeze_candidate(source, revision, workspace, baseline, destination, *, unchanged):
    changed = changed_files(workspace, baseline)
    allowed = set() if unchanged else set(EDITABLE)
    if changed - allowed:
        raise ValueError(
            f"Candidate changed protected tracked files: {sorted(changed - allowed)}"
        )
    if not unchanged and not changed:
        raise ValueError("Optimization must make one instruction-only source change")
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


class StartupResearchHarness:
    def __init__(self, args, config, baseline):
        self.args, self.config, self.baseline = args, config, baseline
        self.delegate = CodexHarness(config)
        self.results = {}
        self.errors = []
        self.claimed = set()
        self.case_count = 0

    def evaluate(self, workspace, iteration, stop, deadline):
        if iteration not in (1, 2) or self.case_count + REPEATS > 6:
            raise HarnessError("Authorized two-iteration/six-case budget exceeded")
        controller = self.args.root / "controller" / f"{iteration:04d}"
        candidate = freeze_candidate(
            self.args.source,
            self.args.revision,
            workspace,
            self.baseline,
            controller / "runtime",
            unchanged=iteration == 1,
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
        if not kwargs.get("writable", False):
            return self.delegate.run(**kwargs)
        workspace = Path(kwargs["workspace"]).resolve()
        iteration = int(
            json.loads((self.args.root / "study/state.json").read_text())["iteration"]
        )
        key = str(workspace)
        if key in self.claimed:
            raise HarnessError(
                "Execute already claimed; inspect retained result without replay"
            )
        deadline = time.monotonic() + min(kwargs.get("timeout_seconds", 1500), 1500)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "source", "prepared-source", "codex-wrapper"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("host", "remote-root", "remote-python"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--gpu", default="0")
    args = parser.parse_args()
    for name in ("root", "source", "prepared_source", "codex_wrapper"):
        setattr(args, name, getattr(args, name).resolve())
    if not args.gpu.isdecimal() or args.host.startswith("-"):
        parser.error("Invalid GPU or SSH host")
    args.root.mkdir(mode=0o700, parents=True, exist_ok=False)
    summary = {"status": "error", "comparison_scope": SCOPE, "max_gpu_jobs": 6}
    harness = None
    started = time.monotonic()
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
        config = HarnessConfig(
            model="gpt-6.1-sol",
            reasoning="medium",
            executable=str(args.codex_wrapper),
            timeout_seconds=1500,
        )
        initialize(
            args.root / "study",
            goal=GOAL,
            harness=config,
            workspace=workspace,
            allow_write=True,
            global_evaluation=EVALUATION,
            max_iterations=2,
            max_calls=10,
            max_seconds=3600,
            strategy={"selection": "direct", "review": "full"},
        )
        queue = TaskQueue(args.root / "study")
        for identity, priority, changes, instruction in (
            (
                "baseline",
                0,
                False,
                "Measure the unchanged baseline using the canonical evaluate submission. No tracked source edits. Complete all three fixed cases and full Review; the optimization task remains pending.",
            ),
            (
                "optimize",
                1,
                True,
                "Read the published baseline receipt and research first. Make one concrete instruction-only startup optimization in SKILL.md and/or references/operations.md. Submit once for the same three-case protocol and complete full Review. Python, scorer and every success requirement are fixed.",
            ),
        ):
            queue.submit(
                {
                    "id": identity,
                    "title": identity,
                    "instruction": instruction,
                    "priority": priority,
                    "allow_changes": changes,
                    "route_id": "new",
                    "base_experiment": None,
                }
            )
        harness = StartupResearchHarness(args, config, baseline)
        state = Study(args.root / "study", harness=harness).run(iterations=2)
        # Canonical task settlement checks publication.metadata == iteration.json,
        # status reviewed, and the published iteration/current-state boundary.
        task_statuses = {row["id"]: row["status"] for row in queue.snapshot()}
        reviewed = all(
            task_statuses.get(name) == "reviewed" for name in ("baseline", "optimize")
        )
        if (
            not reviewed
            or len(harness.results) != 2
            or harness.errors
            or state.get("phase") != "done"
            or state.get("status") == "needs_attention"
        ):
            raise HarnessError(
                "Two evaluated and independently reviewed investigations did not complete"
            )
        summary.update(
            status="completed",
            state=state,
            results=harness.results,
            source_revision=args.revision,
            instruction_only=True,
            task_statuses=task_statuses,
        )
    except Exception as error:
        summary["error"] = f"{type(error).__name__}: {error}"
    finally:
        state_path = args.root / "study/state.json"
        if state_path.is_file():
            summary["state"] = json.loads(state_path.read_text())
        summary.update(
            elapsed_seconds=time.monotonic() - started,
            evaluation_cases_started=harness.case_count if harness else 0,
            unresolved_errors=harness.errors if harness else [],
        )
        _publish(args.root / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False), flush=True)
    return 0 if summary["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
