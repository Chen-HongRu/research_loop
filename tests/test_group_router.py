"""Current Router transport, dynamic pointer schemas and immutable boundary inputs."""

import asyncio
import json
from copy import deepcopy

import pytest

from research_loop import group_router
from research_loop.state import Journal, write_json
from research_workflows.research import create


def decision(action, **values):
    return dict(
        action=action,
        group_id=values.get("group_id", ""),
        run_id=values.get("run_id", ""),
        task_ref=values.get("task_ref", ""),
        input_refs=values.get("input_refs", []),
        prompt=values.get("prompt", ""),
        result_ref=values.get("result_ref", ""),
        reason="Synthetic routing evidence",
    )


def assert_pointer_choices(schema, packet):
    fields = schema["properties"]
    refs = list(packet["references"])
    assert fields["task_ref"]["enum"] == ["", *refs]
    assert fields["result_ref"]["enum"] == ["", *refs]
    assert fields["input_refs"]["items"]["enum"] == refs
    assert fields["group_id"]["enum"] == ["", *[g["id"] for g in packet["groups"]]]
    assert fields["run_id"]["enum"] == ["", *[r["id"] for r in packet["runs"]]]
    assert fields["action"]["enum"] == packet["allowed_actions"]


def test_router_uses_current_ids_declared_profile_and_no_tools(tmp_path, monkeypatch):
    captured, configured, settled = [], [], []
    response = decision("start", group_id="job", task_ref="f0")
    config = dict(
        revision="r1",
        router={"profile": "coordination"},
        profiles={"coordination": {"model": "gpt-6.1-sol", "effort": "medium"}},
    )
    packet = dict(
        references={"f0": "goal.md", "f9": "old-evidence.json"},
        groups=[{"id": "job"}],
        runs=[],
        allowed_actions=["start", "escalate"],
    )

    class Native:
        def __init__(self, config, journal, tools):
            assert tools == []

        async def open(self):
            pass

        async def verify(self, model, effort):
            assert (model, effort) == ("gpt-6.1-sol", "medium")

        async def stop(self):
            settled.append("stop")

        async def settle(self):
            settled.append("settle")

        async def close(self):
            settled.append("close")

        async def run(self, policy, value, schema, **kwargs):
            assert policy == "Exact workflow policy"
            assert kwargs["model"] == "gpt-6.1-sol" and kwargs["effort"] == "medium"
            assert kwargs["allow_tools"] is False
            assert_pointer_choices(schema, value)
            captured.append(deepcopy(schema))
            return dict(response)

    def configure(*args, **kwargs):
        assert kwargs == {"allow_tools": False}
        configured.append(args)
        return None, {"allow_tools": False}

    monkeypatch.setattr(group_router, "configuration", configure)
    journal = Journal(tmp_path / "events.jsonl")
    router = group_router.Router(tmp_path, journal, native_factory=Native)

    async def run():
        assert (
            await router.decide(config, "Exact workflow policy", packet, iteration=1)
            == response
        )
        packet["references"]["f15"] = "new-evidence.json"
        packet["groups"].append({"id": "helper"})
        packet["runs"].append({"id": "waiting", "status": "held"})
        response.update(group_id="helper", task_ref="f15", input_refs=["f9"])
        assert (
            await router.decide(config, "Exact workflow policy", packet, iteration=1)
            == response
        )
        response["task_ref"] = "new-evidence.json"
        with pytest.raises(ValueError, match="unsupplied artifact"):
            await router.decide(config, "Exact workflow policy", packet, iteration=1)

    asyncio.run(run())
    journal.close()
    assert len(configured) == 3 and settled == ["stop", "settle", "close"] * 3
    assert captured[0]["properties"]["run_id"]["enum"] == [""]
    assert captured[1]["properties"]["run_id"]["enum"] == ["", "waiting"]
    assert group_router.DECISION_SCHEMA["properties"]["task_ref"] == {"type": "string"}
    receipts = [
        json.loads(path.read_text())
        for path in (tmp_path / "groups/router").glob("*/call.json")
    ]
    assert all(
        row["policy"] == "Exact workflow policy" and row["config_revision"] == "r1"
        for row in receipts
    )
    records = [
        json.loads(path.read_text())
        for path in (tmp_path / "groups/router").glob("*/records/decision/*.json")
    ]
    assert sorted(row["status"] for row in records) == [
        "completed",
        "completed",
        "failed",
    ]


@pytest.mark.parametrize(
    "action,status,valid",
    [
        ("hold", "returned", True),
        ("close", "failed", True),
        ("resume", "held", True),
        ("close", "needs-reconciliation", False),
        ("hold", "running", False),
        ("resume", "returned", False),
    ],
)
def test_router_checks_actual_boundary_status(action, status, valid):
    packet = dict(
        references={"f0": "known"},
        groups=[{"id": "job"}],
        runs=[{"id": "r", "status": status}],
        allowed_actions=list(group_router.ACTIONS),
    )
    value = decision(action, run_id="r")
    if valid:
        assert group_router.validate_decision(value, packet) == value
    else:
        with pytest.raises(ValueError, match="unavailable Group boundary"):
            group_router.validate_decision(value, packet)


def test_router_rejects_unknown_references_unavailable_actions_and_idle_wait():
    packet = dict(
        references={"f0": "known"},
        groups=[{"id": "job"}],
        runs=[],
        allowed_actions=["start", "wait", "escalate"],
    )
    with pytest.raises(ValueError, match="unsupplied"):
        group_router.validate_decision(
            decision("start", group_id="job", task_ref="path-not-id"), packet
        )
    with pytest.raises(ValueError, match="unavailable at this boundary"):
        group_router.validate_decision(
            decision("finish_round", result_ref="f0"), packet
        )
    with pytest.raises(ValueError, match="No running Group"):
        group_router.validate_decision(decision("wait"), packet)


def test_packet_exposes_only_boundary_and_prior_exports_and_operator_inputs(tmp_path):
    (tmp_path / "goal.md").write_text("A synthetic goal")
    (tmp_path / "call.json").write_text("{}")
    operator = tmp_path / "operator.json"
    operator.write_text("{}")
    prior, final, secret = [
        str(tmp_path / name)
        for name in ("prior.json", "final.json", "private-work.json")
    ]
    directory = tmp_path / "group-runs/r/rounds/000001"
    directory.mkdir(parents=True)
    write_json(directory / "output.json", {"artifacts": [prior]})
    row = dict(
        run_id="r",
        group_id="job",
        config_revision="r1",
        status="held",
        round=2,
        task_ref=str(tmp_path / "goal.md"),
        output={"help": "Need helper", "artifacts": [final]},
    )
    config = dict(
        revision="r1",
        groups=[dict(id="job", version="1", description="Bounded job")],
        input_refs=[str(operator)],
    )
    value = group_router.packet(
        tmp_path,
        config,
        [row],
        ["r"],
        {},
        request={"id": "input", "text": "Do this first"},
    )
    paths = set(value["references"].values())
    assert {prior, final, str(operator)} <= paths and secret not in paths
    assert value["references"][value["runs"][0]["prior_artifacts"][0]] == prior
    assert value["references"][value["input_refs"][0]] == str(operator)
    row["status"] = "running"
    running = group_router.packet(tmp_path, config, [row], ["r"], {})
    assert final not in running["references"].values()
    row["status"] = "closed"
    assert group_router.packet(tmp_path, config, [row], [], {})["runs"] == []


def test_reference_policy_owns_wait_hold_close_and_refuses_unknown_effects():
    workflow = create({})
    rows = [
        {
            "run_id": "r",
            "status": "returned",
            "output": {"completion": "completed", "help": "", "unfinished": []},
        }
    ]
    view = dict(runs=rows, admitted=["r"])
    assert workflow.advance(view) == {"action": "close", "run_id": "r"}
    rows[0]["output"]["help"] = "Need scientific evidence"
    assert workflow.advance(view) == {"action": "hold", "run_id": "r"}
    rows[0]["status"] = "running"
    assert workflow.advance(view) == {"action": "wait"}
    rows.append({"run_id": "unknown", "status": "needs-reconciliation"})
    with pytest.raises(RuntimeError, match="operator reconciliation"):
        workflow.advance(view)
    assert workflow.route({"runs": []})["allowed_actions"] == ["start", "escalate"]


def test_reference_never_finishes_while_assistance_is_held(tmp_path):
    workflow = create({})
    rows = [
        {
            "run_id": "waiting",
            "status": "held",
            "output": {"help": "Need evidence", "artifacts": []},
        },
        {
            "run_id": "candidate",
            "status": "closed",
            "output": {
                "completion": "completed",
                "help": "",
                "unfinished": [],
                "artifacts": ["candidate.json"],
            },
        },
    ]
    view = dict(root=tmp_path, runs=rows, admitted=["waiting", "candidate"])
    assert workflow.advance(view) is None
    with pytest.raises(ValueError, match="All Group Runs must be closed"):
        workflow.complete(view, "candidate.json")


def test_reference_closes_only_failures_with_settled_diagnostic_exports():
    workflow = create({})
    row = {"run_id": "failure", "status": "failed", "output": {"artifacts": []}}
    view = dict(runs=[row], admitted=["failure"])
    with pytest.raises(RuntimeError, match="no settled diagnostic artifact"):
        workflow.advance(view)
    row["output"]["artifacts"] = ["runtime-error.json"]
    assert workflow.advance(view) == {"action": "close", "run_id": "failure"}
