"""Reference cumulative-evidence and Study-disposition Group."""

import json
from pathlib import Path, PurePosixPath


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
_CRITIC = _schema(
    {
        "summary": _TEXT,
        "verdict": {"type": "string", "enum": ["accept", "revise"]},
        "reason": {"type": "string", "minLength": 1},
        "artifacts": _STRINGS,
    }
)


def _write(ctx, name, content):
    path = ctx.work / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


def _json(ctx, name, value):
    return _write(ctx, name, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _validate(value, schema):
    """Validate the small strict result schema, never inspect research datasets."""
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise ValueError("Study result must match its object schema")
        properties = schema.get("properties", {})
        if not set(schema.get("required", [])).issubset(value) or (
            schema.get("additionalProperties") is False and set(value) - set(properties)
        ):
            raise ValueError("Study result fields do not match the admitted schema")
        for key in value:
            if key in properties:
                _validate(value[key], properties[key])
    elif kind == "array":
        if not isinstance(value, list):
            raise ValueError("Study result requires an array")
        for item in value:
            _validate(item, schema["items"])
    elif kind == "string" and not isinstance(value, str):
        raise ValueError("Study result requires a string")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("Study result value is outside the admitted schema")


def _context(ctx, payload):
    study = payload.get("study_context") or getattr(ctx.services, "study_context", None)
    if study is None and Path(payload["task_ref"]).suffix == ".json":
        value = json.loads(Path(payload["task_ref"]).read_text())
        if isinstance(value, dict) and "output_schema" in value:
            study = value
    return study or {}


def _revise(ctx, payload):
    stages = ctx.state.setdefault("synthesis_stages", {})
    attempts = ctx.state.setdefault("synthesis_attempts", {})
    critique = stages.get("critique")
    if (
        critique
        and critique["verdict"] == "revise"
        and payload.get("feedback_refs")
        and attempts["critique"] != ctx.round
    ):
        previous = {}
        for name in ("analysis", "critique"):
            if name in stages:
                previous[name] = _json(
                    ctx,
                    f"reports/history/{attempts[name]:06d}-{name}.json",
                    stages.pop(name),
                )
        ctx.state["synthesis_revision"] = previous
        ctx.save_state()
    return ctx.state.get("synthesis_revision", {})


async def _stage(ctx, payload, name, role, instructions, assignment, schema):
    stages = ctx.state.setdefault("synthesis_stages", {})
    attempts = ctx.state.setdefault("synthesis_attempts", {})
    record = stages.get(name)
    retry = (
        record
        and (record["verdict"] == "revise" if name == "critique" else record["help"])
        and payload.get("feedback_refs")
        and attempts[name] != ctx.round
    )
    if record is None or retry:
        attempts[name] = ctx.round
        record = await ctx.services.call(
            role, f"synthesis-{name}-{ctx.round}", instructions, assignment, schema
        )
        stages[name] = record
        ctx.save_state()
        _json(ctx, f"reports/history/{ctx.round:06d}-{name}.json", record)
    if name == "critique":
        _validate(record, schema)
        if not record["reason"].strip():
            raise ValueError("Synthesis Critic requires a nonempty reason")
    _json(ctx, f"reports/{name}.json", record)
    return record


def _finish(ctx, payload, summary, help_text="", extra=()):
    artifacts = list(extra)
    admitted = {
        payload.get("task_ref"),
        *payload.get("input_refs", []),
        *payload.get("feedback_refs", []),
    }
    for name, record in ctx.state.get("synthesis_stages", {}).items():
        artifacts.append(str(ctx.work / f"reports/{name}.json"))
        for index, ref in enumerate(record.get("artifacts", [])):
            path = Path(ref)
            if not path.is_absolute():
                path = ctx.work / path
            if str(path) in admitted:
                # Input citations stay in the role report; they are not new outputs.
                continue
            if not path.is_relative_to(ctx.work) or ".." in path.parts:
                raise ValueError(
                    f"Synthesis {name}.artifacts[{index}] is neither ownwork nor "
                    f"an explicitly admitted input: {ref}"
                )
            artifacts.append(str(path))
    return {
        "summary": summary,
        "completion": "blocked" if help_text else "completed",
        "unfinished": [help_text] if help_text else [],
        "help": help_text,
        "artifacts": list(dict.fromkeys(artifacts)),
    }


def _relative(name):
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or path == PurePosixPath(".")
    ):
        raise ValueError("Knowledge source and target must be relative file paths")
    return path


def _completed_request_evidence(result, analysis, sources):
    request = sources["study_context"].get("input", {}).get("human_request")
    if not request:
        return
    outcome = result.get("human_request", {})
    if outcome.get("id") != request["id"]:
        raise ValueError("Study result must retain the admitted human request identity")
    if not outcome.get("reason", "").strip():
        raise ValueError("Human request requires an explicit disposition reason")
    if outcome.get("status") == "blocked" and result["study_action"] != "stop":
        raise ValueError("Blocked human request requires Study intervention")
    if outcome.get("status") != "completed":
        return
    if not outcome.get("evidence"):
        raise ValueError("Completed human request requires actual evidence")
    executions = analysis["execution_refs"]
    analyses = analysis["analysis_refs"]
    admitted = set(sources.get("input_refs", [])) | set(
        sources.get("feedback_refs", [])
    )
    if (
        not executions
        or not analyses
        or not set(executions + analyses).issubset(admitted)
    ):
        raise ValueError(
            "Human completion requires admitted execution and analysis artifacts"
        )
    if not set(executions + analyses).issubset(outcome["evidence"]):
        raise ValueError(
            "Human completion evidence must cite the actual execution and analysis"
        )
    # Read only explicit small execution receipts, not model/data/checkpoint contents.
    for ref in executions:
        path = Path(ref)
        if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("Human completion requires a small execution receipt")
        receipt = json.loads(path.read_text())
        if (
            receipt.get("exit_code") != 0
            or receipt.get("running")
            or receipt.get("unknown")
        ):
            raise ValueError(
                "Preparation or failed/unknown execution cannot complete a request"
            )
    for ref in analyses:
        path = Path(ref)
        if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError(
                "Human completion requires a small independent evaluation receipt"
            )
        evaluation = json.loads(path.read_text())
        if evaluation.get("help") != "" or not evaluation.get("summary"):
            raise ValueError("Blocked or missing evaluation cannot complete a request")


async def run(ctx, payload):
    study = _context(ctx, payload)
    if "output_schema" not in study:
        return _finish(
            ctx,
            payload,
            "Study result contract unavailable",
            "Supply study_context.output_schema",
        )
    result_schema = study["output_schema"]
    analysis_schema = _schema(
        {
            **_BASE,
            "study_result": {
                "anyOf": [result_schema, {"type": "string", "enum": [""]}]
            },
            "knowledge_files": {
                "type": "array",
                "items": _schema({"source": _TEXT, "target": _TEXT, "content": _TEXT}),
            },
            "execution_refs": _STRINGS,
            "analysis_refs": _STRINGS,
            "direction_refs": _STRINGS,
            "stop_basis": {
                "type": "string",
                "enum": [
                    "",
                    "goal-satisfied",
                    "authorization-exhausted",
                    "user-stop",
                    "external-blocker",
                    "no-useful-action",
                ],
            },
        }
    )
    sources = {
        **payload,
        "study_context": study,
        "work": str(ctx.work),
        "revision": _revise(ctx, payload),
    }
    analysis = await _stage(
        ctx,
        payload,
        "analysis",
        "analyst",
        "Own what the cumulative evidence supports and the next Study action. Read "
        "the goal, admitted human request, published knowledge index, relevant "
        "positive AND negative history/frontier, direction judgments and new Exp "
        "evaluation/execution artifacts. Synthesize across experiments, separating "
        "observations, reviewed findings, inference, negative results and limitations. "
        "Produce study_result matching exactly the supplied output_schema. Goal "
        "achievement and study_action continue/stop are separate. A completed Group, "
        "prepared command, failed execution, or branch abandonment does not finish "
        "the Study. Stop only for overall goal satisfaction, authorization exhaustion, "
        "user stop, a real external blocker, or no useful justified action; the last "
        "requires supplied Direction frontier judgment/evidence in direction_refs. "
        "Set stop_basis to goal-satisfied, authorization-exhausted, user-stop, "
        "external-blocker or no-useful-action for stop, and '' for continue. "
        "For continue define the next falsifiable action. A completed human_request "
        "must match its admitted ID and be supported by actual successful execution "
        "AND independent analysis: cite admitted small JSON exit_code=0 receipts in "
        "execution_refs and independent evaluate JSON receipts (help='') in analysis_refs. Preparation/failure "
        "alone leaves it pending or blocked. Include those references in its evidence. "
        "Return knowledge_files [{source,target,content}] containing actual scientific "
        "text, including target=index.md and substantive findings/frontier as needed. "
        "Use relative paths, unique source basenames, source under knowledge/. Merge "
        "the existing index references; preserve historical/negative evidence and "
        "limitations. Do not erase prior history or claim it validates new behavior. "
        "Python installs these files verbatim, never fills in scientific content. "
        "Return help with study_result='' if required evidence lies outside scope, "
        "rather than fabricating result fields. Do not read raw agent chats. "
        "Return artifacts only for new files inside this Run's work; admitted input "
        "paths are evidence citations, not new outputs. Use study_result.evidence, "
        "execution_refs, analysis_refs and direction_refs for those citations. "
        "When revision is supplied, revise your prior candidate and knowledge text "
        "using the rejected critique and new feedback. Preserve completed experiment "
        "identities and execution; scientific correction must not rerun compute.",
        sources,
        analysis_schema,
    )
    if analysis["help"]:
        return _finish(ctx, payload, analysis["summary"], analysis["help"])
    result = analysis["study_result"]
    _validate(result, result_schema)
    _completed_request_evidence(result, analysis, sources)
    if result["study_action"] == "stop" and not result["stop_reason"].strip():
        raise ValueError("Study stop requires an explicit Study-level reason")
    if result["study_action"] == "stop":
        if not analysis["stop_basis"]:
            raise ValueError("Study stop requires a declared Study-level basis")
        if analysis["stop_basis"] == "no-useful-action":
            admitted = set(payload.get("input_refs", [])) | set(
                payload.get("feedback_refs", [])
            )
            if not analysis["direction_refs"] or not set(
                analysis["direction_refs"]
            ).issubset(admitted):
                raise ValueError(
                    "No-useful-action stop requires admitted Direction evidence"
                )
    elif analysis["stop_basis"]:
        raise ValueError("A continuing Study cannot declare a stop basis")
    manifest = []
    extra = []
    source_names = set()
    targets = set()
    candidate_round = ctx.state["synthesis_attempts"]["analysis"]
    for document in analysis["knowledge_files"]:
        source = _relative(document["source"])
        target = _relative(document["target"])
        if (
            source.parts[0] != "knowledge"
            or source.name in source_names
            or str(target) in targets
            or not document["content"].strip()
        ):
            raise ValueError(
                "Knowledge files require unique, nonempty explicit outputs"
            )
        source_names.add(source.name)
        targets.add(str(target))
        extra.append(_write(ctx, str(source), document["content"]))
        _write(
            ctx,
            f"reports/candidates/{candidate_round:06d}/{source}",
            document["content"],
        )
        manifest.append({"source": str(source), "target": str(target)})
    if "index.md" not in targets:
        raise ValueError("Synthesis must author a real knowledge index")
    extra.append(_json(ctx, "knowledge-manifest.json", {"files": manifest}))
    candidate = _json(ctx, "reports/study-result-candidate.json", result)
    _json(
        ctx,
        f"reports/candidates/{candidate_round:06d}/study-result-candidate.json",
        result,
    )
    extra.append(candidate)
    review = await _stage(
        ctx,
        payload,
        "critique",
        "critic",
        "Fresh independent review of NEW cross-experiment conclusions, cumulative "
        "knowledge/index and Study disposition only. Do not repeat the full review "
        "of an individual experiment. Read actual cited positive/negative evidence, "
        "frontier and Direction judgments. Check goal/action separation, uncertainty, "
        "historical index references and new claims. No-useful-action stop requires "
        "Direction evidence covering useful alternatives; one failed branch or "
        "finished experiment does not imply Study stop. Check human_request identity "
        "and successful actual execution plus independent analysis before completed. "
        "Respect a user scope explicitly limited to a single task. Stay read-only. "
        "Return artifacts only for this Run's own work files; admitted input "
        "references are evidence citations in your assessment, not new outputs. "
        "Return verdict='accept' when the candidate is supported and ready, with "
        "a nonempty reason explaining acceptance and evidence reviewed. Acceptance "
        "explanation is not an assistance request. Return verdict='revise' for "
        "unjustified claims/disposition or missing evidence, with a nonempty reason "
        "specifying the bounded correction or evidence needed. "
        "New feedback can resolve the blocked review without redoing completed work.",
        {
            **sources,
            "analysis": str(ctx.work / "reports/analysis.json"),
            "candidate_artifacts": extra,
        },
        _CRITIC,
    )
    if review["verdict"] == "revise":
        return _finish(ctx, payload, review["summary"], review["reason"], extra)
    extra.append(_json(ctx, "study-result.json", result))
    return _finish(ctx, payload, result["summary"], extra=extra)
