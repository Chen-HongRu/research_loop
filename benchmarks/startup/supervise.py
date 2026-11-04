"""Durable bounded supervisor for the startup research adapter (no model polling)."""

import argparse
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import time


def publish(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def mirror(root, host, destination, record_root=None):
    record_root = record_root or root
    archive = record_root.with_suffix(".tar.gz")
    subprocess.run(
        [
            "tar",
            "--exclude=.git",
            "--exclude=.venv",
            "--exclude=.tmp",
            "--exclude=__pycache__",
            "-czf",
            str(archive),
            "-C",
            str(root.parent),
            root.name,
            *[
                p.name
                for p in (
                    record_root.with_name(record_root.name + "-supervisor.json"),
                    record_root.with_suffix(".stdout"),
                    record_root.with_suffix(".stderr"),
                )
                if p.exists()
            ],
        ],
        check=True,
        timeout=120,
    )
    subprocess.run(
        [
            "scp",
            "-o",
            "BatchMode=yes",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "ConnectTimeout=10",
            str(archive),
            host + ":" + destination,
        ],
        check=True,
        timeout=120,
    )


def supervise(root, seconds, host, destination, command, *, attempt_id=None):
    if attempt_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]+", attempt_id):
        raise ValueError("Invalid supervision attempt identity")
    record_root = root.with_name(root.name + "-" + attempt_id) if attempt_id else root
    state_path = record_root.with_name(record_root.name + "-supervisor.json")
    state = {
        "pid": os.getpid(),
        "status": "starting",
        "root": str(root),
        "started_at": time.time(),
        "budget_seconds": seconds,
        "remote_artifacts": destination,
        "artifact_mirror_status": "pending",
    }
    if state_path.exists():
        raise ValueError(
            "Supervisor receipt already exists; reconcile instead of replaying"
        )
    started = time.monotonic()
    publish(state_path, state)
    child = None
    try:
        with (
            record_root.with_suffix(".stdout").open("x") as out,
            record_root.with_suffix(".stderr").open("x") as err,
        ):
            child = subprocess.Popen(
                command, stdout=out, stderr=err, start_new_session=True
            )
            state.update(status="running", child_pid=child.pid)
            while child.poll() is None:
                remaining = seconds - (time.monotonic() - started)
                state["observed_at"] = time.time()
                publish(state_path, state)
                if remaining <= 0:
                    state["status"] = "timeout_needs_reconciliation"
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait(timeout=5)
                    break
                try:
                    child.wait(timeout=min(10, remaining))
                except subprocess.TimeoutExpired:
                    pass
            state["exit_code"] = child.returncode
            if state["status"] == "running":
                summary = root / "summary.json"
                result = json.loads(summary.read_text()) if summary.is_file() else {}
                state["status"] = (
                    "completed"
                    if child.returncode == 0
                    and result.get("status") in {"completed", "budget_exhausted"}
                    else "failed"
                )
                state["study_outcome"] = result.get("status")
    except BaseException as error:
        state.update(
            status="supervisor_error", error=f"{type(error).__name__}: {error}"
        )
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            state["execution_effects"] = "reconciliation_required"
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
            state["exit_code"] = child.returncode
    finally:
        # Publish terminal state BEFORE archiving: transfer failure must never leave running.
        state["finished_at"] = time.time()
        state["elapsed_seconds"] = time.monotonic() - started
        publish(state_path, state)
        if root.exists():
            try:
                mirror(root, host, destination, record_root)
                state["artifact_mirror_status"] = "completed"
            except Exception as error:
                state["artifact_mirror_status"] = "failed"
                state["artifact_mirror_error"] = f"{type(error).__name__}: {error}"
        publish(state_path, state)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--budget-seconds", type=int, required=True)
    parser.add_argument(
        "--attempt-id",
        help="New supervisor receipt identity for an authorized recovery",
    )
    parser.add_argument("--host", required=True)
    parser.add_argument("--remote-archive", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or not 1 <= args.budget_seconds <= 36000:
        parser.error("Require a command and a wall budget between 1 and 36000 seconds")

    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"Supervisor received signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    state = supervise(
        args.root.resolve(),
        args.budget_seconds,
        args.host,
        args.remote_archive,
        command,
        attempt_id=args.attempt_id,
    )
    print(json.dumps(state), flush=True)
    return 0 if state["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
