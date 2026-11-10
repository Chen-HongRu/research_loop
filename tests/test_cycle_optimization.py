"""The optimizer configures an isolated, budgeted evaluator without model calls."""

import asyncio
import json
import tomllib
from pathlib import Path

import pytest
from openai_codex.client import CodexConfig

from benchmarks.experiment_cycle import optimization

CASE = (
    Path(__file__).resolve().parents[1]
    / "benchmarks/experiment_cycle/example/case.json"
)


def test_initialize_and_native_tool(tmp_path, monkeypatch):
    study = tmp_path / "study"
    calls = []
    real_initialize = optimization.loop.initialize

    def initialize(*args, **kwargs):
        calls.append(kwargs)
        return real_initialize(*args, **kwargs)

    monkeypatch.setattr(optimization.loop, "initialize", initialize)
    optimization.initialize(study, [CASE], 2)
    config = json.loads((study / "evaluator.json").read_text())
    support = Path(config["support"])
    project = Path(json.loads((study / "config.json").read_text())["project"])
    assert not support.is_relative_to(study)
    assert project == support / "seed"
    assert calls[0]["model"] == optimization.MODEL
    assert calls[0]["delegation"] == "host"
    assert (study / "work/strategy.json").read_bytes() == (
        project / "strategy.json"
    ).read_bytes()
    monkeypatch.setattr(
        optimization, "Native", lambda config, journal, tools: (config, tools)
    )
    paths = {
        str(CASE.parent): "read",
        str(CASE.parent / "nested"): "write",
        str(support): "read",
    }
    permission = (
        "permissions.research.filesystem="
        + "{"
        + ",".join(json.dumps(k) + "=" + json.dumps(v) for k, v in paths.items())
        + "}"
    )
    native_config = CodexConfig(launch_args_override=("--config", permission))
    result, tools = optimization.make_native_factory(study)(
        native_config, None, ["existing"]
    )
    effective = tomllib.loads(
        "paths=" + result.launch_args_override[1].split("=", 1)[1]
    )["paths"]
    assert effective[str(CASE.parent)] == "deny"
    assert str(CASE.parent / "nested") not in effective
    assert effective[str(support)] == "deny"
    assert effective[str(project)] == "read"
    assert tools[0] == "existing" and tools[1].name == "evaluate_candidate"


def test_budget_precharged_and_invalid_candidate_never_dispatches(
    tmp_path, monkeypatch
):
    study = tmp_path / "study"
    optimization.initialize(study, [CASE], 1)
    candidate = study / "work/strategy.json"
    original = candidate.read_text()
    candidate.write_text("{}")
    calls = []

    async def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("secret details")

    monkeypatch.setattr(optimization, "run_case", fail)
    evaluator = optimization.Evaluator(study)
    with pytest.raises(ValueError):
        asyncio.run(evaluator({}))
    assert json.loads((study / "evaluation-ledger.json").read_text())["charged"] == 0
    candidate.write_text(original)
    result = asyncio.run(evaluator({}))
    assert result == {
        "correctness_pass": False,
        "coverage_complete": False,
        "evaluation_failed": True,
        "evaluations_charged": 1,
    }
    assert len(calls) == 1
    assert calls[0][1]["model"] == optimization.MODEL
    with pytest.raises(ValueError, match="budget"):
        asyncio.run(evaluator({}))
    assert len(calls) == 1


def test_aggregate_and_reject_arguments(tmp_path, monkeypatch):
    study = tmp_path / "study"
    optimization.initialize(study, [CASE], 1)

    async def run(*args, **kwargs):
        return {
            "status": "completed",
            "correctness": {"status": "pass", "oracle": "private"},
            "usage": {"coverage": {"complete": True}, "total": {"total_tokens": 100}},
            "wall_seconds": 1.5,
        }

    monkeypatch.setattr(optimization, "run_case", run)
    evaluator = optimization.Evaluator(study)
    with pytest.raises(ValueError):
        asyncio.run(evaluator({"path": "other"}))
    result = asyncio.run(evaluator({}))
    assert result == {
        "correctness_pass": True,
        "coverage_complete": True,
        "total_tokens": 100,
        "wall_seconds": 1.5,
        "cases_evaluated": 1,
        "evaluations_charged": 1,
    }


def test_invalid_budget_and_overlapping_case(tmp_path):
    with pytest.raises(ValueError):
        optimization.initialize(tmp_path / "study", [CASE], 0)
    with pytest.raises(ValueError, match="disjoint"):
        optimization.initialize(CASE.parent / "study", [CASE], 1)
