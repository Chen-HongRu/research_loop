"""Supervise an operator-authorized wall-clock window without model/count budgets."""

import argparse
import asyncio
from datetime import datetime, timezone
import fcntl
import json
import math
from pathlib import Path
import signal
import subprocess
import sys
import time

from research_loop.state import write_json
from .optimization import reconcile_evaluations, resource_preflight


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def prepare_window(study, hours):
    if not math.isfinite(hours) or hours <= 0:
        raise ValueError("hours must be positive and finite")
    path = study / "run-window.json"
    if path.exists():
        window = json.loads(path.read_text())
        if time.time() >= window["deadline_unix"]:
            raise ValueError(
                "The existing window has ended; explicitly configure a new window"
            )
        return window
    started = time.time()
    window = {
        "status": "starting",
        "started_at": utc(started),
        "deadline_at": utc(started + hours * 3600),
        "deadline_unix": started + hours * 3600,
        "duration_seconds": hours * 3600,
        "max_evaluations": None,
        "token_budget": None,
    }
    write_json(path, window)
    return window


async def readiness(study, config):
    ledger = json.loads((study / "evaluation-ledger.json").read_text())
    blocked = await reconcile_evaluations(study, config, ledger["charged"])
    if blocked:
        return blocked
    try:
        return await resource_preflight(config["cases"])
    except (RuntimeError, OSError, TimeoutError) as error:
        return {"resource_blocked": True, "reason": str(error)}


async def settlement(study, config):
    ledger = json.loads((study / "evaluation-ledger.json").read_text())
    return await reconcile_evaluations(study, config, ledger["charged"])


def supervise(study, hours):
    study = Path(study).resolve(strict=True)
    with (study / "session.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (study / "STOP").exists():
            raise ValueError(
                "Operator STOP exists; inspect it before starting a window"
            )
        config = json.loads((study / "evaluator.json").read_text())
        if config.get("max_evaluations") is not None:
            raise ValueError(
                "Remove the evaluation cap before starting the time window"
            )
        window = prepare_window(study, hours)
        path = study / "run-window.json"
        deadline = window["deadline_unix"]
        child = None

        def update(status, **fields):
            window.update(status=status, updated_at=utc(time.time()), **fields)
            write_json(path, window)

        def stop(signum, frame):
            (study / "STOP").write_text("operator stop\n")

        previous = {
            sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)
        }
        try:
            while time.time() < deadline and not (study / "STOP").exists():
                blocked = asyncio.run(readiness(study, config))
                if blocked:
                    update("waiting-for-resource", reason=blocked)
                    time.sleep(min(15, max(0, deadline - time.time())))
                    continue
                if time.time() >= deadline or (study / "STOP").exists():
                    break
                update("running", reason=None)
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "benchmarks.experiment_cycle.optimization",
                        "run",
                        str(study),
                        "--allow-model",
                        "--allow-gpu",
                        "--continue",
                        "--preserve-stop",
                    ],
                    start_new_session=True,
                )
                update("running", controller_pid=child.pid)
                while child.poll() is None:
                    if time.time() >= deadline or (study / "STOP").exists():
                        if not (study / "STOP").exists():
                            (study / "STOP").write_text(
                                "authorized time window ended\n"
                            )
                        child.send_signal(signal.SIGINT)
                        try:
                            child.wait(timeout=180)
                        except subprocess.TimeoutExpired:
                            os_kill_group(child.pid)
                            child.wait(timeout=15)
                            update(
                                "needs-reconciliation",
                                reason="Controller did not settle after STOP",
                            )
                            return window
                        break
                    time.sleep(1)
                code = child.returncode
                child = None
                state = json.loads((study / "status.json").read_text())
                if state.get("cleanup_error"):
                    update(
                        "needs-reconciliation",
                        exit_code=code,
                        reason=state["cleanup_error"],
                    )
                    return window
                if (study / "STOP").exists():
                    break
                # Exit 75 is the adapter's explicit resource-blocked condition.
                # Unexpected controller faults are retained for diagnosis.
                if code not in (0, 75) or state.get("cleanup_error"):
                    update(
                        "blocked",
                        exit_code=code,
                        reason=state.get("error") or state.get("cleanup_error"),
                    )
                    return window
                if code == 0 and state.get("status") == "completed":
                    result_path = study / "result.json"
                    result = (
                        json.loads(result_path.read_text())
                        if result_path.exists()
                        else {}
                    )
                    request = result.get("human_request", {})
                    if request.get("status") == "blocked" or (
                        result.get("study_action") == "stop"
                        and result.get("goal") != "achieved"
                    ):
                        update(
                            "blocked",
                            exit_code=code,
                            reason=request.get("reason") or result.get("stop_reason"),
                        )
                        return window
                # A finished lead iteration is not the end of the user's window.
                # The next admission keeps its thread, reviewed knowledge and costs.
                update("continuing", exit_code=code)
                time.sleep(min(1, max(0, deadline - time.time())))
            if time.time() >= deadline and not (study / "STOP").exists():
                (study / "STOP").write_text("authorized time window ended\n")
            blocked = asyncio.run(settlement(study, config))
            if blocked:
                update(
                    "needs-reconciliation", ended_at=utc(time.time()), reason=blocked
                )
                return window
            if time.time() >= deadline:
                if not (study / "STOP").exists():
                    (study / "STOP").write_text("authorized time window ended\n")
                update(
                    "completed",
                    ended_at=utc(time.time()),
                    reason="10-hour authorization window ended"
                    if hours == 10
                    else "Authorization window ended",
                )
            else:
                update("stopped", ended_at=utc(time.time()), reason="Operator STOP")
            return window
        finally:
            if child is not None and child.poll() is None:
                child.send_signal(signal.SIGINT)
                child.wait(timeout=180)
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def os_kill_group(pid):
    import os

    os.killpg(pid, signal.SIGKILL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study", type=Path)
    parser.add_argument("--hours", type=float, required=True)
    parser.add_argument("--allow-model", action="store_true")
    parser.add_argument("--allow-gpu", action="store_true")
    args = parser.parse_args()
    if not args.allow_model or not args.allow_gpu:
        parser.error("requires --allow-model and --allow-gpu")
    try:
        result = supervise(args.study, args.hours)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] in {"blocked", "needs-reconciliation"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
