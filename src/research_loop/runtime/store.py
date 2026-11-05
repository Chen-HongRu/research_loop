"""SQLite control records owned by one process-bound, fenced coordinator.

Transactions contain only control records and file metadata, never worker calls.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1
DATABASE = "runtime.sqlite3"
ROLES = {"scout", "advocate", "judge", "plan", "execute", "review"}
PHASES = {"plan": "execute", "execute": "review", "review": "publication"}


class StateStoreError(RuntimeError):
    """Invalid or unavailable authoritative control state."""


class StoreExistsError(StateStoreError):
    pass


class OwnershipError(StateStoreError):
    pass


class ConflictError(StateStoreError):
    pass


class BudgetExceeded(StateStoreError):
    pass


class InvalidTransition(StateStoreError):
    pass


_SCHEMA = """
CREATE TABLE control (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), study_id TEXT NOT NULL,
 config_json TEXT NOT NULL, max_iterations INTEGER, max_calls INTEGER,
 max_seconds REAL NOT NULL, admission_deadline REAL,
 calls INTEGER NOT NULL DEFAULT 0, iterations INTEGER NOT NULL DEFAULT 0,
 elapsed_seconds REAL NOT NULL DEFAULT 0, stop_reason TEXT,
 owner_epoch INTEGER NOT NULL DEFAULT 0, owner_open INTEGER NOT NULL DEFAULT 0,
 anchor REAL NOT NULL
);
CREATE TABLE tasks (
 id TEXT PRIMARY KEY, revision INTEGER NOT NULL, specification_json TEXT NOT NULL,
 source TEXT NOT NULL, priority INTEGER NOT NULL, dependencies_json TEXT NOT NULL
);
CREATE TABLE iterations (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
 task_revision INTEGER NOT NULL, revision INTEGER NOT NULL,
 phase TEXT NOT NULL, status TEXT NOT NULL
);
CREATE TABLE attempts (
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, role TEXT NOT NULL,
 iteration_id TEXT REFERENCES iterations(id), expected_revision INTEGER,
 owner_epoch INTEGER NOT NULL, worker_token TEXT NOT NULL, status TEXT NOT NULL,
 output_ref TEXT, output_identity_json TEXT, receipt_json TEXT, failure_reason TEXT
);
CREATE UNIQUE INDEX active_iteration_phase ON attempts(iteration_id, role)
 WHERE status='active' AND iteration_id IS NOT NULL;
"""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _number(value, name, *, nullable=False, integer=False):
    if value is None and nullable:
        return
    if (
        type(value) not in ((int,) if integer else (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be a finite nonnegative number")


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def _timestamp(clock):
    value = clock()
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("clock must return a finite timestamp")
    return value


def _revision(value):
    if type(value) is not int or value < 1:
        raise ConflictError("Expected revision must be a positive integer")


def _decode(row):
    if row is None:
        return None
    result = dict(row)
    for name in list(result):
        if name.endswith("_json"):
            value = result.pop(name)
            result[name[:-5]] = json.loads(value) if value else None
    return result


class StateStore:
    """Open an existing store; creation is always explicit."""

    def __init__(self, root, *, clock=time.time):
        self.root = Path(root).resolve()
        self.path = self.root / DATABASE
        self._clock = clock
        try:
            with self._connection(readonly=True) as conn:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version != SCHEMA_VERSION:
                    raise StateStoreError(
                        f"Unsupported runtime schema version: {version}"
                    )
                if conn.execute("SELECT count(*) FROM control").fetchone()[0] != 1:
                    raise StateStoreError("Invalid runtime control record")
        except sqlite3.Error as exc:
            raise StateStoreError(f"Cannot open runtime store: {exc}") from exc

    @classmethod
    def create(
        cls,
        root,
        *,
        config,
        max_iterations=10,
        max_calls=100,
        max_seconds=3600,
        admission_deadline=None,
        clock=time.time,
    ):
        if not isinstance(config, dict):
            raise ValueError("config must be a dictionary")
        config_json = _json(config)
        _number(max_iterations, "max_iterations", nullable=True, integer=True)
        _number(max_calls, "max_calls", nullable=True, integer=True)
        _number(max_seconds, "max_seconds")
        if max_seconds == 0:
            raise ValueError("max_seconds must be positive")
        if admission_deadline is not None and (
            type(admission_deadline) not in (int, float)
            or not math.isfinite(admission_deadline)
        ):
            raise ValueError("admission_deadline must be a finite timestamp")
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        if any(os.path.lexists(root / name) for name in ("study.json", "state.json")):
            raise StoreExistsError("Legacy Study requires explicit migration")
        path = root / DATABASE
        if any(
            os.path.lexists(str(path) + suffix)
            for suffix in ("-journal", "-wal", "-shm")
        ):
            raise StoreExistsError("Existing SQLite sidecars require explicit recovery")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise StoreExistsError("Runtime store already exists") from exc
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.executescript(_SCHEMA)
                conn.execute(
                    "INSERT INTO control(singleton,study_id,config_json,max_iterations,"
                    "max_calls,max_seconds,admission_deadline,anchor) VALUES(1,?,?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex,
                        config_json,
                        max_iterations,
                        max_calls,
                        max_seconds,
                        admission_deadline,
                        _timestamp(clock),
                    ),
                )
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                conn.commit()
            finally:
                conn.close()
            directory = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return cls(root, clock=clock)
        except BaseException:
            path.unlink(missing_ok=True)
            for suffix in ("-journal", "-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)
            raise

    @contextmanager
    def _connection(self, *, readonly=False):
        mode = "ro" if readonly else "rw"
        conn = sqlite3.connect(
            self.path.as_uri() + f"?mode={mode}",
            uri=True,
            timeout=10,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            if not readonly:
                conn.execute("PRAGMA synchronous=FULL")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def coordinator(self):
        """Acquire the nonblocking OS lock, then advance the durable owner epoch."""
        fd = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        acquired = False
        owner = None
        pid = os.getpid()
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError as exc:
                raise OwnershipError("Another coordinator owns this store") from exc
            with self._connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = dict(conn.execute("SELECT * FROM control").fetchone())
                now = max(_timestamp(self._clock), row["anchor"])
                elapsed = row["elapsed_seconds"]
                if row["owner_open"]:
                    elapsed += now - row["anchor"]
                epoch = row["owner_epoch"] + 1
                conn.execute(
                    "UPDATE control SET elapsed_seconds=?,anchor=?,owner_epoch=?,owner_open=1",
                    (elapsed, now, epoch),
                )
                conn.commit()
            owner = Coordinator(self, epoch, pid)
            yield owner
        finally:
            try:
                if owner is not None and os.getpid() == pid:
                    owner.close()
            finally:
                # A forked child inherits the same flock open-file description.
                # LOCK_UN there would silently unlock the living parent's lock.
                if acquired and os.getpid() == pid:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def snapshot(self):
        """Read one consistent snapshot; never admit work or persist time here."""
        with self._connection(readonly=True) as conn:
            conn.execute("BEGIN")
            row = _decode(conn.execute("SELECT * FROM control").fetchone())
            result = {
                "study_id": row["study_id"],
                "schema_version": SCHEMA_VERSION,
                "config": row["config"],
                "limits": {
                    key: row[key]
                    for key in (
                        "max_iterations",
                        "max_calls",
                        "max_seconds",
                        "admission_deadline",
                    )
                },
                "counters": {
                    key: row[key] for key in ("calls", "iterations", "elapsed_seconds")
                },
                "stop": {
                    "requested": row["stop_reason"] is not None,
                    "reason": row["stop_reason"],
                },
                "owner_epoch": row["owner_epoch"],
                "owner_open": bool(row["owner_open"]),
            }
            for table in ("tasks", "iterations", "attempts"):
                result[table] = [
                    _decode(record)
                    for record in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
                ]
            return result


class Coordinator:
    """A process-bound owner capability; workers receive only attempt tokens."""

    def __init__(self, store, epoch, pid):
        self.store = store
        self.owner_epoch = epoch
        self._pid = pid
        self._closed = False
        self._mutex = threading.RLock()

    def _live(self):
        if self._closed or os.getpid() != self._pid:
            raise OwnershipError("Coordinator is closed or belongs to another process")

    @contextmanager
    def _transaction(self):
        with self._mutex:
            self._live()
            with self.store._connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = dict(conn.execute("SELECT * FROM control").fetchone())
                    if row["owner_epoch"] != self.owner_epoch or not row["owner_open"]:
                        raise OwnershipError("Coordinator epoch is stale")
                    now = max(_timestamp(self.store._clock), row["anchor"])
                    row["elapsed_seconds"] += now - row["anchor"]
                    row["anchor"] = now
                    conn.execute(
                        "UPDATE control SET elapsed_seconds=?,anchor=?",
                        (row["elapsed_seconds"], now),
                    )
                    yield conn, row
                    conn.commit()
                except BaseException:
                    # Anchor rolls back too, so rejected operations cannot erase
                    # time that the next mutation/close/recovery must charge.
                    conn.rollback()
                    raise

    def close(self):
        with self._mutex:
            if self._closed:
                return
            try:
                with self._transaction() as (conn, _):
                    conn.execute("UPDATE control SET owner_open=0")
            finally:
                # Even failed close bookkeeping must revoke this capability
                # before its enclosing context releases the OS lock.
                self._closed = True

    def snapshot(self):
        self._live()
        return self.store.snapshot()

    @staticmethod
    def _admit(control, *, deadline=False):
        if control["stop_reason"] is not None:
            raise BudgetExceeded("STOP is latched")
        if control["elapsed_seconds"] >= control["max_seconds"]:
            raise BudgetExceeded("Wall-time budget exhausted")
        if (
            deadline
            and control["admission_deadline"] is not None
            and control["anchor"] >= control["admission_deadline"]
        ):
            raise BudgetExceeded("Task admission deadline reached")

    def create_task(
        self, task_id, specification, *, source="human", priority=100, dependencies=()
    ):
        if not isinstance(task_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", task_id
        ):
            raise ValueError("Invalid task id")
        if not isinstance(specification, dict):
            raise ValueError("specification must be a dictionary")
        if source not in {"human", "autonomous"} or type(priority) is not int:
            raise ValueError("Invalid task source or priority")
        if (
            not isinstance(dependencies, (list, tuple))
            or any(
                not isinstance(item, str) or item == task_id for item in dependencies
            )
            or len(set(dependencies)) != len(dependencies)
        ):
            raise ValueError("Invalid task dependencies")
        values = (
            task_id,
            1,
            _json(specification),
            source,
            priority,
            _json(list(dependencies)),
        )
        with self._transaction() as (conn, _):
            if conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                raise ConflictError("Task revisions are immutable; task already exists")
            for dependency in dependencies:
                if not conn.execute(
                    "SELECT 1 FROM tasks WHERE id=?", (dependency,)
                ).fetchone():
                    raise ConflictError(f"Unknown dependency: {dependency}")
            conn.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)", values)
            return _decode(
                conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            )

    def claim_task(self, task_id, expected_revision=1):
        _revision(expected_revision)
        with self._transaction() as (conn, control):
            task = _decode(
                conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            )
            if task is None or task["revision"] != expected_revision:
                raise ConflictError("Unknown task or task revision mismatch")
            existing = conn.execute(
                "SELECT * FROM iterations WHERE task_id=?", (task_id,)
            ).fetchone()
            if existing:
                return _decode(existing)
            self._admit(control, deadline=True)
            limit = control["max_iterations"]
            if limit is not None and control["iterations"] >= limit:
                raise BudgetExceeded("Iteration budget exhausted")
            for dependency in task["dependencies"]:
                record = conn.execute(
                    "SELECT status FROM iterations WHERE task_id=?", (dependency,)
                ).fetchone()
                if record is None or record["status"] != "published":
                    raise InvalidTransition(
                        f"Dependency is not published: {dependency}"
                    )
            iteration_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO iterations VALUES(?,?,?,1,'plan','active')",
                (iteration_id, task_id, expected_revision),
            )
            conn.execute("UPDATE control SET iterations=iterations+1")
            return _decode(
                conn.execute(
                    "SELECT * FROM iterations WHERE id=?", (iteration_id,)
                ).fetchone()
            )

    def begin_attempt(
        self, role, *, request_id, iteration_id=None, expected_revision=None
    ):
        if role not in ROLES:
            raise ValueError("Unknown worker role")
        _text(request_id, "request_id")
        if (role in PHASES) != (iteration_id is not None):
            raise InvalidTransition("Worker role requires a different iteration scope")
        if iteration_id is None and expected_revision is not None:
            raise InvalidTransition("Study-level attempts have no iteration revision")
        if iteration_id is not None:
            _revision(expected_revision)
        with self._transaction() as (conn, control):
            existing = _decode(
                conn.execute(
                    "SELECT * FROM attempts WHERE request_id=?", (request_id,)
                ).fetchone()
            )
            if existing:
                if (
                    existing["role"],
                    existing["iteration_id"],
                    existing["expected_revision"],
                ) != (role, iteration_id, expected_revision):
                    raise ConflictError("Request id reused with different intent")
                return existing
            self._admit(control)
            if (
                control["max_calls"] is not None
                and control["calls"] >= control["max_calls"]
            ):
                raise BudgetExceeded("Call budget exhausted")
            if iteration_id is not None:
                iteration = conn.execute(
                    "SELECT * FROM iterations WHERE id=?", (iteration_id,)
                ).fetchone()
                if iteration is None or iteration["revision"] != expected_revision:
                    raise ConflictError("Unknown iteration or revision mismatch")
                if iteration["status"] != "active" or iteration["phase"] != role:
                    raise InvalidTransition(
                        "Attempt role does not match active iteration phase"
                    )
                active = conn.execute(
                    "SELECT 1 FROM attempts WHERE role=? AND iteration_id=? AND status='active'",
                    (role, iteration_id),
                ).fetchone()
                if active:
                    raise InvalidTransition(
                        "This phase already has an active or unresolved attempt"
                    )
            attempt_id, token = uuid.uuid4().hex, uuid.uuid4().hex
            conn.execute(
                "INSERT INTO attempts(id,request_id,role,iteration_id,expected_revision,owner_epoch,worker_token,status) VALUES(?,?,?,?,?,?,?,'active')",
                (
                    attempt_id,
                    request_id,
                    role,
                    iteration_id,
                    expected_revision,
                    self.owner_epoch,
                    token,
                ),
            )
            conn.execute("UPDATE control SET calls=calls+1")
            return _decode(
                conn.execute(
                    "SELECT * FROM attempts WHERE id=?", (attempt_id,)
                ).fetchone()
            )

    def _attempt(
        self,
        conn,
        attempt_id,
        worker_token,
        expected_revision,
        *,
        allow_accepted_replay=False,
    ):
        if expected_revision is not None:
            _revision(expected_revision)
        attempt = _decode(
            conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        )
        if attempt is None:
            raise ConflictError("Unknown attempt")
        if attempt["worker_token"] != worker_token:
            raise OwnershipError("Worker token mismatch")
        if attempt["owner_epoch"] != self.owner_epoch and not (
            allow_accepted_replay and attempt["status"] == "accepted"
        ):
            raise OwnershipError("Worker owner epoch mismatch")
        if attempt["expected_revision"] != expected_revision:
            raise ConflictError("Attempt revision mismatch")
        return attempt

    @staticmethod
    def _iteration(conn, attempt):
        if attempt["iteration_id"] is None:
            return None
        iteration = conn.execute(
            "SELECT * FROM iterations WHERE id=?", (attempt["iteration_id"],)
        ).fetchone()
        if iteration is None or iteration["revision"] != attempt["expected_revision"]:
            raise ConflictError("Iteration revision mismatch")
        if iteration["status"] != "active" or iteration["phase"] != attempt["role"]:
            raise InvalidTransition("Iteration no longer accepts this phase")
        return iteration

    def accept_attempt(
        self, attempt_id, worker_token, output_ref, *, expected_revision
    ):
        from research_loop.runtime.files import validate_artifact_ref

        path = validate_artifact_ref(self.store.root, output_ref)
        stat = path.stat()
        identity = {
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
        with self._transaction() as (conn, _):
            attempt = self._attempt(
                conn,
                attempt_id,
                worker_token,
                expected_revision,
                allow_accepted_replay=True,
            )
            if attempt["status"] == "accepted":
                if (
                    attempt["output_ref"] != output_ref
                    or attempt["output_identity"] != identity
                ):
                    raise ConflictError("Accepted output differs from original receipt")
                return attempt["receipt"]
            if attempt["status"] != "active":
                raise InvalidTransition("Attempt is no longer active")
            iteration = self._iteration(conn, attempt)
            receipt = {
                "attempt_id": attempt_id,
                "output_ref": output_ref,
                "owner_epoch": self.owner_epoch,
                "iteration_id": attempt["iteration_id"],
                "revision": expected_revision,
            }
            if iteration is not None:
                receipt["revision"] = iteration["revision"] + 1
                conn.execute(
                    "UPDATE iterations SET phase=?,revision=revision+1 WHERE id=?",
                    (PHASES[attempt["role"]], iteration["id"]),
                )
            conn.execute(
                "UPDATE attempts SET status='accepted',output_ref=?,output_identity_json=?,receipt_json=? WHERE id=?",
                (output_ref, _json(identity), _json(receipt), attempt_id),
            )
            return receipt

    def fail_attempt(self, attempt_id, worker_token, reason, *, expected_revision):
        _text(reason, "reason")
        with self._transaction() as (conn, _):
            attempt = self._attempt(conn, attempt_id, worker_token, expected_revision)
            if attempt["status"] == "failed" and attempt["failure_reason"] == reason:
                return attempt
            if attempt["status"] != "active":
                raise InvalidTransition("Attempt is no longer active")
            iteration = self._iteration(conn, attempt)
            if iteration is not None:
                conn.execute(
                    "UPDATE iterations SET status='failed',revision=revision+1 WHERE id=?",
                    (iteration["id"],),
                )
            conn.execute(
                "UPDATE attempts SET status='failed',failure_reason=? WHERE id=?",
                (reason, attempt_id),
            )
            return _decode(
                conn.execute(
                    "SELECT * FROM attempts WHERE id=?", (attempt_id,)
                ).fetchone()
            )

    def set_stop(self, reason):
        _text(reason, "reason")
        with self._transaction() as (conn, _):
            conn.execute(
                "UPDATE control SET stop_reason=COALESCE(stop_reason,?)", (reason,)
            )
