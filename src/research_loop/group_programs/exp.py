"""New Group integration of H11 eval33; historical benchmark gains are unverified here.

The source COMMON and phase instructions are retained verbatim. The deterministic
adapter supplies Group scope, file handoffs, receipts and bounded recovery.
"""

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
        "help": {"type": "string"},
        "artifacts": {"type": "array", "items": {"type": "string"}},
        "command": {"type": "string"},
    },
    "required": ["summary", "help", "artifacts", "command"],
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
require help, not an invented choice. Feedback resolves the currently blocked phase;
completed computation is never rerun. The host executes only the prepared command
on the explicit execution_backend and supplies its settled receipt.
The assignment's workspace is this Run's work directory. Write a small complete
scientific plan to scientific-plan.md during Plan and return its exact path as
plan_artifact and in artifacts. The complete plan specifies the intended bounded
command and result filename. During Prepare write small implementation/code and
handoff files there and return the exact bounded command and workspace-relative
result_file; do not execute it or create the result file. On help result_file may
be empty.
Evaluate is the independent Critic, including metrics, comparison, analysis and
interpretation. Return its small evaluation report as JSON with claim and
limitations supported by actual evidence; the host writes evaluate.json. Remain
read-only and do not rerun the experiment. There is no extra
Analyst or internal review stage. No benchmark-specific metric or dataset is implied.
Artifacts must be exact existing file paths under workspace, using relative names
where requested by the phase. Do not export canonical data, checkpoints, raw chats
or large outputs. Plan and Evaluate return command as an empty string. For help,
return an empty command and available small artifacts; plan_artifact may be empty.
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
            phases[name]["help"] or (name == "prepare" and not phases[name]["command"])
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
            result = dict(result)
            invalid = [
                ref for ref in result.get("artifacts", []) if not _artifact(ctx, ref)
            ]
            if name == "plan" and not result.get("help"):
                plan_path = _artifact(ctx, result.get("plan_artifact", ""))
                if plan_path is None:
                    invalid.append("plan_artifact")
                elif str(plan_path) not in result["artifacts"]:
                    result["artifacts"].append(str(plan_path))
            if invalid:
                result["help"] = (
                    "Produce existing small artifact files under this Run's work directory: "
                    + ", ".join(invalid)
                )
            if name == "prepare" and not result["help"]:
                ref = result.get("result_file", "")
                if not ref or Path(ref).is_absolute() or ".." in Path(ref).parts:
                    result["help"] = "Supply the plan's workspace-relative result_file."
            if name == "evaluate" and not result["help"]:
                evidence = result.get("evidence", [])
                if assignment["result_file"] not in evidence or any(
                    Path(ref).is_absolute() or not _artifact(ctx, ref)
                    for ref in evidence
                ):
                    result["help"] = (
                        "Provide existing workspace-relative evidence filenames, "
                        "including the supplied result_file."
                    )
            phases[name] = result
            phase_rounds[name] = ctx.round
            ctx.save_state()
        write_json(ctx.work / f"{name}.json", phases[name])
        return phases[name]

    plan = await phase(
        "plan",
        "proposer",
        "plan",
        PLAN_INSTRUCTIONS,
        PLAN_SCHEMA,
        sources,
    )
    if plan["help"]:
        return _finish(ctx, phases, plan["summary"], plan["help"], backend)
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
    if prepare["help"] or not prepare["command"]:
        return _finish(
            ctx,
            phases,
            prepare["summary"],
            prepare["help"] or "Provide one authorized bounded command.",
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
    return _finish(ctx, phases, evaluate["summary"], evaluate["help"], backend)


def _artifact(ctx, ref):
    if not isinstance(ref, str) or not ref:
        return None
    path = Path(ref)
    if not path.is_absolute():
        path = ctx.work / path
    # Artifact publication separately enforces size, regular identity and symlinks.
    # This cheap handoff check rejects missing/escaping reports before another call.
    try:
        if (
            path.resolve(strict=True).is_relative_to(ctx.work.resolve())
            and path.is_file()
        ):
            return path.resolve()
    except (OSError, ValueError):
        pass
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
        },
    )
    artifacts = [str(report), *[str(ctx.work / f"{name}.json") for name in phases]]
    # Keep the verified small raw evidence beside evaluate.json after export.
    evaluation = phases.get("evaluate", {})
    if not evaluation.get("help"):
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
