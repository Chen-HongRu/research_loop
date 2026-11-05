"""Durable planning triggers, frozen rounds and ordinary budgeted call slots."""

import uuid

from .context import ContextSnapshot, build_context
from .files import publish_artifact
from .publication import Publisher
from .publication_views import build_views, render_research
from .store import ConflictError, InvalidTransition, OwnershipError, _decode, _json
from .updates import CallIdentity, Updater, _identity, _same


class PlanningLedger:
    def __init__(self, coordinator):
        self.owner = coordinator
        self.reader = Updater(coordinator, validators={})

    def trigger(self, producer_key, kind, payload):
        if not isinstance(payload, dict):
            raise ValueError("Planning event payload must be an object")
        _json(payload)
        with self.owner._transaction() as (conn, _):
            return self.owner._planning_event(conn, producer_key, kind, payload)

    @staticmethod
    def _pending(conn):
        watermark = conn.execute(
            "SELECT COALESCE(MAX(event_through),0) FROM planning_rounds"
        ).fetchone()[0]
        return watermark, [
            _decode(row)
            for row in conn.execute(
                "SELECT * FROM planning_events WHERE id>? ORDER BY id",
                (watermark,),
            )
        ]

    def pending(self):
        self.owner._live()
        with self.owner.store._connection(readonly=True) as conn:
            conn.execute("BEGIN")
            return self._pending(conn)[1]

    def get(self, round_id):
        self.owner._live()
        with self.owner.store._connection(readonly=True) as conn:
            row = _decode(
                conn.execute(
                    "SELECT * FROM planning_rounds WHERE id=?", (round_id,)
                ).fetchone()
            )
        if row is None:
            raise ConflictError("Unknown planning round")
        return row

    def calls(self, round_id):
        self.get(round_id)
        with self.owner.store._connection(readonly=True) as conn:
            return {
                row["slot"]: {
                    key: value for key, value in _decode(row).items() if key != "slot"
                }
                for row in conn.execute(
                    "SELECT c.slot,a.* FROM planning_calls c JOIN attempts a ON a.id=c.attempt_id "
                    "WHERE c.round_id=? ORDER BY c.slot",
                    (round_id,),
                )
            }

    def _capture(self):
        with self.owner._mutex:
            self.owner._live()
            with self.owner.store._connection(readonly=True) as conn:
                conn.execute("BEGIN")
                if conn.execute(
                    "SELECT 1 FROM planning_rounds WHERE status='active'"
                ).fetchone():
                    raise InvalidTransition(
                        "A planning round is active; do not redispatch its calls"
                    )
                watermark, events = self._pending(conn)
                if not events:
                    return None
                state = self.owner.snapshot()
                knowledge = Publisher(self.owner).snapshot()
            return (
                dict(
                    round_id=uuid.uuid4().hex,
                    pool={"version": state["pool_version"], "tasks": state["tasks"]},
                    in_flight=state["iterations"],
                    events=events,
                    event_from=watermark + 1,
                    event_through=events[-1]["id"],
                    knowledge_version=knowledge["version"],
                ),
                knowledge,
                state["config"],
            )

    def begin(self):
        captured = self._capture()
        if captured is None:
            return None
        snapshot, knowledge, config = captured
        publications = {
            row["id"]: self.reader._read_record(row["payload_ref"], row["identity"])[
                "normalized_output"
            ]
            for row in knowledge["publications"]
        }
        research = {
            "version": knowledge["version"],
            "research": render_research(knowledge, publications),
            "frontiers": build_views(knowledge, config["global_evaluation"])
            if isinstance(config.get("global_evaluation"), dict)
            else None,
        }
        root = self.owner.store.root
        snapshot_ref = publish_artifact(root, snapshot)
        knowledge_ref = publish_artifact(root, research)
        identity, knowledge_identity = (
            _identity(root, snapshot_ref),
            _identity(root, knowledge_ref),
        )
        with self.owner._transaction() as (conn, control):
            if conn.execute(
                "SELECT 1 FROM planning_rounds WHERE status='active'"
            ).fetchone():
                raise ConflictError("Another planning round was admitted")
            watermark, events = self._pending(conn)
            head = conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM publications"
            ).fetchone()[0]
            if (
                watermark + 1 != snapshot["event_from"]
                or not events
                or events[-1]["id"] != snapshot["event_through"]
                or control["pool_version"] != snapshot["pool"]["version"]
                or head != snapshot["knowledge_version"]
            ):
                raise ConflictError(
                    "Planning inputs advanced during capture; retry capture"
                )
            self.reader._check_identity(snapshot_ref, identity)
            self.reader._check_identity(knowledge_ref, knowledge_identity)
            for row in knowledge["publications"]:
                self.reader._check_identity(row["payload_ref"], row["identity"])
            conn.execute(
                "INSERT INTO planning_rounds(id,owner_epoch,pool_version,event_from,event_through,"
                "snapshot_ref,identity_json,knowledge_ref,knowledge_identity_json,status) VALUES(?,?,?,?,?,?,?,?,?,'active')",
                (
                    snapshot["round_id"],
                    self.owner.owner_epoch,
                    snapshot["pool"]["version"],
                    snapshot["event_from"],
                    snapshot["event_through"],
                    snapshot_ref,
                    _json(identity),
                    knowledge_ref,
                    _json(knowledge_identity),
                ),
            )
            for slot in ("advocate_a", "advocate_b"):
                attempt = self.owner._reserve_attempt(
                    conn,
                    control,
                    "advocate",
                    request_id=f"planning:{snapshot['round_id']}:{slot}",
                )
                conn.execute(
                    "INSERT INTO planning_calls VALUES(?,?,?)",
                    (snapshot["round_id"], slot, attempt["id"]),
                )
            return _decode(
                conn.execute(
                    "SELECT * FROM planning_rounds WHERE id=?", (snapshot["round_id"],)
                ).fetchone()
            )

    def reserve_judge(self, round_id):
        with self.owner._transaction() as (conn, control):
            row = _decode(
                conn.execute(
                    "SELECT * FROM planning_rounds WHERE id=?", (round_id,)
                ).fetchone()
            )
            if row is None or row["status"] != "active":
                raise InvalidTransition("Planning round is not active")
            existing = conn.execute(
                "SELECT a.* FROM attempts a JOIN planning_calls c ON c.attempt_id=a.id "
                "WHERE c.round_id=? AND c.slot='judge'",
                (round_id,),
            ).fetchone()
            if existing:
                return _decode(existing)
            from .dispatch import DispatchLedger

            if not DispatchLedger.can_use_round(conn, round_id, self.owner.owner_epoch):
                raise OwnershipError(
                    "Unresolved prior-owner planning round requires reconciliation"
                )
            statuses = [
                item[0]
                for item in conn.execute(
                    "SELECT a.status FROM planning_calls c JOIN attempts a ON a.id=c.attempt_id "
                    "WHERE c.round_id=? AND c.slot IN ('advocate_a','advocate_b')",
                    (round_id,),
                )
            ]
            if (
                len(statuses) != 2
                or any(status not in {"accepted", "failed"} for status in statuses)
                or "accepted" not in statuses
            ):
                raise InvalidTransition(
                    "Judge requires terminal Advocates and at least one accepted proposal"
                )
            attempt = self.owner._reserve_attempt(
                conn, control, "judge", request_id=f"planning:{round_id}:judge"
            )
            conn.execute(
                "INSERT INTO planning_calls VALUES(?,?,?)",
                (round_id, "judge", attempt["id"]),
            )
            return attempt

    def context(self, call, *, policy=None):
        if not isinstance(call, CallIdentity):
            raise TypeError("Expected CallIdentity")
        record = self.owner._feedback_snapshot(call.to_dict())
        planning = record["planning"]
        if planning is None:
            raise InvalidTransition("Attempt has no planning round")
        inputs = self.reader._planning_inputs(record)
        frozen = inputs["planning"]
        config = record["control"]["config"]
        inputs.update(
            permissions=config.get("permissions", {}),
            task=None,
            budget={
                key: record["control"][key]
                for key in (
                    "max_iterations",
                    "max_calls",
                    "max_seconds",
                    "admission_deadline",
                )
            },
        )
        for key in ("goal", "global_evaluation"):
            if key in config:
                inputs[key] = config[key]
        protected = list(inputs)
        knowledge = self.reader._read_record(
            planning["knowledge_ref"], planning["knowledge_identity"]
        )
        if knowledge["version"] != frozen["knowledge_version"]:
            raise ConflictError(
                "Planning knowledge version differs from captured round"
            )
        inputs.update(research=knowledge["research"], frontiers=knowledge["frontiers"])
        references = {
            "planning": planning["snapshot_ref"],
            "research": planning["knowledge_ref"],
            "frontiers": planning["knowledge_ref"],
        }
        revisions = {
            "planning": {
                "round_id": frozen["round_id"],
                "pool_version": frozen["pool"]["version"],
                "event_through": frozen["event_through"],
            },
            "research": frozen["knowledge_version"],
            "frontiers": frozen["knowledge_version"],
        }
        for advocate in planning.get("advocates", []):
            references[advocate["slot"]] = (
                advocate["output_ref"]
                if advocate["status"] == "accepted"
                else f"control:attempt:{advocate['id']}"
            )
            revisions[advocate["slot"]] = {
                "attempt_id": advocate["id"],
                "status": advocate["status"],
            }
        sources = [
            dict(
                id=key,
                key=key,
                ref=references.get(key, f"control:{key}"),
                revision=revisions.get(key, 1),
                reason="Frozen planning input",
                allow_feedback=True,
            )
            for key in inputs
        ]
        package = ContextSnapshot.capture(
            study_id=call.study_id,
            revisions=dict(
                study=1,
                task=None,
                iteration=None,
                knowledge=frozen["knowledge_version"],
            ),
            inputs=inputs,
            sources=sources,
            protected_keys=protected,
            allowed_evidence=list(
                dict.fromkeys(
                    ref for ref in references.values() if ref.startswith("artifacts/")
                )
            ),
        )
        return build_context(
            package,
            call.role,
            None,
            "task-planning-v1",
            config.get("context_budget", {}),
            policy=policy,
        )

    def finish(self, round_id, status, *, error=None, receipt=None):
        from .dispatch import DispatchLedger, RuntimeEvents

        if status not in {"applied", "failed"}:
            raise ValueError("Invalid planning terminal status")
        if status == "failed" and (not isinstance(error, str) or not error.strip()):
            raise ValueError("Failed planning requires an explicit reason")
        with self.owner._transaction() as (conn, _):
            row = _decode(
                conn.execute(
                    "SELECT * FROM planning_rounds WHERE id=?", (round_id,)
                ).fetchone()
            )
            if row is None:
                raise ConflictError("Unknown planning round")
            if row["status"] != "active":
                if (
                    row["status"] == status
                    and row["error"] == error
                    and _same(row["receipt"], receipt)
                ):
                    return row
                raise ConflictError("Planning round already settled differently")
            if status == "applied":
                batch = conn.execute(
                    "SELECT b.receipt_json FROM task_batches b JOIN planning_calls c ON c.attempt_id=b.attempt_id "
                    "WHERE c.round_id=? AND c.slot='judge'",
                    (round_id,),
                ).fetchone()
                if batch is None or batch["receipt_json"] != _json(receipt):
                    raise InvalidTransition(
                        "Applied planning requires its committed Judge batch"
                    )
            elif not DispatchLedger.can_use_round(
                conn, round_id, self.owner.owner_epoch
            ):
                raise OwnershipError(
                    "Unresolved prior-owner round cannot be silently discarded"
                )
            elif conn.execute(
                "SELECT 1 FROM task_batches b JOIN planning_calls c ON c.attempt_id=b.attempt_id WHERE c.round_id=?",
                (round_id,),
            ).fetchone():
                raise InvalidTransition("Committed Judge batch must settle as applied")
            elif conn.execute(
                "SELECT 1 FROM planning_calls c JOIN attempts a ON a.id=c.attempt_id "
                "WHERE c.round_id=? AND a.status='active'",
                (round_id,),
            ).fetchone():
                raise InvalidTransition(
                    "Active planning calls must be resolved before failure settlement"
                )
            conn.execute(
                "UPDATE planning_rounds SET status=?,error=?,receipt_json=? WHERE id=?",
                (status, error, _json(receipt), round_id),
            )
            RuntimeEvents.emit(
                conn,
                f"planning-terminal:{round_id}",
                "planning_terminal",
                round_id,
                payload={"status": status, "error": error},
            )
            return _decode(
                conn.execute(
                    "SELECT * FROM planning_rounds WHERE id=?", (round_id,)
                ).fetchone()
            )
