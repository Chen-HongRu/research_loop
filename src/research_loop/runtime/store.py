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

from .operation_contract import OPERATION_SCHEMA

SCHEMA_VERSION = 5
DATABASE = "runtime.sqlite3"
ROLES = {"scout", "advocate", "judge", "plan", "execute", "review"}
PHASES = {"plan": "execute", "execute": "review", "review": "publication"}
_CONTROL_METADATA = (
    "study_id,max_iterations,max_calls,max_seconds,admission_deadline,calls,iterations,"
    "elapsed_seconds,stop_reason,owner_epoch,owner_open,anchor,pool_version"
)
_TASK_METADATA = "id,revision,source,priority,dependencies_json,cancelled"


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


_SCHEMA = (
    """
CREATE TABLE control (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), study_id TEXT NOT NULL,
 config_json TEXT NOT NULL, max_iterations INTEGER, max_calls INTEGER,
 max_seconds REAL NOT NULL, admission_deadline REAL,
 calls INTEGER NOT NULL DEFAULT 0, iterations INTEGER NOT NULL DEFAULT 0,
 elapsed_seconds REAL NOT NULL DEFAULT 0, stop_reason TEXT,
 owner_epoch INTEGER NOT NULL DEFAULT 0, owner_open INTEGER NOT NULL DEFAULT 0,
 anchor REAL NOT NULL, pool_version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE tasks (
 id TEXT PRIMARY KEY, revision INTEGER NOT NULL, specification_json TEXT NOT NULL,
 source TEXT NOT NULL, priority INTEGER NOT NULL, dependencies_json TEXT NOT NULL,
 cancelled INTEGER NOT NULL DEFAULT 0 CHECK(cancelled IN (0,1))
);
CREATE TABLE task_revisions (
 id TEXT NOT NULL REFERENCES tasks(id), revision INTEGER NOT NULL,
 specification_json TEXT NOT NULL, source TEXT NOT NULL, priority INTEGER NOT NULL,
 dependencies_json TEXT NOT NULL, cancelled INTEGER NOT NULL CHECK(cancelled IN (0,1)),
 pool_version INTEGER NOT NULL, origin_kind TEXT NOT NULL, origin_id TEXT NOT NULL,
 PRIMARY KEY(id,revision)
);
CREATE TABLE task_requests (
 request_id TEXT PRIMARY KEY, intent_json TEXT NOT NULL, receipt_json TEXT NOT NULL
);
CREATE TABLE task_batches (
 attempt_id TEXT PRIMARY KEY REFERENCES attempts(id), payload_ref TEXT NOT NULL,
 identity_json TEXT NOT NULL, receipt_json TEXT NOT NULL
);
CREATE TABLE planning_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, producer_key TEXT NOT NULL UNIQUE,
 kind TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE planning_rounds (
 id TEXT PRIMARY KEY, owner_epoch INTEGER NOT NULL, pool_version INTEGER NOT NULL,
 event_from INTEGER NOT NULL, event_through INTEGER NOT NULL,
 snapshot_ref TEXT NOT NULL, identity_json TEXT NOT NULL,
 knowledge_ref TEXT NOT NULL, knowledge_identity_json TEXT NOT NULL,
 status TEXT NOT NULL, error TEXT, receipt_json TEXT
);
CREATE UNIQUE INDEX active_planning_round ON planning_rounds(status) WHERE status='active';
CREATE TABLE planning_calls (
 round_id TEXT NOT NULL REFERENCES planning_rounds(id), slot TEXT NOT NULL,
 attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id), PRIMARY KEY(round_id,slot)
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
CREATE TABLE contexts (
 attempt_id TEXT PRIMARY KEY REFERENCES attempts(id),
 context_ref TEXT NOT NULL, identity_json TEXT NOT NULL, revisions_json TEXT NOT NULL,
 evidence_identities_json TEXT NOT NULL
);
CREATE TABLE measurements (
 id TEXT PRIMARY KEY, iteration_id TEXT NOT NULL REFERENCES iterations(id),
 candidate_id TEXT NOT NULL, route_id TEXT,
 level TEXT NOT NULL CHECK(level IN ('global','local')), evaluation_id TEXT NOT NULL,
 definition_json TEXT NOT NULL, artifact_json TEXT NOT NULL,
 evaluator_json TEXT NOT NULL, metrics_json TEXT NOT NULL,
 receipt_ref TEXT NOT NULL UNIQUE, identity_json TEXT NOT NULL,
 plan_attempt_id TEXT NOT NULL REFERENCES attempts(id),
 execute_attempt_id TEXT NOT NULL REFERENCES attempts(id),
 UNIQUE(iteration_id,candidate_id,evaluation_id)
);
CREATE TABLE publications (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id),
 payload_ref TEXT NOT NULL, identity_json TEXT NOT NULL, receipt_json TEXT NOT NULL
);
CREATE TABLE knowledge (
 id TEXT NOT NULL, revision INTEGER NOT NULL, kind TEXT NOT NULL,
 source_role TEXT NOT NULL, publication_id INTEGER NOT NULL REFERENCES publications(id),
 entry_index INTEGER NOT NULL, PRIMARY KEY(id,revision)
);
CREATE TABLE eligibility (
 measurement_id TEXT NOT NULL REFERENCES measurements(id), revision INTEGER NOT NULL,
 valid INTEGER NOT NULL CHECK(valid IN (0,1)),
 verified INTEGER NOT NULL CHECK(verified IN (0,1)),
 publication_id INTEGER NOT NULL REFERENCES publications(id),
 decision_kind TEXT NOT NULL, decision_index INTEGER NOT NULL,
 PRIMARY KEY(measurement_id,revision)
);
CREATE TABLE publication_conflicts (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 publication_id INTEGER NOT NULL REFERENCES publications(id),
 target_kind TEXT NOT NULL, target_id TEXT NOT NULL,
 expected_revision INTEGER NOT NULL, actual_revision INTEGER NOT NULL,
 reason TEXT NOT NULL, entry_index INTEGER NOT NULL
);
"""
    + OPERATION_SCHEMA
)


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
    if "cancelled" in result:
        result["cancelled"] = bool(result["cancelled"])
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
                row = dict(
                    conn.execute(f"SELECT {_CONTROL_METADATA} FROM control").fetchone()
                )
                now = max(_timestamp(self._clock), row["anchor"])
                elapsed = row["elapsed_seconds"]
                external_open = conn.execute(
                    "SELECT 1 FROM operations WHERE external_open=1 LIMIT 1"
                ).fetchone()
                if row["owner_open"] or external_open:
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
                "pool_version": row["pool_version"],
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
                "knowledge_version": conn.execute(
                    "SELECT COALESCE(MAX(id),0) FROM publications"
                ).fetchone()[0],
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
                    row = dict(
                        conn.execute(
                            f"SELECT {_CONTROL_METADATA} FROM control"
                        ).fetchone()
                    )
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
        task = dict(
            id=task_id,
            revision=1,
            specification=json.loads(_json(specification)),
            source=source,
            priority=priority,
            dependencies=list(dependencies),
            cancelled=False,
        )
        with self._transaction() as (conn, _):
            if conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                raise ConflictError("Task revisions are immutable; task already exists")
            for dependency in dependencies:
                if not conn.execute(
                    "SELECT 1 FROM tasks WHERE id=?", (dependency,)
                ).fetchone():
                    raise ConflictError(f"Unknown dependency: {dependency}")
            result = self._save_task(conn, task, "direct", task_id)
            if source == "human":
                self._planning_event(
                    conn,
                    f"direct-human:{task_id}",
                    "human_task",
                    {"id": task_id, "revision": 1},
                )
            return result

    @staticmethod
    def _planning_event(conn, producer_key, kind, payload):
        _text(producer_key, "producer_key")
        _text(kind, "event kind")
        payload_json = _json(payload)
        previous = conn.execute(
            "SELECT * FROM planning_events WHERE producer_key=?", (producer_key,)
        ).fetchone()
        if previous:
            if previous["kind"] != kind or previous["payload_json"] != payload_json:
                raise ConflictError("Planning event key reused with different intent")
            return _decode(previous)
        identity = conn.execute(
            "INSERT INTO planning_events(producer_key,kind,payload_json) VALUES(?,?,?)",
            (producer_key, kind, payload_json),
        ).lastrowid
        return _decode(
            conn.execute(
                "SELECT * FROM planning_events WHERE id=?", (identity,)
            ).fetchone()
        )

    @staticmethod
    def _save_task(conn, task, origin_kind, origin_id):
        """Persist one validated pending revision and its history atomically."""
        prior = _decode(
            conn.execute(
                "SELECT id,revision,source,cancelled FROM tasks WHERE id=?",
                (task["id"],),
            ).fetchone()
        )
        if conn.execute(
            "SELECT 1 FROM iterations WHERE task_id=?", (task["id"],)
        ).fetchone():
            raise InvalidTransition("A claimed task envelope is frozen")
        if prior and prior["cancelled"]:
            raise InvalidTransition("Cancelled task identities remain reserved")
        if prior and prior["source"] == "human" and task["source"] != "human":
            raise InvalidTransition("Human task ownership cannot be downgraded")
        if task["revision"] != (prior["revision"] + 1 if prior else 1):
            raise ConflictError("Task revision must advance exactly once")
        values = (
            task["id"],
            task["revision"],
            _json(task["specification"]),
            task["source"],
            task["priority"],
            _json(task["dependencies"]),
            int(task["cancelled"]),
        )
        conn.execute(
            "INSERT INTO tasks VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "revision=excluded.revision,specification_json=excluded.specification_json,"
            "source=excluded.source,priority=excluded.priority,"
            "dependencies_json=excluded.dependencies_json,cancelled=excluded.cancelled",
            values,
        )
        conn.execute("UPDATE control SET pool_version=pool_version+1")
        version = conn.execute("SELECT pool_version FROM control").fetchone()[0]
        conn.execute(
            "INSERT INTO task_revisions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (*values, version, origin_kind, origin_id),
        )
        return _decode(
            conn.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()
        )

    @staticmethod
    def _ready_tasks(conn):
        claimed = {
            row["task_id"]: row["status"]
            for row in conn.execute("SELECT task_id,status FROM iterations")
        }
        ready = []
        for row in conn.execute(f"SELECT {_TASK_METADATA} FROM tasks"):
            task = _decode(row)
            if task["id"] in claimed or task["cancelled"]:
                continue
            if all(claimed.get(dep) == "published" for dep in task["dependencies"]):
                ready.append(task)
        return sorted(
            ready,
            key=lambda task: (task["source"] != "human", task["priority"], task["id"]),
        )

    def _claim_task(self, conn, control, task, expected_revision, *, ready=None):
        if task is None or task["revision"] != expected_revision:
            raise ConflictError("Unknown task or task revision mismatch")
        existing = conn.execute(
            "SELECT * FROM iterations WHERE task_id=?", (task["id"],)
        ).fetchone()
        if existing:
            return _decode(existing)
        if task["cancelled"]:
            raise InvalidTransition("Cancelled tasks cannot be claimed")
        self._admit(control, deadline=True)
        limit = control["max_iterations"]
        if limit is not None and control["iterations"] >= limit:
            raise BudgetExceeded("Iteration budget exhausted")
        for dependency in task["dependencies"]:
            record = conn.execute(
                "SELECT status FROM iterations WHERE task_id=?", (dependency,)
            ).fetchone()
            if record is None or record["status"] != "published":
                raise InvalidTransition(f"Dependency is not published: {dependency}")
        if ready is None:
            ready = self._ready_tasks(conn)
        if not ready or ready[0]["id"] != task["id"]:
            raise InvalidTransition("Higher-priority ready task must be claimed first")
        iteration_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO iterations VALUES(?,?,?,1,'plan','active')",
            (iteration_id, task["id"], expected_revision),
        )
        conn.execute(
            "UPDATE control SET iterations=iterations+1,pool_version=pool_version+1"
        )
        return _decode(
            conn.execute(
                "SELECT * FROM iterations WHERE id=?", (iteration_id,)
            ).fetchone()
        )

    def claim_next(self):
        """Claim the highest-priority dependency-ready task in one transaction."""
        with self._transaction() as (conn, control):
            ready = self._ready_tasks(conn)
            if not ready:
                return None
            return self._claim_task(
                conn, control, ready[0], ready[0]["revision"], ready=ready
            )

    def claim_task(self, task_id, expected_revision=1):
        _revision(expected_revision)
        with self._transaction() as (conn, control):
            task = _decode(
                conn.execute(
                    f"SELECT {_TASK_METADATA} FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
            )
            return self._claim_task(conn, control, task, expected_revision)

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
            return self._reserve_attempt(
                conn,
                control,
                role,
                request_id=request_id,
                iteration_id=iteration_id,
                expected_revision=expected_revision,
            )

    def _reserve_attempt(
        self,
        conn,
        control,
        role,
        *,
        request_id,
        iteration_id=None,
        expected_revision=None,
    ):
        """Reserve within an existing owner transaction; callers validate scope."""
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
            if role == "review":
                from .operations import Operations

                Operations.check_review_ready(conn, iteration_id)
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
        control["calls"] += 1
        return _decode(
            conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
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
            if conn.execute(
                "SELECT 1 FROM contexts WHERE attempt_id=?", (attempt_id,)
            ).fetchone():
                raise InvalidTransition("Context-bound attempts require the Updater")
            return self._accept_record(conn, attempt, output_ref, identity)

    def _accept_record(
        self, conn, attempt, output_ref, identity, *, receipt_extra=None
    ):
        """Accept within the caller's fenced transaction; never open a nested one."""
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
            "attempt_id": attempt["id"],
            "output_ref": output_ref,
            "owner_epoch": self.owner_epoch,
            "iteration_id": attempt["iteration_id"],
            "revision": attempt["expected_revision"],
        }
        if iteration is not None:
            receipt["revision"] = iteration["revision"] + 1
            conn.execute(
                "UPDATE iterations SET phase=?,revision=revision+1 WHERE id=?",
                (PHASES[attempt["role"]], iteration["id"]),
            )
        if receipt_extra is not None:
            receipt.update(receipt_extra)
        conn.execute(
            "UPDATE attempts SET status='accepted',output_ref=?,output_identity_json=?,receipt_json=? WHERE id=?",
            (output_ref, _json(identity), _json(receipt), attempt["id"]),
        )
        return receipt

    def _feedback_record(self, conn, call, *, metadata_only=False):
        """Read and authenticate the exact call against its original reservation."""
        attempt = self._attempt(
            conn,
            call["attempt_id"],
            call["worker_token"],
            call["expected_revision"],
            allow_accepted_replay=True,
        )
        control = _decode(
            conn.execute(
                "SELECT study_id FROM control"
                if metadata_only
                else "SELECT * FROM control"
            ).fetchone()
        )
        expected = {
            key: attempt[key]
            for key in (
                "worker_token",
                "owner_epoch",
                "role",
                "iteration_id",
                "expected_revision",
            )
        }
        expected.update(study_id=control["study_id"], attempt_id=attempt["id"])
        if _json(call) != _json(expected):
            raise ConflictError("Call identity differs from original reservation")
        iteration = _decode(
            conn.execute(
                "SELECT * FROM iterations WHERE id=?", (attempt["iteration_id"],)
            ).fetchone()
        )
        task = None
        if iteration is not None:
            columns = "id,revision" if metadata_only else "*"
            task = _decode(
                conn.execute(
                    f"SELECT {columns} FROM tasks WHERE id=?", (iteration["task_id"],)
                ).fetchone()
            )
            if task is None or task["revision"] != iteration["task_revision"]:
                raise ConflictError(
                    "Claimed task revision no longer matches its frozen envelope"
                )
        binding = _decode(
            conn.execute(
                "SELECT * FROM contexts WHERE attempt_id=?", (attempt["id"],)
            ).fetchone()
        )
        plan = _decode(
            conn.execute(
                "SELECT attempts.id,attempts.output_ref,attempts.output_identity_json "
                "FROM attempts JOIN contexts ON contexts.attempt_id=attempts.id "
                "WHERE attempts.iteration_id=? AND attempts.role='plan' AND attempts.status='accepted'",
                (attempt["iteration_id"],),
            ).fetchone()
        )
        return dict(
            control=control,
            attempt=attempt,
            iteration=iteration,
            task=task,
            binding=binding,
            plan=plan,
            knowledge_version=conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM publications"
            ).fetchone()[0],
            planning=self._planning_record(conn, attempt),
        )

    @staticmethod
    def _planning_record(conn, attempt):
        row = _decode(
            conn.execute(
                "SELECT r.id,r.snapshot_ref,r.identity_json,r.knowledge_ref,r.knowledge_identity_json FROM planning_rounds r "
                "JOIN planning_calls c ON c.round_id=r.id WHERE c.attempt_id=?",
                (attempt["id"],),
            ).fetchone()
        )
        if row is None:
            return None
        if attempt["role"] == "judge":
            row["advocates"] = [
                _decode(record)
                for record in conn.execute(
                    "SELECT c.slot,a.id,a.status,a.output_ref,a.output_identity_json,a.failure_reason "
                    "FROM planning_calls c JOIN attempts a ON a.id=c.attempt_id "
                    "WHERE c.round_id=? AND c.slot IN ('advocate_a','advocate_b') ORDER BY c.slot",
                    (row["id"],),
                )
            ]
        return row

    def _feedback_snapshot(self, call):
        self._live()
        with self.store._connection(readonly=True) as conn:
            conn.execute("BEGIN")
            return self._feedback_record(conn, call)

    @staticmethod
    def _iteration_for_feedback(record):
        attempt, iteration = record["attempt"], record["iteration"]
        if attempt["iteration_id"] is None:
            return
        if iteration is None or iteration["revision"] != attempt["expected_revision"]:
            raise ConflictError("Iteration revision mismatch")
        if iteration["status"] != "active" or iteration["phase"] != attempt["role"]:
            raise InvalidTransition("Iteration no longer accepts this phase")

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
                conn.execute("UPDATE control SET pool_version=pool_version+1")
                self._planning_event(
                    conn,
                    f"iteration-failed:{iteration['id']}:{attempt_id}",
                    "task_result",
                    {
                        "iteration_id": iteration["id"],
                        "attempt_id": attempt_id,
                        "status": "failed",
                    },
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
