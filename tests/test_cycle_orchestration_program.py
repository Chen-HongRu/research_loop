"""Candidate orchestration has a small, auditable Python surface."""

import asyncio
import json

import pytest

from benchmarks.experiment_cycle.orchestration_program import (
    DEFAULT_PATH,
    MAX_COMMON_BYTES,
    MAX_SOURCE_BYTES,
    load_orchestration,
)
from benchmarks.experiment_cycle.policy import load_policy


class Team:
    def __init__(self):
        self.events = []

    async def call(self, name, **kwargs):
        self.events.append(("call", name, kwargs))
        return {"ok": True, "name": name}

    async def message(self, name, **kwargs):
        self.events.append(("message", name, kwargs))
        return {"ok": True}

    async def execute(self):
        self.events.append(("execute",))
        return {"ok": True}


def load(tmp_path, source):
    path = tmp_path / "candidate.py"
    path.write_text(source)
    return load_orchestration(path)


def test_default_matches_policy():
    program = load_orchestration()
    policy = load_policy()
    assert program.source == DEFAULT_PATH.read_text()
    assert program.common == policy["common"]
    team = Team()
    assert asyncio.run(program.run(team)) == {"ok": True, "name": "evaluate"}
    assert team.events == [
        (
            "call",
            "plan",
            {
                "role": "plan",
                "context": "plan",
                "instructions": policy["stages"]["plan"]["instructions"],
            },
        ),
        (
            "call",
            "prepare",
            {
                "role": "experiment",
                "context": "experiment",
                "instructions": policy["stages"]["prepare"]["instructions"],
            },
        ),
        ("execute",),
        (
            "call",
            "evaluate",
            {
                "role": "critic",
                "context": "critic",
                "instructions": policy["stages"]["evaluate"]["instructions"],
            },
        ),
    ]


def test_unrolled_message_branch_and_shared_context(tmp_path):
    program = load(
        tmp_path,
        """COMMON = "common"
async def orchestrate(team):
    result = await team.call("plan", role="plan", context="shared", instructions="plan")
    if result["ok"]:
        await team.message("plan", context="shared", handoff={"result": result["name"]})
        await team.execute()
        return await team.call("evaluate", role="critic", context="shared", instructions=f"review {result['name']}")
    else:
        return {"ok": False}
""",
    )
    team = Team()
    assert asyncio.run(program.run(team))["name"] == "evaluate"
    assert team.events[1] == (
        "message",
        "plan",
        {"context": "shared", "handoff": {"result": "plan"}},
    )
    assert team.events[-1][2]["instructions"] == "review plan"


@pytest.mark.parametrize(
    "body",
    [
        "import os",
        "x = team.__class__",
        "while True: pass",
        "def helper(): pass",
        "result['key'] = 1",
        "await team.other()",
        "team.call('plan')",
        "f = team.call",
        "await team.call(*(1,))",
        "await team.call('plan', **{})",
        "team = 1",
        "COMMON = 'changed'",
    ],
)
def test_rejects_unsafe_forms(tmp_path, body):
    source = f'COMMON = "common"\nasync def orchestrate(team):\n    {body}\n'
    with pytest.raises(ValueError):
        load(tmp_path, source)


def test_rejects_extra_top_level_and_limits(tmp_path):
    for source in [
        'import os\nCOMMON = "x"\nasync def orchestrate(team): pass\n',
        'COMMON = "x"\nasync def orchestrate(team): return None\ndef helper(): pass\n',
        f"COMMON = {json.dumps('x' * (MAX_COMMON_BYTES + 1))}\nasync def orchestrate(team): return None\n",
        " " * (MAX_SOURCE_BYTES + 1),
    ]:
        with pytest.raises(ValueError):
            load(tmp_path, source)
