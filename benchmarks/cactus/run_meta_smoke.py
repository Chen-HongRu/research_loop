"""One real, development-only Meta Study, paired GPU runs, and supervision canaries."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from meta_inner import (
    FIXED_REQUEST,
    UsageBudgetHarness,
    baseline,
    read,
    usage,
    write,
)


POLICY = {"selection": "direct", "review": "concise"}
BASELINE_POLICY = {"selection": "debate", "review": "full"}
CANDIDATE_ID = "direct-concise-v1"
CHECKS = ["fixed-request", "frozen-execution", "published-review", "complete-telemetry"]


def protocol(host: str, remote_base: str, gpu: str, codex: str) -> dict:
    return {
        "id": "meta-cactus-development-v2",
        "stage": "development",
        "tasks": [{"id": "aerial-cactus", "metric": "auc"}],
        "repeats": 1,
        "checks": CHECKS,
        "run_budget": {"calls": 12, "seconds": 3600, "tokens": 5000000},
        "conditions": {
            "model": "gpt-6.1-sol",
            "reasoning": "medium",
            "codex": codex,
            "host": host,
            "remote_base": remote_base,
            "gpu": gpu,
            "training": FIXED_REQUEST,
            "model_loss": "tiny-cnn/Adam/BCEWithLogitsLoss",
            "train_source_sha256": hashlib.sha256(
                Path(__file__).with_name("train.py").read_bytes()
            ).hexdigest(),
            "data": "TIGER-Lab/mle-bench@01a3db85f9245ba64d87ed2a1170d486ee147e1f; public .19 seed0; dev .20 seed0",
            "order_seed": 0,
            "review_recovery": "at most one known structural Review rejection; successful CLI receipt required; never repeat Execute",
            "read_isolation": "unverified same-UID host; development only",
            "confirmation": "not authorized by this one-pair smoke; never promote",
            "total_endpoint": "after independent Review publication and final artifact verification",
            "trusted_sources": {
                name: hashlib.sha256(
                    Path(__file__).with_name(name).read_bytes()
                ).hexdigest()
                for name in (
                    "bridge.py",
                    "run_smoke.py",
                    "meta_inner.py",
                    "run_meta_smoke.py",
                )
            },
        },
    }


def subprocess_run(meta, args, version: dict, job_id: str, mode: str) -> dict:
    if getattr(args, "stop_event", None) is not None and args.stop_event.is_set():
        raise RuntimeError("Outer Execute ended; no further evaluation may launch")
    calls = 12
    seconds = 1800 if mode == "cactus" else 900
    meta.reserve_job(
        job_id, version["ref"], max_calls=calls, max_seconds=seconds, max_tokens=5000000
    )
    output = Path(meta.status()["controller_root"]) / "runs" / job_id
    command = [
        sys.executable,
        str(Path(__file__).with_name("meta_inner.py")),
        "--source",
        version["path"],
        "--ref",
        version["ref"],
        "--output",
        str(output),
        "--mode",
        mode,
        "--codex",
        args.codex,
        "--host",
        args.host,
        "--remote-base",
        args.remote_base,
        "--gpu",
        args.gpu,
        "--job-prefix",
        args.prefix + "-" + job_id,
    ]
    logs = Path(meta.status()["controller_root"]) / "logs"
    logs.mkdir(exist_ok=True)
    started = time.monotonic()
    with (
        (logs / f"{job_id}.stdout").open("w") as stdout,
        (logs / f"{job_id}.stderr").open("w") as stderr,
    ):
        try:
            timeout = min(
                seconds,
                getattr(args, "deadline", time.monotonic() + seconds)
                - time.monotonic()
                - 10,
            )
            if timeout <= 0:
                raise subprocess.TimeoutExpired(command, 0)
            process = subprocess.Popen(command, stdout=stdout, stderr=stderr)
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # SIGINT lets the inner harness terminate its own Codex group.
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
        except subprocess.TimeoutExpired:
            # Retain full reservation: child/remote effects are unknown, never replay.
            meta.finish_job(
                job_id,
                {
                    "status": "unknown",
                    "calls": calls,
                    "seconds": seconds,
                    "tokens": 5000000,
                },
            )
            raise RuntimeError(
                f"{job_id} timed out; reconcile retained effects before retry"
            )
    result_path = output / "result.json"
    if not result_path.is_file():
        meta.finish_job(
            job_id,
            {
                "status": "unknown",
                "calls": calls,
                "seconds": seconds,
                "tokens": 5000000,
            },
        )
        raise RuntimeError(f"{job_id} has no trusted result; no automatic replay")
    result = read(result_path)
    measured = result.get("usage")
    if not measured:
        try:
            measured = usage(output / "study")
        except (OSError, ValueError, KeyError):
            meta.finish_job(
                job_id,
                {
                    "status": "unknown",
                    "calls": calls,
                    "seconds": seconds,
                    "tokens": 5000000,
                },
            )
            raise RuntimeError(
                f"{job_id} has incomplete usage; full reservation retained"
            ) from None
    valid = process.returncode == 0 and result["status"] == "completed"
    if (
        result["version_ref"] != version["ref"]
        or result["commit"] != version["ref"]
        or result["policy"] != version["policy"]
    ):
        raise ValueError("Frozen subprocess identity mismatch")
    result["in_process_seconds"] = result["total_seconds"]
    result["total_seconds"] = time.monotonic() - started
    meta.finish_job(
        job_id,
        {
            "status": "completed" if valid else "failed",
            "calls": measured["calls"],
            "seconds": result["total_seconds"],
            "tokens": measured["tokens"],
        },
    )
    if not valid:
        raise RuntimeError(
            f"{job_id} failed: {result.get('error')}; no automatic retry"
        )
    write(output / "driver-receipt.json", result)
    return result


class MetaRequestHarness:
    """The Agent requests config only; the fixed driver freezes and evaluates it."""

    def __init__(self, meta, args, config):
        from research_loop.harness import CodexHarness

        self.delegate = CodexHarness(config)
        self.meta, self.args = meta, args
        self.started = False
        self.result = None
        self.errors = []

    def _evaluate(self, request: dict, workspace: Path) -> dict:
        state = self.meta.status()
        if request != {
            "candidate_id": CANDIDATE_ID,
            "parent_ref": state["supervisor_ref"],
            "policy": POLICY,
        }:
            raise ValueError("Only the fixed policy-only candidate is authorized")
        if read(workspace / "strategy.json") != POLICY:
            raise ValueError("Working policy differs from candidate request")
        candidate = self.meta.freeze_candidate(
            POLICY, state["supervisor_ref"], CANDIDATE_ID
        )
        version = self.meta.version(candidate["ref"])
        supervisor = self.meta.version(state["supervisor_ref"])
        # candidate.json is frozen before the first inner run, outside mutable worktree.
        write(self.meta.root / "research/iterations/0001/candidate.json", candidate)
        arms = [("S", supervisor), ("C", version)]
        random.Random(0).shuffle(arms)
        measurements, rows = {}, []
        for arm, frozen in arms:
            job_id = f"inner-{arm.lower()}"
            result = subprocess_run(self.meta, self.args, frozen, job_id, "cactus")
            expected_source = read(Path(state["controller_root"]) / "protocol.json")[
                "conditions"
            ]["train_source_sha256"]
            if (
                result["bridge_jobs"][result["job_id"]]["source_sha256"]
                != expected_source
            ):
                raise ValueError("Training program differs from the frozen protocol")
            measurements[arm] = result
            rows.append(
                {
                    "arm": arm,
                    "pair_id": "aerial-cactus:0",
                    "task": "aerial-cactus",
                    "repeat": 0,
                    "overhead_seconds": result["observations"]["overhead_wall_seconds"],
                    "total_seconds": result["total_seconds"],
                    "tokens": result["usage"]["tokens"],
                    "calls": result["usage"]["calls"],
                    "quality": result["quality"],
                    "valid": True,
                    "version_ref": frozen["ref"],
                    "job_id": job_id,
                }
            )
        canaries = {}
        canary_errors = {}
        for arm, frozen in arms:
            job_id = f"canary-{arm.lower()}"
            try:
                result = subprocess_run(self.meta, self.args, frozen, job_id, "canary")
            except RuntimeError as exc:
                if self.meta.status()["jobs"][job_id]["status"] != "failed":
                    raise
                canaries[arm] = False
                canary_errors[arm] = str(exc)
                continue
            canaries[arm] = (
                result["status"] == "completed"
                and result["actual_training_requests"] == 0
            )
        report = {
            "stage": "development",
            "protocol_ref": state["protocol_ref"],
            "supervisor_ref": supervisor["ref"],
            "candidate_ref": version["ref"],
            "rows": rows,
            "protocol_checks": dict.fromkeys(CHECKS, True),
            "canary": canaries,
            "canary_errors": canary_errors,
            "limitations": [
                "one development pair; no confirmation eligibility",
                "same-UID cross-run read isolation unverified",
                "shared AutoDL and model service; timing noise unestimated",
            ],
        }
        receipt = self.meta.record_evaluation(CANDIDATE_ID, report)
        by_arm = {row["arm"]: row for row in rows}
        public = {
            "status": "completed",
            "candidate": candidate,
            "evaluation_receipt": receipt,
            "report": report,
            "ratios": {
                key: by_arm["C"][key] / by_arm["S"][key]
                for key in ("overhead_seconds", "total_seconds", "tokens")
            },
            "metrics": "C/S",
            "promotion_eligible": False,
            "final_private_feedback": False,
        }
        self.result = {
            "candidate": candidate,
            "measurements": measurements,
            "report": report,
            "canaries": canaries,
        }
        write(
            Path(self.meta.status()["controller_root"]) / "paired-result.json",
            self.result,
        )
        return public

    def _watch(self, workspace: Path, stop: threading.Event):
        request = workspace / "candidate-request.json"
        while not stop.wait(0.2):
            if not request.is_file():
                continue
            if self.started:
                return
            self.started = True
            try:
                if request.is_symlink() or request.stat().st_size > 4096:
                    raise ValueError(
                        "Candidate request must be a small regular JSON file"
                    )
                result = self._evaluate(read(request), workspace)
            except Exception as exc:
                result = {
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "retry": False,
                }
                self.errors.append(result["error"])
            write(workspace / "evaluation-result.json", result)
            return

    def run(self, **kwargs):
        if not kwargs["writable"]:
            return self.delegate.run(**kwargs)
        from bridge import publish_execution, report_arguments

        stop = threading.Event()
        self.args.stop_event = stop
        self.args.deadline = time.monotonic() + kwargs["timeout_seconds"]
        worker_errors = []

        def watch():
            try:
                self._watch(Path(kwargs["workspace"]), stop)
            except BaseException as error:
                worker_errors.append(error)

        thread = threading.Thread(target=watch, daemon=True)
        thread.start()
        try:
            report = self.delegate.run(**report_arguments(kwargs))
        finally:
            stop.set()
            thread.join(timeout=30)
            if thread.is_alive():
                self.errors.append(
                    "Evaluation worker still running; no promotion or replay"
                )

        if worker_errors:
            from research_loop.harness import HarnessError

            raise HarnessError(
                "Evaluation worker failed; effects require reconciliation"
            ) from worker_errors[0]
        if (
            thread.is_alive()
            or self.errors
            or (getattr(self, "started", False) and self.result is None)
        ):
            from research_loop.harness import HarnessError

            raise HarnessError(
                "Evaluation effects are unresolved; reconciliation required"
            )
        artifacts, measurements = [], []
        success = self.result is not None and not thread.is_alive() and not self.errors
        if success:
            candidate = self.result["candidate"]
            artifact = {
                "path": str(Path(kwargs["workspace"]).resolve() / "strategy.json"),
                "version": candidate["ref"],
            }
            by_arm = {row["arm"]: row for row in self.result["report"]["rows"]}
            artifacts = [artifact]
            measurements = [
                {
                    "candidate_id": CANDIDATE_ID,
                    "artifact": artifact,
                    "evaluation_id": "meta-development-v1",
                    "comparison_scope": candidate["parent_ref"],
                    "metrics": [
                        {
                            "name": "mean_rO",
                            "value": by_arm["C"]["overhead_seconds"]
                            / by_arm["S"]["overhead_seconds"],
                        }
                    ],
                    "evidence": [
                        str(
                            Path(kwargs["workspace"]).resolve()
                            / "evaluation-result.json"
                        )
                    ],
                }
            ]
        return publish_execution(
            kwargs["run_dir"], report, artifacts, measurements, success
        )


def verify_outer(root: Path, state: dict, paired: dict) -> None:
    if state["phase"] != "done" or not state.get("review_ref"):
        raise ValueError("Outer investigation has no completed Review")
    executed = read(root / "research" / state["execution_ref"])
    candidate = paired["candidate"]
    artifact = {
        "path": str(root / "research/iterations/0001/workspace/strategy.json"),
        "version": candidate["ref"],
    }
    if (
        executed["outcome"] != "completed"
        or executed["artifacts"] != [artifact]
        or read(Path(artifact["path"])) != POLICY
    ):
        raise ValueError("Outer Execute is not bound to the frozen policy candidate")
    by_arm = {row["arm"]: row for row in paired["report"]["rows"]}
    expected_ratio = by_arm["C"]["overhead_seconds"] / by_arm["S"]["overhead_seconds"]
    measurements = executed["measurements"]
    if len(measurements) != 1:
        raise ValueError(
            "Outer Execute must report one trusted development measurement"
        )
    measurement = measurements[0]
    if (
        measurement["artifact"] != artifact
        or measurement["evaluation_id"] != "meta-development-v1"
        or measurement["comparison_scope"] != candidate["parent_ref"]
        or measurement["metrics"] != [{"name": "mean_rO", "value": expected_ratio}]
    ):
        raise ValueError("Outer measurement differs from the trusted pair")
    reviewed = read(root / "research" / state["review_ref"])
    if len(reviewed["candidate_reviews"]) != 1 or not all(
        entry["valid"] and entry["verified"] for entry in reviewed["candidate_reviews"]
    ):
        raise ValueError("Outer independent Review did not validate the actual policy")
    if read(root / "research/iterations/0001/iteration.json")["status"] != "reviewed":
        raise ValueError("Outer Review was not published")


def supervisor(args) -> int:
    # Bootstrap before importing; the real supervising Study uses frozen S too.
    sys.path.insert(0, str(args.source.resolve() / "src"))
    from research_loop import Study, core, initialize
    from research_loop.harness import HarnessConfig
    from research_loop.meta import MetaStudy

    meta = MetaStudy(args.study)
    state = meta.status()
    frozen = meta.version(state["supervisor_ref"])
    if (
        Path(core.__file__).resolve().parent
        != args.source.resolve() / "src/research_loop"
    ):
        raise ValueError("Supervisor loaded live source")
    request = {
        "candidate_id": CANDIDATE_ID,
        "parent_ref": frozen["ref"],
        "policy": POLICY,
    }
    interface = f"""# First Meta candidate interface
Current frozen supervisor: {frozen["ref"]}.
Only strategy.json is mutable research policy. The first authorized candidate is
{json.dumps(POLICY)}: retain two independent proposals and Judge, omit debate calls,
and retain independent evidence Review with concise expression.
No training, controller, scorer, permissions, protocol, model or budget code edits.
During Execute update strategy.json and atomically submit candidate-request.json:
{json.dumps(request)}
The trusted bridge freezes a new immutable version before evaluation. It runs one
fixed S/C Cactus GPU pair and S/C bounded supervision canaries. This takes minutes.
Wait for evaluation-result.json (up to 2700 seconds; a bounded shell polling loop is
appropriate). Read actual report, do not resubmit, launch child agents or loops,
run network/SSH commands, install packages, or read other studies/controller files.
Only development feedback is returned. One pair cannot authorize promotion.
"""
    goal = """Run one policy-only Meta investigation to reduce selection and Review overhead.
Follow README.md exactly; base_experiment=null, references=[], no local_evaluation.
Plan markdown names only existing baseline README.md/strategy.json inputs with actual
Git HEAD. Plan action creates the exact allowed policy and submits one request. Set
timeout_seconds=2700 for the long evaluation. The future iteration workspace need
not exist during Plan; use the actual path supplied during Execute.
Execute must await evaluation-result.json and inspect report. On completed report
return ONLY markdown under the supplied JSON schema. The controller
constructs the policy artifact/version, candidate ID, evaluation scope, overhead
ratio and evidence from the trusted pair. Describe evidence and limitations; never
fabricate a ratio. Review inspect policy and report, qualify
evidence, update both route/global research, and stop. No production promotion claim.
"""
    config = HarnessConfig(executable=args.codex, timeout_seconds=2700)
    harness = MetaRequestHarness(meta, args, config)
    root = args.study.resolve()
    baseline(root / "baseline", interface, frozen["policy"])
    evaluation = {
        "id": "meta-development-v1",
        "comparison_scope": frozen["ref"],
        "protocol": "One same-seed paired development investigation, trusted wall intervals through Review publication, mean C/S overhead; no formal confirmation.",
        "data_role": "development",
        "allow_feedback": True,
        "objectives": [{"name": "mean_rO", "direction": "minimize", "unit": "ratio"}],
        "constraints": [],
    }
    initialize(
        root / "research",
        goal=goal,
        harness=config,
        workspace=root / "baseline",
        allow_write=True,
        global_evaluation=evaluation,
        max_iterations=1,
        max_calls=12,
        max_seconds=3600,
        selection={"seed": 0, "debate_rounds": 1, "max_resamples": 0},
        strategy=frozen["policy"],
    )
    meta.reserve_job(
        "outer", frozen["ref"], max_calls=12, max_seconds=3600, max_tokens=5000000
    )
    started = time.monotonic()
    result = {
        "status": "running",
        "source": str(args.source),
        "loaded_core": core.__file__,
        "supervisor_ref": frozen["ref"],
    }
    try:
        result["state"] = Study(
            root / "research",
            harness=UsageBudgetHarness(harness, root / "research"),
        ).run(iterations=1)
        measured = usage(root / "research")
        elapsed = time.monotonic() - started
        complete = (
            result["state"]["phase"] == "done"
            and result["state"].get("review_ref")
            and harness.result
            and not harness.errors
        )
        if complete:
            verify_outer(root, result["state"], harness.result)
        meta.finish_job(
            "outer",
            {
                "status": "completed" if complete else "failed",
                "calls": measured["calls"],
                "seconds": elapsed,
                "tokens": measured["tokens"],
            },
        )
        result.update(
            status="completed" if complete else "failed",
            usage=measured,
            total_seconds=elapsed,
            bridge_errors=harness.errors,
        )
        if complete:
            publication = root / "research/iterations/0001/publication.json"
            # Formal promotion also requires a complete outer publication. This
            # development protocol deterministically keeps the incumbent.
            result["decision"] = meta.decide(CANDIDATE_ID, publication=publication)
    except Exception as exc:
        result.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        if meta.status()["jobs"]["outer"]["status"] == "reserved":
            meta.finish_job(
                "outer",
                {"status": "unknown", "calls": 12, "seconds": 3600, "tokens": 5000000},
            )
    write(root / "smoke-summary.json", result)
    return 0 if result["status"] == "completed" else 1


def archive_runner(controller: Path, conditions: dict) -> Path:
    directory = controller / "trusted-runner"
    directory.mkdir()
    for name in (*conditions["trusted_sources"], "train.py"):
        source = Path(__file__).with_name(name)
        target = directory / name
        shutil.copyfile(source, target)
        expected = (
            conditions["train_source_sha256"]
            if name == "train.py"
            else conditions["trusted_sources"][name]
        )
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise ValueError("Trusted source changed during protocol freeze")
        target.chmod(0o444)
    directory.chmod(0o555)
    return directory


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--codex", required=True)
    parser.add_argument(
        "--host", required=True, help="Operator-configured SSH host alias"
    )
    parser.add_argument("--remote-base", required=True)
    parser.add_argument("--gpu", required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--prefix")
    parser.add_argument("--supervisor", action="store_true")
    args = parser.parse_args(argv)
    if args.supervisor:
        return supervisor(args)
    from research_loop.meta import MetaStudy

    project = Path(__file__).resolve().parents[2]
    meta = MetaStudy.initialize(
        args.study,
        source=project,
        protocol=protocol(args.host, args.remote_base, args.gpu, args.codex),
        baseline_policy=BASELINE_POLICY,
        budget={
            "runs": 5,
            "calls": 60,
            "seconds": 10800,
            "tokens": 30000000,
            "candidates": 1,
        },
    )
    frozen = meta.version(meta.status()["supervisor_ref"])
    controller = Path(meta.status()["controller_root"])
    fixed_runner = archive_runner(
        controller, read(controller / "protocol.json")["conditions"]
    )
    prefix = args.prefix or "meta-" + uuid.uuid4().hex[:12]
    command = [
        sys.executable,
        str(fixed_runner / Path(__file__).name),
        "--supervisor",
        "--study",
        str(args.study.resolve()),
        "--source",
        frozen["path"],
        "--codex",
        args.codex,
        "--host",
        args.host,
        "--remote-base",
        args.remote_base,
        "--gpu",
        args.gpu,
        "--prefix",
        prefix,
    ]
    with (
        (controller / "supervisor.stdout").open("w") as stdout,
        (controller / "supervisor.stderr").open("w") as stderr,
    ):
        process = subprocess.run(command, stdout=stdout, stderr=stderr, timeout=3700)
    summary = read(args.study / "smoke-summary.json")
    # Final scoring occurs only after ALL model calls and outer publication.
    # No further model call or candidate change follows these private scores.
    if summary["status"] == "completed":
        sys.path.insert(0, str(fixed_runner))
        from bridge import CactusBridge
        from research_loop.harness import HarnessConfig

        paired = read(controller / "paired-result.json")
        scores = {}
        for arm, measured in paired["measurements"].items():
            bridge = CactusBridge(
                config=HarnessConfig(executable=args.codex),
                host=args.host,
                remote_base=args.remote_base,
                gpu=args.gpu,
                study_id=measured["bridge_id"],
                seconds=180,
            )
            scores[arm] = bridge.evaluate(measured["job_id"], "test")
        write(controller / "final-private-scores.json", scores)
        summary.update(
            private_scores=scores,
            model_calls_after_final_scoring=0,
            candidate_ref=paired["candidate"]["ref"],
            paired_report=paired["report"],
            canaries=paired["canaries"],
            current_supervisor_ref=meta.status()["supervisor_ref"],
        )
        write(args.study / "smoke-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
