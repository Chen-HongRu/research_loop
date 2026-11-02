"""Run the real controller with scripted responses; no agent CLI is invoked."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from research_loop import Study, initialize
from research_loop.harness import publish_completed_response
from research_loop.protocol import SCHEMAS, validate_response

LABEL = "[SCRIPTED OFFLINE DEMO]"
ACTION = "Derive n(n+1) is even by considering the parity of consecutive integers."
PROOF = (
    "Let n be a nonnegative integer. If n is even, n=2k for an integer k, "
    "so n(n+1)=2k(n+1). If n is odd, n=2k+1, so n+1=2(k+1) and "
    "n(n+1)=2n(k+1). In both cases the product is an integer multiple of 2. "
    "Since n²+n=n(n+1), the claim holds for every nonnegative integer n."
)


class ScriptedHarness:
    """An AgentHarness implementation supplying labeled, fixed stage envelopes.

    The controller still builds contexts, validates responses, accounts for calls,
    accepts completion receipts, and publishes Review. No model checks the proof.
    """

    def run(
        self,
        *,
        prompt: str,
        schema: dict,
        workspace: Path,
        run_dir: Path,
        writable: bool = False,
        timeout_seconds: int | None = None,
    ) -> dict:
        phase = next(name for name, value in SCHEMAS.items() if value is schema)
        context = json.loads((run_dir / "context.json").read_text())
        result = self.response(phase, context)
        validate_response(phase, result)
        (run_dir / "prompt.md").write_text(prompt)
        (run_dir / "response.json").write_text(json.dumps(result, indent=2) + "\n")
        (run_dir / "process.json").write_text(
            json.dumps(
                {
                    "status": "succeeded",
                    "backend": "scripted-offline-demo",
                    "model_called": False,
                    "notice": "Fixed demo response; no agent process was started.",
                },
                indent=2,
            )
            + "\n"
        )
        publish_completed_response(run_dir, result)
        return result

    def response(self, phase: str, context: dict) -> dict:
        if phase == "proposal":
            return {
                "markdown": f"{LABEL} Use an exhaustive parity argument, not samples.",
                "route_id": context["route"]["id"],
                "route_title": "Parity of consecutive integers",
                "action": ACTION,
                "base_experiment": None,
                "references": [],
            }
        if phase in {"critique", "response"}:
            return {
                "markdown": f"{LABEL} The proof must cover both parity cases and n=0."
            }
        if phase == "judge":
            return {
                "decision": "select",
                "choice": "a",
                "action": context["proposals"]["a"]["action"],
                "markdown": f"{LABEL} Fixed selection of a; no independent comparison.",
                "goal_status": "undetermined",
                "stop_reason": None,
            }
        if phase == "plan":
            return {
                "decision": "investigate",
                "markdown": f"{LABEL} Plan only: split into even and odd cases.",
                "route_research": f"{LABEL} Question: is n²+n always even for n≥0?",
                "base_experiment": None,
                "references": [],
                "input_artifacts": [],
                "local_evaluation": None,
                "contract": {
                    "rationale": "A bounded qualitative derivation demonstrates the loop.",
                    "action": context["selected"]["judge"]["action"],
                    "evaluation": "Check the two parity cases; no numeric evaluation.",
                    "decision_criteria": "A complete argument for all n≥0, including zero.",
                    "evidence_required": ["Explicit algebra in the execution report"],
                    "timeout_seconds": 30,
                },
                "stop_reason": None,
            }
        if phase == "execute":
            return {
                "outcome": "completed",
                "markdown": f"{LABEL} Prewritten derivation, not model output.\n\n{PROOF}",
                "artifacts": [],
                "measurements": [],
            }
        if phase == "review":
            research = (
                f"# Scripted demo research\n\n{LABEL}\n\n{PROOF}\n\n"
                "Source: this iteration's retained Execute response. The review and "
                "goal judgment are prewritten demonstration data, not an independent "
                "verification or a new research result. No empirical metrics were collected."
            )
            return {
                "markdown": (
                    f"{LABEL} Prewritten review: the parity cases are exhaustive and "
                    "include n=0. Stop after this wiring demonstration."
                ),
                "route_research": research,
                "global_research": research,
                "route_status": "closed",
                "route_reason": "Scripted proof example completed.",
                "resume_condition": None,
                "goal_status": "met",
                "decision": "stop",
                "candidate_reviews": [],
                "candidate_corrections": [],
                "knowledge_changes": [
                    "Prewritten parity derivation retained; no metrics."
                ],
            }
        raise ValueError(f"Unexpected demo phase: {phase}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("studies/demo"), help="New study directory"
    )
    args = parser.parse_args()
    root = args.output.expanduser().absolute()
    # Unlike initialize(), the example also refuses an existing empty directory.
    if root.exists() or root.is_symlink():
        parser.error(f"Refusing to overwrite existing output: {root}")
    goal = Path(__file__).with_name("proof-goal.md").read_text()
    initialize(root, goal=goal, max_iterations=1, max_calls=10, max_seconds=60)
    state = Study(root, ScriptedHarness()).run(iterations=1)
    publication = json.loads((root / "iterations/0001/publication.json").read_text())
    if not (
        state["iteration"] == 1
        and state["phase"] == "done"
        and state["status"] == "stopped"
        and publication["metadata"]["status"] == "reviewed"
    ):
        raise RuntimeError("Demo did not finish one reviewed publication")
    print("SCRIPTED OFFLINE DEMO · no model / network / GPU calls")
    print("Real controller: selection → plan → execute → review → publication")
    print(f"Published: 1 reviewed iteration · {state['call_count']} scripted calls")
    print(f"State: {state['status']} · goal_status={state['goal_status']} (scripted)")
    print(f"Records: {root}")
    print("Inspect: state.json · iterations/0001/publication.json · research.md")


if __name__ == "__main__":
    main()
