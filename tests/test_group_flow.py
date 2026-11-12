"""A real assistance handoff through the current workflow/controller interfaces."""

import asyncio
import json

import yaml

from research_loop import application, group_router
from research_loop.controller import run_foreground
from research_loop.interventions import get_intervention
from test_group_router import decision

MAIN = """
import json
from pathlib import Path
async def run(ctx, payload):
    if ctx.round == 1:
        (ctx.work / 'question.txt').write_text('Return the integer 42.')
        return dict(summary='Need arithmetic evidence', completion='blocked',
                    unfinished=['arithmetic'], help='Need arithmetic specialist', artifacts=['question.txt'])
    answer = Path(payload['feedback_refs'][0]).read_text()
    assert answer == '42'
    request = payload['study_context']['request']
    (ctx.work / 'result.json').write_text(json.dumps({'answer': int(answer), 'input_id': request['id']}))
    return dict(summary='Observed 42', completion='completed', unfinished=[], help='', artifacts=['result.json'])
"""
HELPER = """
from pathlib import Path
async def run(ctx, payload):
    assert Path(payload['task_ref']).read_text() == 'Return the integer 42.'
    (ctx.work / 'answer.txt').write_text(str(6 * 7))
    return dict(summary='Computed 42', completion='completed', unfinished=[], help='', artifacts=['answer.txt'])
"""
WORKFLOW = """
import json
from pathlib import Path
class Workflow:
    router_policy = 'Route bounded jobs; never author their result.'
    max_active_groups = 1
    def context(self, root, config, iteration, previous_result, request):
        return {'request': request}
    def validate_input(self, kind, payload):
        if kind != 'task' or set(payload) != {'text'}:
            raise ValueError('Expected task text')
        return payload
    def route(self, packet):
        return {**packet, 'allowed_actions': ['start', 'resume', 'escalate']}
    def advance(self, view):
        rows = view['runs']
        for row in rows:
            if row['status'] == 'running': return {'action': 'wait'}
            if row['status'] == 'returned':
                return {'action': 'hold' if row['output']['help'] else 'close', 'run_id': row['run_id']}
        if rows and all(row['status'] == 'closed' for row in rows):
            main = next(row for row in rows if row['group_id'] == 'main')
            return {'action': 'finish_round', 'result_ref': main['output']['artifacts'][0]}
    def complete(self, view, result_ref):
        artifacts = []
        for row in view['runs']:
            for number in range(1, row['round'] + 1):
                metadata = Path(view['root']) / 'group-runs' / row['run_id'] / 'rounds' / f'{number:06d}' / 'output.json'
                for ref in json.loads(metadata.read_text())['artifacts']:
                    artifacts.append({'source': ref, 'destination': f"groups/{row['run_id']}/{number:06d}/{Path(ref).name}"})
        return {'action': 'stop', 'result': json.loads(Path(result_ref).read_text()), 'artifacts': artifacts,
                'input_outcome': {'id': view['input']['request']['id'], 'status': 'completed'}}
def create(settings): return Workflow()
"""


def test_assistance_hold_resume_exports_and_work_input_completion(
    tmp_path, monkeypatch
):
    author = tmp_path / "author"
    author.mkdir()
    for name, content in (
        ("main.py", MAIN),
        ("helper.py", HELPER),
        ("workflow.py", WORKFLOW),
        ("goal.md", "Compute a bounded arithmetic result."),
    ):
        (author / name).write_text(content)
    source = author / "run.yaml"
    source.write_text(
        yaml.safe_dump(
            dict(
                version=1,
                workflow={"program": "workflow.py"},
                router={"profile": "coordination"},
                profiles={"coordination": {"model": "gpt-6.1-sol", "effort": "medium"}},
                agents={},
                groups={
                    "main": {
                        "version": "1",
                        "description": "Needs arithmetic evidence",
                        "program": "main.py",
                    },
                    "arithmetic": {
                        "version": "1",
                        "description": "Answers bounded arithmetic",
                        "program": "helper.py",
                    },
                },
            )
        )
    )
    root = tmp_path / "run"
    application.prepare(source, root, project=author, goal=author / "goal.md")
    receipt = application.intervene(
        root,
        kind="task",
        payload={"text": "Compute 6 times 7."},
        identity="priority-task",
    )
    seen = []

    class Native:
        def __init__(self, config, journal, tools):
            assert tools == []

        async def open(self):
            pass

        async def verify(self, *args):
            pass

        async def stop(self):
            pass

        async def settle(self):
            pass

        async def close(self):
            pass

        async def run(self, policy, packet, schema, **kwargs):
            assert kwargs["allow_tools"] is False
            assert json.loads(packet["request"])["text"] == "Compute 6 times 7."
            assert all(row["status"] in {"held", "closed"} for row in packet["runs"])
            rows = {row["group_id"]: row for row in packet["runs"]}
            if not rows:
                value = decision("start", group_id="main", task_ref=packet["goal_ref"])
            elif "arithmetic" not in rows:
                value = decision(
                    "start",
                    group_id="arithmetic",
                    task_ref=rows["main"]["output"]["artifacts"][0],
                )
            else:
                assert (
                    rows["main"]["status"] == "held"
                    and rows["arithmetic"]["status"] == "closed"
                )
                value = decision(
                    "resume",
                    run_id=rows["main"]["id"],
                    input_refs=rows["arithmetic"]["output"]["artifacts"],
                )
            seen.append(value["action"])
            return value

    monkeypatch.setattr(group_router, "configuration", lambda *a, **k: (None, {}))
    state = asyncio.run(run_foreground(root, native_factory=Native))
    assert state["status"] == "completed" and state["result"] == {
        "answer": 42,
        "input_id": "priority-task",
    }
    assert seen == ["start", "start", "resume"]
    outcome = get_intervention(root, receipt["id"])
    assert outcome["status"] == "applied"
    assert outcome["work_outcome"] == "completed"
    rows = [
        json.loads(path.read_text())
        for path in (root / "group-runs").glob("*/run.json")
    ]
    assert len(rows) == 2 and all(row["status"] == "closed" for row in rows)
    assert sorted(row["round"] for row in rows) == [1, 2]
    published = list((root / "outputs/current/groups").glob("*/*/*"))
    assert {path.name for path in published} == {
        "question.txt",
        "answer.txt",
        "result.json",
    }
    assert (
        next(path for path in published if path.name == "question.txt").read_text()
        == "Return the integer 42."
    )
