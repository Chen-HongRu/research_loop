"""Command adapter for the shared application and read-only query interfaces."""

import argparse
import json
import sys
from pathlib import Path

from . import application, queries


def _target(parser, *, revision=False):
    parser.add_argument("--target", default="run", help="run or a Group run ID")
    parser.add_argument("--id", dest="identity", help="Stable intervention ID")
    if revision:
        parser.add_argument("--expected-revision")


def parser():
    result = argparse.ArgumentParser(prog="research-loop", description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser(
        "prepare", help="Prepare one frozen run without model calls"
    )
    prepare.add_argument("root", type=Path)
    prepare.add_argument("--config", type=Path, required=True)
    prepare.add_argument("--project", type=Path, required=True)
    prepare.add_argument("--goal", type=Path, required=True)
    prepare.add_argument("--input-ref", type=Path, action="append", default=[])
    for name in ("inspect", "start", "status", "stop", "resume", "pause", "continue"):
        command = commands.add_parser(name)
        command.add_argument("root", type=Path)
        if name == "start":
            command.add_argument("--foreground", action="store_true")
        elif name in {"stop", "pause", "continue"}:
            _target(command, revision=name != "stop")
    intervene = commands.add_parser(
        "intervene", help="Submit an explicit work or control input"
    )
    intervene.add_argument("root", type=Path)
    intervene.add_argument("--kind", required=True)
    _target(intervene, revision=True)
    source = intervene.add_mutually_exclusive_group()
    source.add_argument("--payload", type=Path, help="JSON object file")
    source.add_argument(
        "--file", type=Path, help="UTF-8 text file, submitted as {text: ...}"
    )
    revise = commands.add_parser(
        "revise", help="Prepare and request explicit configuration activation"
    )
    revise.add_argument("root", type=Path)
    revise.add_argument("--config", type=Path, required=True)
    revise.add_argument(
        "--affected",
        action="append",
        required=True,
        help="Affected Group run ID, or all",
    )
    revise.add_argument("--expected-revision")
    revise.add_argument("--id", dest="identity")
    query = commands.add_parser("query", help="Read persisted execution facts")
    query.add_argument("root", type=Path)
    query.add_argument(
        "--kind",
        required=True,
        choices=("runs", "steps", "calls", "handoffs", "operations", "interventions"),
    )
    query.add_argument("--run-id", help="Group run ID (required for execution records)")
    query.add_argument("--id", help="Record ID for a single result")
    query.add_argument("--cursor")
    query.add_argument("--limit", type=int, default=50)
    efficiency = commands.add_parser(
        "efficiency", help="Report observed execution efficiency without model calls"
    )
    efficiency.add_argument("root", type=Path)
    efficiency.add_argument("--since")
    efficiency.add_argument("--until")
    efficiency.add_argument("--sessions-dir", type=Path)
    efficiency.add_argument("--ledger-path", type=Path)
    commands.add_parser(
        "doctor", help="Report local SDK/login metadata without model calls"
    )
    return result


def _query(args):
    if args.kind == "runs":
        if args.id and args.run_id and args.id != args.run_id:
            raise ValueError("Use one Group run ID for a single run query")
        identity = args.id or args.run_id
        return (
            queries.get_run(args.root, identity)
            if identity
            else queries.list_runs(args.root, cursor=args.cursor, limit=args.limit)
        )
    if args.kind == "interventions":
        return (
            queries.get_intervention(args.root, args.id)
            if args.id
            else queries.list_interventions(
                args.root, cursor=args.cursor, limit=args.limit
            )
        )
    if not args.run_id:
        raise ValueError("Execution record queries require --run-id")
    listing, get = {
        "steps": (queries.list_steps, queries.get_step),
        "calls": (queries.list_calls, queries.get_call),
        "handoffs": (queries.list_handoffs, queries.get_handoff),
        "operations": (queries.list_operations, queries.get_operation),
    }[args.kind]
    return (
        get(args.root, args.run_id, args.id)
        if args.id
        else listing(args.root, args.run_id, cursor=args.cursor, limit=args.limit)
    )


def _dispatch(args):
    command = args.command
    if command == "doctor":
        from importlib.metadata import version
        from codex_cli_bin import bundled_codex_path

        return {
            "python": sys.version.split()[0],
            "codex_sdk": version("openai-codex"),
            "native_binary": str(bundled_codex_path()),
            "subscription_login_present": (Path.home() / ".codex/auth.json").is_file(),
            "model_call": False,
        }
    if command == "prepare":
        return application.prepare(
            args.config,
            args.root,
            project=args.project,
            goal=args.goal,
            input_refs=args.input_ref,
        )
    if command == "inspect":
        return application.inspect(args.root)
    if command == "start":
        if args.foreground:
            return application.start(args.root, foreground=True)
        return application.start(args.root)
    if command == "status":
        return application.status(args.root)
    if command == "resume":
        return application.resume(args.root)
    if command == "stop":
        return application.stop(args.root, target=args.target, identity=args.identity)
    if command in {"pause", "continue", "intervene"}:
        payload = {}
        if command == "intervene":
            if args.payload:
                payload = json.loads(args.payload.read_text())
                if not isinstance(payload, dict):
                    raise ValueError("Intervention payload must be a JSON object")
            elif args.file:
                payload = {"text": args.file.read_text(encoding="utf-8")}
        return application.intervene(
            args.root,
            kind=args.kind if command == "intervene" else command,
            payload=payload,
            target=args.target,
            expected_revision=args.expected_revision,
            identity=args.identity,
        )
    if command == "revise":
        return application.revise(
            args.root,
            args.config,
            affected=args.affected,
            expected_revision=args.expected_revision,
            identity=args.identity,
        )
    if command == "query":
        return _query(args)
    if command == "efficiency":
        from .efficiency import report

        return report(
            args.root,
            since=args.since,
            until=args.until,
            sessions_dir=args.sessions_dir,
            ledger_path=args.ledger_path,
        )
    raise ValueError("Unknown command")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = _dispatch(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return (
            1
            if isinstance(result, dict)
            and result.get("status") == "needs-reconciliation"
            else 0
        )
    except Exception as error:
        # Validation errors may include the entire submitted value on later lines.
        message = str(error).splitlines()[0] if str(error) else "Command failed"
        print(
            json.dumps(
                {"error": type(error).__name__, "message": message[:500]},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
