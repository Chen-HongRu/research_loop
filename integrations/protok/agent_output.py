"""Model-authored reports, separate from controller-owned execution evidence."""

from research_loop.protocol import validate_response


REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["completed", "incomplete"]},
        "markdown": {"type": "string"},
    },
    "required": ["outcome", "markdown"],
    "additionalProperties": False,
}

REPORT_INSTRUCTIONS = """
Final response contract for this controller overrides earlier Execute output examples:
return ONLY outcome and markdown, matching the supplied report schema. Describe
actual actions, observations, limitations and evidence paths in markdown. Do not
return artifacts, measurements, candidate_id, protocol identifiers or metric fields.
The controller constructs those fields from independently verified receipts, including
the completed/incomplete outcome of a submitted job. Without a submitted job, report
incomplete when the contract could not finish; the controller checks scope and effects.
Reports and precheck files may be cited in markdown; they are not ranked candidates.
For a submitted research script, the entire research/ source tree is immutable:
do not add reports or other files there after submission. Put post-submission
narrative in this final markdown response, not in a new workspace report file.
For Python syntax checks use compile(source_text, filename, "exec") in memory.
Do not run py_compile/compileall or import authored scripts merely to check syntax:
they can create __pycache__/*.pyc, which is outside the permitted source file set.
"""


def report_response(report):
    """Construct an unmeasured envelope; trusted jobs replace it after verification."""
    validate_response("execute", report, schema=REPORT_SCHEMA)
    return {**report, "artifacts": [], "measurements": []}


def receipt_response(trusted, markdown):
    """Construct fields whose authority belongs exclusively to the backend receipt."""
    from .backend import METRIC, PROTOCOL

    completed = trusted["status"] == "completed"
    measurements = []
    if completed and trusted.get("purpose") != "research":
        measurements = [
            {
                "candidate_id": trusted["fingerprint"],
                "artifact": trusted["artifact"],
                "evaluation_id": PROTOCOL,
                "comparison_scope": PROTOCOL,
                "metrics": [{"name": METRIC, "value": trusted["value"]}],
                "evidence": trusted["evidence"],
            }
        ]
    return {
        "outcome": "completed" if completed else "incomplete",
        "markdown": markdown,
        "artifacts": [trusted["artifact"]],
        "measurements": measurements,
    }
