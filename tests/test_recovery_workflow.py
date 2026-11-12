"""A stopped settled experiment resumes evaluation in a new Run, without dispatch."""

import asyncio
import json
import shlex
import sys
from pathlib import Path

import pytest

from research_loop.codex import CodexConfig
from research_loop.configuration import prepare_configuration
from research_loop.group_agents import NativeGroupServices
from research_loop.groups import GroupDefinition, GroupRuntime
from research_loop.state import Journal
from research_workflows.research import create


@pytest.mark.parametrize(
    "revision", ["same", "instructions", "instructions-twice", "protocol", "task"]
)
def test_stop_after_command_recovers_evaluation_without_reexecution(
    tmp_path, monkeypatch, revision
):
    source = Path(__file__).resolve().parents[1] / "examples/research.yaml"
    config = prepare_configuration(source, tmp_path / "declarations", revision="fixed")
    config["project"] = str(tmp_path)
    task = tmp_path / "task.md"
    task.write_text(
        "Compute 6 * 7 exactly once on CPU and independently evaluate its actual result."
    )
    evaluation_started = asyncio.Event()
    calls = []
    executions = []
    evaluating = 0

    class Native:
        def __init__(self, config, journal):
            pass

        async def open(self):
            pass

        async def verify(self, *args, **kwargs):
            pass

        async def stop(self):
            pass

        async def settle(self):
            pass

        async def close(self):
            pass

        async def run(self, instructions, payload, schema, **kwargs):
            nonlocal evaluating
            work = Path(payload["work"])
            kwargs["on_started"](f"thread-{len(calls)}")
            fields = schema["properties"]
            base = dict(
                summary="Bounded synthetic evidence",
                status="ready",
                help="",
                artifacts=[],
                command="",
            )
            if "plan_artifact" in fields:
                calls.append("plan")
                (work / "scientific-plan.md").write_text(
                    "Exact integer multiplication, one execution, independent evaluation."
                )
                return dict(
                    base,
                    plan_artifact="scientific-plan.md",
                    artifacts=["scientific-plan.md"],
                )
            if "result_file" in fields:
                calls.append("prepare")
                (work / "experiment.py").write_text(
                    'from pathlib import Path\nPath("result.json").write_text("42")\nPath("dispatch-count").write_text("once")\n'
                )
                return dict(
                    base,
                    command=f"{shlex.quote(sys.executable)} experiment.py",
                    result_file="result.json",
                    artifacts=["experiment.py"],
                )
            assert "claim" in fields
            calls.append("evaluate")
            evaluating += 1
            assert (work / "result.json").read_text() == "42"
            if evaluating == 1 or (
                revision == "instructions-twice" and evaluating == 2
            ):
                evaluation_started.set()
                await asyncio.Future()
            return dict(
                base,
                claim="Observed 42",
                limitations=["Synthetic, recovered evidence"],
                evidence=["result.json"],
            )

    monkeypatch.setattr(
        "research_loop.group_agents.configuration",
        lambda *a, **k: (CodexConfig(cwd=str(tmp_path)), {}),
    )
    original_execute = NativeGroupServices.execute_prepared

    async def execute(self, *args, **kwargs):
        executions.append((self.ctx.run_id, kwargs["operation_id"]))
        return await original_execute(self, *args, **kwargs)

    monkeypatch.setattr(NativeGroupServices, "execute_prepared", execute)
    journal = Journal(tmp_path / "events.jsonl")
    runtime = GroupRuntime(
        tmp_path,
        config["groups"],
        revision=config["revision"],
        service_factory=lambda ctx: NativeGroupServices(
            ctx, config, journal, native_factory=Native
        ),
    )
    workflow = create({})

    async def scenario():
        context = {"input": {}, "output_schema": workflow.result_schema(None)}
        old_id = await runtime.start("exp", str(task), [], context=context)
        await asyncio.wait_for(evaluation_started.wait(), 5)
        await runtime.stop(old_id)
        old = runtime.observe(old_id)
        packet = workflow.recover({"root": tmp_path}, old)
        assert packet["operation"]["origin_group_run_id"] == old_id
        assert packet["operation_id"] == "prepared-experiment"
        assert packet["limitations"]
        if revision != "same":
            declaration = source.read_text().replace(
                "../src/", str(source.parent.parent / "src") + "/"
            )
            if revision in {"instructions", "instructions-twice"}:
                critic = tmp_path / "revised-critic.md"
                critic.write_text(
                    config["agents"]["critic"]["instructions"]
                    + "\nInspect the actual recovered result carefully.\n"
                )
                declaration = declaration.replace(
                    str(
                        source.parent.parent / "src/research_workflows/agents/critic.md"
                    ),
                    str(critic),
                )
            if revision == "protocol":
                declaration = declaration.replace(
                    "execution_backend: local",
                    "execution_backend: local\n      protocol: revised-scientific-protocol",
                )
            revised_source = tmp_path / "revised.yaml"
            revised_source.write_text(declaration)
            revised_config = prepare_configuration(
                revised_source, tmp_path / "revised-declarations", revision="new"
            )
            revised_config["project"] = str(tmp_path)
            config.clear()
            config.update(revised_config)
            runtime.replace_definitions(
                [GroupDefinition.from_json(row) for row in config["groups"]],
                config["revision"],
            )
            if revision == "task":
                task.write_text("Use a different scientific comparison.")
        evaluation_started.clear()
        new_id = await runtime.start(
            "exp", str(task), [], context=context, predecessor=old_id, recovery=packet
        )
        intermediate_id = None
        if revision == "instructions-twice":
            await asyncio.wait_for(evaluation_started.wait(), 5)
            await runtime.stop(new_id)
            intermediate_id = new_id
            packet = workflow.recover(
                {"root": tmp_path}, runtime.observe(intermediate_id)
            )
            assert packet["source_run_id"] == intermediate_id
            assert packet["source_config_revision"] == "new"
            assert packet["operation"]["origin_group_run_id"] == old_id
            assert packet["operation"]["source_config_revision"] == "fixed"
            new_id = await runtime.start(
                "exp",
                str(task),
                [],
                context=context,
                predecessor=intermediate_id,
                recovery=packet,
            )
        while runtime.observe(new_id)["status"] == "running":
            await runtime.wait()
        new = runtime.observe(new_id)
        if revision in {"protocol", "task"}:
            assert new["status"] == "failed", new
            assert executions == [(old_id, "prepared-experiment")]
            assert calls == ["plan", "prepare", "evaluate"]
            await runtime.close_runtime()
            return
        assert new["status"] == "returned", new
        assert new["output"]["completion"] == "completed"
        new_state = json.loads(
            (tmp_path / "group-runs" / new_id / "state.json").read_text()
        )
        assert [call["binding"] for call in new_state["native_calls"]] == ["evaluate"]
        transfers = [
            json.loads(path.read_text())
            for path in (tmp_path / "group-runs" / new_id / "records/handoff").glob(
                "*.json"
            )
        ]
        assert any(
            transfer.get("sender_run_id") == old_id
            and transfer.get("receiver_run_id") == new_id
            for transfer in transfers
        )

        operation = new_state["executions"]["prepared-experiment"]
        assert operation["origin_group_run_id"] == old_id
        assert operation["source_config_revision"] == "fixed"
        assert new["config_revision"] == (
            "new" if revision in {"instructions", "instructions-twice"} else "fixed"
        )
        assert operation["receipt"] == packet["operation"]["receipt"]
        assert not (tmp_path / "group-runs" / new_id / "work/dispatch-count").exists()
        assert (
            tmp_path / "group-runs" / old_id / "work/dispatch-count"
        ).read_text() == "once"
        assert executions == [(old_id, "prepared-experiment")]
        assert calls == ["plan", "prepare", "evaluate", "evaluate"] + (
            ["evaluate"] if intermediate_id else []
        )
        if intermediate_id:
            intermediate_state = json.loads(
                (tmp_path / "group-runs" / intermediate_id / "state.json").read_text()
            )
            assert (
                intermediate_state["executions"]["prepared-experiment"][
                    "origin_group_run_id"
                ]
                == old_id
            )
            assert (
                intermediate_state["executions"]["prepared-experiment"][
                    "source_config_revision"
                ]
                == "fixed"
            )
            assert new_state["recovery_source"] == intermediate_id
            assert new["predecessor"] == intermediate_id
            assert not (
                tmp_path / "group-runs" / intermediate_id / "work/dispatch-count"
            ).exists()
        ledger_path = tmp_path / "group-runs" / old_id / "executions.json"
        ledger = json.loads(ledger_path.read_text())
        ledger["prepared-experiment"]["status"] = "unknown"
        ledger_path.write_text(json.dumps(ledger))
        assert workflow.recover({"root": tmp_path}, old) is None
        await runtime.close_runtime()

    asyncio.run(scenario())
    journal.close()
