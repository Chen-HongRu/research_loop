"""Command-line entry points; running research is always an explicit command."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

from .core import Study, StudyError, initialize
from .harness import HarnessConfig, HarnessError
from .execution_bridge import load_execution_harness
from .meta import MetaStudy
from .tasks import TaskQueue


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="research-loop")
    commands = parser.add_subparsers(dest="command", required=True)
    tasks = commands.add_parser("task", help="Manage durable operator requests")
    task_commands = tasks.add_subparsers(dest="task_command", required=True)
    for name in ("submit", "list", "status", "cancel", "priority"):
        command = task_commands.add_parser(name)
        command.add_argument("study", type=Path)
        if name == "submit":
            command.add_argument("--request", required=True, type=Path)
        if name in {"status", "cancel", "priority"}:
            command.add_argument("id")
        if name == "priority":
            command.add_argument("priority", type=int)
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
    init.add_argument(
        "--long-training", type=Path, help="Authorized long-training policy JSON"
    )
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
    run.add_argument(
        "--backend", type=Path, help="Absolute trusted legacy execution adapter module"
    )
    run.add_argument(
        "--backend-config",
        type=Path,
        help="Absolute immutable adapter JSON configuration",
    )
    resume = commands.add_parser("resume", help="Reopen an unfinished reviewed stop")
    resume.add_argument("study", type=Path)
    resume.add_argument("--reason", required=True)
    for name, description in (
        ("status", "Read the controller state; no model call"),
        ("graph", "Read published experiment nodes; no model call"),
        ("frontiers", "Read the Frontier views; no model call"),
        (
            "knowledge",
            "Read current research plus independently reviewed long training",
        ),
        ("rebuild-frontiers", "Rebuild Frontier views from published records"),
        ("retry", "Allow one new attempt after reconciling failed/unknown effects"),
    ):
        command = commands.add_parser(name, help=description)
        command.add_argument("study", type=Path)
    args = parser.parse_args(argv)
    handle_sigterm = (
        args.command == "run" and threading.current_thread() is threading.main_thread()
    )
    if handle_sigterm:
        previous_sigterm = signal.signal(signal.SIGTERM, _interrupt)
    try:
        if args.command == "task":
            queue = TaskQueue(args.study)
            if args.task_command == "submit":
                result = queue.submit(json.loads(args.request.read_text()))
            elif args.task_command == "cancel":
                result = queue.cancel(args.id)
            elif args.task_command == "priority":
                result = queue.set_priority(args.id, args.priority)
            elif args.task_command == "status":
                queue.request(args.id)
                result = next(row for row in queue.snapshot() if row["id"] == args.id)
            else:
                result = queue.snapshot()
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif args.command == "meta":
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
                long_training=json.loads(args.long_training.read_text(encoding="utf-8"))
                if args.long_training
                else None,
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
                study.harness = load_execution_harness(
                    args.study,
                    study.harness,
                    backend=args.backend,
                    backend_config=args.backend_config,
                )
                result = study.run(iterations=args.iterations)
            elif args.command == "resume":
                result = study.resume(reason=args.reason)
            elif args.command == "retry":
                result = study.retry()
            elif args.command == "graph":
                result = study.graph()
            elif args.command == "knowledge":
                result = study.knowledge()
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
            "Interrupted; run again to recover completed or read-only work. "
            "Incomplete Execute requires inspecting retained effects and explicit retry.",
            file=sys.stderr,
        )
        return 130
    finally:
        if handle_sigterm:
            signal.signal(signal.SIGTERM, previous_sigterm)
    return 0
