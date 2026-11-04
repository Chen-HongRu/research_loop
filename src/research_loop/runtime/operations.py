"""Fenced local authority for finite external operations and their trusted facts.

No backend call occurs here. A persisted intent is never permission to resubmit.
Proof references are trusted controller assertions, not inferred authentication.
"""

import math
import uuid

from .operation_contract import (
    BINDING_FIELDS,
    ExecutionAdmission,
    integer,
    text,
    validate_execution,
    validate_receipt,
)
from .store import BudgetExceeded, ConflictError, InvalidTransition, _decode, _json
from .updates import CallIdentity, Updater, _identity, _same


class Operations:
    def __init__(self, coordinator):
        self.owner = coordinator
        self.reader = Updater(coordinator, validators={})

    def prepare_execution(self, call, normalized_output, plan_output, config):
        """Capture compact authority, never backend parameters, before acceptance."""
        if "operations" not in normalized_output:
            return None
        requests = normalized_output["operations"]
        if requests == []:
            return None
        if not isinstance(call, CallIdentity) or call.role != "execute":
            raise InvalidTransition("Only Execute can admit operation requests")
        record = self.owner._feedback_snapshot(call.to_dict())
        plan = record["plan"]
        if plan is None:
            raise InvalidTransition("Operations require an accepted Plan")
        original = self.reader._read_record(
            plan["output_ref"], plan["output_identity"]
        )["normalized_output"]
        if not _same(original, plan_output) or not _same(
            record["control"]["config"], config
        ):
            raise ConflictError(
                "Operation authority differs from immutable Plan/config"
            )
        validated = validate_execution(requests, plan_output, config)
        entries = []
        for item in validated:
            request = item["request"]
            entries.append(
                dict(
                    logical_id=request["id"],
                    backend=request["backend"],
                    authorization_id=request["authorization_id"],
                    resources=request["resources"],
                    cost_bounds=request["cost_bounds"],
                    timeout_seconds=request["timeout_seconds"],
                    backend_binding=item["backend_binding"],
                    authorization=item["authorization"],
                )
            )
        return ExecutionAdmission.capture(
            dict(
                call=call.to_dict(),
                plan_attempt_id=plan["id"],
                plan_ref=plan["output_ref"],
                plan_identity=plan["output_identity"],
                pools=config["resource_pools"],
                entries=entries,
            )
        )

    @staticmethod
    def seal_execution(conn, call, output_ref, identity, admission, now):
        if admission is None:
            return []
        if not isinstance(admission, ExecutionAdmission):
            raise TypeError("Expected a validated ExecutionAdmission")
        captured = admission.to_dict()
        if not _same(captured["call"], call.to_dict()):
            raise ConflictError("Operation admission belongs to a different Execute")
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ValueError("Acceptance time must be a finite UTC timestamp")
        plan = _decode(
            conn.execute(
                "SELECT * FROM attempts WHERE id=?", (captured["plan_attempt_id"],)
            ).fetchone()
        )
        execute = conn.execute(
            "SELECT role,iteration_id FROM attempts WHERE id=?", (call.attempt_id,)
        ).fetchone()
        if (
            plan is None
            or plan["role"] != "plan"
            or plan["status"] != "accepted"
            or plan["iteration_id"] != call.iteration_id
            or plan["output_ref"] != captured["plan_ref"]
            or not _same(plan["output_identity"], captured["plan_identity"])
            or execute is None
            or execute["role"] != "execute"
            or execute["iteration_id"] != call.iteration_id
        ):
            raise ConflictError("Operation group Plan/Execute binding changed")
        existing = conn.execute(
            "SELECT * FROM operation_groups WHERE execute_attempt_id=?",
            (call.attempt_id,),
        ).fetchone()
        if existing:
            return [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM operations WHERE execute_attempt_id=? ORDER BY entry_index",
                    (call.attempt_id,),
                )
            ]
        conn.execute(
            "INSERT INTO operation_groups(execute_attempt_id,plan_attempt_id,iteration_id) VALUES(?,?,?)",
            (call.attempt_id, captured["plan_attempt_id"], call.iteration_id),
        )
        for pool, capacity in captured["pools"].items():
            conn.execute(
                "INSERT OR IGNORE INTO operation_pool_balances(pool,capacity,held) VALUES(?,?,?)",
                (pool, str(capacity), "0"),
            )
        result = []
        for index, entry in enumerate(captured["entries"]):
            grant, auth = entry["authorization"], entry["authorization_id"]
            conn.execute(
                "INSERT OR IGNORE INTO operation_authorities(id,backends_json,max_operations,deadline) VALUES(?,?,?,?)",
                (
                    auth,
                    _json(grant["backends"]),
                    str(grant["max_operations"])
                    if grant["max_operations"] is not None
                    else None,
                    grant["deadline"],
                ),
            )
            for pool, limit in grant["resource_limits"].items():
                conn.execute(
                    "INSERT OR IGNORE INTO operation_scope_resources VALUES(?,?,?,?)",
                    (auth, pool, str(limit), "0"),
                )
            for unit, limit in grant["cost_limits"].items():
                conn.execute(
                    "INSERT OR IGNORE INTO operation_scope_costs VALUES(?,?,?,?,?)",
                    (auth, unit, str(limit), "0", "0"),
                )
            deadline = now + entry["timeout_seconds"]
            if grant["deadline"] is not None:
                deadline = min(deadline, grant["deadline"])
            if not math.isfinite(deadline):
                raise ValueError("Operation deadline must be finite")
            operation_id = f"{call.attempt_id}:{entry['logical_id']}"
            conn.execute(
                "INSERT INTO operations(id,logical_id,execute_attempt_id,entry_index,request_ref,request_identity_json,authorization_id,backend,backend_version,environment_json,resources_json,cost_bounds_json,deadline) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    operation_id,
                    entry["logical_id"],
                    call.attempt_id,
                    index,
                    output_ref,
                    _json(identity),
                    auth,
                    entry["backend"],
                    entry["backend_binding"]["version"],
                    _json(entry["backend_binding"]["environment"]),
                    _json(entry["resources"]),
                    _json(entry["cost_bounds"]),
                    deadline,
                ),
            )
            result.append(operation_id)
        return result

    @staticmethod
    def check_review_ready(conn, iteration_id):
        group = conn.execute(
            "SELECT * FROM operation_groups WHERE iteration_id=?", (iteration_id,)
        ).fetchone()
        if group is None:
            return
        unresolved = conn.execute(
            "SELECT 1 FROM operations WHERE execute_attempt_id=? AND (state!='terminal' OR collection_state NOT IN ('collected','unavailable') OR inconsistency IS NOT NULL) LIMIT 1",
            (group["execute_attempt_id"],),
        ).fetchone()
        if unresolved is not None or not group["feedback_sealed"]:
            raise InvalidTransition(
                "Operation knowledge or feedback intake is not sealed"
            )

    @staticmethod
    def imported(op):
        origin = op.get("origin")
        return bool(
            op.get("submit_forbidden")
            and isinstance(origin, dict)
            and isinstance(origin.get("original_request_identity"), str)
            and origin["original_request_identity"].strip()
        )

    @staticmethod
    def _get(conn, operation_id):
        row = _decode(
            conn.execute(
                "SELECT o.*,g.plan_attempt_id,g.iteration_id,g.feedback_sealed FROM operations o JOIN operation_groups g ON g.execute_attempt_id=o.execute_attempt_id WHERE o.id=?",
                (operation_id,),
            ).fetchone()
        )
        if row is None:
            raise ConflictError("Unknown operation")
        row["costs_final"] = (
            not row["unexpected_job_ids"]
            and not row["held_costs"]
            and row["cost_bounds"].keys() <= row["actual_costs"].keys()
        )
        for field in (
            "physical_released",
            "cancel_acknowledged",
            "feedback_sealed",
            "external_open",
            "submit_forbidden",
        ):
            row[field] = bool(row[field])
        return row

    def get(self, operation_id):
        self.owner._live()
        with self.owner.store._connection(readonly=True) as conn:
            return self._get(conn, operation_id)

    def list(self, *, execute_attempt_id=None, iteration_id=None):
        self.owner._live()
        with self.owner.store._connection(readonly=True) as conn:
            conn.execute("BEGIN")
            ids = conn.execute(
                "SELECT o.id FROM operations o JOIN operation_groups g ON g.execute_attempt_id=o.execute_attempt_id WHERE (? IS NULL OR o.execute_attempt_id=?) AND (? IS NULL OR g.iteration_id=?) ORDER BY o.rowid",
                (execute_attempt_id, execute_attempt_id, iteration_id, iteration_id),
            ).fetchall()
            return [self._get(conn, row[0]) for row in ids]

    def request(self, operation_id):
        op = self.get(operation_id)
        proposal = self.reader._read_record(op["request_ref"], op["request_identity"])
        request = proposal["normalized_output"]["operations"][op["entry_index"]]
        if request["id"] != op["logical_id"]:
            raise ConflictError("Immutable operation request identity changed")
        return request

    @staticmethod
    def _binding(op):
        return dict(
            operation_id=op["id"],
            request_id=op["logical_id"],
            plan_attempt_id=op["plan_attempt_id"],
            execute_attempt_id=op["execute_attempt_id"],
            backend=op["backend"],
            backend_version=op["backend_version"],
            environment=op["environment"],
            submission_token=op["submission_token"],
            owner_epoch=op["submit_owner_epoch"],
        )

    def envelope(self, operation_id, data):
        return {**self._binding(self.get(operation_id)), "data": data}

    @staticmethod
    def _check_binding(op, payload):
        if not _same(
            {key: payload[key] for key in BINDING_FIELDS}, Operations._binding(op)
        ):
            raise ConflictError(
                "Receipt differs from frozen operation/backend/environment binding"
            )

    @staticmethod
    def _balance(conn, table, keys, column, delta):
        where = " AND ".join(key + "=?" for key in keys)
        values = tuple(keys.values())
        row = conn.execute(
            f"SELECT {column} FROM {table} WHERE {where}", values
        ).fetchone()
        if row is None:
            raise ConflictError("Missing operation authority balance")
        total = int(row[0]) + delta
        if total < 0:
            raise ConflictError("Operation hold accounting underflow")
        conn.execute(
            f"UPDATE {table} SET {column}=? WHERE {where}", (str(total), *values)
        )

    def reserve_submit(self, operation_id, expected_revision):
        with self.owner._transaction() as (conn, control):
            return self._reserve_submit(conn, control, operation_id, expected_revision)

    def _reserve_submit(
        self, conn, control, operation_id, expected_revision, *, submission_token=None
    ):
        """Reserve inside a caller's transaction; dispatch can commit atomically."""
        integer(expected_revision, "expected_revision", minimum=1)
        op = self._get(conn, operation_id)
        if op["submit_forbidden"]:
            raise InvalidTransition(
                "Imported operations can never prepare or submit again"
            )
        if op["submission_token"] is not None:
            raise InvalidTransition(
                "Operation already has a submit intent; inspect original identity"
            )
        if op["revision"] != expected_revision:
            raise ConflictError("Operation revision mismatch")
        if op["state"] != "prepared" or op["inconsistency"]:
            raise InvalidTransition("Operation is not prepared for first submission")
        self.owner._admit(control, deadline=True)
        if control["anchor"] >= op["deadline"]:
            raise BudgetExceeded("Frozen operation deadline reached")
        authority = conn.execute(
            "SELECT * FROM operation_authorities WHERE id=?",
            (op["authorization_id"],),
        ).fetchone()
        if authority["blocked_reason"] or (
            authority["max_operations"] is not None
            and authority["admitted_operations"] >= int(authority["max_operations"])
        ):
            raise BudgetExceeded("Operation authorization is blocked or exhausted")
        for pool, amount in op["resources"].items():
            global_row = conn.execute(
                "SELECT * FROM operation_pool_balances WHERE pool=?", (pool,)
            ).fetchone()
            scoped = conn.execute(
                "SELECT * FROM operation_scope_resources WHERE authorization_id=? AND pool=?",
                (op["authorization_id"], pool),
            ).fetchone()
            if (
                global_row["blocked_reason"]
                or int(global_row["held"]) + amount > int(global_row["capacity"])
                or int(scoped["held"]) + amount > int(scoped["capacity"])
            ):
                raise BudgetExceeded(
                    "Physical resource capacity is held by other operations"
                )
        for unit, amount in op["cost_bounds"].items():
            row = conn.execute(
                "SELECT * FROM operation_scope_costs WHERE authorization_id=? AND unit=?",
                (op["authorization_id"], unit),
            ).fetchone()
            if int(row["settled"]) + int(row["held"]) + amount > int(row["cost_limit"]):
                raise BudgetExceeded("Cost authorization is held or exhausted")
        self._add_resources(conn, op, op["resources"])
        for unit, amount in op["cost_bounds"].items():
            self._balance(
                conn,
                "operation_scope_costs",
                dict(authorization_id=op["authorization_id"], unit=unit),
                "held",
                amount,
            )
        token = submission_token if submission_token is not None else uuid.uuid4().hex
        text(token, "submission_token")
        if conn.execute(
            "SELECT 1 FROM operations WHERE submission_token=?", (token,)
        ).fetchone():
            raise ConflictError("Submission token belongs to an existing operation")
        conn.execute(
            "UPDATE operation_authorities SET admitted_operations=admitted_operations+1 WHERE id=?",
            (op["authorization_id"],),
        )
        conn.execute(
            "UPDATE operations SET state='submitting',revision=revision+1,submission_token=?,submit_owner_epoch=?,held_resources_json=?,held_costs_json=?,external_open=1 WHERE id=?",
            (
                token,
                self.owner.owner_epoch,
                _json(op["resources"]),
                _json(op["cost_bounds"]),
                operation_id,
            ),
        )
        current = self._get(conn, operation_id)
        return {
            **self._binding(current),
            "deadline": current["deadline"],
            "operation_revision": current["revision"],
            "request_ref": current["request_ref"],
        }

    @classmethod
    def _add_resources(cls, conn, op, resources):
        for pool, amount in resources.items():
            cls._balance(
                conn, "operation_pool_balances", dict(pool=pool), "held", amount
            )
            cls._balance(
                conn,
                "operation_scope_resources",
                dict(authorization_id=op["authorization_id"], pool=pool),
                "held",
                amount,
            )

    @classmethod
    def _release_resources(cls, conn, op):
        if op["unexpected_job_ids"]:
            # A release of the primary job cannot close aggregate unknown jobs.
            op.update(physical_released=False, external_open=True)
            return
        cls._add_resources(
            conn, op, {key: -amount for key, amount in op["held_resources"].items()}
        )
        op["held_resources"] = {}
        op["physical_released"] = True

    @staticmethod
    def _block(conn, op, reason):
        conn.execute(
            "UPDATE operation_authorities SET blocked_reason=COALESCE(blocked_reason,?) WHERE id=?",
            (reason, op["authorization_id"]),
        )

    @classmethod
    def _quarantine(cls, conn, op, reason, observed_job_id, *, aggregate_unknown=False):
        """Retain contradictory facts; held quantities become conservative minima."""
        if aggregate_unknown or observed_job_id != op["job_id"]:
            if observed_job_id not in op["unexpected_job_ids"]:
                op["unexpected_job_ids"].append(observed_job_id)
            for pool in op["resources"]:
                conn.execute(
                    "UPDATE operation_pool_balances SET blocked_reason=COALESCE(blocked_reason,?) WHERE pool=?",
                    ("conflicting_job_identity", pool),
                )
        missing = {
            pool: max(0, amount - op["held_resources"].get(pool, 0))
            for pool, amount in op["resources"].items()
        }
        cls._add_resources(conn, op, missing)
        for pool, amount in op["resources"].items():
            op["held_resources"][pool] = max(amount, op["held_resources"].get(pool, 0))
        for unit, bound in op["cost_bounds"].items():
            current = op["held_costs"].get(unit, 0)
            if bound > current:
                cls._balance(
                    conn,
                    "operation_scope_costs",
                    dict(authorization_id=op["authorization_id"], unit=unit),
                    "held",
                    bound - current,
                )
            op["held_costs"][unit] = max(bound, current)
        op.update(
            physical_released=False,
            external_open=True,
            inconsistency=op["inconsistency"] or reason,
        )
        cls._block(conn, op, reason)

    @classmethod
    def _job(cls, conn, op, job_id):
        if job_id is None:
            return
        if op["job_id"] is not None and op["job_id"] != job_id:
            cls._quarantine(conn, op, "conflicting_job_identity", job_id)
        elif op["job_id"] is None:
            op["job_id"] = job_id
            if op["no_submit_proof"] is not None:
                cls._quarantine(
                    conn, op, "positive_job_after_proven_not_submitted", job_id
                )
        else:
            return

    @classmethod
    def _not_submitted(cls, conn, op, proof):
        if proof is None:
            raise InvalidTransition("No-submission outcome requires positive proof")
        if cls.imported(op) and proof["basis"] == "no_submission_intent":
            raise InvalidTransition(
                "Imported absence requires original-sender or backend fencing proof"
            )
        if (
            proof["basis"] == "no_submission_intent"
            and op["submission_token"] is not None
        ):
            raise InvalidTransition(
                "A durable submit intent cannot be erased by owner epoch or missing job ID"
            )
        if op["job_id"] is not None:
            raise InvalidTransition(
                "Known job existence cannot be erased by a no-submit assertion"
            )
        cls._release_resources(conn, op)
        op.update(
            state="terminal",
            terminal_outcome="not_submitted",
            no_submit_proof=proof,
            external_open=False,
        )
        if proof["basis"] == "no_submission_intent" and op["inconsistency"] is None:
            if any(op["actual_costs"].values()):
                # Preserve actual charged facts even when they contradict the
                # free-preparation contract; absence of intent cannot erase them.
                op["inconsistency"] = "cost_without_submission_intent"
                cls._block(conn, op, op["inconsistency"])
            else:
                cls._settle(conn, op, {unit: 0 for unit in op["cost_bounds"]})

    @classmethod
    def _settle(cls, conn, op, final_costs):
        for unit, actual in final_costs.items():
            keys = dict(authorization_id=op["authorization_id"], unit=unit)
            row = conn.execute(
                "SELECT * FROM operation_scope_costs WHERE authorization_id=? AND unit=?",
                tuple(keys.values()),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO operation_scope_costs VALUES(?,?,?,?,?)",
                    (op["authorization_id"], unit, "0", "0", "0"),
                )
            hold = 0 if op["unexpected_job_ids"] else op["held_costs"].pop(unit, 0)
            cls._balance(conn, "operation_scope_costs", keys, "held", -hold)
            cls._balance(
                conn,
                "operation_scope_costs",
                keys,
                "settled",
                actual - op["actual_costs"].get(unit, 0),
            )
            op["actual_costs"][unit] = actual
            row = conn.execute(
                "SELECT * FROM operation_scope_costs WHERE authorization_id=? AND unit=?",
                tuple(keys.values()),
            ).fetchone()
            if actual > op["cost_bounds"].get(unit, 0) or int(row["settled"]) > int(
                row["cost_limit"]
            ):
                cls._block(conn, op, "actual_cost_exceeded_bound_or_authority")

    def _apply(
        self, conn, op, kind, data, event_key, payload_ref, payload_identity, intake
    ):
        if op["submit_forbidden"] and kind in {"prepare", "submission"}:
            raise InvalidTransition(
                "Imported operations cannot accept preparation or submission"
            )
        if kind == "prepare":
            if op["state"] != "pending" or op["submission_token"] is not None:
                raise InvalidTransition(
                    "Preparation cannot change after its first result"
                )
            if data["ready"]:
                op["state"] = "prepared"
            else:
                self._not_submitted(
                    conn, op, dict(basis="no_submission_intent", reference=payload_ref)
                )
        elif kind == "submission":
            outcome = data["outcome"]
            if op["submission_token"] is None and outcome not in {
                "not_submitted",
                "rejected",
            }:
                if data["job_id"] is None:
                    raise InvalidTransition(
                        "A submission result requires its durable submit intent"
                    )
            self._job(conn, op, data["job_id"])
            if (
                op["submission_token"] is None
                and data["job_id"] is not None
                and not self.imported(op)
            ):
                self._quarantine(
                    conn,
                    op,
                    "job_observed_without_submit_intent",
                    data["job_id"],
                    aggregate_unknown=True,
                )
            if outcome == "accepted" and op["state"] == "terminal":
                self._quarantine(
                    conn, op, "positive_submission_after_terminal", data["job_id"]
                )
            if op["inconsistency"] is None:
                if outcome in {"not_submitted", "rejected"}:
                    self._not_submitted(conn, op, data["proof"])
                elif op["state"] == "terminal":
                    raise InvalidTransition(
                        "Terminal operation cannot regress to submission"
                    )
                else:
                    op["state"] = "submitted" if outcome == "accepted" else "unknown"
        elif kind == "inspection":
            self._job(conn, op, data["job_id"])
            if (
                op["submission_token"] is None
                and data["job_id"] is not None
                and not self.imported(op)
            ):
                self._quarantine(
                    conn,
                    op,
                    "job_observed_without_submit_intent",
                    data["job_id"],
                    aggregate_unknown=True,
                )
            if (
                not data["terminal"]
                and op["state"] == "terminal"
                and (data["job_id"] or op["job_id"])
            ):
                self._quarantine(
                    conn, op, "live_job_after_terminal", data["job_id"] or op["job_id"]
                )
            if data["terminal"] and data["outcome"] == "not_submitted":
                self._not_submitted(conn, op, data["not_submitted_proof"])
            elif data["terminal"]:
                if op["job_id"] is None:
                    raise InvalidTransition(
                        "A terminal job outcome needs its actual job ID"
                    )
                if op["inconsistency"] is None:
                    if (
                        op["state"] == "terminal"
                        and op["terminal_outcome"] != data["outcome"]
                    ):
                        self._quarantine(
                            conn,
                            op,
                            "terminal_outcome_disagreement",
                            data["job_id"] or op["job_id"],
                        )
                    else:
                        op.update(state="terminal", terminal_outcome=data["outcome"])
            elif op["state"] != "terminal" and op["inconsistency"] is None:
                if op["submission_token"] is None and not self.imported(op):
                    raise InvalidTransition("Unsubmitted operation has no running job")
                op["state"] = "submitted" if op["job_id"] else "unknown"
            if data["physical_released"]:
                if op["job_id"] is None and op["no_submit_proof"] is None:
                    raise InvalidTransition(
                        "Resource release requires a known job or positive no-submit proof"
                    )
                self._release_resources(conn, op)
            if data["terminal"] and op["physical_released"]:
                op["external_open"] = False
        elif kind == "cancellation":
            if data["job_id"] is not None and data["job_id"] != op["job_id"]:
                raise ConflictError(
                    "Cancellation acknowledgement names a different job"
                )
            op["cancel_acknowledged"] = (
                op["cancel_acknowledged"] or data["acknowledged"]
            )
        elif kind == "settlement":
            self._settle(conn, op, data["final_costs"])
        else:
            if op["state"] != "terminal":
                raise InvalidTransition("Collection requires a known terminal outcome")
            if op["inconsistency"] is not None:
                return  # Retain raw late facts without reopening scientific intake.
            if op["feedback_sealed"] or op["collection_state"] != "pending":
                raise InvalidTransition(
                    "Operation collection is already final or sealed"
                )
            op.update(
                collection_state="unavailable"
                if data["unavailable_reason"]
                else "collected",
                collection_ref=payload_ref,
                collection_identity=payload_identity,
            )
            for item in data["artifacts"]:
                conn.execute(
                    "INSERT INTO operation_artifacts VALUES(?,?,?)",
                    (op["id"], item["path"], item["version"]),
                )
            for item in data["evaluations"]:
                allowed = item["allow_feedback"]
                conn.execute(
                    "INSERT INTO evaluation_intake VALUES(?,?,?,?,?,?,?,?)",
                    (
                        item["receipt_ref"],
                        op["id"],
                        event_key,
                        _json(intake[item["receipt_ref"]]),
                        int(allowed),
                        "pending" if allowed else "rejected",
                        None,
                        None if allowed else "feedback_denied",
                    ),
                )

    @staticmethod
    def _save(conn, op):
        conn.execute(
            "UPDATE operations SET revision=revision+1,state=?,job_id=?,terminal_outcome=?,physical_released=?,held_resources_json=?,held_costs_json=?,actual_costs_json=?,no_submit_proof_json=?,collection_state=?,collection_ref=?,collection_identity_json=?,cancel_acknowledged=?,inconsistency=?,external_open=?,unexpected_job_ids_json=? WHERE id=?",
            (
                op["state"],
                op["job_id"],
                op["terminal_outcome"],
                int(op["physical_released"]),
                _json(op["held_resources"]),
                _json(op["held_costs"]),
                _json(op["actual_costs"]),
                _json(op["no_submit_proof"])
                if op["no_submit_proof"] is not None
                else None,
                op["collection_state"],
                op["collection_ref"],
                _json(op["collection_identity"])
                if op["collection_identity"] is not None
                else None,
                int(op["cancel_acknowledged"]),
                op["inconsistency"],
                int(op["external_open"]),
                _json(op["unexpected_job_ids"]),
                op["id"],
            ),
        )

    def record_receipt(self, operation_id, expected_revision, kind, event_key, payload):
        self.owner._live()
        integer(expected_revision, "expected_revision", minimum=1)
        text(event_key, "event_key")
        imported = self.imported(self.get(operation_id))
        payload = validate_receipt(kind, payload, allow_legacy_epoch=imported)
        with self.owner.store._connection(readonly=True) as conn:
            previous = _decode(
                conn.execute(
                    "SELECT * FROM operation_events WHERE event_key=?", (event_key,)
                ).fetchone()
            )
        if previous is not None:
            if (
                previous["operation_id"],
                previous["kind"],
            ) != (operation_id, kind):
                raise ConflictError("Operation event key reused with different intent")
            original = self.reader._read_record(
                previous["payload_ref"], previous["identity"]
            )
            if not _same(original, payload):
                raise ConflictError("Operation event key reused with different facts")
            return previous["receipt"]
        op = self.get(operation_id)
        if op["revision"] != expected_revision:
            raise ConflictError("Operation revision mismatch")
        self._check_binding(op, payload)
        intake = (
            {
                item["receipt_ref"]: _identity(
                    self.owner.store.root, item["receipt_ref"]
                )
                for item in payload["data"].get("evaluations", [])
            }
            if kind == "collection"
            else {}
        )
        from .files import publish_artifact

        payload_ref = publish_artifact(self.owner.store.root, payload)
        identity = _identity(self.owner.store.root, payload_ref)
        with self.owner._transaction() as (conn, _):
            current = self._get(conn, operation_id)
            if current["revision"] != expected_revision:
                raise ConflictError("Operation revision mismatch")
            self._check_binding(current, payload)
            if conn.execute(
                "SELECT 1 FROM operation_events WHERE event_key=?", (event_key,)
            ).fetchone():
                raise ConflictError(
                    "Operation event concurrently committed; replay its event key"
                )
            self.reader._check_identity(payload_ref, identity)
            for reference, expected in intake.items():
                self.reader._check_identity(reference, expected)
            conn.execute(
                "INSERT INTO operation_events VALUES(?,?,?,?,?,?,?)",
                (
                    event_key,
                    operation_id,
                    kind,
                    expected_revision,
                    payload_ref,
                    _json(identity),
                    "{}",
                ),
            )
            self._apply(
                conn,
                current,
                kind,
                payload["data"],
                event_key,
                payload_ref,
                identity,
                intake,
            )
            self._save(conn, current)
            result = self._get(conn, operation_id)
            from .dispatch import RuntimeEvents

            if op["state"] != "terminal" and result["state"] == "terminal":
                RuntimeEvents.emit(
                    conn,
                    "operation-terminal:" + event_key,
                    "operation_terminal",
                    operation_id,
                    payload_ref=payload_ref,
                    identity=identity,
                    payload={
                        "outcome": result["terminal_outcome"],
                        "iteration_id": result["iteration_id"],
                    },
                )
            if (
                op["inconsistency"] != result["inconsistency"]
                or op["unexpected_job_ids"] != result["unexpected_job_ids"]
            ):
                RuntimeEvents.emit(
                    conn,
                    "operation-attention:" + event_key,
                    "operation_attention",
                    operation_id,
                    payload_ref=payload_ref,
                    identity=identity,
                    payload={"reason": result["inconsistency"]},
                )
            if op["held_resources"] and not result["held_resources"]:
                RuntimeEvents.emit(
                    conn,
                    "operation-release:" + event_key,
                    "resources_released",
                    operation_id,
                    payload_ref=payload_ref,
                    identity=identity,
                )
            if not op["costs_final"] and result["costs_final"]:
                RuntimeEvents.emit(
                    conn,
                    "operation-settled:" + event_key,
                    "operation_settled",
                    operation_id,
                    payload_ref=payload_ref,
                    identity=identity,
                )
            receipt = dict(
                event_key=event_key,
                operation_id=operation_id,
                revision=result["revision"],
                state=result["state"],
                payload_ref=payload_ref,
                job_id=result["job_id"],
                inconsistency=result["inconsistency"],
            )
            conn.execute(
                "UPDATE operation_events SET receipt_json=? WHERE event_key=?",
                (_json(receipt), event_key),
            )
            return receipt

    def reject_evaluation(self, receipt_ref, reason):
        text(reason, "evaluation rejection reason")
        with self.owner._transaction() as (conn, _):
            row = _decode(
                conn.execute(
                    "SELECT * FROM evaluation_intake WHERE receipt_ref=?",
                    (receipt_ref,),
                ).fetchone()
            )
            if row is None:
                raise ConflictError("Unknown evaluator intake reference")
            if row["disposition"] == "rejected" and row["reason"] == reason:
                return row
            if row["disposition"] != "pending":
                raise InvalidTransition(
                    "Evaluator intake already has a different final disposition"
                )
            group = conn.execute(
                "SELECT g.feedback_sealed FROM operation_groups g JOIN operations o ON o.execute_attempt_id=g.execute_attempt_id WHERE o.id=?",
                (row["operation_id"],),
            ).fetchone()
            if group["feedback_sealed"]:
                raise InvalidTransition("Feedback intake is already sealed")
            self.reader._check_identity(receipt_ref, row["identity"])
            conn.execute(
                "UPDATE evaluation_intake SET disposition='rejected',reason=? WHERE receipt_ref=?",
                (reason, receipt_ref),
            )
            row.update(disposition="rejected", reason=reason)
            return row

    def finalize_feedback(self, execute_attempt_id):
        with self.owner._transaction() as (conn, _):
            group = conn.execute(
                "SELECT * FROM operation_groups WHERE execute_attempt_id=?",
                (execute_attempt_id,),
            ).fetchone()
            if group is None:
                raise ConflictError("Unknown operation group")
            unresolved = conn.execute(
                "SELECT 1 FROM operations WHERE execute_attempt_id=? AND (state!='terminal' OR collection_state NOT IN ('collected','unavailable') OR inconsistency IS NOT NULL) LIMIT 1",
                (execute_attempt_id,),
            ).fetchone()
            pending = conn.execute(
                "SELECT 1 FROM evaluation_intake e JOIN operations o ON o.id=e.operation_id WHERE o.execute_attempt_id=? AND e.disposition='pending' LIMIT 1",
                (execute_attempt_id,),
            ).fetchone()
            if unresolved is not None or pending is not None:
                raise InvalidTransition(
                    "Operation knowledge/evaluator intake is not complete"
                )
            conn.execute(
                "UPDATE operation_groups SET feedback_sealed=1 WHERE execute_attempt_id=?",
                (execute_attempt_id,),
            )
            return dict(execute_attempt_id=execute_attempt_id, feedback_sealed=True)
