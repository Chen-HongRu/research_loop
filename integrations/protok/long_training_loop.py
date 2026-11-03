"""Attach the independently authorized long-training lane to the supervisor."""

from __future__ import annotations

import fcntl
import json
from pathlib import Path

from research_loop import Study
from research_loop.scheduling import AsyncStudyScheduler, child_run
from research_loop.harness import CodexHarness, HarnessConfig
from research_loop.long_training import LongTrainingManager, validate_policy
from integrations.protok.backend import ProTokError
from integrations.protok.long_training import (
    METRICS,
    automatic_slot,
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


def manager(root, backend, *, wait_for_slot=False):
    root = Path(root)
    study = _read(root / "study/study.json")
    delegate = CodexHarness(HarnessConfig(**study["harness"]))

    def executor(request, workspace, *, deadline):
        return execute(
            backend,
            root,
            request,
            workspace,
            deadline=deadline,
            wait_for_slot=wait_for_slot,
        )

    def admission(request, token):
        if token or not wait_for_slot:
            return round_authorization(backend, root, request, token)
        # The lock covers the settled-budget admission check only. Plan/Review
        # run independently; allocation itself takes the same shared slot.
        with automatic_slot(root / "control", admission=True):
            return round_authorization(backend, root, request)

    return LongTrainingManager(root / "study", delegate, executor, admission=admission)


def run_one(root, backend, *, run_id=None):
    root = Path(root)
    if (root / "control/STOP").exists():
        raise ProTokError("Parent Study is stopped")
    return manager(root, backend).run_one(
        deadline=_policy(root / "control")["deadline"], run_id=run_id
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
    with child_run(run):
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
        run_one(root, backend, run_id=run_id)
        if not (root / "control/STOP").exists()
        else {"reconciled": run_id, "review_pending": True}
    )


class LongTrainingScheduler(AsyncStudyScheduler):
    def __init__(self, root, backend, *, lane_manager=None, interval=3):
        self.backend = backend
        self.manager = lane_manager
        self._provided_manager = lane_manager is not None
        super().__init__(
            root,
            pending=self._pending,
            run_one=self._run_one,
            name="long-training",
            interval=interval,
        )
        self.nominations_finished = self.draining

    def _pending(self, *, allow_new):
        study = self.root / "study/study.json"
        if not study.exists() or not _read(study).get("long_training"):
            return []
        if self.manager is None:
            self.manager = manager(self.root, self.backend)
        return self.manager.pending(
            deadline=_policy(self.root / "control")["deadline"], allow_new=allow_new
        )

    def _run_one(self, identity, *, allow_new):
        # Harness calls temporarily adjust their timeout config; do not share
        # one mutable Harness instance between child threads.
        current = (
            self.manager
            if self._provided_manager
            else manager(self.root, self.backend, wait_for_slot=True)
        )
        return current.run_one(
            deadline=_policy(self.root / "control")["deadline"],
            allow_new=allow_new,
            run_id=identity,
        )
