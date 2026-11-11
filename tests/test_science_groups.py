"""Scientific Group contracts exercised through real pinned Runs and exports."""

import asyncio
import copy
import json
from pathlib import Path

import pytest

from research_loop.groups import GroupDefinition, GroupRuntime
from research_loop.human_input import result_schema
from research_loop.instructions import RESULT_SCHEMA


PROGRAMS = Path(__file__).resolve().parents[1] / "src/research_loop/group_programs"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return str(path)


class Services:
    def __init__(self, ctx, responses, calls, study_context):
        self.ctx = ctx
        self.responses = responses
        self.calls = calls
        self.study_context = study_context

    async def call(self, role, context, instructions, payload, schema):
        self.calls.append((role, context, instructions, payload, schema))
        expected_role, response = self.responses.pop(0)
        assert role == expected_role
        if callable(response):
            response = response(self.ctx, payload)
        return copy.deepcopy(response)

    async def settle(self):
        return True

    async def execute_prepared(self, *args, **kwargs):
        raise AssertionError(
            "Scientific/diagnostic correction must not execute compute"
        )

    async def close(self):
        return True


def setup(tmp_path, program, responses, *, request=None):
    goal = tmp_path / "goal.md"
    goal.write_text("Synthetic goal: discriminate two fixed hypotheses.")
    schema = result_schema(RESULT_SCHEMA, request)
    study = {
        "input": {
            "goal_file": str(goal),
            "published_knowledge_index_file": str(
                tmp_path / "knowledge/current/index.md"
            ),
            "human_request": request,
        },
        "output_schema": schema,
    }
    task = write(tmp_path / "call.json", study)
    calls = []

    def factory(ctx):
        return Services(ctx, responses, calls, study)

    runtime = GroupRuntime(
        tmp_path,
        [
            GroupDefinition(
                program, "test-v1", "Synthetic test", PROGRAMS / f"{program}.py"
            )
        ],
        service_factory=factory,
    )
    return runtime, task, calls


async def boundary(runtime, run_id):
    await runtime.wait()
    return runtime.observe(run_id)


def artifact(row, name):
    matches = [
        Path(ref) for ref in row["output"]["artifacts"] if Path(ref).name == name
    ]
    assert len(matches) == 1
    return matches[0]


def base(summary="authored assessment", help_text=""):
    return {"summary": summary, "help": help_text, "artifacts": []}


def proposal(*, consequential=True):
    task = {
        "question": "Does intervention A discriminate H1 from H2?",
        "hypothesis": "H1 predicts an effect; H2 predicts none.",
        "controls": ["fixed baseline"],
        "permitted_changes": ["intervention A only"],
        "data_roles": {"selection": "development", "evaluation": "frozen"},
        "evaluation": {"objective": "paired difference", "feedback": "frozen"},
        "inputs": ["synthetic fixture"],
        "constraints": ["CPU only; no remote resources"],
        "completion_criteria": [
            "report discriminating observation or inconclusive result"
        ],
    }
    frontier = {
        "hypotheses": [
            {
                "id": "H1",
                "evidence": ["prior negative result"],
                "cost": "one CPU test",
                "status": "ready",
                "disposition": "test against H2",
            }
        ]
    }
    return {
        **base(),
        "disposition": "experiment",
        "experiment_task_json": json.dumps(task),
        "frontier_json": json.dumps(frontier),
        "consequential_claim": consequential,
    }


def result(request=None):
    value = {
        "summary": "One negative result narrows the frontier.",
        "goal": "not-achieved",
        "evidence": ["actual evaluation"],
        "limitations": ["one synthetic experiment"],
        "next_action": "Test H2 against the fixed baseline.",
        "study_action": "continue",
        "stop_reason": "",
    }
    if request:
        value["human_request"] = {
            "id": request["id"],
            "status": "completed",
            "evidence": ["execution receipt", "independent analysis"],
            "reason": "The requested experiment was executed and independently evaluated.",
        }
    return value


def synthesis(request=None, *, executions=(), analyses=(), help_text=""):
    authored_result = "" if help_text else result(request)
    if request and not help_text:
        authored_result["human_request"]["evidence"] = [*executions, *analyses]
    return {
        **base(help_text=help_text),
        "study_result": authored_result,
        "knowledge_files": []
        if help_text
        else [
            {
                "source": "knowledge/index.md",
                "target": "index.md",
                "content": "# Knowledge\n\n[Prior negative](findings/prior.md)\n[Current](findings/current.md)\n",
            },
            {
                "source": "knowledge/current.md",
                "target": "findings/current.md",
                "content": "# Current evidence\n\nObserved no effect. One synthetic experiment; H2 remains untested.\n",
            },
        ],
        "execution_refs": list(executions),
        "analysis_refs": list(analyses),
        "direction_refs": [],
        "stop_basis": "",
    }


def test_direction_authors_real_task_after_independent_exploration(tmp_path):
    authored = proposal()
    runtime, task, calls = setup(
        tmp_path,
        "direction",
        [
            ("scout", base("An independently sourced alternative")),
            ("proposer", authored),
            ("critic", base("Direction constraints supported")),
        ],
    )

    async def scenario():
        return await boundary(runtime, await runtime.start("direction", task, []))

    row = asyncio.run(scenario())
    assert row["status"] == "returned", row
    assert [call[0] for call in calls] == ["scout", "proposer", "critic"]
    assert calls[0][1] != calls[1][1] != calls[2][1]
    assert json.loads(artifact(row, "experiment-task.json").read_text()) == json.loads(
        authored["experiment_task_json"]
    )
    assert json.loads(artifact(row, "frontier.json").read_text()) == json.loads(
        authored["frontier_json"]
    )
    assert "scientific constraints" in calls[1][2]
    assert "not a duplicate execution Plan" in calls[1][2]
    assert "negative" in calls[0][2]


@pytest.mark.parametrize("human", [True, False])
def test_explicit_experiment_does_not_burn_scout_budget(tmp_path, human):
    request = (
        {"id": "admitted-17", "text": "Run the requested fixed CPU experiment."}
        if human
        else None
    )
    runtime, task, calls = setup(
        tmp_path,
        "direction",
        [("proposer", proposal(consequential=False))],
        request=request,
    )
    if not human:
        task = write(
            tmp_path / "explicit.json",
            {"mode": "explicit_experiment", "question": "Fixed requested question"},
        )

    async def scenario():
        return await boundary(runtime, await runtime.start("direction", task, []))

    row = asyncio.run(scenario())
    assert row["status"] == "returned", row
    assert [call[0] for call in calls] == ["proposer"]
    assert calls[0][3]["explicit_experiment"] is True


def test_direction_feedback_revises_candidate_without_repeating_scout(tmp_path):
    wrong = proposal()
    revised = proposal(consequential=False)
    revised_task = json.loads(revised["experiment_task_json"])
    revised_task["controls"].append("the missing negative control")
    revised["experiment_task_json"] = json.dumps(revised_task)
    runtime, task, calls = setup(
        tmp_path,
        "direction",
        [
            ("scout", base()),
            ("proposer", wrong),
            ("critic", base(help_text="Direction needs the missing negative control.")),
            ("proposer", revised),
            ("critic", base("The supplied control resolves the concern.")),
        ],
    )
    feedback = write(tmp_path / "control.json", {"control": "negative evidence"})

    async def scenario():
        run_id = await runtime.start("direction", task, [])
        first = await boundary(runtime, run_id)
        assert first["output"]["completion"] == "blocked"
        runtime.hold(run_id)
        await runtime.resume(run_id, [], "No new evidence")
        second = await boundary(runtime, run_id)
        assert second["output"]["completion"] == "blocked"
        assert len(calls) == 3
        runtime.hold(run_id)
        await runtime.resume(run_id, [feedback])
        final = await boundary(runtime, run_id)
        ctx_work = tmp_path / "group-runs" / run_id / "work"
        old = ctx_work / "reports/candidates/000001/experiment-task.json"
        assert json.loads(old.read_text())["controls"] == ["fixed baseline"]
        assert (ctx_work / "reports/history/000001-critique.json").is_file()
        return final

    row = asyncio.run(scenario())
    assert row["output"]["completion"] == "completed"
    assert [call[0] for call in calls] == [
        "scout",
        "proposer",
        "critic",
        "proposer",
        "critic",
    ]
    assert calls[1][1] != calls[3][1]
    assert calls[2][1] != calls[4][1]
    assert calls[-1][3]["feedback_refs"] == [feedback]
    assert calls[3][3]["revision"]["propose"].endswith("000001-propose.json")
    assert (
        json.loads(artifact(row, "experiment-task.json").read_text())["controls"]
        == revised_task["controls"]
    )


def test_synthesis_delivers_dynamic_human_result_and_every_manifest_source(tmp_path):
    request = {"id": "admitted-23", "text": "Run one fixed experiment."}
    execution = write(
        tmp_path / "exported-execution.json", {"exit_code": 0, "running": False}
    )
    analysis = write(
        tmp_path / "exported-evaluate.json",
        {"summary": "Actual independent evaluation", "help": ""},
    )
    authored = synthesis(request, executions=[execution], analyses=[analysis])
    runtime, task, calls = setup(
        tmp_path,
        "synthesis",
        [("analyst", authored), ("critic", base())],
        request=request,
    )

    async def scenario():
        return await boundary(
            runtime, await runtime.start("synthesis", task, [execution, analysis])
        )

    row = asyncio.run(scenario())
    assert row["status"] == "returned", row
    assert (
        json.loads(artifact(row, "study-result.json").read_text())
        == authored["study_result"]
    )
    schema = calls[0][4]["properties"]["study_result"]["anyOf"][0]
    assert schema["properties"]["human_request"]["properties"]["id"]["enum"] == [
        request["id"]
    ]
    manifest = json.loads(artifact(row, "knowledge-manifest.json").read_text())
    assert len(manifest["files"]) == 2
    for entry in manifest["files"]:
        exported = artifact(row, Path(entry["source"]).name)
        source = next(
            d for d in authored["knowledge_files"] if d["source"] == entry["source"]
        )
        assert exported.read_text() == source["content"]
    assert [call[0] for call in calls] == ["analyst", "critic"]
    assert "No-useful-action stop requires" in calls[-1][2]


@pytest.mark.parametrize(
    "receipt",
    [
        {"exit_code": 1},
        {"exit_code": 0, "running": True},
        {"prepared": True},
        {"exit_code": 0, "unknown": True},
    ],
)
def test_preparation_or_failed_execution_cannot_complete_human_request(
    tmp_path, receipt
):
    request = {"id": "admitted-31", "text": "Execute the experiment."}
    execution = write(tmp_path / "execution.json", receipt)
    analysis = write(tmp_path / "analysis.json", {"summary": "Interpretation"})
    runtime, task, _ = setup(
        tmp_path,
        "synthesis",
        [("analyst", synthesis(request, executions=[execution], analyses=[analysis]))],
        request=request,
    )

    async def scenario():
        return await boundary(
            runtime, await runtime.start("synthesis", task, [execution, analysis])
        )

    row = asyncio.run(scenario())
    assert row["status"] == "failed"
    assert "cannot complete a request" in row["error"]


def test_human_result_must_keep_admitted_identity(tmp_path):
    request = {"id": "admitted-identity", "text": "Do the experiment."}
    authored = synthesis({"id": "invented-identity"})
    runtime, task, _ = setup(
        tmp_path, "synthesis", [("analyst", authored)], request=request
    )

    async def scenario():
        return await boundary(runtime, await runtime.start("synthesis", task, []))

    row = asyncio.run(scenario())
    assert row["status"] == "failed"
    assert "outside the admitted schema" in row["error"]


def test_blocked_independent_evaluation_cannot_complete_human_request(tmp_path):
    request = {"id": "admitted-evaluation", "text": "Execute and evaluate."}
    execution = write(tmp_path / "execution.json", {"exit_code": 0})
    evaluation = write(
        tmp_path / "evaluate.json",
        {"summary": "Missing frozen evaluator", "help": "Need frozen evaluator"},
    )
    runtime, task, _ = setup(
        tmp_path,
        "synthesis",
        [
            (
                "analyst",
                synthesis(request, executions=[execution], analyses=[evaluation]),
            )
        ],
        request=request,
    )

    async def scenario():
        return await boundary(
            runtime, await runtime.start("synthesis", task, [execution, evaluation])
        )

    row = asyncio.run(scenario())
    assert row["status"] == "failed"
    assert "evaluation cannot complete a request" in row["error"]


@pytest.mark.parametrize("with_direction", [False, True])
def test_no_useful_action_stop_requires_direction_frontier_evidence(
    tmp_path, with_direction
):
    authored = synthesis()
    authored["study_result"].update(
        study_action="stop",
        stop_reason="The reviewed frontier has no useful justified action.",
    )
    authored["stop_basis"] = "no-useful-action"
    direction = write(
        tmp_path / "direction-disposition.json",
        {
            "disposition": "no-useful-action",
            "reason": "Every authorized discriminating test is exhausted.",
        },
    )
    if with_direction:
        authored["direction_refs"] = [direction]
    responses = [("analyst", authored)]
    if with_direction:
        responses.append(
            ("critic", base("The Direction evidence supports Study stop."))
        )
    runtime, task, _ = setup(tmp_path, "synthesis", responses)

    async def scenario():
        return await boundary(
            runtime,
            await runtime.start(
                "synthesis", task, [direction] if with_direction else []
            ),
        )

    row = asyncio.run(scenario())
    if with_direction:
        assert row["status"] == "returned", row
        completed = json.loads(artifact(row, "study-result.json").read_text())
        assert completed["goal"] == "not-achieved"
        assert completed["study_action"] == "stop"
    else:
        assert row["status"] == "failed"
        assert "requires admitted Direction evidence" in row["error"]


def test_synthesis_feedback_corrects_real_result_and_knowledge_without_compute(
    tmp_path,
):
    wrong = synthesis()
    wrong["study_result"]["summary"] = "Unsupported positive effect."
    wrong["knowledge_files"][1]["content"] = "Unsupported positive effect.\n"
    revised = synthesis()
    runtime, task, calls = setup(
        tmp_path,
        "synthesis",
        [
            ("analyst", wrong),
            (
                "critic",
                base(
                    help_text="Claim conflicts with the negative observation; revise it."
                ),
            ),
            ("analyst", revised),
            ("critic", base("Direction evidence supports the continuing action.")),
        ],
    )
    feedback = write(tmp_path / "direction-evidence.json", {"direction": "test H2"})

    async def scenario():
        run_id = await runtime.start("synthesis", task, [])
        first = await boundary(runtime, run_id)
        assert first["output"]["completion"] == "blocked"
        old_export = artifact(first, "current.md")
        assert not any(
            Path(ref).name == "study-result.json"
            for ref in first["output"]["artifacts"]
        )
        runtime.hold(run_id)
        await runtime.resume(run_id, [feedback])
        final = await boundary(runtime, run_id)
        assert old_export.read_text() == "Unsupported positive effect.\n"
        history = tmp_path / "group-runs" / run_id / "work/reports/candidates/000001"
        assert (
            json.loads((history / "study-result-candidate.json").read_text())["summary"]
            == "Unsupported positive effect."
        )
        assert (
            history / "knowledge/current.md"
        ).read_text() == "Unsupported positive effect.\n"
        return final

    row = asyncio.run(scenario())
    assert row["output"]["completion"] == "completed"
    assert [call[0] for call in calls] == ["analyst", "critic", "analyst", "critic"]
    assert calls[0][1] != calls[2][1]
    assert calls[1][1] != calls[3][1]
    assert (
        json.loads(artifact(row, "study-result.json").read_text())["summary"]
        == revised["study_result"]["summary"]
    )
    assert (
        artifact(row, "current.md").read_text()
        == revised["knowledge_files"][1]["content"]
    )


@pytest.mark.parametrize(
    "bad_source,bad_target",
    [("../escape.md", "index.md"), ("knowledge/index.md", "../escape.md")],
)
def test_knowledge_delivery_cannot_write_outside_run_or_publication(
    tmp_path, bad_source, bad_target
):
    authored = synthesis()
    authored["knowledge_files"][0].update(source=bad_source, target=bad_target)
    runtime, task, _ = setup(tmp_path, "synthesis", [("analyst", authored)])

    async def scenario():
        return await boundary(runtime, await runtime.start("synthesis", task, []))

    row = asyncio.run(scenario())
    assert row["status"] == "failed"
    assert "relative file paths" in row["error"]
    assert not (tmp_path / "escape.md").exists()


def test_diagnosis_feedback_corrects_diagnosis_then_obtains_fresh_review(
    tmp_path,
):
    def diagnose(ctx, payload):
        patch = ctx.work / "repair.patch"
        patch.write_text("Synthetic minimal patch; apply in a separate repair Run.\n")
        return {
            **base(),
            "artifacts": [str(patch)],
            "diagnosis": "Observed missing fixture. Restore the authorized fixture in a new Run.\n",
            "review_required": True,
        }

    def revise(ctx, payload):
        assert payload["revision"]["critique"].endswith("000001-critique.json")
        patch = ctx.work / "repair.patch"
        patch.write_text("Revised patch with the authorized fixture identity.\n")
        return {
            **base(),
            "artifacts": [str(patch)],
            "diagnosis": "Corrected fixture identity: synthetic-v1. Preserve the original Run.\n",
            "review_required": False,
        }

    runtime, task, calls = setup(
        tmp_path,
        "diagnosis",
        [
            ("experimenter", diagnose),
            (
                "critic",
                {
                    **base(help_text="Need fixture identity."),
                    "diagnosis": "Missing identity",
                    "review_required": False,
                },
            ),
            ("experimenter", revise),
            (
                "critic",
                {
                    **base(),
                    "diagnosis": "Repair scope supported",
                    "review_required": False,
                },
            ),
        ],
    )
    feedback = write(tmp_path / "fixture-identity.json", {"identity": "synthetic-v1"})

    async def scenario():
        run_id = await runtime.start("diagnosis", task, [])
        first = await boundary(runtime, run_id)
        assert first["output"]["completion"] == "blocked"
        previous = artifact(first, "repair.patch")
        runtime.hold(run_id)
        await runtime.resume(run_id, [feedback])
        final = await boundary(runtime, run_id)
        assert previous.read_text().startswith("Synthetic minimal patch")
        history = (
            tmp_path
            / "group-runs"
            / run_id
            / "work/reports/candidates/000001-diagnosis.md"
        )
        assert history.read_text().startswith("Observed missing fixture.")
        return final

    row = asyncio.run(scenario())
    assert row["status"] == "returned", row
    assert row["output"]["completion"] == "completed"
    assert (
        artifact(row, "diagnosis.md")
        .read_text()
        .startswith("Corrected fixture identity:")
    )
    assert artifact(row, "repair.patch").is_file()
    assert artifact(row, "repair.patch").read_text().startswith("Revised patch")
    assert [call[0] for call in calls] == [
        "experimenter",
        "critic",
        "experimenter",
        "critic",
    ]
    assert "Never edit the original Run" in calls[0][2]
