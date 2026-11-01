"""Bounded bridge to the protected ProTok evaluator; never imports candidate code."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
from pathlib import Path
import signal
import subprocess
import time
import uuid


CANDIDATE_FILES = (
    "protok/model/autoresearch_se3.py",
    "protok/model/autoresearch_se3_loss.py",
    "configs/autoresearch_se3_candidate.json",
)
EVALUATOR = "scripts/autoresearch_se3_eval.py"
PROTOCOL = "se3-search-v2-2k-subset-seed42-v1"
METRIC = "subset_ca_rmsd_2000_seed42"


class ProTokError(RuntimeError):
    """A gate or evidence check failed; no substitute score is available."""


def _read(path):
    return json.loads(Path(path).read_text())


def _write(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _outstanding(state):
    return state.get("status") not in {"complete", "failed"} or any(
        attempt.get("status") != "prepared"
        and (
            not attempt.get("terminal_state")
            or not _finite(attempt.get("gpu_seconds"))
            or attempt["gpu_seconds"] < 0
        )
        for attempt in state.get("attempts", [])
    )


# Only the pinned evaluator is imported to validate source/configuration. Scoring
# invokes its original CLI directly, retaining the global lock and accounting.
_HELPER = """
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location('protected_protok', sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
config = m.load_config(sys.argv[2])
candidates = m.source_boundary(config)
print(json.dumps(dict(fingerprint=m.fingerprint(config, candidates),
                     config_identity=m.config_identity(config))))
"""


class ProTokBackend:
    def __init__(self, project: Path, control: Path, owner_token: str | None = None):
        self.project = Path(project).resolve()
        self.control = Path(control).resolve()
        self.owner_token = owner_token
        if self.control.is_relative_to(self.project):
            raise ProTokError("Control directory must be outside the canonical project")

    def _config(self):
        config = _read(self.project / "autoresearch-run.json")
        if (
            config.get("workspace") != str(self.project)
            or config.get("protocol") != PROTOCOL
            or config.get("metric") != METRIC
            or config.get("seeds") != [42]
        ):
            raise ProTokError("Canonical ProTok protocol/configuration mismatch")
        script = self.project / EVALUATOR
        if (
            script.is_symlink()
            or script.stat().st_size > 4 * 1024 * 1024
            or hashlib.sha256(script.read_bytes()).hexdigest()
            != config.get("locked_files", {}).get(EVALUATOR)
        ):
            raise ProTokError("Protected evaluator does not match its source lock")
        return config

    def _receipt_config_matches(self, frozen, expected):
        frozen = {k: v for k, v in frozen.items() if k != "workspace"}
        expected = {k: v for k, v in expected.items() if k != "workspace"}
        if frozen == expected:
            return True
        continuation = self.control / "continuation.json"
        if not continuation.exists():
            return False
        control = _read(continuation)
        relocation = control.get("relocation", {})
        if (
            control.get("schema") != 2
            or expected.get("records_root") != relocation.get("current_records_root")
            or frozen.get("records_root") != relocation.get("previous_records_root")
        ):
            return False
        amendment = _read(Path(expected["records_root"]) / "protocol-amendment.json")
        previous = amendment["previous_config"]
        if previous.get("records_root") != relocation["previous_records_root"]:
            return False
        canonical = self._config()
        # Only explicitly relocated transport locations may differ. All scoring,
        # resources, source locks and metric fields retain exact comparison.
        for name in ("records_root", "template", "ssh_host", "remote_root"):
            if frozen.get(name) != expected.get(name):
                if frozen.get(name) != previous.get(name) or expected.get(
                    name
                ) != canonical.get(name):
                    return False
                frozen[name] = expected[name]
        return frozen == expected

    def inspect(self) -> dict:
        """Read small control/receipt JSON only; never query SSH or read data."""
        config = self._config()
        root = Path(config["records_root"])
        session = (
            _read(root / "session.json") if (root / "session.json").exists() else None
        )
        states = [(p.parent.name, _read(p)) for p in sorted(root.glob("*/state.json"))]
        receipts, outstanding, used = [], [], 0
        for name, state in states:
            attempts = [
                {
                    k: a[k]
                    for k in (
                        "seed",
                        "status",
                        "owned_job_id",
                        "terminal_state",
                        "exit_code",
                        "gpu_seconds",
                    )
                    if k in a
                }
                for a in state.get("attempts", [])
            ]
            summary = state.get("summary", {})
            row = dict(
                record=name,
                fingerprint=state.get("fingerprint"),
                status=state.get("status"),
                mode=state.get("mode"),
                attempts=attempts,
                summary={
                    k: summary[k]
                    for k in (
                        "protocol",
                        "objective",
                        "value",
                        "quality_score_eligible",
                        "fingerprint",
                    )
                    if k in summary
                },
                evidence=[
                    str(root / name / n)
                    for n in ("state.json", "summary.json", "run-config.json")
                ],
            )
            receipts.append(row)
            if _outstanding(state):
                outstanding.append(row)
            used += sum(
                a["gpu_seconds"] for a in attempts if _finite(a.get("gpu_seconds"))
            )
        continuation = {}
        for name in (
            ".autoresearch-continuation.json",
            "autoresearch-continuation.json",
            "experiments/continuation.json",
        ):
            if (self.project / name).exists():
                continuation[name] = _read(self.project / name)
        return dict(
            project=str(self.project),
            protocol=PROTOCOL,
            metric=METRIC,
            session=session,
            record_count=len(states),
            records=receipts,
            scored_records=sum(s.get("mode") == "scored" for _, s in states),
            used_gpu_seconds=used,
            outstanding=outstanding,
            off=(self.project / ".autoresearch-off").exists(),
            continuation=continuation,
        )

    def _command(self, config_path, operation, *, recover=False):
        command = [
            "uv",
            "run",
            "--no-project",
            "--python",
            "3.12",
            "python",
            "-B",
            "-I",
        ]
        if operation == "prepare":
            return command + [
                "-c",
                _HELPER,
                str(self.project / EVALUATOR),
                str(config_path),
            ]
        if (self.control / "continuation.json").exists():
            return command + [
                str(Path(__file__).with_name("protok_continuation.py").resolve()),
                "--project",
                str(self.project),
                "--control",
                str(self.control),
                "--owner-token",
                self.owner_token or "",
                "--config",
                str(config_path),
                "--evaluate",
                *(["--recover"] if recover else []),
            ]
        return command + [
            str(self.project / EVALUATOR),
            "--config",
            str(config_path),
            "--evaluate",
        ]

    def prepare_config(self, workspace: Path, output: Path) -> dict:
        """Validate the three-file boundary and retarget only operational workspace."""
        config = self._config()
        workspace, output = Path(workspace).resolve(), Path(output).resolve()
        if (
            workspace == self.project
            or output.is_relative_to(workspace)
            or output.is_relative_to(self.project)
        ):
            raise ProTokError(
                "Use an isolated workspace and an external runtime config"
            )

        def git(root, *args):
            result = subprocess.run(
                ["git", "-C", str(root), *args], capture_output=True, text=True
            )
            if result.returncode:
                raise ProTokError(
                    "Cannot establish candidate Git boundary: " + result.stderr.strip()
                )
            return result.stdout

        head = git(self.project, "rev-parse", "HEAD").strip()
        changed = set(
            git(workspace, "diff", "--name-only", "-z", head, "--").split("\0")
        )
        changed |= set(git(workspace, "ls-files", "--others", "-z").split("\0"))
        if extra := changed - set(CANDIDATE_FILES) - {""}:
            raise ProTokError(
                "Changes outside the candidate boundary: " + ", ".join(sorted(extra))
            )
        for name in CANDIDATE_FILES:
            path = workspace / name
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size > 4 * 1024 * 1024
            ):
                raise ProTokError(
                    "Candidate must be a small regular source file: " + name
                )
        config["workspace"] = str(workspace)
        output.parent.mkdir(parents=True, exist_ok=True)
        _write(output, config)
        result = subprocess.run(
            self._command(output, "prepare"),
            capture_output=True,
            text=True,
            cwd=self.control if self.control.exists() else output.parent,
            timeout=120,
        )
        if result.returncode:
            raise ProTokError(
                "Protected config/source validation failed: " + result.stderr[-2000:]
            )
        identity = json.loads(result.stdout)
        return dict(config_path=str(output), workspace=str(workspace), **identity)

    def _gate(self, identity, *, recover=False, preparing=False, config_path=None):
        config, now = self._config(), time.time()
        continuation = (self.control / "continuation.json").exists()
        lease = (
            _read(self.control / "ownership.json")
            if (self.control / "ownership.json").exists()
            else {}
        )
        if not (
            lease.get("schema") == 1
            and self.owner_token
            and lease.get("owner_token") == self.owner_token
            and lease.get("project") == str(self.project)
            and lease.get("handoff_acknowledged") is True
            and _finite(lease.get("acknowledged_at"))
            and _finite(lease.get("expires_at"))
            and lease["acknowledged_at"] <= now
            and ((recover and continuation) or now < lease["expires_at"])
            and lease.get("previous_owner")
            and lease.get("evidence")
        ):
            raise ProTokError("Acknowledged, unexpired owner lease required")
        status = self.inspect()
        if (not recover or not continuation) and (self.control / "STOP").exists():
            raise ProTokError("Continuation is paused")
        if not config.get("launch_enabled") or (not continuation and status["off"]):
            raise ProTokError("Canonical study is off or launch disabled")
        control = None
        if continuation and not (recover and preparing):
            from .protok_continuation import validate_continuation

            try:
                _, _, control = validate_continuation(
                    self.project,
                    self.control,
                    self.owner_token,
                    config_path or self.project / "autoresearch-run.json",
                    recover=recover,
                )
            except (ValueError, RuntimeError, OSError) as error:
                raise ProTokError(f"Continuation admission failed: {error}") from error
        if status["outstanding"] and not (
            recover
            and (
                preparing
                or all(r["fingerprint"] == identity for r in status["outstanding"])
            )
        ):
            raise ProTokError(
                "Outstanding global record requires explicit matching recovery"
            )
        if recover and not preparing:
            for row in status["outstanding"]:
                attempts = [a for a in row["attempts"] if a.get("seed") == 42]
                if not attempts or attempts[-1].get("status") not in {
                    "running",
                    "submitting",
                    "finished",
                    "complete",
                }:
                    raise ProTokError(
                        "Recovery must reconcile an existing allocation; no new submission"
                    )
        if recover and continuation:
            if not preparing and not status["outstanding"]:
                raise ProTokError(
                    "Receipt recovery requires a matching outstanding allocation"
                )
            return
        bound = 1800 * len(config["seeds"])
        if control and control.get("schema") == 2:
            if control["effective_deadline"] - now < bound:
                raise ProTokError("Continuation wall deadline exhausted")
            spent = status["used_gpu_seconds"] - control["initial_gpu_seconds"]
            if (
                spent < 0
                or spent + bound + config["reserve_gpu_seconds"]
                > control["additional_gpu_seconds"]
            ):
                raise ProTokError("Additional GPU budget exhausted")
            return
        session = status["session"]
        if (
            not session
            or not _finite(session.get("deadline"))
            or min(
                session["deadline"],
                control["effective_deadline"] if control else session["deadline"],
            )
            - now
            < bound
        ):
            raise ProTokError("Original session wall deadline exhausted")
        if not continuation and status["scored_records"] >= config["max_evaluations"]:
            raise ProTokError("Original scored experiment budget exhausted")
        if (
            status["used_gpu_seconds"] + bound + config["reserve_gpu_seconds"]
            > config["gpu_budget_seconds"]
        ):
            raise ProTokError("Original GPU budget exhausted")

    def evaluate(
        self,
        workspace: Path,
        output: Path,
        timeout_seconds: float,
        *,
        recover: bool = False,
        cancel_event=None,
    ) -> dict:
        """Run once, preserving actual process status and fingerprint-bound evidence."""
        if not _finite(timeout_seconds) or timeout_seconds <= 0:
            raise ProTokError("A finite positive timeout is required")

        def cancelled():
            return cancel_event is not None and cancel_event.is_set()

        if cancelled():
            raise ProTokError("Evaluation cancelled before launch")
        self.control.mkdir(parents=True, exist_ok=True)
        with (self.control / "protok.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Gate before preparing or invoking the protected evaluator.
            self._gate(None, recover=recover, preparing=True)
            prepared = self.prepare_config(workspace, output)
            identity = prepared["fingerprint"]
            self._gate(identity, recover=recover, config_path=output)
            if cancelled():
                raise ProTokError("Evaluation cancelled before launch")
            run = self.control / ("evaluation-" + uuid.uuid4().hex)
            run.mkdir()
            receipt = dict(
                **prepared,
                metric=METRIC,
                stdout=str(run / "stdout.log"),
                stderr=str(run / "stderr.log"),
                receipt=str(run / "receipt.json"),
                started_at=time.time(),
                verified=False,
            )
            with (
                (run / "stdout.log").open("w") as stdout,
                (run / "stderr.log").open("w") as stderr,
            ):
                process = subprocess.Popen(
                    self._command(output, "evaluate", recover=recover),
                    cwd=self.control,
                    stdout=stdout,
                    stderr=stderr,
                )
                deadline = time.monotonic() + timeout_seconds
                receipt.update(timed_out=False, cancelled=False)
                while process.poll() is None:
                    remaining = deadline - time.monotonic()
                    if cancelled() or remaining <= 0:
                        receipt.update(cancelled=cancelled(), timed_out=not cancelled())
                        receipt["termination_reason"] = (
                            "cancelled" if receipt["cancelled"] else "timed_out"
                        )
                        process.send_signal(signal.SIGTERM)
                        try:
                            process.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        break
                    try:
                        process.wait(timeout=min(0.5, remaining))
                    except subprocess.TimeoutExpired:
                        pass
            receipt.update(exit_code=process.returncode, finished_at=time.time())
            _write(run / "receipt.json", receipt)
            try:
                if (
                    receipt["cancelled"]
                    or receipt["timed_out"]
                    or process.returncode != 0
                ):
                    raise ProTokError(
                        "Protected evaluator failed, cancelled, or timed out"
                    )
                lines = [
                    line
                    for line in (run / "stdout.log").read_text().splitlines()
                    if line.startswith("METRIC ")
                ]
                if len(lines) != 1 or not lines[0].startswith(f"METRIC {METRIC}="):
                    raise ProTokError("Exactly one primary stdout METRIC is required")
                value = float(lines[0].split("=", 1)[1])
                root = Path(self._config()["records_root"]) / identity
                state, summary = (
                    _read(root / "state.json"),
                    _read(root / "summary.json"),
                )
                frozen = _read(root / "run-config.json")
                expected = _read(output)
                if (
                    not math.isfinite(value)
                    or state.get("status") != "complete"
                    or state.get("fingerprint") != identity
                    or summary.get("fingerprint") != identity
                    or summary.get("protocol") != PROTOCOL
                    or summary.get("objective") != METRIC
                    or summary.get("quality_score_eligible") is not True
                    or not _finite(summary.get("value"))
                    or not math.isclose(
                        value, summary["value"], rel_tol=1e-10, abs_tol=1e-12
                    )
                    or not self._receipt_config_matches(frozen, expected)
                ):
                    raise ProTokError(
                        "Summary/frozen config does not bind the measured candidate"
                    )
                after = self.prepare_config(workspace, run / "post-config.json")
                if after["fingerprint"] != identity:
                    raise ProTokError("Candidate changed during evaluation")
                receipt.update(
                    value=summary["value"],
                    verified=True,
                    evidence=[
                        str(root / name)
                        for name in ("state.json", "summary.json", "run-config.json")
                    ],
                )
            except (ValueError, OSError, ProTokError) as error:
                receipt["error"] = str(error)
                _write(run / "receipt.json", receipt)
                raise ProTokError(f"{error}; receipt={receipt['receipt']}") from error
            _write(run / "receipt.json", receipt)
            return receipt
