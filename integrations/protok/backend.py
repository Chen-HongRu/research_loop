"""Bounded bridge to the protected ProTok evaluator; never imports candidate code."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import re
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


class ProTokBudgetError(ProTokError):
    """A known resource allowance cannot fit a new full formal allocation."""


def allocation_record(fingerprint, fresh_attempt=None):
    if fresh_attempt is None:
        return fingerprint
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", fresh_attempt):
        raise ProTokError("Invalid fresh formal allocation identity")
    return fingerprint + "--" + fresh_attempt


def _require_fresh_allocation(state, fingerprint, name):
    if name == fingerprint:
        return
    fresh = name.removeprefix(fingerprint + "--")
    attempts = state.get("attempts", [])
    if (
        state.get("fresh_attempt") != fresh
        or state.get("baseline") is not False
        or state.get("imported_baseline")
        or state.get("mode") != "scored"
        or len(attempts) != 1
    ):
        raise ProTokError("Fresh formal result has no distinct full allocation")
    attempt = attempts[0]
    if (
        attempt.get("seed") != 42
        or attempt.get("mode") != "evaluate"
        or attempt.get("budget_seconds") != 1800
        or attempt.get("include_smoke") is not True
        or attempt.get("terminal_state") != "COMPLETED"
        or attempt.get("exit_code") != "0:0"
        or not re.fullmatch(r"[1-9][0-9]*", str(attempt.get("owned_job_id", "")))
    ):
        raise ProTokError("Fresh formal result cannot reuse a smoke or imported score")


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
        debug_used = 0
        debug_outstanding = []
        for path in sorted((self.control / "debug").glob("*/debug-budget.json")):
            budget = _read(path)
            seconds = budget.get("used_gpu_seconds")
            if type(seconds) is not int or seconds < 0:
                raise ProTokError("Invalid debug accounting; preserve the ledger")
            debug_used += seconds
            if budget.get("active_attempt") is not None:
                debug_outstanding.append(str(path))
        from integrations.protok.research_execution import research_usage

        research = research_usage(self.control)
        from integrations.protok.long_training import usage as long_usage

        long_training = long_usage(self.control)
        return dict(
            project=str(self.project),
            protocol=PROTOCOL,
            metric=METRIC,
            session=session,
            record_count=len(states),
            records=receipts,
            scored_records=sum(s.get("mode") == "scored" for _, s in states),
            formal_gpu_seconds=used,
            research_gpu_seconds=research["gpu_seconds"],
            research_outstanding=research["outstanding"],
            research_overruns=research["overruns"],
            used_gpu_seconds=used + research["gpu_seconds"],
            debug_gpu_seconds=debug_used,
            long_training_gpu_seconds=long_training["gpu_seconds"],
            long_training_outstanding=long_training["outstanding"],
            total_gpu_seconds=used
            + research["gpu_seconds"]
            + debug_used
            + long_training["gpu_seconds"],
            debug_outstanding=debug_outstanding,
            outstanding=outstanding,
            off=(self.project / ".autoresearch-off").exists(),
            continuation=continuation,
        )

    def _command(self, config_path, operation, *, recover=False, fresh_attempt=None):
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
                str(Path(__file__).with_name("continuation.py").resolve()),
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
                *(["--fresh-attempt", fresh_attempt] if fresh_attempt else []),
            ]
        if fresh_attempt is not None:
            raise ProTokError("Fresh formal allocations require continuation control")
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

    def _gate(
        self,
        identity,
        *,
        recover=False,
        preparing=False,
        config_path=None,
        fresh_attempt=None,
    ):
        config, now = self._config(), time.time()
        from .round_admission import active_round

        admitted_round = active_round(self.control.parent, owner_token=self.owner_token)
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
            and (
                (recover and continuation)
                or admitted_round
                or now < lease["expires_at"]
            )
            and lease.get("previous_owner")
            and lease.get("evidence")
        ):
            raise ProTokError("Acknowledged, unexpired owner lease required")
        status = self.inspect()
        if status.get("debug_outstanding"):
            raise ProTokError("Outstanding debug allocation must be reconciled first")
        if (not recover or not continuation) and (self.control / "STOP").exists():
            raise ProTokError("Continuation is paused")
        if not config.get("launch_enabled") or (not continuation and status["off"]):
            raise ProTokError("Canonical study is off or launch disabled")
        control = None
        if continuation and not (recover and preparing):
            from .continuation import validate_continuation

            try:
                _, _, control = validate_continuation(
                    self.project,
                    self.control,
                    self.owner_token,
                    config_path or self.project / "autoresearch-run.json",
                    recover=recover,
                    **({"fresh_attempt": fresh_attempt} if fresh_attempt else {}),
                )
            except (ValueError, RuntimeError, OSError) as error:
                raise ProTokError(f"Continuation admission failed: {error}") from error
        if status.get("research_outstanding"):
            raise ProTokError(
                "Research allocation must settle before formal evaluation"
            )
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
            if not admitted_round and control["effective_deadline"] - now < bound:
                raise ProTokBudgetError("Continuation wall deadline exhausted")
            spent = status["used_gpu_seconds"] - control["initial_gpu_seconds"]
            if spent < 0:
                raise ProTokError(
                    "Historical GPU accounting decreased below its anchor"
                )
            if (
                spent + bound + config["reserve_gpu_seconds"]
                > control["additional_gpu_seconds"]
            ):
                raise ProTokBudgetError("Additional GPU budget exhausted")
            return
        session = status["session"]
        if (
            not session
            or not _finite(session.get("deadline"))
            or (
                not admitted_round
                and min(
                    session["deadline"],
                    control["effective_deadline"] if control else session["deadline"],
                )
                - now
                < bound
            )
        ):
            raise ProTokBudgetError("Original session wall deadline exhausted")
        if not continuation and status["scored_records"] >= config["max_evaluations"]:
            raise ProTokBudgetError("Original scored experiment budget exhausted")
        if (
            status["used_gpu_seconds"] + bound + config["reserve_gpu_seconds"]
            > config["gpu_budget_seconds"]
        ):
            raise ProTokBudgetError("Original GPU budget exhausted")

    def formal_capacity(self, config_path, deadline):
        config = self._config()
        if not _finite(deadline):
            raise ProTokError("Formal capacity requires a finite deadline")
        if deadline - time.time() < 1800 * len(config["seeds"]):
            return {
                "admitted": False,
                "reason": "Full formal allocation does not fit the wall deadline",
            }
        try:
            self._gate(None, config_path=config_path)
        except ProTokBudgetError as error:
            return {"admitted": False, "reason": str(error)}
        return {"admitted": True, "reason": "Full formal allowance remains available"}

    def debug_smoke(
        self, workspace, config_path, debug_root, attempt_id, deadline, *, recover=False
    ):
        """Run a separately accounted smoke; it can never return a quality score."""
        config_path, workspace, debug_root = (
            Path(config_path),
            Path(workspace).resolve(),
            Path(debug_root).resolve(),
        )
        if _read(config_path).get("workspace") != str(workspace):
            raise ProTokError("Debug configuration must bind the frozen source")
        prepared = subprocess.run(
            self._command(config_path, "prepare"),
            capture_output=True,
            text=True,
            cwd=self.control,
            timeout=120,
        )
        if prepared.returncode:
            raise ProTokError("Debug source/configuration validation failed")
        identity = json.loads(prepared.stdout)["fingerprint"]
        logs = debug_root / "processes" / attempt_id / uuid.uuid4().hex
        logs.mkdir(parents=True, exist_ok=False)
        argv = [
            "uv",
            "run",
            "--no-project",
            "--python",
            "3.12",
            "python",
            "-B",
            "-I",
            str(Path(__file__).with_name("debug_backend.py")),
            "--project",
            str(self.project),
            "--control",
            str(self.control),
            "--owner-token",
            self.owner_token or "",
            "--config",
            str(config_path),
            "--debug-root",
            str(debug_root),
            "--attempt-id",
            attempt_id,
            "--deadline",
            str(deadline),
            *(["--recover"] if recover else []),
        ]
        from .round_admission import execution_deadline

        wall_deadline = execution_deadline(
            self.control.parent,
            deadline,
            3600,
            owner_token=self.owner_token,
            attempt=f"debug/{debug_root.name}/{attempt_id}",
        )
        with (
            (logs / "stdout.log").open("w") as stdout,
            (logs / "stderr.log").open("w") as stderr,
        ):
            process = subprocess.Popen(
                argv, cwd=self.control, stdout=stdout, stderr=stderr
            )
            try:
                while process.poll() is None:
                    if not recover and (
                        (self.control / "STOP").exists() or time.time() >= wall_deadline
                    ):
                        process.terminate()
                        try:
                            process.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        break
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        pass
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
        try:
            result = json.loads((logs / "stdout.log").read_text().splitlines()[-1])
        except (OSError, ValueError, IndexError) as error:
            raise ProTokError(
                f"Debug process has no completion receipt; inspect {logs}"
            ) from error
        if (
            result.get("purpose") != "debug"
            or result.get("quality_score_eligible") is not False
            or (result.get("fingerprint") not in (None, identity))
            or "value" in result
            or "metric" in result
        ):
            raise ProTokError(
                "Debug receipt cannot be accepted as a formal measurement"
            )
        return result

    def _settled_failure_state(
        self, workspace, config_path, fingerprint, *, record_name=None
    ):
        """Read source-bound, owned failure evidence without allocating work."""
        config = self._config()
        lease = _read(self.control / "ownership.json")
        if not (
            self.owner_token
            and lease.get("owner_token") == self.owner_token
            and lease.get("project") == str(self.project)
            and lease.get("handoff_acknowledged") is True
        ):
            raise ProTokError(
                "Matching acknowledged owner required for failure recovery"
            )
        workspace, config_path = Path(workspace).resolve(), Path(config_path).resolve()
        frozen = _read(config_path)
        if frozen.get("workspace") != str(
            workspace
        ) or not self._receipt_config_matches(
            frozen, config | {"workspace": str(workspace)}
        ):
            raise ProTokError("Failed source configuration no longer matches")
        name = record_name or fingerprint
        if name != fingerprint and not re.fullmatch(
            re.escape(fingerprint) + r"--[A-Za-z0-9_-]{1,64}", name
        ):
            raise ProTokError("Invalid failed allocation identity")
        prepared = subprocess.run(
            self._command(config_path, "prepare"),
            capture_output=True,
            text=True,
            cwd=self.control,
            timeout=120,
        )
        if (
            prepared.returncode
            or json.loads(prepared.stdout).get("fingerprint") != fingerprint
        ):
            raise ProTokError("Failed source does not match its frozen identity")
        record = Path(config["records_root"]) / name
        state = _read(record / "state.json")
        if (
            state.get("status") != "failed"
            or state.get("fingerprint") != fingerprint
            or state.get("mode") != "scored"
            or _outstanding(state)
            or not self._receipt_config_matches(
                _read(record / "run-config.json"), frozen
            )
            or self.inspect()["outstanding"]
            or not state.get("attempts")
        ):
            raise ProTokError("Failure has unknown or unsettled external effects")
        if (self.control / "continuation.json").exists() and state.get(
            "controller_lease", {}
        ).get("owner_token") != self.owner_token:
            raise ProTokError("Failed allocation belongs to another owner")
        if name != fingerprint and (
            state.get("fresh_attempt") != name.removeprefix(fingerprint + "--")
            or state.get("baseline") is not False
        ):
            raise ProTokError("Failed formal attempt identity differs")
        attempt = state["attempts"][-1]
        if not re.fullmatch(r"[1-9][0-9]*", str(attempt.get("owned_job_id", ""))):
            raise ProTokError("Failed allocation has no owned job identity")
        return name, record, state

    def timed_out_receipt(
        self, workspace, config_path, fingerprint, *, record_name=None
    ):
        """Read a settled formal TIMEOUT as neutral evidence, never a score.

        STOP and lease expiry do not prevent reading already settled accounting.
        The pinned prepare helper validates source/configuration without evaluation.
        """
        name, record, state = self._settled_failure_state(
            workspace, config_path, fingerprint, record_name=record_name
        )
        status = self.inspect()
        if status["debug_outstanding"] or status["research_outstanding"]:
            raise ProTokError("Timeout has outstanding debug or research allocations")
        if state.get("baseline") is not False or state.get("imported_baseline"):
            raise ProTokError("Timeout must belong to a non-baseline formal allocation")
        if name == fingerprint and state.get("fresh_attempt") is not None:
            raise ProTokError("Failed formal attempt identity differs")
        attempts = state["attempts"]
        if any(
            a.get("seed") != 42
            or a.get("mode") != "evaluate"
            or a.get("budget_seconds") != 1800
            or a.get("include_smoke") is not True
            or a.get("status") not in {"complete", "failed"}
            or not re.fullmatch(r"[1-9][0-9]*", str(a.get("owned_job_id", "")))
            or not isinstance(a.get("exit_code"), str)
            or not re.fullmatch(r"[0-9]+:[0-9]+", a["exit_code"])
            for a in attempts
        ):
            raise ProTokError("Timeout has no settled full formal allocation identity")
        attempt = attempts[-1]
        if attempt.get("terminal_state") != "TIMEOUT":
            raise ProTokError("Failure is not a settled allocation TIMEOUT")
        evidence = [str(record / n) for n in ("state.json", "run-config.json")]
        directory = attempt.get("directory", "")
        if re.fullmatch(r"[A-Za-z0-9_-]+", directory):
            log = record / directory / "run.log.tail"
            if log.is_file() and not log.is_symlink():
                evidence.append(str(log))
        return {
            "purpose": "formal",
            "status": "failed",
            "failure_kind": "allocation_timeout",
            "fingerprint": fingerprint,
            "record_name": name,
            "job_id": attempt["owned_job_id"],
            "terminal_state": "TIMEOUT",
            "exit_code": attempt["exit_code"],
            "gpu_seconds": sum(a["gpu_seconds"] for a in attempts),
            "quality_score_eligible": False,
            "cause": "Slurm ended the formal allocation at its scheduling time limit (TIMEOUT).",
            "evidence": evidence,
        }

    def settled_failure(self, workspace, config_path, fingerprint, *, record_name=None):
        """Certify a candidate-code rejection only after its allocation is settled."""
        name, record, state = self._settled_failure_state(
            workspace, config_path, fingerprint, record_name=record_name
        )
        attempt = state["attempts"][-1]
        if attempt.get("terminal_state") not in {"FAILED", "OUT_OF_MEMORY"}:
            raise ProTokError("Failure is not a settled candidate execution rejection")
        directory = attempt.get("directory", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", directory):
            raise ProTokError("Invalid failed attempt log location")
        log = record / directory / "run.log.tail"
        from .debug_backend import failure_detail

        detail = failure_detail(log, attempt, state.get("error"))
        if detail["failure_kind"] != "candidate":
            raise ProTokError(
                "Failure cause is not certified as a candidate-code error"
            )
        cause = detail["cause"]
        return {
            "kind": "candidate_rejected",
            "cause": cause,
            "fingerprint": fingerprint,
            "record_name": name,
            "job_id": attempt["owned_job_id"],
            "terminal_state": attempt["terminal_state"],
            "exit_code": attempt["exit_code"],
            "gpu_seconds": sum(
                a["gpu_seconds"]
                for a in state["attempts"]
                if a.get("status") != "prepared"
            ),
            "quality_score_eligible": False,
            "purpose": "formal",
            "evidence": [str(record / n) for n in ("state.json", "run-config.json")]
            + [str(log)],
        }

    def completed_receipt(
        self, workspace: Path, config_path: Path, fingerprint: str, *, record_name=None
    ):
        """Materialize completed evidence without evaluating or allocating anything.

        STOP and exhausted allocation budgets do not prevent reading a receipt.
        Ownership, the frozen configuration, source identity and settled accounting
        remain mandatory. Only the protected source/config helper is executed.
        """
        config = self._config()
        lease = _read(self.control / "ownership.json")
        if not (
            lease.get("schema") == 1
            and self.owner_token
            and lease.get("owner_token") == self.owner_token
            and lease.get("project") == str(self.project)
            and lease.get("handoff_acknowledged") is True
            and _finite(lease.get("acknowledged_at"))
            and lease["acknowledged_at"] <= time.time()
            and lease.get("previous_owner")
            and lease.get("evidence")
        ):
            raise ProTokError("Acknowledged matching owner required for receipt repair")
        workspace, config_path = Path(workspace).resolve(), Path(config_path).resolve()
        frozen = _read(config_path)
        if frozen.get("workspace") != str(
            workspace
        ) or not self._receipt_config_matches(
            frozen, config | {"workspace": str(workspace)}
        ):
            raise ProTokError("Frozen job configuration no longer matches the protocol")
        for name in CANDIDATE_FILES:
            path = workspace / name
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size > 4 * 1024 * 1024
                or any(
                    p.is_symlink()
                    for p in path.parents
                    if p != workspace and p.is_relative_to(workspace)
                )
            ):
                raise ProTokError(
                    "Frozen candidate must be a small regular source file"
                )
        validated = subprocess.run(
            self._command(config_path, "prepare"),
            capture_output=True,
            text=True,
            cwd=self.control,
            timeout=120,
        )
        if (
            validated.returncode
            or json.loads(validated.stdout).get("fingerprint") != fingerprint
        ):
            raise ProTokError(
                "Completed receipt does not match frozen job source identity"
            )
        if record_name is not None and not (
            record_name == fingerprint
            or re.fullmatch(
                re.escape(fingerprint) + r"--[A-Za-z0-9_-]{1,64}", record_name
            )
        ):
            raise ProTokError("Invalid retained allocation record identity")
        name = record_name or fingerprint
        root = Path(config["records_root"]) / name
        state = _read(root / "state.json")
        summary = _read(root / "summary.json")
        _require_fresh_allocation(state, fingerprint, name)
        if (
            state.get("status") != "complete"
            or _outstanding(state)
            or state.get("fingerprint") != fingerprint
            or summary.get("fingerprint") != fingerprint
            or summary.get("protocol") != PROTOCOL
            or summary.get("objective") != METRIC
            or summary.get("quality_score_eligible") is not True
            or not _finite(summary.get("value"))
            or not self._receipt_config_matches(_read(root / "run-config.json"), frozen)
            or self.inspect()["outstanding"]
        ):
            raise ProTokError("No complete, settled, candidate-bound backend receipt")
        # A committed evaluator summary cannot erase an observed wrapper failure.
        # These are small local process receipts, never dataset/cache contents.
        for path in self.control.glob("evaluation-*/receipt.json"):
            previous = _read(path)
            if (
                previous.get("fingerprint") == fingerprint
                and previous.get("record_name", fingerprint) == name
                and (
                    previous.get("error")
                    or previous.get("timed_out")
                    or previous.get("cancelled")
                    or ("exit_code" in previous and previous["exit_code"] != 0)
                )
            ):
                raise ProTokError(
                    f"Known failed evaluator wrapper receipt blocks reconciliation: {path}"
                )
        return {
            "workspace": str(workspace),
            "config_path": str(config_path),
            "fingerprint": fingerprint,
            "record_name": name,
            "metric": METRIC,
            "value": summary["value"],
            "verified": True,
            "evidence": [
                str(root / name)
                for name in ("state.json", "summary.json", "run-config.json")
            ],
            "provenance": "authoritative-record-reconciliation",
        }

    def evaluate(
        self,
        workspace: Path,
        output: Path,
        timeout_seconds: float,
        *,
        recover: bool = False,
        cancel_event=None,
        fresh_attempt=None,
    ) -> dict:
        """Run once, preserving actual process status and fingerprint-bound evidence."""
        if not _finite(timeout_seconds) or timeout_seconds <= 0:
            raise ProTokError("A finite positive timeout is required")
        if fresh_attempt is not None:
            allocation_record("candidate", fresh_attempt)

        def cancelled():
            return cancel_event is not None and cancel_event.is_set()

        if cancelled():
            raise ProTokError("Evaluation cancelled before launch")
        self.control.mkdir(parents=True, exist_ok=True)
        with (self.control / "protok.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if recover:
                # A settled backend record needs no evaluator process, including
                # when publication was interrupted after its allocation ended.
                prepared = self.prepare_config(workspace, output)
                record = (
                    Path(self._config()["records_root"])
                    / allocation_record(prepared["fingerprint"], fresh_attempt)
                    / "state.json"
                )
                if record.exists() and _read(record).get("status") == "complete":
                    return self.completed_receipt(
                        workspace,
                        output,
                        prepared["fingerprint"],
                        **(
                            {
                                "record_name": allocation_record(
                                    prepared["fingerprint"], fresh_attempt
                                )
                            }
                            if fresh_attempt
                            else {}
                        ),
                    )
            # Gate before preparing or invoking the protected evaluator.
            self._gate(
                None, recover=recover, preparing=True, fresh_attempt=fresh_attempt
            )
            prepared = self.prepare_config(workspace, output)
            identity = prepared["fingerprint"]
            self._gate(
                identity,
                recover=recover,
                config_path=output,
                fresh_attempt=fresh_attempt,
            )
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
                record_name=allocation_record(identity, fresh_attempt),
                fresh_attempt=fresh_attempt,
            )
            with (
                (run / "stdout.log").open("w") as stdout,
                (run / "stderr.log").open("w") as stderr,
            ):
                command = self._command(
                    output,
                    "evaluate",
                    recover=recover,
                    **({"fresh_attempt": fresh_attempt} if fresh_attempt else {}),
                )
                from .round_admission import active_round, execution_deadline

                admitted = active_round(
                    self.control.parent, owner_token=self.owner_token
                )
                if admitted:
                    window = execution_deadline(
                        self.control.parent,
                        admitted["cutoff"],
                        timeout_seconds,
                        owner_token=self.owner_token,
                        attempt=f"formal/{allocation_record(identity, fresh_attempt)}",
                    )
                    command += ["--execution-deadline", str(window)]
                    if not recover:
                        timeout_seconds = min(
                            timeout_seconds, max(0.01, window - time.time())
                        )
                process = subprocess.Popen(
                    command,
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
                root = Path(self._config()["records_root"]) / allocation_record(
                    identity, fresh_attempt
                )
                state, summary = (
                    _read(root / "state.json"),
                    _read(root / "summary.json"),
                )
                _require_fresh_allocation(
                    state, identity, allocation_record(identity, fresh_attempt)
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
