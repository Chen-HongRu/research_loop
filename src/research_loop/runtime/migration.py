"""Explicit, resumable legacy authority transfer; no backend or model calls.

Fence and activation documents are assertions by the trusted operator. Their
shape is not proof that a service was stopped or that an external job ended.
"""

from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .files import _sync_directory, publish_artifact, publish_json
from .format import require_legacy
from .updates import _identity
from .store import (
    DATABASE,
    SCHEMA_VERSION,
    ConflictError,
    InvalidTransition,
    OwnershipError,
    StateStore,
    _decode,
    _json,
    _number,
)


def _read(path):
    return json.loads(Path(path).read_text())


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("Import ID must contain 1–64 safe letters, digits, _ or -")
    return value


def _references(value, name, *, empty=False):
    if (
        not isinstance(value, list)
        or (not empty and not value)
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ValueError(f"{name} requires explicit evidence references")


@contextmanager
def _source_locks(root):
    """Use the same lock order as Study followed by its operator task queue."""
    root = Path(root).resolve()
    queue = root / "tasks"
    if queue.is_symlink():
        raise ValueError("Legacy task directory must not be a symlink")
    queue.mkdir(exist_ok=True)
    descriptors = []
    try:
        for path in (root / ".lock", queue / "queue.lock"):
            if path.is_symlink():
                raise ValueError("Legacy lock must not be a symlink")
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            descriptors.append(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise OwnershipError(
                    "A legacy controller or task writer is active"
                ) from exc
        yield descriptors[0]
    finally:
        for fd in reversed(descriptors):
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _marker(root, value):
    temporary = root / (".runtime-marker-" + uuid.uuid4().hex)
    try:
        with temporary.open("x") as handle:
            json.dump(value, handle, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, root / "runtime.json")
        _sync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)


def _checkpoint(path):
    with sqlite3.connect(path) as conn:
        result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result[0]:
            raise OwnershipError("Staged database still has an active writer")
    with Path(path).open("rb") as handle:
        os.fsync(handle.fileno())
    _sync_directory(Path(path).parent)


def _validate_inputs(inventory, config, fence, deployment):
    from .coordinator import validate_settings

    for key in ("calls", "iterations", "elapsed_seconds"):
        _number(
            inventory["counters"][key],
            f"original {key}",
            integer=key != "elapsed_seconds",
        )
    legacy = json.loads(inventory["records"]["study.json"]["text"])
    original_state = json.loads(inventory["records"]["state.json"]["text"])
    expected_counters = {
        "calls": original_state["call_count"],
        "iterations": original_state["iteration"],
        "elapsed_seconds": original_state["elapsed_seconds"],
    }
    expected_limits = {
        key: legacy[key] for key in ("max_iterations", "max_calls", "max_seconds")
    }
    expected_limits["admission_deadline"] = None
    if (
        inventory["counters"] != expected_counters
        or inventory["limits"] != expected_limits
        or inventory["native_state"] != original_state
        or type(legacy.get("allow_write")) is not bool
    ):
        raise ValueError(
            "Inventory summaries differ from the exact original control records"
        )
    goal = inventory["records"]["goal.md"]["text"]
    if (
        config.get("goal") != goal
        or config.get("global_evaluation") != legacy.get("global_evaluation")
        or config.get("permissions") != {"allow_write": legacy["allow_write"]}
    ):
        raise ValueError(
            "Migration must preserve the original goal, evaluation and write permission"
        )
    validate_settings(config.get("runtime"))
    if type(config.get("legacy_history_feedback", False)) is not bool:
        raise ValueError("Legacy history feedback must be an explicit boolean")
    if not isinstance(config["runtime"].get("application"), dict):
        raise ValueError(
            "Migration requires an explicitly prepared runtime application"
        )
    if (
        not isinstance(fence, dict)
        or fence.get("version") != 1
        or fence.get("source_identity") != inventory["source_identity"]
        or fence.get("launchers_disabled") is not True
        or fence.get("senders_retired") is not True
    ):
        raise ValueError(
            "Cutover requires a source-bound fence for old launchers and senders"
        )
    _references(fence.get("evidence_refs"), "Fencing")
    if (
        not isinstance(deployment, dict)
        or deployment.get("version") != 1
        or deployment.get("runtime_schema") != SCHEMA_VERSION
        or not isinstance(deployment.get("revision"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", deployment["revision"])
        or deployment.get("application") != config["runtime"]["worker_adapter"]
        or not isinstance(deployment.get("environment"), dict)
        or set(deployment["environment"]) != {"id", "version"}
        or any(
            not isinstance(v, str) or not v.strip()
            for v in deployment["environment"].values()
        )
    ):
        raise ValueError(
            "Deployment must bind the source revision, schema, environment and application"
        )


def _history(conn, inventory, import_id):
    """Index native history without reclassifying it as runtime publication."""
    for kind, rows, identity_key, reference_key in (
        ("iteration", inventory["history"]["iterations"], "id", "metadata_ref"),
        ("task", inventory["history"]["tasks"], "id", "request_ref"),
        ("publication", inventory["history"]["publications"], "iteration_id", "ref"),
    ):
        for row in rows:
            conn.execute(
                "INSERT INTO migration_legacy_records VALUES(?,?,?,?,?)",
                (
                    import_id,
                    kind,
                    row[identity_key],
                    row[reference_key],
                    row.get("status"),
                ),
            )
    for task in inventory["history"]["tasks"]:
        if task["status"] not in {"pending", "cancelled"} or (
            task["kind"] != "investigation" and task["status"] != "cancelled"
        ):
            continue
        request = json.loads(inventory["records"][task["request_ref"]]["text"])
        identity = task["id"]
        if conn.execute("SELECT 1 FROM tasks WHERE id=?", (identity,)).fetchone():
            raise ConflictError(
                "Translated execution collides with an unclaimed legacy task"
            )
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identity):
            raise ValueError("Legacy human task has an unsupported identity")
        priority = request.get("priority")
        scheduling = (
            inventory["records"]
            .get(f"tasks/{identity}/scheduling.json", {})
            .get("text")
        )
        if scheduling is not None:
            priority = json.loads(scheduling)["priority"]
        cancelled = int(task["status"] == "cancelled")
        if type(priority) is not int or request.get("id") != identity:
            raise ValueError("Legacy human task requires its exact ID and priority")
        specification = _json(
            {
                "legacy_request": request,
                "source_ref": task["request_ref"],
                "import_id": import_id,
            }
        )
        conn.execute("UPDATE control SET pool_version=pool_version+1")
        pool = conn.execute("SELECT pool_version FROM control").fetchone()[0]
        conn.execute(
            "INSERT INTO tasks(id,revision,specification_json,source,priority,dependencies_json,cancelled) VALUES(?,1,?,'human',?,'[]',?)",
            (identity, specification, priority, cancelled),
        )
        conn.execute(
            "INSERT INTO task_revisions VALUES(?,1,?,'human',?,'[]',?,?,'migration',?)",
            (identity, specification, priority, cancelled, pool, import_id),
        )


def prepare_import(
    root, inventory, translation, config, fence, deployment, *, import_id
):
    """Build a staged database; leave the legacy format and records untouched."""
    from .legacy_inventory import verify_inventory
    from .migration_records import build_translation, install_translation

    root = Path(root).resolve()
    import_id = _id(import_id)
    _validate_inputs(inventory, config, fence, deployment)
    if str(root) != inventory["source_root"]:
        raise ConflictError("Inventory belongs to a different Study root")
    request = dict(
        inventory=inventory,
        translation=translation,
        config=config,
        fence=fence,
        deployment=deployment,
    )
    folder = root / ".runtime-imports" / import_id
    manifest_path = folder / "import.json"
    with _source_locks(root):
        if manifest_path.exists():
            manifest = _read(manifest_path)
            if _read(root / manifest["request_ref"]) != request:
                raise ConflictError("Import ID already has a different request")
            return manifest
        require_legacy(root)
        verify_inventory(root, inventory)
        intent_path = folder / "intent.json"
        if folder.is_symlink() or folder.parent.is_symlink():
            raise ValueError("Import staging must not traverse symlinks")
        folder.mkdir(parents=True, exist_ok=True)
        _sync_directory(folder.parent)
        _sync_directory(root)
        if intent_path.exists():
            refs = _read(intent_path)
            if _read(root / refs["request_ref"]) != request:
                raise ConflictError("Staged import ID has a different request")
            # A staged database is a recoverable derivative, never live authority.
            # Rebuild only these known files after interruption before publication.
            for suffix in ("", "-wal", "-shm", "-journal"):
                (folder / (DATABASE + suffix)).unlink(missing_ok=True)
        else:
            if any(folder.iterdir()):
                raise ConflictError("Unrecognized nonempty import staging directory")
            refs = {
                name + "_ref": publish_artifact(root, value)
                for name, value in (
                    ("request", request),
                    ("snapshot", inventory),
                    ("translation", translation),
                    ("fence", fence),
                    ("deployment", deployment),
                )
            }
            publish_json(intent_path, refs)
        prepared = build_translation(root, inventory, translation, config, import_id)
        legacy_context_ref = publish_artifact(
            root,
            {
                "version": 1,
                "origin": "legacy_v3",
                "import_id": import_id,
                "verification": "original_legacy_claims",
                "research": inventory["records"]["research.md"]["text"],
                "publications": inventory["history"]["publications"],
            },
        )
        legacy_context_identity = _identity(root, legacy_context_ref)
        limits = dict(inventory["limits"])
        # An outer legacy supervisor can carry a stricter absolute admission
        # deadline than study.json. A declared mapping may only tighten it.
        outer_deadline = config.get("migration_admission_deadline")
        if outer_deadline is not None:
            if type(outer_deadline) not in (int, float) or not math.isfinite(
                outer_deadline
            ):
                raise ValueError("Mapped admission deadline must be finite")
            original_deadline = limits["admission_deadline"]
            limits["admission_deadline"] = (
                min(original_deadline, outer_deadline)
                if original_deadline is not None
                else outer_deadline
            )
        store = StateStore.create(folder, config=config, **limits)
        now = time.time()
        stop = inventory["native_state"].get("stop_reason")
        if stop is None and inventory["native_state"].get("status") == "stopped":
            stop = "Legacy Study stopped without a recorded reason"
        for stop_path in ("STOP", "control/STOP"):
            stop_file = inventory["records"].get(stop_path, {}).get("text")
            if stop is None and stop_file is not None:
                stop = stop_file or f"Legacy {stop_path} file"
        with store._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE control SET admission_mode='inspect_only'")
            install_translation(conn, prepared)
            counters = inventory["counters"]
            conn.execute(
                "UPDATE control SET calls=?,iterations=?,elapsed_seconds=?,stop_reason=?,admission_mode='inspect_only',anchor=?",
                (
                    counters["calls"],
                    counters["iterations"],
                    counters["elapsed_seconds"],
                    stop,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO migration_imports(id,snapshot_ref,translation_ref,fence_ref,deployment_ref,source_identity_json,original_counters_json,blockers_json,started_at,context_ref,context_identity_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    import_id,
                    refs["snapshot_ref"],
                    refs["translation_ref"],
                    refs["fence_ref"],
                    refs["deployment_ref"],
                    _json(inventory["source_identity"]),
                    _json(counters),
                    _json(prepared["blockers"]),
                    now,
                    legacy_context_ref,
                    _json(legacy_context_identity),
                ),
            )
            _history(conn, inventory, import_id)
            conn.commit()
        _checkpoint(store.path)
        verify_inventory(root, inventory)
        manifest = dict(
            version=1,
            import_id=import_id,
            study_id=store.snapshot()["study_id"],
            source_root=str(root),
            database=str(store.path.relative_to(root)),
            schema_version=SCHEMA_VERSION,
            **refs,
        )
        publish_json(manifest_path, manifest)
        return manifest


def commit_import(root, import_id, *, _after_step=None):
    """Resume the same fail-closed handoff across every marker/rename boundary."""
    from .legacy_inventory import verify_inventory

    root = Path(root).resolve()
    import_id = _id(import_id)
    manifest = _read(root / ".runtime-imports" / import_id / "import.json")
    if manifest.get("import_id") != import_id or manifest.get("source_root") != str(
        root
    ):
        raise ConflictError("Import manifest belongs to another source")
    expected_stage = f".runtime-imports/{import_id}/{DATABASE}"
    if (
        manifest.get("database") != expected_stage
        or manifest.get("schema_version") != SCHEMA_VERSION
    ):
        raise ConflictError("Import manifest has an unsupported database binding")
    marker = dict(
        version=1,
        format="research-loop-sqlite",
        state="preparing",
        import_id=import_id,
        study_id=manifest["study_id"],
        database=DATABASE,
        stage=expected_stage,
        schema_version=SCHEMA_VERSION,
    )

    def step(name):
        if _after_step is not None:
            _after_step(name)

    with _source_locks(root) as lock_fd:
        current = (
            _read(root / "runtime.json") if (root / "runtime.json").exists() else None
        )
        if current is not None and {
            k: v for k, v in current.items() if k != "state"
        } != {k: v for k, v in marker.items() if k != "state"}:
            raise ConflictError("Another handoff owns the Study format")
        if current is not None and current.get("state") == "committed":
            StateStore(root)
            return {"import_id": import_id, "state": "committed", "replayed": True}
        if current is not None and current.get("state") != "preparing":
            raise ConflictError("Unknown handoff state")
        if current is None:
            require_legacy(root)
        verify_inventory(
            root,
            _read(root / manifest["snapshot_ref"]),
            allow_runtime_handoff=current is not None,
        )
        stage, destination = root / expected_stage, root / DATABASE
        if not destination.exists() and any(
            os.path.lexists(str(destination) + suffix)
            for suffix in ("-journal", "-wal", "-shm")
        ):
            raise ConflictError(
                "Existing destination SQLite sidecars require explicit recovery"
            )
        path = destination if destination.exists() else stage
        if path.is_symlink() or not path.is_file():
            raise ConflictError("Import database is missing or unsafe")
        with sqlite3.connect(path) as conn:
            row = conn.execute("SELECT study_id,admission_mode FROM control").fetchone()
            entry = conn.execute(
                "SELECT snapshot_ref FROM migration_imports WHERE id=?", (import_id,)
            ).fetchone()
            if row != (manifest["study_id"], "inspect_only") or entry != (
                manifest["snapshot_ref"],
            ):
                raise ConflictError("Staged database differs from the import manifest")
            if path == stage:
                now = time.time()
                conn.execute(
                    "UPDATE migration_imports SET committed_at=? WHERE id=?",
                    (now, import_id),
                )
                conn.execute("UPDATE control SET anchor=?", (now,))
                conn.commit()
        _checkpoint(path)
        if current is None:
            _marker(root, marker)
        step("preparing")
        if path == stage:
            os.replace(stage, destination)
            _sync_directory(stage.parent)
            _sync_directory(root)
        step("database")
        _marker(root, {**marker, "state": "committed"})
        step("committed")
        # No release/reacquire gap between old authority and the first new owner.
        with StateStore(root).coordinator(_lock_fd=lock_fd) as owner:
            epoch = owner.owner_epoch
        return {
            "import_id": import_id,
            "state": "committed",
            "replayed": False,
            "admission_mode": "inspect_only",
            "owner_epoch": epoch,
            "callbacks_invoked": 0,
            "submissions": 0,
        }


def resolve_import(owner, import_id, evidence):
    """Record an operator's specific resolution; never infer or waive effects."""
    _id(import_id)
    if (
        not isinstance(evidence, dict)
        or set(evidence) != {"import_id", "expected_revision", "resolutions"}
        or evidence["import_id"] != import_id
    ):
        raise ValueError("Resolution evidence must name this import and revision")
    revision, resolutions = evidence["expected_revision"], evidence["resolutions"]
    if (
        type(revision) is not int
        or revision < 1
        or not isinstance(resolutions, list)
        or not resolutions
    ):
        raise ValueError("Expected a positive revision and explicit resolutions")
    selected = set()
    for item in resolutions:
        if not isinstance(item, dict) or set(item) != {
            "blocker",
            "reason",
            "evidence_refs",
        }:
            raise ValueError(
                "Each resolution requires blocker, reason and evidence references"
            )
        if any(
            not isinstance(item[key], str) or not item[key].strip()
            for key in ("blocker", "reason")
        ):
            raise ValueError("Resolution blocker and reason must be nonblank")
        _references(item["evidence_refs"], "Resolution")
        if item["blocker"] in selected:
            raise ValueError("A blocker may be resolved only once in a request")
        # These omissions require a new complete translation, not a prose waiver.
        if item["blocker"].startswith("untranslated_execution:"):
            raise InvalidTransition(
                "Untranslated executions require operational mapping"
            )
        selected.add(item["blocker"])
    intent = _json(evidence)
    reference = publish_artifact(owner.store.root, evidence)
    with owner._transaction() as (conn, _):
        prior = conn.execute(
            "SELECT evidence_ref,intent_json FROM migration_resolutions WHERE import_id=? AND revision=?",
            (import_id, revision),
        ).fetchone()
        if prior is not None:
            if prior["intent_json"] != intent:
                raise ConflictError(
                    "Resolution revision already records different evidence"
                )
            return {
                "import_id": import_id,
                "state": "resolved",
                "replayed": True,
                "evidence_ref": prior["evidence_ref"],
            }
        imported = _decode(
            conn.execute(
                "SELECT * FROM migration_imports WHERE id=?", (import_id,)
            ).fetchone()
        )
        if (
            imported is None
            or imported["committed_at"] is None
            or imported["activation_ref"] is not None
        ):
            raise InvalidTransition("Resolution requires a committed, inactive import")
        if imported["revision"] != revision or not selected <= set(
            imported["blockers"]
        ):
            raise ConflictError("Import revision or selected blockers changed")
        remaining = [item for item in imported["blockers"] if item not in selected]
        conn.execute(
            "INSERT INTO migration_resolutions VALUES(?,?,?,?)",
            (import_id, revision, reference, intent),
        )
        conn.execute(
            "UPDATE migration_imports SET blockers_json=?,revision=revision+1 WHERE id=?",
            (_json(remaining), import_id),
        )
    return {
        "import_id": import_id,
        "state": "resolved",
        "replayed": False,
        "revision": revision + 1,
        "remaining_blockers": remaining,
        "evidence_ref": reference,
    }


def bind_imported_execute(owner, import_id, evidence):
    """Bind an unavailable imported Execute to a trusted original-effect result.

    This is an operator settlement of an original phase, not a model completion,
    operation submission, new evaluator receipt, or permission to repeat Execute.
    """
    if (
        not isinstance(evidence, dict)
        or set(evidence)
        != {
            "import_id",
            "attempt_id",
            "expected_revision",
            "normalized_output",
            "evidence_refs",
        }
        or evidence["import_id"] != import_id
    ):
        raise ValueError(
            "Execute settlement must identify its import, phase and revision"
        )
    _id(import_id)
    _references(evidence["evidence_refs"], "Original effect settlement")
    if not isinstance(evidence["normalized_output"], dict) or (
        type(evidence["expected_revision"]) is not int
        or evidence["expected_revision"] < 1
    ):
        raise ValueError(
            "Execute settlement requires a normalized object and iteration revision"
        )
    intent = _json(evidence)
    with owner.store._connection(readonly=True) as conn:
        attempt = _decode(
            conn.execute(
                "SELECT * FROM attempts WHERE id=?", (evidence["attempt_id"],)
            ).fetchone()
        )
    if (
        attempt is None
        or attempt["origin_kind"] != "legacy"
        or attempt["role"] != "execute"
    ):
        raise InvalidTransition("Only an imported Execute phase can be settled")
    receipt = attempt["receipt"]
    origin = (
        {
            key: receipt[key]
            for key in (
                "import_id",
                "source_root",
                "source_identity",
                "translator",
                "source_refs",
                "role",
            )
            if key in receipt
        }
        if isinstance(receipt, dict)
        else None
    )
    if not isinstance(origin, dict) or origin.get("import_id") != import_id:
        raise ConflictError("Execute phase belongs to another import")
    output = {
        "version": 1,
        "kind": "legacy_phase_binding",
        "normalized_output": evidence["normalized_output"],
        "origin": {**origin, "settlement_evidence_refs": evidence["evidence_refs"]},
    }
    reference = publish_artifact(owner.store.root, output)
    identity = _identity(owner.store.root, reference)
    with owner._transaction() as (conn, _):
        previous = conn.execute(
            "SELECT binding_ref,intent_json FROM migration_phase_bindings WHERE attempt_id=?",
            (attempt["id"],),
        ).fetchone()
        if previous is not None:
            if previous["intent_json"] != intent:
                raise ConflictError("Execute settlement already has different evidence")
            return {
                "state": "bound",
                "replayed": True,
                "binding_ref": previous["binding_ref"],
            }
        current = conn.execute(
            "SELECT status FROM attempts WHERE id=?", (attempt["id"],)
        ).fetchone()
        iteration = conn.execute(
            "SELECT revision,status,phase FROM iterations WHERE id=?",
            (attempt["iteration_id"],),
        ).fetchone()
        imported = conn.execute(
            "SELECT committed_at FROM migration_imports WHERE id=?", (import_id,)
        ).fetchone()
        if (
            imported is None
            or imported[0] is None
            or current[0] != "unavailable"
            or iteration is None
            or tuple(iteration) != (evidence["expected_revision"], "blocked", "review")
        ):
            raise InvalidTransition(
                "Execute settlement no longer matches the blocked imported phase"
            )
        conn.execute(
            "UPDATE attempts SET status='accepted',output_ref=?,output_identity_json=?,receipt_json=? WHERE id=?",
            (
                reference,
                _json(identity),
                _json(
                    {
                        **receipt,
                        "available": True,
                        "settlement_evidence_refs": evidence["evidence_refs"],
                    }
                ),
                attempt["id"],
            ),
        )
        plan_available = conn.execute(
            "SELECT 1 FROM attempts WHERE iteration_id=? AND role='plan' AND status='accepted'",
            (attempt["iteration_id"],),
        ).fetchone()
        cancelled = conn.execute(
            "SELECT t.cancelled FROM tasks t JOIN iterations i ON i.task_id=t.id WHERE i.id=?",
            (attempt["iteration_id"],),
        ).fetchone()[0]
        conn.execute(
            "UPDATE iterations SET revision=revision+1,status=? WHERE id=?",
            (
                "active" if plan_available and not cancelled else "blocked",
                attempt["iteration_id"],
            ),
        )
        conn.execute(
            "INSERT INTO migration_phase_bindings VALUES(?,?,?,?)",
            (attempt["id"], import_id, reference, intent),
        )
    return {"state": "bound", "replayed": False, "binding_ref": reference}


def activate_import(owner, import_id, evidence):
    """Explicitly admit new work after reconciliation; never clear STOP or holds."""
    _id(import_id)
    if not isinstance(evidence, dict) or evidence.get("import_id") != import_id:
        raise ValueError("Activation evidence must name this import")
    for name in ("authorization_evidence_refs", "time_evidence_refs"):
        _references(evidence.get(name), name)
    _references(
        evidence.get("reconciliation_evidence_refs"), "Reconciliation", empty=True
    )
    additional = evidence.get("additional_elapsed_seconds")
    if (
        type(additional) not in (int, float)
        or not math.isfinite(additional)
        or additional < 0
    ):
        raise ValueError(
            "Unknown legacy wall time requires a nonnegative explicit charge"
        )
    revision = evidence.get("expected_revision")
    if type(revision) is not int or revision < 1:
        raise ValueError("Activation requires the import revision")
    reference = publish_artifact(owner.store.root, evidence)
    with owner._transaction() as (conn, _):
        imported = _decode(
            conn.execute(
                "SELECT * FROM migration_imports WHERE id=?", (import_id,)
            ).fetchone()
        )
        if imported is None or imported["committed_at"] is None:
            raise InvalidTransition("Import is not committed")
        if imported["activation_ref"] is not None:
            previous_ref = imported["activation_ref"]
        else:
            previous_ref = None
            if imported["revision"] != revision or imported["blockers"]:
                raise InvalidTransition(
                    "Migration revision changed or admission blockers remain"
                )
            missing = conn.execute(
                "SELECT id FROM operations o WHERE submit_forbidden=1 AND NOT EXISTS "
                "(SELECT 1 FROM operation_events e WHERE e.operation_id=o.id AND e.kind IN ('inspection','settlement','collection')) LIMIT 1"
            ).fetchone()
            if missing is not None:
                raise InvalidTransition(
                    "Imported operations must be reconciled before new admission"
                )
            conn.execute(
                "UPDATE control SET admission_mode='active',elapsed_seconds=elapsed_seconds+?",
                (additional,),
            )
            conn.execute(
                "UPDATE migration_imports SET activation_ref=?,additional_elapsed_seconds=?,revision=revision+1 WHERE id=?",
                (reference, additional, import_id),
            )
    if previous_ref is not None and _read(owner.store.root / previous_ref) != evidence:
        raise ConflictError("Activation already recorded with different evidence")
    return {
        "import_id": import_id,
        "state": "active",
        "replayed": previous_ref is not None,
        "activation_ref": previous_ref or reference,
    }
