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
    job = control / "jobs" / f"{state['iteration']:04d}" / "job.json"
    if job.exists() and read(job)["status"] != "completed":
        raise RuntimeError("Latest job is not completed")
    if backend.inspect()["outstanding"]:
        raise RuntimeError("Outstanding evaluation effects prevent a handoff")
    return state


def deploy(root, runtime):
    root, runtime = root.resolve(), runtime.resolve()
    commit = (runtime / "SOURCE_COMMIT").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Runtime must name its committed source revision")
    if any(c.isspace() for c in str(root) + str(runtime)):
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
        state = check_boundary(root, backend)
        protected = {
            name: (control / name).read_bytes()
            for name in ("ownership.json", "continuation.json")
        }
        backup = control / "upgrades" / commit
        backup.mkdir(parents=True, exist_ok=False)
        (backup / "service.before").write_text(command("systemctl", "cat", service))
        write(backup / "boundary.json", state)
        dropin = Path("/etc/systemd/system") / f"{service}.d/60-runtime.conf"
        if dropin.exists():
            (backup / "runtime.before.conf").write_bytes(dropin.read_bytes())
        dropin.parent.mkdir(parents=True, exist_ok=True)
        temporary = dropin.with_suffix(".tmp")
        temporary.write_text(
            "[Service]\nExecStart=\n"
            f"ExecStart=/root/research-loop/.venv/bin/python {runtime}/integrations/protok/server.py "
            f"--root {root} --runner {runtime}/integrations/protok/run.py\n"
        )
        temporary.replace(dropin)
        command("systemctl", "daemon-reload")
        if any(
            (control / name).read_bytes() != value for name, value in protected.items()
        ):
            raise RuntimeError("Ownership or budget changed during handoff")
        if marker.read_text() != marker_text or (control / "STOP").exists():
            raise RuntimeError("Operator state changed during handoff")
        marker.unlink()
        write(
            backup / "deployment.json",
            {
                "status": "configured",
                "commit": commit,
                "runtime": str(runtime),
                "after_iteration": state["iteration"],
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
            "at": time.time(),
        },
    )
    return {"status": status, "commit": commit, "after_iteration": state["iteration"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(deploy(args.root, args.runtime)))


if __name__ == "__main__":
    main()
