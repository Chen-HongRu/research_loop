"""Native role composition: Pi tool submissions and explicit imported cleanup.

This initial application supports qualitative Execute only. It neither edits
source nor submits new training jobs. Importing the module performs no CLI calls.
"""

from pathlib import Path

from integrations.native_runtime import policy, transport
from integrations.native_runtime import role_tools
from research_loop.runtime.role_assembly import assemble, definition
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
    fields(
        payload,
        {"call", "context_ref", "package"}
        | (
            {"assembly", "request_budget", "knowledge_config", "planning_libraries"}
            & payload.keys()
        ),
        "agent invocation",
    )
    role = payload["call"]["role"]
    frozen = ContextPackage.from_dict(payload["package"]).to_dict()
    context, manifest = frozen["context"], frozen["manifest"]
    if manifest["role"] != role:
        raise ValueError("Call role differs from bound context")
    assembly = payload.get("assembly")
    tool_kwargs = {}
    draft = {}
    if assembly is not None:
        if (
            assembly["role"] != role
            or assembly["context_ref"] != payload["context_ref"]
        ):
            raise ValueError("Frozen assembly differs from the bound call")
        selected = ContextPackage.from_dict(assembly["package"]).to_dict()
        context, manifest = selected["context"], selected["manifest"]
        prompt, schema = assembly["prompt"], assembly["response_schema"]
        history = json_value(assembly["history"])
        root = payload.get("request_budget", {}).get("study_root")
        if root is None:
            raise ValueError("Prepared role requires its private Study root")
        tool_handler, draft = role_tools.handler(
            assembly, study_root=root, config=payload.get("knowledge_config", {})
        )
        tool_kwargs = dict(
            tools={
                name: value
                for name, value in assembly["tools"].items()
                if name != "submit_result"
            },
            history=history,
            tool_handler=tool_handler,
            request_budget=payload.get("request_budget"),
            system_prompt=assembly["system_prompt"],
            submit_description=assembly["tools"]["submit_result"]["description"],
        )
    else:
        prompt, schema = prepare_submission(role, frozen)
    response = transport.run(
        prompt,
        transport_config,
        response_schema=schema,
        invocation={"call": payload["call"], "context_ref": payload["context_ref"]},
        **tool_kwargs,
    )
    fields(response, {"output", "evidence_refs"}, "model response")
    submitted = response["output"]
    if role == "knowledge_manager" and draft:
        submitted = submitted.copy()
        if not submitted.get("pages"):
            submitted["pages"] = draft["pages"]
        if submitted.get("index_markdown") is None:
            submitted["index_markdown"] = draft["index_markdown"]
    normalized = validate_output(role, submitted, context)
    if role == "knowledge_manager" and assembly is not None:
        for action, tool in (("pages", "edit_page"), ("index_markdown", "edit_index")):
            effect = (
                normalized.get(action) is not None
                if action == "index_markdown"
                else bool(normalized.get(action))
            )
            if effect:
                if tool not in assembly["tools"] and action not in assembly.get(
                    "direct_actions", []
                ):
                    raise ValueError(
                        f"Knowledge Manager lacks write capability for {action}"
                    )
                if action not in assembly["actions"]:
                    raise ValueError(
                        f"Role knowledge_manager lacks permission for {action}"
                    )
                if (
                    "allowed_actions" in context
                    and action not in context["allowed_actions"]
                ):
                    raise ValueError(
                        f"Role knowledge_manager lacks permission for {action}"
                    )
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
    retrieved = {
        record["result"]["evidence_ref"]
        for record in tool_kwargs.get("history", [])
        if isinstance(record, dict)
        and record.get("event") == "success"
        and record.get("request", {}).get("name") == "read"
        and isinstance(record.get("result"), dict)
        and "evidence_ref" in record["result"]
    }
    if not set(refs) <= set(manifest["allowed_evidence"]) | retrieved:
        raise ValueError("Model cited evidence outside the bound manifest")
    if not set(policy.cited_refs(normalized, context)) <= set(refs):
        raise ValueError("Model omitted typed evidence references from evidence_refs")
    return {"output": normalized, "evidence_refs": refs}


def model_registry(models=None):
    selected = policy.MODELS if models is None else models
    return selected | {model.__name__: model for model in selected.values()}


def validate_roles(study_config, *, models=None, definition_root=None):
    registry = model_registry(models)
    root = definition_root or Path(__file__).resolve().parent
    for role in policy.MODELS:
        value, _ = definition(role, study_config, root, registry)
        unknown = set(value["tools"]) - role_tools.TOOLS.keys()
        if unknown:
            raise ValueError(
                f"Role {role} tools: unknown registered tool {sorted(unknown)}"
            )
        if role != "knowledge_manager" and set(value["tools"]) & {
            "edit_page",
            "edit_index",
        }:
            raise ValueError(
                f"Role {role} tools: draft editing requires Knowledge Manager"
            )


def prepare_agent(payload, config, *, study_config, study_root):
    validate_config(config)
    validate_roles(study_config)
    return assemble(
        payload,
        config,
        study_config=study_config,
        study_root=study_root,
        definition_root=Path(__file__).resolve().parent,
        models=model_registry(),
        prepare_submission=policy.prepare_submission,
        tool_registry=role_tools.TOOLS,
        system_prompt=transport.SYSTEM_PROMPT,
        submit_description=transport.SUBMIT_DESCRIPTION,
    )


def resolve_role(role, study_config):
    validate_roles(study_config)
    return definition(
        role, study_config, Path(__file__).resolve().parent, model_registry()
    )[0]
