"""Opt-in frozen research contracts; structural checks do not certify causality."""

import copy
from pathlib import PurePosixPath
import re

from .protocol import BOOLEAN, TEXT, _array, _enum, _nullable, _object, _validate


RESEARCH_CONTRACT = _object(
    purpose=_enum("performance", "mechanism", "observation", "theory"),
    question=TEXT,
    explanations=_array(_object(id=TEXT, statement=TEXT, prediction=TEXT)),
    observables=_array(
        _object(
            id=TEXT,
            definition=TEXT,
            method=TEXT,
            inputs=TEXT,
            timing=TEXT,
            aggregation=TEXT,
            cost=TEXT,
        )
    ),
    interpretation=_array(
        _object(
            explanation_id=TEXT,
            observable_ids=_array(TEXT),
            supports=TEXT,
            refutes=TEXT,
        )
    ),
    inconclusive=TEXT,
    execution=_object(
        mode=_enum("fixed_evaluation", "script"),
        entrypoint=_nullable(TEXT),
        gpu=BOOLEAN,
        time_limit_seconds={"type": "integer"},
        inputs=_array(_object(name=TEXT, source=TEXT, role=TEXT)),
        outputs=_array(TEXT),
    ),
)


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
    result = copy.deepcopy(schema)
    result["properties"]["research_contract"] = _nullable(RESEARCH_CONTRACT)
    result["required"].append("research_contract")
    return result


def _text(value, name):
    if not value.strip():
        raise ValueError(f"Empty research contract {name}")


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
    """Check declarations, references and execution bounds, not scientific meaning."""
    if plan["decision"] != "investigate":
        return
    value = plan.get("research_contract")
    _validate(RESEARCH_CONTRACT, value, "research_contract")
    _text(value["question"], "question")
    _text(value["inconclusive"], "inconclusive")
    explanations = value["explanations"]
    observables = value["observables"]
    for collection, label in (
        (explanations, "explanations"),
        (observables, "observables"),
    ):
        for item in collection:
            for key, text in item.items():
                _text(text, f"{label}.{key}")
        if len({item["id"] for item in collection}) != len(collection):
            raise ValueError(f"Duplicate research {label} id")
    if not observables:
        raise ValueError("Research needs an observable or derivation evidence")
    explanation_ids = {item["id"] for item in explanations}
    observable_ids = {item["id"] for item in observables}
    interpreted = set()
    for item in value["interpretation"]:
        if item["explanation_id"] not in explanation_ids:
            raise ValueError("Interpretation references unknown explanation")
        if (
            not item["observable_ids"]
            or not set(item["observable_ids"]) <= observable_ids
        ):
            raise ValueError("Interpretation needs existing observables")
        _text(item["supports"], "supports")
        _text(item["refutes"], "refutes")
        interpreted.add(item["explanation_id"])
    if value["purpose"] == "mechanism":
        if any(
            len({item[key].strip().casefold() for item in explanations}) < 2
            for key in ("statement", "prediction")
        ):
            raise ValueError(
                "Mechanism research needs two distinct explanations and predictions"
            )
        if interpreted != explanation_ids:
            raise ValueError(
                "Mechanism research needs interpretation for each explanation"
            )
    execution = value["execution"]
    seconds = execution["time_limit_seconds"]
    if not 1 <= seconds <= 1800 or seconds % 60:
        raise ValueError(
            "Research time limit must be whole minutes within 1..1800 seconds"
        )
    names = []
    for item in execution["inputs"]:
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", item["name"])
            or ".." in item["name"]
        ):
            raise ValueError(
                "Research input name must be a simple name without traversal"
            )
        names.append(item["name"])
        _text(item["source"], "input source")
        _text(item["role"], "input role")
    if len(set(names)) != len(names):
        raise ValueError("Duplicate research input name")
    for output in execution["outputs"]:
        _relative_file(output)
    if len(set(execution["outputs"])) != len(execution["outputs"]):
        raise ValueError("Duplicate research output")
    if execution["mode"] == "script":
        entrypoint = execution["entrypoint"]
        if entrypoint is None:
            raise ValueError("Script research needs an entrypoint")
        path = _relative_file(entrypoint)
        if len(path.parts) < 2 or path.parts[0] != "research" or path.suffix != ".py":
            raise ValueError("Script entrypoint must be a Python file under research/")
        if not execution["outputs"]:
            raise ValueError("Script research needs declared output evidence files")
    elif (
        execution["entrypoint"] is not None
        or execution["gpu"] is not True
        or seconds != 1800
        or execution["inputs"]
        or execution["outputs"]
    ):
        raise ValueError(
            "Fixed evaluation uses its original GPU/1800s profile and no script inputs/outputs"
        )


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
For decision=investigate, fill research_contract with purpose, a concrete question,
explanations and predictions where applicable, direct observables (or derivation
evidence), predefined support/refutation rules and an inconclusive condition.
Mechanism work needs at least two competing explanations with different predictions
and interpretation referencing their observable IDs. Other purposes may use empty
explanations/interpretation. Each observable declares definition, method, inputs,
timing, aggregation and cost. Freeze these declarations before Execute.
execution selects fixed_evaluation or script. A script uses research/*.py, named
inputs with source and role, relative output evidence files, GPU requirement and a
whole-minute time limit no greater than 1800 seconds. Source resolution belongs to
the execution adapter. Fixed evaluation may use entrypoint=null, inputs=[],
outputs=[], gpu=true, time_limit_seconds=1800. No decision requires training.
For reselect/stop research_contract may be null; do not invent an experiment.
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
