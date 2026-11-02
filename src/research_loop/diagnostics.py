"""Durable command streams with a separate, redacted Agent-readable view."""

from __future__ import annotations

import argparse
from contextvars import ContextVar
import functools
import json
import os
from pathlib import Path
import re
import shlex
import sys
import subprocess
import threading
import time
import uuid
from urllib.parse import unquote, urlsplit

_MANIFEST = "diagnostic-manifest.json"
_ID = re.compile(r"[a-f0-9]{32}")
_ACTIVE = ContextVar("command_diagnostic", default=None)
_SECONDARY = ContextVar("secondary_diagnostic", default=False)


def diagnostic_secrets(secrets=()):
    """Known controller/environment credentials, used only in memory for redaction."""
    values = [str(value) for value in secrets if value]
    for key, value in os.environ.items():
        if re.search(r"(?i)(?:token|password|passwd|secret|api_?key|credential)$", key):
            values.append(value)
        if key.lower().endswith("proxy"):
            parsed = urlsplit(value)
            if parsed.password:
                values.extend([value, unquote(parsed.password)])
                if parsed.username:
                    values.append(unquote(parsed.username))
    return tuple(value for value in values if value)


def _redact(value, secrets=()):
    text = str(value)
    for secret in sorted({str(s) for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(
        r"(?i)(\b(?:authorization|proxy-authorization)\s*[:=]\s*)(?:bearer\s+|basic\s+)?[^\r\n,;]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)(?<![\w-])((?:[\"']?)(?:[\w-]*(?:token|password|passwd|secret|api[_-]?key|credential)[\w-]*)(?:[\"']?)\s*[:=]\s*)(?:[\"'][^\"'\r\n]*[\"']|[^\s,;}]+)",
        r"\1[REDACTED]",
        text,
    )
    return re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)


def redact_text(value, *, secrets=()):
    """Redact a public text field using the same in-memory credential set."""
    return _redact(value, diagnostic_secrets(secrets))


def _message(error, secrets):
    # Subprocess strings include argv (possibly a whole prompt). Never copy it.
    if isinstance(error, subprocess.CalledProcessError):
        message = f"Command exited with status {error.returncode}"
    elif isinstance(error, subprocess.TimeoutExpired):
        message = f"Command timed out after {error.timeout} seconds"
    else:
        message = str(error)
    message = re.sub(
        r"(?s)Command\s+['\"]?\[.*\]['\"]?(?=\s+(?:returned|timed|died))",
        "Command [argv omitted]",
        message,
    )
    message = _redact(message, secrets)
    frames = []
    current = error.__traceback__
    while current is not None:
        code = current.tb_frame.f_code
        frames.append(
            {
                "file": _redact(code.co_filename, secrets),
                "line": current.tb_lineno,
                "function": code.co_name,
            }
        )
        current = current.tb_next
    return {
        "type": type(error).__name__,
        "message": message[:8192],
        "truncated": len(message) > 8192,
        "frames": frames[-20:],
    }


def diagnostic_view(
    error=None,
    *,
    owner="unknown",
    stage="execute",
    returncode=None,
    logs=(),
    secrets=(),
):
    """Describe an error without argv; ownership comes from the trusted caller."""
    secrets = diagnostic_secrets(secrets)
    causes, seen = [], set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        causes.append(_message(error, secrets))
        if returncode is None:
            returncode = getattr(error, "returncode", None)
        error = error.__cause__ or (
            None if error.__suppress_context__ else error.__context__
        )
    return {
        "owner": owner,
        "stage": stage,
        "returncode": returncode,
        "cause_chain": causes,
        "logs": list(logs),
    }


def _file(directory, relative):
    root = Path(directory).absolute()
    parts = Path(relative).parts
    if Path(relative).is_absolute() or ".." in parts:
        raise ValueError("Diagnostic path must be registered and relative")
    for path in [root, *(root.joinpath(*parts[:i]) for i in range(1, len(parts) + 1))]:
        if path.is_symlink():
            raise ValueError("Diagnostic paths cannot be symlinks")
    return root / relative


def _read_manifest(directory):
    path = _file(directory, _MANIFEST)
    if not path.exists():
        return {"schema": 1, "logs": [], "failures": []}
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "r") as handle:
        manifest = json.load(handle)
    if manifest.get("schema") != 1:
        raise ValueError("Invalid diagnostic manifest")
    return manifest


def read_log_page(directory, log_id, *, offset=0, limit=8192):
    """Read a registered, already-redacted stream, never an arbitrary raw path."""
    if not isinstance(log_id, str) or not _ID.fullmatch(log_id):
        raise ValueError("Invalid diagnostic log ID")
    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= 65536
    ):
        raise ValueError("Invalid diagnostic page bounds")
    manifest = _read_manifest(directory)
    if not any(row.get("log_id") == log_id for row in manifest["logs"]):
        raise ValueError("Unknown diagnostic log ID")
    path = _file(directory, f"public/{log_id}.log")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
        handle.seek(offset)
        data = handle.read(limit)
        eof = handle.read(1) == b""
    return {
        "log_id": log_id,
        "offset": offset,
        "next_offset": offset + len(data),
        "eof": eof,
        "text": data.decode("utf-8", errors="replace"),
    }


def _text(value):
    if value is None:
        return ""
    return (
        value.decode("utf-8", errors="replace")
        if isinstance(value, bytes)
        else str(value)
    )


def _stage(command):
    for token, stage in (
        ("sbatch", "submit"),
        ("squeue", "poll"),
        ("sacct", "accounting"),
        ("scancel", "cleanup"),
    ):
        if re.search(r"\b" + token + r"\b", command):
            return stage
    return (
        "recovery"
        if "job-id.txt" in command and re.search(r"\bcat\b", command)
        else "ssh"
    )


class _SubprocessProxy:
    """Override only a loaded evaluator's binding, never the subprocess module."""

    def __init__(self, original, diagnostics):
        self.original, self.diagnostics = original, diagnostics

    def __getattr__(self, name):
        return getattr(self.original, name)

    def run(self, *args, **kwargs):
        active = _ACTIVE.get()
        if active is None or active[0] is not self.diagnostics:
            return self.original.run(*args, **kwargs)
        active[1]["captured"] = True
        try:
            result = self.original.run(*args, **kwargs)
        except BaseException as error:
            self.diagnostics._capture(error, active[1]["stage"], error=error)
            raise
        self.diagnostics._capture(result, active[1]["stage"])
        return result


class CommandDiagnostics:
    def __init__(self, directory, *, secrets=()):
        self.directory = Path(directory).absolute()
        self.secrets = diagnostic_secrets(secrets)
        self.capture_errors = []
        self._lock = threading.RLock()

    def _save(self, manifest):
        path = _file(self.directory, _MANIFEST)
        temporary = _file(self.directory, f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(manifest, indent=2) + "\n")
        temporary.replace(path)

    def _stream(self, value, *, stream, stage, returncode, owner):
        log_id = uuid.uuid4().hex
        raw, public = _text(value), _redact(_text(value), self.secrets)
        for kind, content in (("raw", raw), ("public", public)):
            folder = _file(self.directory, kind)
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = _file(self.directory, f"{kind}/{log_id}.log")
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "w") as handle:
                handle.write(content)
        return {
            "log_id": log_id,
            "recorded_at": time.time(),
            "stream": stream,
            "stage": stage,
            "returncode": returncode,
            "owner": owner,
            "bytes": len(public.encode("utf-8")),
            "tail": public[-8192:],
        }

    def _capture(self, result, stage, *, error=None, owner="unknown"):
        try:
            self._capture_impl(result, stage, error=error, owner=owner)
        except Exception as failure:
            # Recording must never replace the original command result/exception.
            self.capture_errors.append(
                diagnostic_view(
                    failure,
                    owner="pipeline",
                    stage="diagnostic_capture",
                    secrets=self.secrets,
                )
            )

    def _capture_impl(self, result, stage, *, error=None, owner="unknown"):
        secondary = _SECONDARY.get()
        if secondary:
            stage = "log_collection"
        with self._lock:
            manifest = _read_manifest(self.directory)
            code = getattr(result, "returncode", None)
            manifest["logs"].extend(
                self._stream(
                    getattr(result, stream, None),
                    stream=stream,
                    stage=stage,
                    returncode=code,
                    owner=owner,
                )
                for stream in ("stdout", "stderr")
            )
            if error is not None:
                manifest["failures"].append(
                    {
                        **diagnostic_view(
                            error,
                            stage=stage,
                            returncode=code,
                            owner=owner,
                            secrets=self.secrets,
                        ),
                        "recorded_at": time.time(),
                        "secondary": secondary,
                    }
                )
            self._save(manifest)

    def record_error(self, error, *, stage="execute", owner="unknown"):
        """Keep the operator's original exception and return only its safe message."""
        self._capture(error, stage, error=error, owner=owner)
        try:
            folder = _file(self.directory, "raw")
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = _file(self.directory, f"raw/{uuid.uuid4().hex}.error")
            fd = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "w") as handle:
                handle.write(f"{type(error).__name__}: {error}")
        except Exception as failure:
            self.capture_errors.append(
                diagnostic_view(
                    failure,
                    owner="pipeline",
                    stage="diagnostic_capture",
                    secrets=self.secrets,
                )
            )
        message = _message(error, self.secrets)
        return f"{message['type']}: {message['message']}"

    def wrap_ssh(self, function):
        @functools.wraps(function)
        def ssh(config, command, *args, **kwargs):
            state = {"stage": _stage(command), "captured": False}
            token = _ACTIVE.set((self, state))
            try:
                result = function(config, command, *args, **kwargs)
            except BaseException as error:
                if not state["captured"]:
                    self._capture(error, state["stage"], error=error)
                raise
            else:
                if not state["captured"]:
                    output = subprocess.CompletedProcess([], 0, result, "")
                    self._capture(output, state["stage"])
                return result
            finally:
                _ACTIVE.reset(token)

        ssh._diagnostic_original = function
        return ssh

    def install(self, backend):
        if getattr(backend, "command_diagnostics", None) is self:
            return self
        if hasattr(backend, "subprocess"):
            module = backend.subprocess
            if isinstance(module, _SubprocessProxy):
                module = module.original
            backend.subprocess = _SubprocessProxy(module, self)
        original = getattr(backend.ssh, "_diagnostic_original", backend.ssh)
        backend.ssh = self.wrap_ssh(original)
        backend.command_diagnostics = self
        return self

    def _record_file(self, source, *, stream, stage, owner):
        log_id = uuid.uuid4().hex
        for kind in ("raw", "public"):
            _file(self.directory, kind).mkdir(parents=True, exist_ok=True, mode=0o700)
        raw = _file(self.directory, f"raw/{log_id}.log")
        public = _file(self.directory, f"public/{log_id}.log")
        with source.open("rb") as handle, raw.open("xb") as target:
            while chunk := handle.read(65536):
                target.write(chunk)
        # Literal credentials may straddle input chunks or line boundaries. Keep
        # their longest possible prefix until more input arrives, before generic
        # line redaction. Memory is bounded by chunk + longest credential/line.
        import codecs

        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        overlap = max((len(secret) for secret in self.secrets), default=1)
        pending, line, tail, size = "", "", "", 0

        def literals(text, final=False):
            nonlocal pending
            pending += text
            safe = len(pending) if final else max(0, len(pending) - overlap)
            parts = []
            while safe > 0:
                matches = [
                    (pending.find(secret), secret)
                    for secret in self.secrets
                    if secret in pending
                ]
                match = min(
                    matches, default=None, key=lambda item: (item[0], -len(item[1]))
                )
                if match is None or match[0] >= safe:
                    parts.append(pending[:safe])
                    pending = pending[safe:]
                    break
                index, secret = match
                parts.extend([pending[:index], "[REDACTED]"])
                pending = pending[index + len(secret) :]
                safe = len(pending) if final else max(0, len(pending) - overlap)
            return "".join(parts)

        with raw.open("rb") as handle, public.open("x", encoding="utf-8") as target:
            while True:
                chunk = handle.read(65536)
                line += literals(
                    decoder.decode(chunk, final=not chunk), final=not chunk
                )
                pieces = line.splitlines(keepends=True)
                line = ""
                for piece in pieces:
                    if chunk and not piece.endswith(("\n", "\r")):
                        line = piece
                        continue
                    safe = _redact(piece, self.secrets)
                    target.write(safe)
                    size += len(safe.encode("utf-8"))
                    tail = (tail + safe)[-8192:]
                if not chunk:
                    break
        raw.chmod(0o600)
        public.chmod(0o600)
        return {
            "log_id": log_id,
            "recorded_at": time.time(),
            "stream": stream,
            "stage": stage,
            "returncode": None,
            "owner": owner,
            "bytes": size,
            "tail": tail,
        }

    def record_files(self, files, *, stage="agent", owner="unknown"):
        items = (
            files.items()
            if isinstance(files, dict)
            else ((Path(path).stem, path) for path in files)
        )
        with self._lock:
            manifest = _read_manifest(self.directory)
            for stream, path in items:
                path = Path(path)
                if path.is_symlink():
                    raise ValueError("Diagnostic source cannot be a symlink")
                manifest["logs"].append(
                    self._record_file(path, stream=stream, stage=stage, owner=owner)
                )
            self._save(manifest)
        return self.view(owner=owner)

    def _previews(self, manifest):
        logs = manifest["logs"]
        first_failure = next(
            (i for i, row in enumerate(logs) if row.get("returncode") not in (None, 0)),
            None,
        )
        chosen = logs[-4:]
        if first_failure is not None:
            chosen = logs[first_failure : first_failure + 2] + chosen
        return list({row["log_id"]: row for row in chosen}.values())

    def view(self, *, owner="unknown"):
        with self._lock:
            manifest = _read_manifest(self.directory)
        failures = [row for row in manifest["failures"] if not row.get("secondary")]
        preview_failures = failures[:1] + (failures[-1:] if len(failures) > 1 else [])
        return {
            "owner": owner,
            "stage": failures[0]["stage"] if failures else "execute",
            "returncode": failures[0]["returncode"] if failures else None,
            "cause_chain": [
                cause
                for failure in preview_failures
                for cause in failure["cause_chain"]
            ],
            "logs": self._previews(manifest),
            "log_count": len(manifest["logs"]),
            "capture_errors": self.capture_errors[-4:],
            "failure_count": len(failures),
            "directory": str(self.directory),
            "manifest": str(self.directory / _MANIFEST),
            "read_command": shlex.join(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "read",
                    "--directory",
                    str(self.directory),
                ]
            ),
        }


def fetch_remote_log(backend, config, remote, *, relative="run.log"):
    """Fetch one named log after settlement; failure remains secondary evidence."""
    if relative not in {"run.log", "train/train.csv"}:
        raise ValueError("Only explicit run/training logs can be collected")
    diagnostics = backend.command_diagnostics
    script = (
        "import sys; from pathlib import Path; p=Path("
        + repr(remote + "/" + relative)
        + "); "
        + "assert not p.is_symlink(); import shutil; shutil.copyfileobj(p.open('rb'), sys.stdout.buffer)"
    )
    token = _SECONDARY.set(True)
    try:
        backend.ssh(config, shlex.join([config["remote_python"], "-c", script]))
    except Exception as error:
        diagnostics.capture_errors.append(
            diagnostic_view(
                error,
                owner="pipeline",
                stage="log_collection",
                secrets=diagnostics.secrets,
            )
        )
    finally:
        _SECONDARY.reset(token)
    return diagnostics.view()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("read")
    read.add_argument("--directory", required=True)
    read.add_argument("--log-id", required=True)
    read.add_argument("--offset", type=int, default=0)
    read.add_argument("--limit", type=int, default=8192)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            read_log_page(
                args.directory, args.log_id, offset=args.offset, limit=args.limit
            )
        )
    )


if __name__ == "__main__":
    main()
