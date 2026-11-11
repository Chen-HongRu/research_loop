"""A bounded experiment adapter; the supplied plan owns the science."""

import json
from pathlib import Path

from research_loop.state import write_json

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


async def run(ctx, payload):
    settings = ctx.definition.settings
    plan = payload.get("task_ref") or settings.get("plan")
    if not plan or not Path(plan).is_file():
        return _output(
            "Missing scientific plan", "Provide an existing scientific plan file."
        )
    # Results are committed by phase; a resumed GroupRun does not redo completed work.
    phases = ctx.state.setdefault("experiment_phases", {})
    sources = {
        "plan": str(Path(plan).resolve()),
        "work": str(ctx.work),
        "input_refs": payload.get("input_refs", []),
        "feedback_refs": payload.get("feedback_refs", []),
        "prompt": payload.get("prompt", ""),
    }
    phase_rounds = ctx.state.setdefault("experiment_phase_rounds", {})

    async def phase(name, role, context, instructions, payload):
        blocked = name in phases and (
            phases[name]["help"] or (name == "prepare" and not phases[name]["command"])
        )
        retry = (
            blocked and sources["feedback_refs"] and phase_rounds.get(name) != ctx.round
        )
        if name not in phases or retry:
            phases[name] = await ctx.services.call(
                role,
                context if role == "experimenter" else f"{context}-{ctx.round}",
                instructions,
                payload,
                PHASE_SCHEMA,
            )
            phase_rounds[name] = ctx.round
            ctx.save_state()
        write_json(ctx.work / f"{name}.json", phases[name])
        return phases[name]

    prepare = await phase(
        "prepare",
        "experimenter",
        "implementation",
        "Read the supplied external scientific plan. Resolve only implementation "
        "details, then implement in work and prepare one authorized bounded command. "
        "Do not run that command yourself. Preserve the plan's scientific choices. "
        "If a scientific choice, authorization or input is missing, return help and "
        "an empty command. Artifacts must be exact file paths to small code/report files "
        "under work, suitable for delivery to a diagnostic group.",
        sources,
    )
    if prepare["help"] or not prepare["command"]:
        return _finish(
            ctx, phases, prepare["summary"], prepare["help"] or "No prepared command."
        )
    if "execution" not in phases:
        phases["execution"] = await ctx.services.execute_prepared(
            prepare["command"],
            operation_id="prepared-experiment",
            backend=settings.get("execution_backend", "resource"),
        )
        ctx.save_state()
    write_json(ctx.work / "execution.json", phases["execution"])
    if phases["execution"].get("exit_code") != 0:
        return _finish(
            ctx,
            phases,
            "Prepared command failed",
            "Execution failed. This Run will not repeat execution. Diagnose the exported "
            "code and receipt, close this Run, then create a separate repair Run with "
            "an explicit approved plan; preserve the original scientific contract.",
        )
    analysis = await phase(
        "analysis",
        "analyst",
        "analysis",
        "Independently read the plan, implementation handoff, command receipt and "
        "actual artifacts. Interpret only observed results; preserve negative and "
        "inconclusive evidence. Return assistance needs for missing scientific evidence. "
        "Do not run commands or edit implementation. Return command as empty string.",
        {
            **sources,
            "prepare": str(ctx.work / "prepare.json"),
            "execution": str(ctx.work / "execution.json"),
        },
    )
    if analysis["help"]:
        return _finish(ctx, phases, analysis["summary"], analysis["help"])
    if settings.get("critic", True):
        critique = await phase(
            "critique",
            "critic",
            "critique",
            "Independently inspect the scientific plan, actual artifacts and analysis. "
            "Look for unsupported claims, confounds, protocol drift and missing controls. "
            "Stay read-only; request bounded assistance rather than silently redesigning "
            "the experiment. Return command as empty string.",
            {
                **sources,
                "prepare": str(ctx.work / "prepare.json"),
                "execution": str(ctx.work / "execution.json"),
                "analysis": str(ctx.work / "analysis.json"),
            },
        )
        if critique["help"]:
            return _finish(ctx, phases, critique["summary"], critique["help"])
    return _finish(ctx, phases, analysis["summary"], "")


def _output(summary, help_text, artifacts=None):
    return {
        "summary": summary,
        "completion": "blocked" if help_text else "completed",
        "unfinished": [help_text] if help_text else [],
        "help": help_text,
        "artifacts": artifacts or [],
    }


def _finish(ctx, phases, summary, help_text):
    # Router exports reference only small reports under this run's own work path.
    report = ctx.work / "report.json"
    report.write_text(
        json.dumps(
            {"summary": summary, "help": help_text, "phases": list(phases)},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    artifacts = [str(report), *[str(ctx.work / f"{name}.json") for name in phases]]
    for phase in phases.values():
        for ref in phase.get("artifacts", []):
            path = Path(ref)
            if not path.is_absolute():
                path = ctx.work / path
            if (
                path.is_relative_to(ctx.work)
                and ".." not in path.parts
                and str(path) not in artifacts
            ):
                artifacts.append(str(path))
    # The runtime validates size, regular-file identity and symlinks at publication.
    return _output(summary, help_text, artifacts)
