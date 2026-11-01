"""Protected, local Meta Loop version and promotion controller.

The controller is invoked by a trusted runner. Candidate code runs in a separate
process, never in this module. Its sibling directory must not be made available
to candidate processes; read-only modes alone are not an OS security boundary.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import tempfile
import time
from contextlib import contextmanager

from .strategy import validate_strategy

CONFIRMATION_CHECKS = {
    "control",
    "frontier",
    "multi-route",
    "incomplete-execution",
    "evidence-conflict",
    "human-edit",
    "version-freeze",
    "permissions",
    "read-isolation",
    "evidence",
    "publication",
    "recovery",
    "budget",
}


class MetaError(ValueError):
    """A frozen contract, budget or state transition was not satisfied."""


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
        temporary = stream.name
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def _number(value, name: str, *, positive=False, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MetaError(f"{name}: finite numeric telemetry required")
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise MetaError(
            f"{name}: finite {'positive' if positive else 'nonnegative'} value required"
        )
    if integer and not isinstance(value, int):
        raise MetaError(f"{name}: integer required")
    return value


def _identity(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", value
    ):
        raise MetaError("Invalid record identity")
    return value


def _protocol(protocol: dict) -> dict:
    protocol = json.loads(json.dumps(protocol, allow_nan=False))
    required = {"id", "stage", "tasks", "repeats", "checks", "run_budget", "conditions"}
    if not isinstance(protocol, dict) or not required <= protocol.keys():
        raise MetaError(f"Protocol requires {sorted(required)}")
    _identity(protocol["id"])
    if protocol["stage"] not in {"development", "confirmation"}:
        raise MetaError("Protocol stage must be development or confirmation")
    if not isinstance(protocol["conditions"], dict) or not protocol["conditions"]:
        raise MetaError(
            "Fixed model, tools, training and environment conditions required"
        )
    tasks = protocol["tasks"]
    if not isinstance(tasks, list) or not tasks:
        raise MetaError("Protocol needs explicit tasks")
    for task in tasks:
        if (
            not isinstance(task, dict)
            or set(task) != {"id", "metric"}
            or task["metric"] not in {"auc", "rmse", "log_loss"}
        ):
            raise MetaError("Task requires id and auc/rmse/log_loss metric")
        _identity(task["id"])
    if len({t["id"] for t in tasks}) != len(tasks):
        raise MetaError("Duplicate task")
    _number(protocol["repeats"], "repeats", positive=True, integer=True)
    checks = protocol["checks"]
    if not isinstance(checks, list) or not checks or len(set(checks)) != len(checks):
        raise MetaError("Explicit unique protocol checks required")
    for check in checks:
        _identity(check)
    limits = protocol["run_budget"]
    if set(limits) != {"calls", "seconds", "tokens"}:
        raise MetaError("run_budget requires calls, seconds and tokens")
    for name, limit in limits.items():
        _number(limit, name, positive=True, integer=name != "seconds")
    if protocol["stage"] == "confirmation":
        if set(checks) != CONFIRMATION_CHECKS:
            raise MetaError("Confirmation requires the fixed complete protocol checks")
        conditions = protocol["conditions"]
        fixed = {
            "model",
            "reasoning",
            "tools",
            "training",
            "environment",
            "task_states",
            "seed_plan",
            "order",
            "resources",
        }
        if not fixed <= conditions.keys() or any(not conditions[key] for key in fixed):
            raise MetaError(
                "Confirmation requires complete frozen execution conditions"
            )
        if conditions.get("read_isolation") != "enforced":
            raise MetaError("Confirmation requires enforced cross-run read isolation")
        expected = {
            "denoising-dirty-documents": "rmse",
            "spooky-author-identification": "log_loss",
        }
        if {t["id"]: t["metric"] for t in tasks} != expected or protocol[
            "repeats"
        ] != 3:
            raise MetaError(
                "Confirmation requires the two canonical tasks and three repeats"
            )
    return protocol


class MetaStudy:
    """One immutable evaluation batch and its recoverable supervisor pointer."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.controller = self.root.parent / f".{self.root.name}.meta-controller"
        if not (self.controller / "state.json").is_file():
            raise MetaError("Meta Study is not initialized")

    @classmethod
    def initialize(
        cls,
        root: Path,
        source: Path,
        protocol: dict,
        baseline_policy: dict,
        budget: dict | None = None,
    ):
        root, source = Path(root).resolve(), Path(source).resolve()
        controller = root.parent / f".{root.name}.meta-controller"
        protocol = _protocol(protocol)
        baseline_policy = validate_strategy(baseline_policy)
        limits = {
            "runs": 64,
            "calls": 600,
            "seconds": 28800,
            "tokens": 40000000,
            "candidates": 3,
        }
        if budget:
            if not set(budget) <= limits.keys():
                raise MetaError("Unknown batch budget field")
            limits.update(budget)
        for name, limit in limits.items():
            _number(limit, name, positive=True, integer=name != "seconds")
        if controller.exists() or (root.exists() and any(root.iterdir())):
            raise MetaError("Meta Study root or controller already exists")
        if not (source / "src/research_loop/core.py").is_file():
            raise MetaError("Source must be a research-loop project root")
        root.mkdir(parents=True, exist_ok=True)
        controller.mkdir(mode=0o700)
        study = object.__new__(cls)
        study.root, study.controller = root, controller
        version = study._snapshot(source, baseline_policy)
        state = {
            "schema": 1,
            "a0_ref": version["ref"],
            "supervisor_ref": version["ref"],
            "protocol_ref": _digest(protocol),
            "budget": limits,
            "jobs": {},
            "candidates": {},
            "history": [],
            "confirmation_candidate": None,
            "confirmation_consumed": False,
            "revoked": [],
            "deadline_at": time.time() + limits["seconds"],
        }
        _write(controller / "protocol.json", protocol)
        _write(controller / "state.json", state)
        _write(
            root / "meta.json",
            {k: state[k] for k in ("a0_ref", "supervisor_ref", "protocol_ref")},
        )
        return cls(root)

    @contextmanager
    def _locked(self):
        with (self.controller / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = _json(self.controller / "state.json")
            self._recover(state)
            yield state

    def _save(self, state):
        _write(self.controller / "state.json", state)
        _write(
            self.root / "meta.json",
            {k: state[k] for k in ("a0_ref", "supervisor_ref", "protocol_ref")},
        )

    def _recover(self, state):
        pending = self.controller / "pending.json"
        if not pending.exists():
            return
        intent = _json(pending)
        if state["supervisor_ref"] not in {intent["old_ref"], intent["new_ref"]}:
            raise MetaError(
                "Pointer differs from both pending transition refs; reconciliation required"
            )
        state["supervisor_ref"] = intent["new_ref"]
        if intent["id"] not in state["history"]:
            state["history"].append(intent["id"])
        if intent["kind"] == "promotion":
            state["candidates"][intent["candidate_id"]]["status"] = "promoted"
            state["candidates"][intent["candidate_id"]]["decision"] = intent["id"]
        elif intent["old_ref"] not in state["revoked"]:
            state["revoked"].append(intent["old_ref"])
        self._save(state)
        _write(self.root / "decisions" / f"{intent['id']}.json", intent)
        pending.unlink()

    def _snapshot(self, source: Path, policy: dict, parent_ref: str | None = None):
        versions = self.controller / "versions"
        versions.mkdir(exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="freeze-", dir=self.controller))
        try:
            for filename in ("pyproject.toml", "uv.lock"):
                if not (source / filename).is_file():
                    raise MetaError(f"Missing frozen dependency file: {filename}")
                shutil.copyfile(source / filename, temporary / filename)
            target = temporary / "src/research_loop"
            target.mkdir(parents=True)
            for path in sorted((source / "src/research_loop").glob("*.py")):
                if path.is_symlink():
                    raise MetaError("Frozen source cannot contain symlinks")
                shutil.copyfile(path, target / path.name)
            _write(temporary / "strategy.json", policy)
            env = {
                **os.environ,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
            }

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", str(temporary), *args],
                    env=env,
                    stderr=subprocess.PIPE,
                    text=True,
                ).strip()

            git("init", "-q")
            git("add", "--", "src", "pyproject.toml", "uv.lock", "strategy.json")
            git(
                "-c",
                "user.name=Meta Controller",
                "-c",
                "user.email=meta@localhost",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "Frozen policy version",
            )
            ref = git("rev-parse", "HEAD")
            destination = versions / ref
            if destination.exists():
                shutil.rmtree(temporary)
            else:
                temporary.rename(destination)
                for path in destination.rglob("*"):
                    path.chmod(0o555 if path.is_dir() else 0o444)
                destination.chmod(0o555)
            record = {
                "ref": ref,
                "path": str(destination),
                "policy": policy,
                "parent_ref": parent_ref,
            }
            records = self.controller / "version-records"
            if not (records / f"{ref}.json").exists():
                _write(records / f"{ref}.json", record)
            return record
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def version(self, ref: str):
        if not re.fullmatch(r"[0-9a-f]{40}", ref):
            raise MetaError("Exact Git commit required")
        path = self.controller / "version-records" / f"{ref}.json"
        if not path.is_file():
            raise MetaError("Unknown version")
        return _json(path)

    def status(self):
        with self._locked() as state:
            return {
                **state,
                "root": str(self.root),
                "controller_root": str(self.controller),
            }

    def freeze_candidate(self, policy: dict, parent_ref: str, candidate_id: str):
        policy, candidate_id = validate_strategy(policy), _identity(candidate_id)
        with self._locked() as state:
            if parent_ref != state["supervisor_ref"] or parent_ref in state["revoked"]:
                raise MetaError(
                    "Candidate must derive from current non-revoked supervisor"
                )
            if state["confirmation_consumed"]:
                raise MetaError("Confirmation feedback exhausted this frozen batch")
            if candidate_id in state["candidates"]:
                raise MetaError("Candidate identity already frozen")
            if len(state["candidates"]) >= state["budget"]["candidates"]:
                raise MetaError("Candidate budget exhausted")
            parent = self.version(parent_ref)
            if policy == parent["policy"]:
                raise MetaError("Candidate must change the policy")
            version = self._snapshot(Path(parent["path"]), policy, parent_ref)
            record = {
                "id": candidate_id,
                "ref": version["ref"],
                "parent_ref": parent_ref,
                "protocol_ref": state["protocol_ref"],
                "policy": policy,
                "status": "frozen",
                "reports": [],
            }
            _write(
                self.controller / "candidates" / candidate_id / "candidate.json", record
            )
            _write(self.root / "candidates" / candidate_id / "candidate.json", record)
            state["candidates"][candidate_id] = record
            self._save(state)
            return record

    def reserve_job(
        self,
        job_id: str,
        version_ref: str,
        max_calls: int,
        max_seconds: float,
        max_tokens: int,
        *,
        evaluation: dict | None = None,
    ):
        job_id = _identity(job_id)
        reservation = {
            "version_ref": version_ref,
            "calls": max_calls,
            "seconds": max_seconds,
            "tokens": max_tokens,
        }
        self.version(version_ref)
        for name in ("calls", "seconds", "tokens"):
            _number(reservation[name], name, positive=True, integer=name != "seconds")
        with self._locked() as state:
            if state.get("halted"):
                raise MetaError("Budget breach requires reconciliation before new jobs")
            if time.time() >= state["deadline_at"]:
                raise MetaError("Batch wall-clock budget exhausted")
            if (
                sum(
                    job["status"] in {"reserved", "unknown"}
                    for job in state["jobs"].values()
                )
                >= 2
            ):
                raise MetaError("At most two simultaneous jobs may be reserved")
            if job_id in state["jobs"]:
                raise MetaError(
                    "Job already reserved; reconcile its existing receipt instead of relaunching"
                )
            if version_ref in state["revoked"]:
                raise MetaError("Version revoked")
            if evaluation is not None:
                self._evaluation_slot(state, version_ref, evaluation)
                if any(
                    job.get("evaluation") == evaluation
                    for job in state["jobs"].values()
                ):
                    raise MetaError(
                        "Evaluation slot already launched; repeated measurements cannot replace it"
                    )
                reservation["evaluation"] = dict(evaluation)
            if len(state["jobs"]) >= state["budget"]["runs"]:
                raise MetaError("Run budget exhausted")
            run_budget = _json(self.controller / "protocol.json")["run_budget"]
            for name in ("calls", "seconds", "tokens"):
                if reservation[name] > run_budget[name]:
                    raise MetaError(f"Per-run {name} budget exceeded")
                committed = sum(
                    job.get("receipt", job)[name] for job in state["jobs"].values()
                )
                if committed + reservation[name] > state["budget"][name]:
                    raise MetaError(f"Aggregate {name} budget exceeded")
            record = {**reservation, "status": "reserved"}
            state["jobs"][job_id] = record
            self._save(state)
            return record

    def _evaluation_slot(self, state, version_ref, evaluation):
        if not isinstance(evaluation, dict) or set(evaluation) != {
            "candidate_id",
            "stage",
            "arm",
            "task",
            "repeat",
        }:
            raise MetaError(
                "Evaluation reservation needs candidate, stage, arm, task and repeat"
            )
        candidate = state["candidates"].get(evaluation["candidate_id"])
        if candidate is None or candidate["parent_ref"] != state["supervisor_ref"]:
            raise MetaError(
                "Evaluation must bind the current frozen candidate and supervisor"
            )
        protocol = _json(self.controller / "protocol.json")
        stage = evaluation["stage"]
        if stage == "confirmation":
            if (
                protocol["stage"] != "confirmation"
                or candidate["status"] != "nominated"
                or state["confirmation_consumed"]
            ):
                raise MetaError("Confirmation launch requires the frozen nomination")
            tasks, repeats = protocol["tasks"], protocol["repeats"]
        elif stage == "development" and candidate["status"] in {
            "frozen",
            "dev_evaluated",
        }:
            tasks = (
                protocol["tasks"]
                if protocol["stage"] == "development"
                else [{"id": "aerial-cactus", "metric": "auc"}]
            )
            repeats = 1
        else:
            raise MetaError("Invalid evaluation launch stage")
        expected_ref = {"S": candidate["parent_ref"], "C": candidate["ref"]}
        if stage == "confirmation" and state["a0_ref"] != candidate["parent_ref"]:
            expected_ref["A0"] = state["a0_ref"]
        if expected_ref.get(evaluation["arm"]) != version_ref:
            raise MetaError("Evaluation arm differs from frozen version")
        if (
            evaluation["task"] not in {task["id"] for task in tasks}
            or type(evaluation["repeat"]) is not int
            or not 0 <= evaluation["repeat"] < repeats
        ):
            raise MetaError("Evaluation task/repeat outside frozen protocol")

    def finish_job(self, job_id: str, receipt: dict):
        with self._locked() as state:
            job = state["jobs"].get(job_id)
            if job is None:
                raise MetaError("Job was not reserved")
            if "receipt" in job:
                if job["receipt"] != receipt:
                    raise MetaError("Receipt is immutable")
                return job
            if set(receipt) != {"status", "calls", "seconds", "tokens"} or receipt[
                "status"
            ] not in {"completed", "failed", "unknown"}:
                raise MetaError("Receipt requires status and complete usage")
            exceeded = []
            for name in ("calls", "seconds", "tokens"):
                _number(receipt[name], name, integer=name != "seconds")
                if receipt[name] > job[name]:
                    exceeded.append(name)
            if exceeded:
                job["budget_exceeded"] = exceeded
                state["halted"] = {
                    "job_id": job_id,
                    "reason": "reservation_exceeded",
                    "fields": exceeded,
                }
            if receipt["status"] == "unknown":
                # Unknown effects keep the complete reservation charged.
                job["status"] = "unknown"
            else:
                job["receipt"] = receipt
                job["status"] = receipt["status"]
            self._save(state)
            return job

    def nominate(self, candidate_id: str):
        with self._locked() as state:
            candidate = state["candidates"].get(candidate_id)
            if candidate is None or candidate["status"] != "dev_evaluated":
                raise MetaError("Nomination requires a development-evaluated candidate")
            if state["confirmation_candidate"] is not None:
                raise MetaError("Only one confirmation nomination per batch")
            if candidate["parent_ref"] != state["supervisor_ref"]:
                raise MetaError("Supervisor changed before nomination")
            state["confirmation_candidate"] = candidate_id
            candidate["status"] = "nominated"
            self._save(state)
            return candidate

    def _validate_report(self, state, candidate, report):
        protocol = _json(self.controller / "protocol.json")
        stage = report.get("stage")
        if stage not in {"development", "confirmation"}:
            raise MetaError("Missing evaluation stage")
        for key, expected in {
            "protocol_ref": state["protocol_ref"],
            "supervisor_ref": candidate["parent_ref"],
            "candidate_ref": candidate["ref"],
        }.items():
            if report.get(key) != expected:
                raise MetaError(f"Report {key} mismatch")
        if stage == "confirmation" and protocol["stage"] != "confirmation":
            raise MetaError("Development-only protocol cannot confirm")
        tasks = (
            protocol["tasks"]
            if stage == "confirmation" or protocol["stage"] == "development"
            else [{"id": "aerial-cactus", "metric": "auc"}]
        )
        repeats = protocol["repeats"] if stage == "confirmation" else 1
        arms = {"S": candidate["parent_ref"], "C": candidate["ref"]}
        if stage == "confirmation" and state["a0_ref"] != candidate["parent_ref"]:
            arms["A0"] = state["a0_ref"]
        expected = {
            (arm, t["id"], repeat)
            for arm in arms
            for t in tasks
            for repeat in range(repeats)
        }
        seen, job_ids = set(), set()
        rows = report.get("rows")
        if not isinstance(rows, list):
            raise MetaError("Complete paired rows required")
        for row in rows:
            if not isinstance(row, dict) or type(row.get("repeat")) is not int:
                raise MetaError("Run repeat must be an integer")
            key = (row.get("arm"), row.get("task"), row.get("repeat"))
            if key not in expected or key in seen:
                raise MetaError(
                    "Duplicate or unexpected run; samples cannot be dropped"
                )
            seen.add(key)
            if (
                row.get("pair_id") != f"{key[1]}:{key[2]}"
                or row.get("version_ref") != arms[key[0]]
            ):
                raise MetaError("Pair identity or frozen version mismatch")
            if row.get("valid") is not True:
                raise MetaError("All paired runs must validly complete")
            for name in (
                "overhead_seconds",
                "total_seconds",
                "tokens",
                "calls",
                "quality",
            ):
                _number(
                    row.get(name),
                    name,
                    positive=name in {"total_seconds", "overhead_seconds", "tokens"},
                    integer=name in {"tokens", "calls"},
                )
            if row["overhead_seconds"] > row["total_seconds"]:
                raise MetaError("Overhead exceeds complete run time")
            metric = next(task["metric"] for task in tasks if task["id"] == row["task"])
            if metric == "auc" and row["quality"] > 1:
                raise MetaError("AUC must be between zero and one")
            job_id = row.get("job_id")
            if not isinstance(job_id, str) or job_id in job_ids:
                raise MetaError("Unique reserved job required per row")
            job_ids.add(job_id)
            job = state["jobs"].get(job_id, {})
            slot = {
                "candidate_id": candidate["id"],
                "stage": stage,
                "arm": row["arm"],
                "task": row["task"],
                "repeat": row["repeat"],
            }
            if (stage == "confirmation" or "evaluation" in job) and job.get(
                "evaluation"
            ) != slot:
                raise MetaError(
                    "Job task/repeat scope differs from frozen evaluation slot"
                )
            receipt = job.get("receipt", {})
            if (
                job.get("version_ref") != row["version_ref"]
                or receipt.get("status") != "completed"
                or job.get("budget_exceeded")
            ):
                raise MetaError("Completed trusted job receipt required")
            if any(
                receipt.get(field) != row[key]
                for field, key in {
                    "calls": "calls",
                    "seconds": "total_seconds",
                    "tokens": "tokens",
                }.items()
            ):
                raise MetaError("Report usage differs from reserved job receipt")
            for field, value in {
                "calls": row["calls"],
                "seconds": row["total_seconds"],
                "tokens": row["tokens"],
            }.items():
                if value > protocol["run_budget"][field]:
                    raise MetaError("Absolute per-run budget exceeded")
        if seen != expected:
            raise MetaError("Missing paired samples; incomplete evidence")
        checks = report.get("protocol_checks")
        if (
            not isinstance(checks, dict)
            or set(checks) != set(protocol["checks"])
            or any(v is not True for v in checks.values())
        ):
            raise MetaError("Required protocol checks failed or missing")
        return protocol, tasks, arms

    def record_evaluation(self, candidate_id: str, report: dict):
        # Raw confirmation data stays entirely inside the protected controller.
        report = json.loads(json.dumps(report, allow_nan=False))
        with self._locked() as state:
            candidate = state["candidates"].get(candidate_id)
            if candidate is None or candidate["status"] in {
                "promoted",
                "rejected",
                "inconclusive",
                "confirmed",
            }:
                raise MetaError("Candidate no longer accepts evidence")
            stage = report.get("stage")
            if stage == "confirmation" and (
                candidate["status"] != "nominated" or state["confirmation_consumed"]
            ):
                raise MetaError("Confirmation requires the single nominated candidate")
            if stage == "development" and candidate["status"] not in {
                "frozen",
                "dev_evaluated",
            }:
                raise MetaError("Development feedback cannot follow nomination")
            self._validate_report(state, candidate, report)
            report_id = f"{len(candidate['reports']) + 1:04d}"
            path = self.controller / "evaluations" / candidate_id / f"{report_id}.json"
            _write(path, report)
            candidate["reports"].append(
                {"id": report_id, "stage": stage, "path": str(path)}
            )
            if stage == "development":
                candidate["status"] = "dev_evaluated"
                _write(
                    self.root
                    / "candidates"
                    / candidate_id
                    / f"development-{report_id}.json",
                    report,
                )
            else:
                candidate["status"] = "confirmed"
                state["confirmation_consumed"] = True
            self._save(state)
            return {
                "candidate_id": candidate_id,
                "stage": stage,
                "receipt": report_id,
                "status": candidate["status"],
            }

    def _gates(self, state, candidate, report):
        protocol, tasks, arms = self._validate_report(state, candidate, report)
        if report["stage"] != "confirmation":
            return "inconclusive", ["development_only"]
        if report.get("canary") != {"S": True, "C": True}:
            return "rejected", ["canary"]
        rows = {(r["arm"], r["task"], r["repeat"]): r for r in report["rows"]}
        ratios = {name: [] for name in ("overhead_seconds", "total_seconds", "tokens")}
        failed = []
        for task in tasks:
            task_id, metric = task["id"], task["metric"]
            task_total = []
            for repeat in range(3):
                supervisor, challenger = (
                    rows["S", task_id, repeat],
                    rows["C", task_id, repeat],
                )
                for name in ratios:
                    ratio = challenger[name] / supervisor[name]
                    ratios[name].append(ratio)
                    if name == "total_seconds":
                        task_total.append(ratio)
            if statistics.median(task_total) > 1.05:
                failed.append("task_total_time")
            candidate_mean = statistics.mean(
                rows["C", task_id, i]["quality"] for i in range(3)
            )
            for arm in ("S", "A0" if "A0" in arms else "S"):
                baseline = statistics.mean(
                    rows[arm, task_id, i]["quality"] for i in range(3)
                )
                if (metric == "auc" and candidate_mean < baseline - 0.002) or (
                    metric != "auc" and candidate_mean > baseline * 1.01
                ):
                    failed.append("quality_" + arm)
        if (
            statistics.mean(ratios["overhead_seconds"]) > 0.80
            or sum(r < 1 for r in ratios["overhead_seconds"]) < 5
        ):
            failed.append("overhead")
        if statistics.mean(ratios["total_seconds"]) > 0.95:
            failed.append("total_time")
        if statistics.mean(ratios["tokens"]) > 1.05:
            failed.append("tokens")
        return ("rejected", sorted(set(failed))) if failed else ("promoted", [])

    def decide(self, candidate_id: str, *, publication: Path | None = None):
        with self._locked() as state:
            candidate = state["candidates"].get(candidate_id)
            if candidate is None:
                raise MetaError("Unknown candidate")
            if "decision" in candidate:
                return _json(
                    self.controller / "decisions" / f"{candidate['decision']}.json"
                )
            outcome, failures = "inconclusive", ["confirmation_missing"]
            if candidate["parent_ref"] != state["supervisor_ref"]:
                failures = ["supervisor_changed"]
            elif candidate["reports"]:
                report = _json(Path(candidate["reports"][-1]["path"]))
                try:
                    outcome, failures = self._gates(state, candidate, report)
                except MetaError:
                    outcome, failures = "inconclusive", ["incomplete_evidence"]
            if state.get("halted"):
                outcome, failures = "inconclusive", ["budget_breach"]
            elif time.time() >= state["deadline_at"]:
                outcome, failures = "inconclusive", ["budget_exhausted"]
            publication_record = None
            if outcome == "promoted":
                if any(
                    job["status"] in {"reserved", "unknown"}
                    for job in state["jobs"].values()
                ):
                    raise MetaError(
                        "In-flight or unknown jobs prevent version switching"
                    )
                if publication is None or not Path(publication).is_file():
                    raise MetaError("Completed outer publication manifest required")
                publication_record = _json(Path(publication))
                if publication_record.get("complete") is not True:
                    raise MetaError("Outer publication is incomplete")
                for key, expected in {
                    "supervisor_ref": candidate["parent_ref"],
                    "candidate_ref": candidate["ref"],
                    "protocol_ref": state["protocol_ref"],
                }.items():
                    if publication_record.get(key) != expected:
                        raise MetaError(f"Outer publication {key} mismatch")
            decision_id = f"decision-{candidate_id}"
            decision = {
                "id": decision_id,
                "candidate_id": candidate_id,
                "outcome": outcome,
                "failure_categories": failures,
                "old_ref": candidate["parent_ref"],
                "new_ref": candidate["ref"]
                if outcome == "promoted"
                else state["supervisor_ref"],
                "a0_ref": state["a0_ref"],
                "protocol_ref": state["protocol_ref"],
                "confirmation_receipt": candidate["reports"][-1]["id"]
                if candidate["reports"]
                else None,
                "publication": publication_record,
                "kind": "promotion",
            }
            _write(self.controller / "decisions" / f"{decision_id}.json", decision)
            candidate["decision"] = decision_id
            if outcome == "promoted":
                # Intent precedes the compare-and-swap pointer commit. Recovery
                # accepts only the recorded old/new pair, never a third pointer.
                _write(self.controller / "pending.json", decision)
                self._recover(state)
            else:
                candidate["status"] = outcome
                state["history"].append(decision_id)
                self._save(state)
            _write(self.root / "decisions" / f"{decision_id}.json", decision)
            return decision

    def rollback(self, reason: str, expected_ref: str):
        if not isinstance(reason, str) or not reason.strip():
            raise MetaError("An audited rollback reason is required")
        with self._locked() as state:
            if state["supervisor_ref"] != expected_ref:
                raise MetaError("Supervisor compare-and-swap failed")
            previous = None
            for identity in reversed(state["history"]):
                decision = _json(self.controller / "decisions" / f"{identity}.json")
                if (
                    decision["kind"] == "promotion"
                    and decision["new_ref"] == expected_ref
                    and decision["outcome"] == "promoted"
                ):
                    previous = decision["old_ref"]
                    break
            if previous is None or previous in state["revoked"]:
                previous = state["a0_ref"]
            if previous == expected_ref or previous in state["revoked"]:
                raise MetaError("No eligible prior supervisor; stop and reconcile")
            record = {
                "id": f"rollback-{len(state['history']) + 1:04d}",
                "kind": "rollback",
                "reason": reason,
                "old_ref": expected_ref,
                "new_ref": previous,
                "protocol_ref": state["protocol_ref"],
            }
            _write(self.controller / "decisions" / f"{record['id']}.json", record)
            _write(self.controller / "pending.json", record)
            self._recover(state)
            _write(self.root / "decisions" / f"{record['id']}.json", record)
            return record
