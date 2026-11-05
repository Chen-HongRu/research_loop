"""Native Pi role composition with an explicitly configured script backend."""

from functools import partial
from pathlib import Path
import re

from integrations.native_runtime import transport
from integrations.script_runtime import backend, policy
from integrations.native_runtime import application as native_application
from integrations.native_runtime.application import invoke_agent
from integrations.native_runtime import role_tools
from research_loop.runtime.role_assembly import assemble
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
    return invoke_agent(
        payload,
        config["transport"],
        partial(policy.prepare_submission, config=config),
        partial(policy.validate_output, config=config),
    )


def _definition_roots():
    return [
        Path(__file__).resolve().parent,
        Path(native_application.__file__).resolve().parent,
    ]


def _models():
    return native_application.model_registry(policy.MODELS) | {
        "ScriptPlan": policy.Plan,
        "ScriptExecute": policy.Execute,
    }


def validate_roles(study_config):
    return native_application.validate_roles(
        study_config, models=_models(), definition_root=_definition_roots()
    )


def prepare_agent(payload, config, *, study_config, study_root):
    validate_config(config)
    validate_roles(study_config)
    return assemble(
        payload,
        config,
        study_config=study_config,
        study_root=study_root,
        definition_root=_definition_roots(),
        models=_models(),
        prepare_submission=partial(policy.prepare_submission, config=config),
        tool_registry=role_tools.TOOLS,
        system_prompt=transport.SYSTEM_PROMPT,
        submit_description=transport.SUBMIT_DESCRIPTION,
        input_values={"backend_contracts": policy.public_backend_contracts(config)}
        if payload["call"]["role"] in {"plan", "execute"}
        else None,
    )


def resolve_role(role, study_config):
    from research_loop.runtime.role_assembly import definition

    validate_roles(study_config)
    return definition(
        role,
        study_config,
        _definition_roots(),
        _models(),
    )[0]
