"""Native Pi role composition with an explicitly configured script backend."""

import re

from integrations.native_runtime import transport
from integrations.script_runtime import backend, policy
from research_loop.runtime.context import ContextPackage
from research_loop.runtime.operation_contract import fields, integer, json_value, text

APPLICATION_IDENTITY = {"id": "native-script-runtime-v1", "version": "1"}


def validate_config(config):
    value = json_value(config)
    fields(
        value,
        {"transport", "backend_handlers", "scientific_contract_source"},
        "script application",
    )
    transport.validate_config(value["transport"])
    source = value["scientific_contract_source"]
    if not isinstance(source, str) or not re.fullmatch(
        r"provided:[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", source
    ):
        raise ValueError(
            "Scientific contract requires an explicit provided context source"
        )
    handlers = value["backend_handlers"]
    if not isinstance(handlers, dict) or not handlers:
        raise ValueError("Script application requires configured backend handlers")
    for name, handler in handlers.items():
        text(name, "backend name")
        fields(handler, {"handler", "config"}, "backend handler")
        if handler["handler"] != "script":
            raise ValueError("Only the explicit script handler is supported")
        backend.validate_config(handler["config"])
    return value


def validate(role, output, context, config):
    config = validate_config(config)
    return policy.validate_output(role, output, context, config)


def invoke(kind, payload, config):
    config, payload = validate_config(config), json_value(payload)
    if kind == "backend":
        fields(
            payload,
            {"action", "operation_id", "operation", "request", "permit"}
            | ({"request_event_id"} & payload.keys()),
            "backend invocation",
        )
        operation, request = payload["operation"], payload["request"]
        if (
            payload["operation_id"] != operation["id"]
            or request["id"] != operation["logical_id"]
        ):
            raise ValueError("Backend packet changed operation or request identity")
        permit = payload["permit"]
        if payload["action"] == "submit":
            if not isinstance(permit, dict):
                raise ValueError("Submission requires a native durable permit")
            text(permit.get("submission_token"), "submission token")
            integer(permit.get("owner_epoch"), "submission owner", minimum=1)
            expected = {
                "operation_id": operation["id"],
                "request_id": operation["logical_id"],
                "plan_attempt_id": operation["plan_attempt_id"],
                "execute_attempt_id": operation["execute_attempt_id"],
                "backend": operation["backend"],
                "backend_version": operation["backend_version"],
                "environment": operation["environment"],
                "deadline": operation["deadline"],
                "operation_revision": operation["revision"] + 1,
                "request_ref": operation["request_ref"],
                "submission_token": permit["submission_token"],
                "owner_epoch": permit["owner_epoch"],
            }
            if permit != expected:
                raise ValueError("Submission permit differs from the frozen operation")
            operation = operation | {
                "submission_token": permit["submission_token"],
                "submit_owner_epoch": permit["owner_epoch"],
            }
        elif permit is not None:
            raise ValueError("Only submit carries a submission permit")
        handler = config["backend_handlers"].get(operation.get("backend"))
        if (
            handler is None
            or operation.get("submit_forbidden")
            or operation.get("origin")
        ):
            raise ValueError("No configured native script handler for this operation")
        if request.get("backend") != operation.get("backend"):
            raise ValueError("Operation and request backend differ")
        backend.validate_request(request, handler["config"])
        return json_value(
            backend.invoke(payload["action"], operation, request, handler["config"])
        )
    if kind != "agent":
        raise ValueError("Unknown invocation kind")
    fields(payload, {"call", "context_ref", "package"}, "agent invocation")
    role = payload["call"]["role"]
    package = ContextPackage.from_dict(payload["package"])
    if package.manifest["role"] != role:
        raise ValueError("Call role differs from bound context")
    response = transport.run(
        policy.prompt(role, package.to_dict(), config),
        config["transport"],
        response_schema=policy.native.response_schema(
            policy.MODELS[role], context=package.context if role == "review" else None
        ),
        invocation={"call": payload["call"], "context_ref": payload["context_ref"]},
    )
    fields(response, {"output", "evidence_refs"}, "model response")
    normalized = policy.validate_output(
        role, response["output"], package.context, config
    )
    if role in {"review", "scout"}:
        own_ids = (
            {
                package.context[key]["id"]
                for key in package.manifest["protected_keys"]
                if key.startswith("measurement:")
            }
            if role == "review"
            else set()
        )
        if {item["measurement_id"] for item in normalized["reviews"]} != own_ids:
            raise ValueError("Review must qualify every own measurement exactly once")
        if any(item["measurement_id"] in own_ids for item in normalized["corrections"]):
            raise ValueError(
                "Corrections require historical published measurement eligibility"
            )
    refs = response["evidence_refs"]
    if (
        not isinstance(refs, list)
        or any(not isinstance(ref, str) for ref in refs)
        or len(set(refs)) != len(refs)
    ):
        raise ValueError("Evidence references must be unique strings")
    if not set(refs) <= set(package.manifest["allowed_evidence"]):
        raise ValueError("Model cited evidence outside the bound manifest")
    if not set(policy.cited_refs(normalized, package.context)) <= set(refs):
        raise ValueError("Model omitted typed evidence references from evidence_refs")
    return {"output": normalized, "evidence_refs": refs}
