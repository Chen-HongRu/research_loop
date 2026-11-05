"""ProTok legacy control translation and composable, import-only cleanup.

No model, SSH, submission, source-tree validation or scientific artifact reads
occur here. The application chooses its native Agent policies independently.
Captured assertions remain attributed evidence, not manufactured native results.
"""

import json
import math
from pathlib import PurePosixPath
import re
import stat

from research_loop.debug_budget import TERMINAL_STATES
from research_loop.runtime.operation_contract import (
    fields,
    integer,
    json_value,
    proof,
    text,
    vector,
)
from research_loop.runtime.legacy_inventory import (
    _annex_path,
    _file_identity,
    _metadata,
)


TRANSLATOR = {"id": "protok-legacy-controls", "version": "1"}


def with_captured_controls(inventory, backend_capture):
    """Normalize an already transferred controller capture, without rereading it.

    The caller supplies the trusted read-only capture, not arbitrary source text.
    Fresh local/remote captures can instead use capture_control_annex.
    """
    result, captured = json_value(inventory), json_value(backend_capture)
    if captured.get("version") != 1 or captured.get("source_root") != result.get(
        "source_root"
    ):
        raise ValueError("Backend capture belongs to a different legacy Study")
    original = captured.get("extra_control_refs")
    if not isinstance(original, dict):
        raise ValueError("Expected named captured backend control records")
    annex = result.setdefault("annex", {"version": 1, "records": {}})
    if annex.get("version") != 1 or not isinstance(annex.get("records"), dict):
        raise ValueError("Unsupported captured control annex")
    for name, record in original.items():
        if not isinstance(name, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*", name
        ):
            raise ValueError("Captured control names must be single safe segments")
        path = record.get("source_ref")
        if (
            not isinstance(path, str)
            or not PurePosixPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts
        ):
            raise ValueError("Captured controller source requires an absolute path")
        identity, body = record.get("identity"), record.get("text")
        if identity is not None:
            fields(
                identity,
                {"device", "inode", "mode", "size", "mtime_ns", "ctime_ns"},
                "captured identity",
            )
            if any(
                type(value) is not int for value in identity.values()
            ) or not isinstance(body, str):
                raise ValueError(
                    "Captured controls require exact metadata and UTF-8 text"
                )
            if not stat.S_ISREG(identity["mode"]):
                raise ValueError("Captured controls must be regular files")
        elif body is not None:
            raise ValueError("Missing controls cannot carry a body")
        normalized = {"source_path": path, "identity": identity, "text": body}
        if name in annex["records"] and annex["records"][name] != normalized:
            raise ValueError("Captured annex name already has a different binding")
        annex["records"][name] = normalized
    return result


def _captured(inventory, reference):
    if not isinstance(reference, str):
        raise ValueError("A captured source reference is required")
    if reference.startswith("annex:"):
        value = inventory.get("annex", {}).get("records", {}).get(reference[6:])
    else:
        value = inventory.get("records", {}).get(reference)
    if (
        not isinstance(value, dict)
        or value.get("identity") is None
        or not isinstance(value.get("text"), str)
    ):
        raise ValueError("Selected control reference is absent from the capture")
    return value


def _record(inventory, reference):
    value = _captured(inventory, reference)
    result = json.loads(value["text"])
    if not isinstance(result, dict):
        raise ValueError("Selected controller JSON must be an object")
    return json_value(result)


def _references(inventory, values):
    if not isinstance(values, list) or not values or len(set(values)) != len(values):
        raise ValueError("Explicit unique captured source references are required")
    for value in values:
        _captured(inventory, value)
    return values


def _deadline(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("Original absolute deadline is required")
    return value


def _configuration(inventory, selectors):
    fields(
        selectors,
        {"version", "authorization_ref", "accounting", "jobs", "current_source_refs"},
        "ProTok control selectors",
    )
    if type(selectors["version"]) is not int or selectors["version"] != 1:
        raise ValueError("Unsupported ProTok control selection")
    grant = _record(inventory, selectors["authorization_ref"])
    text(grant.get("grant_id"), "original grant identity")
    integer(grant.get("gpu_seconds"), "original GPU budget", minimum=1)
    _deadline(grant.get("deadline"))
    if grant.get("max_operations") is not None:
        integer(grant["max_operations"], "original operation limit")
    account = selectors["accounting"]
    fields(
        account,
        {"settled_gpu_seconds", "admitted_operations", "source_refs"},
        "historical accounting",
    )
    integer(account["settled_gpu_seconds"], "settled GPU seconds")
    integer(account["admitted_operations"], "historical admitted operations")
    _references(inventory, account["source_refs"])
    _references(inventory, selectors["current_source_refs"])
    if not isinstance(selectors["jobs"], list):
        raise ValueError("Legacy job selections must be an explicit list")
    return grant


def _state(inventory, identity):
    current = inventory["native_state"]
    if str(current.get("iteration")).zfill(4) == identity:
        return current
    bundle = _record(inventory, f"iterations/{identity}/publication.json")
    state = bundle.get("state_after")
    if not isinstance(state, dict):
        raise ValueError("Legacy publication lacks its native state")
    return state


def _plan(inventory, state):
    reference = state.get("plan_ref")
    plan = _record(inventory, reference)
    if plan.get("decision") != "investigate":
        raise ValueError(
            "Only the original accepted investigation Plan can bind cleanup"
        )
    integer(plan.get("timeout_seconds"), "original Plan timeout", minimum=1)
    execution = plan.get("execution")
    if not isinstance(execution, dict) or execution.get("mode") != "script":
        raise ValueError("This adapter supports the recorded script contract only")
    integer(execution.get("time_limit_seconds"), "original operation bound", minimum=1)
    if (
        execution["time_limit_seconds"] > plan["timeout_seconds"]
        or execution.get("gpu") is not True
    ):
        raise ValueError("Unrepresentable original script bounds")
    attempt = str(PurePosixPath(reference).parent)
    raw = _record(inventory, attempt + "/response.json")
    receipt_ref = attempt + "/completed.json"
    receipt = _record(inventory, receipt_ref)
    process_ref = attempt + "/process.json"
    process_present = (
        inventory["records"].get(process_ref, {}).get("identity") is not None
    )
    # ExecutionHarness can publish an outer receipt without an outer process;
    # completed_response uses an empty process record in that same case. This
    # certifies parsed Plan content, never effect/sender retirement.
    process = _record(inventory, process_ref) if process_present else {}
    if (
        receipt.get("version") != 1
        or receipt.get("run_id") != process.get("run_id")
        or receipt.get("response") != raw
    ):
        raise ValueError("Original Plan completion identity differs")
    expected = raw
    if "base_experiment" not in raw:
        proposal = (state.get("selected") or {}).get("proposal")
        if not isinstance(proposal, dict) or "base_experiment" not in proposal:
            raise ValueError("Original controller-selected Plan base is unavailable")
        expected = {**raw, "base_experiment": proposal["base_experiment"]}
    if plan != expected:
        raise ValueError("Canonical Plan differs from its original completed response")
    return plan, [reference, attempt + "/response.json", receipt_ref] + (
        [process_ref] if process_present else []
    )


def _binding(inventory, state, grant):
    identity = str(state["iteration"]).zfill(4)
    plan, refs = _plan(inventory, state)
    attempt = state.get("calls", {}).get("execute")
    if not isinstance(attempt, str) or not re.fullmatch(
        r"iterations/[0-9]{4,}/execute/attempt-[0-9]+", attempt
    ):
        raise ValueError("Original Execute attempt identity is unavailable")
    budget_ref = attempt + "/parameter-budget.json"
    deadline = min(
        _deadline(_record(inventory, budget_ref).get("deadline")), grant["deadline"]
    )
    return {
        "study_root": inventory["source_root"],
        "iteration_id": identity,
        "attempt": attempt,
        "plan_ref": state["plan_ref"],
        "plan": plan,
        "execution": plan["execution"],
        "grant_id": grant["grant_id"],
        "deadline": deadline,
        "seconds": plan["execution"]["time_limit_seconds"],
    }, refs + [budget_ref]


def _job(binding, value):
    for key in ("study_root", "attempt", "plan", "execution", "grant_id", "seconds"):
        if value.get(key) != binding[key]:
            raise ValueError(
                "Legacy receipt differs from the original execution binding"
            )
    if _deadline(value.get("deadline")) > binding["deadline"]:
        raise ValueError("Legacy receipt extends the original execution deadline")
    job = value.get("job_id")
    if job is not None:
        text(job, "original external job identity")
    terminal = value.get("terminal_state")
    if terminal is not None:
        if (
            job is None
            or terminal not in TERMINAL_STATES
            or not isinstance(value.get("exit_code"), str)
            or not re.fullmatch(r"[0-9]+:[0-9]+", value["exit_code"])
        ):
            raise ValueError("Terminal receipt lacks bound scheduler accounting")
        integer(value.get("gpu_seconds"), "actual GPU seconds")
    return value


def _historical_script_scope(inventory):
    """Enumerate the captured prior script scope, not the selected job subset."""
    current = inventory["native_state"]["iteration"]
    expected = set()
    for row in inventory["history"]["iterations"]:
        identity = row["id"]
        if int(identity) >= current:
            continue
        state = _state(inventory, identity)
        if state.get("plan_ref") is None:
            continue
        plan = _record(inventory, state["plan_ref"])
        if (
            isinstance(plan.get("execution"), dict)
            and plan["execution"].get("mode") == "script"
        ):
            expected.add(identity)
    return expected


def _no_submission_assertion(binding, value, *, original_job, grant):
    """A historical assertion is scoped to the actual original execution."""
    authorization = value.get("authorization")
    if (
        type(value.get("version")) is not int
        or value["version"] != 1
        or value.get("effect") != "never_submitted"
        or not isinstance(authorization, (str, dict))
        or not authorization
        or (isinstance(authorization, str) and not authorization.strip())
    ):
        raise ValueError("Legacy no-submission assertion is not explicit")
    if "original_job" in value:
        if value["original_job"] != original_job:
            raise ValueError(
                "Legacy assertion must retain the exact captured original job"
            )
        if authorization != grant:
            raise ValueError("Legacy assertion must retain the exact original grant")
        scope = value["original_job"]
        profile, pointer = "protok-original-job-v1", "/original_job"
    else:
        scope = value
        profile, pointer = "protok-flat-assertion-v1", ""
    for key in ("study_root", "attempt", "grant_id", "plan", "execution"):
        if scope.get(key) != binding[key]:
            raise ValueError(
                "Legacy no-submission assertion lacks its original execution binding"
            )
    return {
        "source_profile": profile,
        "binding_pointer": pointer,
        "authorization_pointer": "/authorization",
        "semantic_kind": "legacy_operator_assertion",
    }


def translate_legacy(
    inventory, backend_controls, *, backend_identity, environment, resource_profile
):
    """Project only the unresolved Execute; archive reviewed iterations unchanged.

    backend_controls is an explicit selector over captured records, not paths to
    read. Resource capacities are an explicit trusted mapping of the old grant.
    No operation-count cap is invented where the original grant had none.
    """
    inventory, selectors = json_value(inventory), json_value(backend_controls)
    fields(backend_identity, {"id", "version"}, "backend identity")
    fields(environment, {"id", "version"}, "backend environment")
    for value in [*backend_identity.values(), *environment.values()]:
        text(value, "backend binding")
    fields(
        resource_profile,
        {"pools", "per_operation", "authorization_limits", "source_refs"},
        "original resource profile",
    )
    for key in ("pools", "authorization_limits"):
        vector(resource_profile[key], key)
    vector(resource_profile["per_operation"], "original resource request", minimum=1)
    if not resource_profile["per_operation"]:
        raise ValueError(
            "Unknown execution exposure requires an explicit resource vector"
        )
    for pool, amount in resource_profile["per_operation"].items():
        if amount > resource_profile["pools"].get(
            pool, -1
        ) or amount > resource_profile["authorization_limits"].get(pool, -1):
            raise ValueError(
                "Original allocation exceeds the explicit resource mapping"
            )
    _references(inventory, resource_profile["source_refs"])
    grant = _configuration(inventory, selectors)
    selected_scope = [
        row.get("iteration_id") for row in selectors["jobs"] if isinstance(row, dict)
    ]
    if (
        len(selected_scope) != len(selectors["jobs"])
        or any(not isinstance(identity, str) for identity in selected_scope)
        or len(set(selected_scope)) != len(selected_scope)
        or set(selected_scope) != _historical_script_scope(inventory)
    ):
        raise ValueError(
            "Job selectors must cover every captured prior script iteration"
        )
    source_refs = [
        "study.json",
        "state.json",
        selectors["authorization_ref"],
        *selectors["accounting"]["source_refs"],
        *resource_profile["source_refs"],
        *selectors["current_source_refs"],
    ]
    known_cost, known_jobs, seen = 0, 0, set()
    archive, assertions, assertion_scopes = [], [], []
    for selected in selectors["jobs"]:
        fields(
            selected,
            {"iteration_id", "record_ref", "never_submitted_ref"},
            "original job selection",
        )
        identity = selected["iteration_id"]
        if (
            not isinstance(identity, str)
            or not re.fullmatch(r"[0-9]{4,}", identity)
            or identity in seen
        ):
            raise ValueError("Original job iteration identities must be unique")
        seen.add(identity)
        state = _state(inventory, identity)
        binding, refs = _binding(inventory, state, grant)
        job = _job(binding, _record(inventory, selected["record_ref"]))
        if any(
            job.get(key)
            for key in (
                "unexpected_job_ids",
                "additional_jobs",
                "aggregate_jobs",
                "jobs",
                "attempts",
            )
        ):
            raise ValueError(
                "Historical extra or aggregate jobs require explicit reconciliation"
            )
        source_refs.extend([selected["record_ref"], *refs])
        if (
            job.get("job_id") is not None
            or job.get("owned_job_id") is not None
            or bool(job.get("unexpected_job_ids"))
        ) and selected["never_submitted_ref"] is not None:
            raise ValueError(
                "Positive historical jobs cannot be overridden by no-submission assertions"
            )
        if job.get("terminal_state") is not None:
            known_cost += job["gpu_seconds"]
            known_jobs += 1
        elif selected["never_submitted_ref"] is not None:
            assertion = _record(inventory, selected["never_submitted_ref"])
            scope = _no_submission_assertion(
                binding, assertion, original_job=job, grant=grant
            )
            # Retain its source scope; do not turn a historical assertion into a
            # fresh sender-retirement proof for the current Execute.
            source_refs.append(selected["never_submitted_ref"])
            assertions.append(selected["never_submitted_ref"])
            assertion_scopes.append(
                {"source_ref": selected["never_submitted_ref"], **scope}
            )
        else:
            raise ValueError("Historical job accounting is unresolved")
        archive.append(identity)
    account = selectors["accounting"]
    if (
        account["settled_gpu_seconds"] < known_cost
        or account["admitted_operations"] < known_jobs + 1
    ):
        raise ValueError(
            "Historical accounting or the unknown reservation cannot be refunded"
        )
    current = inventory["native_state"]
    if (
        current.get("phase") != "execute"
        or current.get("accepted_calls", {}).get("execute") is True
        or current.get("execution_ref") is not None
    ):
        raise ValueError("This translation requires the unresolved original Execute")
    binding, refs = _binding(inventory, current, grant)
    if binding["iteration_id"] in seen:
        raise ValueError("Current execution cannot also be archived as terminal")
    source_refs.extend(refs)
    metadata_ref = f"iterations/{binding['iteration_id']}/iteration.json"
    _record(inventory, metadata_ref)
    source_refs.append(metadata_ref)
    process_ref = binding["attempt"] + "/process.json"
    if inventory["records"].get(process_ref, {}).get("identity") is not None:
        source_refs.append(process_ref)
    source_refs = list(dict.fromkeys(source_refs))
    _references(inventory, source_refs)
    task_id = "legacy-iteration-" + binding["iteration_id"]
    resources = json_value(resource_profile["per_operation"])
    costs = {"gpu_seconds": binding["seconds"]}
    request = {
        "id": "legacy-script",
        "backend": backend_identity["id"],
        "authorization_id": grant["grant_id"],
        "resources": resources,
        "cost_bounds": costs,
        "timeout_seconds": binding["seconds"],
        "input_artifacts": [],
        "parameters": {
            "legacy_execution": binding["execution"],
            "legacy_request_identity": binding["attempt"],
        },
    }
    operation = {
        "id": binding["attempt"],
        "request": request,
        "backend_version": backend_identity["version"],
        "environment": environment,
        "job_id": None,
        "unexpected_job_ids": [],
        "submission_token": None,
        "submit_owner_epoch": None,
        "deadline": binding["deadline"],
        "state": "unknown",
        "terminal_outcome": None,
        "physical_released": False,
        "held_resources": resources,
        "held_costs": costs,
        "actual_costs": {},
        "external_open": True,
        "collection_state": "pending",
        "inconsistency": None,
        "origin": {
            "original_request_identity": binding["attempt"],
            "source_refs": source_refs,
            "metadata": {
                "binding": binding,
                "external_request_identity_known": False,
                "input_versions_available": False,
                "historical_no_submission_scopes": assertion_scopes,
            },
        },
    }
    blockers = [
        "protok:original_execute_effects_unknown",
        "protok:trusted_execute_settlement_required",
    ]
    translation = {
        "version": 1,
        "translator": TRANSLATOR,
        "source_refs": source_refs,
        "executions": [
            {
                "task": {
                    "id": task_id,
                    "revision": 1,
                    "specification": {"legacy_iteration_ref": metadata_ref},
                    "source": "autonomous",
                    "priority": 100,
                    "dependencies": [],
                    "cancelled": False,
                },
                "iteration": {
                    "id": binding["iteration_id"],
                    "task_id": task_id,
                    "task_revision": 1,
                    "revision": 1,
                },
                "plan": {
                    "id": str(PurePosixPath(binding["plan_ref"]).parent),
                    "normalized_output": binding["plan"],
                    "source_refs": refs,
                },
                "execute": {
                    "id": binding["attempt"],
                    "normalized_output": None,
                    "source_refs": [metadata_ref, *selectors["current_source_refs"]],
                },
                "operations": [operation],
            }
        ],
        "authorities": [
            {
                "id": grant["grant_id"],
                "admitted_operations": account["admitted_operations"],
                "settled_costs": {"gpu_seconds": account["settled_gpu_seconds"]},
                "blocked_reason": "legacy_execute_effects_unresolved",
            }
        ],
        "blockers": blockers,
    }
    patch = {
        "execution_backends": {
            backend_identity["id"]: {
                "version": backend_identity["version"],
                "environment": environment,
            }
        },
        "resource_pools": resource_profile["pools"],
        "operation_authorizations": {
            grant["grant_id"]: {
                "backends": [backend_identity["id"]],
                "max_operations": grant.get("max_operations"),
                "resource_limits": resource_profile["authorization_limits"],
                "cost_limits": {"gpu_seconds": grant["gpu_seconds"]},
                "deadline": grant["deadline"],
            }
        },
        "migration_admission_deadline": grant["deadline"],
    }
    return json_value(
        {
            "translation": translation,
            "runtime_config_patch": patch,
            "readiness": {
                "can_review": False,
                "archive_only_iterations": archive,
                "no_submission_assertion_refs": assertions,
                "no_submission_scopes": assertion_scopes,
                "current_iteration": binding["iteration_id"],
                "pending_human_tasks": [
                    row["id"]
                    for row in inventory["history"]["tasks"]
                    if row.get("status") == "pending"
                ],
                "required": blockers,
            },
        }
    )


def query_local_receipt(binding, receipt_path):
    """Read one explicitly configured original controller JSON, never its outputs.

    The application must select a small controller receipt path. Missing metadata
    remains unknown. File identity is checked on both sides of the actual input
    read, without hashing, discovery, SSH or creating any file.
    """
    path = _annex_path(receipt_path)
    before = _metadata(path)
    if before is None:
        return None
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Original controller receipt must be a regular file")
    raw = path.read_bytes().decode("utf-8")
    after = _metadata(_annex_path(receipt_path))
    if after is None or _file_identity(before) != _file_identity(after):
        raise ValueError("Original controller receipt changed during its read")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Original controller receipt must be an object")
    _job(binding, value)
    event_key = "local-receipt:" + ":".join(
        str(value) for value in _file_identity(before).values()
    )
    return {"event_key": event_key, "receipt": json_value(value)}


def _cleanup_configuration(config):
    config = json_value(config)
    if (
        not isinstance(config, dict)
        or set(config) - {"inventory", "backend_controls", "receipt_path"}
        or not {"inventory", "backend_controls"} <= config.keys()
    ):
        raise ValueError(
            "Cleanup requires explicit frozen capture and controller receipt configuration"
        )
    if config.get("receipt_path") is not None:
        path = config["receipt_path"]
        if (
            not isinstance(path, str)
            or not PurePosixPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts
        ):
            raise ValueError(
                "Original controller receipt path must be explicitly absolute"
            )
    inventory = config["inventory"]
    if (
        not isinstance(inventory, dict)
        or not isinstance(inventory.get("records"), dict)
        or not isinstance(inventory.get("native_state"), dict)
        or not isinstance(inventory.get("source_root"), str)
    ):
        raise ValueError("Cleanup requires a captured native state and source records")
    state = inventory["native_state"]
    if (
        type(state.get("iteration")) is not int
        or state["iteration"] < 1
        or not isinstance(state.get("calls"), dict)
        or not isinstance(state.get("plan_ref"), str)
    ):
        raise ValueError("Cleanup requires the original iteration and Plan binding")
    grant = _configuration(config["inventory"], config["backend_controls"])
    binding, _ = _binding(
        config["inventory"], config["inventory"]["native_state"], grant
    )
    return config, binding


def validate_cleanup_config(config):
    """Validate frozen control bindings before reservation, without filesystem I/O."""
    return _cleanup_configuration(config)[0]


def cleanup_action(action, operation, request, config, *, query_original=None):
    """Return one trusted receipt fact; never launch, cancel, collect bytes or retry.

    query_original(binding) is an explicitly supplied read-only transport. It
    returns None or {event_key, receipt}; the receipt uses original flat job keys
    and may include an explicit not_submitted_proof. Missing records prove nothing.
    The trusted application envelopes this data with Operations.envelope.
    """
    if action not in {"inspection", "settlement", "collection"}:
        raise ValueError("Imported ProTok cleanup never prepares or submits operations")
    operation, request = (
        json_value(operation),
        json_value(request),
    )
    config, expected = _cleanup_configuration(config)
    if (
        operation.get("submit_forbidden") is not True
        or not isinstance(operation.get("origin"), dict)
        or not isinstance(operation["origin"].get("original_request_identity"), str)
    ):
        raise ValueError("Cleanup requires an import-only legacy operation")
    binding = operation.get("origin", {}).get("metadata", {}).get("binding")
    if (
        not isinstance(binding, dict)
        or operation.get("id") != binding.get("attempt")
        or operation.get("authorization_id") != binding.get("grant_id")
        or request.get("authorization_id") != binding.get("grant_id")
        or request.get("parameters")
        != {
            "legacy_execution": binding.get("execution"),
            "legacy_request_identity": binding.get("attempt"),
        }
    ):
        raise ValueError("Cleanup differs from the original imported operation")
    if binding != expected or operation.get("deadline") != binding["deadline"]:
        raise ValueError("Cleanup original Plan or deadline binding changed")
    if action == "collection":
        if (
            operation.get("state") != "terminal"
            or operation.get("physical_released") is not True
            or operation.get("inconsistency") is not None
        ):
            raise ValueError(
                "Unknown or quarantined effects cannot enter scientific collection"
            )
        return {
            "event_key": "protok-legacy:"
            + binding["attempt"]
            + ":collection-unavailable",
            "data": {
                "artifacts": [],
                "evaluations": [],
                "unavailable_reason": "Legacy scientific outputs have not been imported as trusted evaluator receipts",
            },
        }
    observed = (
        query_original(json_value(binding))
        if query_original is not None
        else query_local_receipt(binding, config["receipt_path"])
        if config.get("receipt_path") is not None
        else None
    )
    if observed is None:
        if action == "settlement":
            raise ValueError(
                "Unknown fees require explicit bound accounting, never a guessed zero"
            )
        if operation.get("state") == "terminal":
            raise ValueError(
                "Missing evidence cannot contradict an already terminal operation"
            )
        return {
            "event_key": "protok-legacy:" + binding["attempt"] + ":unknown",
            "data": {
                "job_id": operation.get("job_id"),
                "terminal": False,
                "outcome": None,
                "physical_released": False,
                "not_submitted_proof": None,
            },
        }
    fields(observed, {"event_key", "receipt"}, "original query result")
    text(observed["event_key"], "original observation event key")
    receipt = _job(binding, json_value(observed["receipt"]))
    absence = receipt.get("not_submitted_proof")
    proof(absence)
    terminal = receipt.get("terminal_state") in TERMINAL_STATES
    if absence is not None:
        if receipt.get("job_id") is not None or terminal:
            raise ValueError("No-submission proof contradicts a positive original job")
        outcome, terminal = "not_submitted", True
    elif terminal:
        state = receipt["terminal_state"]
        outcome = (
            "succeeded"
            if state == "COMPLETED" and receipt["exit_code"] == "0:0"
            else "cancelled"
            if state == "CANCELLED"
            else "timed_out"
            if state == "TIMEOUT"
            else "failed"
        )
    else:
        outcome = None
    if action == "settlement":
        if not terminal:
            raise ValueError(
                "Final accounting requires terminal or positively absent effects"
            )
        if (
            operation.get("state") != "terminal"
            or operation.get("job_id") != receipt.get("job_id")
            or operation.get("terminal_outcome") != outcome
        ):
            raise ValueError(
                "Final fees require prior inspection of the same job and terminal outcome"
            )
        if outcome == "not_submitted" and operation.get("no_submit_proof") is None:
            raise ValueError(
                "Final no-submission fees require existing positive reconciliation"
            )
        integer(receipt.get("gpu_seconds"), "actual cumulative GPU seconds")
        data = {"final_costs": {"gpu_seconds": receipt["gpu_seconds"]}}
    else:
        data = {
            "job_id": receipt.get("job_id"),
            "terminal": terminal,
            "outcome": outcome,
            "physical_released": terminal,
            "not_submitted_proof": absence,
        }
    return json_value(
        {
            "event_key": "protok-legacy:"
            + binding["attempt"]
            + ":"
            + action
            + ":"
            + observed["event_key"],
            "data": data,
        }
    )
