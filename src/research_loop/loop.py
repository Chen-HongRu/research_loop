"""CLI and lead-thread lifecycle; no host research scheduler or self-repair."""

import argparse
import asyncio
import json
import signal
import sys
from importlib.metadata import version
from pathlib import Path

from .codex import Native, configuration
from .instructions import INSTRUCTIONS, RESULT_SCHEMA
from .state import Journal, Knowledge, PublicationStopped, now, write_json, writer


def read_config(root):
    return json.loads((root / "config.json").read_text())


def resource(config, root):
    if config.get("resource"):
        from .slurm import Slurm

        ledger = Path(config["resource"]["ledger_path"]).resolve()
        lock = ledger.with_name(ledger.name + ".lock").resolve()
        if any(
            target.is_relative_to(path.resolve())
            for target in (ledger, lock)
            for path in (root / "work", root / "knowledge/draft")
        ):
            raise ValueError(
                "The GPU ledger and its lock must be outside agent-writable directories"
            )
        return Slurm(config["resource"], root)
    return None


def initialize(
    root, project, goal, *, resource_file=None, model="gpt-6.1-sol", effort="medium"
):
    project, goal = project.resolve(strict=True), goal.resolve(strict=True)
    if not project.is_dir() or not goal.is_file():
        raise ValueError("Provide an existing project directory and goal text file")
    config = {
        "version": 1,
        "project": str(project),
        "model": model,
        "effort": effort,
        "resource_file": str(resource_file.resolve(strict=True))
        if resource_file
        else None,
    }
    if resource_file:
        source = resource_file.resolve(strict=True)
        settings = json.loads(source.read_text())
        ledger = Path(settings["ledger_path"])
        settings["ledger_path"] = str(
            (ledger if ledger.is_absolute() else source.parent / ledger).resolve()
        )
        config["resource"] = settings
    resource(config, root)  # Validate configuration only: no dispatch or ledger writes.
    goal_text = goal.read_text()
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    (root / "work/tmp").mkdir(parents=True)
    (root / "goal.md").write_text(goal_text)
    write_json(root / "config.json", config)
    Knowledge(root).prepare()
    write_json(
        root / "status.json",
        {"status": "initialized", "created_at": now(), "thread_id": None},
    )
    return {"root": str(root), **config}


def inspect(root):
    config = read_config(root)
    adapter = resource(config, root)
    return {
        "config": config,
        "instructions": INSTRUCTIONS,
        "output_schema": RESULT_SCHEMA,
        "input": {
            "goal_file": str(root / "goal.md"),
            "project_root": config["project"],
            "work_root": str(root / "work"),
            "knowledge_draft": str(root / "knowledge/draft"),
            "published_knowledge_root": str(root / "knowledge/current"),
            "published_knowledge_index_file": str(root / "knowledge/current/index.md"),
            "resource": adapter.status() if adapter else None,
        },
        "tools": [tool.descriptor() for tool in adapter.tools()] if adapter else [],
        "permissions": {
            "read": [config["project"], str(root)],
            "write": [str(root / "work"), str(root / "knowledge/draft")],
            "shell_network": False,
            "external_tools": "lead-only",
        },
    }


def iteration_directory(root, number):
    path = root / "iterations" / f"{number:06d}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def record_status(root, state):
    write_json(iteration_directory(root, state["iteration"]) / "status.json", state)
    write_json(root / "status.json", state)


async def run_iteration(root, state, previous_result, journal, native_factory):
    native, adapter, watch = None, None, None
    cleanup_started = False

    async def cleanup():
        nonlocal cleanup_started
        if cleanup_started:
            return
        cleanup_started = True
        try:
            if native:
                await native.stop()
                await native.settle()
        finally:
            try:
                if native:
                    await native.close()
            finally:
                if adapter:
                    state["resource"] = await adapter.close()

    async def observe_stop():
        while True:
            if (root / "STOP").exists():
                await native.stop()
                return
            await asyncio.sleep(0.25)

    def started(thread_id):
        state.update(thread_id=thread_id, status="running", started_at=now())
        record_status(root, state)

    try:
        call = inspect(root)
        config = call["config"]
        adapter = resource(config, root)
        knowledge = Knowledge(root)
        baseline = knowledge.prepare()
        native_config, effective = configuration(
            root,
            Path(config["project"]),
            [root / "work", knowledge.draft],
            config["model"],
            config["effort"],
        )
        call["effective_native_permissions"] = effective
        call["input"].update(
            iteration=state["iteration"], previous_iteration_result=previous_result
        )
        write_json(root / "call.json", call)
        native = native_factory(
            native_config, journal, adapter.tools() if adapter else []
        )
        await native.open()
        watch = asyncio.create_task(observe_stop())
        await native.verify(config["model"], config["effort"])
        if (root / "STOP").exists():
            state.update(status="stopped")
            return state
        result = await native.run(
            INSTRUCTIONS,
            call["input"],
            RESULT_SCHEMA,
            model=config["model"],
            effort=config["effort"],
            thread_id=state.get("thread_id"),
            on_started=started,
        )
        if result.get("study_action") not in {"continue", "stop"} or not isinstance(
            result.get("stop_reason"), str
        ):
            raise ValueError(
                "Result requires explicit study_action continue/stop and string stop_reason"
            )
        state["result"] = result
        write_json(
            iteration_directory(root, state["iteration"]) / "result.json", result
        )
        write_json(root / "result.json", result)
        # Every native process and external effect settles before this publication.
        await cleanup()
        if (root / "STOP").exists():
            state.update(status="stopped")
        elif adapter and (
            any(
                entry["status"] != "TERMINAL"
                for entry in state["resource"]["reservations"]
            )
            or state["resource"].get("cleanup_errors")
        ):
            state.update(status="needs-reconciliation")
        else:
            publication = {
                **state,
                "status": "completed"
                if result["study_action"] == "stop"
                else "iteration-completed",
                "ended_at": now(),
            }
            if result["study_action"] == "continue":
                publication["next_iteration"] = state["iteration"] + 1
            published = await asyncio.to_thread(
                knowledge.publish, baseline, publication=publication
            )
            state.update(
                status="completed"
                if result["study_action"] == "stop"
                else "iteration-completed",
                knowledge_version=published,
            )
            if result["study_action"] == "continue":
                state["next_iteration"] = state["iteration"] + 1
    except PublicationStopped:
        state.update(status="stopped")
    except BaseException as error:
        state.update(
            status="stopped" if (root / "STOP").exists() else "failed",
            error={"type": type(error).__name__, "message": str(error)},
        )
        journal.emit("run.error", iteration=state["iteration"], error=state["error"])
        raise
    finally:
        if watch:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)
        try:
            await cleanup()
        except Exception as cleanup_error:
            state["cleanup_error"] = str(cleanup_error)
            journal.emit(
                "cleanup.error",
                iteration=state["iteration"],
                message=str(cleanup_error),
            )
        finally:
            state["ended_at"] = now()
            record_status(root, state)
            journal.emit(
                "iteration.finished",
                iteration=state["iteration"],
                status=state["status"],
                thread_id=state.get("thread_id"),
            )
    return state


async def run(root, *, continue_run=False, native_factory=Native):
    # One writer owns the entire Study, including publication and round transitions.
    with writer(root):
        state = json.loads((root / "status.json").read_text())
        published = Knowledge(root).published_state()
        if (
            published
            and published["iteration"] >= state.get("iteration", 0)
            and state.get("knowledge_version") != published["knowledge_version"]
        ):
            # The active pointer proves publication even if latest status was not written.
            state = published
            write_json(root / "result.json", state["result"])
            record_status(root, state)
        if not continue_run and state["status"] == "completed":
            return state
        if (root / "STOP").exists():
            if not continue_run:
                raise RuntimeError(
                    "STOP is recorded; use run --continue for an explicit continuation"
                )
            (root / "STOP").unlink()
        archived_legacy = "iteration" not in state and (root / "result.json").exists()
        if archived_legacy:
            # Preserve pre-upgrade records exactly before replacing either latest file.
            archive = iteration_directory(root, 1)
            for name in ("status.json", "result.json"):
                if not (archive / name).exists():
                    (archive / name).write_bytes((root / name).read_bytes())
            state["iteration"] = 1
        number = state.get("next_iteration")
        if number is None:
            number = state.get("iteration", 0)
            if archived_legacy or state["status"] in {
                "completed",
                "iteration-completed",
            }:
                number += 1
        number = max(1, number)
        journal = Journal(root / "events.jsonl")
        active_loop = asyncio.get_running_loop()

        def request_stop():
            (root / "STOP").write_text("operator stop\n")

        for sig in (signal.SIGINT, signal.SIGTERM):
            active_loop.add_signal_handler(sig, request_stop)
        try:
            while True:
                if (root / "STOP").exists():
                    state.setdefault("iteration", number)
                    state.update(status="stopped", next_iteration=number)
                    # No iteration was admitted here. Preserve the completed archive.
                    write_json(root / "status.json", state)
                    return state
                prior = root / "iterations" / f"{number - 1:06d}" / "result.json"
                previous_result = (
                    json.loads(prior.read_text()) if prior.exists() else None
                )
                for key in (
                    "result",
                    "resource",
                    "cleanup_error",
                    "started_at",
                    "ended_at",
                    "next_iteration",
                ):
                    state.pop(key, None)
                state.update(iteration=number, status="starting", error=None)
                record_status(root, state)
                await run_iteration(
                    root, state, previous_result, journal, native_factory
                )
                if state["status"] != "iteration-completed":
                    return state
                # The snapshot above is visible before another native turn is admitted.
                number += 1
        finally:
            journal.emit(
                "run.finished",
                status=state["status"],
                iteration=state.get("iteration"),
                thread_id=state.get("thread_id"),
            )
            journal.close()
            for sig in (signal.SIGINT, signal.SIGTERM):
                active_loop.remove_signal_handler(sig)


def main():
    if sys.argv[1:2] == ["metrics"]:
        from .metrics import main as metrics_main

        return metrics_main(sys.argv[2:])
    parser = argparse.ArgumentParser(
        description="Codex-led research with native delegation"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "metrics", help="Continuously collect or compare local efficiency measurements"
    )
    commands.add_parser(
        "doctor", help="Check local runtime metadata without a model call"
    )
    init = commands.add_parser(
        "init", help="Create a new private run, without a model call"
    )
    init.add_argument("root", type=Path)
    init.add_argument("--project", type=Path, required=True)
    init.add_argument("--goal", type=Path, required=True)
    init.add_argument("--resource", type=Path)
    init.add_argument("--model", default="gpt-6.1-sol")
    init.add_argument("--effort", default="medium")
    for name in ("inspect", "run", "status", "stop", "efficiency"):
        command = commands.add_parser(name)
        command.add_argument("root", type=Path)
        if name == "run":
            command.add_argument("--continue", action="store_true", dest="continue_run")
        elif name == "efficiency":
            command.add_argument(
                "--since", help="Inclusive ISO 8601 time with timezone"
            )
            command.add_argument(
                "--until", help="Exclusive ISO 8601 time with timezone"
            )
            command.add_argument("--sessions-dir", type=Path)
            command.add_argument("--ledger-path", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            from codex_cli_bin import bundled_codex_path

            result = {
                "python": sys.version.split()[0],
                "codex_sdk": version("openai-codex"),
                "native_binary": str(bundled_codex_path()),
                "subscription_login_present": (
                    Path.home() / ".codex/auth.json"
                ).is_file(),
                "model_call": False,
            }
        else:
            root = args.root.resolve()
            if args.command == "init":
                result = initialize(
                    root,
                    args.project,
                    args.goal,
                    resource_file=args.resource,
                    model=args.model,
                    effort=args.effort,
                )
            elif args.command == "inspect":
                result = inspect(root)
            elif args.command == "efficiency":
                from .efficiency import report

                result = report(
                    root,
                    since=args.since,
                    until=args.until,
                    sessions_dir=args.sessions_dir,
                    ledger_path=args.ledger_path,
                )
            elif args.command == "run":
                result = asyncio.run(run(root, continue_run=args.continue_run))
            elif args.command == "status":
                config = read_config(root)
                result = json.loads((root / "status.json").read_text())
                adapter = resource(config, root)
                if adapter:
                    result["resource"] = adapter.status()
            else:
                read_config(root)
                (root / "STOP").write_text("operator stop\n")
                result = {
                    "stop_requested": True,
                    "physical_job_termination": "inspect original resource receipt",
                }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "run" and result.get("status") == "needs-reconciliation":
            raise SystemExit(1)
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
