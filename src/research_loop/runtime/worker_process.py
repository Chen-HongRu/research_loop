"""Exec-only local supervisor and callback child; never opens SQLite state."""

import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .files import publish_json
from .invocations import (
    _check_input,
    _folder,
    _json,
    _locked,
    _publish_completion,
    _read,
)


def _callback(root, token):
    folder = _folder(root, token)
    claim = _read(folder / "claimed.json")
    try:
        _check_input(root, claim)
        payload = _read(root / claim["binding"]["input_ref"])
        _json(payload)
        _check_input(root, claim)
        specification = importlib.util.spec_from_file_location(
            "local_invocation_callback", claim["module_path"]
        )
        if specification is None or specification.loader is None:
            raise ValueError("Cannot load invocation module")
        module = importlib.util.module_from_spec(specification)
        sys.modules[specification.name] = module
        specification.loader.exec_module(module)
        result = module.invoke(claim["binding"]["kind"], payload, claim["config"])
        _json(result)
        publish_json(folder / "callback-result.json", {"result": result, "error": None})
        return 0
    except BaseException as error:
        publish_json(
            folder / "callback-result.json",
            {
                "result": None,
                "error": f"{type(error).__name__}: {error}"[:1000],
            },
        )
        return 1


def _terminate(child):
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    # Keep the child unreaped until after group cleanup, so its PID cannot be
    # reused as a different process group during the grace interval.
    time.sleep(1)
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS may deny a signal to a group containing only an exited child.
        # An actual wait result is required; a live child remains unresolved.
        if child.poll() is None:
            raise
    return child.wait()


def _supervise(root, token):
    folder = _folder(root, token)
    with _locked(folder, blocking=True) as acquired:
        if not acquired:
            return 1
        claim = _read(folder / "claimed.json")
        if (folder / "completion.json").exists():
            return 0
        publish_json(
            folder / "started.json", {"pid": os.getpid(), "started_at": time.time()}
        )
        child = None
        exit_code = None
        deadline = claim["binding"]["deadline"]
        monotonic_deadline = time.monotonic() + max(0, deadline - time.time())

        def expired():
            return time.time() >= deadline or time.monotonic() >= monotonic_deadline

        try:
            if (folder / "cancel.json").exists():
                status = "cancelled"
            elif expired():
                status = "timed_out"
            else:
                with (folder / "callback.log").open("ab") as log:
                    child = subprocess.Popen(
                        [
                            claim["python"],
                            "-m",
                            "research_loop.runtime.worker_process",
                            "--callback",
                            str(root),
                            token,
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        start_new_session=True,
                        close_fds=True,
                    )
                    while child.poll() is None:
                        cancelled = (folder / "cancel.json").exists()
                        if cancelled or expired():
                            status = "cancelled" if cancelled else "timed_out"
                            exit_code = _terminate(child)
                            break
                        time.sleep(0.05)
                    else:
                        exit_code = child.wait()
                        result = _read(folder / "callback-result.json")
                        status = (
                            "completed"
                            if exit_code == 0 and result["error"] is None
                            else "failed"
                        )
                        _publish_completion(
                            root,
                            folder,
                            claim,
                            status,
                            result=result["result"] if status == "completed" else None,
                            error=result["error"],
                            exit_code=exit_code,
                        )
                        return 0
            _publish_completion(root, folder, claim, status, exit_code=exit_code)
            return 0
        except BaseException as error:
            if child is not None:
                exit_code = _terminate(child) if child.poll() is None else child.wait()
            _publish_completion(
                root,
                folder,
                claim,
                "failed",
                error=f"{type(error).__name__}: {error}"[:1000],
                exit_code=exit_code,
            )
            return 1


def main():
    mode, root, token = sys.argv[1:]
    root = Path(root).resolve()
    if mode == "--callback":
        return _callback(root, token)
    if mode == "--supervise":
        return _supervise(root, token)
    raise ValueError("Unknown worker mode")


if __name__ == "__main__":
    raise SystemExit(main())
