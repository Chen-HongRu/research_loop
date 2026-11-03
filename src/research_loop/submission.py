"""One durable evaluation submission per iteration workspace (Unix).

States: absent -> not_submitted; request -> pending; claimed receipt -> accepted;
result status completed -> completed; error/candidate_failed/failed -> failed.
Pure preclaim parameter rejection -> parameter_error; corrected request -> pending.
Terminal responses include the original result. Keep the receipt for the workspace's
lifetime: an accepted request must never be submitted again, even after a crash.
"""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import sys


REQUEST = "evaluation-request.json"
RESULT = "evaluation-result.json"
RECEIPT = "evaluation-submission.json"
LOCK = ".evaluation-submit.lock"
STAGING = "evaluation-request.json.tmp"
REJECTIONS = "evaluation-parameter-errors.json"
INTERFACE_FILES = {REQUEST, RESULT, RECEIPT, LOCK, STAGING, REJECTIONS}


class SubmissionError(RuntimeError):
    """The workspace cannot be submitted safely without operator inspection."""


def _regular_open(path, flags, mode=0o600):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise SubmissionError(f"Coordination file is not regular: {path.name}")
    return fd


@contextmanager
def _locked(workspace):
    workspace = Path(workspace)
    try:
        fd = _regular_open(workspace / LOCK, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield workspace
        finally:
            os.close(fd)
    except OSError as error:
        raise SubmissionError(str(error)) from error


def _read(path):
    try:
        fd = _regular_open(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            limit = 1024 if path.name in (REQUEST, RECEIPT) else 1024 * 1024
            if os.fstat(stream.fileno()).st_size > limit:
                raise SubmissionError(
                    f"Coordination file exceeds {limit} bytes: {path.name}"
                )
            value = json.load(stream)
    except (ValueError, UnicodeError) as error:
        raise SubmissionError(f"Malformed coordination file: {path.name}") from error
    if not isinstance(value, dict):
        raise SubmissionError(f"Expected JSON object: {path.name}")
    return value


def _request(value, name):
    if value is not None and (
        set(value) != {"action"}
        or value["action"] not in ("evaluate", "import", "research")
    ):
        raise SubmissionError(f"Invalid request in {name}")
    return value


def _state(workspace):
    # Validate every existing interface, even when a terminal result is present.
    if os.path.lexists(workspace / STAGING):
        raise SubmissionError(f"Staging file requires inspection: {STAGING}")
    request = _request(_read(workspace / REQUEST), REQUEST)
    receipt = _request(_read(workspace / RECEIPT), RECEIPT)
    result = _read(workspace / RESULT)
    rejections = _rejections(workspace)
    if request is not None and receipt is not None:
        raise SubmissionError("Both pending request and claimed receipt exist")
    response = {"status": "not_submitted"}
    if rejections:
        response = {"status": "parameter_error", "feedback": rejections[-1]}
    current = receipt if receipt is not None else request
    if current is not None:
        response.update(
            status="accepted" if receipt is not None else "pending",
            action=current["action"],
        )
    if result is not None:
        result_status = result.get("status")
        if result_status == "completed":
            response["status"] = "completed"
        elif result_status in ("error", "candidate_failed", "failed"):
            response["status"] = "failed"
        else:
            raise SubmissionError("Invalid evaluation result status")
        response["result"] = result
    return response


def _rejections(workspace):
    history = _read(workspace / REJECTIONS)
    if history is None:
        return []
    if set(history) != {"rejections"} or type(history["rejections"]) is not list:
        raise SubmissionError("Invalid parameter rejection history")
    for entry in history["rejections"]:
        if (
            type(entry) is not dict
            or set(entry) != {"request", "error"}
            or type(entry["error"]) is not str
            or not entry["error"]
            or type(entry["request"]) is not dict
        ):
            raise SubmissionError("Invalid parameter rejection history")
        _request(entry["request"], REJECTIONS)
    return history["rejections"]


def _reject(workspace, request, error):
    history = _rejections(workspace)
    history.append({"request": request, "error": str(error)[:2048]})
    content = json.dumps({"rejections": history}, ensure_ascii=False) + "\n"
    if len(content.encode()) > 65536:
        raise SubmissionError("Parameter rejection history exceeds 65536 bytes")
    staging = workspace / STAGING
    fd = _regular_open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, workspace / REJECTIONS)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    # Retain feedback before removing the never-claimed request. A crash here
    # leaves it pending for pure preflight again, never for computation replay.
    _sync_directory(workspace)
    (workspace / REQUEST).unlink()
    _sync_directory(workspace)


def _sync_directory(workspace):
    fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def status(workspace):
    """Return the durable state without publishing a request."""
    with _locked(workspace) as directory:
        return _state(directory)


def submit(workspace, action="evaluate"):
    """Publish once using an ordinary named file and atomic replacement.

    A failure before replacement removes only this call's staging file. A failure
    after replacement preserves the published request; retry observes its state.
    """
    if action not in ("evaluate", "import", "research"):
        raise SubmissionError("Action must be evaluate, import or research")
    with _locked(workspace) as directory:
        current = _state(directory)
        if "action" in current and current["action"] != action:
            raise SubmissionError("Conflicting action already submitted")
        if current["status"] not in ("not_submitted", "parameter_error"):
            return current
        staging = directory / STAGING
        fd = _regular_open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"action": action}, stream, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staging, directory / REQUEST)
        except BaseException:
            staging.unlink(missing_ok=True)
            raise
        _sync_directory(directory)
        return {"status": "pending", "action": action}


def claim_request(workspace, *, preflight=None):
    """Atomically retain a pending request as a receipt and return it once.

    None means no unclaimed request remains. A crash after rename deliberately
    leaves an accepted receipt; recovery must inspect it, never resubmit it.
    An optional trusted, side-effect-free preflight may raise ParameterError to
    retain feedback and discard only this never-claimed request. Other errors block.
    """
    with _locked(workspace) as directory:
        current = _state(directory)
        if current["status"] != "pending":
            return None
        request = {"action": current["action"]}
        if preflight is not None:
            # Lazy import keeps the standalone submit/status CLI independent
            # of package discovery in an Agent's isolated workspace.
            from research_loop.correction import ParameterError

            try:
                preflight(request)
            except ParameterError as error:
                _reject(directory, request, error)
                return None
        os.replace(directory / REQUEST, directory / RECEIPT)
        _sync_directory(directory)
        return request


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("submit", "status"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--workspace", required=True)
        if command == "submit":
            subparser.add_argument(
                "--action",
                choices=("evaluate", "import", "research"),
                default="evaluate",
            )
    args = parser.parse_args(argv)
    try:
        value = (
            submit(args.workspace, args.action)
            if args.command == "submit"
            else status(args.workspace)
        )
    except SubmissionError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps(value, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
