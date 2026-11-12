"""Real asynchronous Group programs; no models, network or remote execution."""

import asyncio
import json
import stat
from pathlib import Path

import pytest

from research_loop.groups import GroupDefinition, GroupRuntime


PROGRAM = """
import asyncio
import json

async def run(ctx, payload):
    await asyncio.sleep(0.01)
    ctx.state["count"] = ctx.state.get("count", 0) + 1
    ctx.save_state()
    path = ctx.work / "report.json"
    path.write_text(json.dumps({
        "run_id": ctx.run_id, "round": ctx.round,
        "count": ctx.state["count"], "version": ctx.definition.version,
        "feedback_refs": payload["feedback_refs"],
        "previous": ctx.previous_output,
    }))
    return {
        "summary": "round " + str(ctx.round), "completion": "report written",
        "unfinished": [], "help": "", "artifacts": ["report.json"],
    }
"""


def definition(tmp_path, source=PROGRAM, version="v1", settings=None):
    path = tmp_path / f"program-{version}.py"
    path.write_text(source)
    return GroupDefinition(
        "synthesis", version, "Synthetic scope", path, settings or {}
    )


def task_file(tmp_path):
    path = tmp_path / "task.md"
    path.write_text("Compute a synthetic report")
    return path


def test_concurrent_runs_are_isolated_and_deliver_readonly_copies(tmp_path):
    async def scenario():
        runtime = GroupRuntime(tmp_path / "study", [definition(tmp_path)])
        task = task_file(tmp_path)
        first = await runtime.start("synthesis", task, [])
        second = await runtime.start("synthesis", task, [])
        assert first != second
        assert runtime.observe(first)["task_ref"] == str(task)
        assert runtime.observe(first)["input_refs"] == []
        assert runtime.observe(first)["prompt"] == ""
        assert runtime.observe(first)["status"] == "running"
        with pytest.raises(RuntimeError, match="boundary"):
            runtime.hold(first)
        events = [await runtime.wait(), await runtime.wait()]
        assert {event["run_id"] for event in events} == {first, second}
        assert all(event["status"] == "returned" for event in events)
        outputs = [
            Path(runtime.observe(run)["output"]["artifacts"][0])
            for run in (first, second)
        ]
        assert [json.loads(path.read_text())["count"] for path in outputs] == [1, 1]
        assert [json.loads(path.read_text())["run_id"] for path in outputs] == [
            first,
            second,
        ]
        assert all(not path.stat().st_mode & stat.S_IWUSR for path in outputs)
        original = runtime.root / first / "work" / "report.json"
        assert original.stat().st_ino != outputs[0].stat().st_ino
        original.write_text("changed after delivery")
        assert json.loads(outputs[0].read_text())["count"] == 1
        snapshot = runtime.observe(first)
        snapshot["output"]["summary"] = "external mutation"
        assert runtime.observe(first)["output"]["summary"] == "round 1"
        runtime.close(second)
        assert runtime.observe(second)["status"] == "closed"
        await runtime.close_runtime()

    asyncio.run(scenario())


def test_hold_resume_reuses_state_and_pins_version_across_restart(tmp_path):
    async def scenario():
        study = tmp_path / "study"
        first_definition = definition(tmp_path)
        runtime = GroupRuntime(study, [first_definition])
        run_id = await runtime.start("synthesis", task_file(tmp_path), [])
        first = await runtime.wait()
        first_artifact = Path(first["output"]["artifacts"][0])
        first_bytes = first_artifact.read_bytes()
        first_record = (
            runtime.root / run_id / "rounds/000001/output.json"
        ).read_bytes()
        runtime.hold(run_id)
        await runtime.close_runtime()
        new = definition(
            tmp_path, PROGRAM.replace("report written", "new program"), "v2"
        )
        runtime = GroupRuntime(study, [new])
        assert await runtime.resume(run_id, [first_artifact], "follow up") == run_id
        second = await runtime.wait()
        report = json.loads(Path(second["output"]["artifacts"][0]).read_text())
        assert report["count"] == 2
        assert report["round"] == 2
        assert report["version"] == "v1"
        assert report["previous"] == first["output"]
        assert report["feedback_refs"] == [str(first_artifact)]
        assert second["feedback_refs"] == [str(first_artifact)]
        assert second["task_ref"] == first["task_ref"]
        assert second["prompt"] == "follow up"
        assert first_artifact.read_bytes() == first_bytes
        assert (
            runtime.root / run_id / "rounds/000001/output.json"
        ).read_bytes() == first_record
        assert second["output"]["completion"] == "report written"
        new_run = await runtime.start("synthesis", task_file(tmp_path), [])
        await runtime.wait()
        assert runtime.observe(new_run)["version"] == "v2"
        assert runtime.observe(new_run)["output"]["completion"] == "new program"
        await runtime.close_runtime()

    asyncio.run(scenario())


def test_same_version_rejects_source_and_configuration_drift(tmp_path):
    group = definition(tmp_path)
    GroupRuntime(tmp_path / "study", [group])
    group.program.write_text(PROGRAM + "\n# changed\n")
    with pytest.raises(ValueError, match="new version"):
        GroupRuntime(tmp_path / "study", [group])
    group.program.write_text(PROGRAM)
    changed = GroupDefinition(
        group.id, group.version, group.description, group.program, {"changed": True}
    )
    with pytest.raises(ValueError, match="new version"):
        GroupRuntime(tmp_path / "study", [changed])


def test_service_cleanup_is_a_boundary_barrier(tmp_path):
    async def scenario():
        reached = asyncio.Event()
        release = asyncio.Event()
        calls = []

        class Services:
            async def settle(self):
                calls.append("settle")
                reached.set()
                await release.wait()

            async def close(self):
                calls.append("close")

        runtime = GroupRuntime(
            tmp_path / "study", [definition(tmp_path)], lambda ctx: Services()
        )
        run_id = await runtime.start("synthesis", task_file(tmp_path), [])
        await reached.wait()
        assert runtime.observe(run_id)["status"] == "running"
        assert not list((runtime.root / run_id / "exports").iterdir())
        release.set()
        assert (await runtime.wait())["status"] == "returned"
        assert calls == ["settle", "close"]
        await runtime.close_runtime()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["settle", "close"])
def test_unsettled_services_require_reconciliation(tmp_path, failure):
    async def scenario():
        calls = []

        class Services:
            async def settle(self):
                calls.append("settle")
                if failure == "settle":
                    raise RuntimeError("execution outcome unknown")

            async def close(self):
                calls.append("close")
                if failure == "close":
                    raise RuntimeError("expert still running")

        runtime = GroupRuntime(
            tmp_path / "study", [definition(tmp_path)], lambda ctx: Services()
        )
        run_id = await runtime.start("synthesis", task_file(tmp_path), [])
        event = await runtime.wait()
        assert event["status"] == "needs-reconciliation"
        assert calls == ["settle", "close"]
        assert not list((runtime.root / run_id / "exports").iterdir())
        with pytest.raises(RuntimeError, match="held"):
            await runtime.resume(run_id, [])
        await runtime.close_runtime()

    asyncio.run(scenario())


def test_restart_marks_running_unknown_without_automatic_execution(tmp_path):
    async def scenario():
        study = tmp_path / "study"
        group = definition(tmp_path)
        runtime = GroupRuntime(study, [group])
        run_id = await runtime.start("synthesis", task_file(tmp_path), [])
        await runtime.wait()
        await runtime.close_runtime()
        record_path = runtime.root / run_id / "run.json"
        record = json.loads(record_path.read_text())
        record.update(status="running", output=None)
        record_path.write_text(json.dumps(record))
        runtime = GroupRuntime(study, [group])
        assert runtime.observe(run_id)["status"] == "needs-reconciliation"
        assert runtime._tasks == {}
        with pytest.raises(RuntimeError, match="held"):
            await runtime.resume(run_id, [])
        assert (
            json.loads((runtime.root / run_id / "state.json").read_text())["count"] == 1
        )
        await runtime.close_runtime()

    asyncio.run(scenario())


@pytest.mark.parametrize("artifact", ["outside", "symlink", "directory", "parent"])
def test_invalid_boundary_artifacts_fail(tmp_path, artifact):
    async def scenario():
        prefix = {
            "outside": 'path = ctx.root / "outside.md"\n    path.write_text("outside")',
            "symlink": 'path = ctx.work / "link.md"\n    path.symlink_to(payload["task_ref"])',
            "directory": 'path = ctx.work / "directory"\n    path.mkdir()',
            "parent": 'path = ctx.work / ".." / "outside.md"\n    path.write_text("outside")',
        }[artifact]
        source = """
async def run(ctx, payload):
    PREFIX
    return {"summary": "synthetic", "completion": "", "unfinished": [], "help": "", "artifacts": [str(path)]}
""".replace("PREFIX", prefix)
        runtime = GroupRuntime(tmp_path / "study", [definition(tmp_path, source)])
        run_id = await runtime.start("synthesis", task_file(tmp_path), [])
        assert (await runtime.wait())["status"] == "failed"
        assert runtime.observe(run_id)["output"]["summary"] == "Group execution failed"
        await runtime.close_runtime()

    asyncio.run(scenario())


def test_cross_run_inputs_accept_deliveries_and_reject_work(tmp_path):
    async def scenario():
        runtime = GroupRuntime(tmp_path / "study", [definition(tmp_path)])
        task = task_file(tmp_path)
        first = await runtime.start("synthesis", task, [])
        output = (await runtime.wait())["output"]
        with pytest.raises(ValueError, match="exported"):
            await runtime.start(
                "synthesis", task, [runtime.root / first / "work/report.json"]
            )
        second = await runtime.start("synthesis", task, output["artifacts"])
        assert (await runtime.wait())["run_id"] == second
        await runtime.close_runtime()

    asyncio.run(scenario())


def test_stop_cancels_existing_work_and_rejects_admission(tmp_path):
    async def scenario():
        runtime = GroupRuntime(tmp_path / "study", [definition(tmp_path)])
        task = task_file(tmp_path)
        waiting = asyncio.create_task(runtime.wait())
        await asyncio.sleep(0)
        assert not waiting.done()
        run_id = await runtime.start("synthesis", task, [])
        stopping = asyncio.create_task(runtime.stop())
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="stopped"):
            await runtime.start("synthesis", task, [])
        await stopping
        assert (await waiting)["run_id"] == run_id
        assert runtime.observe(run_id)["status"] == "failed"
        assert "CancelledError" in runtime.observe(run_id)["error"]
        assert await runtime.wait() == {"stopped": True}
        assert await runtime.wait() == {"stopped": True}
        with pytest.raises(RuntimeError, match="stopped"):
            await runtime.resume(run_id, [])

    asyncio.run(scenario())


def test_stop_file_prevents_start_without_program_execution(tmp_path):
    async def scenario():
        study = tmp_path / "study"
        runtime = GroupRuntime(study, [definition(tmp_path)])
        (study / "STOP").touch()
        with pytest.raises(RuntimeError, match="stopped"):
            await runtime.start("synthesis", task_file(tmp_path), [])
        assert runtime.observe()["runs"] == []
        await runtime.close_runtime()

    asyncio.run(scenario())


def test_definition_relative_program_resolves_at_catalog_file(tmp_path):
    group = definition(tmp_path)
    catalog = tmp_path / "definition.json"
    catalog.write_text(json.dumps({**group.metadata(), "program": group.program.name}))
    restored = GroupDefinition.from_json(catalog)
    assert restored.program == group.program
    assert restored.id == group.id


@pytest.mark.parametrize("asynchronous", [False, True])
def test_service_factory_failure_is_recorded(tmp_path, asynchronous):
    async def scenario():
        def factory(ctx):
            assert ctx.study_root == (tmp_path / "study").resolve()
            raise RuntimeError("cannot construct service")

        async def async_factory(ctx):
            return factory(ctx)

        runtime = GroupRuntime(
            tmp_path / "study",
            [definition(tmp_path)],
            async_factory if asynchronous else factory,
        )
        run_id = await runtime.start("synthesis", task_file(tmp_path), [])
        result = await runtime.wait()
        assert result["run_id"] == run_id
        assert result["status"] == "failed"
        assert "cannot construct service" in result["error"]
        await runtime.close_runtime()

    asyncio.run(scenario())


def test_stop_notifies_only_owned_services_and_settles_before_boundary(tmp_path):
    async def scenario():
        source = PROGRAM.replace(
            "await asyncio.sleep(0.01)", "await asyncio.sleep(3600)"
        )
        calls = []

        class Services:
            def __init__(self, ctx):
                self.run_id = ctx.run_id

            async def stop(self):
                calls.append((self.run_id, "stop"))

            async def settle(self):
                calls.append((self.run_id, "settle"))

            async def close(self):
                calls.append((self.run_id, "close"))

        group = definition(tmp_path, source)
        runtime = GroupRuntime(tmp_path / "study", [group], Services)
        other = GroupRuntime(tmp_path / "other-study", [group], Services)
        first = await runtime.start("synthesis", task_file(tmp_path), [])
        second = await other.start("synthesis", task_file(tmp_path), [])
        await asyncio.wait_for(runtime.stop(), 1)
        assert calls == [(first, "stop"), (first, "settle"), (first, "close")]
        assert runtime.observe(first)["status"] == "failed"
        assert other.observe(second)["status"] == "running"
        await other.stop()

    asyncio.run(scenario())


def test_stop_failure_requires_reconciliation(tmp_path):
    async def scenario():
        class Services:
            async def stop(self):
                raise RuntimeError("unknown stop outcome")

            async def settle(self):
                pass

            async def close(self):
                pass

        runtime = GroupRuntime(
            tmp_path / "study", [definition(tmp_path)], lambda ctx: Services()
        )
        run_id = await runtime.start("synthesis", task_file(tmp_path), [])
        await runtime.stop()
        result = runtime.observe(run_id)
        assert result["status"] == "needs-reconciliation"
        assert "unknown stop outcome" in result["error"]

    asyncio.run(scenario())


def test_work_directory_symlink_cannot_export_external_file(tmp_path):
    async def scenario():
        source = """
from pathlib import Path
async def run(ctx, payload):
    ctx.work.rmdir()
    ctx.work.symlink_to(Path(payload["task_ref"]).parent, target_is_directory=True)
    return {"summary": "synthetic", "completion": "", "unfinished": [], "help": "", "artifacts": ["task.md"]}
"""
        runtime = GroupRuntime(tmp_path / "study", [definition(tmp_path, source)])
        run_id = await runtime.start("synthesis", task_file(tmp_path), [])
        assert (await runtime.wait())["status"] == "failed"
        assert runtime.observe(run_id)["output"]["summary"] == "Group execution failed"
        await runtime.close_runtime()

    asyncio.run(scenario())


def test_cross_run_input_rejects_unpublished_export_file(tmp_path):
    async def scenario():
        runtime = GroupRuntime(tmp_path / "study", [definition(tmp_path)])
        task = task_file(tmp_path)
        run_id = await runtime.start("synthesis", task, [])
        await runtime.wait()
        pending = runtime.root / run_id / "exports/000002"
        pending.mkdir()
        report = pending / "unpublished.md"
        report.write_text("not a delivery")
        with pytest.raises(ValueError, match="published boundary"):
            await runtime.start("synthesis", task, [report])
        await runtime.close_runtime()

    asyncio.run(scenario())


def test_settled_failure_publishes_transferable_diagnostic_and_can_close(tmp_path):
    async def scenario():
        source = "async def run(ctx, payload):\n    raise RuntimeError('synthetic failure')\n"
        broken = definition(tmp_path, source)
        healthy = definition(tmp_path, PROGRAM, version="v2")
        healthy = GroupDefinition(
            "diagnose", healthy.version, healthy.description, healthy.program
        )
        calls = []

        class Services:
            async def settle(self):
                calls.append("settle")

            async def close(self):
                calls.append("close")

        runtime = GroupRuntime(
            tmp_path / "study", [broken, healthy], lambda ctx: Services()
        )
        task = task_file(tmp_path)
        run_id = await runtime.start("synthesis", task, [])
        result = await runtime.wait()
        assert result["status"] == "failed"
        assert calls == ["settle", "close"]
        assert result["output"]["completion"] == ""
        report = Path(result["output"]["artifacts"][0])
        assert json.loads(report.read_text()) == {
            "message": "RuntimeError: synthetic failure"
        }
        assert not report.stat().st_mode & stat.S_IWUSR
        followup = await runtime.start("diagnose", task, [report])
        assert (await runtime.wait())["run_id"] == followup
        with pytest.raises(RuntimeError, match="boundary"):
            runtime.hold(run_id)
        with pytest.raises(RuntimeError, match="held"):
            await runtime.resume(run_id, [])
        assert runtime.close(run_id)["status"] == "closed"
        await runtime.close_runtime()

    asyncio.run(scenario())


def test_unknown_effects_never_publish_diagnostic_or_allow_close(tmp_path):
    async def scenario():
        class Services:
            async def settle(self):
                raise RuntimeError("effect unknown")

            async def close(self):
                pass

        runtime = GroupRuntime(
            tmp_path / "study", [definition(tmp_path)], lambda ctx: Services()
        )
        run_id = await runtime.start("synthesis", task_file(tmp_path), [])
        result = await runtime.wait()
        assert result["status"] == "needs-reconciliation"
        assert result["output"] is None
        assert not list((runtime.root / run_id / "exports").iterdir())
        with pytest.raises(RuntimeError, match="boundary"):
            runtime.close(run_id)
        await runtime.close_runtime()

    asyncio.run(scenario())


def test_resume_clears_active_boundary_and_keeps_prior_exports_discoverable(tmp_path):
    from research_loop.group_router import GroupCoordinator

    async def scenario():
        study = tmp_path / "study"
        runtime = GroupRuntime(study, [definition(tmp_path)])
        goal = study / "goal.md"
        goal.write_text("synthetic task")
        run_id = await runtime.start("synthesis", goal, [])
        first = await runtime.wait()
        old_artifact = first["output"]["artifacts"][0]
        old_metadata = runtime.root / run_id / "rounds/000001/output.json"
        runtime.hold(run_id)
        await runtime.resume(run_id, [old_artifact])
        active = runtime.observe(run_id)
        assert active["status"] == "running"
        assert active["output"] is None
        assert (
            json.loads((runtime.root / run_id / "run.json").read_text())["output"]
            is None
        )
        assert json.loads(old_metadata.read_text()) == first["output"]
        coordinator = GroupCoordinator(study, {"groups": []}, None)
        coordinator.runtime = runtime
        packet = coordinator.packet({"iteration": 1})
        row = packet["runs"][0]
        assert row["output"] == {}
        assert [
            coordinator.resolve_ref(ref, packet) for ref in row["prior_artifacts"]
        ] == [old_artifact]
        with pytest.raises(RuntimeError, match="boundary"):
            runtime.hold(run_id)
        second = await runtime.wait()
        assert second["status"] == "returned"
        assert second["output"]["summary"] == "round 2"
        report = json.loads(Path(second["output"]["artifacts"][0]).read_text())
        assert report["previous"] == first["output"]
        runtime.hold(run_id)
        assert runtime.observe(run_id)["status"] == "held"
        await runtime.close_runtime()

    asyncio.run(scenario())
