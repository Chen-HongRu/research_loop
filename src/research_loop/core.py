"""Persistent evidence-driven research with bounded selection and publication."""

from __future__ import annotations

import fcntl
import json
import os
import random
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .diagnostics import diagnostic_view
from .evaluation import build_frontier, validate_evaluation
from .long_training import (
    completed_results as long_training_results,
    guidance as long_training_guidance,
    nominations as long_training_nominations,
    observation_context as long_observation_context,
    validate_observation_reviews,
    stage_schema as long_training_schema,
    validate_policy as validate_long_training,
)
from .exploration import (
    allocation,
    guidance,
    route_candidates,
    stage_schema,
    validate_design,
    validate_exploration,
)
from .harness import (
    AgentHarness,
    HarnessConfig,
    HarnessError,
    RecoverableExecutionError,
    completed_response,
    make_harness,
    publish_completed_response,
)
from .protocol import SCHEMAS, prompt, validate_response
from .research_contract import validate_research_execution
from .strategy import debate_rounds, review_guidance, validate_strategy
from .tasks import TaskQueue, completed_results as task_results


class StudyError(RuntimeError):
    pass


class BudgetExhausted(StudyError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _write(path: Path, value) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _git(folder: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(folder), *args], capture_output=True, text=True
    )
    if result.returncode:
        raise StudyError(f"Git failed: {result.stderr.strip()}")
    return result.stdout.strip()


def initialize(
    root: Path,
    *,
    goal: str,
    harness: HarnessConfig | None = None,
    workspace: Path | None = None,
    max_iterations: int | None = 10,
    allow_write: bool = False,
    global_evaluation: dict | None = None,
    max_calls: int | None = 100,
    max_seconds: int = 3600,
    selection: dict | None = None,
    strategy: dict | None = None,
    exploration: dict | None = None,
    research_execution: dict | None = None,
    long_training: dict | None = None,
) -> Path:
    root = Path(root).resolve()
    workspace = Path(workspace).resolve() if workspace else root / "workspace"
    if not goal.strip():
        raise ValueError("A nonempty research goal is required")
    for name, value in (
        ("max_iterations", max_iterations),
        ("max_calls", max_calls),
        ("max_seconds", max_seconds),
    ):
        if value is None and name != "max_seconds":
            continue
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    long_training = validate_long_training(long_training)
    if long_training and (
        not global_evaluation
        or global_evaluation["id"] == long_training["evaluation"]["id"]
        or global_evaluation["comparison_scope"]
        == long_training["evaluation"]["comparison_scope"]
    ):
        raise ValueError(
            "Long training requires separate short and long development evaluations"
        )
    strategy = validate_strategy(strategy)
    exploration = validate_exploration(exploration)
    research_execution = validate_research_execution(research_execution)
    policy = {"seed": 0, "debate_rounds": 1, "max_resamples": 1}
    if selection:
        if set(selection) - set(policy):
            raise ValueError("Unknown selection settings")
        policy.update(selection)
    if any(type(v) is not int or v < 0 for v in policy.values()):
        raise ValueError("Selection settings must be nonnegative integers")
    if policy["debate_rounds"] < 1:
        raise ValueError("At least one debate round is required")
    if root.is_relative_to(workspace):
        raise ValueError("Workspace must not contain the study's control files")
    if root.exists() and any(root.iterdir()):
        raise StudyError(f"Refusing to initialize a nonempty directory: {root}")
    if global_evaluation is not None:
        validate_evaluation(global_evaluation)
    baseline = None
    if allow_write:
        workspace.mkdir(parents=True, exist_ok=True)
        if not (workspace / ".git").exists():
            if any(workspace.iterdir()):
                raise StudyError(
                    "Writable workspace must be an existing clean Git repo or empty"
                )
            _git(workspace, "init")
            _git(
                workspace,
                "-c",
                "user.name=Research Loop",
                "-c",
                "user.email=research-loop@localhost",
                "commit",
                "--allow-empty",
                "-m",
                "Initial research workspace",
            )
        if _git(workspace, "status", "--porcelain"):
            raise StudyError(
                "Commit or isolate existing workspace changes before initializing"
            )
        baseline = _git(workspace, "rev-parse", "HEAD")
    root.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    (root / "iterations").mkdir()
    _atomic_text(root / "goal.md", goal.rstrip() + "\n")
    _atomic_text(
        root / "research.md", "# Current research\n\nNo investigations completed.\n"
    )
    _write(
        root / "study.json",
        {
            "version": 2,
            "workspace": str(workspace),
            "baseline": baseline,
            "harness": asdict(harness or HarnessConfig()),
            "max_iterations": max_iterations,
            "allow_write": allow_write,
            "global_evaluation": global_evaluation,
            "max_calls": max_calls,
            "max_seconds": max_seconds,
            "selection": policy,
            "strategy": strategy,
            **({"exploration": exploration} if exploration else {}),
            **({"long_training": long_training} if long_training else {}),
            **(
                {"research_execution": research_execution} if research_execution else {}
            ),
        },
    )
    _write(root / "routes/index.json", {})
    _write(root / "frontiers.json", {"global": {}, "local": {}})
    _write(
        root / "state.json",
        {
            "iteration": 0,
            "phase": "done",
            "status": "ready",
            "calls": {},
            "call_count": 0,
            "elapsed_seconds": 0.0,
            "sampling_queue": [],
            "sample_counter": 0,
            "selection_round": 0,
            "active_attempt": None,
            "debugging": None,
            "stop_reason": None,
            "goal_status": "undetermined",
            "review_ref": None,
        },
    )
    return root


class Study:
    def __init__(self, root: Path, harness: AgentHarness | None = None):
        self.root = Path(root).resolve()
        self.config = _read(self.root / "study.json")
        if self.config.get("version") != 2:
            raise StudyError(
                "Unsupported study format; preserve old records and initialize a new study"
            )
        self.strategy = validate_strategy(self.config.get("strategy"))
        self.exploration = validate_exploration(self.config.get("exploration"))
        validate_research_execution(self.config.get("research_execution"))
        self.long_training = validate_long_training(self.config.get("long_training"))
        self.workspace = Path(self.config["workspace"])
        self.harness = harness or make_harness(HarnessConfig(**self.config["harness"]))
        self._run_start = None
        self._elapsed_start = 0.0

    def status(self) -> dict:
        return _read(self.root / "state.json")

    def knowledge(self) -> dict:
        return {
            "research": (self.root / "research.md").read_text(),
            "long_training": long_training_results(self.root),
            "tasks": task_results(self.root),
        }

    def frontiers(self) -> dict:
        return _read(self.root / "frontiers.json")

    def graph(self) -> list[dict]:
        return [
            _read(p)
            for p in sorted((self.root / "iterations").glob("*/iteration.json"))
        ]

    def _observations(self) -> dict:
        path = self.root / "observations.json"
        return (
            _read(path)
            if path.exists()
            else {"version": 1, "runs": [], "intervals": []}
        )

    def _save_observations(self, observations: dict) -> None:
        records = observations["runs"] + observations["intervals"]
        complete = all(r["wall_seconds"] is not None for r in records)

        def total(records):
            # An unknown crash interval must never become zero measured overhead.
            if any(r["wall_seconds"] is None for r in records):
                return None
            return sum(r["wall_seconds"] for r in records)

        intervals = observations["intervals"]
        observations["totals"] = {
            "run_wall_seconds": total(observations["runs"]),
            "selection_wall_seconds": total(
                [r for r in intervals if r["phase"] == "selection"]
            ),
            "review_wall_seconds": total(
                [r for r in intervals if r["phase"] == "review"]
            ),
            "overhead_wall_seconds": total(intervals),
            "complete": complete,
        }
        _write(self.root / "observations.json", observations)

    def _recover_observations(self) -> None:
        observations = self._observations()
        for record in observations["runs"] + observations["intervals"]:
            if record["status"] == "running":
                record["status"] = "incomplete"
        self._save_observations(observations)

    @contextmanager
    def _observe(self, phase: str, iteration: int | None = None):
        started = time.monotonic()
        identity = uuid.uuid4().hex
        key = "runs" if phase == "run" else "intervals"
        observations = self._observations()
        observations[key].append(
            {
                "id": identity,
                "phase": phase,
                "iteration": iteration,
                "started_at": _now(),
                "ended_at": None,
                "wall_seconds": None,
                "status": "running",
            }
        )
        self._save_observations(observations)
        status = "failed"
        try:
            yield
            status = "succeeded"
        finally:
            elapsed = time.monotonic() - started
            observations = self._observations()
            record = next(r for r in observations[key] if r["id"] == identity)
            record.update(ended_at=_now(), wall_seconds=elapsed, status=status)
            self._save_observations(observations)

    @contextmanager
    def _lock(self):
        with (self.root / ".lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StudyError("Another controller is using this study") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _save(self, state: dict) -> None:
        if self._run_start is not None:
            state["elapsed_seconds"] = (
                self._elapsed_start + time.monotonic() - self._run_start
            )
        _write(self.root / "state.json", state)

    def retry(self, *, readonly: bool = False) -> dict:
        with self._lock():
            self._recover_observations()
            state = self.status()
            if state["status"] not in {"needs_attention", "running"}:
                raise StudyError("There is no interrupted or failed call to retry")
            if readonly and (
                state["status"] != "needs_attention"
                or state["phase"] not in {"selection", "plan", "review"}
                or state.get("debugging")
            ):
                raise StudyError("Automatic retry requires a failed read-only phase")
            active = state["active_attempt"]
            if active and (readonly or completed_response(self.root / active) is None):
                self._check_attempt_stopped(self.root / active)
            if active and not (
                state["phase"] == "execute"
                and (
                    state.get("accepted_calls", {}).get("execute")
                    or completed_response(self.root / active) is not None
                )
            ):
                state["calls"] = {
                    k: v for k, v in state["calls"].items() if v != active
                }
            if state["phase"] == "review":
                folder = self._folder(state)
                if (folder / "publication.json").exists():
                    with (
                        self._observe("run"),
                        self._observe("review", state["iteration"]),
                    ):
                        self._publish(state, folder, _read(folder / "publication.json"))
                    return state
                state["calls"].pop("review", None)
                self._review_before(folder, state["route_id"])
            state.update(status="ready", active_attempt=None, error=None)
            self._save(state)
            return state

    def _folder(self, state: dict) -> Path:
        return self.root / "iterations" / f"{state['iteration']:04d}"

    def _check_attempt_stopped(self, attempt: Path) -> None:
        for receipt in (attempt / "process.json", attempt / "agent/process.json"):
            if not receipt.exists():
                continue
            process = _read(receipt)
            pid = process.get("pid")
            if process.get("status") != "running" or type(pid) is not int or pid <= 0:
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
            raise StudyError(
                f"Retained attempt may still have an active CLI (PID {pid}); "
                "wait or reconcile it before resuming"
            )

    def _can_resume(self, state: dict) -> bool:
        if state.get("debugging"):
            return (state.get("error") or "").startswith(
                ("KeyboardInterrupt:", "OSError:")
            )
        if (
            state["phase"] == "review"
            and (self._folder(state) / "publication.json").exists()
        ):
            return True
        active = state.get("active_attempt")
        if not active:
            return False
        result = completed_response(self.root / active)
        if result is None:
            return state["phase"] != "execute"
        # A completed response can still fail protocol or downstream semantic checks.
        # Replaying it cannot resolve that rejection; require explicit reconciliation.
        return (state.get("error") or "").startswith(("KeyboardInterrupt:", "OSError:"))

    def run(self, *, iterations: int = 1) -> dict:
        if type(iterations) is not int or iterations < 1:
            raise ValueError("iterations must be positive")
        with self._lock():
            self._recover_observations()
            with self._observe("run"):
                return self._run(iterations)

    def _run(self, iterations: int) -> dict:
        state = self.status()
        self._run_start = time.monotonic()
        self._elapsed_start = state["elapsed_seconds"]
        try:
            for _ in range(iterations):
                if state["status"] == "stopped":
                    break
                if state["status"] == "needs_attention":
                    if self._can_resume(state):
                        state["status"] = "ready"
                    else:
                        raise StudyError(
                            "Inspect retained effects then explicitly retry; no action was repeated"
                        )
                if state["phase"] == "done":
                    if (
                        self.config["max_iterations"] is not None
                        and state["iteration"] >= self.config["max_iterations"]
                    ):
                        self._stop(state, "iteration_budget")
                        break
                    self._begin(state)
                admission = getattr(self.harness, "admit_iteration", None)
                if admission is not None and state["phase"] != "done":
                    token = admission(f"{state['iteration']:04d}")
                    metadata = self._folder(state) / "iteration.json"
                    _write(metadata, _read(metadata) | {"round_admission": token})
                    commit = getattr(self.harness, "commit_iteration", None)
                    committed_at = (
                        commit(f"{state['iteration']:04d}", token) if commit else None
                    )
                    state.update(round_admission=token, round_admitted_at=committed_at)
                    self._save(state)
                while state["phase"] != "done" and state["status"] != "stopped":
                    self._step(state)
            return state
        except BudgetExhausted as exc:
            self._stop(state, str(exc))
            return state
        except (
            HarnessError,
            StudyError,
            OSError,
            ValueError,
            KeyboardInterrupt,
        ) as exc:
            if (
                not state["active_attempt"]
                and state.get("last_call_key") in state["calls"]
            ):
                state["active_attempt"] = state["calls"][state["last_call_key"]]
            state.update(status="needs_attention", error=f"{type(exc).__name__}: {exc}")
            diagnostic = self.root / "diagnostics" / f"failure-{uuid.uuid4().hex}.json"
            try:
                _write(
                    diagnostic,
                    {
                        **diagnostic_view(
                            exc,
                            owner="pipeline",
                            stage=state["phase"],
                            secrets=getattr(self.harness, "diagnostic_secrets", ()),
                        ),
                        "recorded_at": _now(),
                        "iteration": state["iteration"],
                        "attempt": state.get("active_attempt"),
                        "effects": "unknown"
                        if state["phase"] == "execute"
                        else "read_only",
                    },
                )
                state["diagnostic_ref"] = str(diagnostic.relative_to(self.root))
            except (OSError, ValueError) as diagnostic_error:
                state["diagnostic_error"] = diagnostic_view(diagnostic_error)[
                    "cause_chain"
                ]
            self._save(state)
            raise
        finally:
            self._run_start = None

    def _stop(self, state: dict, reason: str) -> None:
        state.update(
            status="stopped", phase="done", stop_reason=reason, active_attempt=None
        )
        self._save(state)

    def _begin(self, state: dict) -> None:
        admission = getattr(self.harness, "admit_iteration", None)
        token = (
            admission(f"{state['iteration'] + 1:04d}")
            if admission is not None
            else None
        )
        # Human research edits become explicit immutable revisions before sampling.
        routes = _read(self.root / "routes/index.json")
        changed = False
        for route_id, route in routes.items():
            current = self.root / "routes" / route_id / "research.md"
            if (
                current.exists()
                and current.read_text()
                != (self.root / route["research_ref"]).read_text()
            ):
                relative = f"revisions/{route_id}-{uuid.uuid4().hex}.md"
                _atomic_text(self.root / relative, current.read_text())
                _write(
                    self.root / (relative + ".json"),
                    {
                        "source": "human",
                        "previous": route["research_ref"],
                        "created_at": _now(),
                    },
                )
                route["research_ref"] = relative
                changed = True
        if changed:
            _write(self.root / "routes/index.json", routes)
        folder = self.root / "iterations" / f"{state['iteration'] + 1:04d}"
        folder.mkdir(exist_ok=True)
        if any(
            p.name not in {"context", "iteration.json", "iteration.json.tmp"}
            for p in folder.iterdir()
        ):
            raise StudyError(
                "Unexpected unowned iteration contents; inspect before continuing"
            )
        context = folder / "context"
        for source, destination in (
            ("goal.md", "goal.md"),
            ("study.json", "study.before.json"),
            ("research.md", "global.before.md"),
            ("routes/index.json", "routes.before.json"),
            ("frontiers.json", "frontiers.before.json"),
        ):
            target = context / destination
            if not target.exists():
                _atomic_text(target, (self.root / source).read_text())
        task_path = context / "task.json"
        if not task_path.exists():
            manual = self.config.get("manual_task") or TaskQueue(self.root).claim(
                "investigation", iteration=state["iteration"] + 1
            )
            _write(task_path, manual)
        results_path = context / "task_results.json"
        if not results_path.exists():
            _write(results_path, task_results(self.root))
        manual = _read(task_path)
        long_results = context / "long_training_results.json"
        if not long_results.exists() and (
            self.long_training or (self.root / "long-runs").exists()
        ):
            _write(long_results, long_training_results(self.root))
        if self.exploration:
            # Inputs arriving during this investigation are consumed next time.
            # A missing/failed scout never blocks the experiment loop.
            for source, destination in (
                (self.root / "research_inputs.json", "research_inputs.json"),
                (self.root.parent / "control/scout/latest.json", "scout_inputs.json"),
            ):
                target = context / destination
                if not target.exists():
                    try:
                        value = (
                            _read(source)
                            if source.exists() and source.stat().st_size <= 131072
                            else None
                        )
                    except (OSError, ValueError) as error:
                        value = {"unavailable": type(error).__name__}
                    _write(target, value)
            recent = [
                p
                for p in sorted((self.root / "iterations").glob("*/insight.json"))
                if (p.parent / "publication.json").exists()
                and _read(p.parent / "iteration.json")["status"] == "reviewed"
            ][-3:]
            target = context / "recent_insights.json"
            if not target.exists():
                _write(
                    target, [{"iteration": p.parent.name, **_read(p)} for p in recent]
                )
        number = state["iteration"] + 1
        metadata = folder / "iteration.json"
        if metadata.exists() and _read(metadata)["id"] != f"{number:04d}":
            raise StudyError("Inconsistent iteration metadata")
        if not metadata.exists():
            _write(
                metadata,
                {
                    "id": f"{number:04d}",
                    "previous_iteration": state["iteration"] or None,
                    "created_at": _now(),
                    "route_id": None,
                    "base_experiment": None,
                    "references": [],
                    "input_artifacts": [],
                    "output_artifacts": [],
                    "evaluation_refs": [],
                    "status": "selecting",
                },
            )
        if token is not None:
            _write(metadata, _read(metadata) | {"round_admission": token})
        commit = getattr(self.harness, "commit_iteration", None)
        committed_at = commit(f"{number:04d}", token) if commit else None
        state.update(
            round_admitted_at=committed_at,
            round_admission=token,
            iteration=number,
            phase="selection",
            status="ready",
            calls={},
            accepted_calls={},
            active_attempt=None,
            selection_round=0,
            selected=None,
            route_id=None,
            plan_ref=None,
            execution_ref=None,
            review_ref=None,
            error=None,
            debugging=None,
        )
        if manual:
            external = manual["kind"] == "long_training"
            route = "new" if external else manual["route_id"]
            base = None if external else manual["base_experiment"]
            proposal = {
                "markdown": manual["instruction"],
                "route_id": route,
                "route_title": manual["title"],
                "action": manual["instruction"],
                "base_experiment": base,
                "references": [base] if base else [],
            }
            state.update(
                phase="plan",
                selected={
                    "proposal": proposal,
                    "judge": {
                        "decision": "select",
                        "choice": "manual",
                        "action": manual["instruction"],
                        "markdown": "Operator supplied task",
                        "goal_status": "undetermined",
                        "stop_reason": None,
                    },
                },
            )
            _write(
                metadata,
                _read(metadata) | {"task_id": manual["id"], "task_origin": "manual"},
            )
        self._save(state)

    def _continue_execution(
        self, state: dict, *, source_iteration: str, continuation: dict
    ) -> None:
        """Reserve an execution record for an accepted, unmeasured Plan.

        The adapter holds the study lock and supplies operator reconciliation;
        this path never samples a research allocation or asks for another Plan.
        """
        if (
            not isinstance(source_iteration, str)
            or not source_iteration.isdigit()
            or source_iteration != f"{int(source_iteration):04d}"
            or int(source_iteration) < 1
        ):
            raise StudyError("Invalid continuation source iteration")
        required = {
            "source_iteration",
            "source_workspace_ref",
            "plan_ref",
            "authorization",
            "reconciliation",
            "directive",
            "source_signatures",
        }
        if (
            not isinstance(continuation, dict)
            or not required <= continuation.keys()
            or continuation["source_iteration"] != source_iteration
        ):
            raise StudyError("Invalid execution continuation record")
        source = self.root / "iterations" / source_iteration
        source_metadata = _read(source / "iteration.json")
        publication = _read(source / "publication.json")
        published = publication["state_after"]
        if (
            source_metadata.get("status") != "reviewed"
            or publication["metadata"] != source_metadata
            or not published.get("accepted_calls", {}).get("execute")
            or published.get("execution_ref")
            != str(Path(published["calls"]["execute"]) / "response.json")
        ):
            raise StudyError("Continuation requires a published accepted Execute")
        execution = _read(self.root / published["execution_ref"])
        if (
            execution.get("outcome") != "incomplete"
            or execution.get("artifacts") != []
            or execution.get("measurements") != []
            or source_metadata.get("output_artifacts") != []
        ):
            raise StudyError("Continuation requires an unmeasured incomplete Execute")
        version = continuation["source_workspace_ref"]
        if not version or version != source_metadata.get("workspace_ref"):
            raise StudyError("Continuation source workspace version changed")
        plan_ref = continuation["plan_ref"]
        plan_path = Path(plan_ref) if isinstance(plan_ref, str) else Path()
        if (
            plan_path.is_absolute()
            or ".." in plan_path.parts
            or len(plan_path.parts) < 4
            or plan_path.parts[0] != "iterations"
            or not plan_path.parts[1].isdigit()
            or not 1 <= int(plan_path.parts[1]) <= int(source_iteration)
        ):
            raise StudyError("Invalid original Plan reference")
        owner = self.root / "iterations" / plan_path.parts[1]
        owner_publication = _read(owner / "publication.json")
        owner_state = owner_publication["state_after"]
        plan_keys = [
            key
            for key, call in owner_state["calls"].items()
            if key.startswith("plan-")
            and str(Path(call) / "response.json") == plan_ref
            and owner_state.get("accepted_calls", {}).get(key)
        ]
        if (
            owner_publication["metadata"].get("status") != "reviewed"
            or owner_state.get("plan_ref") != plan_ref
            or not plan_keys
        ):
            raise StudyError("Continuation Plan must be an accepted published Plan")
        plan = _read(self.root / plan_ref)
        if plan.get("decision") != "investigate":
            raise StudyError("Continuation requires an investigating Plan")
        if (self.root / "goal.md").read_bytes() != (
            owner / "context/goal.md"
        ).read_bytes():
            raise StudyError("Continuation requires the original frozen goal")
        current_config = _read(self.root / "study.json")
        original_config = _read(owner / "context/study.before.json")
        # Capability policy may evolve for future Plans. The accepted Plan keeps
        # its owner's policy; every other study setting remains its contract.
        if {k: v for k, v in current_config.items() if k != "research_execution"} != {
            k: v for k, v in original_config.items() if k != "research_execution"
        }:
            raise StudyError("Continuation requires the original study configuration")
        if self.config != current_config:
            raise StudyError("Reload the current study configuration")
        number = int(source_iteration) + 1
        if state["status"] != "ready" or not (
            (state["iteration"] == int(source_iteration) and state["phase"] == "done")
            or (
                state["iteration"] == number
                and state["phase"] in {"selection", "execute"}
            )
        ):
            raise StudyError("Continuation is not at the source execution boundary")
        if (
            self.config["max_iterations"] is not None
            and number > self.config["max_iterations"]
        ):
            raise StudyError("Continuation exceeds the physical iteration budget")
        if state["iteration"] == int(source_iteration):
            self._begin(state)
        folder = self._folder(state)
        metadata = _read(folder / "iteration.json")
        workdir = folder / "workspace" if self.config["allow_write"] else self.workspace
        expected = {
            "route_id": source_metadata["route_id"],
            "base_experiment": plan["base_experiment"],
            "references": plan["references"],
            "input_artifacts": plan["input_artifacts"],
            "output_artifacts": [],
            "evaluation_refs": owner_publication["metadata"]["evaluation_refs"],
            "workspace": str(workdir),
            "workspace_ref": version,
            "status": "planned",
            "route_title": source_metadata["route_title"],
            "continuation": continuation,
        }
        if metadata.get("id") != f"{number:04d}" or metadata.get(
            "previous_iteration"
        ) != int(source_iteration):
            raise StudyError("Unexpected continuation target identity")
        if "continuation" in metadata:
            if any(metadata.get(key) != value for key, value in expected.items()):
                raise StudyError("Existing continuation target does not match")
        elif (
            state["phase"] == "selection"
            and metadata.get("status") == "selecting"
            and not state["calls"]
        ):
            if any(
                p.name not in {"context", "iteration.json"} for p in folder.iterdir()
            ):
                raise StudyError("Unexpected unowned continuation target contents")
            metadata.update(expected)
            _write(folder / "iteration.json", metadata)
        else:
            raise StudyError("Existing iteration belongs to another investigation")
        selected = owner_state["selected"]
        if state["phase"] == "execute" and (
            state.get("plan_ref") != plan_ref
            or state.get("route_id") != expected["route_id"]
            or state.get("selected") != selected
        ):
            raise StudyError("Existing continuation state does not match")

        def preserve(path: Path, value: str) -> None:
            if path.exists():
                if path.read_text() != value:
                    raise StudyError(
                        "Existing continuation file does not match: " + str(path)
                    )
            else:
                _atomic_text(path, value)

        preserve(folder / "plan.md", (owner / "plan.md").read_text())
        for name in ("local-evaluations.after.json", "context/route.before.md"):
            preserve(folder / name, (owner / name).read_text())
        if self.config["allow_write"]:
            if not workdir.exists():
                _git(
                    self.workspace, "worktree", "add", "--detach", str(workdir), version
                )
            if _git(workdir, "rev-parse", "HEAD") != version or _git(
                workdir, "rev-parse", "--path-format=absolute", "--git-common-dir"
            ) != _git(
                self.workspace,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ):
                raise StudyError(
                    "Unexpected continuation workspace version or repository"
                )
        if state["phase"] == "execute":
            return
        state.update(
            route_id=expected["route_id"],
            phase="execute",
            plan_ref=plan_ref,
            selected=selected,
        )
        self._save(state)

    def _context(self, folder: Path) -> dict:
        config = _read(folder / "context/study.before.json")
        evaluation = config["global_evaluation"]
        views = _read(folder / "context/frontiers.before.json")
        views["global"] = {
            k: v for k, v in views["global"].items() if v.get("allow_feedback", True)
        }
        context = {
            "study_root": str(self.root),
            "goal": (folder / "context/goal.md").read_text(),
            "global_research": (folder / "context/global.before.md").read_text(),
            "global_evaluation": evaluation,
            "frontiers": views,
            "routes": _read(folder / "context/routes.before.json"),
        }
        task_path = folder / "context/task.json"
        manual = _read(task_path) if task_path.exists() else None
        if manual:
            context["manual_task"] = manual
            context["manual_task_constraints"] = (
                "This operator request fixes the question, instruction, route, base and frozen source. "
                "Plan must preserve them, respect allow_changes and budget, and must not reselect. "
                "When allow_changes is false, do not edit candidate source code; execution may "
                "still write evaluation transport files and result artifacts when Study permits writes. "
                "Stop with an explicit limitation if infeasible. Preserve ordinary Plan/Execute/Review evidence."
            )
            long_request = folder / "context/manual-long-request.json"
            if manual["kind"] == "long_training" and long_request.exists():
                actual = _read(long_request)
                if actual.get("manual_task_id") != manual["id"]:
                    raise StudyError("Manual long request belongs to another task")
                context["long_training_run"] = actual
        results_path = folder / "context/task_results.json"
        if results_path.exists():
            context["task_results"] = _read(results_path)
            context["task_results_guidance"] = (
                "Reviewed manual long-training children are reusable evidence with their frozen "
                "source, evaluation scope and stated limitations. Do not mix them into short-run "
                "Frontiers or infer comparable rankings across different evaluations."
            )
        if config.get("long_training_run") is not None:
            context["long_training_run"] = config["long_training_run"]
        long_policy = validate_long_training(config.get("long_training"))
        if long_policy:
            context["long_training"] = long_policy
        long_results = folder / "context/long_training_results.json"
        if long_results.exists():
            context["long_training_results"] = _read(long_results)
        metadata = _read(folder / "iteration.json")
        continuation = metadata.get("continuation")
        scientific_folder = folder
        scientific_config = config
        if continuation:
            context["execution_continuation"] = continuation
            scientific_folder = (
                self.root / "iterations" / Path(continuation["plan_ref"]).parts[1]
            )
            scientific_config = _read(scientific_folder / "context/study.before.json")
        research_execution = validate_research_execution(
            scientific_config.get("research_execution")
        )
        if research_execution:
            context["research_execution"] = research_execution
        slot = allocation(
            scientific_config.get("exploration"), int(scientific_folder.name)
        )
        if slot and not manual:
            context["exploration"] = slot
            for name in ("research_inputs", "scout_inputs", "recent_insights"):
                path = scientific_folder / "context" / f"{name}.json"
                context[name] = _read(path) if path.exists() else None
        return context

    def _route_context(self, folder: Path, route_id: str) -> dict:
        routes = _read(folder / "context/routes.before.json")
        if route_id not in routes:
            return {
                "id": "new",
                "research": "New route; propose a bounded opportunity.",
                "evaluations": {},
            }
        route = routes[route_id]
        return {
            "id": route_id,
            "metadata": route,
            "research": (self.root / route["research_ref"]).read_text(),
            "evaluations": _read(self.root / route["evaluation_ref"]),
        }

    def _call(
        self,
        state: dict,
        phase: str,
        key: str,
        context: dict,
        directory: Path,
        *,
        workspace: Path | None = None,
        harness: AgentHarness | None = None,
    ) -> dict:
        harness = harness or self.harness
        state["last_call_key"] = key
        if key in state["calls"]:
            attempt = self.root / state["calls"][key]
            failure = attempt / "failure.json"
            if phase == "execute" and failure.exists():
                self._check_attempt_stopped(attempt)
                record = _read(failure)
                if record.get("version") != 1 or not isinstance(
                    record.get("details"), dict
                ):
                    raise StudyError("Invalid recoverable failure receipt")
                raise RecoverableExecutionError(
                    record["error"], details=record["details"]
                )
            result = completed_response(attempt)
            validate_completed = getattr(harness, "validate_completed", None)
            if (
                result is not None
                and phase == "execute"
                and not state.get("accepted_calls", {}).get(key)
                and validate_completed is not None
            ):
                result = validate_completed(
                    result, workspace=workspace or self.workspace, run_dir=attempt
                )
            if result is None:
                state["active_attempt"] = str(attempt.relative_to(self.root))
                self._check_attempt_stopped(attempt)
                if phase == "execute":
                    raise StudyError(
                        "Interrupted Execute has no successful completion receipt; "
                        "inspect retained effects then explicitly retry; no action was repeated"
                    )
                # Keep the interrupted attempt as evidence. A fresh read-only call
                # consumes the same persisted call and time budgets as any new call.
                del state["calls"][key]
                self._save(state)
        if key not in state["calls"]:
            if (
                self.config["max_calls"] is not None
                and state["call_count"] >= self.config["max_calls"]
            ):
                raise BudgetExhausted("call_budget")
            self._save(state)
            remaining = self.config["max_seconds"] - state["elapsed_seconds"]
            call_allowance = getattr(self.harness, "call_allowance", None)
            if call_allowance is not None:
                remaining = call_allowance(remaining)
            if remaining < 1:
                raise BudgetExhausted("time_budget")
            attempt = (
                directory / f"attempt-{len(list(directory.glob('attempt-*'))) + 1:03d}"
            )
            attempt.mkdir(parents=True)
            _write(attempt / "context.json", context)
            relative = str(attempt.relative_to(self.root))
            state["calls"][key] = relative
            state.update(
                status="running",
                active_attempt=relative,
                call_count=state["call_count"] + 1,
            )
            self._save(state)
            timeout = min(int(remaining), self.config["harness"]["timeout_seconds"])
            if phase == "execute":
                timeout = min(timeout, context["plan"]["contract"]["timeout_seconds"])
            try:
                result = harness.run(
                    prompt=prompt(phase, context)
                    + guidance(phase, context)
                    + long_training_guidance(phase, context)
                    + (
                        "\n\n" + context["task_results_guidance"]
                        if context.get("task_results")
                        else ""
                    )
                    + (
                        "\n\n" + context["manual_task_constraints"]
                        if context.get("manual_task")
                        else ""
                    )
                    + (
                        "\n\n" + review_guidance(self.strategy)
                        if phase == "review" and review_guidance(self.strategy)
                        else ""
                    ),
                    schema=long_training_schema(
                        phase, context, stage_schema(phase, context, SCHEMAS)
                    ),
                    workspace=workspace or self.workspace,
                    run_dir=attempt,
                    writable=phase == "execute" and self.config["allow_write"],
                    timeout_seconds=max(1, timeout),
                )
            except RecoverableExecutionError as exc:
                if phase == "execute":
                    _write(
                        attempt / "failure.json",
                        {"version": 1, "error": str(exc), "details": exc.details},
                    )
                    _write(
                        attempt / "failure-diagnostics.json",
                        {
                            **diagnostic_view(
                                exc,
                                owner="workspace",
                                stage="execute",
                                secrets=getattr(self.harness, "diagnostic_secrets", ()),
                            ),
                            "effects": "reconciled",
                            "server": exc.details.get("diagnostics"),
                        },
                    )
                raise
            except (HarnessError, OSError, ValueError, KeyboardInterrupt) as exc:
                # Adapters alone can certify recoverable workspace effects.
                # Preserve diagnosis for every other call failure without retrying
                # an Execute merely because its local CLI has stopped.
                target = attempt / "failure-diagnostics.json"
                if not target.exists():
                    _write(
                        target,
                        {
                            **diagnostic_view(
                                exc,
                                owner="pipeline",
                                stage=phase,
                                secrets=getattr(self.harness, "diagnostic_secrets", ()),
                            ),
                            "effects": "unknown" if phase == "execute" else "read_only",
                            "server": getattr(exc, "diagnostics", None),
                        },
                    )
                raise
        self._validate_result(phase, result, context)
        state.setdefault("accepted_calls", {})[key] = True
        state.update(status="ready", active_attempt=None)
        self._save(state)
        return result

    def _validate_result(self, phase: str, result: dict, context: dict) -> None:
        validate_response(
            phase,
            result,
            schema=long_training_schema(
                phase, context, stage_schema(phase, context, SCHEMAS)
            ),
        )
        validate_design(phase, result, context)
        if not result["markdown"].strip():
            raise StudyError("Stage report must be nonempty")

    def _recover_execution(self, state: dict, folder: Path, context: dict) -> dict:
        failure_ref = state["debugging"]
        record = _read(self.root / failure_ref)
        if record.get("version") != 1 or not isinstance(record.get("details"), dict):
            raise StudyError("Invalid recoverable failure receipt")
        recover = getattr(self.harness, "recover_execution", None)
        if recover is None:
            raise StudyError("Harness has no execution recovery handler")
        workspace = Path(context["workspace"])
        self._save(state)
        context = {
            **context,
            "diagnostics": self._execution_diagnostics(folder),
            "budget": {
                "calls_left": self.config["max_calls"] - state["call_count"]
                if self.config["max_calls"] is not None
                else None,
                "seconds_left": self.config["max_seconds"] - state["elapsed_seconds"],
            },
        }
        run_dir = folder / "execute/recovery"
        run_dir.mkdir(parents=True, exist_ok=True)
        result = completed_response(run_dir)
        if result is None:
            returned = run_dir / "returned.json"
            if returned.exists():
                receipt = _read(returned)
                if receipt.get("failure_ref") != failure_ref:
                    raise StudyError("Recovery response belongs to another failure")
                result = receipt["response"]
            else:

                def invoke(key, context, directory, harness):
                    result = self._call(
                        state,
                        "execute",
                        f"debug:{key}",
                        {**context, **execution_context},
                        directory,
                        workspace=workspace,
                        harness=harness,
                    )
                    if (
                        result["outcome"] != "incomplete"
                        or result["artifacts"]
                        or result["measurements"]
                    ):
                        raise StudyError(
                            "Repair calls must return incomplete with no artifacts or measurements"
                        )
                    return result

                execution_context = context
                result = recover(
                    record["details"],
                    workspace=workspace,
                    run_dir=run_dir,
                    context=context,
                    invoke=invoke,
                )
                _write(
                    returned,
                    {
                        "version": 1,
                        "failure_ref": failure_ref,
                        "response": result,
                    },
                )
            validate_completed = getattr(self.harness, "validate_completed", None)
            if validate_completed is not None:
                result = validate_completed(
                    result, workspace=workspace, run_dir=run_dir
                )
            self._validate_result("execute", result, context)
            _write(run_dir / "response.json", result)
            publish_completed_response(run_dir, result)
        self._validate_result("execute", result, context)
        state["calls"]["execute"] = str(run_dir.relative_to(self.root))
        state.setdefault("accepted_calls", {})["execute"] = True
        state.update(
            debugging=None, status="ready", active_attempt=None, last_call_key="execute"
        )
        self._save(state)
        return result

    def _execution_diagnostics(self, folder: Path) -> dict:
        """Freeze references to this investigation's public failure records only."""
        attempts = []
        for path in sorted((folder / "execute").glob("**/failure-diagnostics.json")):
            if path.is_symlink() or path.stat().st_size > 1024 * 1024:
                raise StudyError("Invalid execution diagnostic record")
            attempts.append(
                {
                    "attempt": str(path.parent.relative_to(self.root)),
                    "record": _read(path),
                }
            )
        result = {"attempts": attempts}
        adapter_context = getattr(self.harness, "diagnostic_context", None)
        if adapter_context is not None:
            result["server"] = adapter_context(folder.name)
        return result

    def _sample(self, state: dict, folder: Path, round_id: int) -> list[str]:
        path = folder / "selection.json"
        selections = _read(path) if path.exists() else {"rounds": {}}
        record = selections["rounds"].get(str(round_id))
        if record is None:
            routes = _read(folder / "context/routes.before.json")
            slot = self._context(folder).get("exploration")
            eligible = [r for r, v in routes.items() if v["status"] == "active"]
            queue = [r for r in state["sampling_queue"] if r in eligible or r == "new"]
            rng = random.Random(
                self.config["selection"]["seed"] + state["sample_counter"]
            )
            candidates = []
            if slot and slot["kind"] != "refinement":
                candidates = route_candidates(slot, routes, self.graph())
            elif not eligible:
                candidates = ["new", "new"]
            else:
                while len(candidates) < 2:
                    if not queue:
                        queue = eligible + ["new"]
                        rng.shuffle(queue)
                    index = next(
                        (i for i, r in enumerate(queue) if r not in candidates), None
                    )
                    if index is None:
                        refill = [r for r in eligible + ["new"] if r not in candidates]
                        rng.shuffle(refill)
                        queue += refill
                        continue
                    candidates.append(queue.pop(index))
            record = {
                "candidates": candidates,
                "queue_after": queue,
                "counter": state["sample_counter"] + 1,
                "seed": self.config["selection"]["seed"],
                **({"exploration": slot} if slot else {}),
            }
            selections["rounds"][str(round_id)] = record
            _write(path, selections)
        if state["sample_counter"] < record["counter"]:
            state.update(
                sampling_queue=record["queue_after"], sample_counter=record["counter"]
            )
            self._save(state)
        return record["candidates"]

    def _select(self, state: dict, folder: Path) -> None:
        round_id = state["selection_round"]
        common = self._context(folder)
        common["budget"] = {
            "calls_left": self.config["max_calls"] - state["call_count"]
            if self.config["max_calls"] is not None
            else None,
            "seconds_left": self.config["max_seconds"] - state["elapsed_seconds"],
        }
        choices = self._sample(state, folder, round_id)
        base = folder / "selection" / f"round-{round_id + 1:03d}"
        proposals = {}
        for side, route_id in zip(("a", "b"), choices):
            context = {
                **common,
                "role": side,
                "route": self._route_context(folder, route_id),
            }
            proposals[side] = self._call(
                state,
                "proposal",
                f"s{round_id}-{side}",
                context,
                base / f"{side}-proposal",
            )
            if route_id != "new" and proposals[side]["route_id"] != route_id:
                raise StudyError("Advocate changed its sampled route")
            if (
                proposals[side]["route_id"] != "new"
                and proposals[side]["route_id"] not in common["routes"]
            ):
                raise StudyError("Unknown proposed route")
        debate = []
        for turn in range(
            debate_rounds(self.strategy, self.config["selection"]["debate_rounds"])
        ):
            for phase in ("critique", "response"):
                for side in ("a", "b"):
                    context = {
                        **common,
                        "role": side,
                        "route": self._route_context(
                            folder, proposals[side]["route_id"]
                        ),
                        "proposals": proposals,
                        "debate": debate,
                    }
                    result = self._call(
                        state,
                        phase,
                        f"s{round_id}-{turn}-{side}-{phase}",
                        context,
                        base / f"{turn + 1}-{side}-{phase}",
                    )
                    debate.append(
                        {"side": side, "phase": phase, "markdown": result["markdown"]}
                    )
        judge = self._call(
            state,
            "judge",
            f"s{round_id}-judge",
            {**common, "proposals": proposals, "debate": debate},
            base / "judge",
        )
        path = folder / "selection.json"
        records = _read(path)
        records["rounds"][str(round_id)].update(proposals=proposals, judge=judge)
        _write(path, records)
        if judge["decision"] == "stop":
            state["goal_status"] = judge["goal_status"]
            self._stop(state, judge["stop_reason"] or "judge")
        elif judge["decision"] == "resample":
            self._reselect(state)
        else:
            if judge["choice"] not in proposals:
                raise StudyError("Judge select must identify a proposal")
            state.update(
                selected={"proposal": proposals[judge["choice"]], "judge": judge},
                phase="plan",
            )
            self._save(state)

    def _reselect(self, state: dict) -> None:
        if self._context(self._folder(state)).get("manual_task"):
            raise StudyError("Manual task cannot reselect its question or base")
        if state["selection_round"] >= self.config["selection"]["max_resamples"]:
            self._stop(state, "selection_budget")
        else:
            state.update(
                selection_round=state["selection_round"] + 1,
                phase="selection",
                selected=None,
            )
            self._save(state)

    def _step(self, state: dict) -> None:
        phase = state["phase"]
        if phase in {"selection", "review"}:
            with self._observe(phase, state["iteration"]):
                self._dispatch_step(state)
        else:
            self._dispatch_step(state)

    def _dispatch_step(self, state: dict) -> None:
        folder = self._folder(state)
        if state["phase"] == "selection":
            self._select(state, folder)
            return
        if state["phase"] == "plan":
            proposal = state["selected"]["proposal"]
            context = {
                **self._context(folder),
                "selected": state["selected"],
                "route": self._route_context(folder, proposal["route_id"]),
                "workspace": str(folder / "workspace"),
            }
            result = self._call(
                state,
                "plan",
                f"plan-{state['selection_round']}",
                context,
                folder / "plan",
            )
            if result["decision"] == "stop":
                self._stop(state, result["stop_reason"] or "plan")
                return
            if result["decision"] == "reselect":
                self._reselect(state)
                return
            self._accept_plan(state, folder, result)
            return
        if state["phase"] == "execute":
            metadata = _read(folder / "iteration.json")
            context = {
                **self._context(folder),
                "route_research": (folder / "context/route.before.md").read_text(),
                "plan": _read(self.root / state["plan_ref"]),
                "experiment": metadata,
                "workspace": metadata["workspace"],
                "execution_writes_enabled": self.config["allow_write"],
            }
            self._check_readonly_task_source(
                folder, metadata, context.get("manual_task")
            )
            # Execution receives relevant global context but no debate transcript.
            if not state.get("debugging"):
                try:
                    result = self._call(
                        state,
                        "execute",
                        "execute",
                        context,
                        folder / "execute",
                        workspace=Path(metadata["workspace"]),
                    )
                except RecoverableExecutionError:
                    state["debugging"] = str(
                        Path(state["calls"]["execute"]) / "failure.json"
                    )
                    state.update(status="ready", active_attempt=None)
                    self._save(state)
            if state.get("debugging"):
                result = self._recover_execution(state, folder, context)
            definition = self.config["global_evaluation"]
            if (
                definition
                and not definition["allow_feedback"]
                and any(
                    m["evaluation_id"] == definition["id"]
                    for m in result["measurements"]
                )
            ):
                raise StudyError(
                    "Final-only evaluations must be graded outside the research loop"
                )
            _atomic_text(folder / "execute.md", result["markdown"])
            state["execution_ref"] = str(
                Path(state["calls"]["execute"]) / "response.json"
            )
            metadata["output_artifacts"] = result["artifacts"]
            if self.config["allow_write"] and metadata["status"] != "executed":
                metadata["workspace_ref"] = self._checkpoint(
                    Path(metadata["workspace"]), result["artifacts"]
                )
            metadata["status"] = "executed"
            _write(folder / "iteration.json", metadata)
            if not (folder / "review.before.json").exists():
                self._review_before(folder, state["route_id"])
            state["phase"] = "review"
            self._save(state)
            return
        if state["phase"] == "review":
            self._review(state, folder)

    def _check_readonly_task_source(
        self, folder: Path, metadata: dict, manual: dict | None
    ) -> None:
        if (
            self.config["allow_write"]
            or not manual
            or manual["kind"] != "investigation"
        ):
            return
        error = (
            "Read-only manual task has no verifiable frozen source; "
            "inspect retained state before resuming Execute"
        )
        version = manual.get("source", {}).get("revision")
        workdir = folder / "workspace"
        if (
            not version
            or metadata.get("task_id") != manual["id"]
            or metadata.get("workspace_ref") != version
            or metadata.get("workspace") != str(workdir)
            or workdir.resolve() != workdir
        ):
            raise StudyError(error)
        try:
            request = TaskQueue(self.root).request(manual["id"])
            if request != manual or _git(workdir, "rev-parse", "HEAD") != version:
                raise StudyError(error)
        except (OSError, ValueError, StudyError) as exc:
            raise StudyError(error) from exc

    def _accept_plan(self, state: dict, folder: Path, result: dict) -> None:
        proposal = state["selected"]["proposal"]
        if result["base_experiment"] != proposal["base_experiment"]:
            raise StudyError("Plan changed the selected base; return reselect instead")
        for ref in set(
            result["references"]
            + ([result["base_experiment"]] if result["base_experiment"] else [])
        ):
            if not ref.isdigit() or int(ref) >= state["iteration"]:
                raise StudyError(
                    "Graph references must point to earlier reviewed experiments"
                )
            prior = _read(self.root / "iterations" / ref / "iteration.json")
            if prior["status"] != "reviewed":
                raise StudyError("Graph reference has no published review")
        local = result["local_evaluation"]
        if result["contract"]["timeout_seconds"] < 1:
            raise StudyError("Execution contract timeout must be positive")
        if local:
            validate_evaluation(local)
            if (
                self.config["global_evaluation"]
                and local["id"] == self.config["global_evaluation"]["id"]
            ):
                raise StudyError("Local evaluation cannot reuse the global identity")
            if not local["allow_feedback"]:
                raise StudyError("Local search evaluation must allow feedback")
        routes = _read(folder / "context/routes.before.json")
        route_id = proposal["route_id"]
        if route_id == "new":
            route_id = f"R{max([int(r[1:]) for r in routes] or [0]) + 1:04d}"
        route = self._route_context(folder, proposal["route_id"])
        definitions = route["evaluations"]
        if local:
            if local["id"] in definitions and definitions[local["id"]] != local:
                raise StudyError("Changed local definition requires a new ID")
            definitions[local["id"]] = local
        _write(folder / "local-evaluations.after.json", definitions)
        _atomic_text(
            folder / "context/route.before.md",
            route["research"]
            if proposal["route_id"] != "new"
            else result["route_research"],
        )
        _atomic_text(folder / "plan.md", result["markdown"])
        workdir = self.workspace
        version = self.config["baseline"]
        manual = self._context(folder).get("manual_task")
        if manual:
            version = manual["source"]["revision"]
        if result["base_experiment"] and not manual:
            version = _read(
                self.root / "iterations" / result["base_experiment"] / "iteration.json"
            ).get("workspace_ref")
        if self.config["allow_write"] or manual:
            if not version:
                raise StudyError("Selected experiment has no recoverable Git version")
            workdir = folder / "workspace"
            if not workdir.exists():
                project = (
                    Path(manual["source"]["project"])
                    if manual and not self.config["allow_write"]
                    else self.workspace
                )
                _git(project, "worktree", "add", "--detach", str(workdir), version)
            if _git(workdir, "rev-parse", "HEAD") != version:
                raise StudyError(
                    "Unexpected workspace version while preparing execution"
                )
        metadata = _read(folder / "iteration.json")
        metadata.update(
            route_id=route_id,
            base_experiment=result["base_experiment"],
            references=result["references"],
            input_artifacts=result["input_artifacts"],
            evaluation_refs=[
                d["id"] for d in (self.config["global_evaluation"], local) if d
            ],
            workspace=str(workdir),
            workspace_ref=version,
            status="planned",
            route_title=proposal["route_title"],
        )
        _write(folder / "iteration.json", metadata)
        state.update(
            route_id=route_id,
            phase="execute",
            plan_ref=str(
                Path(state["calls"][f"plan-{state['selection_round']}"])
                / "response.json"
            ),
        )
        self._save(state)

    def _checkpoint(self, workdir: Path, artifacts: list[dict]) -> str:
        source_suffixes = {
            ".py",
            ".toml",
            ".md",
            ".yaml",
            ".yml",
            ".json",
            ".c",
            ".h",
            ".cpp",
            ".rs",
            ".jl",
            ".sh",
        }
        modified = _git(workdir, "diff", "--name-only", "HEAD").splitlines()
        for name in modified:
            if Path(name).suffix not in source_suffixes:
                raise StudyError(
                    "Tracked non-source artifact changed; preserve it and explicitly version the input"
                )
            _git(workdir, "add", "--", name)
        # Enumerate names only; never read/hash datasets or binary checkpoints.
        for name in _git(
            workdir, "ls-files", "--others", "--exclude-standard"
        ).splitlines():
            path = workdir / name
            if path.suffix in source_suffixes:
                if path.stat().st_size > 1024 * 1024:
                    raise StudyError(
                        "Unversioned large text artifact requires an explicit external reference"
                    )
                _git(workdir, "add", "--", name)
        for artifact in artifacts:
            path = Path(artifact["path"])
            path = path if path.is_absolute() else workdir / path
            if path.suffix in source_suffixes and path.is_relative_to(workdir):
                _git(workdir, "add", "--", str(path))
        # Deterministic code commits make receipt recovery idempotent, even if the
        # controller dies between publishing the Git object and node metadata.
        folder = workdir.parent
        metadata = _read(folder / "iteration.json")
        tree = _git(workdir, "write-tree")
        env = dict(
            os.environ,
            GIT_AUTHOR_NAME="Research Loop",
            GIT_AUTHOR_EMAIL="research-loop@localhost",
            GIT_COMMITTER_NAME="Research Loop",
            GIT_COMMITTER_EMAIL="research-loop@localhost",
            GIT_AUTHOR_DATE=metadata["created_at"],
            GIT_COMMITTER_DATE=metadata["created_at"],
        )
        result = subprocess.run(
            [
                "git",
                "-C",
                str(workdir),
                "commit-tree",
                tree,
                "-p",
                metadata["workspace_ref"],
                "-m",
                f"Research candidate {metadata['id']}",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        commit = result.stdout.strip()
        _git(workdir, "reset", "--soft", commit)
        return commit

    def _review_before(self, folder: Path, route_id: str) -> None:
        current_config = _read(self.root / "study.json")
        frozen_config = _read(folder / "context/study.before.json")
        if {k: v for k, v in current_config.items() if k != "research_execution"} != {
            k: v for k, v in frozen_config.items() if k != "research_execution"
        }:
            raise StudyError(
                "Study configuration changed during the investigation; reconcile the frozen contract"
            )
        if (self.root / "goal.md").read_bytes() != (
            folder / "context/goal.md"
        ).read_bytes():
            raise StudyError(
                "Study goal changed during the investigation; reconcile the frozen contract"
            )
        if (self.root / "telemetry").is_dir():
            from .telemetry import freeze

            freeze(self.root, folder.name, folder / "context/telemetry.json")
        paths = [
            "study.json",
            "goal.md",
            "research.md",
            "routes/index.json",
            "frontiers.json",
            f"routes/{route_id}/research.md",
            f"routes/{route_id}/evaluation.json",
        ]
        _write(
            folder / "review.before.json",
            {
                p: (self.root / p).read_text() if (self.root / p).exists() else None
                for p in paths
            },
        )

    def _review(self, state: dict, folder: Path) -> None:
        publication = folder / "publication.json"
        if publication.exists():
            self._publish(state, folder, _read(publication))
            return
        before = _read(folder / "review.before.json")
        route_text = (
            before[f"routes/{state['route_id']}/research.md"]
            or (folder / "context/route.before.md").read_text()
        )
        context = {
            **self._context(folder),
            "route_research": route_text,
            "global_research": before["research.md"],
            "plan": _read(self.root / state["plan_ref"]),
            "execute": _read(self.root / state["execution_ref"]),
            "experiment": _read(folder / "iteration.json"),
            "workspace": str(folder / "workspace"),
            "selected_research_design": state.get("selected", {})
            .get("proposal", {})
            .get("research_design"),
        }
        telemetry = folder / "context/telemetry.json"
        if telemetry.exists():
            context["telemetry"] = _read(telemetry)
        context["diagnostics"] = self._execution_diagnostics(folder)
        if context.get("long_training_run"):
            context["long_training_observations"] = long_observation_context(context)
        guidance = review_guidance(self.strategy)
        if guidance:
            context["review_guidance"] = guidance
        result = self._call(state, "review", "review", context, folder / "review")
        validate_observation_reviews(context, result)
        execute = context["execute"]
        reviews = {r["candidate_id"]: r for r in result["candidate_reviews"]}
        if len(reviews) != len(result["candidate_reviews"]):
            raise StudyError("Duplicate candidate reviews")
        measured_ids = [m["candidate_id"] for m in execute["measurements"]]
        if len(set(measured_ids)) != len(measured_ids) or set(reviews) != set(
            measured_ids
        ):
            raise StudyError(
                "Candidate reviews must match unique current measurements exactly"
            )
        plan = context["plan"]
        definitions = [
            d for d in (self.config["global_evaluation"], plan["local_evaluation"]) if d
        ]
        candidates = []
        for measurement in execute["measurements"]:
            cid = measurement["candidate_id"]
            if cid not in reviews:
                raise StudyError("Every measurement requires explicit review")
            definition = next(
                (d for d in definitions if d["id"] == measurement["evaluation_id"]),
                None,
            )
            if definition is None:
                raise StudyError(
                    "Measurement uses an evaluation outside the frozen contract"
                )
            if not definition["allow_feedback"]:
                raise StudyError(
                    "Final-only evaluations must be graded outside the research loop"
                )
            metrics = {m["name"]: m["value"] for m in measurement["metrics"]}
            if len(metrics) != len(measurement["metrics"]):
                raise StudyError("Duplicate metric names")
            artifact = measurement["artifact"]
            artifact_path = Path(artifact["path"])
            if not artifact_path.is_absolute():
                artifact_path = Path(context["experiment"]["workspace"]) / artifact_path
            artifact_path = artifact_path.resolve()
            if artifact not in execute["artifacts"]:
                raise StudyError(
                    "Measured candidate is not a declared execution artifact"
                )
            if not artifact_path.is_relative_to(
                Path(context["experiment"]["workspace"]).resolve()
            ):
                raise StudyError(
                    "Candidate must belong to the isolated execution workspace"
                )
            if reviews[cid]["verified"] and not artifact_path.exists():
                raise StudyError("Verified candidate artifact is missing")
            candidates.append(
                {
                    "id": f"{state['iteration']:04d}:{cid}",
                    "experiment_id": f"{state['iteration']:04d}",
                    "artifact": {
                        "path": str(artifact_path),
                        "version": artifact["version"],
                    },
                    "evaluation_id": measurement["evaluation_id"],
                    "comparison_scope": measurement["comparison_scope"],
                    "metrics": metrics,
                    "evidence": measurement["evidence"],
                    "verified": reviews[cid]["verified"],
                    "valid": reviews[cid]["valid"],
                }
            )
        _write(
            folder / "candidates.json",
            {
                "route_id": state["route_id"],
                "global_evaluation_id": self.config["global_evaluation"]["id"]
                if self.config["global_evaluation"]
                else None,
                "definitions": definitions,
                "candidates": candidates,
                "corrections": result.get("candidate_corrections", []),
            },
        )
        views = self._build_views(extra=folder)
        if context.get("long_training"):
            requests = long_training_nominations(
                self.root,
                context["long_training"],
                result["long_training_decision"],
                candidates,
                context["frontiers"],
                views,
                context["experiment"],
            )
            _write(
                folder / "long-training.json",
                {"decision": result["long_training_decision"], "requests": requests},
            )
        metadata = context["experiment"]
        metadata["status"] = "reviewed"
        metadata["references"] = sorted(
            set(metadata["references"])
            | {
                correction["candidate_id"].split(":", 1)[0]
                for correction in result.get("candidate_corrections", [])
            }
        )
        routes = json.loads(before["routes/index.json"])
        relative = str(folder.relative_to(self.root))
        routes[state["route_id"]] = {
            "title": metadata["route_title"],
            "status": result["route_status"],
            "reason": result["route_reason"],
            "resume_condition": result["resume_condition"],
            "research_ref": f"{relative}/route.after.md",
            "evaluation_ref": f"{relative}/local-evaluations.after.json",
        }
        writes = {
            "research.md": result["global_research"],
            "routes/index.json": json.dumps(routes, indent=2, ensure_ascii=False)
            + "\n",
            "frontiers.json": json.dumps(views, indent=2, ensure_ascii=False) + "\n",
            f"routes/{state['route_id']}/research.md": result["route_research"],
            f"routes/{state['route_id']}/evaluation.json": (
                folder / "local-evaluations.after.json"
            ).read_text(),
        }
        _atomic_text(folder / "review.md", result["markdown"])
        _atomic_text(folder / "route.after.md", result["route_research"])
        _atomic_text(folder / "global.after.md", result["global_research"])
        state_after = dict(state)
        state_after.update(
            status="ready",
            phase="done",
            active_attempt=None,
            review_ref=str(Path(state["calls"]["review"]) / "response.json"),
            goal_status=result["goal_status"],
        )
        if result["decision"] == "stop" or (
            self.config["max_iterations"] is not None
            and state["iteration"] >= self.config["max_iterations"]
        ):
            state_after.update(
                status="stopped",
                stop_reason="review"
                if result["decision"] == "stop"
                else "iteration_budget",
            )
        bundle = {
            "before": before,
            "writes": writes,
            "metadata": metadata,
            "state_after": state_after,
        }
        # Refuse conflicts before preparing a replayable publication.
        self._check_publication(bundle)
        if "insight" in result:
            _write(folder / "insight.json", result["insight"])
        _write(publication, bundle)
        self._publish(state, folder, bundle)

    def _check_publication(self, bundle: dict) -> None:
        for relative, old in bundle["before"].items():
            path = self.root / relative
            current = path.read_text() if path.exists() else None
            if current not in (old, bundle["writes"].get(relative, old)):
                raise StudyError(
                    f"{relative} changed during review; preserve the edit and retry"
                )

    def _publish(self, state: dict, folder: Path, bundle: dict) -> None:
        self._check_publication(bundle)
        for relative, value in bundle["writes"].items():
            _atomic_text(self.root / relative, value)
        _write(folder / "iteration.json", bundle["metadata"])
        next_state = dict(bundle["state_after"])
        self._save(next_state)
        state.update(next_state)

    def _build_views(self, *, extra: Path | None = None) -> dict:
        records = []
        for folder in sorted((self.root / "iterations").iterdir()):
            if folder == extra or (
                (folder / "publication.json").exists()
                and _read(folder / "iteration.json")["status"] == "reviewed"
            ):
                path = folder / "candidates.json"
                if path.exists():
                    records.append(_read(path))
        definitions = {}
        current = self.config["global_evaluation"]
        if current:
            definitions[(None, current["id"])] = current
        for record in records:
            for definition in record["definitions"]:
                owner = (
                    None
                    if definition["id"] == record["global_evaluation_id"]
                    else record["route_id"]
                )
                key = (owner, definition["id"])
                if key in definitions and definitions[key] != definition:
                    raise StudyError(
                        "Evaluation identity was reused with a different definition"
                    )
                definitions[key] = definition
        result = {"global": {}, "local": {}}
        # Corrections change eligibility only; original measurement records stay intact.
        known = {}
        for record in records:
            for correction in record.get("corrections", []):
                candidate_id = correction["candidate_id"]
                if candidate_id not in known or not correction["reason"].strip():
                    raise StudyError(
                        "Correction requires an earlier published candidate and reason"
                    )
                known[candidate_id].update(
                    valid=correction["valid"], verified=correction["verified"]
                )
            record["candidates"] = [dict(c) for c in record["candidates"]]
            for candidate in record["candidates"]:
                if candidate["id"] in known:
                    raise StudyError("Duplicate qualified candidate ID")
                known[candidate["id"]] = candidate
        for (owner, evaluation_id), definition in definitions.items():
            candidates = [
                c
                for r in records
                if (owner is None and r["global_evaluation_id"] == evaluation_id)
                or (
                    owner == r["route_id"]
                    and evaluation_id != r["global_evaluation_id"]
                )
                for c in r["candidates"]
                if c["evaluation_id"] == evaluation_id
            ]
            view = build_frontier(definition, candidates)
            view["allow_feedback"] = definition["allow_feedback"]
            if owner is None:
                result["global"][evaluation_id] = view
            else:
                result["local"].setdefault(owner, {})[evaluation_id] = view
        return result

    def rebuild_frontiers(self) -> dict:
        with self._lock():
            if self.status()["phase"] != "done":
                raise StudyError(
                    "Finish or reconcile the active iteration before rebuilding"
                )
            result = self._build_views()
            _write(self.root / "frontiers.json", result)
            return result
