"""User-authored priority experiments; only the controller consumes requests."""

import fcntl
import hashlib
import os
import shlex
import stat
import subprocess
from contextlib import contextmanager
from uuid import uuid4


@contextmanager
def input_lock(root):
    directory = root / "inputs"
    if directory.is_symlink():
        raise ValueError("Study inputs must be a real directory")
    directory.mkdir(exist_ok=True)
    with (directory / "human.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield directory / "human.md"


def _read(path):
    if path.is_symlink():
        raise ValueError("Human input must be a regular file, not a symlink")
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("Human input must be a regular file")
        with path.open("rb") as stream:
            content = stream.read()
            metadata = os.fstat(stream.fileno())
    except FileNotFoundError:
        return None
    text = content.decode("utf-8")
    if not text.strip():
        return None
    # This reads only the small user request, never research data or artifacts.
    identity = (
        f"{metadata.st_dev}:{metadata.st_ino}:"
        f"{metadata.st_mtime_ns}:{metadata.st_ctime_ns}"
    )
    return {
        "id": hashlib.sha256(identity.encode() + b"\0" + content).hexdigest(),
        "text": text,
    }


def _replace(path, text):
    temporary = path.with_name(".human-" + uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def submit(root, text):
    """Inject a request without silently replacing an existing pending request."""
    if not text.strip():
        raise ValueError("Provide a nonempty experiment request")
    with input_lock(root) as path:
        if _read(path):
            raise ValueError(
                "A human request is pending; use edit-experiment to revise it"
            )
        _replace(path, text)
        request = _read(path)
        return {"request_id": request["id"], "file": str(path)}


def edit(root):
    """Hold the consumption lock for the entire user editor session."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        raise ValueError("Set VISUAL or EDITOR to a foreground editor command")
    with input_lock(root) as path:
        _read(path)  # Reject symlinks before launching the editor.
        if not path.exists():
            path.touch(exist_ok=False)
        subprocess.run([*shlex.split(editor), str(path)], check=True)
        request = _read(path)
        return {"request_id": request["id"] if request else None, "file": str(path)}


def admit(root):
    with input_lock(root) as path:
        return _read(path)


def consume(root, publication):
    """Replayable acknowledgement, based only on an activated publication receipt."""
    request = publication.get("human_request")
    outcome = publication.get("result", {}).get("human_request", {})
    if not request or outcome.get("status") != "completed":
        return
    if outcome.get("id") != request["id"]:
        raise ValueError(
            "Published human request acknowledgement has the wrong identity"
        )
    with input_lock(root) as path:
        current = _read(path)
        if current and current["id"] == request["id"]:
            _replace(path, "")


def validate_result(request, result):
    if not request:
        return
    outcome = result.get("human_request", {})
    if (
        not isinstance(outcome, dict)
        or outcome.get("id") != request["id"]
        or outcome.get("status") not in {"completed", "pending", "blocked"}
        or not isinstance(outcome.get("reason"), str)
        or not outcome["reason"].strip()
        or not isinstance(outcome.get("evidence"), list)
        or not all(isinstance(item, str) for item in outcome["evidence"])
    ):
        raise ValueError("Result requires a matching human request disposition")
    if outcome["status"] == "completed" and not outcome["evidence"]:
        raise ValueError("Completed human request requires evidence references")
    if outcome["status"] == "blocked" and result.get("study_action") != "stop":
        raise ValueError("Blocked human request must stop for user intervention")


def result_schema(base, request):
    if not request:
        return base
    return {
        **base,
        "properties": {
            **base["properties"],
            "human_request": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "enum": [request["id"]]},
                    "status": {
                        "type": "string",
                        "enum": ["completed", "pending", "blocked"],
                    },
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
                "required": ["id", "status", "evidence", "reason"],
                "additionalProperties": False,
            },
        },
        "required": [*base["required"], "human_request"],
    }
