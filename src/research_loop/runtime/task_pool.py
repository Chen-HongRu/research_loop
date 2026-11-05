"""Trusted human task edits and snapshot-bound autonomous Judge changes."""

import json
import re

from .context import ContextPackage
from .store import (
    ConflictError,
    InvalidTransition,
    PendingTaskLimitExceeded,
    _decode,
    _json,
    _text,
)
from .updates import CallIdentity, Updater, _same

_TASK_FIELDS = {
    "id",
    "revision",
    "specification",
    "source",
    "priority",
    "dependencies",
    "cancelled",
}
_ROUND_FIELDS = "r.id,r.owner_epoch,r.pool_version,r.event_from,r.event_through,r.snapshot_ref,r.identity_json,r.status"


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value
    ):
        raise ValueError("Invalid task id")


def _revision(value):
    if type(value) is not int or value < 1:
        raise ValueError("Task revision must be a positive integer")


def _fields(identity, specification, priority, dependencies):
    _identifier(identity)
    if not isinstance(specification, dict) or type(priority) is not int:
        raise ValueError("Task specification must be an object and priority an integer")
    if not isinstance(dependencies, (tuple, list)):
        raise ValueError("Task dependencies must be an explicit list")
    for dependency in dependencies:
        _identifier(dependency)
    if len(set(dependencies)) != len(dependencies):
        raise ValueError("Task dependencies must be unique")
    return dict(
        specification=json.loads(_json(specification)),
        priority=priority,
        dependencies=list(dependencies),
    )


def _dependency_issue(conn, identity, dependencies):
    for dependency in dependencies:
        if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (dependency,)).fetchone():
            if dependency != identity:
                return "missing_dependency"
    pending = list(dependencies)
    visited = set()
    while pending:
        dependency = pending.pop()
        if dependency == identity:
            return "dependency_cycle"
        if dependency in visited:
            continue
        visited.add(dependency)
        row = conn.execute(
            "SELECT dependencies_json FROM tasks WHERE id=?", (dependency,)
        ).fetchone()
        if row is not None:
            pending.extend(json.loads(row[0]))
    return None


class TaskPool:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.reader = Updater(coordinator, validators={})

    def snapshot(self):
        """Inspect one coherent pool version without admitting or changing work."""
        self.coordinator._live()
        with self.coordinator.store._connection(readonly=True) as conn:
            conn.execute("BEGIN")
            version = conn.execute("SELECT pool_version FROM control").fetchone()[0]
            tasks = [
                _decode(row)
                for row in conn.execute(
                    "SELECT * FROM tasks ORDER BY CASE WHEN source='human' THEN 0 ELSE 1 END,priority,id"
                )
            ]
            by_id = {task["id"]: task for task in tasks}
            iterations = {
                row["task_id"]: _decode(row)
                for row in conn.execute("SELECT * FROM iterations")
            }
            result = []
            for task in tasks:
                iteration = iterations.get(task["id"])
                status = (
                    "claimed"
                    if iteration is not None
                    else ("cancelled" if task["cancelled"] else "pending")
                )
                blocked = []
                if status == "pending":
                    for dependency in task["dependencies"]:
                        prior = iterations.get(dependency)
                        if by_id.get(dependency, {}).get("cancelled"):
                            reason = "cancelled"
                        elif prior is None:
                            reason = "unclaimed"
                        elif prior["status"] == "failed":
                            reason = "failed"
                        elif prior["status"] != "published":
                            reason = "unpublished"
                        else:
                            continue
                        blocked.append(f"dependency:{dependency}:{reason}")
                result.append(
                    {
                        **task,
                        "status": status,
                        "iteration": iteration,
                        "blocked_reasons": blocked,
                        "ready": status == "pending" and not blocked,
                    }
                )
            return dict(version=version, tasks=result)

    def create_human(
        self, task_id, specification, *, request_id, priority=100, dependencies=()
    ):
        return self._human(
            "create",
            task_id,
            request_id=request_id,
            fields=_fields(task_id, specification, priority, dependencies),
        )

    def edit_human(
        self,
        task_id,
        expected_revision,
        *,
        request_id,
        specification,
        priority,
        dependencies,
    ):
        _revision(expected_revision)
        return self._human(
            "update",
            task_id,
            request_id=request_id,
            expected_revision=expected_revision,
            fields=_fields(task_id, specification, priority, dependencies),
        )

    def cancel_human(self, task_id, expected_revision, *, request_id):
        _identifier(task_id)
        _revision(expected_revision)
        return self._human(
            "cancel",
            task_id,
            request_id=request_id,
            expected_revision=expected_revision,
        )

    def _human(
        self, action, identity, *, request_id, expected_revision=None, fields=None
    ):
        _text(request_id, "request_id")
        intent = dict(action=action, id=identity)
        if expected_revision is not None:
            intent["expected_revision"] = expected_revision
        if fields is not None:
            intent.update(fields)
        intent_json = _json(intent)
        with self.coordinator._transaction() as (conn, _):
            previous = _decode(
                conn.execute(
                    "SELECT * FROM task_requests WHERE request_id=?", (request_id,)
                ).fetchone()
            )
            if previous is not None:
                if not _same(previous["intent"], intent):
                    raise ConflictError(
                        "Human task request id reused with a different intent"
                    )
                return previous["receipt"]
            current = _decode(
                conn.execute("SELECT * FROM tasks WHERE id=?", (identity,)).fetchone()
            )
            if action == "create":
                if current is not None:
                    raise ConflictError(
                        "Task id already exists, including cancelled tasks"
                    )
                task = dict(
                    id=identity, revision=1, source="human", cancelled=False, **fields
                )
            else:
                if current is None or current["revision"] != expected_revision:
                    raise ConflictError("Unknown task or task revision mismatch")
                task = {
                    **current,
                    "revision": expected_revision + 1,
                    "source": "human",
                    "cancelled": action == "cancel",
                }
                if fields is not None:
                    task.update(fields)
            issue = (
                _dependency_issue(conn, identity, task["dependencies"])
                if action != "cancel"
                else None
            )
            if issue is not None:
                raise ConflictError(issue)
            saved = self.coordinator._save_task(conn, task, "human", request_id)
            receipt = dict(
                id=identity,
                revision=saved["revision"],
                source=saved["source"],
                cancelled=bool(saved["cancelled"]),
                pool_version=conn.execute(
                    "SELECT pool_version FROM control"
                ).fetchone()[0],
            )
            self.coordinator._planning_event(
                conn,
                "human:" + request_id,
                "human_task",
                dict(id=identity, revision=saved["revision"]),
            )
            conn.execute(
                "INSERT INTO task_requests VALUES(?,?,?)",
                (request_id, intent_json, _json(receipt)),
            )
            return receipt

    @staticmethod
    def _round(conn, attempt_id):
        row = conn.execute(
            f"SELECT {_ROUND_FIELDS} FROM planning_rounds r JOIN planning_calls c ON c.round_id=r.id "
            "WHERE c.attempt_id=? AND c.slot='judge'",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise InvalidTransition("Judge attempt is not a reserved planning slot")
        return _decode(row)

    def _capture(self, call):
        if not isinstance(call, CallIdentity):
            raise TypeError("Expected CallIdentity")
        record = self.coordinator._feedback_snapshot(call.to_dict())
        if (
            record["attempt"]["role"] != "judge"
            or record["attempt"]["status"] != "accepted"
            or record["binding"] is None
        ):
            raise InvalidTransition(
                "Task changes require accepted context-bound Judge feedback"
            )
        with self.coordinator.store._connection(readonly=True) as conn:
            conn.execute("BEGIN")
            round_record = self._round(conn, call.attempt_id)
            previous = _decode(
                conn.execute(
                    "SELECT * FROM task_batches WHERE attempt_id=?", (call.attempt_id,)
                ).fetchone()
            )
        binding = record["binding"]
        snapshot = self.reader._read_record(
            round_record["snapshot_ref"], round_record["identity"]
        )
        package = ContextPackage.from_dict(
            self.reader._read_record(binding["context_ref"], binding["identity"])
        )
        if "planning" not in package.manifest["protected_keys"] or not _same(
            package.context.get("planning"), snapshot
        ):
            raise ConflictError(
                "Judge planning context differs from its immutable round"
            )
        return record, round_record, snapshot, previous

    @staticmethod
    def _snapshot_tasks(round_record, snapshot):
        if (
            not isinstance(snapshot, dict)
            or set(snapshot)
            != {
                "round_id",
                "pool",
                "in_flight",
                "events",
                "event_from",
                "event_through",
                "knowledge_version",
            }
            or not isinstance(snapshot["pool"], dict)
            or set(snapshot["pool"]) != {"version", "tasks"}
            or snapshot["round_id"] != round_record["id"]
            or snapshot["pool"]["version"] != round_record["pool_version"]
            or snapshot["event_from"] != round_record["event_from"]
            or snapshot["event_through"] != round_record["event_through"]
            or not isinstance(snapshot["pool"]["tasks"], list)
        ):
            raise ValueError("Malformed immutable planning snapshot")
        tasks = {}
        for task in snapshot["pool"]["tasks"]:
            if not isinstance(task, dict) or set(task) != _TASK_FIELDS:
                raise ValueError("Malformed planning task envelope")
            _identifier(task["id"])
            _revision(task["revision"])
            if task["id"] in tasks:
                raise ValueError("Duplicate task in planning snapshot")
            tasks[task["id"]] = task
        return tasks

    @staticmethod
    def _changes(output):
        if (
            not isinstance(output, dict)
            or set(output) != {"changes"}
            or not isinstance(output["changes"], list)
        ):
            raise ValueError("Judge output must contain an explicit changes list")
        changes = []
        seen = set()
        for item in output["changes"]:
            if not isinstance(item, dict):
                raise ValueError("Judge changes must be objects")
            action = item.get("action")
            keys = (
                {"action", "id", "specification", "priority", "dependencies"}
                if action == "create"
                else (
                    {
                        "action",
                        "id",
                        "expected_revision",
                        "specification",
                        "priority",
                        "dependencies",
                    }
                    if action == "update"
                    else {"action", "id", "expected_revision"}
                )
            )
            if (
                not isinstance(action, str)
                or action not in {"create", "update", "cancel"}
                or set(item) != keys
            ):
                raise ValueError("Unknown or unauthorized Judge change fields")
            _identifier(item["id"])
            if item["id"] in seen:
                raise ValueError("A Judge batch cannot target the same task twice")
            seen.add(item["id"])
            change = dict(action=action, id=item["id"])
            if action != "create":
                _revision(item["expected_revision"])
                change["expected_revision"] = item["expected_revision"]
            if action != "cancel":
                change.update(
                    _fields(
                        item["id"],
                        item["specification"],
                        item["priority"],
                        item["dependencies"],
                    )
                )
            changes.append(change)
        return changes

    @staticmethod
    def _conflict(conn, change, shown):
        identity, action = change["id"], change["action"]
        current = _decode(
            conn.execute("SELECT * FROM tasks WHERE id=?", (identity,)).fetchone()
        )
        if action == "create":
            return current, "already_exists" if current is not None else None
        frozen = shown.get(identity)
        if frozen is None:
            return current, "not_in_snapshot"
        if change["expected_revision"] != frozen["revision"]:
            return current, "revision_not_visible"
        if current is None:
            return current, "not_found"
        if current["source"] != "autonomous":
            return current, "human_owned"
        if conn.execute(
            "SELECT 1 FROM iterations WHERE task_id=?", (identity,)
        ).fetchone():
            return current, "claimed"
        if current["cancelled"]:
            return current, "cancelled"
        if current["revision"] != change["expected_revision"]:
            return current, "revision_mismatch"
        return current, None

    def apply_judge(self, call_identity):
        captured, round_record, snapshot, previous = self._capture(call_identity)
        attempt = captured["attempt"]
        proposal = self.reader._read_record(
            attempt["output_ref"], attempt["output_identity"]
        )
        if (
            not _same(proposal["call"], call_identity.to_dict())
            or proposal["context_ref"] != captured["binding"]["context_ref"]
        ):
            raise ConflictError("Judge proposal differs from its accepted call")
        self.reader._check_frozen_refs(captured, captured)
        self.reader._check_accepted_refs(captured, proposal)
        if previous is not None:
            if previous["payload_ref"] != attempt["output_ref"] or not _same(
                previous["identity"], attempt["output_identity"]
            ):
                raise ConflictError(
                    "Committed Judge batch differs from accepted output"
                )
            with self.coordinator._transaction() as (conn, _):
                current = self.coordinator._feedback_record(
                    conn, call_identity.to_dict(), metadata_only=True
                )
                if not _same(current["attempt"], attempt):
                    raise ConflictError("Accepted Judge attempt changed")
                self.reader._check_frozen_refs(current, captured)
                self.reader._check_accepted_refs(current, proposal)
                return previous["receipt"]
        shown = self._snapshot_tasks(round_record, snapshot)
        if round_record["status"] != "active":
            raise InvalidTransition(
                "An unapplied Judge batch requires an active planning round"
            )
        changes = self._changes(proposal["normalized_output"])
        with self.coordinator._transaction() as (conn, _):
            current = self.coordinator._feedback_record(
                conn, call_identity.to_dict(), metadata_only=True
            )
            current_round = self._round(conn, call_identity.attempt_id)
            if (
                not _same(current["attempt"], attempt)
                or not _same(current["binding"], captured["binding"])
                or not _same(
                    {
                        key: value
                        for key, value in current_round.items()
                        if key != "status"
                    },
                    {
                        key: value
                        for key, value in round_record.items()
                        if key != "status"
                    },
                )
            ):
                raise ConflictError(
                    "Judge attempt, planning snapshot or binding changed"
                )
            self.reader._check_frozen_refs(current, captured)
            self.reader._check_accepted_refs(current, proposal)
            existing = _decode(
                conn.execute(
                    "SELECT * FROM task_batches WHERE attempt_id=?",
                    (call_identity.attempt_id,),
                ).fetchone()
            )
            if existing is not None:
                return existing["receipt"]
            if current_round["status"] != "active":
                raise InvalidTransition(
                    "An unapplied Judge batch requires an active planning round"
                )
            receipt = dict(
                attempt_id=call_identity.attempt_id, applied=[], conflicts=[]
            )
            for index, change in enumerate(changes):
                old, reason = self._conflict(conn, change, shown)
                if reason is None and change["action"] != "cancel":
                    reason = _dependency_issue(
                        conn, change["id"], change["dependencies"]
                    )
                if reason is not None:
                    receipt["conflicts"].append(
                        dict(index=index, id=change["id"], reason=reason)
                    )
                    continue
                if change["action"] == "create":
                    task = dict(
                        id=change["id"],
                        revision=1,
                        specification=change["specification"],
                        source="autonomous",
                        priority=change["priority"],
                        dependencies=change["dependencies"],
                        cancelled=False,
                    )
                else:
                    task = {
                        **old,
                        "revision": old["revision"] + 1,
                        "cancelled": change["action"] == "cancel",
                    }
                    if change["action"] == "update":
                        task.update(
                            {
                                key: change[key]
                                for key in ("specification", "priority", "dependencies")
                            }
                        )
                try:
                    saved = self.coordinator._save_task(
                        conn, task, "judge", call_identity.attempt_id
                    )
                except PendingTaskLimitExceeded:
                    receipt["conflicts"].append(
                        dict(index=index, id=change["id"], reason="max_pending")
                    )
                    continue
                receipt["applied"].append(
                    dict(id=saved["id"], revision=saved["revision"])
                )
            conn.execute(
                "INSERT INTO task_batches VALUES(?,?,?,?)",
                (
                    call_identity.attempt_id,
                    attempt["output_ref"],
                    _json(attempt["output_identity"]),
                    _json(receipt),
                ),
            )
            return receipt
