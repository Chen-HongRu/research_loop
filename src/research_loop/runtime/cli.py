"""Explicit SQLite-runtime commands; legacy Study commands keep their format."""

import argparse
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import sys
import threading
import time
import uuid

from ..evaluation import validate_evaluation
from .application import load_application, prepare_application
from .config_yaml import load_config
from .controls import ControlInbox, receipt, submit_command
from .coordinator import ConcurrentCoordinator, validate_settings
from .store import OwnershipError, StateStore, _decode


def _object(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def _count(value):
    if value == "none":
        return None
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("Expected a nonnegative count or none")
    return number


def _add_config_arguments(command):
    command.add_argument(
        "--config", required=True, type=Path, help="Study configuration (.yaml or .yml)"
    )
    command.add_argument("--application", required=True, type=Path)
    command.add_argument(
        "--application-config",
        type=Path,
        help="Trusted application configuration (.yaml or .yml)",
    )


def _parser():
    parser = argparse.ArgumentParser(prog="research-loop runtime")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser(
        "init", help="Create a new SQLite Study without invoking callbacks"
    )
    init.add_argument("study", type=Path)
    _add_config_arguments(init)
    init.add_argument("--max-iterations", default=argparse.SUPPRESS, type=_count)
    init.add_argument("--max-calls", default=argparse.SUPPRESS, type=_count)
    init.add_argument("--max-seconds", default=argparse.SUPPRESS, type=float)
    init.add_argument("--admission-deadline", default=argparse.SUPPRESS, type=float)
    run = commands.add_parser(
        "run", help="Own scheduling until idle, interrupted, or a tick limit"
    )
    run.add_argument("study", type=Path)
    run.add_argument("--poll-interval", type=float, default=0.1)
    run.add_argument("--max-ticks", type=int)
    run.add_argument(
        "--watch",
        action="store_true",
        help="Keep the owner available for new control requests while idle",
    )
    for name in ("status", "events", "dashboard", "request-status"):
        command = commands.add_parser(name)
        command.add_argument("study", type=Path)
        if name == "events":
            command.add_argument("--consumer", required=True)
        elif name == "dashboard":
            command.add_argument("--port", type=int, default=8000)
        elif name == "request-status":
            command.add_argument("request_id")
    for name in (
        "stop",
        "scout",
        "plan",
        "retry-review",
        "cancel-operation",
        "retry-action",
        "ack-event",
    ):
        command = commands.add_parser(name)
        command.add_argument("study", type=Path)
        command.add_argument("--request-id", default=None)
        if name in {"stop", "plan", "retry-review", "cancel-operation", "retry-action"}:
            command.add_argument("--reason", required=True)
        if name == "stop":
            command.add_argument("--cancel-running", action="store_true")
        elif name == "retry-review":
            command.add_argument("iteration_id")
            command.add_argument("--expected-revision", type=int, required=True)
            command.add_argument("--proof-ref", required=True)
        elif name in {"cancel-operation", "retry-action"}:
            command.add_argument("operation_id")
            if name == "retry-action":
                command.add_argument(
                    "action",
                    choices=(
                        "prepare",
                        "inspection",
                        "collection",
                        "cancellation",
                        "settlement",
                    ),
                )
        elif name == "ack-event":
            command.add_argument("--consumer", required=True)
            command.add_argument("event_id", type=int)
    tasks = commands.add_parser("task", help="Submit protected human task changes")
    task_commands = tasks.add_subparsers(dest="task_command", required=True)
    for name in ("create", "edit", "cancel"):
        command = task_commands.add_parser(name)
        command.add_argument("study", type=Path)
        command.add_argument("id")
        command.add_argument("--request-id", default=None)
        if name in {"edit", "cancel"}:
            command.add_argument("--expected-revision", type=int, required=True)
        if name in {"create", "edit"}:
            command.add_argument("--specification", type=Path, required=True)
            command.add_argument(
                "--priority", type=int, required=name == "edit", default=100
            )
            command.add_argument("--depends-on", action="append", default=[])
    migrate = commands.add_parser(
        "migrate", help="Explicit legacy preview, staging and authority transfer"
    )
    migration_commands = migrate.add_subparsers(dest="migration_command", required=True)
    for name in ("preview", "prepare", "commit", "resolve", "bind-execute", "activate"):
        command = migration_commands.add_parser(name)
        command.add_argument("study", type=Path)
        if name == "preview":
            command.add_argument(
                "--output",
                type=Path,
                help="Save private exact control inventory to a new file",
            )
            command.add_argument(
                "--annex-spec",
                type=Path,
                help="JSON mapping of explicit controller document names to absolute paths",
            )
        elif name == "prepare":
            command.add_argument("--import-id", required=True)
            _add_config_arguments(command)
            for item in ("inventory", "translation", "fence", "deployment"):
                command.add_argument("--" + item, required=True, type=Path)
        else:
            command.add_argument("import_id")
            if name in {"resolve", "bind-execute", "activate"}:
                command.add_argument("--evidence", required=True, type=Path)
    return parser


def migrate(args):
    from .legacy_inventory import capture_control_annex, capture_legacy
    from .migration import (
        activate_import,
        bind_imported_execute,
        commit_import,
        prepare_import,
        resolve_import,
    )

    if args.migration_command == "preview":
        inventory = capture_legacy(args.study)
        if args.annex_spec is not None:
            inventory = capture_control_annex(inventory, _object(args.annex_spec))
        if args.output is not None:
            descriptor = os.open(
                args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            with os.fdopen(descriptor, "w") as handle:
                json.dump(inventory, handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
        return {
            key: inventory[key]
            for key in (
                "version",
                "source_root",
                "source_identity",
                "counters",
                "limits",
                "blockers",
                "history",
            )
        }
    if args.migration_command == "prepare":
        config = load_config(args.config)
        settings = config.get("runtime")
        validate_settings(settings)
        if "application" in settings or args.application.resolve().is_relative_to(
            args.study.resolve()
        ):
            raise ValueError(
                "Select an external immutable application with --application"
            )
        settings["application"] = prepare_application(
            args.application,
            load_config(args.application_config) if args.application_config else {},
            settings["worker_adapter"],
        )
        return prepare_import(
            args.study,
            _object(args.inventory),
            _object(args.translation),
            config,
            _object(args.fence),
            _object(args.deployment),
            import_id=args.import_id,
        )
    if args.migration_command == "commit":
        return commit_import(args.study, args.import_id)
    with StateStore(args.study).coordinator() as owner:
        if args.migration_command == "bind-execute":
            return bind_imported_execute(owner, args.import_id, _object(args.evidence))
        if args.migration_command == "resolve":
            return resolve_import(owner, args.import_id, _object(args.evidence))
        return activate_import(owner, args.import_id, _object(args.evidence))


def initialize(args):
    config = load_config(args.config)
    if not isinstance(config.get("goal"), str) or not config["goal"].strip():
        raise ValueError("Configuration requires a nonblank goal")
    if not isinstance(config.get("permissions"), dict):
        raise ValueError("Configuration requires explicit permissions")
    if config.get("global_evaluation") is not None:
        config["global_evaluation"] = validate_evaluation(config["global_evaluation"])
    settings = config.get("runtime")
    validate_settings(settings)
    if "application" in settings:
        raise ValueError(
            "Select the application with --application during initialization"
        )
    if args.application.resolve().is_relative_to(args.study.resolve()):
        raise ValueError(
            "Application module must be outside the Study control directory"
        )
    settings["application"] = prepare_application(
        args.application,
        load_config(args.application_config) if args.application_config else {},
        settings["worker_adapter"],
    )
    limits = {
        key: getattr(args, key)
        for key in ("max_iterations", "max_calls", "max_seconds", "admission_deadline")
        if hasattr(args, key)
    }
    if (
        "budget" not in config
        and not {"max_iterations", "max_calls", "max_seconds"} <= limits.keys()
    ):
        raise ValueError(
            "Supply config.budget or all three --max-iterations, --max-calls and --max-seconds"
        )
    store = StateStore.create(args.study, config=config, **limits)
    return {
        "study_id": store.snapshot()["study_id"],
        "state": "initialized",
        "callbacks_invoked": 0,
    }


def _command(args):
    if args.command == "task":
        payload = {"id": args.id}
        if args.task_command != "create":
            payload["expected_revision"] = args.expected_revision
        if args.task_command != "cancel":
            payload.update(
                specification=_object(args.specification),
                priority=args.priority,
                dependencies=args.depends_on,
            )
        return "task_" + args.task_command, payload
    if args.command == "stop":
        return "stop", {"reason": args.reason, "cancel_running": args.cancel_running}
    if args.command == "scout":
        return "scout", {}
    if args.command == "plan":
        return "planning", {"reason": args.reason}
    if args.command == "retry-review":
        return "retry_review", {
            "iteration_id": args.iteration_id,
            "expected_revision": args.expected_revision,
            "reason": args.reason,
            "proof_ref": args.proof_ref,
        }
    if args.command == "cancel-operation":
        return "cancel_operation", {
            "operation_id": args.operation_id,
            "reason": args.reason,
        }
    if args.command == "retry-action":
        return "retry_action", {
            "operation_id": args.operation_id,
            "action": args.action,
            "reason": args.reason,
        }
    if args.command == "ack-event":
        return "ack_event", {"consumer": args.consumer, "event_id": args.event_id}
    raise ValueError("Unknown runtime command")


def submit(args):
    kind, payload = _command(args)
    request_id = args.request_id or uuid.uuid4().hex
    result = submit_command(args.study, kind, payload, request_id)
    store = StateStore(args.study)
    try:
        with store.coordinator() as owner:
            results = ControlInbox(owner).drain()
            matching = [
                item
                for item in results
                if item["request_id"] == request_id
                and item.get("request_ref") == result.get("request_ref")
            ]
            if matching:
                return matching[-1]
            return receipt(args.study, request_id) or result
    except OwnershipError:
        return receipt(args.study, request_id) or result


def events(root, consumer):
    if not consumer.strip():
        raise ValueError("Consumer must be nonblank")
    store = StateStore(root)
    with store._connection(readonly=True) as conn:
        conn.execute("BEGIN")
        return [
            _decode(row)
            for row in conn.execute(
                "SELECT e.* FROM runtime_events e LEFT JOIN runtime_event_acks a ON a.event_id=e.id AND a.consumer=? WHERE a.event_id IS NULL ORDER BY e.id",
                (consumer,),
            )
        ]


def run(args):
    if not math.isfinite(args.poll_interval) or args.poll_interval <= 0:
        raise ValueError("Poll interval must be finite and positive")
    if args.max_ticks is not None and args.max_ticks < 1:
        raise ValueError("Tick limit must be positive")
    store = StateStore(args.study)
    with store.coordinator() as owner:
        inbox = ControlInbox(owner)
        inbox.drain()  # STOP and human edits do not require loading task code.
        application = load_application(owner)
        runner = ConcurrentCoordinator(
            owner, application.invoker, application.validators
        )
        ticks = 0
        while True:
            inbox.drain(admit_calls=True)
            result = runner.tick()
            ticks += 1
            if args.max_ticks is not None and ticks >= args.max_ticks:
                return {"state": "tick_limit", "ticks": ticks, **result}
            # Unknown sender retirement cannot be inferred from a clock. Stop
            # this foreground driver and retain its unresolved ledger for repair.
            unresolved = [
                row
                for row in runner.ledger.list()
                if not row["worker_retired"]
                and row["state"] == "unknown"
                and time.time() >= row["deadline"]
            ]
            if unresolved and not result["progress"]:
                return {"state": "needs_attention", "ticks": ticks, **result}
            if (
                not result["progress"]
                and not result["waiting"]
                and result["next_wake"] is None
            ):
                if not args.watch or result["stop"]["requested"]:
                    return {"state": "idle", "ticks": ticks, **result}
            if not result["progress"]:
                delay = args.poll_interval
                if result["next_wake"] is not None:
                    delay = min(delay, max(0, result["next_wake"] - time.time()))
                time.sleep(delay)


def main(argv=None):
    args = _parser().parse_args(argv)
    previous = None
    handles_signal = threading.current_thread() is threading.main_thread()
    if handles_signal:

        def interrupt(signum, frame):
            raise KeyboardInterrupt

        previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        if args.command == "init":
            result = initialize(args)
        elif args.command == "migrate":
            result = migrate(args)
        elif args.command == "run":
            result = run(args)
        elif args.command == "status":
            from .status import status_snapshot

            result = status_snapshot(args.study)
        elif args.command == "events":
            result = events(args.study, args.consumer)
        elif args.command == "request-status":
            result = receipt(args.study, args.request_id)
            if result is None:
                result = {
                    "request_id": args.request_id,
                    "status": "not_recorded",
                    "note": "May still be queued; no accepted receipt exists",
                }
        elif args.command == "dashboard":
            from .dashboard import serve

            serve(args.study, port=args.port)
            return 0
        else:
            result = submit(args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return (
            2
            if isinstance(result, dict)
            and (
                result.get("status") == "rejected"
                or result.get("state") == "needs_attention"
            )
            else 0
        )
    except (RuntimeError, OSError, ValueError, sqlite3.Error) as error:
        print(f"research-loop runtime: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "Runtime owner interrupted; recorded intents, holds and running callbacks are retained. Run again to reconcile; interruption does not clear or request STOP.",
            file=sys.stderr,
        )
        return 130
    finally:
        if handles_signal:
            signal.signal(signal.SIGTERM, previous)
