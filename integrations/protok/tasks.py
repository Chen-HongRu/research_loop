"""User-directed investigations and independently funded background long training."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid

from research_loop import Study, initialize
from research_loop.harness import (
    CodexHarness,
    HarnessConfig,
    HarnessError,
    publish_completed_response,
)
from research_loop.long_training import admission_path, admitted_round
from research_loop.tasks import TaskQueue
from research_loop.protocol import validate_response
from research_loop.schema import omit_fields

from integrations.protok.agent_output import REPORT_SCHEMA, report_response

from integrations.protok.backend import CANDIDATE_FILES, ProTokBackend, ProTokError
from integrations.protok.continuation import _load_backend
from integrations.protok.long_training import execute, round_authorization
from integrations.protok.long_training_loop import LongTrainingScheduler, study_policy


def _read(path):
    return json.loads(Path(path).read_text())


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _git(path, *arguments):
    return subprocess.check_output(
        ["git", "-C", str(path), *arguments], text=True, stderr=subprocess.PIPE
    ).strip()


class ManualLongHarness:
    """Author one candidate, then run the protected evaluator outside the Agent."""

    def __init__(self, root, backend, request, delegate):
        self.root, self.backend = Path(root), backend
        self.request, self.delegate = request, delegate
        self.folder = self.root / "study/tasks" / request["id"]

    def admit_iteration(self, iteration):
        if iteration != "0001":
            raise ProTokError("Only one investigation is authorized per manual task")
        token = admitted_round(self.folder)
        identity = round_authorization(
            self.backend,
            self.root,
            {"manual_task_id": self.request["id"]},
            token,
        )
        if token:
            return token["token"]
        now = time.time()
        cutoff = self.request["budget"]["deadline"]
        if now >= cutoff:
            raise ProTokError("Manual task admission cutoff reached")
        record = {
            "token": {
                "version": 1,
                "id": self.request["id"],
                "nonce": uuid.uuid4().hex,
            },
            "child": str((self.folder / "study").resolve()),
            "request": self.request,
            "cutoff": cutoff,
            "entered_at": now,
            "authorization": identity,
        }
        _write(admission_path(self.folder), record)
        return record["token"]

    def commit_iteration(self, iteration, token):
        existing = admitted_round(self.folder)
        if existing:
            self.admit_iteration(iteration)
            return existing["committed_at"]
        record = _read(admission_path(self.folder))
        identity = round_authorization(
            self.backend, self.root, {"manual_task_id": self.request["id"]}
        )
        now = time.time()
        if (
            record["token"] != token
            or record["request"] != self.request
            or record["authorization"] != identity
            or now >= record["cutoff"]
        ):
            raise ProTokError("Manual admission changed before commit")
        _write(admission_path(self.folder), record | {"committed_at": now})
        return now

    def call_allowance(self, remaining):
        # Queue waiting does not consume an admitted task's Agent-call allowance.
        return max(remaining, 3600) if admitted_round(self.folder) else remaining

    def _execution(self, workspace, *, recover=False):
        manifest = (
            self.folder / "study/iterations/0001/context/manual-long-request.json"
        )
        request = _read(manifest)
        job = self.root / "control/manual-training-jobs" / request["id"] / "job.json"
        deadline = (
            _read(job)["deadline"]
            if job.exists()
            else time.time() + self.request["budget"]["gpu_seconds_per_run"]
        )
        return execute(
            self.backend,
            self.root,
            request,
            Path(workspace),
            deadline=deadline,
            recover=recover,
        )

    def validate_completed(self, result, *, workspace, run_dir):
        retained = self._execution(workspace, recover=True)
        if retained != result:
            raise HarnessError(
                "Manual Execute differs from the trusted retained result"
            )
        return result

    def _freeze_and_execute(self, workspace):
        from integrations.protok.run import candidate_bytes, freeze_source

        baseline = self.request["source"]["revision"]
        revision, _ = freeze_source(
            self.folder / "baseline", workspace, self.folder / "source", baseline
        )
        config = self.backend._config()
        fixed, _ = _load_backend(
            self.backend.project,
            {
                "evaluator_sha256": config["locked_files"][
                    "scripts/autoresearch_se3_eval.py"
                ]
            },
        )
        fingerprint = fixed.fingerprint(config, candidate_bytes(self.folder / "source"))
        policy = study_policy(self.root, grant=self.request["budget"])
        actual = {
            "id": "L"
            + hashlib.sha256(str(self.folder.resolve()).encode()).hexdigest()[:16],
            "manual_task_id": self.request["id"],
            "source_iteration": "0001",
            "source_project": str((self.folder / "baseline").resolve()),
            "source_workspace": str((self.folder / "source").resolve()),
            "source_workspace_ref": revision,
            "candidate": {
                "id": fingerprint,
                "artifact": {"path": CANDIDATE_FILES[2], "version": fingerprint},
            },
            "recipe": policy["recipe"],
            "evaluation": policy["evaluation"],
        }
        _write(
            self.folder / "study/iterations/0001/context/manual-long-request.json",
            actual,
        )
        return self._execution(workspace)

    def recover_authored(self, workspace, attempt):
        """Finish frozen authoring only with proof that no allocation was prepared."""
        from integrations.protok.run import candidate_bytes, check_scope, signatures

        workspace = Path(workspace)
        authored = _read(self.folder / "authored.json")
        token = admitted_round(self.folder)
        if token is None or (self.root / "control/STOP").exists():
            raise ProTokError(
                "First manual allocation requires an admitted, resumed task"
            )
        round_authorization(
            self.backend, self.root, {"manual_task_id": self.request["id"]}, token
        )
        if (
            authored["attempt"] != str(attempt)
            or authored["baseline"] != self.request["source"]["revision"]
            or signatures(candidate_bytes(workspace)) != authored["source_signatures"]
        ):
            raise ProTokError("Preserved manual authoring source changed")
        check_scope(workspace, authored["baseline"])
        run_id = (
            "L" + hashlib.sha256(str(self.folder.resolve()).encode()).hexdigest()[:16]
        )
        record = self.root / "control/manual-training-jobs" / run_id
        # job/state precede every submission. Missing/corrupt submitted metadata
        # cannot be treated as an unallocated task.
        if record.exists():
            if any(
                (record / name).exists()
                for name in ("job.json", "state.json", "receipt.json", "blocked.json")
            ):
                raise ProTokError("Retained allocation must reconcile its original job")
            record.rename(
                record.with_name(record.name + ".pre-allocation." + uuid.uuid4().hex)
            )
        manifest = (
            self.folder / "study/iterations/0001/context/manual-long-request.json"
        )
        if manifest.exists():
            return self._execution(workspace)
        source = self.folder / "source"
        if source.exists():
            source.rename(self.folder / ("source.pre-allocation." + uuid.uuid4().hex))
        return self._freeze_and_execute(workspace)

    def run(self, **kwargs):
        from integrations.protok.run import candidate_bytes, check_scope, signatures

        phase = kwargs["run_dir"].parent.name
        token = admitted_round(self.folder)
        if token is None:
            raise ProTokError("Manual child round is not committed")
        round_authorization(
            self.backend, self.root, {"manual_task_id": self.request["id"]}, token
        )
        allocation = self.request["budget"]["gpu_seconds_per_run"]
        kwargs["prompt"] += (
            "\nManual long-training controller: timeout_seconds is exactly "
            + str(allocation)
            + " Slurm GPU-allocation seconds. Queue waiting has no separate wall cutoff; "
            "authoring and Review retain their configured CLI timeout. Preserve the fixed "
            "fresh seed42/10k/full4491 protocol. Only the three candidate files may change. "
            "No short evaluation, submission API, SSH, package installation or direct training. "
            "During Execute implement the requested candidate, then report authoring outcome "
            "and markdown only; the trusted controller runs long training after authoring ends. "
            "The parent experiment is an external input, not a child graph reference."
        )
        if phase != "execute":
            if phase != "plan":
                return self.delegate.run(**kwargs)
            schema = omit_fields(
                kwargs["schema"],
                "base_experiment",
                "local_evaluation",
                "references",
                "timeout_seconds",
            )
            outer = Path(kwargs["run_dir"])
            agent = outer / "agent"
            agent.mkdir()
            shutil.copyfile(outer / "context.json", agent / "context.json")
            _write(agent / "schema.json", schema)
            result = self.delegate.run(
                **{
                    **kwargs,
                    "run_dir": agent,
                    "schema": schema,
                    "prompt": kwargs["prompt"]
                    + "\nPlan output override: omit base_experiment, local_evaluation, references and timeout_seconds. The controller supplies the fixed external source, no local evaluation, no graph references, and the authorized GPU allocation timeout.\n",
                }
            )
            validate_response("plan", result, schema=schema)
            result = {
                **result,
                "local_evaluation": None,
                "references": [],
                "timeout_seconds": allocation,
            }
            if "base_experiment" in kwargs["schema"]["properties"]:
                result["base_experiment"] = None
            publish_completed_response(outer, result)
            return result
        outer, workspace = Path(kwargs["run_dir"]), Path(kwargs["workspace"])
        manifest = (
            self.folder / "study/iterations/0001/context/manual-long-request.json"
        )
        if manifest.exists() or (self.folder / "source").exists():
            raise ProTokError(
                "Retained manual execution requires same-job reconciliation"
            )
        baseline = self.request["source"]["revision"]
        before = candidate_bytes(workspace)
        if not self.request["allow_changes"]:
            for name, content in before.items():
                committed = subprocess.check_output(
                    [
                        "git",
                        "-C",
                        str(self.folder / "baseline"),
                        "show",
                        baseline + ":" + name,
                    ]
                )
                if content != committed:
                    raise ProTokError(
                        "Fixed-candidate task differs from its frozen input"
                    )
        if self.request["allow_changes"]:
            agent = outer / "agent"
            agent.mkdir()
            shutil.copyfile(outer / "context.json", agent / "context.json")
            _write(agent / "schema.json", REPORT_SCHEMA)
            authored = report_response(
                self.delegate.run(
                    **{
                        **kwargs,
                        "run_dir": agent,
                        "schema": REPORT_SCHEMA,
                        "prompt": kwargs["prompt"]
                        + "\nAuthoring output override: return only outcome and markdown. Mark completed only when candidate authoring finished; otherwise incomplete. Do not output artifacts, measurements or scores. The controller checks source scope and runs the authorized long-training evaluation.\n",
                    }
                )
            )
            if authored["outcome"] != "completed":
                raise ProTokError(
                    "Manual candidate authoring did not complete without scores"
                )
        check_scope(workspace, baseline)
        if not self.request["allow_changes"] and candidate_bytes(workspace) != before:
            raise ProTokError("Fixed-candidate manual task changed candidate bytes")
        _write(
            self.folder / "authored.json",
            {
                "attempt": str(outer),
                "baseline": baseline,
                "source_signatures": signatures(candidate_bytes(workspace)),
            },
        )
        result = self._freeze_and_execute(workspace)
        publish_completed_response(outer, result)
        return result


class ManualTrainingManager:
    def __init__(self, root, backend, *, delegate=None):
        self.root, self.backend, self.delegate = Path(root), backend, delegate

    def run_one(self, *, allow_new=True):
        Study(self.root / "study")
        control = self.root / "control"
        control.mkdir(parents=True, exist_ok=True)
        with (control / "manual-task-runner.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if (control / "STOP").exists():
                return None
            queue = TaskQueue(self.root / "study")
            request = queue.claim(
                "long_training",
                allow_new=allow_new and not (control / "HANDOFF").exists(),
            )
            if request is None:
                return None
            folder = self.root / "study/tasks" / request["id"]
            child, baseline = folder / "study", folder / "baseline"
            if not (child / "study.json").exists():
                if not baseline.exists():
                    subprocess.run(
                        [
                            "git",
                            "clone",
                            "--shared",
                            "--quiet",
                            request["source"]["project"],
                            str(baseline),
                        ],
                        check=True,
                    )
                    _git(
                        baseline, "checkout", "--detach", request["source"]["revision"]
                    )
                if _git(baseline, "rev-parse", "HEAD") != request["source"]["revision"]:
                    raise ProTokError(
                        "Manual task baseline differs from its frozen input"
                    )
                parent = _read(self.root / "study/study.json")
                policy = study_policy(self.root, grant=request["budget"])
                initialize(
                    child,
                    goal=(
                        "Complete the user-directed long-training experiment below. Keep all "
                        "protected training/data/evaluation conditions fixed. Only edit "
                        + ", ".join(CANDIDATE_FILES)
                        + ". New source can train directly without a short score or automatic "
                        "nomination. Independently Review the complete or partial trajectory; "
                        "retain negative results, limitations and exact checkpoint evidence.\n\n"
                        + request["instruction"]
                    ),
                    workspace=baseline,
                    harness=HarnessConfig(**parent["harness"]),
                    allow_write=True,
                    global_evaluation=policy["evaluation"],
                    max_iterations=1,
                    max_calls=12,
                    max_seconds=max(3600, policy["max_seconds_per_run"]),
                )
                _write(
                    child / "study.json",
                    _read(child / "study.json") | {"manual_task": request},
                )
            if _read(child / "study.json").get("manual_task") != request:
                raise ProTokError("Manual child request changed")
            admitted = admitted_round(folder)
            if admitted is None and (not allow_new or (control / "HANDOFF").exists()):
                return None
            current = next(
                row for row in queue.snapshot() if row["id"] == request["id"]
            )
            if current["status"] == "expired":
                return {"status": "expired", "task_id": request["id"]}
            delegate = self.delegate or CodexHarness(
                HarnessConfig(**_read(child / "study.json")["harness"])
            )
            try:
                state = Study(
                    child, ManualLongHarness(self.root, self.backend, request, delegate)
                ).run()
            except ProTokError:
                row = next(
                    row for row in queue.snapshot() if row["id"] == request["id"]
                )
                if row["status"] == "expired":
                    return {"status": "expired", "task_id": request["id"]}
                raise
            row = next(row for row in queue.snapshot() if row["id"] == request["id"])
            return row["result"] or state

    def recover(self, task_id):
        """Reconcile only the original allocation and retain the original Execute attempt."""
        Study(self.root / "study")
        folder = self.root / "study/tasks" / task_id
        request = TaskQueue(self.root / "study").request(task_id)
        with (self.root / "control/manual-task-runner.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            child = Study(folder / "study")
            with child._lock():
                state = child.status()
                if state["phase"] != "execute" or not state["calls"].get("execute"):
                    raise ProTokError("Manual recovery requires a retained Execute")
                attempt = child.root / state["calls"]["execute"]
                child._check_attempt_stopped(attempt)
                metadata = _read(child.root / "iterations/0001/iteration.json")
                harness = ManualLongHarness(
                    self.root, self.backend, request, self.delegate
                )
                manifest = (
                    child.root / "iterations/0001/context/manual-long-request.json"
                )
                actual = _read(manifest) if manifest.exists() else None
                job = (
                    self.root
                    / "control/manual-training-jobs"
                    / actual["id"]
                    / "job.json"
                    if actual
                    else None
                )
                result = (
                    harness._execution(metadata["workspace"], recover=True)
                    if job is not None and job.exists()
                    else harness.recover_authored(metadata["workspace"], attempt)
                )
                publish_completed_response(attempt, result)
            child.retry()
        return (
            self.run_one(allow_new=False)
            if not (self.root / "control/STOP").exists()
            else {"reconciled": task_id, "review_pending": True}
        )


class ManualTrainingScheduler:
    def __init__(self, root, backend, *, manager=None, interval=3):
        self.root, self.backend = Path(root), backend
        self.manager = manager or ManualTrainingManager(root, backend)
        self.interval = interval
        self.stopped, self.draining = threading.Event(), threading.Event()
        self._status = {"status": "idle", "reason": None}
        self.thread = threading.Thread(
            target=self._run, daemon=True, name="manual-training"
        )

    def status(self):
        return dict(self._status)

    def start(self):
        self.thread.start()

    def finish_nominations(self):
        self.draining.set()

    def _run(self):
        try:
            while not self.stopped.is_set():
                if (self.root / "control/STOP").exists():
                    self._status = {"status": "finished", "reason": "user_stop"}
                    return
                handoff = (self.root / "control/HANDOFF").exists()
                if (self.root / "study/study.json").exists():
                    self._status = {"status": "running", "reason": None}
                    result = self.manager.run_one(allow_new=not handoff)
                    if result is not None and result.get("status") in {
                        "needs_attention",
                        "stopped",
                    }:
                        raise ProTokError("Manual child requires attention")
                    if result is None and (self.draining.is_set() or handoff):
                        self._status = {
                            "status": "finished",
                            "reason": "handoff" if handoff else "no_pending",
                        }
                        return
                elif self.draining.is_set() or handoff:
                    self._status = {"status": "finished", "reason": "no_study"}
                    return
                self._status = {"status": "idle", "reason": None}
                self.stopped.wait(self.interval)
        except Exception as error:
            self._status = {
                "status": "blocked",
                "reason": f"{type(error).__name__}: {error}",
            }
            _write(
                self.root / "control/manual-training-error.json",
                self._status | {"at": time.time()},
            )

    def shutdown(self):
        self.stopped.set()
        if self.thread.ident is not None:
            self.thread.join()


class ExperimentScheduler:
    """One supervisor entry point, with independent automatic and manual long workers."""

    def __init__(self, root, backend, *, automatic=None, manual=None):
        self.workers = (
            automatic or LongTrainingScheduler(root, backend),
            manual or ManualTrainingScheduler(root, backend),
        )

    def start(self):
        for worker in self.workers:
            worker.start()

    def status(self):
        rows = [worker.status() for worker in self.workers]
        blocked = next((row for row in rows if row["status"] == "blocked"), None)
        return blocked or {
            "status": "finished"
            if all(row["status"] == "finished" for row in rows)
            else "running",
            "reason": None,
        }

    def finish_nominations(self):
        for worker in self.workers:
            worker.finish_nominations()

    def shutdown(self):
        for worker in self.workers:
            worker.shutdown()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--recover", metavar="TASK_ID")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    owner = _read(root / "control/ownership.json")
    backend = ProTokBackend(
        Path(owner["project"]), root / "control", owner["owner_token"]
    )
    manager = ManualTrainingManager(root, backend)
    result = manager.recover(args.recover) if args.recover else manager.run_one()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
