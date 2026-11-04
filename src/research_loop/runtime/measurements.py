"""Register evaluator receipts supplied by the trusted backend/controller.

Agent narratives cannot call this interface. The backend is responsible for
authenticating the evaluator and its execution; shape checks do not prove truth.
Version 1 binds direct Execute artifacts. Version 2 binds a pending, feedback-
permitted intake and an exact operation collection event; sealed Execute outputs
are never amended. Denied intake bodies are never opened.
"""

import json
import math
import uuid

from research_loop.evaluation import validate_evaluation

from .store import ConflictError, InvalidTransition, _decode, _json, _text
from .updates import Updater, _identity, _same


class MeasurementRegistry:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.reader = Updater(coordinator, validators={})

    @staticmethod
    def _accepted(conn, iteration_id):
        return {
            row["role"]: _decode(row)
            for row in conn.execute(
                "SELECT a.id,a.role,a.output_ref,a.output_identity_json "
                "FROM attempts a JOIN contexts c ON c.attempt_id=a.id "
                "WHERE a.iteration_id=? AND a.status='accepted' "
                "AND a.role IN ('plan','execute')",
                (iteration_id,),
            )
        }

    @staticmethod
    def _operation_sources(conn, iteration_id, receipt_ref):
        group = _decode(
            conn.execute(
                "SELECT * FROM operation_groups WHERE iteration_id=?", (iteration_id,)
            ).fetchone()
        )
        intake = _decode(
            conn.execute(
                "SELECT * FROM evaluation_intake WHERE receipt_ref=?", (receipt_ref,)
            ).fetchone()
        )
        operation, event, artifacts = None, None, []
        if intake is not None:
            operation = _decode(
                conn.execute(
                    "SELECT id,execute_attempt_id,state,inconsistency,collection_state,collection_ref,collection_identity_json "
                    "FROM operations WHERE id=?",
                    (intake["operation_id"],),
                ).fetchone()
            )
            event = _decode(
                conn.execute(
                    "SELECT event_key,operation_id,kind,payload_ref,identity_json "
                    "FROM operation_events WHERE event_key=?",
                    (intake["collection_event_key"],),
                ).fetchone()
            )
            artifacts = [
                dict(row)
                for row in conn.execute(
                    "SELECT path,version FROM operation_artifacts WHERE operation_id=? ORDER BY path,version",
                    (intake["operation_id"],),
                )
            ]
        return dict(
            group=group,
            intake=intake,
            operation=operation,
            event=event,
            artifacts=artifacts,
        )

    @staticmethod
    def _source_identity(sources):
        return {
            **sources,
            "group": {
                key: value
                for key, value in sources["group"].items()
                if key != "feedback_sealed"
            }
            if sources["group"]
            else None,
            "operation": {
                key: value
                for key, value in sources["operation"].items()
                if key not in {"state", "inconsistency"}
            }
            if sources["operation"]
            else None,
            "intake": {
                key: value
                for key, value in sources["intake"].items()
                if key not in {"disposition", "measurement_id", "reason"}
            }
            if sources["intake"]
            else None,
        }

    def _check_sources(self, sources, identity, existing=None):
        group, intake = sources["group"], sources["intake"]
        if existing is None and group is not None and group["feedback_sealed"]:
            raise InvalidTransition("Operation feedback is sealed")
        if intake is None:
            return
        if intake["allow_feedback"] != 1 or intake["disposition"] == "rejected":
            raise InvalidTransition("Evaluator intake does not permit feedback")
        expected = "registered" if existing is not None else "pending"
        if intake["disposition"] != expected or not _same(intake["identity"], identity):
            raise ConflictError(
                "Evaluator intake disposition or immutable identity differs"
            )
        if existing is not None and intake["measurement_id"] != existing["id"]:
            raise ConflictError("Intake references a different registered measurement")
        operation, event = sources["operation"], sources["event"]
        if (
            existing is None
            and operation is not None
            and (
                operation["state"] != "terminal"
                or operation["inconsistency"] is not None
            )
        ):
            raise InvalidTransition("Operation lacks uncontested terminal evidence")
        if (
            group is None
            or operation is None
            or event is None
            or operation["execute_attempt_id"] != group["execute_attempt_id"]
            or operation["collection_state"] not in {"collected", "unavailable"}
            or event["kind"] != "collection"
            or event["operation_id"] != operation["id"]
            or event["payload_ref"] != operation["collection_ref"]
            or not _same(event["identity"], operation["collection_identity"])
        ):
            raise ConflictError(
                "Evaluator intake has no matching accepted operation collection"
            )
        if existing is not None and (
            group["iteration_id"] != existing["iteration_id"]
            or group["plan_attempt_id"] != existing["plan_attempt_id"]
            or group["execute_attempt_id"] != existing["execute_attempt_id"]
        ):
            raise ConflictError("Registered operation group binding differs")
        self.reader._check_identity(
            operation["collection_ref"], operation["collection_identity"]
        )

    @staticmethod
    def _receipt_version(receipt):
        fields = {
            "version",
            "iteration_id",
            "execute_attempt_id",
            "candidate_id",
            "artifact",
            "evaluation_id",
            "comparison_scope",
            "evaluator",
            "metrics",
        }
        if (
            not isinstance(receipt, dict)
            or type(receipt.get("version")) is not int
            or receipt["version"] not in (1, 2)
        ):
            raise ValueError("Unsupported evaluator receipt version")
        if receipt["version"] == 2:
            fields |= {"operation_id", "collection_event_key"}
        if set(receipt) != fields:
            raise ValueError("Malformed evaluator receipt")
        return receipt["version"]

    @classmethod
    def _operation_artifact(cls, receipt, sources):
        version = cls._receipt_version(receipt)
        intake = sources["intake"]
        if version == 1:
            if intake is not None:
                raise ConflictError(
                    "Operation intake requires an explicit version-2 evaluator receipt"
                )
            return False
        if (
            intake is None
            or receipt["operation_id"] != intake["operation_id"]
            or receipt["collection_event_key"] != intake["collection_event_key"]
            or not any(
                _same(receipt["artifact"], artifact)
                for artifact in sources["artifacts"]
            )
        ):
            raise ConflictError(
                "Evaluator receipt differs from its operation collection"
            )
        return True

    def _capture(self, iteration_id, receipt_ref):
        self.coordinator._live()
        with self.coordinator.store._connection(readonly=True) as conn:
            conn.execute("BEGIN")
            existing = _decode(
                conn.execute(
                    "SELECT * FROM measurements WHERE receipt_ref=?", (receipt_ref,)
                ).fetchone()
            )
            sources = self._operation_sources(conn, iteration_id, receipt_ref)
            if existing:
                return {"existing": existing, "operation_sources": sources}
            iteration = _decode(
                conn.execute(
                    "SELECT * FROM iterations WHERE id=?", (iteration_id,)
                ).fetchone()
            )
            if iteration is None:
                raise ConflictError("Unknown measurement iteration")
            task = _decode(
                conn.execute(
                    "SELECT * FROM tasks WHERE id=?", (iteration["task_id"],)
                ).fetchone()
            )
            if task is None or task["revision"] != iteration["task_revision"]:
                raise ConflictError(
                    "Measurement task differs from its claimed revision"
                )
            config = json.loads(
                conn.execute("SELECT config_json FROM control").fetchone()[0]
            )
            self._before_review(conn, iteration_id)
            return dict(
                iteration=iteration,
                task=task,
                config=config,
                accepted=self._accepted(conn, iteration_id),
                operation_sources=sources,
            )

    @staticmethod
    def _before_review(conn, iteration_id):
        iteration = conn.execute(
            "SELECT phase,status FROM iterations WHERE id=?", (iteration_id,)
        ).fetchone()
        if iteration is None or tuple(iteration) != ("review", "active"):
            raise InvalidTransition("Measurement requires accepted Plan and Execute")
        if conn.execute(
            "SELECT 1 FROM attempts WHERE iteration_id=? AND role='review'",
            (iteration_id,),
        ).fetchone():
            raise InvalidTransition(
                "Measurements freeze before the first Review attempt"
            )

    def _prepare(self, iteration_id, receipt, captured):
        self._receipt_version(receipt)
        if receipt["iteration_id"] != iteration_id:
            raise ConflictError("Evaluator receipt belongs to a different iteration")
        accepted = captured["accepted"]
        if set(accepted) != {"plan", "execute"}:
            raise InvalidTransition("Measurement requires validated Plan and Execute")
        if receipt["execute_attempt_id"] != accepted["execute"]["id"]:
            raise ConflictError("Evaluator receipt belongs to a different Execute")
        sources = captured["operation_sources"]
        asynchronous = self._operation_artifact(receipt, sources)
        if asynchronous and (
            sources["group"]["plan_attempt_id"] != accepted["plan"]["id"]
            or sources["group"]["execute_attempt_id"] != accepted["execute"]["id"]
            or sources["group"]["iteration_id"] != iteration_id
        ):
            raise ConflictError(
                "Operation collection belongs to different accepted calls"
            )
        outputs = {
            role: self.reader._read_record(row["output_ref"], row["output_identity"])[
                "normalized_output"
            ]
            for role, row in accepted.items()
        }
        for key in ("candidate_id", "evaluation_id", "comparison_scope"):
            _text(receipt[key], key)
        for name, keys in (
            ("artifact", {"path", "version"}),
            ("evaluator", {"id", "version"}),
        ):
            value = receipt[name]
            if not isinstance(value, dict) or set(value) != keys:
                raise ValueError(f"Malformed receipt {name}")
            for key in keys:
                _text(value[key], f"{name}.{key}")
        artifacts = outputs["execute"].get("artifacts")
        if not asynchronous and (
            not isinstance(artifacts, list)
            or not any(_same(receipt["artifact"], artifact) for artifact in artifacts)
        ):
            raise ConflictError("Receipt artifact was not declared by accepted Execute")
        metrics = receipt["metrics"]
        if not isinstance(metrics, dict):
            raise ValueError("Measurement metrics must be an object")
        for name, value in metrics.items():
            _text(name, "metric name")
            if type(value) is not int and not (
                type(value) is float and math.isfinite(value)
            ):
                raise ValueError("Measurement metrics must be finite numbers")
        global_definition = captured["config"].get("global_evaluation")
        if not isinstance(global_definition, dict):
            raise ValueError(
                "Measurement registration requires config.global_evaluation"
            )
        validate_evaluation(global_definition)
        local_definition = outputs["plan"].get("local_evaluation")
        if local_definition is not None:
            validate_evaluation(local_definition)
            if local_definition["id"] == global_definition["id"]:
                raise ConflictError(
                    "Local evaluation cannot alias the global evaluation"
                )
        if receipt["evaluation_id"] == global_definition["id"]:
            level, definition = "global", global_definition
        elif local_definition and receipt["evaluation_id"] == local_definition["id"]:
            level, definition = "local", local_definition
        else:
            raise ConflictError("Receipt evaluation was not fixed by Study or Plan")
        if receipt["comparison_scope"] != definition["comparison_scope"]:
            raise ConflictError(
                "Receipt comparison scope differs from fixed evaluation"
            )
        if definition["data_role"] != "development" or not definition["allow_feedback"]:
            raise ValueError(
                "Final-only or feedback-denied measurements cannot be published"
            )
        route_id = captured["task"]["specification"].get("route_id")
        if level == "local" or route_id is not None:
            _text(route_id, "route_id")
        return dict(
            id=uuid.uuid4().hex,
            iteration_id=iteration_id,
            candidate_id=receipt["candidate_id"],
            route_id=route_id,
            level=level,
            evaluation_id=definition["id"],
            definition=definition,
            artifact=receipt["artifact"],
            evaluator=receipt["evaluator"],
            metrics=metrics,
            plan_attempt_id=accepted["plan"]["id"],
            execute_attempt_id=accepted["execute"]["id"],
        )

    def register(self, iteration_id, receipt_ref):
        """Register one immutable receipt, before any Review reservation.

        Missing metrics remain incomplete observations and cannot enter a Frontier.
        Exact receipt replay returns the original measurement, including on restart.
        """
        _text(iteration_id, "iteration_id")
        root = self.coordinator.store.root
        identity = _identity(root, receipt_ref)
        captured = self._capture(iteration_id, receipt_ref)
        existing = captured.get("existing")
        sources = captured["operation_sources"]
        self._check_sources(sources, identity, existing)
        if existing:
            if existing["iteration_id"] != iteration_id or not _same(
                existing["identity"], identity
            ):
                raise ConflictError("Registered receipt identity or iteration differs")
            receipt = self.reader._read_record(receipt_ref, identity)
            self._operation_artifact(receipt, sources)
            if not _same(receipt["artifact"], existing["artifact"]):
                raise ConflictError("Registered receipt artifact differs")
        else:
            receipt = self.reader._read_record(receipt_ref, identity)
            record = self._prepare(iteration_id, receipt, captured)
            record.update(receipt_ref=receipt_ref, identity=identity)
            # Serialize small derived records before acquiring the write lock.
            values = tuple(
                _json(record[key[:-5]]) if key.endswith("_json") else record[key]
                for key in (
                    "id",
                    "iteration_id",
                    "candidate_id",
                    "route_id",
                    "level",
                    "evaluation_id",
                    "definition_json",
                    "artifact_json",
                    "evaluator_json",
                    "metrics_json",
                    "receipt_ref",
                    "identity_json",
                    "plan_attempt_id",
                    "execute_attempt_id",
                )
            )
        with self.coordinator._transaction() as (conn, _):
            self.reader._check_identity(receipt_ref, identity)
            current = _decode(
                conn.execute(
                    "SELECT * FROM measurements WHERE receipt_ref=?", (receipt_ref,)
                ).fetchone()
            )
            current_sources = self._operation_sources(conn, iteration_id, receipt_ref)
            if not _same(
                self._source_identity(sources), self._source_identity(current_sources)
            ):
                raise ConflictError("Operation collection or intake identity changed")
            self._check_sources(current_sources, identity, current)
            if current:
                if current["iteration_id"] != iteration_id or not _same(
                    current["identity"], identity
                ):
                    raise ConflictError(
                        "Registered receipt identity or iteration differs"
                    )
                return current
            if existing:
                raise ConflictError("Registered measurement disappeared")
            self._before_review(conn, iteration_id)
            if not _same(self._accepted(conn, iteration_id), captured["accepted"]):
                raise ConflictError("Accepted measurement inputs changed")
            for row in captured["accepted"].values():
                self.reader._check_identity(row["output_ref"], row["output_identity"])
            if conn.execute(
                "SELECT 1 FROM measurements WHERE iteration_id=? AND candidate_id=? AND evaluation_id=?",
                (iteration_id, record["candidate_id"], record["evaluation_id"]),
            ).fetchone():
                raise ConflictError(
                    "Candidate evaluation already has a different receipt"
                )
            if (
                record["level"] == "local"
                and conn.execute(
                    "SELECT 1 FROM measurements WHERE level='local' AND route_id=? "
                    "AND evaluation_id=? AND definition_json<>?",
                    (
                        record["route_id"],
                        record["evaluation_id"],
                        _json(record["definition"]),
                    ),
                ).fetchone()
            ):
                raise ConflictError("Local evaluation changed within a route")
            conn.execute(
                "INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values
            )
            if current_sources["intake"] is not None:
                conn.execute(
                    "UPDATE evaluation_intake SET disposition='registered',measurement_id=?,reason=NULL "
                    "WHERE receipt_ref=?",
                    (record["id"], receipt_ref),
                )
            return record
