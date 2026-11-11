"""Reference scientific direction Group; its specialists author the science."""

import json
from pathlib import Path


def _schema(properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_TEXT = {"type": "string"}
_STRINGS = {"type": "array", "items": _TEXT}
_BASE = {"summary": _TEXT, "help": _TEXT, "artifacts": _STRINGS}
_SCOUT = _schema(_BASE)
_PROPOSAL = _schema(
    {
        **_BASE,
        "disposition": {
            "type": "string",
            "enum": ["experiment", "no-useful-action", "blocked"],
        },
        "experiment_task_json": _TEXT,
        "frontier_json": _TEXT,
        "consequential_claim": {"type": "boolean"},
    }
)
_TASK_FIELDS = {
    "question",
    "hypothesis",
    "controls",
    "permitted_changes",
    "data_roles",
    "evaluation",
    "inputs",
    "constraints",
    "completion_criteria",
}


def _write(ctx, name, content):
    path = ctx.work / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


def _json(ctx, name, value):
    return _write(ctx, name, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _context(ctx, payload):
    context = payload.get("study_context") or getattr(
        ctx.services, "study_context", None
    )
    if context is None:
        candidate = Path(payload["task_ref"])
        if candidate.suffix == ".json":
            value = json.loads(candidate.read_text())
            if isinstance(value, dict) and "output_schema" in value:
                context = value
    return context or {}


def _revise(ctx, payload):
    stages = ctx.state.setdefault("direction_stages", {})
    attempts = ctx.state.setdefault("direction_attempts", {})
    critique = stages.get("critique")
    if (
        critique
        and critique["help"]
        and payload.get("feedback_refs")
        and attempts["critique"] != ctx.round
    ):
        previous = {}
        for name in ("propose", "critique"):
            if name in stages:
                previous[name] = _json(
                    ctx,
                    f"reports/history/{attempts[name]:06d}-{name}.json",
                    stages.pop(name),
                )
        ctx.state["direction_revision"] = previous
        ctx.state["direction_review_required"] = True
        ctx.save_state()
    return ctx.state.get("direction_revision", {})


async def _stage(ctx, payload, name, role, instructions, assignment, schema):
    stages = ctx.state.setdefault("direction_stages", {})
    attempts = ctx.state.setdefault("direction_attempts", {})
    record = stages.get(name)
    retry = (
        record
        and record["help"]
        and payload.get("feedback_refs")
        and attempts[name] != ctx.round
    )
    if record is None or retry:
        attempts[name] = ctx.round
        record = await ctx.services.call(
            role, f"direction-{name}-{ctx.round}", instructions, assignment, schema
        )
        stages[name] = record
        ctx.save_state()
        _json(ctx, f"reports/history/{ctx.round:06d}-{name}.json", record)
    _json(ctx, f"reports/{name}.json", record)
    return record


def _finish(ctx, summary, help_text="", extra=()):
    artifacts = [*extra]
    for name, stage in ctx.state.get("direction_stages", {}).items():
        artifacts.append(str(ctx.work / f"reports/{name}.json"))
        for ref in stage.get("artifacts", []):
            path = Path(ref)
            if not path.is_absolute():
                path = ctx.work / path
            if not path.is_relative_to(ctx.work) or ".." in path.parts:
                raise ValueError("Direction artifacts must be inside this Run's work")
            artifacts.append(str(path))
    return {
        "summary": summary,
        "completion": "blocked" if help_text else "completed",
        "unfinished": [help_text] if help_text else [],
        "help": help_text,
        "artifacts": list(dict.fromkeys(artifacts)),
    }


async def run(ctx, payload):
    """Select the next budget-worthy question, without generating its full Plan."""
    study = _context(ctx, payload)
    inputs = study.get("input", {})
    task_path = Path(payload["task_ref"])
    task = {}
    if task_path.suffix == ".json":
        value = json.loads(task_path.read_text())
        if isinstance(value, dict):
            task = value
    explicit = bool(inputs.get("human_request")) or task.get("mode") == (
        "explicit_experiment"
    )
    sources = {
        **payload,
        "study_context": study,
        "work": str(ctx.work),
        "explicit_experiment": explicit,
        "revision": _revise(ctx, payload),
    }
    if not explicit:
        scout = await _stage(
            ctx,
            payload,
            "scout",
            "scout",
            "Independently explore the next question worth spending the authorized "
            "budget on. Read goal/task, published knowledge index and relevant actual "
            "positive AND negative/history evidence. Find discriminating alternatives "
            "to extending the incumbent; no exploration quota. Preserve stable "
            "hypothesis IDs and cite inspected evidence, uncertainty and estimated "
            "cost. Write a concise report inside work and return its artifact path. "
            "Do not read chats or pretend recalled literature was retrieved. Return "
            "help for evidence or authorization missing outside this scope.",
            sources,
            _SCOUT,
        )
        if scout["help"]:
            return _finish(ctx, scout["summary"], scout["help"])
        sources["scout_report"] = str(ctx.work / "reports/scout.json")
    proposal = await _stage(
        ctx,
        payload,
        "propose",
        "proposer",
        "Own selection of the next scientific question and its scientific constraints. "
        "Read the goal, task, knowledge index, relevant positive AND negative actual "
        "history and the independent Scout report when supplied. An admitted human "
        "experiment has priority: confirm its scope and boundaries directly without "
        "requiring unrelated alternatives or Scout exploration. Otherwise choose a "
        "useful discriminating experiment from the frontier, explaining why its "
        "information is worth its cost. Preserve stable hypothesis IDs. Return "
        "frontier_json as a JSON object with hypotheses array; every hypothesis has "
        "id, evidence, cost, status and disposition, plus actual execution references "
        "where available. Preserve prior attempts, negative evidence and reasons for "
        "disposition. For disposition=experiment return experiment_task_json as an "
        "actual JSON object containing question, hypothesis, controls, permitted_changes, "
        "data_roles, evaluation, inputs, constraints, completion_criteria. These are "
        "scientific constraints, not a duplicate execution Plan; Exp owns concrete Plan "
        "and Prepare. Include model/loss/masks/time/state/evaluation/resource boundaries "
        "where relevant. Do not fill scientific gaps with null: return help instead. "
        "For no-useful-action give concrete Study-wide frontier/evidence reasons, not "
        "merely one failed branch, and leave experiment_task_json empty. Set "
        "consequential_claim=true for material direction claims or no-useful-action; "
        "a fresh Critic will review that judgment. Return no scientific conclusion "
        "based only on command success. You may write small supporting work reports.",
        # Revision inputs are scientific artifacts, never prior role conversations.
        {
            **sources,
            "revision_instruction": "When revision is supplied, revise your prior candidate using its report, the rejected critique and new feedback. Stay within Direction scope; preserve completed Scout evidence and actual experiment identities.",
        },
        _PROPOSAL,
    )
    if proposal["help"] or proposal["disposition"] == "blocked":
        return _finish(
            ctx, proposal["summary"], proposal["help"] or "Direction remains blocked"
        )
    frontier = json.loads(proposal["frontier_json"])
    if not isinstance(frontier, dict) or not isinstance(
        frontier.get("hypotheses"), list
    ):
        raise ValueError("Direction requires an authored hypothesis frontier")
    ids = []
    for hypothesis in frontier["hypotheses"]:
        if not isinstance(hypothesis, dict) or not {
            "id",
            "evidence",
            "cost",
            "status",
            "disposition",
        }.issubset(hypothesis):
            raise ValueError(
                "Frontier hypothesis lacks its identity/evidence/disposition"
            )
        if not isinstance(hypothesis["id"], str) or not hypothesis["id"].strip():
            raise ValueError("Frontier hypothesis requires a stable nonempty ID")
        ids.append(hypothesis["id"])
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate frontier hypothesis identity")
    extra = [_json(ctx, "frontier.json", frontier)]
    candidate_round = ctx.state["direction_attempts"]["propose"]
    _json(ctx, f"reports/candidates/{candidate_round:06d}/frontier.json", frontier)
    if proposal["disposition"] == "experiment":
        experiment = json.loads(proposal["experiment_task_json"])
        if (
            not isinstance(experiment, dict)
            or not _TASK_FIELDS.issubset(experiment)
            or any(experiment[key] is None for key in _TASK_FIELDS)
        ):
            raise ValueError("Direction requires a complete authored experiment task")
        extra.append(_json(ctx, "experiment-task.json", experiment))
        _json(
            ctx,
            f"reports/candidates/{candidate_round:06d}/experiment-task.json",
            experiment,
        )
    else:
        disposition = {
            "disposition": proposal["disposition"],
            "reason": proposal["summary"],
        }
        extra.append(
            _json(
                ctx,
                "direction-disposition.json",
                disposition,
            )
        )
        _json(
            ctx,
            f"reports/candidates/{candidate_round:06d}/direction-disposition.json",
            disposition,
        )
    if (
        ctx.state.get("direction_review_required")
        or proposal["consequential_claim"]
        or proposal["disposition"] == "no-useful-action"
    ):
        critique = await _stage(
            ctx,
            payload,
            "critique",
            "critic",
            "Independently review only the new consequential direction judgment, "
            "frontier disposition and scientific constraints using source evidence. "
            "Do not repeat experiment execution review. Check stable hypothesis IDs, "
            "negative/history evidence, human priority, discriminatory value and cost. "
            "A branch abandonment is not no useful Study action. Stay read-only; "
            "return help for unsupported claims or needed scientific revision, with "
            "bounded evidence requests rather than inventing a replacement design. "
            "New feedback may resolve the blocked judgment; preserve completed work.",
            {**sources, "direction_artifacts": extra},
            _SCOUT,
        )
        if critique["help"]:
            return _finish(ctx, critique["summary"], critique["help"], extra)
    return _finish(ctx, proposal["summary"], extra=extra)
