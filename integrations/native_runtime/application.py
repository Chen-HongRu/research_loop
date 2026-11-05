"""Native role composition: Pi tool submissions and explicit imported cleanup.

This initial application supports qualitative Execute only. It neither edits
source nor submits new training jobs. Importing the module performs no CLI calls.
"""

from integrations.native_runtime import policy, transport
from research_loop.runtime.context import ContextPackage
from research_loop.runtime.operation_contract import fields, json_value

APPLICATION_IDENTITY = {"id": "native-runtime-v1", "version": "1"}


def validate_config(config):
    value = json_value(config)
    fields(value, {"transport", "backend_handlers"}, "native application")
    transport.validate_config(value["transport"])
    if not isinstance(value["backend_handlers"], dict):
        raise ValueError("Backend handlers must be explicitly configured")
    for backend, handler in value["backend_handlers"].items():
        if not isinstance(backend, str) or not backend.strip():
            raise ValueError("Backend name must be nonblank")
        fields(handler, {"handler", "config"}, "backend handler")
        if handler["handler"] != "protok_cleanup" or not isinstance(
            handler["config"], dict
        ):
            raise ValueError(
                "Only the explicit imported ProTok cleanup handler is implemented"
            )
        from integrations.protok.migration import validate_cleanup_config

        validate_cleanup_config(handler["config"])
    return value


def validate(role, output, context, config):
    validate_config(config)
    return policy.validate_output(role, output, context)


def invoke(kind, payload, config):
    config = validate_config(config)
    payload = json_value(payload)
    if kind == "backend":
        operation = payload["operation"]
        handler = config["backend_handlers"].get(operation["backend"])
        if (
            handler is None
            or not operation.get("submit_forbidden")
            or not operation.get("origin")
        ):
            raise ValueError("No configured imported-operation cleanup handler")
        if payload["action"] not in {
            "inspection",
            "cancellation",
            "collection",
            "settlement",
        }:
            raise ValueError("Native composition cannot prepare or submit backend jobs")
        from integrations.protok.migration import cleanup_action

        return json_value(
            cleanup_action(
                payload["action"], operation, payload["request"], handler["config"]
            )
        )
    if kind != "agent":
        raise ValueError("Unknown invocation kind")
    return invoke_agent(
        payload, config["transport"], policy.prepare_submission, policy.validate_output
    )


def invoke_agent(payload, transport_config, prepare_submission, validate_output):
    """Bind the role, deliver its result, and check every cited reference."""
    fields(payload, {"call", "context_ref", "package"}, "agent invocation")
    role = payload["call"]["role"]
    frozen = ContextPackage.from_dict(payload["package"]).to_dict()
    context, manifest = frozen["context"], frozen["manifest"]
    if manifest["role"] != role:
        raise ValueError("Call role differs from bound context")
    prompt, schema = prepare_submission(role, frozen)
    response = transport.run(
        prompt,
        transport_config,
        response_schema=schema,
        invocation={"call": payload["call"], "context_ref": payload["context_ref"]},
    )
    fields(response, {"output", "evidence_refs"}, "model response")
    normalized = validate_output(role, response["output"], context)
    if role in {"review", "scout"}:
        own_ids = (
            {
                context[key]["id"]
                for key in manifest["protected_keys"]
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
    if not set(refs) <= set(manifest["allowed_evidence"]):
        raise ValueError("Model cited evidence outside the bound manifest")
    if not set(policy.cited_refs(normalized, context)) <= set(refs):
        raise ValueError("Model omitted typed evidence references from evidence_refs")
    return {"output": normalized, "evidence_refs": refs}
