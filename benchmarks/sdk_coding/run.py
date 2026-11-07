"""Exercise the full native Agents SDK topology on a synthetic coding fixture."""

import argparse
import asyncio
import json
import os
import tempfile
from collections import Counter
from pathlib import Path

from agents import Agent, ModelSettings, OpenAIChatCompletionsModel, function_tool
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from execution import run_fixture_tests
from openai import AsyncOpenAI

from research_loop.task_pool import CreateTask, TaskPool, TaskRequest
from research_loop.tracing import configure_local_tracing
from research_loop.workflow import (
    Evidence,
    Investigation,
    JudgeDecision,
    Plan,
    Proposal,
    ResearchAgents,
    Review,
    ScoutReport,
    run_research,
)

BROKEN = "def normalize(values):\n    return [x / sum(values) for x in values]\n"
FIXED = (
    "def normalize(values):\n"
    "    clipped = [max(0.0, x) for x in values]\n"
    "    scale = max(clipped, default=0.0)\n"
    "    return [x / scale if scale else 0.0 for x in clipped]\n"
)
TESTS = """import unittest
from solution import normalize
class NormalizeTests(unittest.TestCase):
    def test_contract(self):
        for source, expected in [([], []), ([0, 0], [0, 0]),
                ([-2, -1], [0, 0]), ([-2, 2, 4], [0, .5, 1]),
                ([3], [1]), ([2, 2], [1, 1])]:
            self.assertEqual(normalize(source), expected)
"""
REFERENCE_ID = "synthetic:normalization-contract-v1"
REFERENCE = (
    "SYNTHETIC local reference: clip each negative input to zero, then divide "
    "by the largest clipped value. Empty inputs stay empty; zero maxima yield "
    "zeros. This is a fixture contract, not external research or holdout evidence."
)


def payload(call):
    if isinstance(call.input, str):
        return json.loads(call.input)
    return json.loads(call.input[0]["content"])


def tool_outputs(call):
    if isinstance(call.input, str):
        return []
    return [
        item["output"]
        for item in call.input
        if item.get("type") == "function_call_output"
    ]


def scripted_response(role, call):
    """Respond to actual SDK inputs and tool feedback, never fabricated test results."""
    outputs = tool_outputs(call)
    if role == "Execute":
        if not outputs:
            name, args = "read_fixture", {}
        elif len(outputs) == 1:
            assert "def normalize" in outputs[-1]
            name, args = "run_tests", {}
        elif len(outputs) == 2:
            result = json.loads(outputs[-1])
            if result["exit_code"] == 0:
                return [assistant_message("Existing candidate passes fixed tests.")]
            name, args = "replace_code", {"source": FIXED}
        elif len(outputs) == 3:
            assert outputs[-1] == "solution.py updated"
            name, args = "run_tests", {}
        else:
            assert json.loads(outputs[-1])["exit_code"] == 0
            return [
                assistant_message("Updated candidate passes fixed development tests.")
            ]
        return [function_call(name, args, call_id=f"fixture-{len(outputs)}")]
    if role == "Scout":
        if not outputs:
            return [function_call("lookup_reference", {}, call_id="reference")]
        material = json.loads(outputs[-1])
        output = ScoutReport(
            summary=material["text"],
            sources=(material["id"],),
            limitations=(
                "Synthetic local material; no web search or independent research.",
            ),
        )
    else:
        data = payload(call)
        if role.startswith("Advocate"):
            humans = sorted(
                (r for r in data["reviewed_records"] if r["source"] == "human"),
                key=lambda r: r["task"]["id"],
            )
            task_id = "auto-a" if role.endswith("A") else "auto-b"
            tasks = ()
            if not any(t["request"]["id"] == task_id for t in data["pool"]):
                tasks = (
                    TaskRequest(
                        id=task_id,
                        question="Independently recheck the reviewed normalization candidate.",
                        priority=10 if role.endswith("A") else 20,
                        dependencies=(
                            humans[0]["task"]["id"] if humans else "human-a",
                        ),
                    ),
                )
            output = Proposal(
                rationale="Propose a recheck blocked on a human dependency until its Review exists.",
                tasks=tasks,
            )
        elif role == "Judge":
            existing = {t["request"]["id"] for t in data["pool"]}
            proposals = [
                TaskRequest.model_validate(task)
                for outcome in data["advocates"]
                for task in outcome.get("proposal", {}).get("tasks", [])
                if task["id"] not in existing and set(task["dependencies"]) <= existing
            ]
            # One incremental admission selected from the actual advisory outputs.
            chosen = sorted(proposals, key=lambda task: (task.priority, task.id))[:1]
            output = JudgeDecision(
                changes=tuple(
                    CreateTask(action="create", task=task) for task in chosen
                ),
                stop=False,
                reason="Admit one dependency-blocked proposal; retain protected human tasks.",
            )
        elif role == "Plan":
            output = Plan(
                question=data["task"]["request"]["question"],
                change="Read candidate, run fixed tests, repair normalization if needed, recheck.",
                rationale="Use fixed development evidence; preserve the synthetic contract.",
                based_on=tuple(r["id"] for r in data["reviewed_records"]),
            )
        elif role == "Review":
            evidence = data["trusted_evidence"]
            assert evidence["execution_complete"]
            observations = evidence["observations"]
            assert evidence["valid"] == (observations["exit_code"] == 0)
            output = Review(
                interpretation=(
                    "Host independently reran fixed development tests: "
                    f"exit_code={observations['exit_code']}."
                ),
                limitations=(
                    "Synthetic development fixture; no generalization or goal claim.",
                ),
                knowledge_changed=evidence["valid"],
                goal="not-achieved",
                continue_research=True,
                next_question="Recheck the candidate in an independent task workspace.",
            )
        elif role == "Manager":
            lines = [data["previous_notes"] or "# Research notes"]
            for item in data["evidence"]:
                if item["kind"] == "review":
                    record = item["record"]
                    review = record["review"]
                    lines.append(
                        f"- {record['id']} ({record['task']['id']}): "
                        f"{review['interpretation']} Limits: {'; '.join(review['limitations'])}"
                    )
                else:
                    report = item["report"]
                    lines.append(
                        f"- Scout sources: {', '.join(report['sources'])}. "
                        f"{report['summary']} Limits: {'; '.join(report['limitations'])}"
                    )
            return [assistant_message("\n\n".join(lines))]
        else:
            raise ValueError(f"Unknown scripted role: {role}")
    return [assistant_message(output.model_dump_json())]


async def run(args):
    root = args.run_dir.resolve()
    # Keep private traces and candidate code outside any Git worktree.
    if any((parent / ".git").exists() for parent in (root, *root.parents)):
        raise ValueError("run_dir must be outside Git")
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    processor = configure_local_tracing(root / "trace.jsonl")
    client = None
    calls = Counter()
    try:
        external_model = None
        if args.model != "scripted":
            if not os.environ.get("OPENAI_API_KEY"):
                raise ValueError("External mode requires OPENAI_API_KEY")
            client = AsyncOpenAI(max_retries=0, timeout=30)
            external_model = OpenAIChatCompletionsModel(
                model=args.model, openai_client=client
            )
        settings = ModelSettings(max_tokens=2048, parallel_tool_calls=False)

        def agent(role, instructions, *, output_type=None, tools=()):
            model = external_model
            if args.model == "scripted":

                def respond(call):
                    calls[role] += 1
                    return scripted_response(role, call)

                # Background publication/planning counts depend on event ordering.
                model = ScriptedModel(
                    [ModelStep.respond(respond)] * 128, emit_traces=True
                )
            return Agent(
                name=role,
                instructions=instructions,
                model=model,
                model_settings=settings,
                output_type=output_type,
                tools=list(tools),
            )

        @function_tool(failure_error_function=None)
        def lookup_reference() -> str:
            """Read the synthetic local normalization contract; no network access."""
            return json.dumps({"id": REFERENCE_ID, "text": REFERENCE})

        agents = ResearchAgents(
            advocates=tuple(
                agent(
                    f"Advocate {label}",
                    "Independently propose advisory normalization rechecks using supplied "
                    "reviewed records and current tasks. Depend on an existing human task. Do not propose "
                    "existing IDs or modify human tasks. Dependencies block execution until reviewed.",
                    output_type=Proposal,
                )
                for label in ("A", "B")
            ),
            judge=agent(
                "Judge",
                "Compare both advisory proposals against the supplied pool and reviewed "
                "records. Admit at most one new dependent task incrementally. Preserve "
                "human tasks, bounded capacity and dependencies. Do not stop for fixture success.",
                output_type=JudgeDecision,
            ),
            scout=agent(
                "Scout",
                "Call lookup_reference and report its actual synthetic material, source "
                "identifier and limitations. Never claim web research or holdout evidence.",
                output_type=ScoutReport,
                tools=[lookup_reference],
            ),
            knowledge_manager=agent(
                "Manager",
                "Update previous_notes from the new evidence batch as Markdown. Preserve "
                "every prior record ID, failures, sources and limitations. Notes are not measurements.",
            ),
        )

        def prepare(state, records, directory, experiments):
            fixture = directory / "candidate"
            fixture.mkdir()
            source = BROKEN
            for record in records:
                if (
                    record.task.id in state.request.dependencies
                    and record.evidence.execution_complete
                    and record.evidence.valid
                ):
                    source = record.evidence.observations["candidate_source"]
                    break
            (fixture / "solution.py").write_text(source)
            (fixture / "test_solution.py").write_text(TESTS)

            @function_tool(failure_error_function=None)
            def read_fixture() -> str:
                """Read the current candidate and fixed development tests."""
                return (fixture / "solution.py").read_text() + "\n" + TESTS

            @function_tool(failure_error_function=None)
            async def run_tests() -> str:
                """Run actual fixed tests in the restricted macOS subprocess."""
                async with experiments:
                    result = await asyncio.to_thread(run_fixture_tests, fixture)
                return json.dumps(
                    {"exit_code": result.returncode, "output": result.stderr}
                )

            @function_tool(failure_error_function=None)
            def replace_code(source: str) -> str:
                """Replace only candidate solution.py, leaving fixed tests unchanged."""
                if len(source) > 12000:
                    raise ValueError("Fixture source exceeds 12000 characters")
                (fixture / "solution.py").write_text(source)
                return "solution.py updated"

            async def collect_evidence():
                candidate = (fixture / "solution.py").read_text()
                # The controller already owns the shared experiment semaphore here.
                with tempfile.TemporaryDirectory(
                    prefix="sdk-independent-check-"
                ) as tmp:
                    independent = Path(tmp)
                    (independent / "solution.py").write_text(candidate)
                    (independent / "test_solution.py").write_text(TESTS)
                    result = await asyncio.to_thread(run_fixture_tests, independent)
                return Evidence(
                    execution_complete=True,
                    valid=result.returncode == 0,
                    observations={
                        "feedback": "development",
                        "candidate_source": candidate,
                        "fixed_tests": TESTS,
                        "exit_code": result.returncode,
                        "output": result.stderr,
                    },
                )

            return Investigation(
                planner=agent(
                    "Plan",
                    "Plan one concrete normalization change using only supplied reviewed "
                    "record IDs in based_on. Keep fixed evaluation unchanged.",
                    output_type=Plan,
                ),
                executor=agent(
                    "Execute",
                    "Implement the frozen Plan. Read fixture, run tests, edit solution.py "
                    "only when needed and rerun. Clip negatives to zero and divide by "
                    "the maximum clipped value; empty stays empty, zero maxima yield zeros. "
                    "Report actual results, including failures.",
                    tools=[read_fixture, run_tests, replace_code],
                ),
                reviewer=agent(
                    "Review",
                    "Independently interpret the frozen Plan, accepted summary and host "
                    "candidate plus fixed-test evidence. No hidden Execute history or write "
                    "tools. Prior reviews are evidence; Scout reports and notes are advisory. Distinguish validity, knowledge, goal and continuation. These "
                    "synthetic development tests cannot establish the research goal: use "
                    "goal not-achieved and continue_research true. Preserve limitations.",
                    output_type=Review,
                ),
                collect_evidence=collect_evidence,
            )

        pool = TaskPool(
            [
                TaskRequest(
                    id="human-a",
                    question="Fix empty and zero normalization.",
                    priority=1,
                ),
                TaskRequest(
                    id="human-b",
                    question="Fix negative and mixed normalization.",
                    priority=2,
                ),
            ],
            max_pending=8,
        )
        async with asyncio.timeout(args.seconds):
            result = await run_research(
                goal="Investigate the synthetic normalization contract without generalization claims.",
                pool=pool,
                agents=agents,
                prepare=prepare,
                output_dir=root / "research",
                max_rounds=args.rounds,
                investigation_concurrency=args.investigations,
                experiment_concurrency=args.experiments,
                max_turns=12,
            )
        print(
            json.dumps(
                {
                    "model_kind": "scripted"
                    if args.model == "scripted"
                    else "external",
                    "rounds": len(result.records),
                    "task_ids": [record.task.id for record in result.records],
                    "goal": result.goal,
                    "stopped": result.stopped,
                    "scripted_model_calls": dict(calls),
                    "trace_id": result.trace_id,
                    "trace": str(root / "trace.jsonl"),
                    "result": str(root / "research" / "result.json"),
                }
            )
        )
    finally:
        if client is not None:
            await client.close()
        processor.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="New private directory outside Git")
    parser.add_argument(
        "--model",
        default="scripted",
        help="scripted or an authorized exact Chat Completions model ID",
    )
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--investigations", type=int, default=2)
    parser.add_argument("--experiments", type=int, default=1)
    parser.add_argument("--seconds", type=float, default=180)
    args = parser.parse_args()
    if min(args.rounds, args.investigations, args.experiments, args.seconds) <= 0:
        parser.error("All limits must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
