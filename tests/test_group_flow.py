"""A real filesystem assistance round trip, with scripted model decisions."""

import asyncio
import json

from research_loop import group_router, human_input, loop
from test_group_router import decision

MAIN_PROGRAM = """
import json
from pathlib import Path

async def run(ctx, payload):
    if ctx.round == 1:
        (ctx.work / 'question.txt').write_text('Return the integer 42.')
        return dict(summary='Need arithmetic evidence', completion='',
                    unfinished=['arithmetic'], help='Need arithmetic specialist',
                    artifacts=['question.txt'])
    answer = Path(payload['feedback_refs'][0]).read_text()
    assert answer == '42'
    call = json.loads(Path(payload['input_refs'][0]).read_text())
    request = call['input']['human_request']
    result = dict(summary='Observed '+answer, goal='unknown', evidence=payload['feedback_refs'],
                  limitations=['synthetic'], next_action='', study_action='stop',
                  stop_reason='bounded task complete',
                  human_request=dict(id=request['id'],status='completed',
                                     evidence=payload['feedback_refs'], reason='executed and analyzed'))
    (ctx.work/'result.json').write_text(json.dumps(result))
    return dict(summary='Observed 42',completion='requested arithmetic complete',
                unfinished=[],help='',artifacts=['result.json'])
"""
HELPER_PROGRAM = """
from pathlib import Path

async def run(ctx, payload):
    assert Path(payload['task_ref']).read_text() == 'Return the integer 42.'
    (ctx.work/'answer.txt').write_text(str(6*7))
    return dict(summary='Computed 42',completion='computed requested integer',
                unfinished=[],help='',artifacts=['answer.txt'])
"""


def test_assistance_hold_resume_and_human_request_publication(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    goal = tmp_path / "goal.md"
    goal.write_text("Compute a bounded arithmetic result.")
    (tmp_path / "main.py").write_text(MAIN_PROGRAM)
    (tmp_path / "helper.py").write_text(HELPER_PROGRAM)
    catalog = tmp_path / "groups.json"
    catalog.write_text(
        json.dumps(
            [
                dict(
                    id="experiment",
                    version="1",
                    description="Bounded result",
                    program="main.py",
                ),
                dict(
                    id="arithmetic",
                    version="1",
                    description="Arithmetic",
                    program="helper.py",
                ),
            ]
        )
    )
    root = tmp_path / "study"
    loop.initialize(root, project, goal, coordinator="router", groups_file=catalog)
    human_input.submit(root, "Compute 6 times 7.")
    seen = []

    class Services:
        def __init__(self, *args, **kwargs):
            pass

        async def settle(self):
            pass

        async def close(self):
            pass

    class Native:
        def __init__(self, *args, **kwargs):
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

        async def run(self, policy, packet, schema, **kwargs):
            assert packet["request"] == "Compute 6 times 7."
            runs = {r["group_id"]: r for r in packet["runs"]}
            main, helper = runs.get("experiment"), runs.get("arithmetic")
            if main is None:
                d = decision(
                    "start",
                    group_id="experiment",
                    task_ref=packet["goal_ref"],
                    input_refs=[packet["request_ref"]],
                )
            elif any(r["status"] == "running" for r in runs.values()):
                d = decision("wait")
            elif main["status"] == "returned" and main["output"]["help"]:
                d = decision("hold", run_id=main["id"])
            elif helper is None:
                d = decision(
                    "start",
                    group_id="arithmetic",
                    task_ref=main["output"]["artifacts"][0],
                )
            elif helper["status"] == "returned":
                d = decision("close", run_id=helper["id"])
            elif main["status"] == "held":
                d = decision(
                    "resume",
                    run_id=main["id"],
                    input_refs=helper["output"]["artifacts"],
                )
            elif main["status"] == "returned":
                d = decision("close", run_id=main["id"])
            else:
                d = decision("finish_round", result_ref=main["output"]["artifacts"][0])
            seen.append(d["action"])
            return d

    monkeypatch.setattr(group_router, "configuration", lambda *a, **k: (None, {}))
    monkeypatch.setattr(group_router, "NativeGroupServices", Services)
    state = asyncio.run(loop.run(root, native_factory=Native))
    assert state["status"] == "completed"
    assert state["result"]["summary"] == "Observed 42"
    assert (root / "inputs/human.md").read_text() == ""
    assert seen.count("start") == 2 and seen.count("resume") == 1
    assert "hold" in seen and seen[-1] == "finish_round"
    records = [
        json.loads(p.read_text()) for p in (root / "group-runs").glob("*/run.json")
    ]
    assert len(records) == 2 and all(r["status"] == "closed" for r in records)
    assert sorted(r["round"] for r in records) == [1, 2]
    assert len(list((root / "knowledge/current/groups").glob("*/*/*"))) == 2
