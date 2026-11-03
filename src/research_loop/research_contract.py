"""Opt-in executable bounds; scientific design lives in frozen Plan markdown."""

import copy
from pathlib import PurePosixPath
import re

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .schema import OutputModel, OutputSchema, with_fields


class ResearchInput(OutputModel):
    name: str
    source: str


class ExecutionEnvelope(OutputModel):
    mode: Literal["fixed_evaluation", "script"]
    entrypoint: str | None
    gpu: bool
    time_limit_seconds: int
    inputs: list[ResearchInput]
    outputs: list[str]


class ResearchExecution(ExecutionEnvelope):
    time_limit_seconds: Annotated[int, Field(ge=60, le=1800, multiple_of=60)]

    @model_validator(mode="after")
    def executable_bounds(self):
        seconds = self.time_limit_seconds
        names = []
        for item in self.inputs:
            if (
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", item.name)
                or ".." in item.name
            ):
                raise ValueError(
                    "Research input name must be a simple name without traversal"
                )
            if not item.source.strip():
                raise ValueError("Empty research input source")
            names.append(item.name)
        if len(set(names)) != len(names):
            raise ValueError("Duplicate research input name")
        for output in self.outputs:
            _relative_file(output)
        if len(set(self.outputs)) != len(self.outputs):
            raise ValueError("Duplicate research output")
        if self.mode == "script":
            if self.entrypoint is None:
                raise ValueError("Script research needs an entrypoint")
            path = _relative_file(self.entrypoint)
            if (
                len(path.parts) < 2
                or path.parts[0] != "research"
                or path.suffix != ".py"
            ):
                raise ValueError(
                    "Script entrypoint must be a Python file under research/"
                )
            if not self.outputs:
                raise ValueError("Script research needs declared output evidence files")
        elif (
            self.entrypoint is not None
            or self.gpu is not True
            or seconds != 1800
            or self.inputs
            or self.outputs
        ):
            raise ValueError(
                "Fixed evaluation uses its original GPU/1800s profile and no script inputs/outputs"
            )
        return self


EXECUTION = OutputSchema(ResearchExecution)


def validate_research_execution(value):
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"version"}
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise ValueError("research_execution must be {'version': 1}")
    return copy.deepcopy(value)


def plan_schema(schema):
    return with_fields(schema, execution=ResearchExecution | None)


def _relative_file(value):
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise ValueError(f"Expected a relative file without traversal: {value!r}")
    return path


def validate_contract(plan):
    """Validate the same executable model emitted to the research agent."""
    if plan["decision"] == "investigate":
        EXECUTION.validate(plan.get("execution"))


def guidance(phase):
    common = """
This Study enables frozen research contracts. Training and model architecture
changes are optional. A useful intervention may be a measurement design, analysis,
controlled ablation, derivation or observation using existing artifacts.
"""
    if phase == "proposal":
        return (
            common
            + """
When research_design is requested, its legacy structural_change field may describe
an experimental intervention or measurement design without a model change.
"""
        )
    if phase == "plan":
        return (
            common
            + """
Write the scientific plan in markdown: describe the question, allowed action,
relevant inputs and versions, measurement or derivation, and how evidence will be
interpreted. Explain uncertainty and limitations where relevant. No research type,
explanation IDs or observation IDs are required. Freeze this prose before Execute.
When live telemetry is available, state the observable names, steps/units, conditions,
publication boundaries and intended tags. Use dashboard only for user-facing progress;
retain diagnostic/custom tags for Review. Log tags do not change the frozen scientific
scope, data roles, required evidence or interpretation of incomplete measurements.
execution selects fixed_evaluation or script. A script uses research/*.py, named
inputs with name and source, relative output evidence files, GPU requirement and a
whole-minute time limit no greater than 1800 seconds. Source resolution belongs to
the execution adapter. Time limits are 60..1800 seconds in multiples of 60.
Input names are unique simple letters/digits/underscore/dot/hyphen names without
traversal; sources are nonempty. Script entrypoints are Python files
under research/; output paths are unique relative files without dot/traversal
components, and scripts require at least one output. Fixed evaluation requires entrypoint=null, inputs=[],
outputs=[], gpu=true, time_limit_seconds=1800. No decision requires training.
For reselect/stop execution may be null; do not invent an experiment.
"""
        )
    if phase in ("judge", "review"):
        return (
            common
            + """
Judge scientific discriminability, not merely presence of contract strings. Check
whether the proposed/actual observables truly distinguish the competing explanations
and whether support and refutation criteria were explicit before execution.
An RMSD-only result generally does not establish mechanism. Different time points
are not independent random seeds. Audit controls, aggregation, dependence, actual
artifacts and evidence scope; report inconclusive when criteria cannot be resolved.
Structural contract validation does not certify these scientific judgments.
Keep intervention correctness, mechanism observations and performance stability as
separate evidence requirements: one cannot substitute for another. New benchmarks
need their own local_evaluation scope; a theory question should state assumptions,
the proposition and derivation scope without inventing an empirical mechanism.
"""
        )
    return common if phase == "execute" else ""
