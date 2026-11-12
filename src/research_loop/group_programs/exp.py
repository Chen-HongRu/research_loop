"""New Group integration of H11 eval33; historical benchmark gains are unverified here.

The source COMMON and phase instructions are retained verbatim. The deterministic
adapter supplies Group scope, file handoffs, receipts and bounded recovery.
"""

from copy import deepcopy
from pathlib import Path

from research_loop.state import write_json

COMMON = (
    "You are one specialist in a bounded, coordinator-free benchmark.\n"
    "A deterministic host transfers artifacts and executes the prepared command.\n"
    "Use only the supplied task, source pack and artifacts. No delegation, web,\n"
    "network, GPU, package installation or access to other runs or hidden checks.\n"
    "Preserve data roles and inputs. Do not change the task or claim unseen results.\n"
    "Return the requested JSON schema. Keep your conclusion within the task scope.\n"
)

PLAN_INSTRUCTIONS = "Design this bounded experiment: hypothesis, matched control, permitted change, evaluation rule and outcome interpretations. Do not implement or run the experiment. Work efficiently: inspect only files needed for this phase, batch independent file inspections into as few tool calls as practical, and avoid repeated reads or redundant checks. Preserve every requested requirement and verify the facts needed for your output. When the supplied task identifies files to inspect, read those files directly from the supplied workspace; discover files only when needed. Express each required quantitative transformation with explicit operators, operands and order; preserve the supplied constants and data roles, and flag uncertainty rather than infer missing details."

PREPARE_INSTRUCTIONS = "Implement the supplied plan in the candidate workspace. You may syntax-check only; do not run the substantive experiment or create its result file. Return the exact supplied command, smoke evidence and artifact paths. Work efficiently: inspect only files needed for this phase, batch independent file inspections into as few tool calls as practical, and avoid repeated reads or redundant checks. Preserve every requested requirement and verify the facts needed for your output."

EVALUATE_INSTRUCTIONS = "Read the supplied plan, execution receipt and actual result. Independently compute the metrics and return the bounded conclusion and limitations. Each evidence entry must be an exact workspace-relative filename only, without explanation or sentences; include the supplied result_file. Remain read-only and do not rerun the experiment. Work efficiently: inspect only files needed for this phase, batch independent file inspections into as few tool calls as practical, and avoid repeated reads or redundant checks. Preserve every requested requirement and verify the facts needed for your output. For workspace-relative artifact reads, run commands with the supplied workspace as the working directory."

PHASE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "status": {
            "type": "string",
            "enum": ["ready", "blocked"],
            "description": "Use ready when this phase can advance, even with informational help. Use blocked for missing inputs, authorization or required correction and explain it in help.",
        },
        "help": {"type": "string"},
        "artifacts": {"type": "array", "items": {"type": "string"}},
        "command": {"type": "string"},
    },
    "required": ["summary", "status", "help", "artifacts", "command"],
    "additionalProperties": False,
}
PLAN_SCHEMA = {
    **PHASE_SCHEMA,
    "properties": {
        **PHASE_SCHEMA["properties"],
        "plan_artifact": {"type": "string"},
    },
    "required": [*PHASE_SCHEMA["required"], "plan_artifact"],
}
PREPARE_SCHEMA = {
    **PHASE_SCHEMA,
    "properties": {
        **PHASE_SCHEMA["properties"],
        "result_file": {"type": "string"},
    },
    "required": [*PHASE_SCHEMA["required"], "result_file"],
}
EVALUATE_SCHEMA = {
    **PHASE_SCHEMA,
    "properties": {
        **PHASE_SCHEMA["properties"],
        "claim": {"type": "string"},
        "limitations": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": [*PHASE_SCHEMA["required"], "claim", "limitations", "evidence"],
}

GROUP_CONTRACT = """This is a new integrated Group version of the H11 workflow.
The task_ref is the selected scientific question and frozen constraints, or the
user's complete task file. Read it and admitted input_refs directly; JSON tasks
may name question, hypothesis, controls, permitted_changes, data_roles, evaluation,
inputs, constraints and completion_criteria. Markdown tasks are equally valid.
Do not select another research question or change any explicit upstream choice.
Plan develops one complete experiment within those constraints; Prepare implements
that plan. Missing external scientific decisions, evidence, authorization or inputs
require status=blocked and meaningful help, not an invented choice. Use status=ready
when the phase can advance; informational help is retained without blocking.
Feedback resolves the currently blocked phase;
completed computation is never rerun. The host executes only the prepared command
on the explicit execution_backend and supplies its settled receipt.
The assignment's workspace is this Run's work directory. Write a small complete
scientific plan to scientific-plan.md during Plan and return its exact path as
plan_artifact and in artifacts. The complete plan specifies the intended bounded
command and result filename. During Prepare write small implementation/code and
handoff files there and return the exact bounded command and workspace-relative
result_file; do not execute it or create the result file. Only when status=blocked
may result_file be empty.
Evaluate is the independent Critic, including metrics, comparison, analysis and
interpretation. Return its small evaluation report as JSON with claim and
limitations supported by actual evidence; the host writes evaluate.json. Remain
read-only and do not rerun the experiment. There is no extra
Analyst or internal review stage. No benchmark-specific metric or dataset is implied.
Artifacts must be exact existing file paths under workspace, using relative names
where requested by the phase. Do not export canonical data, checkpoints, raw chats
or large outputs. Plan and Evaluate return command as an empty string. When
status=blocked, return an empty command and available small artifacts;
plan_artifact may be empty. When status=ready, all required phase artifacts,
Prepare's command/result_file and Evaluate's evidence remain mandatory even if
help contains an informational note.
"""


async def run(ctx, payload):
    task_ref = payload.get("task_ref")
    if not task_ref or not Path(task_ref).is_file():
        return _output(
            "Missing experiment task", "Provide an existing complete task file."
        )
    backend = ctx.definition.settings.get("execution_backend")
    if backend not in {"local", "resource"}:
        return _output(
            "Missing execution authorization",
            "Set execution_backend explicitly to local or resource for this Group.",
        )
    phases = ctx.state.setdefault("experiment_phases", {})
    phase_rounds = ctx.state.setdefault("experiment_phase_rounds", {})
    validation = ctx.state.setdefault("experiment_phase_validation", {})
    history = ctx.state.setdefault("experiment_phase_history", {})
    sources = {
        "task_ref": str(Path(task_ref).resolve()),
        "workspace": str(ctx.work),
        "input_refs": payload.get("input_refs", []),
        "feedback_refs": payload.get("feedback_refs", []),
        "prompt": payload.get("prompt", ""),
        "execution_backend": backend,
        "group_contract": GROUP_CONTRACT,
    }

    async def phase(name, role, context, instructions, schema, assignment):
        blocked = name in phases and (
            phases[name]["status"] == "blocked"
            or (name == "prepare" and not phases[name]["command"])
        )
        retry = (
            blocked and sources["feedback_refs"] and phase_rounds.get(name) != ctx.round
        )
        if name not in phases or retry:
            result = await ctx.services.call(
                role,
                context,
                instructions,
                assignment,
                schema,
                common=COMMON,
                include_role_template=False,
                cwd=ctx.work,
            )
            history.setdefault(name, []).append(
                {"round": ctx.round, "result": deepcopy(result)}
            )
            phases[name] = deepcopy(result)
            phase_rounds[name] = ctx.round
        # Boundary repairs never turn an independent assessment into a new model
        # task. Recheck the original receipt, preserving scientific help verbatim.
        original = history.get(name, [])
        record = original[-1]["result"] if original else phases[name]
        phases[name], validation[name] = _normalize_phase(ctx, name, record, assignment)
        validation[name]["round"] = phase_rounds.get(name, ctx.round)
        ctx.save_state()
        write_json(ctx.work / f"{name}.json", phases[name])
        return phases[name]

    def phase_help(name, result):
        errors = validation[name]["errors"]
        return (result["help"] if result["status"] == "blocked" else "") or (
            "Artifact boundary requires mechanical repair: "
            + "; ".join(f"{error['field']}: {error['reason']}" for error in errors)
            if errors
            else ""
        )

    plan = await phase(
        "plan",
        "proposer",
        "plan",
        PLAN_INSTRUCTIONS,
        PLAN_SCHEMA,
        sources,
    )
    if phase_help("plan", plan):
        return _finish(ctx, phases, plan["summary"], phase_help("plan", plan), backend)
    prepare = await phase(
        "prepare",
        "experimenter",
        "plan",
        PREPARE_INSTRUCTIONS,
        PREPARE_SCHEMA,
        {
            **sources,
            "plan_ref": str(ctx.work / "plan.json"),
            "plan_artifact": plan["plan_artifact"],
        },
    )
    if phase_help("prepare", prepare) or not prepare["command"]:
        return _finish(
            ctx,
            phases,
            prepare["summary"],
            phase_help("prepare", prepare) or "Provide one authorized bounded command.",
            backend,
        )
    if "execution" not in phases:
        # The service durably admits this identity before dispatch. Unknown effects
        # raise and require reconciliation; they never become a retryable phase.
        receipt = await ctx.services.execute_prepared(
            prepare["command"],
            operation_id="prepared-experiment",
            backend=backend,
        )
        phases["execution"] = {**receipt, "backend": backend}
        ctx.save_state()
    write_json(ctx.work / "execution.json", phases["execution"])
    if phases["execution"].get("exit_code") != 0:
        return _finish(
            ctx,
            phases,
            "Prepared command failed",
            "Execution failed. This Run will not repeat execution. Diagnose the exported "
            "code and receipt, close this Run, then create a separate repair Run with "
            "an explicit approved task; preserve the original scientific contract.",
            backend,
        )
    evaluate = await phase(
        "evaluate",
        "critic",
        "critic",
        EVALUATE_INSTRUCTIONS,
        EVALUATE_SCHEMA,
        {
            **sources,
            "plan_ref": str(ctx.work / "plan.json"),
            "plan_artifact": plan["plan_artifact"],
            "prepare_ref": str(ctx.work / "prepare.json"),
            "execution_ref": str(ctx.work / "execution.json"),
            "result_file": prepare["result_file"],
        },
    )
    return _finish(
        ctx, phases, evaluate["summary"], phase_help("evaluate", evaluate), backend
    )


def _reference(ctx, ref, admitted=(), *, existing=True):
    """Classify exact file references without reading scientific input contents."""
    if not isinstance(ref, str) or not ref:
        raise ValueError("expected a nonempty file reference")
    path = Path(ref)
    path = path if path.is_absolute() else ctx.work / path
    resolved = path.resolve()
    admitted = {str(Path(value).resolve()) for value in admitted if value}
    if str(resolved) in admitted:
        return str(resolved), True
    work = ctx.work.resolve()
    if not resolved.is_relative_to(work) or ".." in path.parts:
        raise ValueError("not ownwork or an explicitly admitted input")
    relative = path.relative_to(ctx.work)
    cursor = ctx.work
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError("ownwork references may not contain symlinks")
    if existing and not resolved.is_file():
        raise ValueError("ownwork file does not exist")
    if resolved == work:
        raise ValueError("expected a file inside ownwork")
    return resolved.relative_to(work).as_posix(), False


def _normalize_phase(ctx, name, record, assignment):
    if (
        record.get("status") not in {"ready", "blocked"}
        or not isinstance(record.get("help"), str)
        or (record["status"] == "blocked" and not record["help"].strip())
    ):
        raise ValueError("Exp phase requires ready/blocked status and blocked help")
    result = deepcopy(record)
    errors, citations = [], []
    admitted = [
        assignment.get("task_ref"),
        *assignment.get("input_refs", []),
        *assignment.get("feedback_refs", []),
    ]

    def normalize(ref, field, *, existing=True, inputs=True):
        try:
            value, citation = _reference(
                ctx, ref, admitted if inputs else (), existing=existing
            )
            if citation:
                citations.append(value)
            return value, citation
        except (OSError, ValueError) as error:
            errors.append({"field": field, "reason": str(error)})
            return ref, False

    outputs = []
    for index, ref in enumerate(result.get("artifacts", [])):
        value, citation = normalize(ref, f"{name}.artifacts[{index}]")
        if not citation:
            outputs.append(value)
    result["artifacts"] = list(dict.fromkeys(outputs))
    if result["status"] == "ready":
        if name == "plan":
            value, _ = normalize(
                result.get("plan_artifact", ""), "plan.plan_artifact", inputs=False
            )
            result["plan_artifact"] = value
            if not errors and value not in result["artifacts"]:
                result["artifacts"].append(value)
        if name == "prepare":
            result["result_file"], _ = normalize(
                result.get("result_file", ""),
                "prepare.result_file",
                existing=False,
                inputs=False,
            )
        if name == "evaluate":
            result["evidence"] = [
                normalize(ref, f"evaluate.evidence[{index}]")[0]
                for index, ref in enumerate(result.get("evidence", []))
            ]
            if assignment["result_file"] not in result["evidence"]:
                errors.append(
                    {
                        "field": "evaluate.evidence",
                        "reason": "missing supplied result_file",
                    }
                )
    return result, {
        "origin": "host-boundary-validation",
        "scientific_help": record["help"],
        "errors": errors,
        "input_citations": list(dict.fromkeys(citations)),
    }


def _artifact(ctx, ref):
    try:
        relative, _ = _reference(ctx, ref)
        return (ctx.work / relative).resolve()
    except (OSError, ValueError):
        return None


def _output(summary, help_text, artifacts=None):
    return {
        "summary": summary,
        "completion": "blocked" if help_text else "completed",
        "unfinished": [help_text] if help_text else [],
        "help": help_text,
        "artifacts": artifacts or [],
    }


def _finish(ctx, phases, summary, help_text, backend):
    report = ctx.work / "report.json"
    write_json(
        report,
        {
            "summary": summary,
            "help": help_text,
            "phases": list(phases),
            "execution_backend": backend,
            "integration": "h11-eval33-group-v1; historical gains unverified",
            "phase_validation": ctx.state.get("experiment_phase_validation", {}),
        },
    )
    artifacts = [str(report), *[str(ctx.work / f"{name}.json") for name in phases]]
    # Keep the verified small raw evidence beside evaluate.json after export.
    evaluation = phases.get("evaluate", {})
    if evaluation.get("status") == "ready":
        for ref in evaluation.get("evidence", []):
            path = _artifact(ctx, ref)
            if path is not None and str(path) not in artifacts:
                artifacts.append(str(path))
    for result in phases.values():
        for ref in result.get("artifacts", []):
            path = _artifact(ctx, ref)
            if path is not None and str(path) not in artifacts:
                artifacts.append(str(path))
    return _output(summary, help_text, artifacts)
