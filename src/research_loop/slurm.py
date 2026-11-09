"""Single-GPU Slurm allocations with durable, study-wide resource accounting.

Commands run through SSH on a held salloc allocation. This is resource accounting,
not a shell sandbox: callers retain their configured remote account permissions.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import shlex
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .codex import Tool

TERMINAL = {
    "COMPLETED",
    "CANCELLED",
    "FAILED",
    "TIMEOUT",
    "OUT_OF_MEMORY",
    "NODE_FAIL",
    "PREEMPTED",
    "BOOT_FAIL",
    "DEADLINE",
    "REVOKED",
}
SSH_OPTIONS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ClearAllForwardings=yes",
    "-o",
    "ControlMaster=no",
    "-o",
    "ControlPath=none",
    "-o",
    "StrictHostKeyChecking=yes",
    "-o",
    "UpdateHostKeys=no",
    "-o",
    "ConnectTimeout=12",
)
BUFFER_BYTES = 128 * 1024


def _save(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _validate(ledger: dict, authorized: int) -> None:
    if (
        ledger.get("schema_version") != 1
        or ledger.get("authorized_seconds") != authorized
        or not isinstance(ledger.get("entries"), list)
    ):
        raise ValueError(
            "GPU ledger schema/authorization mismatch; never reset spending"
        )
    identities, names, jobs = set(), set(), set()
    for entry in ledger["entries"]:
        if not isinstance(entry, dict):
            raise ValueError("GPU reservation must be an object")
        identity, name = entry.get("reservation_id"), entry.get("job_name")
        if (
            not isinstance(identity, str)
            or not identity
            or identity in identities
            or not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name)
            or name in names
        ):
            raise ValueError("invalid/duplicate GPU reservation identity")
        identities.add(identity)
        names.add(name)
        if (
            type(entry.get("reserved_seconds")) is not int
            or entry["reserved_seconds"] < 0
            or entry.get("status") not in {"RESERVED", "UNKNOWN", "TERMINAL"}
        ):
            raise ValueError("invalid GPU reservation")
        receipt = entry.get("receipt", {})
        if not isinstance(receipt, dict):
            raise ValueError("GPU receipt must be an object")
        if (
            entry.get("job_id")
            and receipt.get("job_id")
            and entry["job_id"] != receipt["job_id"]
        ):
            raise ValueError("GPU receipt conflicts with original job ID")
        job = entry.get("job_id") or receipt.get("job_id")
        if job is not None:
            if not isinstance(job, str) or not job.isdigit() or job in jobs:
                raise ValueError("invalid/duplicate original Slurm job ID")
            jobs.add(job)
        if entry["status"] == "TERMINAL":
            if (
                receipt.get("state") not in TERMINAL | {"NOT_DISPATCHED"}
                or type(receipt.get("elapsed_seconds")) is not int
                or receipt["elapsed_seconds"] < 0
                or (receipt["state"] != "NOT_DISPATCHED" and job is None)
                or (
                    receipt["state"] == "NOT_DISPATCHED"
                    and receipt["elapsed_seconds"] != 0
                )
            ):
                raise ValueError(
                    "terminal GPU receipt lacks exact original-job accounting"
                )


def _summary(ledger: dict) -> dict:
    used = sum(
        e["receipt"]["elapsed_seconds"]
        for e in ledger["entries"]
        if e["status"] == "TERMINAL"
    )
    reserved = sum(
        e["reserved_seconds"] for e in ledger["entries"] if e["status"] != "TERMINAL"
    )
    return {
        "authorized_seconds": ledger["authorized_seconds"],
        "used_seconds": used,
        "reserved_seconds": reserved,
        "remaining_seconds": max(0, ledger["authorized_seconds"] - used - reserved),
        "continuation_blocked": any(
            e["status"] != "TERMINAL" or not e.get("continuation_authorized", False)
            for e in ledger["entries"]
        ),
    }


@dataclass
class _Session:
    child: asyncio.subprocess.Process
    log_path: Path
    reservation_id: str
    output: bytearray = field(default_factory=bytearray)
    dropped: int = 0
    done: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task: asyncio.Task | None = None
    error: str | None = None


class Slurm:
    """One sequential allocation at a time, sharing a locked cumulative ledger."""

    def __init__(self, config: dict, run_root: Path):
        self.config = dict(config)
        for key in ("host", "partition", "account"):
            value = config.get(key)
            if key == "account" and value is None:
                continue
            if not isinstance(value, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value
            ):
                raise ValueError(f"{key} must be a safe SSH alias/Slurm identifier")
        root = config.get("remote_root")
        if (
            not isinstance(root, str)
            or not root.startswith("/")
            or any(c in root for c in "\n\r\x00")
        ):
            raise ValueError("remote_root must be an absolute POSIX path")
        for key, minimum in (
            ("gpu_seconds_total", 1),
            ("job_seconds", 60),
            ("cpus", 1),
            ("grace_seconds", 0),
        ):
            value = config.get(key, {"cpus": 4, "grace_seconds": 30}.get(key))
            if type(value) is not int or value < minimum:
                raise ValueError(f"{key} must be an integer >= {minimum}")
            self.config[key] = value
        if config["job_seconds"] % 60:
            raise ValueError("job_seconds must be an exact whole number of minutes")
        memory = config.get("memory", "48G")
        if not isinstance(memory, str) or not re.fullmatch(
            r"[1-9][0-9]*[KMGTP]?", memory
        ):
            raise ValueError("memory must be a positive Slurm memory quantity")
        self.config["memory"] = memory
        ledger_path = config.get("ledger_path")
        if not isinstance(ledger_path, (str, Path)) or not str(ledger_path):
            raise ValueError("ledger_path is required")
        self.path, self.root = Path(ledger_path).expanduser().absolute(), Path(run_root)
        self._ledger = None
        self._file_lock = None
        self._operation = asyncio.Lock()
        self._current = None
        self._allocator = None
        self._allocator_task = None
        self._ready = asyncio.Event()
        self._marker = "__RL_ALLOC_" + uuid.uuid4().hex + "__"
        self._sessions: dict[str, _Session] = {}
        self._closing_allocation = False
        self._closed = False
        self._close_result = None

    def tools(self) -> list[Tool]:
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

        return [
            tool(
                "gpu_status",
                "Read the current budget, original reservation IDs, and sessions without network access or resource changes.",
                {},
                [],
                self._status,
            ),
            tool(
                "gpu_exec",
                "Execute a shell command in the original single-GPU allocation. Poll returned session IDs; commands are never automatically retried.",
                {"command": {"type": "string"}},
                ["command"],
                self._exec,
            ),
            tool(
                "gpu_poll",
                "Read incremental command output and optionally send stdin to that existing session.",
                {"session_id": {"type": "string"}, "chars": {"type": "string"}},
                ["session_id"],
                self._poll,
            ),
            tool(
                "gpu_reconcile",
                "Query the original reserved job only; exact terminal accounting can unblock its budget. Never submits a replacement.",
                {"reservation_id": {"type": "string"}},
                ["reservation_id"],
                self._reconcile,
            ),
            tool(
                "gpu_release",
                "Release an idle allocation and reconcile its exact accounting before further allocation.",
                {},
                [],
                self._release,
            ),
        ]

    def _open(self):
        if self._file_lock is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_name(self.path.name + ".lock").open("a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock.seek(0)
            initialized = bool(lock.read())
            if self.path.exists():
                ledger = json.loads(self.path.read_text())
                _validate(ledger, self.config["gpu_seconds_total"])
            elif initialized:
                raise ValueError(
                    "initialized GPU ledger is missing; refusing budget reset"
                )
            else:
                ledger = {
                    "schema_version": 1,
                    "authorized_seconds": self.config["gpu_seconds_total"],
                    "entries": [],
                }
                _save(self.path, ledger)
            if not initialized:
                lock.write("budget-initialized-v1\n")
                lock.flush()
                os.fsync(lock.fileno())
            self._file_lock, self._ledger = lock, ledger
        except BaseException:
            lock.close()
            raise

    def _persist(self):
        _validate(self._ledger, self.config["gpu_seconds_total"])
        _save(self.path, self._ledger)

    def status(self) -> dict:
        ledger = self._ledger
        if ledger is None:
            lock_path = self.path.with_name(self.path.name + ".lock")
            if (
                not self.path.exists()
                and lock_path.exists()
                and lock_path.stat().st_size
            ):
                raise ValueError(
                    "initialized GPU ledger is missing; refusing budget reset"
                )
            ledger = (
                json.loads(self.path.read_text())
                if self.path.exists()
                else {
                    "schema_version": 1,
                    "authorized_seconds": self.config["gpu_seconds_total"],
                    "entries": [],
                }
            )
            _validate(ledger, self.config["gpu_seconds_total"])
        return {
            "closed": self._closed,
            "budget": _summary(ledger),
            "reservations": json.loads(json.dumps(ledger["entries"])),
            "sessions": list(self._sessions),
        }

    async def _status(self) -> dict:
        return self.status()

    def _ssh(self, command: str) -> list[str]:
        return ["ssh", *SSH_OPTIONS, self.config["host"], command]

    async def _query(self, argv: list[str]) -> str:
        child = await asyncio.create_subprocess_exec(
            *self._ssh(shlex.join(argv)),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(child.communicate(), 20)
        except BaseException:
            if child.returncode is None:
                child.kill()
            await child.wait()
            raise
        if child.returncode:
            raise RuntimeError(
                f"Slurm query failed ({child.returncode}): {stderr.decode(errors='replace')[:1000]}"
            )
        return stdout.decode(errors="replace")

    def _unknown(self, reason):
        if self._current is not None and self._current["status"] != "TERMINAL":
            self._current.update(status="UNKNOWN", transport_error=str(reason)[:1000])
            self._persist()

    async def _read_allocator(self, entry, child):
        pending = b""
        try:
            with (self.root / (entry["reservation_id"] + ".allocator.log")).open(
                "ab"
            ) as log:
                while chunk := await child.stdout.read(8192):
                    log.write(chunk)
                    log.flush()
                    pending = (pending + chunk)[-BUFFER_BYTES:]
                    match = re.search(
                        re.escape(self._marker.encode()) + rb"([0-9]+)\r?\n", pending
                    )
                    if match and not self._ready.is_set():
                        entry["job_id"] = match[1].decode()
                        self._persist()
                        self._ready.set()
            await child.wait()
            if not self._closing_allocation:
                self._unknown(
                    f"allocator exited ({child.returncode}); reconcile original job"
                )
        except Exception as error:
            self._unknown(error)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
            self._ready.set()

    async def _allocate(self):
        summary = _summary(self._ledger)
        if summary["continuation_blocked"]:
            raise RuntimeError(
                "unresolved original GPU reservation; use gpu_status for its reservation_id, then gpu_reconcile"
            )
        seconds = min(
            self.config["job_seconds"],
            ((summary["remaining_seconds"] - self.config["grace_seconds"]) // 60) * 60,
        )
        if seconds < 60:
            raise RuntimeError(
                "GPU budget exhausted: requires a minute plus termination grace"
            )
        identity = uuid.uuid4().hex
        entry = {
            "reservation_id": identity,
            "job_name": "rl-" + identity,
            "host_alias": self.config["host"],
            "status": "RESERVED",
            "requested_wall_seconds": seconds,
            "termination_grace_seconds": self.config["grace_seconds"],
            "reserved_seconds": seconds + self.config["grace_seconds"],
            "continuation_authorized": False,
            "dispatched_at": datetime.now(UTC).isoformat(),
        }
        self._ledger["entries"].append(entry)
        self._persist()  # Durable reservation BEFORE any SSH allocation dispatch.
        self._current = entry
        self._ready = asyncio.Event()
        self._closing_allocation = False
        holder = f'printf {shlex.quote(self._marker + "%s\\n")} "$SLURM_JOB_ID"; while IFS= read -r _; do :; done'
        argv = [
            "salloc",
            "--immediate=60",
            "--kill-command=TERM",
            "-p",
            self.config["partition"],
            "-N1",
            "-n1",
            f"-c{self.config['cpus']}",
            f"--mem={self.config['memory']}",
            "--gres=gpu:1",
            f"--time={seconds // 60}",
            "--job-name=" + entry["job_name"],
        ]
        if self.config.get("account"):
            argv.append("--account=" + self.config["account"])
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            if (self.root / "STOP").exists():
                entry.update(
                    status="TERMINAL",
                    continuation_authorized=True,
                    receipt={
                        "state": "NOT_DISPATCHED",
                        "job_id": None,
                        "job_name": entry["job_name"],
                        "elapsed_seconds": 0,
                        "gpu_hours": 0.0,
                    },
                )
                self._persist()
                raise RuntimeError(
                    "STOP recorded before allocation dispatch; no GPU allocation submitted"
                )
            self._allocator = await asyncio.create_subprocess_exec(
                *self._ssh(shlex.join([*argv, "bash", "-c", holder])),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self._allocator_task = asyncio.create_task(
                self._read_allocator(entry, self._allocator)
            )
            await asyncio.wait_for(self._ready.wait(), 80)
            if (
                entry["status"] != "RESERVED"
                or not entry.get("job_id")
                or self._allocator.returncode is not None
            ):
                raise RuntimeError(
                    "allocation acknowledgement lost; reconcile original reservation"
                )
        except BaseException as error:
            if entry["status"] == "TERMINAL":
                # STOP before SSH dispatch proves this original reservation had no effect.
                raise
            self._unknown(error)
            if isinstance(error, Exception):
                raise RuntimeError(
                    f"allocation dispatch uncertain for original reservation {identity}; "
                    "use gpu_status, then gpu_reconcile with its reservation_id; no replacement submitted"
                ) from error
            raise

    async def _read_session(self, session):
        try:
            with session.log_path.open("ab") as log:
                while chunk := await session.child.stdout.read(8192):
                    log.write(chunk)
                    log.flush()
                    session.output.extend(chunk)
                    overflow = max(0, len(session.output) - BUFFER_BYTES)
                    if overflow:
                        del session.output[:overflow]
                        session.dropped += overflow
            await session.child.wait()
            if session.child.returncode == 255:
                self._unknown(
                    "step SSH transport lost; command effect is unknown, never retry automatically"
                )
        except Exception as error:
            session.error = str(error)[:1000]
            self._unknown(error)
        finally:
            if session.child.returncode is None:
                session.child.kill()
                await session.child.wait()
            session.done.set()

    async def _update(self, identity, session, wait):
        async with session.lock:
            try:
                await asyncio.wait_for(session.done.wait(), wait)
            except TimeoutError:
                pass
            output = bytes(session.output).decode(errors="replace")
            session.output.clear()
            if session.dropped:
                output = (
                    f"[{session.dropped} earlier bytes truncated; full log retained]\n"
                    + output
                )
                session.dropped = 0
            return {
                "session_id": identity,
                "running": not session.done.is_set(),
                "exit_code": session.child.returncode
                if session.done.is_set()
                else None,
                "output": output,
                "log_path": str(session.log_path),
                "error": session.error,
                "reservation_id": session.reservation_id,
            }

    async def _exec(self, command: str):
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise ValueError("command must be a nonempty shell string without NUL")
        async with self._operation:
            if self._closed:
                raise RuntimeError("GPU adapter is closed")
            if (self.root / "STOP").exists():
                raise RuntimeError("STOP recorded; GPU command dispatch is stopped")
            self._open()
            if any(not s.done.is_set() for s in self._sessions.values()):
                raise RuntimeError(
                    "GPU command still running; poll its existing session"
                )
            if self._current is None or self._current["status"] == "TERMINAL":
                if self._current is not None:
                    await self._stop_holder()
                await self._allocate()
            if (
                self._current["status"] != "RESERVED"
                or self._allocator is None
                or self._allocator.returncode is not None
            ):
                raise RuntimeError(
                    "allocation uncertain; use gpu_status for the original reservation_id, "
                    "then gpu_reconcile before execution"
                )
            step = [
                "srun",
                "--jobid",
                self._current["job_id"],
                "--exact",
                "-N1",
                "-n1",
                f"-c{self.config['cpus']}",
                "--gres=gpu:1",
                "bash",
                "-lc",
                "cd " + shlex.quote(self.config["remote_root"]) + " &&\n" + command,
            ]
            if (self.root / "STOP").exists():
                await self._finish(cancel=True)
                raise RuntimeError(
                    "STOP recorded before command dispatch; original allocation cleanup attempted; "
                    "use gpu_status for its reservation and accounting"
                )
            try:
                child = await asyncio.create_subprocess_exec(
                    *self._ssh(shlex.join(step)),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except BaseException as error:
                self._unknown(error)
                if isinstance(error, Exception):
                    raise RuntimeError(
                        f"step dispatch uncertain for original reservation {self._current['reservation_id']}; "
                        "use gpu_status, then gpu_reconcile; command was not retried"
                    ) from error
                raise
            identity = uuid.uuid4().hex
            session = _Session(
                child, self.root / (identity + ".log"), self._current["reservation_id"]
            )
            self._sessions[identity] = session
            session.task = asyncio.create_task(self._read_session(session))
        return await self._update(identity, session, 1)

    async def _poll(self, session_id: str, chars: str = ""):
        if not isinstance(chars, str):
            raise ValueError("chars must be a string")
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError("unknown GPU session ID")
        if chars and not session.done.is_set():
            session.child.stdin.write(chars.encode())
            try:
                await session.child.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
        return await self._update(session_id, session, 0.25)

    async def _observe(self, entry):
        if entry.get("host_alias", self.config["host"]) != self.config["host"]:
            raise ValueError("reconciliation requires the original configured SSH host")
        output = await self._query(
            ["squeue", "-h", "--name", entry["job_name"], "-o", "%i|%j|%T"]
        )
        rows = [line.split("|") for line in output.splitlines() if line.strip()]
        if (
            any(
                len(row) != 3 or row[1] != entry["job_name"] or not row[0].isdigit()
                for row in rows
            )
            or len(rows) > 1
        ):
            raise RuntimeError("ambiguous original-name queue observation")
        job = entry.get("job_id") or entry.get("receipt", {}).get("job_id")
        if rows:
            if job is not None and job != rows[0][0]:
                raise RuntimeError("original job identity conflicts with queue")
            entry["job_id"] = rows[0][0]
            self._persist()
        start = (
            datetime.fromisoformat(
                entry.get("dispatched_at", "1970-01-02T00:00:00+00:00")
            )
            - timedelta(days=1)
        ).strftime("%Y-%m-%d")
        argv = ["sacct", "-n", "-P", "-X", "-S", start]
        argv += ["-j", job] if job else ["--name", entry["job_name"]]
        output = await self._query(
            [*argv, "-o", "JobIDRaw,JobName%128,State,ElapsedRaw,AllocTRES%256"]
        )
        accounting = [
            row
            for line in output.splitlines()
            if len(row := line.split("|")) >= 5
            and row[1] == entry["job_name"]
            and row[0].isdigit()
            and (job is None or row[0] == job)
        ]
        if not accounting and not rows and job is None:
            # OpenSSH's banner exchange precedes authentication and remote command
            # execution. An exact, protected allocator diagnostic proves that this
            # attempt never reached salloc; absence from accounting alone does not.
            log = self.root / (entry["reservation_id"] + ".allocator.log")
            try:
                with log.open("rb") as stream:
                    diagnostic = stream.read(4097)
            except OSError:
                diagnostic = b""
            if len(diagnostic) <= 4096 and re.fullmatch(
                rb"Connection timed out during banner exchange\r?\n"
                rb"Connection to [^\r\n]+ port [0-9]+ timed out\r?\n",
                diagnostic,
            ):
                receipt = {
                    "state": "NOT_DISPATCHED",
                    "job_id": None,
                    "job_name": entry["job_name"],
                    "elapsed_seconds": 0,
                    "gpu_hours": 0.0,
                    "evidence": "OpenSSH banner timeout before remote command",
                }
                entry.update(
                    status="TERMINAL", receipt=receipt, continuation_authorized=True
                )
                self._persist()
                return receipt
        if len(accounting) != 1:
            raise RuntimeError(
                "exact original allocation accounting unavailable; reservation remains charged"
            )
        row = accounting[0]
        if rows and rows[0][0] != row[0]:
            raise RuntimeError("queue and accounting original job identities disagree")
        entry["job_id"] = row[0]
        self._persist()
        state = row[2].split()[0].rstrip("+") if row[2].strip() else "UNKNOWN"
        if (
            rows
            or state not in TERMINAL
            or not row[3].isdigit()
            or not re.search(r"(?:^|,)gres/gpu=1(?:,|$)", row[4])
        ):
            raise RuntimeError(
                "original job is live or lacks exact terminal single-GPU accounting"
            )
        seconds = int(row[3])
        receipt = {
            "job_id": row[0],
            "job_name": entry["job_name"],
            "state": state,
            "elapsed_seconds": seconds,
            "gpu_hours": seconds / 3600,
        }
        entry.update(
            status="TERMINAL",
            receipt=receipt,
            continuation_authorized=seconds <= entry["reserved_seconds"],
        )
        self._persist()
        return receipt

    async def _stop_holder(self):
        self._closing_allocation = True
        if self._allocator is not None:
            try:
                if self._allocator.stdin:
                    self._allocator.stdin.close()
                await asyncio.wait_for(asyncio.shield(self._allocator_task), 5)
            except TimeoutError:
                if self._allocator.returncode is None:
                    self._allocator.kill()
                await self._allocator_task
            finally:
                if self._allocator.returncode is None:
                    self._allocator.kill()
                await self._allocator.wait()

    async def _finish(self, *, cancel):
        entry = self._current
        if entry is None:
            return {"state": "NOT_DISPATCHED", "job_id": None, "elapsed_seconds": 0}
        if entry["status"] == "TERMINAL":
            await self._stop_holder()
            return entry["receipt"]
        errors = []
        try:
            if cancel:
                queue = await self._query(
                    ["squeue", "-h", "--name", entry["job_name"], "-o", "%i|%j|%T"]
                )
                rows = [line.split("|") for line in queue.splitlines() if line.strip()]
                if (
                    len(rows) == 1
                    and len(rows[0]) == 3
                    and rows[0][1] == entry["job_name"]
                    and rows[0][0].isdigit()
                    and (not entry.get("job_id") or entry["job_id"] == rows[0][0])
                ):
                    entry["job_id"] = rows[0][0]
                    self._persist()
                    await self._query(["scancel", entry["job_id"]])
        except Exception as error:
            errors.append(str(error)[:1000])
        finally:
            try:
                await self._stop_holder()
            except Exception as error:
                errors.append(str(error)[:1000])
        try:
            return await self._observe(entry)
        except Exception as error:
            errors.append(str(error)[:1000])
            entry.update(
                status="UNKNOWN",
                continuation_authorized=False,
                receipt={
                    "state": "UNKNOWN",
                    "job_id": entry.get("job_id"),
                    "job_name": entry["job_name"],
                    "elapsed_seconds": None,
                    "gpu_hours": None,
                    "errors": errors,
                },
            )
            self._persist()
            return entry["receipt"]

    async def _release(self):
        async with self._operation:
            if self._closed:
                raise RuntimeError("GPU adapter is closed")
            if any(not s.done.is_set() for s in self._sessions.values()):
                raise RuntimeError("cannot release GPU with an unsettled command")
            self._open()
            return await self._finish(cancel=True)

    async def _reconcile(self, reservation_id: str):
        async with self._operation:
            if self._closed:
                raise RuntimeError("GPU adapter is closed")
            self._open()
            entries = [
                e
                for e in self._ledger["entries"]
                if e["reservation_id"] == reservation_id
            ]
            if len(entries) != 1:
                raise ValueError("unknown original reservation ID")
            entry = entries[0]
            if entry["status"] == "TERMINAL":
                return json.loads(json.dumps(entry["receipt"]))
            if entry is self._current and any(
                not s.done.is_set() for s in self._sessions.values()
            ):
                raise RuntimeError(
                    "poll original running command before reconciliation"
                )
            # A live holder is not silently released by read-only reconciliation.
            return await self._observe(entry)

    async def close(self) -> dict:
        task = asyncio.create_task(self._close())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _close(self) -> dict:
        async with self._operation:
            if self._close_result is not None:
                return self._close_result
            self._closed = True
            cleanup_errors = []
            try:
                for session in self._sessions.values():
                    if session.child.returncode is None:
                        try:
                            session.child.terminate()
                        except ProcessLookupError:
                            pass
                for session in self._sessions.values():
                    try:
                        await asyncio.wait_for(asyncio.shield(session.task), 5)
                    except TimeoutError:
                        if session.child.returncode is None:
                            session.child.kill()
                        await session.task
                    except Exception as error:
                        cleanup_errors.append(str(error)[:1000])
                receipt = await self._finish(cancel=True)
                self._close_result = {
                    "receipt": receipt,
                    "cleanup_errors": cleanup_errors,
                    **self.status(),
                }
                return self._close_result
            finally:
                if self._file_lock is not None:
                    self._file_lock.close()
                    self._file_lock = None
