"""Typed script roles over frozen native context; task science stays in inputs."""

from copy import deepcopy
import json
from typing import Annotated, Literal

from pydantic import Field

from integrations.native_runtime import policy as native
from integrations.script_runtime import backend
from research_loop.runtime.operation_contract import json_value, validate_execution


class Artifact(native.StrictModel):
    path: native.Text
    version: native.Text


class ScriptInput(native.StrictModel):
    name: native.Text
    source: native.Text


class Execution(native.StrictModel):
    mode: Literal["script"]
    entrypoint: native.Text
    gpu: bool
    time_limit_seconds: Annotated[int, Field(ge=1, le=1800)]
    inputs: list[ScriptInput]
    outputs: list[native.Text]


class PlannedParameters(native.StrictModel):
    baseline_ref: native.Text
    execution: Execution


class SourceFile(native.StrictModel):
    path: native.Text
    content: str


class Parameters(PlannedParameters):
    source_files: list[SourceFile]


class PlannedOperation(native.StrictModel):
    id: native.Text
    backend: native.Text
    authorization_id: native.Text
    resources: dict[str, int]
    cost_bounds: dict[str, int]
    timeout_seconds: Annotated[int, Field(gt=0)]
    input_artifacts: list[Artifact]
    parameters: PlannedParameters


class Operation(PlannedOperation):
    parameters: Parameters


class Plan(native.Plan):
    baseline_ref: native.Text
    backend: native.Text
    scientific_contract: dict
    operations: list[PlannedOperation]


class Execute(native.StrictModel):
    operations: list[Operation]
    artifacts: Annotated[list[Artifact], Field(max_length=0)]


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
    if not _same(value["scientific_contract"], contract):
        raise ValueError("Plan changed the frozen scientific contract")
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
    plan = _validate_plan(context.get("plan"), context, config)
    if [_planned(item) for item in value["operations"]] != plan["operations"]:
        raise ValueError("Execute must match accepted Plan except source_files")
    for item in value["operations"]:
        backend.validate_request(
            item, config["backend_handlers"][item["backend"]]["config"]
        )
    validate_execution(value["operations"], plan, context)
    return value


def prompt(role, package, config):
    if role not in {"plan", "execute"}:
        return (
            "Backend sources whose context keys start evidence: are unverified script reports. "
            "They may support interpretation knowledge only, never external/local_measurement knowledge, "
            "registered measurements or Frontier scores.\n"
            + native.prompt(role, package)
        )
    return (
        "You are the native script research runtime role " + role + ". "
        "Use only this frozen ContextPackage. No tools, filesystem access or source editing are available. "
        "Preserve the exact scientific contract at context["
        + json.dumps(config["scientific_contract_source"])
        + "]. "
        "Plan may use operations=[] for a qualitative investigation that answers a concrete research question "
        "using available context, with no resource allocation. State that question and the usable context in "
        "experiment_definition; do not invent experiments, measurements or results. If the task requires an actual "
        "diagnostic run, include its bounded script operations. For each operation freeze backend, baseline, execution, inputs, outputs, "
        "authorization, resources, rounded GPU cost and lifecycle timeout. Its operations parameters contain only "
        "baseline_ref and execution. Do not generate source_files in Plan. Keep local_evaluation exactly as frozen "
        "in the scientific contract (None if absent), never override the fixed global evaluation. "
        "Execute must return operations=[] when accepted Plan operations are empty. Otherwise it copies every "
        "accepted Plan operation exactly and adds only parameters.source_files, each a path/content "
        "text edit within operator scope. Return artifacts=[]; a tool-free model cannot create artifact evidence. "
        "The trusted backend runs the script. Script reports are unverified interpretation evidence, not registered "
        "measurements or Frontier scores. Never fabricate measurements. Preserve conditions, uncertainty and limitations. "
        "Return exactly one JSON object with output and evidence_refs. Cite only actually used manifest.allowed_evidence "
        "references. Output schema:\n"
        + json.dumps(MODELS[role].model_json_schema())
        + "\nContextPackage:\n"
        + json.dumps(package, ensure_ascii=False)
    )
