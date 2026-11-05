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
    reminder = ""
    if role == "plan":
        science = package["context"][config["scientific_contract_source"]]
        mappings = science.get("execution_interface", {}).get("input_mappings", [])
        reminder = (
            "For Plan, operations and scientific_contract are required sibling fields inside output, "
            "alongside the other required Plan fields; neither belongs at the root or inside the other. "
            "Copy the entire frozen scientific_contract JSON below into output.scientific_contract, preserving every "
            "field and value, including null, list order, numeric types and exact text. Do not omit, add, normalize "
            "or paraphrase anything, even a single protocol word.\nExact frozen scientific_contract:\n"
            + json.dumps(science, ensure_ascii=False)
            + "\nCanonical input mappings:\n"
            + json.dumps(mappings, ensure_ascii=False)
            + "\nEach execution.inputs item has only name and source. source is the selected mapping's canonical source "
            "alias, name is its script-facing name, and mount is its container path for script access. Never put a "
            "mount path in source or add mount to an execution.inputs item. "
            "The root has exactly two sibling keys: output and evidence_refs. evidence_refs must not be inside output. "
            "Inside output, scientific_contract and operations are separate required sibling fields; never put "
            "operations inside scientific_contract. Copy scientific_contract verbatim, including null and all text, "
            "without paraphrasing. "
        )
    return (
        "You are the native script research runtime role "
        + role
        + ". "
        + native.role_instructions(role)
        + "Use only this frozen ContextPackage. No tools, filesystem access or source editing are available. "
        "Preserve the exact scientific contract at context["
        + json.dumps(config["scientific_contract_source"])
        + "]. "
        "Plan may use operations=[] for a qualitative investigation that answers a concrete research question "
        "using available context, with no resource allocation. State that question and the usable context in "
        "experiment_definition; do not invent experiments, measurements or results. If the task requires an actual "
        "diagnostic run, include its bounded script operations. For each operation freeze backend, baseline, execution, inputs, outputs, "
        "authorization, resources, rounded GPU cost and lifecycle timeout. Its operations parameters contain only "
        "baseline_ref and execution. Entrypoint must be a relative research/*.py path, such as research/diagnostic.py, "
        "and gpu=true is required. Declared outputs must be paths relative to /output, such as report.json or "
        "train/summary.json, without an /output/ prefix. Script source may write /output/report.json; that is distinct "
        "from the relative outputs declaration. Rounded allocation is ceil(time_limit_seconds/60)*60 GPU seconds. "
        "cost_bounds uses only that rounded allocation in the configured cost unit, never the longer lifecycle timeout. "
        "Each operation timeout_seconds must be at least rounded allocation plus the headroom at the frozen scientific "
        "contract's execution_interface.lifecycle_headroom_seconds (the configured staging_headroom_seconds). "
        "Plan.timeout_seconds must be at least every operation timeout_seconds. "
        "Do not generate source_files in Plan. Keep local_evaluation exactly as frozen "
        "in the scientific contract (None if absent), never override the fixed global evaluation. "
        "Execute must return operations=[] when accepted Plan operations are empty. Otherwise it copies every "
        "accepted Plan operation exactly and adds only parameters.source_files, each a path/content "
        "text edit within operator scope. Return artifacts=[]; a tool-free model cannot create artifact evidence. "
        "The trusted backend runs the script. Script reports are unverified interpretation evidence, not registered "
        "measurements or Frontier scores. Never fabricate measurements. Preserve conditions, uncertainty and limitations. "
        "For each operation use the selected backend's exact resources map below, with no extra CPU or memory keys; "
        "CPU and memory are hardware settings owned by the operator. cost_bounds must contain exactly the configured "
        "cost unit and the rounded allocation for that operation, not lifecycle timeout or a multiple of candidate count. "
        "For time_limit_seconds=1800, cost is exactly 1800, even when timeout_seconds is larger. "
        "Per-backend public operation contracts:\n"
        + json.dumps(contracts, sort_keys=True)
        + "\nContextPackage:\n"
        + json.dumps(package, ensure_ascii=False)
        + native.response_footer(
            role,
            MODELS[role],
            reminder=reminder,
        )
    )
