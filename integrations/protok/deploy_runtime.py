"""Switch the server runtime after an explicit, fully published handoff."""

import argparse
import fcntl
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from integrations.protok.backend import ProTokBackend  # noqa: E402
from integrations.protok.run import (  # noqa: E402
    ProTokHarness,
    debug_policy,
    job_path,
    timed_out_job,
)
from research_loop.harness import completed_response  # noqa: E402


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def command(*args):
    return subprocess.run(
        args, check=True, capture_output=True, text=True, timeout=120
    ).stdout.strip()


def check_boundary(root, backend):
    control = root / "control"
    if (control / "STOP").exists():
        raise RuntimeError("Existing STOP requires explicit reconciliation")
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
    iteration = f"{state['iteration']:04d}"
    job = job_path(root, iteration) / "job.json"
    if job.exists() and read(job)["status"] != "completed":
        timeout = job.parent / "timeout.json"
        decision = control / "debug" / iteration / "decision.json"
        rejected = read(decision) if decision.exists() else {}
        if timeout.exists() and read(job)["status"] == "failed":
            workspace = read(folder / "iteration.json")["workspace"]
            timed_out_job(root, backend, workspace, iteration)
        elif not (
            read(job)["status"] == "failed"
            and rejected.get("status") == "failed"
            and rejected.get("response", {}).get("outcome") == "incomplete"
            and rejected["response"].get("measurements") == []
        ):
            raise RuntimeError(
                "Latest job is not completed or a reviewed debug rejection"
            )
    status = backend.inspect()
    if (
        status["outstanding"]
        or status.get("debug_outstanding")
        or status.get("research_outstanding")
        or status.get("long_training_outstanding")
        or status.get("manual_training_outstanding")
    ):
        raise RuntimeError("Outstanding evaluation effects prevent a handoff")
    return state


def check_timeout_boundary(root, backend, *, research=False):
    """Resume only the explicitly reconciled, settled failed allocation."""
    state = read(root / "study/state.json")
    iteration = f"{state['iteration']:04d}"
    relative = state.get("calls", {}).get("execute", "")
    if (
        state.get("phase") != "execute"
        or state.get("status") != "ready"
        or state.get("debugging")
        or state.get("active_attempt")
        or state.get("accepted_calls", {}).get("execute")
        or not re.fullmatch(rf"iterations/{iteration}/execute/attempt-[0-9]+", relative)
    ):
        raise RuntimeError("An explicitly reconciled formal timeout is required")
    attempt = root / "study" / relative
    record = read(attempt / "reconciliation.json")
    stop = root / "control/STOP"
    stop_text = stop.read_text() if stop.exists() else None
    if (
        record.get("operator_action")
        != (
            "reconcile-settled-research-result"
            if research
            else "reconcile-settled-formal-timeout"
        )
        or not isinstance(record.get("authorization"), str)
        or not record["authorization"].strip()
        or record.get("stop_before") != "Supervisor stopped: attention\n"
        or stop_text != record["stop_before"]
    ):
        raise RuntimeError("STOP changed or is not the reconciled system attention")
    owner = read(root / "control/ownership.json")
    lease = read(root / "control/continuation.json")
    if not time.time() < min(owner["expires_at"], lease["deadline"]):
        raise RuntimeError("Timeout continuation cannot extend the original deadline")
    if record.get("prepared_state") != state or any(
        (root / name).read_text() != value
        for name, value in record["protected"].items()
    ):
        raise RuntimeError("Timeout preparation or protected contract changed")
    result = completed_response(attempt)
    if result is None:
        raise RuntimeError("Timeout reconciliation has no completed controller receipt")
    ProTokHarness(backend, root, None, 0).validate_completed(
        result, workspace=Path(record["workspace"]), run_dir=attempt
    )
    return state


def check_debug_boundary(root, backend):
    state = read(root / "study/state.json")
    if (
        state.get("status") != "ready"
        or state.get("phase") != "execute"
        or not state.get("debugging")
    ):
        raise RuntimeError("An explicitly prepared debug continuation is required")
    iteration = f"{state['iteration']:04d}"
    debug_policy(root)
    preparation = read(
        root / "control/reconciliations" / f"begin-debug-{iteration}.json"
    )
    failure = read(root / "study" / state["debugging"])
    if (
        preparation.get("failure") != failure
        or failure["details"].get("kind") != "candidate_rejected"
    ):
        raise RuntimeError("Debug preparation no longer matches the certified failure")
    stop = root / "control/STOP"
    if (stop.read_text() if stop.exists() else None) != preparation.get("stop_before"):
        raise RuntimeError("STOP changed after the explicit debug preparation")
    status = backend.inspect()
    if (
        status["outstanding"]
        or status.get("debug_outstanding")
        or status.get("research_outstanding")
        or status.get("long_training_outstanding")
        or status.get("manual_training_outstanding")
    ):
        raise RuntimeError(
            "Outstanding effects prevent starting a prepared debug session"
        )
    return state


def deploy(
    root,
    runtime,
    *,
    resume_debug=False,
    resume_submission=False,
    resume_timeout=False,
    resume_research=False,
):
    if sum((resume_debug, resume_submission, resume_timeout, resume_research)) > 1:
        raise ValueError("Choose one explicit recovery mode")
    root, runtime = root.resolve(), runtime.resolve()
    commit = (runtime / "SOURCE_COMMIT").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Runtime must name its committed source revision")
    python = Path(sys.executable).absolute()
    if any(c.isspace() for c in str(root) + str(runtime) + str(python)):
        raise ValueError("Systemd runtime paths must not contain whitespace")
    service = "research-loop-protok.service"
    if (
        command("systemctl", "show", service, "-p", "ActiveState", "--value")
        != "inactive"
    ):
        raise RuntimeError("Wait for the old supervisor's HANDOFF exit")
    control = root / "control"
    marker = control / "HANDOFF"
    marker_text = f"Upgrade to {commit}\n"
    if not marker.exists() or marker.read_text() != marker_text:
        raise RuntimeError("Expected this revision's explicit HANDOFF request")
    owner = read(control / "ownership.json")
    backend = ProTokBackend(Path(owner["project"]), control, owner["owner_token"])
    with (
        (control / "supervisor.lock").open("a") as supervisor_lock,
        (control / "runner.lock").open("a") as runner_lock,
    ):
        fcntl.flock(supervisor_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(runner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if resume_submission:
            from integrations.protok.resume_submission import validate_prepared

            state = validate_prepared(root, backend)
        elif resume_timeout or resume_research:
            state = check_timeout_boundary(root, backend, research=resume_research)
        else:
            state = (
                check_debug_boundary(root, backend)
                if resume_debug
                else check_boundary(root, backend)
            )
        stop = control / "STOP"
        stop_before = stop.read_text() if stop.exists() else None
        protected = {
            name: (control / name).read_bytes()
            for name in ("ownership.json", "continuation.json")
        }
        backup = control / "upgrades" / commit
        backup.mkdir(parents=True, exist_ok=False)
        (backup / "service.before").write_text(command("systemctl", "cat", service))
        write(backup / "boundary.json", state)
        if stop_before is not None:
            (backup / "STOP.before").write_text(stop_before)
        dropin = Path("/etc/systemd/system") / f"{service}.d/60-runtime.conf"
        if dropin.exists():
            (backup / "runtime.before.conf").write_bytes(dropin.read_bytes())
        dropin.parent.mkdir(parents=True, exist_ok=True)
        temporary = dropin.with_suffix(".tmp")
        temporary.write_text(
            "[Service]\nExecStart=\n"
            f"ExecStart={python} {runtime}/integrations/protok/server.py "
            f"--root {root} --runner {runtime}/integrations/protok/run.py\n"
        )
        temporary.replace(dropin)
        command("systemctl", "daemon-reload")
        if any(
            (control / name).read_bytes() != value for name, value in protected.items()
        ):
            raise RuntimeError("Ownership or budget changed during handoff")
        if (
            marker.read_text() != marker_text
            or read(root / "study/state.json") != state
            or (stop.read_text() if stop.exists() else None) != stop_before
            or (
                stop.exists()
                and not (
                    resume_debug
                    or resume_submission
                    or resume_timeout
                    or resume_research
                )
            )
        ):
            raise RuntimeError("Operator state changed during handoff")
        marker.unlink()
        if (
            resume_debug or resume_submission or resume_timeout or resume_research
        ) and stop.exists():
            stop.unlink()
        write(
            backup / "deployment.json",
            {
                "status": "configured",
                "commit": commit,
                "runtime": str(runtime),
                "after_iteration": state["iteration"],
                "resume_debug": resume_debug,
                "resume_submission": resume_submission,
                "resume_timeout": resume_timeout,
                "resume_research": resume_research,
                "at": time.time(),
            },
        )
    command("systemctl", "start", service)
    status = command("systemctl", "is-active", service)
    write(
        backup / "deployment.json",
        {
            "status": status,
            "commit": commit,
            "runtime": str(runtime),
            "after_iteration": state["iteration"],
            "resume_debug": resume_debug,
            "resume_submission": resume_submission,
            "resume_timeout": resume_timeout,
            "resume_research": resume_research,
            "at": time.time(),
        },
    )
    return {"status": status, "commit": commit, "after_iteration": state["iteration"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    recovery = parser.add_mutually_exclusive_group()
    recovery.add_argument("--resume-debug", action="store_true")
    recovery.add_argument("--resume-submission", action="store_true")
    recovery.add_argument("--resume-timeout", action="store_true")
    recovery.add_argument("--resume-research", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            deploy(
                args.root,
                args.runtime,
                resume_debug=args.resume_debug,
                resume_submission=args.resume_submission,
                resume_timeout=args.resume_timeout,
                resume_research=args.resume_research,
            )
        )
    )


if __name__ == "__main__":
    main()
