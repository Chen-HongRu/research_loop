"""Study result and priority experiment contracts."""

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "goal": {"type": "string", "enum": ["achieved", "not-achieved", "unknown"]},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "limitations": {"type": "array", "items": {"type": "string"}},
        "next_action": {"type": "string"},
        "study_action": {"type": "string", "enum": ["continue", "stop"]},
        "stop_reason": {"type": "string"},
    },
    "required": [
        "summary",
        "goal",
        "evidence",
        "limitations",
        "next_action",
        "study_action",
        "stop_reason",
    ],
    "additionalProperties": False,
}


def validate_result(request, result):
    if not request:
        return
    outcome = result.get("human_request", {})
    if (
        not isinstance(outcome, dict)
        or outcome.get("id") != request["id"]
        or outcome.get("status") not in {"completed", "pending", "blocked"}
        or not isinstance(outcome.get("reason"), str)
        or not outcome["reason"].strip()
        or not isinstance(outcome.get("evidence"), list)
        or not all(isinstance(item, str) for item in outcome["evidence"])
    ):
        raise ValueError("Result requires a matching human request disposition")
    if outcome["status"] == "completed" and not outcome["evidence"]:
        raise ValueError("Completed human request requires evidence references")
    if outcome["status"] == "blocked" and result.get("study_action") != "stop":
        raise ValueError("Blocked human request must stop for user intervention")


def result_schema(base, request):
    if not request:
        return base
    return {
        **base,
        "properties": {
            **base["properties"],
            "human_request": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "enum": [request["id"]]},
                    "status": {
                        "type": "string",
                        "enum": ["completed", "pending", "blocked"],
                    },
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
                "required": ["id", "status", "evidence", "reason"],
                "additionalProperties": False,
            },
        },
        "required": [*base["required"], "human_request"],
    }


STAGE_STATUS = {
    "type": "string",
    "enum": ["ready", "blocked"],
    "description": "Use ready when the stage can advance, even with informational help. Use blocked for missing inputs, authorization or required correction and explain it in help.",
}


def validate_stage(record, scope):
    if (
        not isinstance(record, dict)
        or not isinstance(record.get("status"), str)
        or record["status"] not in {"ready", "blocked"}
        or not isinstance(record.get("help"), str)
        or (record["status"] == "blocked" and not record["help"].strip())
    ):
        raise ValueError(f"{scope} requires ready/blocked status and blocked help")
