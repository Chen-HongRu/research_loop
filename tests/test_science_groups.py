"""Scientific Group contracts exercised through real pinned Runs and exports."""

import asyncio
import copy
import json
from pathlib import Path

import pytest

from research_loop.group_programs.synthesis import (
    bind_recovery_analysis,
    recovery_author_schema,
)
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

    async def call(self, role, context, instructions, payload, schema, **kwargs):
        packet = self.study_context.get("input", {}).get("shared_input", {})
        if isinstance(packet, dict) and packet.get("kind") == "exp-recovery-v1":
            assert kwargs == {"allow_tools": False, "include_role_template": False}
        else:
            assert kwargs == {}
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


def setup(tmp_path, program, responses, *, request=None, shared_input=None):
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
    if shared_input is not None:
        study["input"]["shared_input"] = shared_input
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


def synthesis_review(
    verdict="accept", reason="The cumulative evidence supports this disposition."
):
    return {
        "summary": "Independent synthesis review",
        "verdict": verdict,
        "reason": reason,
        "artifacts": [],
    }


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
        [("analyst", authored), ("critic", synthesis_review())],
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


def test_synthesis_accept_explanation_does_not_request_assistance(tmp_path):
    reason = "No revision or further computation is needed. Evidence reviewed: the supplied completed experiment."
    runtime, task, calls = setup(
        tmp_path,
        "synthesis",
        [
            ("analyst", synthesis()),
            ("critic", synthesis_review("accept", reason)),
        ],
    )

    async def scenario():
        return await boundary(runtime, await runtime.start("synthesis", task, []))

    row = asyncio.run(scenario())
    assert row["status"] == "returned", row
    assert row["output"]["completion"] == "completed"
    assert row["output"]["help"] == ""
    assert row["output"]["unfinished"] == []
    assert artifact(row, "study-result.json").is_file()
    review = json.loads(artifact(row, "critique.json").read_text())
    assert review["verdict"] == "accept"
    assert review["reason"] == reason
    assert "help" not in review
    assert [call[0] for call in calls] == ["analyst", "critic"]


def test_synthesis_keeps_admitted_citations_without_exporting_them_again(tmp_path):
    evidence = write(tmp_path / "admitted-evidence.json", {"observed": "synthetic"})
    feedback = write(tmp_path / "admitted-feedback.json", {"correction": "synthetic"})

    def analyze(ctx, payload):
        report = ctx.work / "new-analysis.md"
        report.write_text("New cumulative assessment.\n")
        return {
            **synthesis(),
            "artifacts": [str(report), payload["task_ref"], evidence, feedback],
        }

    def critique(ctx, payload):
        return {
            **synthesis_review(),
            "artifacts": [
                str(ctx.work / "reports/study-result-candidate.json"),
                evidence,
                feedback,
            ],
        }

    runtime, task, _ = setup(
        tmp_path,
        "synthesis",
        [
            ("analyst", synthesis(help_text="Need the bounded feedback artifact.")),
            ("analyst", analyze),
            ("critic", critique),
        ],
    )

    async def scenario():
        run_id = await runtime.start("synthesis", task, [evidence])
        first = await boundary(runtime, run_id)
        assert first["output"]["completion"] == "blocked"
        runtime.hold(run_id)
        await runtime.resume(run_id, [feedback])
        return await boundary(runtime, run_id)

    row = asyncio.run(scenario())
    assert row["status"] == "returned", row
    assert row["output"]["completion"] == "completed"
    assert (
        artifact(row, "new-analysis.md").read_text() == "New cumulative assessment.\n"
    )
    exported_names = {Path(ref).name for ref in row["output"]["artifacts"]}
    assert not exported_names.intersection(
        {Path(task).name, Path(evidence).name, Path(feedback).name}
    )
    analyst_report = json.loads(artifact(row, "analysis.json").read_text())
    critique_report = json.loads(artifact(row, "critique.json").read_text())
    assert analyst_report["artifacts"][1:] == [task, evidence, feedback]
    assert critique_report["artifacts"][1:] == [evidence, feedback]


def test_synthesis_rejects_unknown_external_artifact_with_field_location(tmp_path):
    unknown = tmp_path / "unadmitted-evidence.md"
    unknown.write_text("Unadmitted content must never appear in diagnostic output.\n")

    def critique(ctx, payload):
        return {
            **synthesis_review(),
            "artifacts": [
                str(ctx.work / "reports/study-result-candidate.json"),
                str(unknown),
            ],
        }

    runtime, task, _ = setup(
        tmp_path, "synthesis", [("analyst", synthesis()), ("critic", critique)]
    )

    async def scenario():
        return await boundary(runtime, await runtime.start("synthesis", task, []))

    row = asyncio.run(scenario())
    assert row["status"] == "failed"
    assert "critique.artifacts[1]" in row["error"]
    assert "neither ownwork nor an explicitly admitted input" in row["error"]
    assert str(unknown) in row["error"]
    assert unknown.read_text().strip() not in row["error"]


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
            (
                "critic",
                synthesis_review(reason="The Direction evidence supports Study stop."),
            )
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
                synthesis_review(
                    "revise",
                    "Claim conflicts with the negative observation; revise it.",
                ),
            ),
            ("analyst", revised),
            (
                "critic",
                synthesis_review(
                    reason="Direction evidence supports the continuing action."
                ),
            ),
        ],
    )
    feedback = write(tmp_path / "direction-evidence.json", {"direction": "test H2"})

    async def scenario():
        run_id = await runtime.start("synthesis", task, [])
        first = await boundary(runtime, run_id)
        assert first["output"]["completion"] == "blocked"
        assert (
            first["output"]["help"]
            == "Claim conflicts with the negative observation; revise it."
        )
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


def recovery_candidate(tmp_path):
    request = {"id": "recovery-request", "text": "Publish the settled experiment."}
    execution = write(tmp_path / "execution.json", {"exit_code": 0})
    evaluation = write(
        tmp_path / "evaluation.json",
        {"summary": "The formal criterion was satisfied.", "help": ""},
    )
    original = write(
        tmp_path / "original-evaluation.json",
        {"summary": "Original independent assessment.", "help": "publication boundary"},
    )
    packet = {
        "kind": "exp-recovery-v1",
        "execution_ref": execution,
        "evaluation_ref": evaluation,
        "original_evaluation_ref": original,
        "evidence_files": [
            {"kind": kind, "ref": ref, "content": json.loads(Path(ref).read_text())}
            for kind, ref in (
                ("execution", execution),
                ("evaluation", evaluation),
                ("original_evaluation", original),
            )
        ],
        "limitations": ["One synthetic experiment; no general causal conclusion."],
        "publication_paths": {original: "recovery-evidence/original-evaluation.json"},
    }
    authored = synthesis(request, executions=[execution], analyses=[evaluation])
    refs = [execution, evaluation, original]
    authored["study_result"].update(
        summary="The settled formal criterion is satisfied.",
        goal="achieved",
        study_action="stop",
        stop_reason="The scoped goal's formal criterion is satisfied.",
        next_action="",
        evidence=list(refs),
    )
    authored["study_result"]["human_request"]["evidence"] = list(refs)
    authored["stop_basis"] = "goal-satisfied"
    authored["knowledge_files"][1]["content"] = (
        "Formal criterion satisfied under the original constraints. "
        f"Original independent assessment: {original}. "
        "One synthetic experiment; no general causal conclusion.\n"
    )
    authored = bind_recovery_analysis(authored, recovery_sources(request, packet, refs))
    return request, packet, authored, refs


def recovery_sources(request, packet, refs):
    return {
        "study_context": {
            "input": {"human_request": request, "shared_input": packet},
            "output_schema": result_schema(RESULT_SCHEMA, request),
        },
        "input_refs": refs,
        "recovery_evidence": packet,
    }


def compact_recovery(authored):
    compact = copy.deepcopy(authored)
    for key in ("artifacts", "execution_refs", "analysis_refs", "direction_refs"):
        del compact[key]
    del compact["study_result"]["evidence"]
    if "human_request" in compact["study_result"]:
        del compact["study_result"]["human_request"]["evidence"]
    for document in compact["knowledge_files"]:
        document["content"] = document["content"].split(
            "\n\n<!-- recovery-provenance -->"
        )[0]
    return compact


def test_recovery_synthesis_has_inline_tool_free_author_and_candidate_review(tmp_path):
    request, packet, authored, refs = recovery_candidate(tmp_path)

    def review(ctx, payload):
        assert payload["analysis_record"] == authored
        assert "study_result" not in payload
        assert "knowledge_files" not in payload
        return synthesis_review(reason="Only the publication delta was reviewed.")

    runtime, task, calls = setup(
        tmp_path,
        "synthesis",
        [("analyst", compact_recovery(authored)), ("critic", review)],
        request=request,
        shared_input=packet,
    )

    async def scenario():
        return await boundary(runtime, await runtime.start("synthesis", task, refs))

    row = asyncio.run(scenario())
    assert row["status"] == "returned", row
    assert row["output"]["completion"] == "completed"
    assert len(calls) == 2
    assert [call[0] for call in calls] == ["analyst", "critic"]
    for call in calls:
        assert call[3]["recovery_evidence"] == packet
        assert call[3]["study_context"]["input"]["human_request"] == request
        assert call[3]["study_context"]["output_schema"]["properties"]["human_request"]
        assert "Do not execute compute" in call[2]
        assert "review the whole Exp again" in call[2]
    completed = json.loads(artifact(row, "study-result.json").read_text())
    assert completed == authored["study_result"]
    assert (
        artifact(row, "current.md").read_text()
        == authored["knowledge_files"][1]["content"]
    )
    manifest = json.loads(artifact(row, "knowledge-manifest.json").read_text())
    assert manifest["files"] == [
        {"source": document["source"], "target": document["target"]}
        for document in authored["knowledge_files"]
    ]


@pytest.mark.parametrize(
    "missing",
    ["study_evidence", "human_evidence", "knowledge", "goal", "status", "admission"],
)
def test_recovery_completion_requires_original_identity_and_final_disposition(
    tmp_path, missing
):
    request, packet, authored, refs = recovery_candidate(tmp_path)
    original = packet["original_evaluation_ref"]
    if missing == "study_evidence":
        authored["study_result"]["evidence"].remove(original)
    elif missing == "human_evidence":
        authored["study_result"]["human_request"]["evidence"] = refs[:-1]
    elif missing == "knowledge":
        authored["knowledge_files"][1]["content"] = ""
    elif missing == "goal":
        authored["study_result"]["goal"] = "not-achieved"
    elif missing == "status":
        authored["study_result"]["human_request"]["status"] = "pending"
    else:
        refs = refs[:-1]
    runtime, task, calls = setup(
        tmp_path,
        "synthesis",
        [("analyst", authored)],
        request=request,
        shared_input=packet,
    )

    async def scenario():
        return await boundary(runtime, await runtime.start("synthesis", task, refs))

    row = asyncio.run(scenario())
    assert row["status"] == "failed", row
    assert "Recovery" in row["error"]
    assert len(calls) == 1


@pytest.mark.parametrize(
    "invalid",
    [
        "extra_file",
        "long_content",
        "long_summary",
        "long_result_summary",
        "archive_root",
        "archive_child",
    ],
)
def test_recovery_rejects_oversized_output_and_reserved_archive_targets(
    tmp_path, invalid
):
    request, packet, authored, refs = recovery_candidate(tmp_path)
    if invalid == "extra_file":
        authored["knowledge_files"].append(
            {
                "source": "knowledge/extra.md",
                "target": "extra.md",
                "content": "Extra output",
            }
        )
    elif invalid == "long_content":
        authored["knowledge_files"][0]["content"] = "x" * 2001
    elif invalid == "long_summary":
        authored["summary"] = "x" * 401
    elif invalid == "long_result_summary":
        authored["study_result"]["summary"] = "x" * 401
    else:
        authored["knowledge_files"][1]["target"] = (
            "recovery-evidence"
            if invalid == "archive_root"
            else "recovery-evidence/original-evaluation.json"
        )
    runtime, task, calls = setup(
        tmp_path,
        "synthesis",
        [("analyst", authored)],
        request=request,
        shared_input=packet,
    )

    async def scenario():
        return await boundary(runtime, await runtime.start("synthesis", task, refs))

    row = asyncio.run(scenario())
    assert row["status"] == "failed", row
    assert "Recovery" in row["error"]
    assert len(calls) == 1


def test_recovery_author_schema_bounds_do_not_change_normal_schema():
    from research_loop.group_programs.synthesis import analysis_schema

    schema = result_schema(RESULT_SCHEMA, None)
    normal = analysis_schema(schema)
    recovery = analysis_schema(schema, recovery=True)
    assert "maxLength" not in normal["properties"]["summary"]
    assert "maxLength" not in schema["properties"]["summary"]
    assert "maxItems" not in normal["properties"]["knowledge_files"]
    assert recovery["properties"]["summary"]["maxLength"] == 400
    assert (
        recovery["properties"]["study_result"]["anyOf"][0]["properties"]["summary"][
            "maxLength"
        ]
        == 400
    )
    assert recovery["properties"]["knowledge_files"]["maxItems"] == 2
    assert (
        recovery["properties"]["knowledge_files"]["items"]["properties"]["content"][
            "maxLength"
        ]
        == 2000
    )


def test_recovery_synthesis_without_admitted_human_request(tmp_path):
    _, packet, authored, refs = recovery_candidate(tmp_path)
    del authored["study_result"]["human_request"]
    runtime, task, calls = setup(
        tmp_path,
        "synthesis",
        [("analyst", compact_recovery(authored)), ("critic", synthesis_review())],
        shared_input=packet,
    )

    async def scenario():
        return await boundary(runtime, await runtime.start("synthesis", task, refs))

    row = asyncio.run(scenario())
    assert row["status"] == "returned", row
    assert row["output"]["completion"] == "completed"
    result = json.loads(artifact(row, "study-result.json").read_text())
    assert result == authored["study_result"]
    assert "human_request" not in result
    assert result["goal"] == "achieved"
    assert result["study_action"] == "stop"
    assert set(refs).issubset(result["evidence"])
    assert len(calls) == 2
    assert (
        "human_request"
        not in calls[0][4]["properties"]["study_result"]["anyOf"][0]["properties"]
    )
    assert calls[-1][3]["analysis_record"] == authored


def test_recovery_compact_schema_omits_all_mechanical_citations():
    request = {"id": "compact-request", "text": "Publish reviewed evidence."}
    schema = recovery_author_schema(result_schema(RESULT_SCHEMA, request))
    properties = schema["properties"]
    assert not {
        "artifacts",
        "execution_refs",
        "analysis_refs",
        "direction_refs",
    }.intersection(properties)
    result = properties["study_result"]["anyOf"][0]["properties"]
    assert "evidence" not in result
    assert "evidence" not in result["human_request"]["properties"]
    assert (
        properties["knowledge_files"]["items"]["properties"]["content"]["maxLength"]
        == 1200
    )


def test_recovery_binder_preserves_science_and_is_idempotent(tmp_path):
    request, packet, expected, refs = recovery_candidate(tmp_path)
    candidate = compact_recovery(expected)
    original = copy.deepcopy(candidate)
    sources = recovery_sources(request, packet, refs)
    bound = bind_recovery_analysis(candidate, sources)
    assert candidate == original
    assert bound == expected
    assert bind_recovery_analysis(bound, sources) == bound
    assert bound["study_result"]["evidence"] == refs
    assert bound["study_result"]["human_request"]["evidence"] == refs
    for key, value in original["study_result"].items():
        if key != "human_request":
            assert bound["study_result"][key] == value
    for key, value in original["study_result"]["human_request"].items():
        assert bound["study_result"]["human_request"][key] == value
    content = bound["knowledge_files"][1]["content"]
    assert content.startswith(original["knowledge_files"][1]["content"])
    assert "Host-added provenance" in content
    assert "(../recovery-evidence/original-evaluation.json)" in content
    assert len(content) <= 2000


@pytest.mark.parametrize(
    "field",
    ["summary", "help", "stop_basis", "knowledge_files", "study_action", "limitations"],
)
def test_recovery_binder_rejects_missing_scientific_fields(tmp_path, field):
    request, packet, authored, refs = recovery_candidate(tmp_path)
    candidate = compact_recovery(authored)
    fields = (
        candidate["study_result"]
        if field in {"study_action", "limitations"}
        else candidate
    )
    del fields[field]
    with pytest.raises(ValueError, match="fields do not match"):
        bind_recovery_analysis(candidate, recovery_sources(request, packet, refs))


@pytest.mark.parametrize(
    "field",
    [
        "artifacts",
        "execution_refs",
        "analysis_refs",
        "direction_refs",
        "study_evidence",
        "human_evidence",
        "footer",
    ],
)
def test_recovery_binder_rejects_contradictory_mechanical_fields(tmp_path, field):
    request, packet, authored, refs = recovery_candidate(tmp_path)
    if field == "study_evidence":
        authored["study_result"]["evidence"] = ["contradictory-receipt"]
    elif field == "human_evidence":
        authored["study_result"]["human_request"]["evidence"] = [
            "contradictory-receipt"
        ]
    elif field == "footer":
        authored["knowledge_files"][1]["content"] += "Contradictory footer suffix"
    else:
        authored[field] = ["contradictory-receipt"]
    with pytest.raises(ValueError, match="contradictory"):
        bind_recovery_analysis(authored, recovery_sources(request, packet, refs))


def test_recovery_binder_preserves_authored_disposition_and_help(tmp_path):
    request, packet, authored, refs = recovery_candidate(tmp_path)
    candidate = compact_recovery(authored)
    candidate.update(
        summary="Scientific correction needed", help="Review uncertainty", stop_basis=""
    )
    candidate["study_result"].update(
        goal="not-achieved",
        study_action="continue",
        stop_reason="",
        next_action="Resolve the scientific uncertainty without new compute.",
        limitations=["Unresolved scientific ambiguity."],
    )
    candidate["study_result"]["human_request"].update(
        status="pending", reason="The admitted request remains unresolved."
    )
    bound = bind_recovery_analysis(candidate, recovery_sources(request, packet, refs))
    for key in ("summary", "help", "stop_basis"):
        assert bound[key] == candidate[key]
    for key in ("goal", "study_action", "stop_reason", "next_action", "limitations"):
        assert bound["study_result"][key] == candidate["study_result"][key]
    assert bound["study_result"]["human_request"]["status"] == "pending"
    assert (
        bound["study_result"]["human_request"]["reason"]
        == candidate["study_result"]["human_request"]["reason"]
    )


def test_recovery_scientific_critic_can_request_revision(tmp_path):
    request, packet, authored, refs = recovery_candidate(tmp_path)
    runtime, task, calls = setup(
        tmp_path,
        "synthesis",
        [
            ("analyst", compact_recovery(authored)),
            (
                "critic",
                synthesis_review(
                    "revise",
                    "Preserve the original uncertainty in the scientific prose.",
                ),
            ),
        ],
        request=request,
        shared_input=packet,
    )

    async def scenario():
        return await boundary(runtime, await runtime.start("synthesis", task, refs))

    row = asyncio.run(scenario())
    assert row["status"] == "returned", row
    assert row["output"]["completion"] == "blocked"
    assert "original uncertainty" in row["output"]["help"]
    assert len(calls) == 2
    assert not any(
        Path(ref).name == "study-result.json" for ref in row["output"]["artifacts"]
    )
