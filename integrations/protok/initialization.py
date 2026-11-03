"""Read-only readiness checks for published ProTok parent Studies.

Read a fixed set of controller records, not history trees, workspaces, datasets or
checkpoints. This is an initialization gate, not a replacement for core recovery
or backend authorization/accounting checks.
"""

import fcntl
import json
import math
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path

from research_loop import Study

from .backend import ProTokError

COMPLETION = "protok-initialization.json"
MAX_CONTROL_BYTES = 4 * 1024 * 1024
SEEDS = ("R0001", "R0002", "R0003")


def completion(baseline):
    return {"version": 1, "status": "complete", "baseline": baseline}


def plain_path(root, relative):
    """Reject symlinks in adapter-owned paths, including dangling links."""
    path = Path(root)
    if path.is_symlink():
        raise ValueError("Symlink at initialization root")
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("Symlink in initialization control path")
    return path


@contextmanager
def initialization_lock(root):
    """Serialize cooperative initializers before touching either staging tree."""
    try:
        path = plain_path(root, "control/initialization.lock")
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except (OSError, ValueError) as error:
        raise ProTokError("Unsafe or unavailable initialization lock") from error
    with os.fdopen(descriptor, "a") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise ProTokError("Initialization lock must be a regular file")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ProTokError("Another ProTok initializer is active") from error
        yield


def _record(folder, relative):
    path = plain_path(folder, relative)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONTROL_BYTES:
        raise ValueError("Controller record is not a bounded regular file")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Controller record is not an object")
    return value


def _text_prefix(folder, relative):
    path = plain_path(folder, relative)
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Research document is not a regular file")
    # Research notes may grow; readiness needs presence, not their full contents.
    with path.open() as stream:
        prefix = stream.read(128)
    if not prefix.strip():
        raise ValueError("Required research document is empty")
    return prefix


def ready(root):
    """Return a Study only with current state and retained completion evidence.

    Never repair or certify a legacy directory by writing a completion marker.
    A legacy zero-round core initialize() alone is not adapter initialization.
    A previously running Study instead proves progress with the current core
    iteration and its frozen context; evolved routes need not retain seed IDs.
    """
    try:
        folder = plain_path(root, "study")
        config = _record(folder, "study.json")
        study = Study(folder)  # Preserve the core's version/contract checks.
        baseline = config.get("baseline")
        if not isinstance(baseline, str) or not re.fullmatch(r"[0-9a-f]{40}", baseline):
            raise ValueError("Missing frozen Git baseline")
        state = _record(folder, "state.json")
        if (
            type(state.get("iteration")) is not int
            or state["iteration"] < 0
            or state.get("phase")
            not in {"done", "selection", "plan", "execute", "review"}
            or state.get("status")
            not in {"ready", "running", "stopped", "needs_attention"}
            or type(state.get("call_count")) is not int
            or state["call_count"] < 0
            or not isinstance(state.get("calls"), dict)
            or type(state.get("elapsed_seconds")) not in (int, float)
            or not math.isfinite(state["elapsed_seconds"])
            or state["elapsed_seconds"] < 0
        ):
            raise ValueError("Invalid current Study state")
        routes = _record(folder, "routes/index.json")
        frontiers = _record(folder, "frontiers.json")
        if not all(isinstance(frontiers.get(key), dict) for key in ("global", "local")):
            raise ValueError("Invalid current frontier indexes")
        _text_prefix(folder, "goal.md")
        narrative = _text_prefix(folder, "research.md")
        marker = plain_path(folder, COMPLETION)
        if marker.exists() and _record(folder, COMPLETION) != completion(baseline):
            raise ValueError("Initialization completion identity differs")
        if state["iteration"] == 0:
            if not marker.exists() and not narrative.startswith(
                "# Historical ProTok evidence\n"
            ):
                raise ValueError("Legacy historical seed missing")
            for rid in SEEDS:
                if not isinstance(routes.get(rid), dict):
                    raise ValueError("Initial route seed missing")
                _text_prefix(folder, f"routes/{rid}/research.md")
                _record(folder, f"routes/{rid}/evaluation.json")
        else:
            iteration = f"iterations/{state['iteration']:04d}"
            metadata = _record(folder, f"{iteration}/iteration.json")
            frozen = _record(folder, f"{iteration}/context/study.before.json")
            if (
                metadata.get("id") != f"{state['iteration']:04d}"
                or metadata.get("status")
                not in {"selecting", "planned", "executed", "reviewed"}
                or not metadata.get("created_at")
                or frozen.get("baseline") != baseline
                or frozen.get("workspace") != config["workspace"]
            ):
                raise ValueError("Invalid retained iteration evidence")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise ProTokError(
            "Parent Study initialization/state is incomplete; preserve and reconcile it"
        ) from error
    return study
