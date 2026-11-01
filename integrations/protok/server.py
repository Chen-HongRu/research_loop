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
                state = read(self.root / "study/state.json")
                job = job_path(self.root, f"{state['iteration']:04d}") / "job.json"
                if job.exists() and read(job).get("status") == "submitted":
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


def drive(root, runner, backend, *, clock=time.time):
    control = root / "control"
    lease = read(control / "continuation.json")
    if lease.get("schema") != 2 or lease.get("seconds") != 86400:
        raise ValueError("Server requires an activated schema2 lease")
    if (root / "study/state.json").exists():
        runner.repair_completed_control(root, backend)
    while not (control / "STOP").exists():
        state_path = root / "study/state.json"
        state = read(state_path) if state_path.exists() else None
        # This marker is checked only between complete, published iterations.
        if (control / "HANDOFF").exists() and state and state["phase"] == "done":
            return "handoff"
        status = backend.inspect()
        if status.get("research_outstanding") or (
            status["outstanding"] and not (state and state.get("debugging"))
        ):
            raise RuntimeError("Outstanding job requires explicit reconciliation")
        remaining = lease["deadline"] - clock()
        spent = status["used_gpu_seconds"] - lease["initial_gpu_seconds"]
        if spent < 0:
            raise RuntimeError("Historical GPU accounting decreased")
        reserve = backend._config()["reserve_gpu_seconds"]
        fresh = state is None or state["phase"] == "done"
        minimum = 60 if (control / "research-policy.json").exists() else 1800
        if fresh and (
            remaining < minimum
            or spent + minimum + reserve > lease["additional_gpu_seconds"]
        ):
            return "budget"
        if not (root / "study/study.json").exists():
            runner.init_study(root, backend)
        state = runner.read(root / "study/state.json")
        if state["status"] == "stopped":
            return "attention"
        state = runner.run_one(root, backend)
        if state["status"] != "ready":
            return "review_stop"
    return "user_stop"


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
        try:
            study_config = root / "study/study.json"
            if study_config.exists() and read(study_config).get("exploration"):
                scout = ScoutScheduler(root)
                scout.start()
            reason = drive(root, runner, backend)
        except BaseException as error:
            write(
                control / "supervisor-error.json",
                {"error": f"{type(error).__name__}: {error}", "at": time.time()},
            )
            (control / "STOP").write_text("Stopped; preserve and reconcile effects.\n")
            try:
                recover_owned(root, runner, backend)
            except Exception as recovery_error:
                write(
                    control / "recovery-error.json",
                    {"error": str(recovery_error), "at": time.time()},
                )
            if isinstance(error, KeyboardInterrupt):
                reason = "user_stop"
        finally:
            if scout is not None:
                scout.shutdown()
            if reason != "handoff":
                (control / "STOP").write_text(f"Supervisor stopped: {reason}\n")
            status = backend.inspect()
            lease = read(control / "continuation.json")
            write(
                control / "final-summary.json",
                {
                    "at": time.time(),
                    "reason": reason,
                    "used_gpu_seconds": status["used_gpu_seconds"],
                    "additional_gpu_seconds_used": status["used_gpu_seconds"]
                    - lease["initial_gpu_seconds"],
                    "outstanding": status["outstanding"],
                    "best": runner.read(control / "active-best.json")
                    if (control / "active-best.json").exists()
                    else None,
                },
            )


if __name__ == "__main__":
    main()
