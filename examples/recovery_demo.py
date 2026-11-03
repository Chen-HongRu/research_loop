"""Offline recovery wiring: accepted request, proven no compute, retry, Review.

Uses the real Study/Git controller and a scripted local backend. No Agent, network
or GPU is used. The scripted Review demonstrates evidence delivery, not research
quality or a model's interpretation of it.
"""

import argparse
import json
from pathlib import Path

from offline_demo import ScriptedHarness
from research_loop import Study, initialize
from research_loop.submission import claim_request, submit


def read(path):
    return json.loads(path.read_text())


class LocalHarness(ScriptedHarness):
    def __init__(self, root):
        self.root = root
        self.allocations = 0

    def run(self, **kwargs):
        if "Stage: execute\n" in kwargs["prompt"]:
            attempt = kwargs["run_dir"]
            context = read(attempt / "context.json")
            workspace = kwargs["workspace"]
            submit(workspace)
            claim_request(workspace)
            if attempt.name == "attempt-001":
                # Synthetic trusted backend boundary: this branch never starts
                # computation. In a real adapter use its durable execution journal.
                proof = {
                    "attempt": str(attempt.relative_to(self.root)),
                    "plan": context["plan"],
                    "submission_intent": False,
                    "allocation_count": 0,
                }
                (attempt / "backend-proof.json").write_text(json.dumps(proof))
                raise RuntimeError("Synthetic preparation failure before compute")
            self.allocations += 1
        return super().run(**kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.expanduser().resolve()
    if root.exists():
        parser.error("Output must be a new directory")
    initialize(
        root,
        goal="Demonstrate recovery evidence delivery with scripted responses.",
        allow_write=True,
        max_iterations=1,
        max_calls=12,
        max_seconds=120,
        strategy={"selection": "direct", "review": "full"},
    )
    harness = LocalHarness(root)
    study = Study(root, harness)
    try:
        study.run()
    except RuntimeError as error:
        if str(error) != "Synthetic preparation failure before compute":
            raise
    before = study.status()
    source_attempt = before["calls"]["execute"]
    plan_ref = before["plan_ref"]
    attempt = root / source_attempt
    proof_path = attempt / "backend-proof.json"
    archive = attempt / "recovery-interfaces/preparation-failure"

    def certify(state):
        proof = read(proof_path)
        if (
            proof["attempt"] != source_attempt
            or proof["plan"] != read(root / plan_ref)
            or proof["submission_intent"] is not False
            or proof["allocation_count"] != 0
            or harness.allocations != 0
        ):
            raise RuntimeError("Backend effects are unresolved")
        return {
            "source_iteration": "0001",
            "source_attempt": source_attempt,
            "plan_ref": plan_ref,
            "action": "retry",
            "effect": "never_submitted",
            "summary": "Controller accepted the request; backend proved no compute submission.",
            "proof_refs": [str(proof_path)],
        }

    prepared = study.reconcile_execution(
        recovery_id="preparation-failure", certify=certify
    )
    assert prepared["call_count"] == before["call_count"]
    assert prepared["elapsed_seconds"] == before["elapsed_seconds"]
    state = study.run()
    review = root / state["calls"]["review"]
    context = read(review / "context.json")
    assert read(archive / "retirement.json")["state_before"]["status"] == "accepted"
    assert context["execution_recovery"]
    assert str(proof_path) in (review / "prompt.md").read_text()
    assert harness.allocations == 1
    assert state["status"] == "stopped"
    assert (attempt / "backend-proof.json").exists()
    print("OFFLINE: real Study/Git, scripted backend and Review; no model/network/GPU")
    print("Accepted first request → proven never submitted → one allocation → Review")
    print(f"Review context and prompt contain recovery proof: {review}")


if __name__ == "__main__":
    main()
