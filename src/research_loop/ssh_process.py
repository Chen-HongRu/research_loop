"""Lead-only SSH process controls with original-identity reconciliation.

The operator installs a remote JSON process launcher. This module knows its
process protocol, not the research task, scheduler, pricing or scientific result.
"""

import asyncio
import base64
import fcntl
import json
import os
import re
import shlex
from pathlib import Path
from uuid import uuid4

from .codex import Tool
from .state import now, write_json

TERMINAL = {"completed", "failed", "stopped"}


class SSHProcess:
    def __init__(self, config, run_root):
        self.config = dict(config)
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", config.get("host", "")
        ):
            raise ValueError("host must be a safe SSH alias")
        for key in ("remote_root", "remote_python", "remote_launcher"):
            value = config.get(key)
            if (
                not isinstance(value, str)
                or not value.startswith("/")
                or any(char in value for char in "\n\r\x00")
            ):
                raise ValueError(f"{key} must be an absolute POSIX path")
        for key in ("allow_start", "cpu_smoke"):
            if type(config.get(key, False)) is not bool:
                raise ValueError(f"{key} must be a boolean")
            self.config[key] = config.get(key, False)
        gpu = config.get("gpu")
        if gpu is not None and (
            not isinstance(gpu, str) or not re.fullmatch(r"[0-9]+(,[0-9]+)*", gpu)
        ):
            raise ValueError("gpu must be null or explicit numeric device IDs")
        self.config["gpu"] = gpu
        if self.config["allow_start"] and self.config["cpu_smoke"] == (gpu is not None):
            raise ValueError("enabled starts require frozen gpu or explicit cpu_smoke")
        if not config.get("ledger_path"):
            raise ValueError("ledger_path is required for the job registry")
        self.path = Path(config["ledger_path"]).resolve()
        self.root = Path(run_root).resolve()
        self.identity = {
            key: self.config[key]
            for key in (
                "host",
                "remote_root",
                "remote_python",
                "remote_launcher",
                "gpu",
                "cpu_smoke",
            )
        }
        self._lock = None
        self._operation = asyncio.Lock()
        self._closed = False
        self._close_result = None
        self._read()  # Configuration/metadata only; no SSH or writes.

    def _read(self):
        if not self.path.exists():
            lock = self.path.with_name(self.path.name + ".lock")
            if lock.exists() and lock.stat().st_size:
                raise ValueError("initialized job registry is missing")
            return {"schema_version": 1, "identity": self.identity, "entries": []}
        data = json.loads(self.path.read_text())
        if data.get("schema_version") != 1 or data.get("identity") != self.identity:
            raise ValueError("original SSH process registry identity differs")
        entries = data["entries"]
        if not isinstance(entries, list) or len(
            {item["job_id"] for item in entries}
        ) != len(entries):
            raise ValueError("invalid or duplicate registry job identities")
        return data

    def _open(self):
        if self._lock is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.with_name(self.path.name + ".lock").open("a+")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            data = self._read()
            write_json(self.path, data)
            stream.write("ssh-process-registry-v1\n")
            stream.flush()
            self._lock = stream
        except BaseException:
            stream.close()
            raise

    def status(self):
        return {
            "adapter": "ssh-process",
            "closed": self._closed,
            "allow_start": self.config["allow_start"],
            "gpu": self.config["gpu"],
            "reservations": self._read()["entries"],
        }

    def tools(self):
        def tool(name, description, properties, required, function):
            async def call(arguments):
                task = asyncio.create_task(function(**arguments))
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise

            return Tool(
                name,
                description,
                {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
                call,
            )

        identity = {"job_id": {"type": "string"}}
        return [
            tool(
                "ssh_prepare",
                "Read remote process launcher preflight; never starts a job.",
                {},
                [],
                self.prepare,
            ),
            tool(
                "ssh_start",
                "Start one original argv once, only with operator-enabled frozen device config; never retry an uncertain identity.",
                {
                    **identity,
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "config_paths": {"type": "array", "items": {"type": "string"}},
                },
                ["job_id", "argv"],
                self.start,
            ),
            tool(
                "ssh_status",
                "Reconcile only a recorded original job ID against its remote receipt.",
                identity,
                ["job_id"],
                self.reconcile,
            ),
            tool(
                "ssh_stop",
                "Request the remote launcher's verified owned-process stop, then reconcile; never broad kill.",
                identity,
                ["job_id"],
                self.stop,
            ),
            tool(
                "ssh_fetch",
                "Copy one explicit owned-job artifact into a new local work/artifacts file; no traversal or overwrite. Default maximum 64 MiB.",
                {
                    **identity,
                    "relative_path": {"type": "string"},
                    "max_bytes": {"type": "integer", "minimum": 1},
                },
                ["job_id", "relative_path"],
                self.fetch,
            ),
        ]

    async def _remote(self, action, **arguments):
        command = shlex.join(
            [
                self.config["remote_python"],
                self.config["remote_launcher"],
                "_remote",
            ]
        )
        child = await asyncio.create_subprocess_exec(
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            self.config["host"],
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        request = {"action": action, "project": self.config["remote_root"], **arguments}
        try:
            async with asyncio.timeout(20):
                output, error = await child.communicate(json.dumps(request).encode())
        except BaseException:
            if child.returncode is None:
                child.kill()
            await child.wait()
            raise
        if child.returncode:
            raise RuntimeError(
                f"SSH process request failed ({child.returncode}): "
                + (error or output).decode(errors="replace")[:2000]
            )
        receipt = json.loads(output)
        if not isinstance(receipt, dict) or receipt.get("state") == "error":
            raise RuntimeError("remote process request returned an error")
        return receipt

    async def prepare(self):
        return await self._remote("prepare")

    def _entry(self, data, job_id):
        matches = [item for item in data["entries"] if item["job_id"] == job_id]
        if not matches or matches[0]["run_root"] != str(self.root):
            raise ValueError("job ID is not recorded as owned by this run")
        return matches[0]

    def _receipt(self, entry, receipt):
        if (
            receipt.get("job_id") != entry["job_id"]
            or receipt.get("controller_token") != entry["controller_token"]
        ):
            raise ValueError("remote receipt job identity differs")
        entry.update(
            status="TERMINAL" if receipt.get("state") in TERMINAL else "UNKNOWN",
            receipt=receipt,
            observed_at=now(),
        )

    async def start(self, job_id, argv, config_paths=None):
        async with self._operation:
            if self._closed or not self.config["allow_start"]:
                raise ValueError(
                    "SSH process starts are disabled by operator configuration"
                )
            if (self.root / "STOP").exists():
                raise ValueError("STOP prevents new process dispatch")
            if not isinstance(job_id, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", job_id
            ):
                raise ValueError("job_id must be a simple unique name")
            if (
                not isinstance(argv, list)
                or not argv
                or not all(isinstance(item, str) for item in argv)
            ):
                raise ValueError("argv must be a nonempty string list")
            if config_paths is not None and (
                not isinstance(config_paths, list)
                or not all(isinstance(item, str) for item in config_paths)
            ):
                raise ValueError("config_paths must be a string list")
            self._open()
            data = self._read()
            if any(item["job_id"] == job_id for item in data["entries"]):
                raise ValueError(
                    "job ID already reserved; reconcile the original, never retry start"
                )
            if any(item["status"] != "TERMINAL" for item in data["entries"]):
                raise ValueError(
                    "reconcile outstanding original jobs before new dispatch"
                )
            entry = {
                "reservation_id": job_id,
                "job_id": job_id,
                "run_root": str(self.root),
                "status": "UNKNOWN",
                "controller_token": uuid4().hex,
                "requested_at": now(),
                "argv": list(argv),
            }
            data["entries"].append(entry)
            write_json(self.path, data)  # Durable original ID before any remote effect.
            try:
                receipt = await self._remote(
                    "start",
                    job_id=job_id,
                    argv=argv,
                    config_paths=config_paths or [],
                    gpu=self.config["gpu"],
                    cpu_smoke=self.config["cpu_smoke"],
                    controller_token=entry["controller_token"],
                )
                self._receipt(entry, receipt)
            except BaseException as error:
                entry["error"] = type(error).__name__
                raise
            finally:
                write_json(self.path, data)
            return receipt

    async def _control(self, action, job_id):
        async with self._operation:
            self._open()
            data = self._read()
            entry = self._entry(data, job_id)
            if action == "stop" and entry["status"] == "TERMINAL":
                return entry["receipt"]
            try:
                receipt = await self._remote(
                    action, job_id=job_id, controller_token=entry["controller_token"]
                )
                self._receipt(entry, receipt)
                return receipt
            except BaseException as error:
                entry.update(status="UNKNOWN", error=type(error).__name__)
                raise
            finally:
                write_json(self.path, data)

    async def reconcile(self, job_id):
        return await self._control("status", job_id)

    async def stop(self, job_id):
        await self._control("stop", job_id)
        return await self.reconcile(job_id)

    def _artifact_file(self, job_id, relative):
        # Walk beneath the protected run root using no-follow directory handles;
        # an agent-controlled symlink in work cannot redirect the host's writes.
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in ("work", "artifacts", job_id, *relative.parts[:-1]):
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                next_descriptor = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = next_descriptor
            target = os.open(
                relative.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=descriptor,
            )
            return os.fdopen(target, "wb")
        finally:
            os.close(descriptor)

    async def fetch(self, job_id, relative_path, max_bytes=67108864):
        if not isinstance(relative_path, str) or not relative_path:
            raise ValueError("relative_path is required")
        relative = Path(relative_path)
        if relative.is_absolute() or any(
            part in {"..", "."} for part in relative.parts
        ):
            raise ValueError("artifact path must be relative without traversal")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        async with self._operation:
            entry = self._entry(self._read(), job_id)
            chunk = await self._remote(
                "read_artifact",
                job_id=job_id,
                relative_path=str(relative),
                offset=0,
                controller_token=entry["controller_token"],
            )
            size = chunk["size"]
            if type(size) is not int or not 0 <= size <= max_bytes:
                raise ValueError("artifact exceeds requested maximum")
            offset = 0
            with self._artifact_file(job_id, relative) as stream:
                while True:
                    if (
                        chunk.get("job_id") != job_id
                        or chunk.get("offset") != offset
                        or chunk.get("size") != size
                    ):
                        raise ValueError(
                            "artifact identity, offset or size changed during transfer"
                        )
                    content = base64.b64decode(chunk["content_base64"], validate=True)
                    if len(content) > 1048576 or offset + len(content) > size:
                        raise ValueError("invalid artifact chunk")
                    stream.write(content)
                    offset += len(content)
                    if offset == size:
                        break
                    if not content:
                        raise ValueError("incomplete artifact transfer")
                    chunk = await self._remote(
                        "read_artifact",
                        job_id=job_id,
                        relative_path=str(relative),
                        offset=offset,
                        controller_token=entry["controller_token"],
                    )
            return {
                "job_id": job_id,
                "bytes": offset,
                "local_path": str(self.root / "work/artifacts" / job_id / relative),
            }

    async def close(self):
        if self._close_result is not None:
            return self._close_result
        self._closed = True
        errors = []
        try:
            for entry in self._read()["entries"]:
                if (
                    entry["run_root"] == str(self.root)
                    and entry["status"] != "TERMINAL"
                ):
                    try:
                        await self.stop(entry["job_id"])
                    except Exception as error:
                        errors.append(
                            {"job_id": entry["job_id"], "error": type(error).__name__}
                        )
            self._close_result = {**self.status(), "cleanup_errors": errors}
            return self._close_result
        finally:
            if self._lock:
                self._lock.close()
                self._lock = None
