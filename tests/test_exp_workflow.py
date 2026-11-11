"""H11 Group phase wiring, scoped handoffs and real bounded CPU execution."""

import asyncio
import json
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from openai_codex.client import CodexConfig

from research_loop.group_agents import GROUP_GUARD, NativeGroupServices
from research_loop.group_programs import exp
from research_loop.state import Journal, write_json


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
        state={},
        definition=SimpleNamespace(settings={"execution_backend": "local"}),
    )
    ctx.save_state = lambda: write_json(ctx.root / "state.json", ctx.state)
    goal = tmp_path / "goal.md"
    goal.write_text("One bounded synthetic check only.\n")
    published = tmp_path / "knowledge/versions/initial"
    published.mkdir(parents=True)
    (published / "index.md").write_text("No reviewed findings.\n")
    (tmp_path / "knowledge/current").symlink_to("versions/initial")
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
            }[payload["instructions"]]
            thread = kwargs["thread_id"] or f"thread-{len(calls)}"
            kwargs["on_started"](thread)
            calls.append((phase, thread, self.config, instructions, payload))
            result = {"summary": phase, "help": "", "artifacts": [], "command": ""}
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
                result["help"] = "Supply the approved missing scientific decision."
                result["command"] = ""
            return result

    journal = Journal(tmp_path / "events.jsonl")
    service = NativeGroupServices(
        ctx,
        {
            "project": str(tmp_path / "project"),
            "model": "gpt-6.1-sol",
            "effort": "medium",
        },
        journal,
        native_factory=Native,
    )
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
        assert [call[4]["role"] for call in calls] == [
            "proposer",
            "experimenter",
            "critic",
        ]
        assert all(call[3] == exp.COMMON + "\n" + GROUP_GUARD for call in calls)
        assert all("role_template" not in call[4] for call in calls)
        assert all(call[2].cwd == str(ctx.work) for call in calls)
        assert all(call[2].launch_args_override == ("preserved",) for call in calls)
        assert len({str(args[0]) for args, _ in configurations}) == 1
        assert all(
            args[1] == Path(ctx.services.config["project"])
            for args, _ in configurations
        )
        assert all(not kwargs.get("denied_roots") for _, kwargs in configurations)
        assert ctx.services.study_context["input"]["published_knowledge_root"] == str(
            (ctx.study_root / "knowledge/current").resolve()
        )
        assert calls[2][4]["study_context"]["output_schema"] == {"type": "object"}
        assert ctx.study_root / "call.json" not in ctx.services.readable
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


def test_published_context_is_pinned_across_service_recreation(experiment):
    ctx, _, _, _, _ = experiment
    snapshot = ctx.services.study_context
    write_json(ctx.study_root / "call.json", {"input": {"goal_file": "changed"}})
    ctx.services.__init__(
        ctx,
        ctx.services.config,
        ctx.services.raw_journal,
        native_factory=ctx.services.native_factory,
    )
    assert ctx.services.study_context == snapshot
    assert Path(snapshot["input"]["published_knowledge_root"]) in ctx.services.readable
