"""Read-only, allowlisted monitoring facts from one coherent SQLite snapshot.

No owner is acquired, elapsed budget is not advanced, and no adapter or artifact
body is consulted. Callback retirement, external release and fees are separate.
"""

from datetime import datetime, timezone
import json

from .store import StateStore


def _rows(conn, statement):
    return [dict(row) for row in conn.execute(statement)]


def status_snapshot(root):
    """Return detached public control metadata, excluding raw/private inputs."""
    store = StateStore(root)
    with store._connection(readonly=True) as conn:
        conn.execute("BEGIN")
        control = dict(
            conn.execute(
                "SELECT study_id,max_iterations,max_calls,max_seconds,admission_deadline,"
                "calls,iterations,elapsed_seconds,stop_reason IS NOT NULL AS stopped,"
                "owner_epoch,owner_open,pool_version,admission_mode,"
                "json_extract(config_json,'$.task_pool.max_pending') AS max_pending FROM control"
            ).fetchone()
        )
        schema = conn.execute("PRAGMA user_version").fetchone()[0]
        migrations = _rows(
            conn,
            "SELECT id,revision,committed_at,activation_ref IS NOT NULL AS activated,additional_elapsed_seconds,blockers_json FROM migration_imports",
        )
        legacy_records = _rows(
            conn,
            "SELECT import_id,kind,legacy_id,native_status FROM migration_legacy_records ORDER BY kind,legacy_id",
        )
        for migration in migrations:
            migration["blockers"] = json.loads(migration.pop("blockers_json"))
        controls = _rows(
            conn,
            "SELECT request_id,kind,status FROM control_requests WHERE status!='applied' ORDER BY rowid",
        )
        tasks = _rows(
            conn,
            "SELECT id,revision,source,priority,dependencies_json,cancelled FROM tasks "
            "ORDER BY CASE WHEN source='human' THEN 0 ELSE 1 END,priority,id",
        )
        iterations = _rows(
            conn,
            "SELECT id,task_id,task_revision,revision,phase,status FROM iterations ORDER BY id",
        )
        attempts = _rows(
            conn,
            "SELECT id,role,iteration_id,status,owner_epoch FROM attempts ORDER BY rowid",
        )
        runs = _rows(
            conn,
            "SELECT attempt_id,operation_id,category,action_kind,state,status,worker_retired,settled,owner_epoch "
            "FROM dispatch_runs ORDER BY rowid",
        )
        operations = _rows(
            conn,
            "SELECT o.id,o.logical_id,o.execute_attempt_id,g.iteration_id,o.backend,o.state,"
            "o.terminal_outcome,o.physical_released,o.held_resources_json,o.held_costs_json,"
            "o.actual_costs_json,o.cost_bounds_json,json_array_length(o.unexpected_job_ids_json) AS unexpected_job_count,"
            "o.inconsistency,o.collection_state,o.external_open,g.feedback_sealed "
            "FROM operations o JOIN operation_groups g ON g.execute_attempt_id=o.execute_attempt_id ORDER BY o.id",
        )
        intakes = _rows(
            conn,
            "SELECT operation_id,disposition,COUNT(*) AS count "
            "FROM evaluation_intake GROUP BY operation_id,disposition",
        )
        publication_version = conn.execute(
            "SELECT COALESCE(MAX(id),0) FROM publications"
        ).fetchone()[0]
        published_attempts = {
            row[0] for row in conn.execute("SELECT attempt_id FROM publications")
        }
    by_iteration = {row["task_id"]: row for row in iterations}
    tasks_by_id = {row["id"]: row for row in tasks}
    runs_by_attempt = {
        row["attempt_id"]: row for row in runs if row["attempt_id"] is not None
    }
    for attempt in attempts:
        run = runs_by_attempt.get(attempt["id"])
        attempt["dispatch"] = (
            None
            if run is None
            else {
                "state": run["state"],
                "status": run["status"],
                "worker_retired": bool(run["worker_retired"]),
                "settled": bool(run["settled"]),
            }
        )
        attempt["published"] = attempt["id"] in published_attempts
        attempt["unresolved"] = attempt["status"] == "active" and (
            attempt["owner_epoch"] < control["owner_epoch"]
            or run is not None
            and run["state"] == "unknown"
        )
    intake_counts = {}
    for row in intakes:
        intake_counts.setdefault(
            row["operation_id"], dict(pending=0, registered=0, rejected=0)
        )[row["disposition"]] = row["count"]
    for operation in operations:
        for field in ("held_resources", "held_costs", "actual_costs"):
            operation[field] = json.loads(operation.pop(field + "_json"))
        bounds = json.loads(operation.pop("cost_bounds_json"))
        operation["costs_final"] = (
            operation["unexpected_job_count"] == 0
            and not operation["held_costs"]
            and bounds.keys() <= operation["actual_costs"].keys()
        )
        for field in ("physical_released", "external_open", "feedback_sealed"):
            operation[field] = bool(operation[field])
        operation["intake"] = intake_counts.get(
            operation["id"], dict(pending=0, registered=0, rejected=0)
        )
        local_runs = [row for row in runs if row["operation_id"] == operation["id"]]
        latest = local_runs[-1] if local_runs else None
        operation["dispatch"] = (
            None
            if latest is None
            else {
                "action": latest["action_kind"],
                "state": latest["state"],
                "status": latest["status"],
                "worker_retired": bool(latest["worker_retired"]),
                "settled": bool(latest["settled"]),
            }
        )
        operation["unresolved"] = (
            operation["inconsistency"] is not None
            or operation["state"] == "unknown"
            or latest is not None
            and not latest["settled"]
            and (
                latest["state"] == "unknown"
                or latest["owner_epoch"] < control["owner_epoch"]
            )
        )
    for task in tasks:
        task["dependencies"] = json.loads(task.pop("dependencies_json"))
        task["cancelled"] = bool(task["cancelled"])
        iteration = by_iteration.get(task["id"])
        blocked = []
        if iteration is None and not task["cancelled"]:
            for identity in task["dependencies"]:
                prior = by_iteration.get(identity)
                if tasks_by_id.get(identity, {}).get("cancelled"):
                    reason = "cancelled"
                elif prior is None:
                    reason = "unclaimed"
                elif prior["status"] == "failed":
                    reason = "failed"
                elif prior["status"] != "published":
                    reason = "unpublished"
                else:
                    continue
                blocked.append(dict(id=identity, reason=reason))
        task.update(
            iteration_id=iteration["id"] if iteration else None,
            iteration_status=iteration["status"] if iteration else None,
            phase=iteration["phase"] if iteration else None,
            ready=iteration is None and not task["cancelled"] and not blocked,
            dependency_blocked=bool(blocked),
            blocked_dependencies=blocked,
        )
        local_attempts = [
            row
            for row in attempts
            if iteration and row["iteration_id"] == iteration["id"]
        ]
        local_operations = [
            row
            for row in operations
            if iteration and row["iteration_id"] == iteration["id"]
        ]
        task["execution"] = (
            "terminal"
            if iteration
            and (
                iteration["phase"] in {"review", "publication", "done"}
                and all(row["state"] == "terminal" for row in local_operations)
            )
            else "pending"
        )
        executes = [row for row in local_attempts if row["role"] == "execute"]
        if not local_operations and executes and executes[-1]["status"] == "failed":
            task["execution"] = "failed"
        task["fees_settled"] = (
            all(row["costs_final"] for row in local_operations)
            if local_operations
            else None
        )
        task["physical_released"] = (
            all(row["physical_released"] for row in local_operations)
            if local_operations
            else None
        )
        task["feedback_sealed"] = (
            all(row["feedback_sealed"] for row in local_operations)
            if local_operations
            else None
        )
        task["unresolved"] = any(
            row["unresolved"] for row in local_attempts + local_operations
        )
        reviewed = any(
            row["role"] == "review" and row["status"] == "accepted"
            for row in local_attempts
        )
        running = any(
            row["status"] == "active"
            and row["dispatch"] is not None
            and row["dispatch"]["state"] == "running"
            and not row["dispatch"]["worker_retired"]
            for row in local_attempts
        ) or any(
            row["state"] in {"submitting", "submitted"}
            or (
                row["dispatch"] is not None
                and row["dispatch"]["state"] == "running"
                and not row["dispatch"]["worker_retired"]
            )
            for row in local_operations
        )
        task["status"] = (
            "published"
            if iteration and iteration["status"] == "published"
            else "unresolved"
            if task["unresolved"]
            else "reviewed"
            if reviewed
            else "cancelled"
            if task["cancelled"]
            else "failed"
            if iteration and iteration["status"] == "failed"
            else "running"
            if running
            else "queued"
            if iteration is None
            or any(row["status"] == "active" for row in local_attempts)
            else "waiting"
        )
        task["queued"] = task["status"] == "queued"
    return dict(
        study_id=control["study_id"],
        schema_version=schema,
        reported_at=datetime.now(timezone.utc).isoformat(),
        owner_open=bool(control["owner_open"]),
        owner_epoch=control["owner_epoch"],
        pool_version=control["pool_version"],
        task_pool={
            "max_pending": control["max_pending"],
            "pending": sum(
                t["iteration_id"] is None and not t["cancelled"] for t in tasks
            ),
            "ready": sum(t["ready"] for t in tasks),
        },
        publication_version=publication_version,
        admission_mode=control["admission_mode"],
        migrations=migrations,
        legacy_records=legacy_records,
        stop={"requested": bool(control["stopped"])},
        limits={
            key: control[key]
            for key in (
                "max_iterations",
                "max_calls",
                "max_seconds",
                "admission_deadline",
            )
        },
        counters={
            key: control[key] for key in ("calls", "iterations", "elapsed_seconds")
        },
        tasks=tasks,
        iterations=iterations,
        attempts=attempts,
        operations=operations,
        control_requests=controls,
    )
