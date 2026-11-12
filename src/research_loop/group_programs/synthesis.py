"""Reference cumulative-evidence and Study-disposition Group."""

from copy import deepcopy
import json
import posixpath
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
_STATUS = {
    "type": "string",
    "enum": ["ready", "blocked"],
    "description": "Use ready when analysis can advance, even with informational help. Use blocked for missing inputs, authorization or required correction and explain it in help.",
}
_CRITIC = _schema(
    {
        "summary": _TEXT,
        "verdict": {"type": "string", "enum": ["accept", "revise"]},
        "reason": {"type": "string", "minLength": 1},
        "artifacts": _STRINGS,
    }
)


AUTHOR_INSTRUCTIONS = (
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
    "execution_refs and independent evaluate JSON receipts in analysis_refs. "
    "A receipt with status must be ready; informational help is allowed. Existing "
    "status-less receipts require help=''. Preparation/failure "
    "alone leaves it pending or blocked. Include those references in its evidence. "
    "Use ONLY standardized host execution receipts in execution_refs and independent "
    "evaluation receipts in analysis_refs. When supplied, choose the exact admitted "
    "execution.json and evaluate.json paths allowed by those fields. Auxiliary runner "
    "receipts, logs and result files belong in study_result.evidence or knowledge, "
    "not primary execution_refs. "
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
    "identities and execution; scientific correction must not rerun compute."
)

REVIEW_INSTRUCTIONS = (
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
    "New feedback can resolve the blocked review without redoing completed work."
)

RECOVERY_INSTRUCTIONS = (
    " Recovery-only contract: recovery_evidence is the complete bounded inline "
    "evidence packet; use its contents without opening paths or using tools. "
    "Do not return help solely because tools or paths are unavailable: the packet "
    "contains the required evidence. The formal criterion is already settled by "
    "the completed original Exp. Do not execute compute, recompute metrics, "
    "reinterpret the criterion, or review the whole Exp again. Author only the "
    "scientific Study disposition and knowledge publication delta, preserving "
    "original constraints, negative evidence and limitations. Python binds exact "
    "receipt citations and appends a mechanical provenance footer to substantive "
    "knowledge. For the Author, the compact response schema overrides the generic "
    "citation requirements and full publication output_schema; Python supplies "
    "those publication fields. Omit evidence, artifacts and execution/analysis/direction refs "
    "from your compact Author result. The recovery disposition is goal=achieved, "
    "study_action=stop and stop_basis=goal-satisfied. Only when "
    "study_context.input.human_request is admitted, preserve its identity and "
    "author its status=completed and reason; never invent a human request. "
    "The Critic's full canonical candidate is analysis_record, including "
    "study_result and knowledge_files. Review only this candidate delta against "
    "the packet; do not invent new causal claims or leave scientific authorship "
    "to the Critic. Scientific review may accept or request bounded revision. "
    "Publication paths are literal destinations, never additional evidence. "
    "Author exactly two knowledge_files, each scientific content at most 2400 "
    "characters (the final document with provenance is at most 3200), and "
    "summaries at most 400 characters. Knowledge targets must not overwrite the "
    "reserved recovery-evidence archive or its contents."
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
    packet = assignment.get("recovery_evidence", {})
    recovery = isinstance(packet, dict) and packet.get("kind") == "exp-recovery-v1"
    retry = (
        record
        and (
            record["verdict"] == "revise"
            if name == "critique"
            else record["help"]
            if recovery
            else record["status"] == "blocked"
        )
        and payload.get("feedback_refs")
        and attempts[name] != ctx.round
    )
    if record is None or retry:
        attempts[name] = ctx.round
        record = await ctx.services.call(
            role,
            f"synthesis-{name}-{ctx.round}",
            instructions,
            assignment,
            schema,
            **(
                {
                    "allow_tools": False,
                    "include_role_template": False,
                    "max_output_chars": 12000 if name == "analysis" else 4000,
                }
                if recovery
                else {}
            ),
        )
        if recovery and name == "analysis":
            record = bind_recovery_analysis(record, assignment)
        stages[name] = record
        ctx.save_state()
        _json(ctx, f"reports/history/{ctx.round:06d}-{name}.json", record)
    if name == "critique":
        _validate(record, schema)
        if not record["reason"].strip():
            raise ValueError("Synthesis Critic requires a nonempty reason")
    elif not recovery and (
        record.get("status") not in {"ready", "blocked"}
        or not isinstance(record.get("help"), str)
        or (record["status"] == "blocked" and not record["help"].strip())
    ):
        raise ValueError(
            "Synthesis stage requires ready/blocked status and blocked help"
        )
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
        # Both ordinary and recovery Synthesis read sealed assessments predating
        # explicit stage status, as well as current default Exp receipts.
        ready = (
            evaluation["status"] == "ready"
            if "status" in evaluation
            else evaluation.get("help") == ""
        )
        if not ready or not evaluation.get("summary"):
            raise ValueError("Blocked or missing evaluation cannot complete a request")


def validate_analysis(analysis, result_schema, sources):
    """Validate completion and knowledge delivery without scientific judgment."""
    recovery = sources.get("recovery_evidence") or sources.get("study_context", {}).get(
        "input", {}
    ).get("shared_input", {})
    is_recovery = (
        isinstance(recovery, dict) and recovery.get("kind") == "exp-recovery-v1"
    )
    _validate(
        analysis, analysis_schema(result_schema, recovery=is_recovery, sources=sources)
    )
    result = analysis["study_result"]
    _validate(result, result_schema)
    _completed_request_evidence(result, analysis, sources)
    if result["study_action"] == "stop" and not result["stop_reason"].strip():
        raise ValueError("Study stop requires an explicit Study-level reason")
    if result["study_action"] == "stop":
        if not analysis["stop_basis"]:
            raise ValueError("Study stop requires a declared Study-level basis")
        if analysis["stop_basis"] == "no-useful-action":
            admitted = set(sources.get("input_refs", [])) | set(
                sources.get("feedback_refs", [])
            )
            if not analysis["direction_refs"] or not set(
                analysis["direction_refs"]
            ).issubset(admitted):
                raise ValueError(
                    "No-useful-action stop requires admitted Direction evidence"
                )
    elif analysis["stop_basis"]:
        raise ValueError("A continuing Study cannot declare a stop basis")
    source_names = set()
    targets = set()
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
    if "index.md" not in targets:
        raise ValueError("Synthesis must author a real knowledge index")
    if is_recovery:
        if (
            len(analysis["knowledge_files"]) != 2
            or len(analysis["summary"]) > 400
            or len(result["summary"]) > 400
            or any(
                len(document["content"]) > 3200
                for document in analysis["knowledge_files"]
            )
        ):
            raise ValueError(
                "Recovery requires two bounded knowledge files and concise summaries"
            )
        if any(
            PurePosixPath(target).parts[0] == "recovery-evidence" for target in targets
        ):
            raise ValueError(
                "Recovery knowledge cannot overwrite the recovery-evidence archive"
            )
        refs = {
            recovery.get("execution_ref"),
            recovery.get("evaluation_ref"),
            recovery.get("original_evaluation_ref"),
        }
        admitted = set(sources.get("input_refs", [])) | set(
            sources.get("feedback_refs", [])
        )
        request = sources["study_context"].get("input", {}).get("human_request")
        outcome = result.get("human_request", {})
        if (
            None in refs
            or not refs.issubset(admitted)
            or result["goal"] != "achieved"
            or result["study_action"] != "stop"
            or analysis["stop_basis"] != "goal-satisfied"
            or not refs.issubset(result["evidence"])
            or (
                request
                and (
                    outcome.get("status") != "completed"
                    or not refs.issubset(outcome.get("evidence", []))
                )
            )
            or recovery["execution_ref"] not in analysis["execution_refs"]
            or recovery["evaluation_ref"] not in analysis["analysis_refs"]
        ):
            raise ValueError(
                "Recovery requires completed disposition and exact admitted evidence"
            )
        if not any(
            recovery["original_evaluation_ref"] in document["content"]
            for document in analysis["knowledge_files"]
        ):
            raise ValueError("Recovery knowledge must cite the original evaluation")
    return result


def analysis_schema(result_schema, *, recovery=False, sources=None):
    """Strict Author result contract shared with the recovery Lead baseline."""
    schema = _schema(
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

    if not recovery:
        schema["properties"]["status"] = _STATUS
        schema["required"].append("status")

    if sources and not recovery:
        admitted = dict.fromkeys(
            [*sources.get("input_refs", []), *sources.get("feedback_refs", [])]
        )
        for field, basename in (
            ("execution_refs", "execution.json"),
            ("analysis_refs", "evaluate.json"),
        ):
            choices = [ref for ref in admitted if Path(ref).name == basename]
            if choices:
                schema["properties"][field] = {
                    "type": "array",
                    "items": {**_TEXT, "enum": choices},
                }

    if recovery:
        bounded_result = deepcopy(result_schema)
        bounded_result["properties"]["summary"]["maxLength"] = 400
        schema["properties"]["summary"] = {**_TEXT, "maxLength": 400}
        schema["properties"]["study_result"]["anyOf"][0] = bounded_result
        knowledge = schema["properties"]["knowledge_files"]
        knowledge["maxItems"] = 2
        knowledge["items"]["properties"]["content"] = {**_TEXT, "maxLength": 3200}
    return schema


def recovery_author_schema(result_schema):
    """Scientific Author fields only; receipt identities are bound by Python."""
    schema = analysis_schema(result_schema, recovery=True)
    fields = schema["properties"]
    for field in ("artifacts", "execution_refs", "analysis_refs", "direction_refs"):
        del fields[field]
        schema["required"].remove(field)
    result = fields["study_result"]["anyOf"][0]
    del result["properties"]["evidence"]
    result["required"].remove("evidence")
    if "human_request" in result["properties"]:
        outcome = result["properties"]["human_request"]
        del outcome["properties"]["evidence"]
        outcome["required"].remove("evidence")
    fields["knowledge_files"]["items"]["properties"]["content"]["maxLength"] = 2400
    return schema


def bind_recovery_analysis(candidate, sources):
    """Bind only deterministic citations/provenance, preserving scientific fields."""
    packet = sources.get("recovery_evidence") or sources["study_context"]["input"].get(
        "shared_input", {}
    )
    if not isinstance(packet, dict) or packet.get("kind") != "exp-recovery-v1":
        raise ValueError("Recovery binding requires the admitted evidence packet")
    refs = [
        packet.get(key)
        for key in ("execution_ref", "evaluation_ref", "original_evaluation_ref")
    ]
    if any(not isinstance(ref, str) or not ref for ref in refs):
        raise ValueError("Recovery binding requires exact receipt identities")
    record = deepcopy(candidate)

    def bind(fields, key, expected):
        if key in fields and fields[key] != expected:
            raise ValueError(f"Recovery has contradictory mechanical field: {key}")
        fields[key] = expected

    bind(record, "artifacts", [])
    bind(record, "execution_refs", [packet["execution_ref"]])
    bind(record, "analysis_refs", [packet["evaluation_ref"]])
    bind(record, "direction_refs", [])
    result = record.get("study_result")
    if isinstance(result, dict):
        bind(result, "evidence", refs)
        if "human_request" in result:
            bind(result["human_request"], "evidence", refs)
    output_schema = sources["study_context"]["output_schema"]
    _validate(record, analysis_schema(output_schema, recovery=True))
    if isinstance(result, dict):
        _validate(result, output_schema)
    elif result != "" or not record["help"]:
        raise ValueError("Recovery requires an authored Study result or explicit help")
    documents = record["knowledge_files"]
    if documents:
        archive = packet.get("publication_paths", {}).get(
            packet["original_evaluation_ref"]
        )
        if not isinstance(archive, str) or not archive:
            raise ValueError(
                "Recovery binding requires the original evaluation archive path"
            )
        _relative(archive)
        substantive = next(
            (
                document
                for document in documents
                if document.get("target") != "index.md"
            ),
            None,
        )
        if substantive is None:
            raise ValueError(
                "Recovery requires substantive knowledge alongside its index"
            )
        target = _relative(substantive["target"])
        archive_link = posixpath.relpath(archive, str(target.parent))
        footer = (
            "\n\n<!-- recovery-provenance -->\n"
            "Host-added provenance (receipt identity only; no scientific claim):\n"
            f"Original evaluation: {packet['original_evaluation_ref']}\n"
            f"Archived assessment: [original evaluation]({archive_link})\n"
        )
        for document in documents:
            content = document["content"]
            scientific = content
            if document is substantive:
                if content.endswith(footer):
                    scientific = content[: -len(footer)]
                elif "<!-- recovery-provenance -->" in content:
                    raise ValueError("Recovery has contradictory provenance footer")
                else:
                    document["content"] = content + footer
            if not scientific.strip() or len(scientific) > 2400:
                raise ValueError(
                    "Recovery scientific knowledge content must be nonempty and at most 2400 characters"
                )
            if len(document["content"]) > 3200:
                raise ValueError(
                    "Recovery knowledge including provenance exceeds 3200 characters"
                )
    return record


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
    sources = {
        **payload,
        "study_context": study,
        "work": str(ctx.work),
        "revision": _revise(ctx, payload),
    }
    packet = study.get("input", {}).get("shared_input", {})
    recovery = isinstance(packet, dict) and packet.get("kind") == "exp-recovery-v1"
    author_schema = (
        recovery_author_schema(result_schema)
        if recovery
        else analysis_schema(result_schema, sources=sources)
    )
    if recovery:
        sources["recovery_evidence"] = packet
    analysis = await _stage(
        ctx,
        payload,
        "analysis",
        "analyst",
        AUTHOR_INSTRUCTIONS
        + (
            RECOVERY_INSTRUCTIONS
            if recovery
            else " Return status=ready when analysis can advance, even with informational help. "
            "Return status=blocked with meaningful help and study_result='' when required "
            "evidence, inputs, authorization or corrections are missing."
        ),
        sources,
        author_schema,
    )
    if analysis["help"] if recovery else analysis["status"] == "blocked":
        return _finish(ctx, payload, analysis["summary"], analysis["help"])
    validate_analysis(analysis, result_schema, sources)
    result = analysis["study_result"]
    manifest = []
    extra = []
    candidate_round = ctx.state["synthesis_attempts"]["analysis"]
    for document in analysis["knowledge_files"]:
        source = _relative(document["source"])
        target = _relative(document["target"])
        extra.append(_write(ctx, str(source), document["content"]))
        _write(
            ctx,
            f"reports/candidates/{candidate_round:06d}/{source}",
            document["content"],
        )
        manifest.append({"source": str(source), "target": str(target)})
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
        REVIEW_INSTRUCTIONS + (RECOVERY_INSTRUCTIONS if recovery else ""),
        {
            **sources,
            "analysis": str(ctx.work / "reports/analysis.json"),
            "candidate_artifacts": extra,
            **({"analysis_record": analysis} if recovery else {}),
        },
        _CRITIC,
    )
    if review["verdict"] == "revise":
        return _finish(ctx, payload, review["summary"], review["reason"], extra)
    extra.append(_json(ctx, "study-result.json", result))
    return _finish(ctx, payload, result["summary"], extra=extra)
