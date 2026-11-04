"""Exec-only local supervisor and callback child; never opens SQLite state."""

import ctypes
import errno
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .files import publish_json
from .invocations import (
    _check_input,
    _folder,
    _json,
    _locked,
    _load_module,
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
        module = _load_module(claim["module_path"], claim["module_identity"])
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


def _wait_without_reaping(pid):
    """Observe this child's exit while its unreaped PID still pins the group."""
    if hasattr(os, "waitid"):
        result = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
        if result is None or result.si_pid != pid:
            raise RuntimeError("Original callback exit was not observed")
        return
    # CPython on macOS exposes WNOWAIT but not os.waitid. A blocking native
    # wait needs no platform-dependent siginfo layout: WEXITED admits only exit,
    # and WNOWAIT leaves the child waitable. The aligned buffer exceeds siginfo.
    libc = ctypes.CDLL(None, use_errno=True)
    waitid = libc.waitid
    waitid.argtypes = (ctypes.c_int, ctypes.c_uint, ctypes.c_void_p, ctypes.c_int)
    waitid.restype = ctypes.c_int
    information = (ctypes.c_long * 128)()
    while waitid(os.P_PID, pid, ctypes.byref(information), os.WEXITED | os.WNOWAIT):
        code = ctypes.get_errno()
        if code != errno.EINTR:
            raise OSError(code, os.strerror(code))


def _watch_exit(child):
    exited, errors = threading.Event(), []

    def observe():
        try:
            _wait_without_reaping(child.pid)
        except BaseException as error:
            errors.append(error)
        finally:
            exited.set()

    threading.Thread(target=observe, daemon=True).start()
    return exited, errors


def _live_group_members(group):
    # One small OS process-metadata query normally suffices; cleanup may repeat
    # it within its bound. No command arguments, datasets or artifact bodies are
    # read. Only zombies/dead tasks are nonexecuting; stopped tasks remain live.
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,pgid=,stat="],
        capture_output=True,
        text=True,
        check=True,
        timeout=1,
    )
    live = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
            raise RuntimeError("Cannot interpret callback group process metadata")
        if int(fields[1]) == group and not fields[2].startswith(("Z", "X")):
            live.append(int(fields[0]))
    return live


def _signal_group(child, value):
    if child.returncode is not None:
        raise RuntimeError("Callback leader was reaped before group cleanup")
    try:
        os.killpg(child.pid, value)
    except (ProcessLookupError, PermissionError):
        # macOS returns EPERM for zombie-only groups. Neither error proves
        # retirement: the original exit and kernel group states must agree.
        pass


def _retire_group(child, watch, *, graceful=False):
    """Clean a trusted, nondetaching callback group before reaping its leader.

    We cannot waitpid() indirect children. After an owned group kill, kernel
    states must show no executable group members, and WNOWAIT must positively
    observe the original child's exit. Any inspection/cleanup failure leaves
    the invocation unknown, without a canonical sender-retirement receipt.
    """
    exited, errors = watch
    if graceful:
        _signal_group(child, signal.SIGTERM)
        exited.wait(1)
    _signal_group(child, signal.SIGKILL)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if errors:
            raise RuntimeError("Cannot observe unreaped callback exit") from errors[0]
        live = _live_group_members(child.pid)
        if exited.is_set() and not live:
            return child.wait(timeout=0.2)
        _signal_group(child, signal.SIGKILL)
        exited.wait(0.02)
        if exited.is_set():
            time.sleep(0.01)
    raise TimeoutError("Callback process-group cleanup remains unresolved")


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
        watch = None
        retired = False
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
                    watch = _watch_exit(child)
                    while not watch[0].wait(0.05):
                        cancelled = (folder / "cancel.json").exists()
                        if cancelled or expired():
                            status = "cancelled" if cancelled else "timed_out"
                            exit_code = _retire_group(child, watch, graceful=True)
                            retired = True
                            break
                    else:
                        exit_code = _retire_group(child, watch)
                        retired = True
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
            if child is not None and not retired:
                try:
                    watch = _watch_exit(child) if watch is None else watch
                    exit_code = _retire_group(child, watch)
                except BaseException as cleanup_error:
                    # No later group signal is attempted on this path. Reap
                    # our direct child when possible, while retaining unknown
                    # group retirement and withholding the canonical receipt.
                    try:
                        child.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        pass
                    print(
                        "Callback cleanup unresolved: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}",
                        file=sys.stderr,
                    )
                    return 1
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
