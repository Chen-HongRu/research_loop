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
