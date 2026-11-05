"""Pure backend-operation contracts and local SQL schema.

Receipt assertions originate in a trusted backend/controller. Shape validation
never authenticates their sender or establishes the truth of a proof reference.
"""

from dataclasses import dataclass
import json
import math
import re

OPERATION_SCHEMA = """
CREATE TABLE operation_groups (
 execute_attempt_id TEXT PRIMARY KEY REFERENCES attempts(id),
 plan_attempt_id TEXT NOT NULL REFERENCES attempts(id),
 iteration_id TEXT NOT NULL UNIQUE REFERENCES iterations(id),
 feedback_sealed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE operation_authorities (
 id TEXT PRIMARY KEY, backends_json TEXT NOT NULL, max_operations TEXT NOT NULL,
 deadline REAL, admitted_operations INTEGER NOT NULL DEFAULT 0, blocked_reason TEXT
);
CREATE TABLE operation_pool_balances (pool TEXT PRIMARY KEY, capacity TEXT NOT NULL, held TEXT NOT NULL, blocked_reason TEXT);
CREATE TABLE operation_scope_resources (
 authorization_id TEXT NOT NULL REFERENCES operation_authorities(id), pool TEXT NOT NULL,
 capacity TEXT NOT NULL, held TEXT NOT NULL, PRIMARY KEY(authorization_id,pool)
);
CREATE TABLE operation_scope_costs (
 authorization_id TEXT NOT NULL REFERENCES operation_authorities(id), unit TEXT NOT NULL,
 cost_limit TEXT NOT NULL, held TEXT NOT NULL, settled TEXT NOT NULL,
 PRIMARY KEY(authorization_id,unit)
);
CREATE TABLE operations (
 id TEXT PRIMARY KEY, logical_id TEXT NOT NULL,
 execute_attempt_id TEXT NOT NULL REFERENCES operation_groups(execute_attempt_id),
 entry_index INTEGER NOT NULL, request_ref TEXT NOT NULL, request_identity_json TEXT NOT NULL,
 authorization_id TEXT NOT NULL REFERENCES operation_authorities(id),
 backend TEXT NOT NULL, backend_version TEXT NOT NULL, environment_json TEXT NOT NULL,
 resources_json TEXT NOT NULL, cost_bounds_json TEXT NOT NULL, deadline REAL NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'pending',
 submission_token TEXT, submit_owner_epoch INTEGER, job_id TEXT, terminal_outcome TEXT,
 physical_released INTEGER NOT NULL DEFAULT 0,
 held_resources_json TEXT NOT NULL DEFAULT '{}', held_costs_json TEXT NOT NULL DEFAULT '{}',
 actual_costs_json TEXT NOT NULL DEFAULT '{}', no_submit_proof_json TEXT,
 collection_state TEXT NOT NULL DEFAULT 'pending', collection_ref TEXT, collection_identity_json TEXT,
 cancel_acknowledged INTEGER NOT NULL DEFAULT 0, inconsistency TEXT,
 external_open INTEGER NOT NULL DEFAULT 0,
 unexpected_job_ids_json TEXT NOT NULL DEFAULT '[]',
 UNIQUE(execute_attempt_id,logical_id)
);
CREATE INDEX open_external_operations ON operations(external_open) WHERE external_open=1;
CREATE TABLE operation_events (
 event_key TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES operations(id),
 kind TEXT NOT NULL, expected_revision INTEGER NOT NULL,
 payload_ref TEXT NOT NULL, identity_json TEXT NOT NULL, receipt_json TEXT NOT NULL
);
CREATE TABLE operation_artifacts (
 operation_id TEXT NOT NULL REFERENCES operations(id), path TEXT NOT NULL, version TEXT NOT NULL,
 PRIMARY KEY(operation_id,path,version)
);
CREATE TABLE evaluation_intake (
 receipt_ref TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES operations(id),
 collection_event_key TEXT NOT NULL REFERENCES operation_events(event_key),
 identity_json TEXT NOT NULL, allow_feedback INTEGER NOT NULL,
 disposition TEXT NOT NULL, measurement_id TEXT REFERENCES measurements(id), reason TEXT
);
"""

REQUEST_FIELDS = {
    "id",
    "backend",
    "authorization_id",
    "resources",
    "cost_bounds",
    "timeout_seconds",
    "input_artifacts",
    "parameters",
}
BINDING_FIELDS = {
    "operation_id",
    "request_id",
    "plan_attempt_id",
    "execute_attempt_id",
    "backend",
    "backend_version",
    "environment",
    "submission_token",
    "owner_epoch",
}
RECEIPT_KINDS = {
    "prepare",
    "submission",
    "inspection",
    "cancellation",
    "collection",
    "settlement",
}
TERMINAL_OUTCOMES = {"succeeded", "failed", "cancelled", "timed_out", "not_submitted"}
PROOF_BASES = {"sender_retired", "backend_no_late_create", "no_submission_intent"}


def json_value(value):
    def check(item):
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("JSON object keys must be strings")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif item is None or type(item) in (str, bool, int):
            pass
        elif type(item) is float and math.isfinite(item):
            pass
        else:
            raise ValueError("Expected finite JSON data")

    check(value)
    return json.loads(json.dumps(value, allow_nan=False))


def text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonblank text")


def integer(value, name, *, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def fields(value, expected, name):
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"Invalid {name} fields")


def vector(value, name, *, minimum=0):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    for key, amount in value.items():
        text(key, name)
        integer(amount, name, minimum=minimum)
    return value


def artifact(value):
    fields(value, {"path", "version"}, "artifact")
    text(value["path"], "artifact.path")
    text(value["version"], "artifact.version")
    return value


def proof(value):
    if value is None:
        return
    fields(value, {"basis", "reference"}, "no-submit proof")
    if not isinstance(value["basis"], str) or value["basis"] not in PROOF_BASES:
        raise ValueError("Unknown no-submit proof basis")
    text(value["reference"], "proof.reference")


@dataclass(frozen=True, init=False)
class ExecutionAdmission:
    _serialized: str

    @classmethod
    def capture(cls, value):
        value = json_value(value)
        instance = object.__new__(cls)
        object.__setattr__(instance, "_serialized", json.dumps(value, sort_keys=True))
        return instance

    def to_dict(self):
        return json.loads(self._serialized)


def validate_execution(requests, plan, config):
    """Validate nonempty operation requests against immutable Plan/config values."""
    if not isinstance(requests, list):
        raise ValueError("Execute.operations must be a finite explicit list")
    if not requests:
        return []
    requests, plan, config = json_value(requests), json_value(plan), json_value(config)
    integer(plan.get("timeout_seconds"), "Plan.timeout_seconds", minimum=1)
    backends, pools, grants = (
        config.get(key)
        for key in ("execution_backends", "resource_pools", "operation_authorizations")
    )
    if not isinstance(backends, dict) or not isinstance(grants, dict):
        raise ValueError("Operations require explicit backend and authorization maps")
    vector(pools, "resource_pools")
    for name, backend in backends.items():
        text(name, "backend name")
        fields(backend, {"version", "environment"}, "backend configuration")
        text(backend["version"], "backend.version")
        fields(backend["environment"], {"id", "version"}, "backend environment")
        for value in backend["environment"].values():
            text(value, "backend environment")
    for identity, grant in grants.items():
        text(identity, "authorization id")
        fields(
            grant,
            {
                "backends",
                "max_operations",
                "resource_limits",
                "cost_limits",
                "deadline",
            },
            "authorization",
        )
        allowed = grant["backends"]
        if (
            not isinstance(allowed, list)
            or any(
                not isinstance(name, str) or name not in backends for name in allowed
            )
            or len(set(allowed)) != len(allowed)
        ):
            raise ValueError("Authorization backends must be known unique names")
        integer(grant["max_operations"], "max_operations")
        vector(grant["resource_limits"], "resource_limits")
        vector(grant["cost_limits"], "cost_limits")
        if not set(grant["resource_limits"]) <= pools.keys():
            raise ValueError("Authorization names an unknown physical pool")
        deadline = grant["deadline"]
        if deadline is not None and (
            type(deadline) not in (int, float) or not math.isfinite(deadline)
        ):
            raise ValueError("Authorization deadline must be a finite UTC timestamp")
    seen, normalized = set(), []
    for request in requests:
        fields(request, REQUEST_FIELDS, "operation request")
        identity = request["id"]
        if (
            not isinstance(identity, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identity)
            or identity in seen
        ):
            raise ValueError("Operation logical IDs must be unique safe slugs")
        seen.add(identity)
        for name in ("backend", "authorization_id"):
            text(request[name], name)
        if (
            request["backend"] not in backends
            or request["authorization_id"] not in grants
        ):
            raise ValueError("Unknown operation backend or authorization")
        grant = grants[request["authorization_id"]]
        if request["backend"] not in grant["backends"]:
            raise ValueError("Backend is outside operation authorization")
        vector(request["resources"], "resources", minimum=1)
        vector(request["cost_bounds"], "cost_bounds")
        if (
            not request["resources"].keys() <= grant["resource_limits"].keys()
            or not request["cost_bounds"].keys() <= grant["cost_limits"].keys()
        ):
            raise ValueError("Operation names an unauthorized resource or cost unit")
        if any(
            amount > min(pools[key], grant["resource_limits"][key])
            for key, amount in request["resources"].items()
        ):
            raise ValueError("Operation resource request exceeds its fixed authority")
        if any(
            amount > grant["cost_limits"][key]
            for key, amount in request["cost_bounds"].items()
        ):
            raise ValueError("Operation bound exceeds its fixed cost authority")
        integer(request["timeout_seconds"], "timeout_seconds", minimum=1)
        if request["timeout_seconds"] > plan["timeout_seconds"]:
            raise ValueError("Operation timeout exceeds accepted Plan")
        try:
            if not math.isfinite(float(request["timeout_seconds"])):
                raise ValueError("Operation timeout must fit a finite timestamp")
        except OverflowError as exc:
            raise ValueError("Operation timeout must fit a finite timestamp") from exc
        if not isinstance(request["input_artifacts"], list):
            raise ValueError("Operation inputs must be an explicit list")
        for item in request["input_artifacts"]:
            artifact(item)
        if not isinstance(request["parameters"], dict):
            raise ValueError("Operation parameters must be a JSON object")
        normalized.append(
            dict(
                request=request,
                backend_binding=backends[request["backend"]],
                authorization=grant,
            )
        )
    return normalized


def validate_receipt(kind, payload):
    if not isinstance(kind, str) or kind not in RECEIPT_KINDS:
        raise ValueError("Unknown operation receipt kind")
    payload = json_value(payload)
    fields(payload, BINDING_FIELDS | {"data"}, "operation receipt")
    for key in (
        "operation_id",
        "request_id",
        "plan_attempt_id",
        "execute_attempt_id",
        "backend",
        "backend_version",
    ):
        text(payload[key], key)
    fields(payload["environment"], {"id", "version"}, "receipt environment")
    for value in payload["environment"].values():
        text(value, "receipt environment")
    if payload["submission_token"] is None:
        if payload["owner_epoch"] is not None:
            raise ValueError("No token can carry an owner epoch")
    else:
        text(payload["submission_token"], "submission_token")
        integer(payload["owner_epoch"], "owner_epoch", minimum=1)
    data = payload["data"]
    if kind == "prepare":
        fields(data, {"ready", "reason", "effective_parameters"}, "prepare receipt")
        if type(data["ready"]) is not bool or not isinstance(
            data["effective_parameters"], dict
        ):
            raise ValueError("Invalid prepare result")
        text(data["reason"], "prepare reason")
    elif kind == "submission":
        fields(data, {"outcome", "job_id", "proof", "reason"}, "submission receipt")
        if not isinstance(data["outcome"], str) or data["outcome"] not in {
            "accepted",
            "rejected",
            "unknown",
            "not_submitted",
        }:
            raise ValueError("Unknown submission outcome")
        proof(data["proof"])
        text(data["reason"], "submission reason")
        if data["outcome"] == "accepted" and data["job_id"] is None:
            raise ValueError("Accepted submission requires an actual job ID")
        if data["outcome"] in {"rejected", "not_submitted"} and data["proof"] is None:
            raise ValueError("Absence requires positive no-late-create proof")
    elif kind == "inspection":
        fields(
            data,
            {
                "job_id",
                "terminal",
                "outcome",
                "physical_released",
                "not_submitted_proof",
            },
            "inspection receipt",
        )
        if (
            type(data["terminal"]) is not bool
            or type(data["physical_released"]) is not bool
        ):
            raise ValueError("Inspection flags must be booleans")
        proof(data["not_submitted_proof"])
        if data["terminal"]:
            if (
                not isinstance(data["outcome"], str)
                or data["outcome"] not in TERMINAL_OUTCOMES
            ):
                raise ValueError("Terminal inspection requires an explicit outcome")
            if (
                data["outcome"] == "not_submitted"
                and data["not_submitted_proof"] is None
            ):
                raise ValueError("No-submission inspection requires positive proof")
        elif data["outcome"] is not None or data["not_submitted_proof"] is not None:
            raise ValueError(
                "Nonterminal inspection cannot claim an outcome or absence"
            )
    elif kind == "cancellation":
        fields(data, {"job_id", "acknowledged", "reason"}, "cancellation receipt")
        if type(data["acknowledged"]) is not bool:
            raise ValueError("Cancellation acknowledgement must be boolean")
        text(data["reason"], "cancellation reason")
    elif kind == "collection":
        if (
            not isinstance(data, dict)
            or not {"artifacts", "evaluations"} <= data.keys()
            or not data.keys() <= {"artifacts", "evaluations", "unavailable_reason"}
        ):
            raise ValueError("Invalid collection receipt fields")
        if not isinstance(data["artifacts"], list) or not isinstance(
            data["evaluations"], list
        ):
            raise ValueError("Collection artifacts/evaluations must be explicit lists")
        for item in data["artifacts"]:
            artifact(item)
        if len({(item["path"], item["version"]) for item in data["artifacts"]}) != len(
            data["artifacts"]
        ):
            raise ValueError("Duplicate collected artifact")
        refs = []
        for entry in data["evaluations"]:
            fields(entry, {"receipt_ref", "allow_feedback"}, "collection evaluation")
            text(entry["receipt_ref"], "receipt_ref")
            if type(entry["allow_feedback"]) is not bool:
                raise ValueError("allow_feedback must be boolean")
            refs.append(entry["receipt_ref"])
        if len(set(refs)) != len(refs):
            raise ValueError("Duplicate collected evaluator receipt")
        data.setdefault("unavailable_reason", None)
        if data["unavailable_reason"] is not None:
            text(data["unavailable_reason"], "unavailable_reason")
    else:
        fields(data, {"final_costs"}, "settlement receipt")
        vector(data["final_costs"], "final_costs")
    if "job_id" in data and data["job_id"] is not None:
        text(data["job_id"], "job_id")
    return payload
