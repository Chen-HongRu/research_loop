"""Trusted subprocess entry for one frozen research version; outputs stay external."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


FIXED_REQUEST = {
    "job_id": "tiny-001",
    "epochs": 1,
    "lr": 0.001,
    "batch_size": 128,
    "seed": 0,
}


def read(path: Path):
    return json.loads(path.read_text())


def write(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def fixed_request(request: dict) -> dict:
    if request != FIXED_REQUEST or any(
        type(request.get(key)) is not type(value)
        for key, value in FIXED_REQUEST.items()
    ):
        raise ValueError("This paired protocol permits only the exact fixed request")
    return dict(request)


def usage(study: Path) -> dict:
    """Missing or partial CLI usage is an error, never a zero-token observation."""
    total = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}
    processes = sorted(study.glob("iterations/**/process.json"))
    if not processes:
        raise ValueError("No CLI completion receipts")
    for path in processes:
        process = read(path)
        if process["status"] != "succeeded":
            raise ValueError(f"Unsuccessful CLI receipt: {path}")
        events = [
            json.loads(line)
            for line in path.with_name("stdout.jsonl").read_text().splitlines()
            if line.strip()
        ]
        completions = [
            event for event in events if event.get("type") == "turn.completed"
        ]
        if len(completions) != 1:
            raise ValueError(f"Missing or ambiguous CLI usage: {path}")
        reported = completions[0].get("usage", {})
        for key in total:
            value = reported.get(key)
            if type(value) is not int or value < 0:
                raise ValueError(f"Missing or invalid {key}: {path}")
            total[key] += value
    return {
        **total,
        "tokens": total["input_tokens"] + total["output_tokens"],
        "calls": len(processes),
    }


class UsageBudgetHarness:
    """Stop at a completed CLI boundary; retain any single-call token overshoot."""

    def __init__(self, delegate, study: Path, token_limit: int = 5000000):
        self.delegate, self.study, self.token_limit = delegate, study, token_limit
        self.tokens = 0

    def run(self, **kwargs):
        if self.tokens >= self.token_limit:
            raise ValueError("Reported token budget exhausted before next call")
        result = self.delegate.run(**kwargs)
        self.tokens = usage(self.study)["tokens"]
        if self.tokens > self.token_limit:
            raise ValueError("Reported token budget exceeded; retain receipt and stop")
        return result


def run_review_recovery(study):
    """One structural, read-only retry; never repeat Execute or unknown effects."""
    from research_loop.core import StudyError
    from pydantic import ValidationError

    try:
        return study.run(iterations=1)
    except (StudyError, ValueError) as error:
        structural_error = (
            str(error)
            == "Candidate reviews must match unique current measurements exactly"
        )
        if isinstance(error, ValidationError):
            details = error.errors(include_input=False)
            structural_error = (
                len(details) == 1
                and details[0]["loc"] == ("candidate_reviews",)
                and details[0]["type"] == "too_long"
                and details[0].get("ctx", {}).get("max_length") == 0
            )
        state = study.status()
        if (
            state.get("phase") == "review"
            and state.get("status") == "needs_attention"
            and structural_error
            and state["call_count"] < study.config["max_calls"]
        ):
            receipt = study.root / state["calls"]["review"] / "process.json"
            if receipt.is_file() and read(receipt).get("status") == "succeeded":
                study.retry()
                return study.run(iterations=1)
        raise


def baseline(path: Path, text: str, policy: dict) -> None:
    path.mkdir(parents=True, exist_ok=False)
    (path / "README.md").write_text(text)
    write(path / "strategy.json", policy)
    for arguments in (
        ["init"],
        ["add", "README.md", "strategy.json"],
        [
            "-c",
            "user.name=Meta Smoke",
            "-c",
            "user.email=meta@localhost",
            "commit",
            "-m",
            "Fixed investigation interface",
        ],
    ):
        subprocess.run(
            ["git", "-C", str(path), *arguments], check=True, capture_output=True
        )


CANARY_INTERFACE = """# Bounded supervision canary
Propose one auditable policy configuration for a subsequent investigation.
The only permitted policy keys are selection (debate/direct) and review
(full/concise). Inspect strategy.json, then write proposal.json containing
exactly these keys and allowed values, with selection=direct, review=concise.
This is a proposal only. Do not run training, launch agents, subprocess research
loops, use network, install packages, or change control/evaluation files.
There is no training or Meta request bridge in this workspace.
"""
CANARY_GOAL = """Complete one bounded supervision investigation using README.md.
Propose the allowed policy configuration in proposal.json, verify its actual contents,
and publish route and global research. No training, recursive loop, or performance claim.
Plan base_experiment=null and references=[]; describe only existing baseline
README.md and strategy.json inputs with their actual Git HEAD in markdown.
Execute returns exactly one artifact: proposal.json in the current workspace,
version=policy-proposal-v1, measurements=[]. Review reads the actual file and confirms
scope, updates both memories, then stops after one iteration. Do not name an absent
workspace as an input artifact. Plan timeout_seconds must be <=600.
Review candidate_reviews corresponds ONLY to numeric measurement candidate IDs,
not every artifact: because measurements=[], candidate_reviews MUST be []. Describe
the actual proposal-file verification in markdown and research memory instead.
Return both complete research texts; the controller publishes them AFTER Review,
so Review does not need to observe its own future publication to finish its job.
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=["cactus", "canary"], required=True)
    parser.add_argument("--codex", required=True)
    parser.add_argument(
        "--host", required=True, help="Operator-configured SSH host alias"
    )
    parser.add_argument("--remote-base", required=True)
    parser.add_argument("--gpu", default="1")
    parser.add_argument("--job-prefix", required=True)
    args = parser.parse_args(argv)
    source = args.source.resolve()
    # Resolve the package before imports, even with an editable local installation.
    sys.path.insert(0, str(source / "src"))
    from research_loop import Study, initialize
    from research_loop import core, harness, protocol, strategy
    from research_loop.harness import CodexHarness, HarnessConfig
    from bridge import CactusBridge
    from run_smoke import GOAL, INTERFACE, frozen_job

    loaded = {
        module.__name__: str(Path(module.__file__).resolve())
        for module in (core, harness, protocol, strategy)
    }
    if not all(Path(path).is_relative_to(source / "src") for path in loaded.values()):
        raise ValueError("Loaded a live module instead of the frozen version")
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    policy = strategy.validate_strategy(read(source / "strategy.json"))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    summary = {
        "version_ref": args.ref,
        "commit": commit,
        "policy": policy,
        "loaded_modules": loaded,
        "mode": args.mode,
        "status": "running",
    }
    write(output / "result.json", summary)
    config = HarnessConfig(executable=args.codex, timeout_seconds=900)

    class PairedBridge(CactusBridge):
        def _train(self, request, destination):
            fixed_request(request)
            return super()._train(request, destination)

    bridge = PairedBridge(
        config=config,
        host=args.host,
        remote_base=args.remote_base,
        gpu=args.gpu,
        study_id=args.job_prefix,
        seconds=1800,
    )
    try:
        is_cactus = args.mode == "cactus"
        interface = (
            INTERFACE
            + "\nPaired protocol: the request must be exactly "
            + json.dumps(FIXED_REQUEST)
            + ". All fields are fixed; no configuration search.\n"
            if is_cactus
            else CANARY_INTERFACE
        )
        baseline(output / "baseline", interface, policy)
        evaluation = (
            {
                "id": "global-development-v1",
                "comparison_scope": "development-v1",
                "protocol": "Fixed Cactus dev split, final tiny-cnn checkpoint, exact paired request, one job.",
                "data_role": "development",
                "allow_feedback": True,
                "objectives": [
                    {"name": "auc", "direction": "maximize", "unit": "fraction"}
                ],
                "constraints": [],
            }
            if is_cactus
            else None
        )
        initialize(
            output / "study",
            goal=(
                GOAL + "\nUse the exact paired request in README.md.\n"
                if is_cactus
                else CANARY_GOAL
            ),
            workspace=output / "baseline",
            harness=config,
            allow_write=True,
            global_evaluation=evaluation,
            max_iterations=1,
            max_calls=12,
            max_seconds=1800 if is_cactus else 900,
            selection={"seed": 0, "debate_rounds": 1, "max_resamples": 0},
            strategy=policy,
        )
        active_harness = UsageBudgetHarness(
            bridge if is_cactus else CodexHarness(config), output / "study"
        )
        state = run_review_recovery(Study(output / "study", harness=active_harness))
        summary["state"] = state
        if (
            state["phase"] != "done"
            or not state.get("review_ref")
            or not (output / "study/iterations/0001/publication.json").is_file()
        ):
            raise ValueError("Investigation did not publish an independent Review")
        if is_cactus:
            job, measurement = frozen_job(output / "study", state, bridge)
            review = read(output / "study" / state["review_ref"])
            if len(review["candidate_reviews"]) != 1 or any(
                entry["valid"] is not True or entry["verified"] is not True
                for entry in review["candidate_reviews"]
            ):
                raise ValueError(
                    "Independent Review did not validate the actual candidate"
                )
            summary.update(
                job_id=job,
                bridge_id=args.job_prefix,
                measurement=measurement,
                quality=bridge.jobs[job]["score"],
                bridge_jobs=bridge.jobs,
            )
        else:
            executed = read(output / "study" / state["execution_ref"])
            if len(executed["artifacts"]) != 1 or executed["measurements"]:
                raise ValueError(
                    "Canary must produce exactly one proposal and no measurements"
                )
            artifact = executed["artifacts"][0]
            if (
                Path(artifact["path"]).resolve()
                != output / "study/iterations/0001/workspace/proposal.json"
                or artifact["version"] != "policy-proposal-v1"
            ):
                raise ValueError("Unexpected canary artifact")
            if strategy.validate_strategy(read(Path(artifact["path"]))) != {
                "selection": "direct",
                "review": "concise",
            }:
                raise ValueError("Canary did not propose the bounded policy")
            summary.update(
                proposal=artifact, training_capability=False, recursive_capability=False
            )
        observations = read(output / "study/observations.json")
        if not observations["totals"]["complete"]:
            raise ValueError("Incomplete phase clocks")
        summary.update(
            status="completed",
            usage=usage(output / "study"),
            observations=observations["totals"],
        )
    except Exception as exc:
        summary.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        summary.update(
            total_seconds=time.monotonic() - started,
            actual_training_requests=int(bridge.started),
            bridge_errors=bridge.bridge_errors,
        )
        write(output / "result.json", summary)
    print(
        json.dumps({k: summary[k] for k in ("status", "version_ref", "total_seconds")})
    )
    return 0 if summary["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
