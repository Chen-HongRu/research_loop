"""Trusted, fixed-command bridge from one local request to one remote GPU job."""

from __future__ import annotations

import json
import math
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath

from research_loop.harness import (
    CodexHarness,
    HarnessConfig,
    HarnessError,
    publish_completed_response,
)
from research_loop.protocol import validate_response

_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_FILES = (
    "checkpoint.pt",
    "dev-predictions.csv",
    "test-predictions.csv",
    "train.py",
    "run.json",
    "timing.json",
)


def parse_request(value: object) -> dict:
    """Accept configuration only: never code, commands, paths or evaluator flags."""
    fields = {"job_id", "epochs", "lr", "batch_size", "seed"}
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"Request must contain exactly {sorted(fields)}")
    if type(value["job_id"]) is not str or not _JOB_ID.fullmatch(value["job_id"]):
        raise ValueError("job_id must be 1–64 ASCII letters/digits/underscores/hyphens")
    for name, low, high in (("epochs", 1, 5), ("batch_size", 32, 512)):
        if type(value[name]) is not int or not low <= value[name] <= high:
            raise ValueError(f"{name} must be an integer in [{low}, {high}]")
    if type(value["seed"]) is not int or not 0 <= value["seed"] <= 2**32 - 1:
        raise ValueError("seed must be an integer in [0, 2**32 - 1]")
    lr = value["lr"]
    if type(lr) not in (int, float) or not 0 < lr <= 0.1 or not math.isfinite(lr):
        raise ValueError("lr must be finite and in (0, 0.1]")
    return dict(value)


def _publish(path: Path, value: dict) -> None:
    if path.is_symlink():
        raise ValueError(f"Refusing symlink output: {path}")
    temporary = path.with_name(path.name + ".bridge-tmp")
    # Exclusive creation also rejects an agent-created temporary symlink.
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    temporary.replace(path)


REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "markdown": {"type": "string"},
    },
    "required": ["markdown"],
    "additionalProperties": False,
}
REPORT_INSTRUCTIONS = """
Final Execute output: return ONLY markdown under the supplied JSON schema.
Describe actual actions, evidence and limitations. The trusted bridge constructs
artifacts, measurements, IDs, metric values and receipt-derived outcome. Do not copy
these fields into your response. Earlier full-output examples are superseded.
"""


def report_arguments(kwargs):
    return {
        **kwargs,
        "schema": REPORT_SCHEMA,
        "prompt": kwargs["prompt"] + REPORT_INSTRUCTIONS,
    }


def publish_execution(run_dir, report, artifacts, measurements, completed):
    validate_response("execute", report, schema=REPORT_SCHEMA)
    # Preserve the original author receipt before publishing the controller envelope.
    run_dir = Path(run_dir)
    _publish(run_dir / "agent-report.json", report)
    original = run_dir / "completed.json"
    if original.is_file():
        _publish(run_dir / "agent-completed.json", json.loads(original.read_text()))
    result = {
        "outcome": "completed" if completed else "incomplete",
        "markdown": report["markdown"],
        "artifacts": artifacts,
        "measurements": measurements,
    }
    validate_response("execute", result)
    publish_completed_response(run_dir, result)
    return result


class CactusBridge:
    """Delegate every stage to real Codex; service only writable Execute requests."""

    def __init__(
        self,
        *,
        config: HarnessConfig,
        host: str,
        remote_base: str,
        gpu: str,
        study_id: str,
        seconds: int = 1800,
    ):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", host):
            raise ValueError("Invalid SSH host")
        if not re.fullmatch(r"[0-9]+", gpu):
            raise ValueError("GPU must be a confirmed numeric device index")
        if not _JOB_ID.fullmatch(study_id):
            raise ValueError("Invalid unique study ID")
        base = PurePosixPath(remote_base)
        if not base.is_absolute() or ".." in base.parts:
            raise ValueError("remote_base must be an absolute normalized path")
        self.delegate = CodexHarness(config)
        self.host = host
        self.base = base
        self.gpu = gpu
        self.study_id = study_id
        self.deadline = time.monotonic() + seconds
        self.started = False
        self.jobs: dict[str, dict] = {}
        self.bridge_errors: list[str] = []
        self.run_dir: Path | None = None
        self.remote_error_count = 0

    def _remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Smoke wall-clock budget exhausted")
        return remaining

    def _ssh(self, words: list[str]) -> str:
        argv = ["ssh", "-o", "BatchMode=yes", self.host, shlex.join(words)]
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=self._remaining(),
        )
        if result.returncode:
            # Controller diagnostics stay in the attempt, never in Agent results.
            self.remote_error_count += 1
            if self.run_dir is not None:
                try:
                    _publish(
                        self.run_dir
                        / f"controller-remote-error-{self.remote_error_count:03d}.json",
                        {
                            "argv": argv,
                            "remote_argv": words,
                            "returncode": result.returncode,
                            "stdout": result.stdout,
                            "stderr": result.stderr,
                        },
                    )
                except (OSError, ValueError):
                    self.bridge_errors.append(
                        "Could not save controller remote diagnostic"
                    )
            raise RuntimeError(f"Fixed remote command exited {result.returncode}")
        return result.stdout

    def evaluate(self, job_id: str, split: str) -> dict:
        """Controller-only evaluator. Test is called only after Study.run returns."""
        if split not in {"dev", "test"} or not _JOB_ID.fullmatch(job_id):
            raise ValueError("Invalid controller evaluation request")
        output = self.base / "runs" / f"{self.study_id}-{job_id}"
        result = json.loads(
            self._ssh(
                [
                    str(self.base / ".venv/bin/python"),
                    str(self.base / "evaluate.py"),
                    "--controller",
                    str(self.base / "prepared/controller"),
                    "--submission",
                    str(output / f"{split}-predictions.csv"),
                    "--split",
                    split,
                ]
            )
        )
        score = result.get("score")
        if (
            result.get("split") != split
            or result.get("metric") != "roc_auc"
            or type(score) not in (int, float)
            or not math.isfinite(score)
            or not 0 <= score <= 1
        ):
            raise ValueError("Invalid trusted evaluator response")
        return result

    def _train(self, request: dict, destination: Path) -> dict:
        job = request["job_id"]
        output = self.base / "runs" / f"{self.study_id}-{job}"
        # train.py itself exclusively creates output; it refuses existing jobs.
        words = [
            "env",
            f"CUDA_VISIBLE_DEVICES={self.gpu}",
            str(self.base / ".venv/bin/python"),
            str(self.base / "train.py"),
            "--data",
            str(self.base / "prepared/data"),
            "--output",
            str(output),
            "--device",
            "cuda:0",
            "--threads",
            "4",
            "--model",
            "tiny-cnn",
        ]
        for flag, field in (
            ("--epochs", "epochs"),
            ("--lr", "lr"),
            ("--batch-size", "batch_size"),
            ("--seed", "seed"),
        ):
            words.extend([flag, str(request[field])])
        started = time.monotonic()
        self._ssh(words)
        dev = self.evaluate(job, "dev")
        for name in _FILES:
            target = destination / name
            if target.exists() or target.is_symlink():
                raise ValueError("Refusing to overwrite an existing local artifact")
            # Fixed filenames only. No controller labels are copied or read locally.
            source = f"{self.host}:{shlex.quote(str(output / name))}"
            subprocess.run(
                ["scp", "-q", "-o", "BatchMode=yes", source, str(target)],
                capture_output=True,
                check=True,
                timeout=self._remaining(),
            )
        run = json.loads((destination / "run.json").read_text())
        version = f"{self.study_id}-{job}"
        result = {
            "status": "completed",
            "job_id": job,
            "request": request,
            "split": "dev",
            "metric": "auc",
            "score": dev["score"],
            "evaluator_metric": dev["metric"],
            "artifact": {
                "path": str(destination / "checkpoint.pt"),
                "version": version,
            },
            "source_sha256": run["source_sha256"],
            "evidence": [
                str(destination / name)
                for name in ("run.json", "timing.json", "dev-score.json")
            ],
            "bridge_wall_seconds": time.monotonic() - started,
            "remote_output": str(output),
        }
        _publish(destination / "dev-score.json", dev)
        return result

    def _watch(self, workspace: Path, stop: threading.Event) -> None:
        request_path = workspace / "train-request.json"
        seen = None
        while not stop.wait(0.2):
            if not request_path.exists():
                continue
            try:
                if request_path.is_symlink() or request_path.stat().st_size > 4096:
                    raise ValueError(
                        "Request must be a regular JSON file of at most 4096 bytes"
                    )
                raw = request_path.read_text()
                if raw == seen:
                    continue
                seen = raw
                request = parse_request(json.loads(raw))
                results = workspace / "results"
                if results.is_symlink():
                    raise ValueError("results must not be a symlink")
                results.mkdir(exist_ok=True)
                destination = results / request["job_id"]
                destination.mkdir(exist_ok=False)
                if self.started:
                    result = {
                        "status": "error",
                        "error": "Only one training request is allowed in this smoke",
                    }
                else:
                    self.started = True
                    try:
                        result = self._train(request, destination)
                    except Exception as exc:
                        result = {
                            "status": "error",
                            "job_id": request["job_id"],
                            "error": f"{type(exc).__name__}: remote training, grading, or artifact transfer failed; no automatic retry",
                        }
                    self.jobs[request["job_id"]] = result
                _publish(destination / "result.json", result)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                self.bridge_errors.append(message)
                try:
                    _publish(
                        workspace / "bridge-error.json",
                        {"status": "error", "error": message},
                    )
                except (OSError, ValueError):
                    pass

    def run(self, **kwargs) -> dict:
        self.run_dir = Path(kwargs["run_dir"]).resolve()
        if not kwargs.get("writable", False):
            return self.delegate.run(**kwargs)
        stop = threading.Event()
        worker_errors = []

        def watch():
            try:
                self._watch(Path(kwargs["workspace"]).resolve(), stop)
            except BaseException as error:
                worker_errors.append(error)

        watcher = threading.Thread(
            target=watch,
            name="cactus-trusted-bridge",
            daemon=True,
        )
        watcher.start()
        try:
            # Includes timeout_seconds, when supplied by the controller.
            report = self.delegate.run(**report_arguments(kwargs))
        finally:
            stop.set()
            watcher.join(timeout=max(0, self.deadline - time.monotonic()) + 2)
            if watcher.is_alive():
                self.bridge_errors.append(
                    "Bridge worker did not settle before smoke deadline"
                )

        if worker_errors:
            raise HarnessError(
                "Bridge worker failed; effects require reconciliation"
            ) from worker_errors[0]
        if (
            watcher.is_alive()
            or self.bridge_errors
            or any(job.get("status") != "completed" for job in self.jobs.values())
        ):
            raise HarnessError("Bridge effects are unresolved; reconciliation required")
        completed = [
            job for job in self.jobs.values() if job.get("status") == "completed"
        ]
        artifacts, measurements = [], []
        success = (
            len(completed) == 1 and not watcher.is_alive() and not self.bridge_errors
        )
        if success:
            job = completed[0]
            artifacts = [job["artifact"]]
            measurements = [
                {
                    "candidate_id": job["job_id"],
                    "artifact": job["artifact"],
                    "evaluation_id": "global-development-v1",
                    "comparison_scope": "development-v1",
                    "metrics": [{"name": "auc", "value": job["score"]}],
                    "evidence": job["evidence"],
                }
            ]
        return publish_execution(self.run_dir, report, artifacts, measurements, success)
