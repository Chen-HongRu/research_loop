"""H11 Group phase wiring, scoped handoffs and real bounded CPU execution."""

import asyncio
import json
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from openai_codex.client import CodexConfig

from research_loop.group_agents import NativeGroupServices
from research_loop.configuration import prepare_configuration
from research_workflows.programs import exp
from research_loop.state import Journal, write_json
from research_loop.records import ExecutionTrace


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    work = tmp_path / "group-runs/exp/work"
    work.mkdir(parents=True)
    task = tmp_path / "experiment-task.json"
    write_json(
        task,
        {
            "question": "Does one bounded CPU execution produce 42?",
            "hypothesis": "6 * 7 equals 42",
            "controls": ["Use exact integer arithmetic"],
            "permitted_changes": ["Create a synthetic Python script"],
            "data_roles": {"synthetic": "No dataset"},
            "evaluation": {"expected": 42},
            "inputs": [],
            "constraints": ["Local CPU only; execute once"],
            "completion_criteria": ["Independent Critic checks the actual result"],
        },
    )
    ctx = SimpleNamespace(
        run_id="exp",
        root=work.parent,
        work=work,
        study_root=tmp_path,
        round=1,
        config_revision="revision-test",
        state={},
        definition=SimpleNamespace(
            settings={"execution_backend": "local"},
            agents={
                "plan": "experiment",
                "prepare": "experiment",
                "evaluate": "critic",
            },
        ),
    )
    ctx.trace = ExecutionTrace(
        ctx.root, run_id=ctx.run_id, round=ctx.round, revision=ctx.config_revision
    )
    ctx.save_state = lambda: write_json(ctx.root / "state.json", ctx.state)
    goal = tmp_path / "goal.md"
    goal.write_text("One bounded synthetic check only.\n")
    published = tmp_path / "outputs/versions/initial"
    published.mkdir(parents=True)
    (published / "index.md").write_text("No reviewed findings.\n")
    (tmp_path / "outputs/current").symlink_to("versions/initial")
    write_json(
        tmp_path / "call.json",
        {
            "input": {
                "goal_file": str(goal),
                "project_root": str(tmp_path / "project"),
            },
            "output_schema": {"type": "object"},
        },
    )
    input_path = ctx.root / "rounds/000001/input.json"
    input_path.parent.mkdir(parents=True)
    write_json(
        input_path, {"task_ref": str(task), "input_refs": [], "feedback_refs": []}
    )
    configuration_calls = []

    def config(*args, **kwargs):
        configuration_calls.append((args, kwargs))
        return CodexConfig(
            cwd="/original/project", launch_args_override=("preserved",)
        ), {}

    monkeypatch.setattr("research_loop.group_agents.configuration", config)
    calls = []
    blocked = {"phase": None}

    class Native:
        def __init__(self, config, journal):
            self.config = config

        async def open(self):
            pass

        async def verify(self, *args, **kwargs):
            pass

        async def settle(self):
            pass

        async def close(self):
            pass

        async def stop(self):
            pass

        async def run(self, instructions, payload, schema, **kwargs):
            phase = {
                exp.PLAN_INSTRUCTIONS: "plan",
                exp.PREPARE_INSTRUCTIONS: "prepare",
                exp.EVALUATE_INSTRUCTIONS: "evaluate",
            }[payload["instructions"].removeprefix(exp.COMMON + "\n")]
            thread = kwargs["thread_id"] or f"thread-{len(calls)}"
            kwargs["on_started"](thread)
            calls.append((phase, thread, self.config, instructions, payload))
            result = {
                "summary": phase,
                "status": "ready",
                "help": "",
                "artifacts": [],
                "command": "",
            }
            if phase == "plan":
                result["plan_artifact"] = "scientific-plan.md"
                (work / "scientific-plan.md").write_text(
                    "Compute 6 * 7 exactly once on CPU.\n"
                )
                result["artifacts"] = ["scientific-plan.md"]
            elif phase == "prepare":
                code = work / "implementation.py"
                code.write_text(
                    "from pathlib import Path\n"
                    'p = Path("dispatches")\n'
                    'p.write_text(p.read_text() + "once" if p.exists() else "once")\n'
                    'Path("result.json").write_text("42")\n'
                )
                result["artifacts"] = ["implementation.py"]
                result["result_file"] = "result.json"
                result["command"] = f"{shlex.quote(sys.executable)} implementation.py"
            else:
                result["evidence"] = ["result.json"]
                result["claim"] = "Observed 42 in the actual synthetic output."
                result["limitations"] = ["Synthetic CPU test only"]
                assert json.loads((work / "result.json").read_text()) == 42
                result["summary"] = result["claim"]
            if blocked["phase"] == phase and not payload["assignment"]["feedback_refs"]:
                result["status"] = "blocked"
                result["help"] = "Supply the approved missing scientific decision."
                result["command"] = ""
            return result

    journal = Journal(tmp_path / "events.jsonl")
    config = prepare_configuration(
        Path(__file__).resolve().parents[1] / "examples/research.yaml",
        tmp_path / "configurations",
        revision=ctx.config_revision,
    )
    config["project"] = str(tmp_path / "project")
    ctx.state["research_context"] = {
        "input": {"published_knowledge_root": str(published.resolve())},
        "output_schema": {"type": "object"},
    }
    service = NativeGroupServices(ctx, config, journal, native_factory=Native)
    ctx.services = service
    yield ctx, task, calls, blocked, configuration_calls
    journal.close()


def test_h11_exact_prompts_roles_threads_cwd_and_real_command(experiment):
    ctx, task, calls, _, configurations = experiment

    async def check():
        result = await exp.run(ctx, {"task_ref": str(task)})
        assert result["completion"] == "completed"
        assert set(result) == {
            "summary",
            "completion",
            "unfinished",
            "help",
            "artifacts",
        }
        assert [call[0] for call in calls] == ["plan", "prepare", "evaluate"]
        assert calls[0][1] == calls[1][1] != calls[2][1]
        assert [call["binding"] for call in ctx.state["native_calls"]] == [
            "plan",
            "prepare",
            "evaluate",
        ]
        assert [call["agent_id"] for call in ctx.state["native_calls"]] == [
            "experiment",
            "experiment",
            "critic",
        ]
        assert calls[0][3] == calls[1][3] != calls[2][3]
        assert all("role_template" not in call[4] for call in calls)
        assert all(call[2].cwd == str(ctx.work) for call in calls)
        assert all(call[2].launch_args_override == ("preserved",) for call in calls)
        assert configurations[0][0][0] == configurations[1][0][0]
        assert configurations[1][0][0] != configurations[2][0][0]
        assert all(
            args[1] == Path(ctx.services.config["project"])
            for args, _ in configurations
        )
        assert all(not kwargs.get("denied_roots") for _, kwargs in configurations)
        assert ctx.state["research_context"]["input"][
            "published_knowledge_root"
        ] == str(published_root := (ctx.study_root / "outputs/current").resolve())
        assert published_root.is_dir()
        assert calls[2][4]["assignment"]["study_context"]["output_schema"] == {
            "type": "object"
        }
        assert (ctx.work / "dispatches").read_text() == "once"
        assert ctx.state["experiment_phases"]["execution"]["backend"] == "local"
        assert {
            "plan.json",
            "prepare.json",
            "execution.json",
            "evaluate.json",
            "report.json",
            "scientific-plan.md",
            "implementation.py",
        }.issubset({Path(ref).name for ref in result["artifacts"]})
        assert await exp.run(ctx, {"task_ref": str(task)}) == result
        assert len(calls) == 3
        assert (ctx.work / "dispatches").read_text() == "once"
        await ctx.services.close()

    asyncio.run(check())


@pytest.mark.parametrize("phase", ["plan", "prepare", "evaluate"])
def test_feedback_resumes_only_blocked_phase_without_repeated_compute(
    experiment, phase
):
    ctx, task, calls, blocked, _ = experiment
    blocked["phase"] = phase

    async def check():
        first = await exp.run(ctx, {"task_ref": str(task)})
        assert first["completion"] == "blocked"
        before = len(calls)
        assert await exp.run(ctx, {"task_ref": str(task)}) == first
        assert len(calls) == before
        ctx.round += 1
        feedback = ctx.study_root / "feedback.md"
        feedback.write_text("Approved missing decision; preserve the frozen task.\n")
        final = await exp.run(
            ctx, {"task_ref": str(task), "feedback_refs": [str(feedback)]}
        )
        assert final["completion"] == "completed"
        assert [call[0] for call in calls].count(phase) == 2
        assert all(
            [call[0] for call in calls].count(other) == 1
            for other in {"plan", "prepare", "evaluate"} - {phase}
        )
        assert (ctx.work / "dispatches").read_text() == "once"
        assert len(ctx.state["executions"]) == 1
        await ctx.services.close()

    asyncio.run(check())


def test_exp_requires_explicit_backend_and_real_plan_artifact(experiment):
    ctx, task, calls, _, _ = experiment
    ctx.definition.settings = {}
    missing = asyncio.run(exp.run(ctx, {"task_ref": str(task)}))
    assert missing["completion"] == "blocked"
    assert "execution_backend" in missing["help"]
    assert not calls
    ctx.definition.settings = {"execution_backend": "local"}
    assert exp._artifact(ctx, str(task)) is None
    assert exp._artifact(ctx, "missing.md") is None
    assert exp._artifact(ctx, "../state.json") is None
    (ctx.work / "real.md").write_text("Synthetic report.\n")
    (ctx.work / "linked.md").symlink_to("real.md")
    assert exp._artifact(ctx, "linked.md") is None


def test_published_context_is_pinned_across_service_recreation(experiment):
    ctx, task, calls, _, _ = experiment
    snapshot = json.loads(json.dumps(ctx.state["research_context"]))
    asyncio.run(exp.run(ctx, {"task_ref": str(task)}))
    replacement = ctx.study_root / "outputs/versions/replacement"
    replacement.mkdir()
    (ctx.study_root / "outputs/current").unlink()
    (ctx.study_root / "outputs/current").symlink_to("versions/replacement")
    ctx.services.__init__(
        ctx,
        ctx.services.config,
        ctx.services.agents.journal,
        native_factory=ctx.services.agents.native_factory,
    )
    asyncio.run(
        exp.run(
            ctx,
            {
                "task_ref": str(task),
                "study_context": {
                    "input": {"published_knowledge_root": str(replacement)}
                },
            },
        )
    )
    assert ctx.state["research_context"] == snapshot
    assert len(calls) == 3


def test_absolute_refs_and_admitted_citations_need_no_scientific_retry(experiment):
    ctx, task, calls, _, _ = experiment
    original = ctx.services.call
    raw_results = []

    async def absolute_refs(*args, **kwargs):
        result = await original(*args, **kwargs)
        if args[0] == "evaluate":
            result["evidence"] = [str(ctx.work / "result.json"), str(task)]
            result["artifacts"] = [str(task)]
        raw_results.append(json.loads(json.dumps(result)))
        return result

    ctx.services.call = absolute_refs

    async def check():
        output = await exp.run(ctx, {"task_ref": str(task)})
        assert output["completion"] == "completed"
        evaluation = ctx.state["experiment_phases"]["evaluate"]
        assert evaluation["evidence"] == ["result.json", str(task)]
        assert evaluation["artifacts"] == []
        assert ctx.state["experiment_phase_validation"]["evaluate"][
            "input_citations"
        ] == [str(task)]
        assert (
            ctx.state["experiment_phase_history"]["evaluate"][0]["result"]
            == raw_results[-1]
        )
        assert ctx.state["native_calls"][0]["result"]["artifacts"] == [
            "scientific-plan.md"
        ]
        assert str(task) not in output["artifacts"]
        ctx.round += 1
        await exp.run(ctx, {"task_ref": str(task), "feedback_refs": [str(task)]})
        assert len(calls) == 3
        assert (ctx.work / "dispatches").read_text() == "once"
        await ctx.services.close()

    asyncio.run(check())


def test_boundary_error_preserves_assessment_and_repairs_without_model(experiment):
    ctx, task, calls, _, _ = experiment
    original = ctx.services.call

    async def missing_evidence(*args, **kwargs):
        result = await original(*args, **kwargs)
        if args[0] == "evaluate":
            result["evidence"].append("independent-check.md")
        return result

    ctx.services.call = missing_evidence

    async def check():
        first = await exp.run(ctx, {"task_ref": str(task)})
        assert first["completion"] == "blocked"
        assessment = ctx.state["experiment_phases"]["evaluate"]
        assert assessment["help"] == ""
        assert "evaluate.evidence[1]" in first["help"]
        before = json.loads(json.dumps(assessment))
        ctx.round += 1
        still_blocked = await exp.run(
            ctx, {"task_ref": str(task), "feedback_refs": [str(task)]}
        )
        assert still_blocked["completion"] == "blocked"
        assert len(calls) == 3
        (ctx.work / "independent-check.md").write_text("Synthetic receipt.\n")
        final = await exp.run(ctx, {"task_ref": str(task)})
        assert final["completion"] == "completed"
        assert ctx.state["experiment_phases"]["evaluate"] == before
        assert len(calls) == 3
        assert (ctx.work / "dispatches").read_text() == "once"
        await ctx.services.close()

    asyncio.run(check())


@pytest.mark.parametrize("phase", ["plan", "prepare", "evaluate"])
def test_ready_phase_with_informational_help_advances_once(experiment, phase):
    ctx, task, calls, _, _ = experiment
    original = ctx.services.call
    note = "No phase blocker; retain the stated limitations."

    async def informative(*args, **kwargs):
        result = await original(*args, **kwargs)
        if result["summary"] == phase or (
            phase == "evaluate" and args[0] == "evaluate"
        ):
            result["help"] = note
        return result

    ctx.services.call = informative

    async def check():
        output = await exp.run(ctx, {"task_ref": str(task)})
        assert output["completion"] == "completed"
        assert output["help"] == ""
        assert ctx.state["experiment_phases"][phase]["help"] == note
        assert json.loads((ctx.work / f"{phase}.json").read_text())["help"] == note
        ctx.round += 1
        again = await exp.run(
            ctx, {"task_ref": str(task), "feedback_refs": [str(task)]}
        )
        assert again == output
        assert [call[0] for call in calls] == ["plan", "prepare", "evaluate"]
        assert (ctx.work / "dispatches").read_text() == "once"
        await ctx.services.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    "status,help_text", [(None, ""), ("invalid", ""), ("blocked", " ")]
)
def test_exp_rejects_missing_status_or_empty_blocker(experiment, status, help_text):
    ctx, task, calls, _, _ = experiment
    original = ctx.services.call

    async def malformed(*args, **kwargs):
        result = await original(*args, **kwargs)
        result.update(status=status, help=help_text)
        if status is None:
            del result["status"]
        return result

    ctx.services.call = malformed
    with pytest.raises(ValueError, match="status"):
        asyncio.run(exp.run(ctx, {"task_ref": str(task)}))
    assert [call[0] for call in calls] == ["plan"]
    assert not (ctx.work / "dispatches").exists()


def test_ready_note_cannot_mask_missing_required_plan(experiment):
    ctx, task, calls, _, _ = experiment
    original = ctx.services.call

    async def missing_plan(*args, **kwargs):
        result = await original(*args, **kwargs)
        result.update(plan_artifact="missing-plan.md", help="No scientific blocker.")
        return result

    ctx.services.call = missing_plan
    output = asyncio.run(exp.run(ctx, {"task_ref": str(task)}))
    assert output["completion"] == "blocked"
    assert "plan.plan_artifact" in output["help"]
    assert [call[0] for call in calls] == ["plan"]
    assert not (ctx.work / "dispatches").exists()


def test_failed_prepared_command_is_never_rerun_with_feedback(experiment):
    ctx, task, calls, _, _ = experiment
    original = ctx.services.call

    async def failing_command(*args, **kwargs):
        result = await original(*args, **kwargs)
        result["help"] = "No phase blocker."
        if args[0] == "prepare":
            script = ctx.work / "implementation.py"
            script.write_text(script.read_text() + "raise SystemExit(7)\n")
        return result

    ctx.services.call = failing_command

    async def check():
        output = await exp.run(ctx, {"task_ref": str(task)})
        assert output["completion"] == "blocked"
        assert ctx.state["experiment_phases"]["execution"]["exit_code"] == 7
        ctx.round += 1
        assert (
            await exp.run(ctx, {"task_ref": str(task), "feedback_refs": [str(task)]})
            == output
        )
        assert [call[0] for call in calls] == ["plan", "prepare"]
        assert (ctx.work / "dispatches").read_text() == "once"
        await ctx.services.close()

    asyncio.run(check())


def test_unknown_prepared_command_cannot_redispatch_with_feedback(experiment):
    ctx, task, calls, _, _ = experiment
    ctx.state["executions"]["prepared-experiment"] = {
        "status": "unknown",
        "tool": "local",
        "arguments": {"command": f"{shlex.quote(sys.executable)} implementation.py"},
    }

    async def check():
        for feedback in ([], [str(task)]):
            with pytest.raises(RuntimeError, match="reconciliation"):
                await exp.run(ctx, {"task_ref": str(task), "feedback_refs": feedback})
            ctx.round += 1
        assert [call[0] for call in calls] == ["plan", "prepare"]
        assert "execution" not in ctx.state["experiment_phases"]
        assert not (ctx.work / "dispatches").exists()
        with pytest.raises(RuntimeError, match="reconciliation"):
            await ctx.services.close()

    asyncio.run(check())
