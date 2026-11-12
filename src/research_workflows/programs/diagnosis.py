"""Reference tool/environment/implementation diagnosis Group, not a science solver."""

import json
from pathlib import Path

from research_workflows.programs.tracing import references, step
from research_workflows.contracts import STAGE_STATUS, validate_stage


_TEXT = {"type": "string"}
_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": _TEXT,
        "status": STAGE_STATUS,
        "help": _TEXT,
        "artifacts": {"type": "array", "items": _TEXT},
        "diagnosis": _TEXT,
        "review_required": {"type": "boolean"},
    },
    "required": [
        "summary",
        "status",
        "help",
        "artifacts",
        "diagnosis",
        "review_required",
    ],
    "additionalProperties": False,
}


async def run(ctx, payload):
    stages = ctx.state.setdefault("diagnosis_stages", {})
    rounds = ctx.state.setdefault("diagnosis_rounds", {})
    critique = stages.get("critique")
    if (
        critique
        and critique["status"] == "blocked"
        and payload.get("feedback_refs")
        and rounds["critique"] != ctx.round
    ):
        previous = {}
        for name in ("diagnose", "critique"):
            if name in stages:
                path = ctx.work / "reports/history" / f"{rounds[name]:06d}-{name}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(stages.pop(name), ensure_ascii=False, indent=2) + "\n"
                )
                previous[name] = str(path)
        ctx.state["diagnosis_revision"] = previous
        ctx.state["diagnosis_review_required"] = True
        ctx.save_state()
    sources = {
        **payload,
        "work": str(ctx.work),
        "study_context": ctx.state.setdefault(
            "research_context", payload.get("study_context", {})
        ),
        "revision": ctx.state.get("diagnosis_revision", {}),
    }

    async def stage(name, instructions, assignment):
        record = stages.get(name)
        retry = (
            record
            and record["status"] == "blocked"
            and payload.get("feedback_refs")
            and rounds[name] != ctx.round
        )
        if record is None or retry:
            rounds[name] = ctx.round
            with step(
                ctx,
                f"diagnosis.{name}",
                references(assignment),
                after=("diagnosis.diagnose",) if name == "critique" else (),
            ) as step_id:
                record = await ctx.services.call(
                    name,
                    f"diagnosis-{name}-{ctx.round}",
                    instructions,
                    assignment,
                    _SCHEMA,
                    step_id=step_id,
                )
            stages[name] = record
            ctx.save_state()
            history = ctx.work / "reports/history" / f"{ctx.round:06d}-{name}.json"
            history.parent.mkdir(parents=True, exist_ok=True)
            history.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        validate_stage(record, "Diagnosis stage")
        path = ctx.work / "reports" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        return record

    def finish(summary, help_text=""):
        artifacts = [str(ctx.work / "diagnosis.md")]
        for name, record in stages.items():
            artifacts.append(str(ctx.work / "reports" / f"{name}.json"))
            for ref in record.get("artifacts", []):
                path = Path(ref)
                if not path.is_absolute():
                    path = ctx.work / path
                if not path.is_relative_to(ctx.work) or ".." in path.parts:
                    raise ValueError(
                        "Diagnosis artifacts must be inside this Run's work"
                    )
                artifacts.append(str(path))
        with step(
            ctx,
            "diagnosis.boundary",
            list(dict.fromkeys(artifacts)),
            after=("diagnosis.diagnose", "diagnosis.critique"),
            completion="blocked" if help_text else "completed",
            waiting_for_assistance=bool(help_text),
        ):
            pass
        return {
            "summary": summary,
            "completion": "blocked" if help_text else "completed",
            "unfinished": [help_text] if help_text else [],
            "help": help_text,
            "artifacts": list(dict.fromkeys(artifacts)),
        }

    diagnosis = await stage(
        "diagnose",
        "Diagnose only the supplied tool, environment or implementation fault. "
        "Read the exported failure receipt, code and scientific constraints. Return "
        "observed cause, evidence, smallest correction, recovery steps and unknowns "
        "in diagnosis. Any patch files or reproduction scripts must be small and "
        "written only inside this Run's work; return their exact paths. Never edit "
        "the original Run, controller/runtime, canonical inputs/checkpoints, or "
        "execute/restart the experiment; preserve operation identity and reconcile "
        "unknown effects. Do not expand authorization or bypass host resource tools. "
        "Scientific design/evidence disputes require status=blocked and meaningful help from Direction, not a "
        "diagnostic replacement experiment. Set review_required=true when the fix "
        "could affect the scientific contract, permissions, recovery or shared "
        "state; request external maintenance for a controller defect. When revision "
        "is supplied, correct your prior diagnosis/patch based on the rejected "
        "critique and feedback, within this same scope. Return status=ready when diagnosis can advance even with informational help. Do not rerun experiments.",
        sources,
    )
    (ctx.work / "diagnosis.md").write_text(diagnosis["diagnosis"], encoding="utf-8")
    candidate = (
        ctx.work / "reports/candidates" / f"{rounds['diagnose']:06d}-diagnosis.md"
    )
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(diagnosis["diagnosis"], encoding="utf-8")
    if diagnosis["status"] == "blocked":
        return finish(diagnosis["summary"], diagnosis["help"])
    if not diagnosis["diagnosis"].strip():
        raise ValueError("Diagnosis requires an authored report")
    if ctx.state.get("diagnosis_review_required") or diagnosis["review_required"]:
        critique = await stage(
            "critique",
            "Independently check the diagnosis, proposed patch scope and recovery "
            "instructions against the actual failure and scientific contract. "
            "Stay read-only. Check that the proposal does not alter experiment "
            "meaning, edit original Runs/controller, widen permissions, duplicate "
            "an unknown execution or pretend a diagnostic run completes science. "
            "Scientific disputes require Direction assistance. Return status=blocked with meaningful help for "
            "unsafe or unsupported repairs; otherwise return status=ready even with informational help. Diagnosis can summarize your assessment.",
            {
                **sources,
                "diagnosis_ref": str(ctx.work / "diagnosis.md"),
                "diagnosis_artifacts": diagnosis["artifacts"],
            },
        )
        if critique["status"] == "blocked":
            return finish(critique["summary"], critique["help"])
    return finish(diagnosis["summary"])
