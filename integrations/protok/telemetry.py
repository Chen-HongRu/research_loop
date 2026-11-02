"""Bounded, failure-isolated live observations from already owned allocations."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import shlex
import threading
import time
import re
import subprocess
import uuid

from research_loop import telemetry as store

MAX_POLL_BYTES = 128 * 1024
MAX_LINE_BYTES = 8192
POLL_SECONDS = 5
SSH_TIMEOUT = 10


def writer_source():
    """Freeze this small stdlib writer independently of canonical trainer locks."""
    data = Path(store.__file__).read_bytes()
    return data, hashlib.sha256(data).hexdigest()


def stage_writer(destination, data):
    Path(destination, "research_loop_events.py").write_bytes(data)


def _read_remote(ssh, config, path, offset, *, timeout=SSH_TIMEOUT):
    # Only metadata and the next bounded byte page are read. A short partial
    # tail waits for its newline; a proven oversized fragment advances raw bytes
    # so malformed stdout cannot hide later complete observations.
    script = f"""import base64,json,os,stat
path={str(path)!r}; offset={offset!r}; result={{"missing":True}}
try:
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
 try:
  info=os.fstat(fd)
  if not stat.S_ISREG(info.st_mode): raise ValueError("nonregular event log")
  os.lseek(fd,offset,os.SEEK_SET); data=os.read(fd,{MAX_POLL_BYTES})
  end=data.rfind(b"\\n")+1; complete=data[:end]
  if len(data)-end>{MAX_LINE_BYTES}: complete=data
  result={{"identity":[info.st_dev,info.st_ino],"size":info.st_size,"data":base64.b64encode(complete).decode("ascii")}}
 finally: os.close(fd)
except FileNotFoundError: pass
print(json.dumps(result))
"""
    response = ssh(
        config,
        shlex.join([config["remote_python"], "-c", script]),
        timeout=timeout,
    )
    if len(response) > 2 * MAX_POLL_BYTES:
        raise ValueError("Remote telemetry response exceeds bound")
    result = json.loads(response)
    if result.get("missing"):
        if offset:
            raise ValueError("Previously observed remote log disappeared")
        return None
    identity, size = result["identity"], result["size"]
    if (
        not isinstance(identity, list)
        or len(identity) != 2
        or any(type(value) is not int or value < 0 for value in identity)
        or type(size) is not int
        or size < 0
    ):
        raise ValueError("Invalid remote telemetry metadata")
    if size < offset:
        raise ValueError("Remote log truncated; retained prefix preserved")
    data = base64.b64decode(result["data"], validate=True)
    if len(data) > MAX_POLL_BYTES or offset + len(data) > size:
        raise ValueError("Invalid remote telemetry byte range")
    return identity, size, data


class RemoteRelay:
    """Copy an owned event log using its actual local byte length as cursor."""

    def __init__(self, study_root, stream_id, source, ssh, config, remote_path):
        self.root, self.stream_id, self.source = Path(study_root), stream_id, source
        self.ssh, self.config, self.remote_path = ssh, config, remote_path
        store.register(self.root, stream_id, source)
        self._halted = False
        self._abandoned = False
        self._publication_lock = threading.Lock()
        self._io_deadline = None

    def _status(self, **value):
        with self._publication_lock:
            if self._abandoned:
                return store.info(self.root, self.stream_id)
            return store.update_status(self.root, self.stream_id, **value)

    def _append(self, data, *, offset):
        with self._publication_lock:
            if self._abandoned:
                return
            store.append_chunk(
                self.root, self.stream_id, self.source, data, offset=offset
            )

    def abandon(self):
        # A timed-out SSH reader may still return, but cannot publish afterwards.
        with self._publication_lock:
            self._abandoned = True
            store.update_status(
                self.root,
                self.stream_id,
                status="sync_incomplete",
                error="Relay close timed out before source synchronization; recover retained cursor",
            )

    def _read_remote(self, offset):
        if self._abandoned:
            raise TimeoutError("Relay closed before source synchronization")
        remaining = (
            SSH_TIMEOUT
            if self._io_deadline is None
            else self._io_deadline - time.monotonic()
        )
        if remaining <= 0:
            raise TimeoutError("Final telemetry drain deadline reached")
        return _read_remote(
            self.ssh,
            self.config,
            self.remote_path,
            offset,
            timeout=min(SSH_TIMEOUT, remaining),
        )

    def cursor(self):
        return store.info(self.root, self.stream_id)["byte_end"]

    def finish(self, deadline):
        self._io_deadline = deadline
        try:
            self._finish(deadline)
        finally:
            self._io_deadline = None

    def _finish(self, deadline):
        if time.monotonic() >= deadline:
            self._status(
                status="sync_incomplete", error="Final telemetry drain deadline reached"
            )
            return
        self.poll()
        current = store.info(self.root, self.stream_id)
        target = current.get("remote_size", 0)
        if current["status"] == "integrity_error":
            return
        while self.cursor() < target and time.monotonic() < deadline:
            if not self.poll():
                break
        current = store.info(self.root, self.stream_id)
        if self.cursor() < max(target, current.get("remote_size", 0)) or current.get(
            "error"
        ):
            self._status(
                status="sync_incomplete",
                error=current.get("error")
                or "Final telemetry drain deadline reached; recover retained cursor",
            )
        else:
            self._status(status="synced")

    def poll(self):
        if self._halted:
            return False
        try:
            current = store.info(self.root, self.stream_id)
            if current["status"] == "integrity_error":
                return False
            if current["source"] != self.source:
                raise ValueError("Telemetry stream source identity mismatch")
            offset = current["byte_end"]
            result = self._read_remote(offset)
            if result is None:
                self._status(status="waiting", error=None)
                return False
            identity, size, data = result
            if current.get("remote_identity", identity) != identity:
                self._halted = True
                raise ValueError(
                    "Remote log identity changed; retained prefix preserved"
                )
            self._status(remote_identity=identity, remote_size=size)
            if data:
                self._append(data, offset=offset)
            self._status(status="live", error=None)
            return bool(data)
        except Exception as error:
            # An observation failure must not cancel, resubmit, or alter the job.
            try:
                self._status(
                    status=(
                        "integrity_error"
                        if isinstance(error, ValueError)
                        and any(
                            word in str(error)
                            for word in (
                                "truncated",
                                "identity changed",
                                "identity mismatch",
                            )
                        )
                        else "error"
                    ),
                    error=f"{type(error).__name__}: {error}"[:2000],
                )
            except Exception:
                pass
            return False


class BackgroundRelay:
    """One daemon poller; bounded closing never waits indefinitely on transport."""

    def __init__(self, poll, *, interval=POLL_SECONDS):
        self.poll, self.interval = poll, interval
        self._stop = threading.Event()
        self._closed = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _once(self):
        with self._lock:
            try:
                return self.poll()
            except Exception:
                return False

    def _run(self):
        while not self._stop.is_set():
            self._once()
            self._stop.wait(self.interval)

    def close(self):
        if self._closed:
            return
        self._closed = True
        deadline = time.monotonic() + 30
        self._stop.set()
        owner = getattr(self.poll, "__self__", None)
        if owner is not None and hasattr(owner, "stop_polling"):
            owner.stop_polling()
        self._thread.join(
            timeout=min(SSH_TIMEOUT + 1, max(0, deadline - time.monotonic()))
        )
        if self._thread.is_alive():
            if owner is not None and hasattr(owner, "abandon"):
                owner.abandon()
            return
        if owner is not None and hasattr(owner, "finish"):
            try:
                owner.finish(deadline)
            except Exception:
                if hasattr(owner, "abandon"):
                    owner.abandon()


def job_relay(study_root, stream_id, source, ssh, config, remote_path):
    """Telemetry setup failures are observations, never allocation failures."""
    try:
        relay = RemoteRelay(study_root, stream_id, source, ssh, config, remote_path)
    except Exception:
        return None
    return BackgroundRelay(relay.poll)


class NativeCSVRelay(RemoteRelay):
    """Adapt locked trainer CSV rows without changing or rereading the trainer."""

    def __init__(self, *args, scope, **kwargs):
        super().__init__(*args, **kwargs)
        self.scope = scope
        self._recover_cursor()

    def _recover_cursor(self):
        current = store.info(self.root, self.stream_id)
        self.offset, self.header = 0, None
        self._discard_csv_fragment = False
        # Recover the source cursor from the last retained converted event,
        # including the append-before-metadata crash window.
        path = Path(current["log_path"])
        with path.open("rb") as handle:
            handle.seek(max(0, current["byte_end"] - MAX_POLL_BYTES))
            tail = handle.read(MAX_POLL_BYTES)
        for line in reversed(tail.splitlines(keepends=True)):
            if not line.endswith(b"\n"):
                continue
            try:
                context = json.loads(line)["context"]
                self.offset = context["remote_end_offset"]
                self.header = context["csv_header"]
                self._discard_csv_fragment = context.get(
                    "csv_discard_until_newline", False
                )
                break
            except (ValueError, KeyError, TypeError):
                continue

    def _repair_tail(self):
        current = store.info(self.root, self.stream_id)
        if not current["byte_end"]:
            return
        with Path(current["log_path"]).open("rb") as handle:
            handle.seek(-1, 2)
            incomplete = handle.read(1) != b"\n"
        if incomplete:
            # Preserve the fragment, delimit it, then confirm only complete JSON
            # records. A malformed fragment remains visible as a reader error;
            # its uncommitted native row is fetched again from the last cursor.
            self._append(b"\n", offset=current["byte_end"])
            self._recover_cursor()

    def cursor(self):
        return self.offset

    def poll(self):
        try:
            self._repair_tail()
            current = store.info(self.root, self.stream_id)
            if current["status"] == "integrity_error":
                return False
            if current["source"] != self.source:
                raise ValueError("Telemetry stream source identity mismatch")
            result = self._read_remote(self.offset)
            if result is None:
                return False
            identity, size, data = result
            if current.get("remote_identity", identity) != identity:
                raise ValueError(
                    "Remote CSV identity changed; retained observations preserved"
                )
            self._status(remote_identity=identity, remote_size=size)
            events, cursor = [], self.offset
            for line in data.splitlines(keepends=True):
                start = cursor
                cursor += len(line)
                complete = line.endswith(b"\n")
                discard = self._discard_csv_fragment
                self._discard_csv_fragment = not complete
                event = {
                    "version": 1,
                    "id": str(
                        uuid.uuid5(uuid.NAMESPACE_URL, f"{self.stream_id}:{cursor}")
                    ),
                    "time": time.time(),
                    "name": "native/" + self.scope,
                    "value": None,
                    "tags": ["dashboard", "unreviewed", "native"],
                    "step": None,
                    "unit": None,
                    "context": {
                        "source": self.remote_path,
                        "scope": self.scope,
                        "remote_start_offset": start,
                        "remote_end_offset": cursor,
                        "csv_header": self.header,
                        "csv_discard_until_newline": self._discard_csv_fragment,
                    },
                }
                try:
                    if discard or not complete or len(line) > MAX_LINE_BYTES:
                        raise ValueError("Oversized native CSV row fragment discarded")
                    values = next(csv.reader(io.StringIO(line.decode()), strict=True))
                    if self.header is None:
                        if not any(
                            key in values
                            for key in ("optimizer_step", "step", "global_step")
                        ):
                            raise ValueError(
                                "Native CSV header requires an explicit step column"
                            )
                        # Keep the recovery context and any error event bounded.
                        if len(json.dumps(values).encode()) > MAX_LINE_BYTES // 2:
                            raise ValueError("Native CSV header exceeds context bound")
                        self.header = values
                        continue
                    if len(values) != len(self.header):
                        raise ValueError("Native CSV row differs from header")
                    row = dict(zip(self.header, values, strict=True))
                    step = next(
                        row[key]
                        for key in ("optimizer_step", "step", "global_step")
                        if key in row
                    )
                    event["step"] = int(step)
                    metrics = {}
                    for key, value in row.items():
                        if key not in {
                            "optimizer_step",
                            "step",
                            "global_step",
                            "scope",
                        }:
                            try:
                                metrics[key] = store._observed(float(value))
                            except ValueError:
                                metrics[key] = value
                    event["value"] = metrics
                    event["context"]["scope"] = row.get("scope", self.scope)
                    encoded = (
                        json.dumps(
                            event, allow_nan=False, separators=(",", ":")
                        ).encode()
                        + b"\n"
                    )
                    if len(encoded) > MAX_LINE_BYTES:
                        raise ValueError("Converted native row exceeds event bound")
                except (ValueError, UnicodeError, csv.Error, StopIteration) as error:
                    # Nonmetric evidence commits the source cursor too, so a bad
                    # row cannot hide later observations or replay after restart.
                    event.update(
                        name="native/csv_error",
                        value=f"{type(error).__name__}: {error}"[:500],
                        tags=["internal", "unreviewed", "native", "error"],
                        step=None,
                    )
                    event["context"]["scope"] = self.scope
                    encoded = (
                        json.dumps(
                            event, allow_nan=False, separators=(",", ":")
                        ).encode()
                        + b"\n"
                    )
                events.append(encoded)
            if events:
                self._append(b"".join(events), offset=current["byte_end"])
            self.offset = cursor
            self._status(status="live", error=None, remote_offset=self.offset)
            return bool(data)
        except Exception as error:
            try:
                self._recover_cursor()
                self._status(
                    status=(
                        "integrity_error"
                        if isinstance(error, ValueError)
                        and any(
                            word in str(error)
                            for word in (
                                "truncated",
                                "identity changed",
                                "identity mismatch",
                            )
                        )
                        else "error"
                    ),
                    error=f"{type(error).__name__}: {error}"[:2000],
                )
            except Exception:
                pass
            return False


class NativeEvaluationRelay(NativeCSVRelay):
    """Use the existing complete scoped evaluation JSON marker in run.log."""

    def poll(self):
        try:
            self._repair_tail()
            current = store.info(self.root, self.stream_id)
            if current["status"] == "integrity_error":
                return False
            result = self._read_remote(self.offset)
            if result is None:
                return False
            identity, size, data = result
            if current.get("remote_identity", identity) != identity:
                raise ValueError(
                    "Remote JSON log identity changed; retained observations preserved"
                )
            self._status(remote_identity=identity, remote_size=size)
            cursor, events = self.offset, []
            for line in data.splitlines(keepends=True):
                cursor += len(line)
                row = None
                try:
                    row = json.loads(line).get("evaluation")
                except (ValueError, AttributeError):
                    pass
                is_evaluation = (
                    isinstance(row, dict)
                    and row.get("scope") in {"subset", "full", "smoke"}
                    and type(row.get("optimizer_step")) is int
                )
                event = {
                    "version": 1,
                    "id": str(
                        uuid.uuid5(uuid.NAMESPACE_URL, f"{self.stream_id}:{cursor}")
                    ),
                    "time": time.time(),
                    "name": "native/evaluation" if is_evaluation else "native/log",
                    "value": row
                    if is_evaluation
                    else line.decode(errors="replace").rstrip()[:1000],
                    "tags": ["dashboard", "unreviewed", "native"]
                    if is_evaluation
                    else ["internal", "unreviewed", "native"],
                    "step": row["optimizer_step"] if is_evaluation else None,
                    "unit": None,
                    "context": {
                        "source": self.remote_path,
                        "scope": row["scope"] if is_evaluation else "log",
                        "remote_end_offset": cursor,
                        "csv_header": None,
                        "raw_fragment_bytes": len(line),
                        "raw_fragment_complete": line.endswith(b"\n"),
                    },
                }
                encoded = (
                    json.dumps(event, allow_nan=False, separators=(",", ":")).encode()
                    + b"\n"
                )
                if len(encoded) > MAX_LINE_BYTES:
                    raise ValueError("Converted native log record exceeds event bound")
                events.append(encoded)
            if events:
                self._append(b"".join(events), offset=current["byte_end"])
            self.offset = cursor
            self._status(status="live", error=None, remote_offset=self.offset)
            return bool(data)
        except Exception as error:
            try:
                self._recover_cursor()
                self._status(
                    status=(
                        "integrity_error"
                        if isinstance(error, ValueError)
                        and any(
                            word in str(error)
                            for word in (
                                "truncated",
                                "identity changed",
                                "identity mismatch",
                            )
                        )
                        else "error"
                    ),
                    error=f"{type(error).__name__}: {error}"[:2000],
                )
            except Exception:
                pass
            return False


def _ssh(config, command, *, timeout):
    return subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ClearAllForwardings=yes",
            config["ssh_host"],
            command,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    ).stdout


class FixedMonitor:
    """Discover only receipted attempts in the exact protected allocation record."""

    def __init__(
        self, study_root, experiment_id, record, fingerprint, config, ssh=_ssh
    ):
        self.root, self.experiment_id = Path(study_root), experiment_id
        self.record, self.fingerprint = Path(record), fingerprint
        self.config, self.ssh, self.relays = config, ssh, {}
        self._stop_polling = threading.Event()

    def _discover(self):
        path = self.record / "state.json"
        if not path.exists() or path.stat().st_size > 65536:
            return tuple(self.relays.values())
        state = json.loads(path.read_text())
        if state.get("fingerprint") != self.fingerprint:
            raise ValueError("Fixed telemetry allocation fingerprint mismatch")
        for attempt in state.get("attempts", []):
            owned = str(attempt.get("owned_job_id", ""))
            remote = attempt.get("remote")
            if not re.fullmatch(r"[1-9][0-9]*", owned) or not isinstance(remote, str):
                continue
            if not Path(remote).is_relative_to(Path(self.config["remote_root"])):
                raise ValueError("Fixed telemetry path outside owned experiment root")
            source = {
                "kind": "fixed-short",
                "experiment_id": self.experiment_id,
                "source_identity": self.fingerprint,
                "execution_identity": hashlib.sha256(
                    json.dumps(
                        {
                            "record": str(self.record),
                            "job": owned,
                            "remote": remote,
                            "seed": attempt.get("seed"),
                        },
                        sort_keys=True,
                    ).encode()
                ).hexdigest(),
                "owned_job_id": owned,
                "seed": attempt.get("seed"),
            }
            for suffix, relative, cls, scope in (
                ("training", "train/train.csv", NativeCSVRelay, "training"),
                ("evaluation", "run.log", NativeEvaluationRelay, "evaluation"),
            ):
                key = owned + "-" + suffix
                if key not in self.relays:
                    source_for_log = {**source, "native_log": relative}
                    self.relays[key] = cls(
                        self.root,
                        "fixed-" + self.experiment_id + "-" + key,
                        source_for_log,
                        self.ssh,
                        self.config,
                        remote + "/" + relative,
                        scope=scope,
                    )
        return tuple(self.relays.values())

    def poll(self):
        progress = False
        # Discover every owned source before network I/O so a pending reader's
        # bounded close can mark all unresolved streams, including unread ones.
        for relay in self._discover():
            if self._stop_polling.is_set():
                break
            progress = relay.poll() or progress
        return progress

    def stop_polling(self):
        self._stop_polling.set()

    def abandon(self):
        self.stop_polling()
        for relay in tuple(self.relays.values()):
            relay.abandon()

    def finish(self, deadline):
        for relay in self._discover():
            if time.monotonic() >= deadline:
                relay._status(
                    status="sync_incomplete",
                    error="Final telemetry drain deadline reached",
                )
            else:
                relay.finish(deadline)


def fixed_relay(control, workspace, record_name, fingerprint, config):
    # Bind only the two authoritative frozen source layouts owned by control.
    control, workspace = Path(control).resolve(), Path(workspace).resolve()
    if not workspace.is_relative_to(control):
        return None
    parts = workspace.relative_to(control).parts
    if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "source":
        experiment_id = parts[1]
    elif (
        len(parts) == 4
        and parts[0] == "debug"
        and parts[3] == "source"
        and re.fullmatch(r"formal-[0-9]{3,}", parts[2])
    ):
        experiment_id = parts[1]
    else:
        return None
    if not re.fullmatch(r"[0-9]{4,}", experiment_id):
        return None
    monitor = FixedMonitor(
        Path(control).parent / "study",
        experiment_id,
        Path(config["records_root"]) / record_name,
        fingerprint,
        config,
    )
    try:
        return BackgroundRelay(monitor.poll)
    except Exception:
        return None


def drain_existing(study_root, stream_id, source, config, remote_path, ssh=_ssh):
    """Receipt-only recovery can finish a previously observed stream prefix."""
    try:
        store.info(study_root, stream_id)
        relay = RemoteRelay(study_root, stream_id, source, ssh, config, remote_path)
        relay.finish(time.monotonic() + 30)
    except Exception:
        pass
