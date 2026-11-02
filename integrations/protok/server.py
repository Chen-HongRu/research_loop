"""Single server supervisor for an already activated, bounded ProTok lease."""

import argparse
import fcntl
import importlib.util
import json
import signal
import sys
import threading
import time
from pathlib import Path

# Runtime selection must not depend on a previous deployment's PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from integrations.protok.run import job_path  # noqa: E402
from research_loop.diagnostics import CommandDiagnostics, diagnostic_view  # noqa: E402


class ScoutScheduler:
    """Poll independently while run_one is blocked on an actual GPU evaluation."""

    def __init__(self, root, *, manager=None, interval=3):
        self.root = Path(root)
        if manager is None:
            from research_loop.scout import ScoutManager
            from integrations.protok.backend import CANDIDATE_FILES

            control = self.root / "control"
            manager = ScoutManager(
                root,
                deadline=min(
                    read(control / "continuation.json")["deadline"],
                    read(control / "ownership.json")["expires_at"],
                ),
                source_files=CANDIDATE_FILES,
            )
        self.manager = manager
        self.interval = interval
        self.stopped = threading.Event()
        self.thread = threading.Thread(
            target=self._run, daemon=True, name="scout-scheduler"
        )

    def _run(self):
        while not self.stopped.is_set():
            try:
                supervisor = self.root / "control/supervisor-status.json"
                state = read(self.root / "study/state.json")
                job = job_path(self.root, f"{state['iteration']:04d}") / "job.json"
                if (
                    (
                        not supervisor.exists()
                        or read(supervisor).get("status") == "running"
                    )
                    and job.exists()
                    and read(job).get("status") == "submitted"
                ):
                    self.manager.launch_if_due()
            except (OSError, ValueError, KeyError, TypeError) as error:
                write(
                    self.root / "control/scout/scheduler-error.json",
                    {"error": f"{type(error).__name__}: {error}", "at": time.time()},
                )
            self.stopped.wait(self.interval)

    def start(self):
        self.thread.start()

    def close(self):
        self.stopped.set()
        self.manager.close()

    def shutdown(self):
        """Settle local Scout processes before the supervisor exits."""
        self.close()
        if self.thread.ident is not None:
            self.thread.join()
        self.manager.shutdown()


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def recover_owned(root, runner, backend):
    """Reconcile existing attempts only; protected recovery forbids submission."""
    runner.reconcile_debug_allocations(root, backend)
    from integrations.protok.research_backend import run_script

    status = backend.inspect()
    research = status.get("research_outstanding", [])
    for directory in research:
        job = read(Path(directory) / "job.json")
        run_script(
            backend,
            root=root,
            iteration=Path(directory).name,
            source=root / "control/research-sources" / Path(directory).name / "source",
            execution=job["execution"],
            deadline=job["deadline"],
            recover=True,
        )
    if research:
        status = backend.inspect()
    if status.get("research_outstanding"):
        raise RuntimeError("Unreconciled research allocation remains")
    outstanding = status["outstanding"]
    for row in outstanding:
        jobs = [
            job_path(root, folder.name)
            for folder in (root / "control/jobs").glob("*")
            if read(job_path(root, folder.name) / "job.json").get("fingerprint")
            == row["fingerprint"]
            and read(job_path(root, folder.name) / "job.json").get(
                "record_name", row["fingerprint"]
            )
            == row.get("record", row["fingerprint"])
        ]
        if len(jobs) != 1:
            raise RuntimeError("Cannot bind outstanding receipt to one owned job")
        job = jobs[0]
        try:
            backend.evaluate(
                job / "source",
                job / "runtime-config.json",
                timeout_seconds=240,
                recover=True,
                **(
                    {"fresh_attempt": read(job / "job.json")["fresh_attempt"]}
                    if read(job / "job.json").get("fresh_attempt")
                    else {}
                ),
            )
        except runner.ProTokError:
            if backend.inspect()["outstanding"]:
                raise
    if backend.inspect()["outstanding"]:
        raise RuntimeError("Unreconciled job remains; no further work allowed")


def readonly_resume(root, state):
    """Request the core's locked retry for a settled read-only attempt."""
    if state.get("status") != "needs_attention" or state.get("debugging"):
        return False
    if state.get("phase") not in {"selection", "plan", "review"}:
        return False
    from research_loop.core import Study

    Study(root / "study").retry(readonly=True)
    return True


def drive(root, runner, backend, *, clock=time.time, wait=time.sleep):
    from integrations.protok.round_admission import active_round

    control = root / "control"
    lease = read(control / "continuation.json")
    if lease.get("schema") != 2 or lease.get("seconds") != 86400:
        raise ValueError("Server requires an activated schema2 lease")
    if (control / "STOP").exists():
        return "user_stop"
    # The original lease is an entry cutoff; an admitted round can finish.
    deadline = lease["deadline"]
    supervisor_path = control / "supervisor-status.json"
    previous = read(supervisor_path) if supervisor_path.exists() else {}
    first_error = previous.get("first_error")
    failed_state = previous.get("failed_state")
    owner_token = getattr(backend, "owner_token", None)
    diagnostics = CommandDiagnostics(
        control / "pipeline-diagnostics",
        secrets=(owner_token,) if isinstance(owner_token, str) else (),
    )
    last_error = (previous.get("stage"), previous.get("error"))
    delay = 5
    repaired = False
    while not (control / "STOP").exists():
        stage = "inspection"
        state = None
        try:
            ownership = control / "ownership.json"
            if ownership.exists():
                deadline = min(deadline, read(ownership)["expires_at"])
            state_path = root / "study/state.json"
            state = read(state_path) if state_path.exists() else None
            if (control / "HANDOFF").exists() and state and state["phase"] == "done":
                return "handoff"
            if clock() >= deadline and not active_round(
                root, owner_token=getattr(backend, "owner_token", None)
            ):
                return "deadline"
            if state and not repaired:
                runner.repair_completed_control(root, backend)
                repaired = True
                state = read(state_path)
            status = backend.inspect()
            if status.get("research_outstanding") or (
                status["outstanding"] and not (state and state.get("debugging"))
            ):
                raise RuntimeError("Outstanding job requires explicit reconciliation")
            spent = status["used_gpu_seconds"] - lease["initial_gpu_seconds"]
            if spent < 0:
                raise RuntimeError("Historical GPU accounting decreased")
            reserve = backend._config()["reserve_gpu_seconds"]
            fresh = state is None or state["phase"] == "done"
            minimum = 60 if (control / "research-policy.json").exists() else 1800
            if fresh and spent + minimum + reserve > lease["additional_gpu_seconds"]:
                return "budget"
            if state and state.get("status") == "stopped":
                return "attention"
            if state and (
                state.get("status") == "needs_attention" or state == failed_state
            ):
                if readonly_resume(root, state):
                    # Review publication may have completed the iteration. Re-read
                    # state and admission before starting any subsequent agent call.
                    failed_state = None
                    continue
                raise RuntimeError("Retained effects require explicit reconciliation")
            if not (root / "study/study.json").exists():
                stage = "initialization"
                runner.init_study(root, backend)
            write(
                supervisor_path,
                {"status": "running", "at": clock(), "first_error": first_error},
            )
            stage = "iteration"
            state = runner.run_one(root, backend)
            failed_state = None
            delay = 5
            if state["status"] == "needs_attention":
                raise RuntimeError("Iteration requires attention")
            if state["status"] != "ready":
                return "review_stop"
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            first_error = first_error or message
            if (stage, message) != last_error:
                diagnostics.record_error(error, stage=stage, owner="pipeline")
                last_error = (stage, message)
            if stage in {"iteration", "initialization"}:
                path = root / "study/state.json"
                failed_state = state
                try:
                    failed_state = read(path) if path.exists() else state
                except (OSError, ValueError) as state_error:
                    message += f"; state unavailable: {state_error}"
            write(
                supervisor_path,
                {
                    "status": "blocked",
                    "at": clock(),
                    "stage": stage,
                    "first_error": first_error,
                    "error": message,
                    "failed_state": failed_state,
                    "diagnostics": diagnostics.view(owner="pipeline"),
                },
            )
            # Polling only reads receipts. Execute remains blocked until explicit
            # reconciliation changes its retained state; core budgets bound retries.
            wait(delay if clock() >= deadline else min(delay, deadline - clock()))
            delay = min(delay * 2, 60)
    return "user_stop"


def finish_long_training(root, scheduler, reason, *, wait=time.sleep):
    """Finish eligible long work after the short lane has naturally stopped."""
    if reason not in {"budget", "deadline", "review_stop"}:
        return reason, None
    control = root / "control"
    scheduler.finish_nominations()
    while True:
        if (control / "STOP").exists():
            return "user_stop", None
        if (control / "HANDOFF").exists():
            return "handoff", None
        status = scheduler.status()
        if status["status"] == "blocked":
            return "attention", status.get("reason")
        if status["status"] == "finished":
            return reason, None
        wait(3)


def final_summary(root, runner, backend, reason, *, first_error=None):
    """Publish unknown accounting rather than lose the summary on inspect failure."""
    control = root / "control"
    errors = []

    def attempt(label, action):
        try:
            return action()
        except Exception as error:
            errors.append(f"{label}: {type(error).__name__}: {error}")
            return None

    status = attempt("inspection", backend.inspect)
    lease = attempt("lease", lambda: read(control / "continuation.json"))
    supervisor = attempt(
        "supervisor status",
        lambda: (
            read(control / "supervisor-status.json")
            if (control / "supervisor-status.json").exists()
            else {}
        ),
    )
    best = attempt(
        "best",
        lambda: (
            runner.read(control / "active-best.json")
            if (control / "active-best.json").exists()
            else None
        ),
    )
    used = status.get("used_gpu_seconds") if status is not None else None
    additional = (
        attempt("additional accounting", lambda: used - lease["initial_gpu_seconds"])
        if used is not None and lease is not None
        else None
    )
    write(
        control / "final-summary.json",
        {
            "at": time.time(),
            "reason": reason,
            "first_error": (supervisor or {}).get("first_error") or first_error,
            "used_gpu_seconds": used,
            "additional_gpu_seconds_used": additional,
            "outstanding": status.get("outstanding") if status is not None else None,
            "research_outstanding": status.get("research_outstanding")
            if status is not None
            else None,
            "long_training_gpu_seconds": status.get("long_training_gpu_seconds", 0)
            if status is not None
            else None,
            "long_training_outstanding": status.get("long_training_outstanding", [])
            if status is not None
            else None,
            "total_gpu_seconds": status.get("total_gpu_seconds", used)
            if status is not None
            else None,
            "accounting_status": "known" if status is not None else "unknown",
            "cleanup_errors": errors,
            "best": best,
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--runner", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    control = root / "control"
    # A copied archive or pending grant cannot become executable by starting this.
    if (control / "STOP").exists():
        raise SystemExit("Study is stopped; no action taken")
    owner = read(control / "ownership.json")
    spec = importlib.util.spec_from_file_location("trusted_protok_runner", args.runner)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    backend = runner.ProTokBackend(
        Path(owner["project"]), control, owner["owner_token"]
    )

    def stop(signum, frame):
        (control / "STOP").write_text("Supervisor stop requested; cancel owned work.\n")
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with (control / "supervisor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        reason = "attention"
        scout = None
        first_error = None
        long_training = None
        try:
            from integrations.protok.long_training_loop import LongTrainingScheduler

            long_training = LongTrainingScheduler(root, backend)
            long_training.start()
            study_config = root / "study/study.json"
            if study_config.exists() and read(study_config).get("exploration"):
                scout = ScoutScheduler(root)
                scout.start()
            reason = drive(root, runner, backend)
        except BaseException as error:
            first_error = f"{type(error).__name__}: {error}"
            write(
                control / "supervisor-error.json",
                {
                    "error": f"{type(error).__name__}: {error}",
                    "at": time.time(),
                    "diagnostics": diagnostic_view(
                        error,
                        owner="pipeline",
                        stage="supervisor",
                        secrets=(getattr(backend, "owner_token", None),),
                    ),
                },
            )
            (control / "STOP").write_text("Stopped; preserve and reconcile effects.\n")
            if isinstance(error, KeyboardInterrupt):
                reason = "user_stop"
        finally:
            try:
                if scout is not None:
                    scout.shutdown()
            except Exception as error:
                first_error = first_error or f"{type(error).__name__}: {error}"
                write(
                    control / "scout-shutdown-error.json",
                    {"error": f"{type(error).__name__}: {error}", "at": time.time()},
                )
            if long_training is not None:
                try:
                    reason, long_error = finish_long_training(
                        root, long_training, reason
                    )
                    if long_error:
                        first_error = (
                            first_error or f"Long-training scheduler: {long_error}"
                        )
                except KeyboardInterrupt:
                    reason = "user_stop"
                except Exception as error:
                    reason = "attention"
                    first_error = first_error or f"{type(error).__name__}: {error}"
                    write(
                        control / "long-training-shutdown-error.json",
                        {
                            "error": f"{type(error).__name__}: {error}",
                            "at": time.time(),
                        },
                    )
                try:
                    # HANDOFF drains only the current child; STOP is observed by
                    # the child and cancels its owned allocation.
                    long_training.shutdown()
                except KeyboardInterrupt:
                    reason = "user_stop"
                    long_training.shutdown()
                except Exception as error:
                    first_error = first_error or f"{type(error).__name__}: {error}"
                    write(
                        control / "long-training-shutdown-error.json",
                        {
                            "error": f"{type(error).__name__}: {error}",
                            "at": time.time(),
                        },
                    )
            if reason != "handoff":
                (control / "STOP").write_text(f"Supervisor stopped: {reason}\n")
                try:
                    # Terminal admission blocking can leave owned allocations even
                    # when drive returns normally. Recovery cannot submit new work.
                    recover_owned(root, runner, backend)
                except Exception as recovery_error:
                    first_error = first_error or (
                        f"{type(recovery_error).__name__}: {recovery_error}"
                    )
                    write(
                        control / "recovery-error.json",
                        {"error": str(recovery_error), "at": time.time()},
                    )
            final_summary(root, runner, backend, reason, first_error=first_error)


if __name__ == "__main__":
    main()
