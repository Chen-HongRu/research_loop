"""The replaceable reference workflow for a complete research Study."""

from pathlib import Path

from .groups import GroupDefinition


def definitions(*, resource=False, recovery=False):
    directory = Path(__file__).with_name("group_programs")
    groups = [
        GroupDefinition(
            "direction",
            "3",
            "Select the next worthwhile research question. Read the goal, admitted human "
            "experiment and published knowledge; explore alternatives when autonomous, "
            "maintain the frontier and export experiment-task.json with scientific constraints. "
            "Does not implement or execute experiments or write a Study conclusion.",
            directory / "direction.py",
        ),
        GroupDefinition(
            "exp",
            "h11-3",
            "Answer one selected experiment question: Plan and Prepare in a shared context, "
            "execute the prepared command once, independently Evaluate its evidence. "
            "Accept experiment-task.json or an explicit scientific task; return experiment "
            "reports or assistance needs, not a Study-level final result.",
            directory / "exp.py",
            {"execution_backend": "resource" if resource else "local"},
        ),
        GroupDefinition(
            "synthesis",
            "7",
            "Integrate completed experimental evidence across the Study. Maintain scientific "
            "knowledge, negative results and uncertainty, obtain independent scrutiny of new "
            "claims, and export study-result.json plus knowledge-manifest.json. Owns the "
            "Study goal/continue/stop and human-request disposition; does not execute experiments.",
            directory / "synthesis.py",
            {"produces_study_result": True},
        ),
        GroupDefinition(
            "diagnosis",
            "2",
            "Diagnose a bounded implementation, tool or environment failure outside another "
            "Group's scope. Export a diagnosis, patch or recovery instructions in its own "
            "workspace. Does not change scientific design or repair/activate the controller.",
            directory / "diagnosis.py",
        ),
    ]
    if recovery:
        return [group for group in groups if group.id == "synthesis"]
    return groups
