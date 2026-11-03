"""Attach the independently authorized long-training lane to the supervisor."""

from __future__ import annotations

import fcntl
import json
from pathlib import Path
import threading
import time

from research_loop import Study
from research_loop.harness import CodexHarness, HarnessConfig
from research_loop.long_training import LongTrainingManager, validate_policy, snapshot
from integrations.protok.backend import ProTokError
from integrations.protok.long_training import (
    METRICS,
    RECIPE,
    _policy,
    _run_id,
    execute,
    round_authorization,
)

PROTOCOL = "se3-long-v1-10k-full4491-seed42"


def _read(path):
    return json.loads(Path(path).read_text())


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def study_policy(root, *, grant=None):
    """Freeze research scope from an explicit separate resource grant."""
    path = Path(root) / "control/long-training-policy.json"
    if grant is None:
        if not path.exists():
            return None
        grant = _policy(path.parent)
    return validate_policy(
        {
            "version": 1,
            "recipe": RECIPE,
            "evaluation": {
                "id": PROTOCOL,
                "comparison_scope": PROTOCOL,
                "protocol": "Fresh seed42; 10000 optimizer steps; original 50k scheduler; full4491 development proteins every2000; Euler100. Endpoint metrics share checkpoint10000; no best-over-curve substitution.",
                "data_role": "development",
                "allow_feedback": True,
                "objectives": [
                    {
                        "name": name,
                        "direction": "minimize" if key == "ca_rmsd" else "maximize",
                        "unit": "Angstrom" if key == "ca_rmsd" else "fraction",
                    }
                    for name, key in METRICS.items()
                ],
                "constraints": [],
            },
            "max_runs": None,
            "max_seconds_per_run": grant["gpu_seconds_per_run"],
        }
    )


def enable(root, backend):
    """Explicit configuration change, only between fully published investigations."""
    from integrations.protok.run import admission

    root = Path(root)
    with (
        (root / "control/supervisor.lock").open("a") as supervisor,
        (root / "control/runner.lock").open("a") as lock,
    ):
        fcntl.flock(supervisor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        admission(root, backend)
        study = Study(root / "study")
        with study._lock():
            state = study.status()
            if state["phase"] != "done":
                raise ProTokError(
                    "Enable long training only at a published iteration boundary"
                )
            if (
                state["iteration"]
                and not (
                    study.root
                    / "iterations"
                    / f"{state['iteration']:04d}"
                    / "publication.json"
                ).exists()
            ):
                raise ProTokError("Latest short Review is unpublished")
            policy = study_policy(root)
            if policy is None:
                raise ProTokError(
                    "Create the explicit separate long-training resource grant first"
                )
            if study.config.get("long_training"):
                return study.config["long_training"]
            study.config["long_training"] = policy
            _write(study.root / "study.json", study.config)
            return policy


def manager(root, backend):
    root = Path(root)
    study = _read(root / "study/study.json")
    delegate = CodexHarness(HarnessConfig(**study["harness"]))

    def executor(request, workspace, *, deadline):
        return execute(backend, root, request, workspace, deadline=deadline)

    return LongTrainingManager(
        root / "study",
        delegate,
        executor,
        admission=lambda request, token: round_authorization(
            backend, root, request, token
        ),
    )


def run_one(root, backend):
    root = Path(root)
    if (root / "control/STOP").exists():
        raise ProTokError("Parent Study is stopped")
    return manager(root, backend).run_one(
        deadline=_policy(root / "control")["deadline"]
    )


def recover(root, backend, run_id):
    """Reconcile an existing owned job, then accept that same Execute and Review."""
    from research_loop.harness import publish_completed_response

    root = Path(root)
    Study(root / "study")
    run_id = _run_id(run_id)
    job_dir = root / "control/long-training-jobs" / run_id
    job = _read(job_dir / "job.json")
    if job["run_id"] != run_id:
        raise ProTokError("Recovery run identity differs")
    run = root / "study/long-runs" / run_id
    child = Study(run / "study")
    with (root / "study/long-runs/lane.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with child._lock():
            state = child.status()
            if state["phase"] != "execute" or not state["calls"].get("execute"):
                raise ProTokError(
                    "Recovery requires the retained child Execute attempt"
                )
            attempt = child.root / state["calls"]["execute"]
            child._check_attempt_stopped(attempt)
            result = execute(
                backend,
                root,
                job["request"],
                child.workspace,
                deadline=job["deadline"],
                recover=True,
            )
            publish_completed_response(attempt, result)
        if child.status()["status"] in {"running", "needs_attention"}:
            child.retry()
    # Ordinary Study recovery retains this successful receipt; no new allocation.
    return (
        run_one(root, backend)
        if not (root / "control/STOP").exists()
        else {"reconciled": run_id, "review_pending": True}
    )


class LongTrainingScheduler:
    def __init__(self, root, backend, *, lane_manager=None, interval=3):
        self.root = Path(root)
        self.backend = backend
        self.manager = lane_manager
        self.interval = interval
        self.stopped = threading.Event()
        self.nominations_finished = threading.Event()
        self._status = {"status": "idle", "reason": None}
        self.thread = threading.Thread(
            target=self._run, daemon=True, name="long-training"
        )

    def status(self):
        return dict(self._status)

    def finish_nominations(self):
        """No more parent nominations will arrive; drain the existing legal queue."""
        self.nominations_finished.set()

    def _run(self):
        try:
            while not self.stopped.is_set():
                study = self.root / "study/study.json"
                if (self.root / "control/STOP").exists():
                    self._status = {"status": "finished", "reason": "user_stop"}
                    return
                enabled = study.exists() and _read(study).get("long_training")
                handoff = (self.root / "control/HANDOFF").exists()
                if enabled:
                    if self.manager is None:
                        self.manager = manager(self.root, self.backend)
                    self._status = {"status": "running", "reason": None}
                    result = self.manager.run_one(
                        deadline=_policy(self.root / "control")["deadline"],
                        allow_new=not handoff,
                    )
                    if result is not None and result.get("status") in {
                        "needs_attention",
                        "stopped",
                    }:
                        raise ProTokError("Long child requires reconciliation")
                    cutoff = time.time() >= _policy(self.root / "control")["deadline"]
                    rows = snapshot(self.root / "study")
                    pending = any(
                        row["status"] not in {"reviewed", "withdrawn"} for row in rows
                    )
                    if (
                        result is None
                        and pending
                        and not any(row["status"] == "queued" for row in rows)
                        and not (cutoff or handoff)
                    ):
                        raise ProTokError(
                            "Retained long child cannot make progress; inspect its state"
                        )
                    if result is None and (
                        (self.nominations_finished.is_set() and not pending)
                        or cutoff
                        or handoff
                    ):
                        self._status = {
                            "status": "finished",
                            "reason": "handoff"
                            if handoff
                            else "admission_cutoff"
                            if cutoff
                            else "no_pending",
                        }
                        return
                elif self.nominations_finished.is_set() or handoff:
                    self._status = {"status": "finished", "reason": "disabled"}
                    return
                self._status = {"status": "idle", "reason": None}
                self.stopped.wait(self.interval)
        except (Exception, KeyboardInterrupt) as error:
            message = f"{type(error).__name__}: {error}"
            budget = "GPU budget exhausted" in str(error)
            self._status = {
                "status": "finished" if budget else "blocked",
                "reason": "budget_exhausted" if budget else message,
            }
            _write(
                self.root / "control/long-training-error.json",
                {"error": message, "at": time.time()},
            )

    def start(self):
        self.thread.start()

    def shutdown(self):
        self.stopped.set()
        if self.thread.ident is not None:
            self.thread.join()
