"""Switch an existing supervisor only after its complete iteration is published."""

import argparse
import fcntl
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from research_loop.exploration import validate_exploration  # noqa: E402
from research_loop.protok import ProTokBackend  # noqa: E402


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command(*args):
    return subprocess.run(
        args, check=True, capture_output=True, text=True, timeout=120
    ).stdout.strip()


def check_boundary(root, backend):
    """The caller must hold runner.lock throughout this check and service stop."""
    control = root / "control"
    if (control / "STOP").exists():
        raise RuntimeError("Existing STOP requires reconciliation; do not clear it")
    state = read(root / "study/state.json")
    if state["status"] != "ready" or state["phase"] != "done":
        raise RuntimeError("An investigation is still active or needs attention")
    folder = root / "study/iterations" / f"{state['iteration']:04d}"
    if (
        not (folder / "publication.json").exists()
        or read(folder / "iteration.json")["status"] != "reviewed"
    ):
        raise RuntimeError("Latest Review has not been fully published")
    runner = read(control / "runner.json")
    if runner["status"] != "completed" or runner.get("iteration") != state["iteration"]:
        raise RuntimeError("Runner has not finished control publication")
    job = control / "jobs" / f"{state['iteration']:04d}" / "job.json"
    if job.exists() and read(job)["status"] != "completed":
        raise RuntimeError("Latest job is not completed")
    status = backend.inspect()
    if status["outstanding"]:
        raise RuntimeError("Outstanding evaluation effects prevent a handoff")
    return state


def check_service_state(root, state, allow_stopped):
    if state == "active":
        return
    receipt = (
        root / "control/reconciliations/readonly-proposal-handoff-0007/result.json"
    )
    if (
        not allow_stopped
        or state != "inactive"
        or not receipt.exists()
        or read(receipt).get("status") != "restored_published_boundary"
    ):
        raise RuntimeError(
            "Expected active supervisor or explicit reconciled read-only stop"
        )


def deploy(root, runtime, *, max_wait=3600, allow_stopped=False):
    root, runtime = root.resolve(), runtime.resolve()
    control = root / "control"
    service = "research-loop-protok.service"
    dropin = Path("/etc/systemd/system") / (service + ".d/50-exploration-v1.conf")
    if dropin.exists():
        raise RuntimeError(
            "Exploration deployment already exists; inspect before rerunning"
        )
    manifest = read(runtime / "verification/manifest.json")
    for name, expected in manifest["files"].items():
        if digest(runtime / name) != expected:
            raise RuntimeError(f"Untested runtime change: {name}")
    check_service_state(
        root,
        command("systemctl", "show", service, "-p", "ActiveState", "--value"),
        allow_stopped,
    )
    owner = read(control / "ownership.json")
    backend = ProTokBackend(Path(owner["project"]), control, owner["owner_token"])
    protected = {
        name: digest(control / name) for name in ["ownership.json", "continuation.json"]
    }
    lease = read(control / "continuation.json")
    stop_waiting = min(time.time() + max_wait, lease["deadline"] - 1800)
    print("Waiting for complete iteration publication; GPU work continues.", flush=True)
    with (control / "exploration-deploy.lock").open("a") as deployment_lock:
        fcntl.flock(deployment_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (control / "runner.lock").open("a") as runner_lock:
            while True:
                try:
                    fcntl.flock(runner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.time() >= stop_waiting or (control / "STOP").exists():
                        raise RuntimeError(
                            "Handoff window ended; existing work left unchanged"
                        )
                    time.sleep(0.1)
            state = check_boundary(root, backend)
            iteration = state["iteration"]
            backup = (
                control / "reconciliations" / f"exploration-v1-after-{iteration:04d}"
            )
            backup.mkdir(parents=True, exist_ok=False)
            paths = [
                "study/study.json",
                "study/state.json",
                "study/goal.md",
                "study/research.md",
                "study/routes/index.json",
                "control/active-best.json",
                "control/runner.json",
                "control/journal.jsonl",
            ] + [
                str(p.relative_to(root))
                for p in (root / "study/routes").glob("*/research.md")
            ]
            for name in paths:
                target = backup / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / name, target)
            for name in [
                "study/research_inputs.json",
                "control/scout/latest.json",
                "control/supervisor-error.json",
                "control/final-summary.json",
            ]:
                if (root / name).exists():
                    target = backup / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(root / name, target)
            unit = Path(
                command("systemctl", "show", service, "-p", "FragmentPath", "--value")
            )
            shutil.copyfile(unit, backup / "service.before")
            record = {
                "status": "prepared",
                "at": time.time(),
                "completed_iteration": iteration,
                "runtime": str(runtime),
                "protected_hashes": protected,
                "backup": str(backup),
            }
            write(backup / "handoff.json", record)
            print(
                f"Iteration {iteration:04d} published; switching supervisor at boundary.",
                flush=True,
            )
            try:
                command("systemctl", "stop", service)
                if command(
                    "systemctl", "show", service, "-p", "ActiveState", "--value"
                ) not in {"inactive", "failed"}:
                    raise RuntimeError("Old supervisor is not stopped")
                if (
                    read(root / "study/state.json") != state
                    or backend.inspect()["outstanding"]
                ):
                    raise RuntimeError(
                        "State/effects changed during handoff; preserve STOP"
                    )
                error_path = control / "supervisor-error.json"
                old_error = backup / "control/supervisor-error.json"
                if error_path.exists() and (
                    not old_error.exists() or digest(error_path) != digest(old_error)
                ):
                    error = read(error_path)["error"]
                    if not (
                        error.startswith("KeyboardInterrupt:")
                        or error == "ProTokError: Another ProTok runner is active"
                    ):
                        raise RuntimeError(
                            "Unexpected supervisor error during handoff; preserve STOP"
                        )
                stop = control / "STOP"
                if stop.exists():
                    if stop.read_text() not in {
                        "Supervisor stopped: user_stop\n",
                        "Supervisor stopped: attention\n",
                    }:
                        raise RuntimeError("Unrecognized stop reason; do not clear")
                    shutil.copyfile(stop, backup / "controlled-stop.txt")
                config = read(root / "study/study.json")
                config["strategy"] = {"selection": "direct", "review": "concise"}
                config["exploration"] = validate_exploration(
                    {
                        "version": 1,
                        "start_iteration": iteration + 1,
                        "cycle": [
                            "architecture",
                            "diagnostic",
                            "architecture",
                            "refinement",
                            "architecture",
                        ],
                        "priority_routes": ["R0002", "R0003"],
                    }
                )
                write(root / "study/study.json", config)
                shutil.copyfile(
                    runtime / "research-inputs/goal-exploration.md",
                    root / "study/goal.md",
                )
                shutil.copyfile(
                    runtime / "research-inputs/research_inputs.json",
                    root / "study/research_inputs.json",
                )
                scout = control / "scout"
                scout.mkdir(exist_ok=True)
                if not (scout / "latest.json").exists():
                    shutil.copyfile(
                        runtime / "research-inputs/scout-bootstrap.json",
                        scout / "latest.json",
                    )
                note = (
                    "## Operator research update, 2026-11-01\n\n"
                    "The research goal and allocation now prioritize substantive architecture and discriminating diagnostics. "
                    "Consult frozen research_inputs/scout_inputs: original ProTok capability, H early/late degradation, "
                    "depthwise H conditioning, and future conditional-diversity evaluation. "
                    "Existing X/H-Kabsch and node/pair attempts are prior art. Scalar-only tweaks belong to refinement slots. "
                    "Full 4491 historical values are not comparable rankings against this 449-protein protocol. "
                    "Prior Reviews and raw receipts remain immutable; the text below is historical context.\n\n"
                )
                for name in [
                    "research.md",
                    "routes/R0002/research.md",
                    "routes/R0003/research.md",
                ]:
                    path = root / "study" / name
                    path.write_text(note + path.read_text())
                dropin.parent.mkdir(parents=True, exist_ok=True)
                dropin.write_text(
                    "[Service]\nExecStart=\nExecStart=/root/research-loop/.venv/bin/python "
                    f"{runtime}/integrations/protok/server.py --root {root} "
                    f"--runner {runtime}/integrations/protok/run.py\n"
                )
                command("systemctl", "daemon-reload")
                if any(
                    digest(control / name) != value for name, value in protected.items()
                ):
                    raise RuntimeError("Ownership or budget changed; refuse to resume")
                if stop.exists():
                    stop.unlink()  # Only the recognized, archived, controlled boundary stop.
                record.update(status="configured", next_iteration=iteration + 1)
                write(backup / "handoff.json", record)
            except BaseException as error:
                record.update(status="failed", error=f"{type(error).__name__}: {error}")
                write(backup / "handoff.json", record)
                raise
        # Release runner.lock before the new supervisor is admitted.
        command("systemctl", "start", service)
        record.update(
            status="started",
            finished_at=time.time(),
            service_state=command("systemctl", "is-active", service),
            main_pid=command("systemctl", "show", service, "-p", "MainPID", "--value"),
        )
        write(backup / "handoff.json", record)
        print(
            json.dumps(
                {
                    k: record[k]
                    for k in [
                        "status",
                        "completed_iteration",
                        "next_iteration",
                        "service_state",
                        "main_pid",
                        "backup",
                    ]
                }
            ),
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--max-wait", type=int, default=3600)
    parser.add_argument("--allow-stopped", action="store_true")
    args = parser.parse_args()
    deploy(
        args.root,
        args.runtime,
        max_wait=args.max_wait,
        allow_stopped=args.allow_stopped,
    )


if __name__ == "__main__":
    main()
