"""Typed script roles over frozen native context; task science stays in inputs."""

from copy import deepcopy
import json
from typing import Annotated, Literal

from pydantic import Field

from integrations.native_runtime import policy as native
from integrations.script_runtime import backend
from research_loop.runtime.operation_contract import json_value, validate_execution


class Artifact(native.StrictModel):
    path: native.Text = Field(description="Artifact path from the frozen context.")
    version: native.Text = Field(
        description="Exact version of the referenced artifact."
    )


class ScriptInput(native.StrictModel):
    name: native.Text = Field(
        description="Script-facing input name from the canonical mapping."
    )
    source: native.Text = Field(
        description="Canonical source alias from the input mappings. The container mount path is provided separately for script access."
    )


class Execution(native.StrictModel):
    mode: Literal["script"] = Field(
        description="Execute a script through the trusted backend."
    )
    entrypoint: native.Text = Field(
        description="Relative research/*.py entrypoint. Plan may name a file for Execute to create."
    )
    gpu: bool = Field(description="Must be true for this one-GPU backend.")
    time_limit_seconds: Annotated[int, Field(ge=1, le=1800)] = Field(
        description="GPU runtime limit in seconds, between 1 and 1800."
    )
    inputs: list[ScriptInput] = Field(
        description="Explicit canonical input mappings. Each source and name must be unique."
    )
    outputs: list[native.Text] = Field(
        description="Unique artifact paths relative to /output, such as report.json or train/summary.json. Source code writes these files under /output."
    )


class PlannedParameters(native.StrictModel):
    baseline_ref: native.Text = Field(
        description="Exact frozen source baseline reference."
    )
    execution: Execution = Field(
        description="Frozen script execution settings approved by Plan."
    )


class SourceFile(native.StrictModel):
    path: native.Text = Field(
        description="Relative text file path within the operator's editable scope."
    )
    content: str = Field(description="Complete proposed text content for this file.")


class Parameters(PlannedParameters):
    source_files: list[SourceFile] = Field(
        description="Source proposals within operator scope. Execute adds only these files to the accepted operation parameters."
    )


class PlannedOperation(native.StrictModel):
    id: native.Text = Field(description="Logical operation ID unique within this Plan.")
    backend: native.Text = Field(
        description="Configured script backend name matching the Plan."
    )
    authorization_id: native.Text = Field(
        description="ID of the visible operation grant authorizing this backend and allocation."
    )
    resources: dict[str, int] = Field(
        description="Exact resources map from the selected public backend contract. CPU and memory are operator settings."
    )
    cost_bounds: dict[str, int] = Field(
        description="Exact configured cost-unit map with ceil(time_limit_seconds/60)*60 GPU seconds. Lifecycle headroom and candidate count do not increase this allocation."
    )
    timeout_seconds: Annotated[int, Field(gt=0)] = Field(
        description="Lifecycle timeout in seconds, at least the rounded GPU allocation plus the backend staging headroom."
    )
    input_artifacts: list[Artifact] = Field(
        description="Versioned artifact references used by this operation."
    )
    parameters: PlannedParameters = Field(
        description="Frozen baseline and execution settings. Plan supplies these without source files."
    )


class Operation(PlannedOperation):
    parameters: Parameters = Field(
        description="Accepted Plan parameters with only source_files added."
    )


class Plan(native.Plan):
    baseline_ref: native.Text = Field(
        description="Exact baseline reference from the frozen scientific contract."
    )
    backend: native.Text = Field(
        description="Configured script backend used by every operation."
    )
    operations: list[PlannedOperation] = Field(
        description="Bounded script operations for this investigation. Use an empty list for a qualitative question answerable from available context. Include operations when the task requires a diagnostic run."
    )
    local_evaluation: dict | None = Field(
        description="Exact local_evaluation from the frozen scientific contract, or null when absent."
    )
    timeout_seconds: Annotated[int, Field(gt=0)] = Field(
        description="Investigation lifetime in seconds, at least every operation timeout_seconds."
    )


class Execute(native.StrictModel):
    operations: list[Operation] = Field(
        description="Copy every accepted Plan operation exactly, adding only parameters.source_files. Return an empty list when the accepted Plan has no operations."
    )
    artifacts: Annotated[list[Artifact], Field(max_length=0)] = Field(
        description="Return an empty list. The trusted backend creates artifacts after execution."
    )


MODELS = native.MODELS | {"plan": Plan, "execute": Execute}
cited_refs = native.cited_refs


def _same(left, right):
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def _planned(request):
    value = deepcopy(request)
    del value["parameters"]["source_files"]
    return value


def _validate_plan(output, context, config):
    value = Plan.model_validate(json_value(output)).model_dump()
    source = config["scientific_contract_source"]
    contract = context.get(source)
    if (
        not isinstance(contract, dict)
        or not {"baseline_ref", "global_evaluation"} <= contract.keys()
    ):
        raise ValueError("Frozen scientific contract is absent or incomplete")
    if not _same(contract["global_evaluation"], context.get("global_evaluation")):
        raise ValueError("Scientific contract differs from fixed global evaluation")
    if not _same(value["local_evaluation"], contract.get("local_evaluation")):
        raise ValueError("Plan changed the frozen local evaluation")
    native.validate_output(
        "plan",
        {
            key: value[key]
            for key in ("timeout_seconds", "local_evaluation", "experiment_definition")
        },
        context,
    )
    handler = config["backend_handlers"].get(value["backend"])
    if handler is None:
        raise ValueError("Plan names an unconfigured script backend")
    if (
        value["baseline_ref"] != contract["baseline_ref"]
        or value["baseline_ref"] != handler["config"]["baseline_ref"]
    ):
        raise ValueError("Plan baseline differs from the frozen source")
    requests = []
    for item in value["operations"]:
        if (
            item["backend"] != value["backend"]
            or item["parameters"]["baseline_ref"] != value["baseline_ref"]
        ):
            raise ValueError("Planned operations changed the backend or baseline")
        request = deepcopy(item)
        request["parameters"]["source_files"] = []
        backend.validate_request(request, handler["config"])
        requests.append(request)
    # Core owns resource grants and absolute deadline clamping, not model prose.
    validate_execution(requests, value, context)
    return value


def validate_output(role, output, context, config):
    if role == "plan":
        return _validate_plan(output, context, config)
    if role != "execute":
        return native.validate_output(role, output, context)
    value = Execute.model_validate(json_value(output)).model_dump()
    accepted = context.get("plan")
    if isinstance(accepted, dict) and "scientific_contract" in accepted:
        # Previously accepted Plans carry a copy of the frozen operator input.
        accepted = accepted.copy()
        if not _same(
            accepted.pop("scientific_contract"),
            context.get(config["scientific_contract_source"]),
        ):
            raise ValueError(
                "Accepted Plan differs from the frozen scientific contract"
            )
    plan = _validate_plan(accepted, context, config)
    if [_planned(item) for item in value["operations"]] != plan["operations"]:
        raise ValueError("Execute must match accepted Plan except source_files")
    for item in value["operations"]:
        if (
            "allowed_actions" in context
            and "source_edits" not in context["allowed_actions"]
            and item["parameters"]["source_files"]
        ):
            raise ValueError("Execute lacks permission for source_edits")
        backend.validate_request(
            item, config["backend_handlers"][item["backend"]]["config"]
        )
    validate_execution(value["operations"], plan, context)
    return value


def prompt(role, package, config):
    return prepare_submission(role, package, config)[0]


def prepare_submission(role, package, config):
    if role not in {"plan", "execute"}:
        message, schema = native.prepare_submission(role, package)
        return (
            "Backend sources whose context keys start evidence: are unverified script reports. "
            "They may support interpretation knowledge only, never external/local_measurement knowledge, "
            "registered measurements or Frontier scores.\n" + message,
            schema,
        )
    schema = native.response_schema(MODELS[role])
    contracts = public_backend_contracts(config)
    reminder = ""
    if role == "plan":
        science = package["context"][config["scientific_contract_source"]]
        mappings = science.get("execution_interface", {}).get("input_mappings", [])
        reminder = (
            "Canonical input mappings:\n"
            + json.dumps(mappings, ensure_ascii=False)
            + "\n"
        )

    return (
        "You are the native script research runtime role "
        + role
        + ". "
        + native.role_instructions(role)
        + "Use only this frozen ContextPackage. Use only the tools in the frozen role grant. Filesystem access and source editing are disabled. "
        "Preserve the exact scientific contract at context["
        + json.dumps(config["scientific_contract_source"])
        + "]. "
        "The trusted backend runs the script. Script reports are unverified interpretation evidence, not registered "
        "measurements or Frontier scores. Never fabricate measurements. Preserve conditions, uncertainty and limitations. "
        "Per-backend public operation contracts:\n"
        + json.dumps(contracts, sort_keys=True)
        + "\nContextPackage:\n"
        + json.dumps(package, ensure_ascii=False)
        + native.response_footer(role, schema, reminder=reminder),
        schema,
    )


def public_backend_contracts(config):
    contracts = {}
    for name, handler in config["backend_handlers"].items():
        settings = handler["config"]
        contracts[name] = {
            "resources": {settings["resource_pool"]: 1},
            "cost_bounds": {settings["cost_unit"]: "ceil(time_limit_seconds/60)*60"},
            "staging_headroom_seconds": settings["staging_headroom_seconds"],
            "example": {
                "time_limit_seconds": 1800,
                "cost_bounds": {settings["cost_unit"]: 1800},
                "minimum_timeout_seconds": 1800 + settings["staging_headroom_seconds"],
            },
        }
    return contracts
