"""The optimizer configures an isolated, budgeted evaluator without model calls."""

import asyncio
import json
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
    assert project != support
    assert not project.is_relative_to(support)
    assert not support.is_relative_to(project)
    assert calls[0]["model"] == optimization.MODEL
    assert calls[0]["delegation"] == "host"
    assert (study / "work/orchestration.py").read_bytes() == (
        project / "orchestration.py"
    ).read_bytes()
    monkeypatch.setattr(
        optimization, "Native", lambda config, journal, tools: (config, tools)
    )
    native_config = CodexConfig(
        launch_args_override=("--config", 'sandbox_mode="workspace-write"')
    )
    wrapped = optimization.make_native_factory(study)(native_config, None, ["existing"])
    result, tools = wrapped.native
    assert 'sandbox_mode="workspace-write"' in result.launch_args_override
    assert not any(
        arg.startswith("permissions.") for arg in result.launch_args_override
    )
    assert tools[0] == "existing" and tools[1].name == "evaluate_candidate"


def test_budget_precharged_and_invalid_candidate_never_dispatches(
    tmp_path, monkeypatch
):
    study = tmp_path / "study"
    optimization.initialize(study, [CASE], 1)
    candidate = study / "work/orchestration.py"
    original = candidate.read_text()
    candidate.write_text("invalid python!")
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
    assert result["evaluation_failed"]
    assert not result["correctness_pass"]
    assert not result["coverage_complete"]
    assert result["evaluations_charged"] == 1
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
            "phase_protocol_pass": True,
            "correctness": {"status": "pass", "oracle": "private"},
            "usage": {"coverage": {"complete": True}, "total": {"total_tokens": 100}},
            "wall_seconds": 1.5,
        }

    monkeypatch.setattr(optimization, "run_case", run)
    evaluator = optimization.Evaluator(study)
    with pytest.raises(ValueError):
        asyncio.run(evaluator({"path": "other"}))
    result = asyncio.run(evaluator({}))
    assert result["correctness_pass"]
    assert result["coverage_complete"]
    assert result["phase_protocol_pass"]
    assert result["total_tokens"] == 100
    assert result["cases_evaluated"] == 1
    assert result["evaluations_charged"] == 1


def test_invalid_budget_and_overlapping_case(tmp_path):
    with pytest.raises(ValueError):
        optimization.initialize(tmp_path / "study", [CASE], 0)
    with pytest.raises(ValueError, match="disjoint"):
        optimization.initialize(CASE.parent / "study", [CASE], 1)


def test_parallel_lanes_stop_queued_and_keep_returned_costs(tmp_path, monkeypatch):
    study = tmp_path / "study"
    optimization.initialize(study, [CASE], 1)
    evaluator = optimization.Evaluator(study)
    evaluator.config["cases"] = ["a", "b", "c", "d"]
    resources = {"a": "1", "b": "1", "c": "2", "d": "2"}
    monkeypatch.setattr(
        optimization,
        "case_backend",
        lambda path: (
            lambda p: {"remote": {"ssh_host": "host", "gpu_uuid": resources[p]}},
            None,
            object(),
        ),
    )

    async def preflight(cases):
        return None

    monkeypatch.setattr(optimization, "resource_preflight", preflight)
    started = []
    both_started = asyncio.Event()

    async def run(path, output, **kwargs):
        assert (
            kwargs["orchestration"].read_text()
            == (study / "work/orchestration.py").read_text()
        )
        assert (
            json.loads((study / "evaluation-ledger.json").read_text())["charged"] == 1
        )
        started.append(path.name)
        if len(started) == 2:
            both_started.set()
        await both_started.wait()
        if path.name == "a":
            raise RuntimeError("failure")
        await asyncio.sleep(0)
        return {
            "status": "completed",
            "phase_protocol_pass": True,
            "correctness": {"status": "pass"},
            "usage": {"coverage": {"complete": True}, "total": {"total_tokens": 23}},
        }

    monkeypatch.setattr(optimization, "run_case", run)
    result = asyncio.run(evaluator({}))
    assert set(started) == {"a", "c"}
    assert result["cases_evaluated"] == 1
    assert result["total_tokens"] == 23
    assert result["evaluation_failed"]
    assert not result["correctness_pass"] and not result["phase_protocol_pass"]


def test_partial_cost_recovery_and_durable_result(tmp_path, monkeypatch):
    study = tmp_path / "study"
    optimization.initialize(study, [CASE], 1)

    async def run(case, output, **kwargs):
        output.mkdir()
        (output / "report.json").write_text(
            json.dumps({"usage": {"total": {"total_tokens": 42}}})
        )
        raise RuntimeError("after reporting")

    monkeypatch.setattr(optimization, "run_case", run)
    evaluator = optimization.Evaluator(study)
    result = asyncio.run(evaluator({}))
    assert result["total_tokens"] == 42
    assert result["partial_cases"] == 1 and result["cases_evaluated"] == 0
    assert not result["coverage_complete"]
    durable = Path(evaluator.config["support"]) / "evaluation-000001/result.json"
    assert json.loads(durable.read_text()) == result


def test_host_stops_after_lead_response_and_keeps_analysis(tmp_path):
    study = tmp_path / "study"
    optimization.initialize(study, [CASE], 1)
    evaluator = optimization.Evaluator(study)
    (study / "evaluation-ledger.json").write_text(json.dumps({"charged": 1}))

    class FakeNative:
        async def run(self):
            return {
                "study_action": "continue",
                "stop_reason": "",
                "analysis": "measured findings",
            }

    result = asyncio.run(optimization.BoundedNative(FakeNative(), evaluator).run())
    assert result["study_action"] == "stop"
    assert result["analysis"] == "measured findings"


def test_best_is_host_recorded_from_passing_result(tmp_path, monkeypatch):
    study = tmp_path / "study"
    optimization.initialize(study, [CASE], 1)

    async def run(*args, **kwargs):
        return {
            "status": "completed",
            "phase_protocol_pass": True,
            "correctness": {"status": "pass"},
            "usage": {"coverage": {"complete": True}, "total": {"total_tokens": 7}},
        }

    monkeypatch.setattr(optimization, "run_case", run)
    result = asyncio.run(optimization.Evaluator(study)({}))
    best = json.loads((study / "best.json").read_text())
    assert best["total_tokens"] == result["total_tokens"] == 7
    assert (
        Path(best["orchestration"]).read_text()
        == (study / "work/orchestration.py").read_text()
    )


def test_host_baseline_precedes_optimizer_and_exhausts_single_budget(
    tmp_path, monkeypatch
):
    study = tmp_path / "study"
    optimization.initialize(study, [CASE], 1)
    events = []

    async def run_case(*args, **kwargs):
        events.append("baseline")
        return {
            "status": "completed",
            "phase_protocol_pass": True,
            "correctness": {"status": "pass"},
            "usage": {"coverage": {"complete": True}, "total": {"total_tokens": 7}},
        }

    async def run_loop(*args, **kwargs):
        events.append("optimizer")
        return {"status": "completed"}

    monkeypatch.setattr(optimization, "run_case", run_case)
    monkeypatch.setattr(optimization.loop, "run", run_loop)
    result = asyncio.run(optimization.run_study(study))
    assert result["status"] == "budget_exhausted"
    assert events == ["baseline"]
    assert json.loads((study / "baseline.json").read_text())["total_tokens"] == 7
    assert len(json.loads((study / "evaluation-history.json").read_text())) == 1


def test_failed_baseline_stops_before_optimizer(tmp_path, monkeypatch):
    study = tmp_path / "study"
    optimization.initialize(study, [CASE], 3)

    async def fail(*args, **kwargs):
        raise RuntimeError("case failure")

    async def forbidden(*args, **kwargs):
        raise AssertionError("optimizer admitted after failed baseline")

    monkeypatch.setattr(optimization, "run_case", fail)
    monkeypatch.setattr(optimization.loop, "run", forbidden)
    assert asyncio.run(optimization.run_study(study))["status"] == "baseline_failed"
    assert (study / "baseline.json").exists()


def test_edited_initial_candidate_rejected_before_charge(tmp_path):
    study = tmp_path / "study"
    optimization.initialize(study, [CASE], 3)
    candidate = study / "work/orchestration.py"
    candidate.write_text(candidate.read_text() + "\n# altered\n")
    with pytest.raises(ValueError, match="unmodified seed"):
        asyncio.run(optimization.run_study(study))
    assert json.loads((study / "evaluation-ledger.json").read_text())["charged"] == 0


def test_passing_baseline_precedes_optimizer(tmp_path, monkeypatch):
    study = tmp_path / "study"
    optimization.initialize(study, [CASE], 3)
    events = []

    async def run_case(*args, **kwargs):
        events.append("baseline")
        return {
            "status": "completed",
            "phase_protocol_pass": True,
            "correctness": {"status": "pass"},
            "usage": {"coverage": {"complete": True}, "total": {"total_tokens": 7}},
        }

    async def run_loop(*args, **kwargs):
        assert optimization.passes(json.loads((study / "baseline.json").read_text()))
        events.append("optimizer")
        return {"status": "completed"}

    monkeypatch.setattr(optimization, "run_case", run_case)
    monkeypatch.setattr(optimization.loop, "run", run_loop)
    assert asyncio.run(optimization.run_study(study))["status"] == "completed"
    assert events == ["baseline", "optimizer"]


def test_initialize_freezes_declared_inputs(tmp_path):
    import shutil

    source = tmp_path / "source"
    shutil.copytree(CASE.parent, source)
    study = tmp_path / "study"
    optimization.initialize(study, [source / "case.json"], 1)
    config = json.loads((study / "evaluator.json").read_text())
    frozen = Path(config["cases"][0]).parent
    old_task = (frozen / "task.md").read_text()
    old_source = (frozen / "workspace/experiment.py").read_text()
    (source / "task.md").write_text("changed")
    (source / "workspace/experiment.py").write_text("changed")
    assert (frozen / "task.md").read_text() == old_task
    assert (frozen / "workspace/experiment.py").read_text() == old_source
    assert str(source) in config["source_case_roots"]


def test_pending_operation_blocks_then_clears_without_charge(tmp_path, monkeypatch):
    from benchmarks.experiment_cycle import collection

    study = tmp_path / "study"
    optimization.initialize(study, [CASE], 2)
    pending = [{"remote": {"ssh_host": "host"}, "operation_id": "cycle-1"}]
    (study / "pending-operations.json").write_text(json.dumps(pending))
    calls = []

    async def remote(config, action, identity=None):
        calls.append((action, identity))
        if len(calls) == 1:
            raise TimeoutError()
        return {"exit_code": 0}

    async def run(*args, **kwargs):
        return {
            "status": "completed",
            "phase_protocol_pass": True,
            "correctness": {"status": "pass"},
            "usage": {"coverage": {"complete": True}, "total": {"total_tokens": 7}},
        }

    monkeypatch.setattr(collection, "remote", remote)
    monkeypatch.setattr(optimization, "run_case", run)
    evaluator = optimization.Evaluator(study)
    blocked = asyncio.run(evaluator({}))
    assert blocked["reconciliation_required"]
    assert json.loads((study / "evaluation-ledger.json").read_text())["charged"] == 0
    result = asyncio.run(evaluator({}))
    assert result["correctness_pass"]
    assert json.loads((study / "pending-operations.json").read_text()) == []
    assert calls == [("reconcile", "cycle-1"), ("reconcile", "cycle-1")]


def test_dispatch_record_recovered_and_terminal_status_not_requeued(
    tmp_path, monkeypatch
):
    from benchmarks.experiment_cycle import collection

    study = tmp_path / "study"
    optimization.initialize(study, [CASE], 2)
    evaluator = optimization.Evaluator(study)
    evaluator.config["cases"] = ["remote-case"]
    case = {"remote": {"ssh_host": "host"}, "intervention_id": 1}
    monkeypatch.setattr(
        optimization, "case_backend", lambda _: (lambda _: case, None, object())
    )
    operation = (
        Path(evaluator.config["support"])
        / "evaluation-000001/case-0000/execute/remote-operation.json"
    )
    operation.parent.mkdir(parents=True)
    operation.write_text(
        json.dumps({"operation_id": "cycle-1", "status": "dispatching"})
    )
    replies = iter([{"status": "running"}, {"exit_code": 1}])

    async def remote(*args):
        return next(replies)

    monkeypatch.setattr(collection, "remote", remote)
    blocked = asyncio.run(
        optimization.reconcile_evaluations(study, evaluator.config, 1)
    )
    assert blocked["reconciliation_required"]
    assert (
        asyncio.run(optimization.reconcile_evaluations(study, evaluator.config, 1))
        is None
    )
    assert (
        asyncio.run(optimization.reconcile_evaluations(study, evaluator.config, 1))
        is None
    )


def test_explicit_continue_keeps_existing_baseline_and_budget(tmp_path, monkeypatch):
    study = tmp_path / "study"
    optimization.initialize(study, [CASE], 3)
    (study / "evaluation-ledger.json").write_text(json.dumps({"charged": 1}))
    (study / "baseline.json").write_text(
        json.dumps(
            {
                "correctness_pass": True,
                "coverage_complete": True,
                "phase_protocol_pass": True,
                "evaluation_failed": False,
            }
        )
    )
    captured = []

    async def forbidden(*args, **kwargs):
        raise AssertionError("completed baseline must not run again")

    async def resume(root, **kwargs):
        captured.append(kwargs["continue_run"])
        return {"status": "running"}

    monkeypatch.setattr(optimization, "run_case", forbidden)
    monkeypatch.setattr(optimization.loop, "run", resume)
    assert (
        asyncio.run(optimization.run_study(study, continue_run=True))["status"]
        == "running"
    )
    assert captured == [True]
    assert json.loads((study / "evaluation-ledger.json").read_text())["charged"] == 1


def test_unlimited_study_evaluates_and_resumes_after_three_charges(
    tmp_path, monkeypatch
):
    study = tmp_path / "study"
    optimization.initialize(study, [CASE])
    assert json.loads((study / "evaluator.json").read_text())["max_evaluations"] is None
    (study / "evaluation-ledger.json").write_text(json.dumps({"charged": 3}))
    baseline = {
        "correctness_pass": True,
        "coverage_complete": True,
        "phase_protocol_pass": True,
        "evaluation_failed": False,
    }
    (study / "baseline.json").write_text(json.dumps(baseline))

    async def run_case(*args, **kwargs):
        return {
            "status": "completed",
            "phase_protocol_pass": True,
            "correctness": {"status": "pass"},
            "usage": {"coverage": {"complete": True}, "total": {"total_tokens": 7}},
        }

    async def resume(root, **kwargs):
        assert kwargs["continue_run"]
        return {"status": "running"}

    monkeypatch.setattr(optimization, "run_case", run_case)
    monkeypatch.setattr(optimization.loop, "run", resume)
    result = asyncio.run(optimization.Evaluator(study)({}))
    assert result["evaluations_charged"] == 4
    assert (
        asyncio.run(optimization.run_study(study, continue_run=True))["status"]
        == "running"
    )
    assert json.loads((study / "evaluation-ledger.json").read_text())["charged"] == 4
    assert json.loads((study / "baseline.json").read_text()) == baseline


def test_unlimited_native_does_not_stop_at_three(tmp_path):
    study = tmp_path / "study"
    optimization.initialize(study, [CASE])
    (study / "evaluation-ledger.json").write_text(json.dumps({"charged": 3}))

    class FakeNative:
        async def run(self):
            return {"study_action": "continue"}

    result = asyncio.run(
        optimization.BoundedNative(FakeNative(), optimization.Evaluator(study)).run()
    )
    assert result["study_action"] == "continue"


def test_closed_time_window_does_not_charge_or_dispatch(tmp_path, monkeypatch):
    study = tmp_path / "study"
    optimization.initialize(study, [CASE])
    (study / "run-window.json").write_text(json.dumps({"deadline_unix": 100}))
    monkeypatch.setattr(optimization, "time", lambda: 100)
    result = asyncio.run(optimization.Evaluator(study)({}))
    assert result["time_window_finished"]
    assert json.loads((study / "evaluation-ledger.json").read_text()) == {"charged": 0}
