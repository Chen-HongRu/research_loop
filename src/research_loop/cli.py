"""Command-line entry points; running research is always an explicit command."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import Study, StudyError, initialize
from .harness import HarnessConfig, HarnessError
from .meta import MetaStudy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="research-loop")
    commands = parser.add_subparsers(dest="command", required=True)
    meta = commands.add_parser("meta", help="Manage protected Meta Loop versions")
    meta_commands = meta.add_subparsers(dest="meta_command", required=True)
    meta_init = meta_commands.add_parser("init", help="Freeze a baseline and protocol")
    meta_init.add_argument("study", type=Path)
    meta_init.add_argument("--source", required=True, type=Path)
    meta_init.add_argument("--protocol", required=True, type=Path)
    meta_init.add_argument("--policy", required=True, type=Path)
    meta_init.add_argument("--budget", type=Path)
    meta_status = meta_commands.add_parser("status")
    meta_status.add_argument("study", type=Path)
    meta_decide = meta_commands.add_parser("decide")
    meta_decide.add_argument("study", type=Path)
    meta_decide.add_argument("candidate_id")
    meta_decide.add_argument("--publication", type=Path)
    meta_rollback = meta_commands.add_parser("rollback")
    meta_rollback.add_argument("study", type=Path)
    meta_rollback.add_argument("--reason", required=True)
    meta_rollback.add_argument("--expected-ref", required=True)
    init = commands.add_parser("init", help="Create study documents; no model call")
    init.add_argument("study", type=Path)
    init.add_argument("--goal", type=Path, required=True)
    init.add_argument("--workspace", type=Path)
    init.add_argument("--evaluation", type=Path, help="Global evaluation JSON file")
    init.add_argument("--harness", choices=["codex", "pi"], default="codex")
    init.add_argument("--model", default="gpt-6.1-sol")
    init.add_argument("--reasoning", default="medium")
    init.add_argument("--executable", help="Exact CLI path; defaults to codex or pi")
    init.add_argument(
        "--timeout", type=int, default=600, help="Seconds per CLI subprocess"
    )
    init.add_argument("--max-iterations", type=int, default=10)
    init.add_argument("--allow-write", action="store_true")
    init.add_argument("--max-calls", type=int, default=100)
    init.add_argument("--max-seconds", type=int, default=3600)
    init.add_argument("--selection-seed", type=int, default=0)
    init.add_argument("--debate-rounds", type=int, default=1)
    init.add_argument("--max-resamples", type=int, default=1)
    init.add_argument(
        "--selection-policy", choices=["debate", "direct"], default="debate"
    )
    init.add_argument("--review-policy", choices=["full", "concise"], default="full")
    run = commands.add_parser("run", help="Run or recover bounded research iterations")
    run.add_argument("study", type=Path)
    run.add_argument("--iterations", type=int, default=1)
    for name, description in (
        ("status", "Read the controller state; no model call"),
        ("graph", "Read published experiment nodes; no model call"),
        ("frontiers", "Read the Frontier views; no model call"),
        ("rebuild-frontiers", "Rebuild Frontier views from published records"),
        ("retry", "Allow one new attempt after reconciling failed/unknown effects"),
    ):
        command = commands.add_parser(name, help=description)
        command.add_argument("study", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "meta":
            if args.meta_command == "init":
                study = MetaStudy.initialize(
                    args.study,
                    source=args.source,
                    protocol=json.loads(args.protocol.read_text(encoding="utf-8")),
                    baseline_policy=json.loads(args.policy.read_text(encoding="utf-8")),
                    budget=json.loads(args.budget.read_text(encoding="utf-8"))
                    if args.budget
                    else None,
                )
                result = study.status()
            else:
                study = MetaStudy(args.study)
                if args.meta_command == "decide":
                    result = study.decide(
                        args.candidate_id, publication=args.publication
                    )
                elif args.meta_command == "rollback":
                    result = study.rollback(args.reason, args.expected_ref)
                else:
                    result = study.status()
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif args.command == "init":
            root = initialize(
                args.study,
                goal=args.goal.read_text(encoding="utf-8"),
                workspace=args.workspace,
                harness=HarnessConfig(
                    backend=args.harness,
                    model=args.model,
                    reasoning=args.reasoning,
                    executable=args.executable,
                    timeout_seconds=args.timeout,
                ),
                max_iterations=args.max_iterations,
                allow_write=args.allow_write,
                global_evaluation=(
                    json.loads(args.evaluation.read_text(encoding="utf-8"))
                    if args.evaluation
                    else None
                ),
                max_calls=args.max_calls,
                max_seconds=args.max_seconds,
                selection={
                    "seed": args.selection_seed,
                    "debate_rounds": args.debate_rounds,
                    "max_resamples": args.max_resamples,
                },
                strategy={
                    "selection": args.selection_policy,
                    "review": args.review_policy,
                },
            )
            print(f"Initialized {root}; review goal.md and study.json before running.")
        else:
            study = Study(args.study)
            if args.command == "run":
                result = study.run(iterations=args.iterations)
            elif args.command == "retry":
                result = study.retry()
            elif args.command == "graph":
                result = study.graph()
            elif args.command == "frontiers":
                result = study.frontiers()
            elif args.command == "rebuild-frontiers":
                result = study.rebuild_frontiers()
            else:
                result = study.status()
            print(json.dumps(result, indent=2, ensure_ascii=False))
    except (StudyError, HarnessError, OSError, ValueError) as exc:
        print(f"research-loop: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "Interrupted; inspect the retained attempt before retrying.",
            file=sys.stderr,
        )
        return 130
    return 0
