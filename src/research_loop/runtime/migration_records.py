"""Explicit trusted translations of legacy operational facts, never new grants.

This module checks captured control references and representable bindings. It does
not authenticate an operator, infer scientific output, read external references,
or prove that a legacy sender stopped. Cutover/fencing belongs to the importer.
"""

import json
import math
import re

from .files import publish_artifact
from .operation_contract import (
    REQUEST_FIELDS,
    artifact,
    fields,
    integer,
    json_value,
    text,
    vector,
)
from .updates import _identity


_TASK = {
    "id",
    "revision",
    "specification",
    "source",
    "priority",
    "dependencies",
    "cancelled",
}
_OPERATION = {
    "id",
    "request",
    "backend_version",
    "environment",
    "job_id",
    "unexpected_job_ids",
    "submission_token",
    "submit_owner_epoch",
    "deadline",
    "state",
    "terminal_outcome",
    "physical_released",
    "held_resources",
    "held_costs",
    "actual_costs",
    "external_open",
    "collection_state",
    "inconsistency",
    "origin",
}


def _refs(value, available, selected):
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(ref, str) or ref not in available for ref in value)
    ):
        raise ValueError("Translation needs present captured source references")
    if len(set(value)) != len(value) or not set(value) <= selected:
        raise ValueError("Unselected or duplicate translation source reference")


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value
    ):
        raise ValueError("Unrepresentable legacy task identity")


def validate_translation(inventory, translation, runtime_config):
    """Validate explicit operator data outside any writer; return detached JSON."""
    translation = json_value(translation)
    config = json_value(runtime_config)
    fields(
        translation,
        {
            "version",
            "translator",
            "source_refs",
            "executions",
            "authorities",
            "blockers",
        },
        "translation",
    )
    if type(translation["version"]) is not int or translation["version"] != 1:
        raise ValueError("Unsupported translation version")
    fields(translation["translator"], {"id", "version"}, "translator identity")
    for value in translation["translator"].values():
        text(value, "translator identity")
    if not isinstance(inventory, dict) or not isinstance(
        inventory.get("records"), dict
    ):
        raise ValueError("A captured legacy inventory is required")
    available = {
        key
        for key, value in inventory["records"].items()
        if value.get("identity") is not None
    }
    annex = inventory.get("annex")
    if annex is not None:
        fields(annex, {"version", "records"}, "control annex")
        if (
            type(annex["version"]) is not int
            or annex["version"] != 1
            or not isinstance(annex["records"], dict)
        ):
            raise ValueError("Invalid control annex")
        for name, record in annex["records"].items():
            if not isinstance(name, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]*", name
            ):
                raise ValueError("Invalid annex logical name")
            if not isinstance(record, dict):
                raise ValueError("Invalid annex record")
            if record.get("identity") is not None:
                available.add("annex:" + name)
    _refs(translation["source_refs"], available, available)
    selected = set(translation["source_refs"])
    for name in ("executions", "authorities", "blockers"):
        if not isinstance(translation[name], list):
            raise ValueError(f"{name} must be an explicit list")
    for blocker in translation["blockers"]:
        text(blocker, "blocker")
    backends = config.get("execution_backends", {})
    grants = config.get("operation_authorizations", {})
    pools = config.get("resource_pools", {})
    if not isinstance(backends, dict) or not isinstance(grants, dict):
        raise ValueError("Unknown backend/authorization mapping")
    vector(pools, "physical pools")
    authorizations = {}
    for row in translation["authorities"]:
        fields(
            row,
            {"id", "admitted_operations", "settled_costs", "blocked_reason"},
            "historical authorization",
        )
        identity = row["id"]
        text(identity, "authorization id")
        if identity not in grants or identity in authorizations:
            raise ValueError("Unknown or duplicate authorization")
        integer(row["admitted_operations"], "historical admitted operations")
        vector(row["settled_costs"], "historical settled costs")
        if row["blocked_reason"] is not None:
            text(row["blocked_reason"], "blocked reason")
        grant = grants[identity]
        fields(
            grant,
            {
                "backends",
                "max_operations",
                "resource_limits",
                "cost_limits",
                "deadline",
            },
            "frozen authorization",
        )
        if grant["max_operations"] is not None:
            integer(grant["max_operations"], "max operations")
        vector(grant["resource_limits"], "resource limits")
        vector(grant["cost_limits"], "cost limits")
        if (
            not isinstance(grant["backends"], list)
            or any(name not in backends for name in grant["backends"])
            or not grant["resource_limits"].keys() <= pools.keys()
            or not row["settled_costs"].keys() <= grant["cost_limits"].keys()
        ):
            raise ValueError("Unrepresentable historical authorization units")
        if grant["deadline"] is not None and (
            type(grant["deadline"]) not in (int, float)
            or not math.isfinite(grant["deadline"])
        ):
            raise ValueError("Invalid original authorization deadline")
        authorizations[identity] = row
    if authorizations.keys() != grants.keys():
        raise ValueError("Every configured authorization needs historical accounting")
    identities = set()
    submitted = {identity: 0 for identity in grants}
    actuals = {identity: {} for identity in grants}
    for execution in translation["executions"]:
        fields(
            execution,
            {"task", "iteration", "plan", "execute", "operations"},
            "translated execution",
        )
        task, iteration = execution["task"], execution["iteration"]
        fields(task, _TASK, "claimed task")
        _identifier(task["id"])
        integer(task["revision"], "task revision", minimum=1)
        if (
            not isinstance(task["specification"], dict)
            or task["source"] not in {"human", "autonomous"}
            or type(task["priority"]) is not int
            or type(task["cancelled"]) is not bool
            or not isinstance(task["dependencies"], list)
            or len(set(task["dependencies"])) != len(task["dependencies"])
        ):
            raise ValueError("Invalid claimed task fields")
        for dependency in task["dependencies"]:
            _identifier(dependency)
        fields(iteration, {"id", "task_id", "task_revision", "revision"}, "iteration")
        text(iteration["id"], "iteration id")
        integer(iteration["revision"], "iteration revision", minimum=1)
        if (
            iteration["task_id"] != task["id"]
            or iteration["task_revision"] != task["revision"]
        ):
            raise ValueError("Imported task claim identity differs")
        for kind, identity in (("task", task["id"]), ("iteration", iteration["id"])):
            key = (kind, identity)
            if key in identities:
                raise ValueError("Duplicate translated identity")
            identities.add(key)
        for role in ("plan", "execute"):
            phase = execution[role]
            fields(
                phase,
                {"id", "normalized_output", "source_refs"},
                "legacy phase binding",
            )
            text(phase["id"], "original phase id")
            if ("attempt", phase["id"]) in identities:
                raise ValueError("Duplicate original attempt identity")
            identities.add(("attempt", phase["id"]))
            if phase["normalized_output"] is not None and not isinstance(
                phase["normalized_output"], dict
            ):
                raise ValueError(
                    "Phase binding must have explicit output or be unavailable"
                )
            _refs(phase["source_refs"], available, selected)
        if not isinstance(execution["operations"], list):
            raise ValueError("Operations must be explicit")
        logical_ids = set()
        for op in execution["operations"]:
            fields(op, _OPERATION, "imported operation")
            text(op["id"], "original operation id")
            if ("operation", op["id"]) in identities:
                raise ValueError("Duplicate original operation id")
            identities.add(("operation", op["id"]))
            req = op["request"]
            fields(req, REQUEST_FIELDS, "original operation request")
            text(req["id"], "original request id")
            if req["id"] in logical_ids:
                raise ValueError("Duplicate logical operation id in one execution")
            logical_ids.add(req["id"])
            if req["backend"] not in backends or req["authorization_id"] not in grants:
                raise ValueError("Unmapped original backend/authorization")
            grant = grants[req["authorization_id"]]
            if req["backend"] not in grant["backends"]:
                raise ValueError("Original backend lies outside its authorization")
            binding = backends[req["backend"]]
            fields(binding, {"version", "environment"}, "backend binding")
            fields(op["environment"], {"id", "version"}, "original environment")
            for value in (op["backend_version"], *op["environment"].values()):
                text(value, "backend/environment version")
            if (
                op["backend_version"] != binding["version"]
                or op["environment"] != binding["environment"]
            ):
                raise ValueError(
                    "Original backend/environment differs from configuration"
                )
            for name in ("resources", "cost_bounds"):
                vector(req[name], name)
            integer(req["timeout_seconds"], "original timeout", minimum=1)
            if not isinstance(req["parameters"], dict) or not isinstance(
                req["input_artifacts"], list
            ):
                raise ValueError("Invalid original request parameters/inputs")
            for item in req["input_artifacts"]:
                artifact(item)
            if (
                not req["resources"].keys() <= grant["resource_limits"].keys()
                or not req["cost_bounds"].keys() <= grant["cost_limits"].keys()
            ):
                raise ValueError("Unmapped original resource/cost units")
            for name in ("held_resources", "held_costs", "actual_costs"):
                vector(op[name], name)
            if (
                not op["held_resources"].keys() <= req["resources"].keys()
                or not (op["held_costs"].keys() | op["actual_costs"].keys())
                <= grant["cost_limits"].keys()
            ):
                raise ValueError("Unmapped historical accounting units")
            if not isinstance(op["unexpected_job_ids"], list):
                raise ValueError("Unexpected job identities must be an explicit list")
            for job in op["unexpected_job_ids"]:
                text(job, "unexpected job identity")
            if (
                len(set(op["unexpected_job_ids"])) != len(op["unexpected_job_ids"])
                or op["job_id"] in op["unexpected_job_ids"]
            ):
                raise ValueError("Duplicate primary/unexpected job identity")
            for name in ("job_id", "submission_token", "inconsistency"):
                if op[name] is not None:
                    text(op[name], name)
            if op["submit_owner_epoch"] is not None:
                integer(op["submit_owner_epoch"], "original owner epoch", minimum=1)
                if op["submission_token"] is None:
                    raise ValueError("Owner epoch without original submission token")
            if type(op["deadline"]) not in (int, float) or not math.isfinite(
                op["deadline"]
            ):
                raise ValueError("Original absolute deadline is required")
            if grant["deadline"] is not None and op["deadline"] > grant["deadline"]:
                raise ValueError("Imported deadline exceeds original authorization")
            if op["state"] not in {
                "pending",
                "prepared",
                "submitting",
                "unknown",
                "submitted",
                "terminal",
            }:
                raise ValueError("Unrepresentable original operation state")
            if op["terminal_outcome"] not in {
                None,
                "succeeded",
                "failed",
                "cancelled",
                "timed_out",
                "not_submitted",
            }:
                raise ValueError("Unrepresentable terminal outcome")
            if (op["state"] == "terminal") != (op["terminal_outcome"] is not None):
                raise ValueError(
                    "Terminal outcome must be explicit exactly for terminal state"
                )
            for name in ("physical_released", "external_open"):
                if type(op[name]) is not bool:
                    raise ValueError(
                        "Physical/external flags must be explicit booleans"
                    )
            if op["physical_released"] and op["held_resources"]:
                raise ValueError("Released resources cannot remain held")
            if not op["physical_released"] and any(
                op["held_resources"].get(k, 0) < v for k, v in req["resources"].items()
            ):
                raise ValueError(
                    "Unknown physical release must retain original resource holds"
                )
            if (op["state"] != "terminal" or not op["physical_released"]) and not op[
                "external_open"
            ]:
                raise ValueError("Unresolved imported effects cannot be marked closed")
            if any(
                unit not in op["actual_costs"] and op["held_costs"].get(unit, 0) < bound
                for unit, bound in req["cost_bounds"].items()
            ):
                raise ValueError("Unknown costs must retain their original bound")
            if op["collection_state"] != "pending":
                raise ValueError(
                    "Imported scientific collection needs a separate explicit intake; only pending is supported"
                )
            fields(
                op["origin"],
                {"original_request_identity", "source_refs", "metadata"},
                "original operation provenance",
            )
            text(op["origin"]["original_request_identity"], "original request identity")
            if not isinstance(op["origin"]["metadata"], dict):
                raise ValueError("Origin metadata must be an object")
            _refs(op["origin"]["source_refs"], available, selected)
            auth = req["authorization_id"]
            submitted[auth] += int(
                op["state"] != "terminal"
                or op["terminal_outcome"] != "not_submitted"
                or op["submission_token"] is not None
            )
            for unit, amount in op["actual_costs"].items():
                actuals[auth][unit] = actuals[auth].get(unit, 0) + amount
    for identity, row in authorizations.items():
        if row["admitted_operations"] < submitted[identity] or any(
            row["settled_costs"].get(unit, 0) < amount
            for unit, amount in actuals[identity].items()
        ):
            raise ValueError("Historical counts or settled fees cannot be reset")
    return translation


def build_translation(root, inventory, translation, runtime_config, import_id):
    """Publish selected phase/request records outside the staged writer."""
    text(import_id, "import id")
    value = validate_translation(inventory, translation, runtime_config)
    config = json_value(runtime_config)
    prepared = dict(
        version=1,
        import_id=import_id,
        translation=value,
        bindings=[],
        operations=[],
        pools={},
        authorities={},
        blockers=list(value["blockers"]),
    )
    prepared["blockers"] = list(
        dict.fromkeys([*inventory.get("blockers", []), *prepared["blockers"]])
    )
    for row in inventory.get("history", {}).get("iterations", []):
        if row.get("publication") == "absent" and not any(
            row["metadata_ref"] in phase["source_refs"]
            for execution in value["executions"]
            for phase in (execution["plan"], execution["execute"])
        ):
            prepared["blockers"].append(f"untranslated_execution:{row['id']}")
    source = dict(
        import_id=import_id,
        source_root=inventory["source_root"],
        source_identity=inventory["source_identity"],
        translator=value["translator"],
    )
    for execution in value["executions"]:
        for role in ("plan", "execute"):
            phase = execution[role]
            origin = {**source, "source_refs": phase["source_refs"], "role": role}
            reference = (
                None
                if phase["normalized_output"] is None
                else publish_artifact(
                    root,
                    dict(
                        version=1,
                        kind="legacy_phase_binding",
                        normalized_output=phase["normalized_output"],
                        origin=origin,
                    ),
                )
            )
            prepared["bindings"].append(
                dict(
                    id=phase["id"],
                    role=role,
                    iteration_id=execution["iteration"]["id"],
                    output_ref=reference,
                    output_identity=_identity(root, reference) if reference else None,
                    origin=origin,
                )
            )
        for op in execution["operations"]:
            op = json_value(op)
            if op["unexpected_job_ids"]:
                # Match native quarantine: quantities are lower bounds while
                # additional external identities remain unresolved.
                op.update(
                    physical_released=False,
                    external_open=True,
                    inconsistency=op["inconsistency"] or "conflicting_job_identity",
                )
                for pool, amount in op["request"]["resources"].items():
                    op["held_resources"][pool] = max(
                        amount, op["held_resources"].get(pool, 0)
                    )
                for unit, bound in op["request"]["cost_bounds"].items():
                    op["held_costs"][unit] = max(bound, op["held_costs"].get(unit, 0))
            origin = {**source, **op["origin"]}
            reference = publish_artifact(
                root,
                dict(
                    version=1,
                    kind="legacy_operation_request",
                    normalized_output={"operations": [op["request"]]},
                    origin=origin,
                ),
            )
            prepared["operations"].append(
                dict(
                    **{key: value for key, value in op.items() if key != "origin"},
                    iteration_id=execution["iteration"]["id"],
                    plan_attempt_id=execution["plan"]["id"],
                    execute_attempt_id=execution["execute"]["id"],
                    request_ref=reference,
                    request_identity=_identity(root, reference),
                    origin=origin,
                )
            )
    for pool, capacity in config.get("resource_pools", {}).items():
        held = sum(op["held_resources"].get(pool, 0) for op in prepared["operations"])
        blocked = "imported_resource_overcommit" if held > capacity else None
        if any(
            op["unexpected_job_ids"] and pool in op["request"]["resources"]
            for op in prepared["operations"]
        ):
            blocked = "conflicting_job_identity"
        prepared["pools"][pool] = dict(
            capacity=capacity, held=held, blocked_reason=blocked
        )
        if blocked:
            prepared["blockers"].append(f"physical_pool:{pool}:{blocked}")
    for historical in value["authorities"]:
        identity = historical["id"]
        grant = config["operation_authorizations"][identity]
        ops = [
            op
            for op in prepared["operations"]
            if op["request"]["authorization_id"] == identity
        ]
        reason = historical["blocked_reason"]
        if any(
            amount > op["request"]["cost_bounds"].get(unit, 0)
            for op in ops
            for unit, amount in op["actual_costs"].items()
        ):
            reason = reason or "actual_cost_exceeded_bound_or_authority"
        if any(op["inconsistency"] is not None for op in ops):
            reason = reason or "imported_operation_inconsistency"
        if (
            grant["max_operations"] is not None
            and historical["admitted_operations"] > grant["max_operations"]
        ):
            reason = reason or "historical_admissions_exceed_limit"
        resources = {
            pool: dict(
                capacity=limit,
                held=sum(op["held_resources"].get(pool, 0) for op in ops),
            )
            for pool, limit in grant["resource_limits"].items()
        }
        costs = {
            unit: dict(
                cost_limit=limit,
                held=sum(op["held_costs"].get(unit, 0) for op in ops),
                settled=historical["settled_costs"].get(unit, 0),
            )
            for unit, limit in grant["cost_limits"].items()
        }
        if any(row["held"] > row["capacity"] for row in resources.values()) or any(
            row["held"] + row["settled"] > row["cost_limit"] for row in costs.values()
        ):
            reason = reason or "historical_exposure_exceeds_limit"
        prepared["authorities"][identity] = dict(
            backends=grant["backends"],
            max_operations=grant["max_operations"],
            deadline=grant["deadline"],
            admitted_operations=historical["admitted_operations"],
            blocked_reason=reason,
            resources=resources,
            costs=costs,
        )
        if reason:
            prepared["blockers"].append(f"authorization:{identity}:{reason}")
    return prepared


def _insert(conn, table, keys, values):
    where = " AND ".join(key + "=?" for key in keys)
    prior = conn.execute(
        f"SELECT {','.join(values)} FROM {table} WHERE {where}",
        [values[key] for key in keys],
    ).fetchone()
    if prior is not None:
        if dict(prior) != values:
            raise ValueError(f"Existing {table} differs from translated facts")
        return
    conn.execute(
        f"INSERT INTO {table}({','.join(values)}) VALUES({','.join('?' for _ in values)})",
        list(values.values()),
    )


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def install_translation(conn, prepared):
    """Install staged records only; no file reads, callbacks, counters or grants."""
    control = conn.execute("SELECT admission_mode,pool_version FROM control").fetchone()
    if control["admission_mode"] != "inspect_only":
        raise ValueError("Operational import requires staged inspect-only admission")
    for execution in prepared["translation"]["executions"]:
        task = execution["task"]
        _insert(
            conn,
            "tasks",
            ["id"],
            dict(
                id=task["id"],
                revision=task["revision"],
                specification_json=_json(task["specification"]),
                source=task["source"],
                priority=task["priority"],
                dependencies_json=_json(task["dependencies"]),
                cancelled=int(task["cancelled"]),
            ),
        )
        iteration = execution["iteration"]
        complete = (
            all(
                execution[role]["normalized_output"] is not None
                for role in ("plan", "execute")
            )
            and not task["cancelled"]
        )
        _insert(
            conn,
            "iterations",
            ["id"],
            dict(
                **iteration, phase="review", status="active" if complete else "blocked"
            ),
        )
    for phase in prepared["bindings"]:
        _insert(
            conn,
            "attempts",
            ["id"],
            dict(
                id=phase["id"],
                request_id=f"legacy:{prepared['import_id']}:{phase['id']}",
                role=phase["role"],
                iteration_id=phase["iteration_id"],
                expected_revision=None,
                owner_epoch=0,
                worker_token="",
                origin_kind="legacy",
                status="accepted" if phase["output_ref"] is not None else "unavailable",
                output_ref=phase["output_ref"],
                output_identity_json=_json(phase["output_identity"])
                if phase["output_ref"]
                else None,
                receipt_json=_json(
                    dict(
                        kind="legacy_phase_binding",
                        available=phase["output_ref"] is not None,
                        **phase["origin"],
                    )
                ),
            ),
        )
    for execution in prepared["translation"]["executions"]:
        if execution["operations"]:
            _insert(
                conn,
                "operation_groups",
                ["execute_attempt_id"],
                dict(
                    execute_attempt_id=execution["execute"]["id"],
                    plan_attempt_id=execution["plan"]["id"],
                    iteration_id=execution["iteration"]["id"],
                    feedback_sealed=0,
                ),
            )
    for pool, row in prepared["pools"].items():
        _insert(
            conn,
            "operation_pool_balances",
            ["pool"],
            dict(
                pool=pool,
                capacity=str(row["capacity"]),
                held=str(row["held"]),
                blocked_reason=row["blocked_reason"],
            ),
        )
    for identity, row in prepared["authorities"].items():
        _insert(
            conn,
            "operation_authorities",
            ["id"],
            dict(
                id=identity,
                backends_json=_json(row["backends"]),
                max_operations=str(row["max_operations"])
                if row["max_operations"] is not None
                else None,
                deadline=row["deadline"],
                admitted_operations=row["admitted_operations"],
                blocked_reason=row["blocked_reason"],
            ),
        )
        for pool, balance in row["resources"].items():
            _insert(
                conn,
                "operation_scope_resources",
                ["authorization_id", "pool"],
                dict(
                    authorization_id=identity,
                    pool=pool,
                    capacity=str(balance["capacity"]),
                    held=str(balance["held"]),
                ),
            )
        for unit, balance in row["costs"].items():
            _insert(
                conn,
                "operation_scope_costs",
                ["authorization_id", "unit"],
                dict(
                    authorization_id=identity,
                    unit=unit,
                    cost_limit=str(balance["cost_limit"]),
                    held=str(balance["held"]),
                    settled=str(balance["settled"]),
                ),
            )
    for op in prepared["operations"]:
        request = op["request"]
        _insert(
            conn,
            "operations",
            ["id"],
            dict(
                id=op["id"],
                logical_id=request["id"],
                execute_attempt_id=op["execute_attempt_id"],
                entry_index=0,
                request_ref=op["request_ref"],
                request_identity_json=_json(op["request_identity"]),
                authorization_id=request["authorization_id"],
                backend=request["backend"],
                backend_version=op["backend_version"],
                environment_json=_json(op["environment"]),
                resources_json=_json(request["resources"]),
                cost_bounds_json=_json(request["cost_bounds"]),
                deadline=op["deadline"],
                submission_token=op["submission_token"],
                submit_owner_epoch=op["submit_owner_epoch"],
                job_id=op["job_id"],
                unexpected_job_ids_json=_json(op["unexpected_job_ids"]),
                state=op["state"],
                terminal_outcome=op["terminal_outcome"],
                physical_released=int(op["physical_released"]),
                held_resources_json=_json(op["held_resources"]),
                held_costs_json=_json(op["held_costs"]),
                actual_costs_json=_json(op["actual_costs"]),
                external_open=int(op["external_open"]),
                collection_state=op["collection_state"],
                inconsistency=op["inconsistency"],
                submit_forbidden=1,
                origin_json=_json(op["origin"]),
            ),
        )
    return dict(
        import_id=prepared["import_id"],
        operations=[op["id"] for op in prepared["operations"]],
        attempts=[phase["id"] for phase in prepared["bindings"]],
        blockers=list(prepared["blockers"]),
    )
