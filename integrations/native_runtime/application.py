"""Native role composition: Pi tool submissions and explicit imported cleanup.

This initial application supports qualitative Execute only. It neither edits
source nor submits new training jobs. Importing the module performs no CLI calls.
"""

import base64
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

from integrations.native_runtime import policy, transport
from integrations.native_runtime.filesystem import NATIVE_TOOLS
from research_loop.runtime.role_assembly import assemble, definition
from research_loop.runtime.role_access import check_draft_actions
from research_loop.runtime.context import ContextPackage
from research_loop.runtime.operation_contract import fields, json_value
from research_loop.runtime.model_requests import parse_decision_reference

APPLICATION_IDENTITY = {"id": "native-runtime-v1", "version": "1"}
WRITE_TOOLS = {"edit", "write"}


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


def _collect_draft(draft, submitted, actions, writable):
    changes = policy.DraftSubmission.model_validate(submitted)
    files = {}
    for path in draft.rglob("*"):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError("Only regular files can be published from a private draft")
        if path.is_file():
            name = path.relative_to(draft).as_posix()
            body = path.read_bytes()
            try:
                files[name] = body.decode("utf-8")
            except UnicodeDecodeError:
                files[name] = {"base64": base64.b64encode(body).decode("ascii")}
    paths = set(files).union(changes.remove_paths)
    if paths and not writable:
        raise ValueError("Knowledge publishing requires a writable Manager draft")
    check_draft_actions(paths, actions)
    return changes.model_dump() | {"files": files}


def invoke_agent(payload, transport_config, prepare_submission, validate_output):
    """Bind the role, deliver its result, and check every cited reference."""
    fields(
        payload,
        {"call", "context_ref", "package"}
        | ({"assembly", "request_budget", "knowledge_config"} & payload.keys()),
        "agent invocation",
    )
    role = payload["call"]["role"]
    frozen = ContextPackage.from_dict(payload["package"]).to_dict()
    context, manifest = frozen["context"], frozen["manifest"]
    if manifest["role"] != role:
        raise ValueError("Call role differs from bound context")
    assembly = payload.get("assembly")
    tool_kwargs = {}
    if assembly is not None:
        if (
            assembly["role"] != role
            or assembly["context_ref"] != payload["context_ref"]
        ):
            raise ValueError("Frozen assembly differs from the bound call")
        selected = ContextPackage.from_dict(assembly["package"]).to_dict()
        context, manifest = selected["context"], selected["manifest"]
        prompt, schema = assembly["prompt"], assembly["response_schema"]
        root = payload.get("request_budget", {}).get("study_root")
        if root is None:
            raise ValueError("Prepared role requires its private Study root")
        tool_kwargs = dict(
            tools=[name for name in assembly["tools"] if name != "submit_result"],
            filesystem={**assembly["filesystem"], "write_root": None},
            request_budget=payload.get("request_budget"),
            system_prompt=assembly["system_prompt"],
        )
    else:
        prompt, schema = prepare_submission(role, frozen)
    with ExitStack() as stack:
        draft = None
        if role == "knowledge_manager":
            if assembly is None:
                raise ValueError(
                    "Knowledge maintenance requires a prepared private draft"
                )
            draft = Path(stack.enter_context(TemporaryDirectory(prefix="rl-kb-draft-")))
            if WRITE_TOOLS.intersection(tool_kwargs["tools"]):
                tool_kwargs["filesystem"]["write_root"] = str(draft)
        response = transport.run(
            prompt,
            transport_config,
            response_schema=schema,
            invocation={"call": payload["call"], "context_ref": payload["context_ref"]},
            **tool_kwargs,
        )
        fields(response, {"output", "evidence_refs"}, "model response")
        submitted = response["output"]
        if draft is not None:
            actions = set(assembly["actions"]).intersection(
                context.get("allowed_actions", assembly["actions"])
            )
            submitted = _collect_draft(
                draft,
                submitted,
                actions,
                tool_kwargs["filesystem"]["write_root"] is not None,
            )
    normalized = validate_output(role, submitted, context)
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
    explicit = set(refs) - set(manifest["allowed_evidence"])
    if explicit:
        if assembly is None or not all(ref.startswith("kb:") for ref in explicit):
            raise ValueError("Model cited evidence outside the bound manifest")
        from research_loop.runtime.knowledge import KnowledgeBase

        library = KnowledgeBase(
            root,
            config=payload["knowledge_config"],
            source_scope=assembly["source_scope"],
        )
        for ref in explicit:
            library.authorize_reference(
                parse_decision_reference(ref),
                aliases=assembly["definition"]["knowledge_bases"],
                role=role,
                bindings=assembly["knowledge_libraries"],
            )
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
        unknown = set(value["tools"]) - NATIVE_TOOLS - {"submit_result"}
        if unknown:
            raise ValueError(
                f"Role {role} tools: unknown registered tool {sorted(unknown)}"
            )
        if role != "knowledge_manager" and set(value["tools"]) & WRITE_TOOLS:
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
        system_prompt=transport.SYSTEM_PROMPT,
    )


def resolve_role(role, study_config):
    validate_roles(study_config)
    return definition(
        role, study_config, Path(__file__).resolve().parent, model_registry()
    )[0]
