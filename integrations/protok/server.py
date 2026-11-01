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
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


class ScoutScheduler:
    """Poll independently while run_one is blocked on an actual GPU evaluation."""

    def __init__(self, root, *, manager=None, interval=3):
        if manager is None:
            from research_loop.scout import ScoutManager

            manager = ScoutManager(root)
        self.manager = manager
        self.interval = interval
        self.stopped = threading.Event()
        self.thread = threading.Thread(
            target=self._run, daemon=True, name="scout-scheduler"
        )

    def _run(self):
        while not self.stopped.is_set():
            self.manager.launch_if_due()
            self.stopped.wait(self.interval)

    def start(self):
        self.thread.start()

    def close(self):
        self.stopped.set()
        self.manager.close()


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def recover_owned(root, runner, backend):
    """Reconcile existing attempts only; protected recovery forbids submission."""
    outstanding = backend.inspect()["outstanding"]
    for row in outstanding:
        jobs = [
            folder
            for folder in (root / "control/jobs").glob("*")
            if read(folder / "job.json").get("fingerprint") == row["fingerprint"]
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
    while not (control / "STOP").exists():
        # This marker is checked only between complete, published iterations.
        if (control / "HANDOFF").exists():
            return "handoff"
        status = backend.inspect()
        if status["outstanding"]:
            raise RuntimeError("Outstanding job requires explicit reconciliation")
        remaining = lease["deadline"] - clock()
        spent = status["used_gpu_seconds"] - lease["initial_gpu_seconds"]
        if spent < 0:
            raise RuntimeError("Historical GPU accounting decreased")
        reserve = backend._config()["reserve_gpu_seconds"]
        if remaining < 1800 or spent + 1800 + reserve > lease["additional_gpu_seconds"]:
            return "budget"
        if not (root / "study/study.json").exists():
            runner.init_study(root, backend)
        state = runner.read(root / "study/state.json")
        if state["status"] != "ready":
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
                scout.close()
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
