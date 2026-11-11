import asyncio
import json

import pytest

from research_loop import group_router, loop
from research_loop.instructions import RESULT_SCHEMA


PROGRAM = """
import json
from pathlib import Path

async def run(ctx, payload):
    number = len(Path(payload['task_ref']).read_text())
    result = {'summary': f'CPU count {number}', 'goal': 'unknown',
              'evidence': [], 'limitations': ['synthetic'], 'next_action': '',
              'study_action': 'stop', 'stop_reason': 'synthetic complete'}
    (ctx.work / 'result.json').write_text(json.dumps(result))
    return {'summary': 'done', 'completion': 'done', 'unfinished': [],
            'help': '', 'artifacts': ['result.json']}
"""


def decision(action, **values):
    result = dict(
        action=action,
        group_id="",
        run_id="",
        task_ref="",
        input_refs=[],
        prompt="",
        result_ref="",
        reason="synthetic test",
    )
    result.update(values)
    return result


def setup_study(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    goal = tmp_path / "goal.md"
    goal.write_text("synthetic CPU task")
    program = tmp_path / "program.py"
    program.write_text(PROGRAM)
    catalog = tmp_path / "groups.json"
    catalog.write_text(
        json.dumps(
            [dict(id="test", version="1", description="CPU", program="program.py")]
        )
    )
    root = tmp_path / "study"
    loop.initialize(root, project, goal, coordinator="router", groups_file=catalog)
    return root, program, catalog


def test_router_main_loop_publishes_frozen_group_result(tmp_path, monkeypatch):
    root, program, catalog = setup_study(tmp_path)
    program.write_text('raise RuntimeError("changed source must not run")')
    catalog.write_text("[]")
    configurations = []
    transports = []

    def configuration(*args, **kwargs):
        configurations.append(kwargs)
        return None, {}

    class Services:
        def __init__(self, *args, **kwargs):
            pass

        async def settle(self):
            pass

        async def close(self):
            pass

    class FakeNative:
        def __init__(self, config, journal, tools):
            assert tools == []

        async def open(self):
            pass

        async def verify(self, *args, **kwargs):
            pass

        async def run(self, policy, packet, schema, **kwargs):
            transports.append(kwargs)
            assert schema == group_router.DECISION_SCHEMA
            if not packet["runs"]:
                return decision("start", group_id="test", task_ref=packet["goal_ref"])
            row = packet["runs"][0]
            if row["status"] == "running":
                return decision("wait")
            if row["status"] == "returned":
                return decision("close", run_id=row["id"])
            return decision("finish_round", result_ref=row["output"]["artifacts"][0])

        async def stop(self):
            pass

        async def settle(self):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(group_router, "configuration", configuration)
    monkeypatch.setattr(group_router, "NativeGroupServices", Services)
    state = asyncio.run(loop.run(root, native_factory=FakeNative))
    assert state["status"] == "completed"
    assert state["result"]["summary"] == "CPU count 18"
    assert state["result"]["study_action"] == "stop"
    assert (root / "knowledge/current").exists()
    assert all(
        c["allow_tools"] is False and c["delegation"] == "none" for c in configurations
    )
    assert all(
        c["allow_tools"] is False and c["delegation"] == "none" for c in transports
    )
    assert (
        json.loads((root / "iterations/000001/result.json").read_text())
        == state["result"]
    )


def test_router_rejects_unsupplied_reference_and_running_intervention():
    packet = {
        "references": ["/known"],
        "groups": [{"id": "test"}],
        "runs": [{"id": "r", "status": "running"}],
    }
    with pytest.raises(ValueError, match="unsupplied"):
        group_router.validate_decision(
            decision("start", group_id="test", task_ref="/unknown"), packet
        )
    with pytest.raises(ValueError, match="returned"):
        group_router.validate_decision(decision("hold", run_id="r"), packet)


def test_finish_requires_closed_current_round_result(tmp_path):
    result = tmp_path / "result.json"
    result.write_text(json.dumps({k: "" for k in RESULT_SCHEMA["required"]}))
    coordinator = group_router.GroupCoordinator(tmp_path, {"groups": []}, None)
    coordinator.result_schema = RESULT_SCHEMA
    coordinator.admitted = {"r"}
    packet = {
        "runs": [
            {"id": "r", "status": "returned", "output": {"artifacts": [str(result)]}}
        ]
    }
    with pytest.raises(ValueError, match="closed"):
        coordinator.study_result(str(result), packet)
    packet["runs"][0]["status"] = "closed"
    coordinator.admitted.clear()
    with pytest.raises(ValueError, match="current-round"):
        coordinator.study_result(str(result), packet)


def test_router_may_close_settled_failure_but_not_uncertain_effects():
    packet = {"references": [], "groups": [], "runs": [{"id": "r", "status": "failed"}]}
    group_router.validate_decision(decision("close", run_id="r"), packet)
    with pytest.raises(ValueError, match="returned"):
        group_router.validate_decision(decision("hold", run_id="r"), packet)
    packet["runs"][0]["status"] = "needs-reconciliation"
    with pytest.raises(ValueError, match="returned"):
        group_router.validate_decision(decision("close", run_id="r"), packet)


def test_packet_reference_paths_round_trip_without_changing_ids(tmp_path):
    from types import SimpleNamespace

    root = tmp_path / "study"
    root.mkdir()
    (root / "goal.md").write_text("synthetic scope")
    artifact = str(root / "group-runs/run/exports/000001/report.json")
    outside = str(tmp_path / "outside.json")
    coordinator = group_router.GroupCoordinator(root, {"groups": []}, None)
    coordinator.runtime = SimpleNamespace(
        observe=lambda: {
            "runs": [
                {
                    "run_id": "run",
                    "group_id": "test",
                    "status": "returned",
                    "task_ref": str(root / "goal.md"),
                    "output": {
                        "summary": "done",
                        "completion": "completed",
                        "help": "",
                        "unfinished": [],
                        "artifacts": [artifact, outside],
                    },
                }
            ]
        }
    )
    packet = coordinator.packet({"iteration": 1})
    original = sorted(
        {
            str(root / "goal.md"),
            str(root / "call.json"),
            str(root / "knowledge/current/index.md"),
            artifact,
            outside,
        }
    )
    assert list(packet["references"]) == [f"f{index}" for index in range(len(original))]
    for index, full_path in enumerate(original):
        ref_id = f"f{index}"
        assert coordinator.resolve_ref(ref_id, packet) == full_path
        displayed = packet["references"][ref_id]
        assert displayed == (
            full_path.removeprefix(str(root) + "/")
            if full_path.startswith(str(root) + "/")
            else full_path
        )
    assert [
        coordinator.resolve_ref(ref, packet)
        for ref in packet["runs"][0]["output"]["artifacts"]
    ] == [artifact, outside]
    assert coordinator.resolve_ref(artifact, packet) == artifact
    assert (
        coordinator.resolve_ref("relative-already-full-ref.json", packet)
        == "relative-already-full-ref.json"
    )
    assert coordinator.resolve_ref("", packet) == ""


def test_packet_size_is_independent_of_study_root_prefix_length(tmp_path):
    from types import SimpleNamespace

    def packet_at(root):
        root.mkdir(parents=True)
        (root / "goal.md").write_text("same scientific task")
        coordinator = group_router.GroupCoordinator(root, {"groups": []}, None)
        coordinator.runtime = SimpleNamespace(
            observe=lambda: {
                "runs": [
                    {
                        "run_id": "run",
                        "group_id": "test",
                        "status": "returned",
                        "task_ref": str(root / "goal.md"),
                        "output": {
                            "summary": "same result",
                            "completion": "completed",
                            "help": "",
                            "unfinished": [],
                            "artifacts": [
                                str(root / "group-runs/run/exports/000001/report.json")
                            ],
                        },
                    }
                ]
            }
        )
        return coordinator.packet({"iteration": 1})

    short = packet_at(tmp_path / "short")
    long = packet_at(tmp_path / ("long-study-prefix-" * 12) / ("nested-" * 25))
    assert long == short
    assert len(json.dumps(long, ensure_ascii=False)) == len(
        json.dumps(short, ensure_ascii=False)
    )


def test_prior_round_exports_remain_discoverable_and_reach_next_group(
    tmp_path, monkeypatch
):
    from pathlib import Path
    from research_loop.groups import GroupDefinition, GroupRuntime

    root = tmp_path / "study"
    root.mkdir()
    goal = root / "goal.md"
    goal.write_text("synthetic original task")
    program = tmp_path / "rounds.py"
    program.write_text("""
import json
from pathlib import Path
async def run(ctx, payload):
    name = "evaluate.json" if ctx.round == 1 else "final.json"
    source = Path(payload["task_ref"]).read_text()
    (ctx.work / name).write_text(json.dumps({"round": ctx.round, "source": source}))
    return {"summary": "filename repaired" if ctx.round == 2 else "original assessment",
            "completion": "completed", "help": "", "unfinished": [], "artifacts": [name]}
""")
    definition = GroupDefinition("test", "1", "Synthetic evidence handoff", program)

    async def check():
        runtime = GroupRuntime(root, [definition])
        run_id = await runtime.start("test", str(goal), [])
        first = await runtime.wait()
        original = first["output"]["artifacts"][0]
        runtime.hold(run_id)
        await runtime.resume(run_id, [], prompt="Repair only the result filename")
        latest = await runtime.wait()
        current = latest["output"]["artifacts"][0]
        coordinator = group_router.GroupCoordinator(
            root, {"groups": [group_router.definition_record(definition)]}, None
        )
        coordinator.runtime = runtime
        reads = []
        original_read = Path.read_text

        def record_read(path, *args, **kwargs):
            reads.append(path)
            return original_read(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_text", record_read)
            packet = coordinator.packet({"iteration": 1})
        row = packet["runs"][0]
        assert [
            coordinator.resolve_ref(ref, packet) for ref in row["prior_artifacts"]
        ] == [original]
        assert [
            coordinator.resolve_ref(ref, packet) for ref in row["output"]["artifacts"]
        ] == [current]
        assert row["output"]["summary"] == "filename repaired"
        assert reads == [
            root / "group-runs" / run_id / "rounds/000001/output.json",
            goal,
        ]
        # A running next round has no output.json yet; all earlier published
        # rounds remain discoverable without exposing a stale active boundary.
        from types import SimpleNamespace

        coordinator.runtime = SimpleNamespace(
            observe=lambda: {"runs": [{**latest, "round": 3, "status": "running"}]}
        )
        active_packet = coordinator.packet({"iteration": 1})
        active = active_packet["runs"][0]
        assert [
            coordinator.resolve_ref(ref, active_packet)
            for ref in active["prior_artifacts"]
        ] == [original, current]
        assert active["output"] == {}
        coordinator.runtime = runtime
        old_ref = row["prior_artifacts"][0]
        assert not Path(packet["references"][old_ref]).is_absolute()
        handoff = decision(
            "start", group_id="test", task_ref=packet["goal_ref"], input_refs=[old_ref]
        )
        group_router.validate_decision(handoff, packet)
        followup = await runtime.start(
            "test",
            coordinator.resolve_ref(old_ref, packet),
            [coordinator.resolve_ref(ref, packet) for ref in handoff["input_refs"]],
        )
        delivered = await runtime.wait()
        assert delivered["task_ref"] == original
        assert (
            json.loads(Path(delivered["output"]["artifacts"][0]).read_text())["source"]
            == Path(original).read_text()
        )
        # Mechanical publication preserves earlier evidence for the next Study
        # iteration, without granting old artifacts final-result eligibility.
        from research_loop.state import Knowledge

        coordinator.admitted = {run_id}
        closed_packet = {**packet, "runs": [{**row, "status": "closed"}]}
        with pytest.raises(ValueError, match="current-round Group artifact"):
            coordinator.study_result(original, closed_packet)
        knowledge = Knowledge(root)
        baseline = knowledge.prepare()
        duplicate_packet = {
            **packet,
            "runs": [
                {
                    **row,
                    "prior_artifacts": [old_ref, old_ref, *row["output"]["artifacts"]],
                }
            ],
        }
        coordinator.publish_artifacts(duplicate_packet)
        relative = (
            Path("groups")
            / run_id
            / Path(original).relative_to(runtime.root / run_id / "exports")
        )
        index = (knowledge.draft / "group-artifacts.md").read_text()
        assert index.count(relative.as_posix()) == 1
        assert index.count("final.json") == 2  # One link label and one target.
        knowledge.publish(baseline)
        published_original = knowledge.current / relative
        assert published_original.read_text() == Path(original).read_text()
        consumer = await runtime.start("test", published_original, [])
        next_iteration = await runtime.wait()
        assert next_iteration["task_ref"] == str(published_original.resolve())
        assert (
            json.loads(Path(next_iteration["output"]["artifacts"][0]).read_text())[
                "source"
            ]
            == Path(original).read_text()
        )
        runtime.close(consumer)
        coordinator.admitted.clear()
        runtime.close(run_id)
        runtime.close(followup)
        with monkeypatch.context() as patch:
            reads.clear()
            patch.setattr(Path, "read_text", record_read)
            assert coordinator.packet({"iteration": 1})["runs"] == []
        assert reads == [goal]
        await runtime.close_runtime()

    asyncio.run(check())
