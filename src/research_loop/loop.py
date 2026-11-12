"""CLI and lead-thread lifecycle; no host research scheduler or self-repair."""

import argparse
import asyncio
import json
import os
import re
import signal
import sys
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlparse

from .codex import Native, configuration
from . import environment, human_input, publication
from .instructions import RESULT_SCHEMA, instructions
from .state import (
    Journal,
    Knowledge,
    PublicationStopped,
    iteration_directory,
    now,
    record_status,
    write_json,
    writer,
)


def read_config(root):
    return json.loads((root / "config.json").read_text())


DEFAULT_MODEL = "gpt-6.1-sol"
DEFAULT_MODEL_CONTEXT_WINDOW = 131072


def provider_config(args):
    """Resolve an optional OpenAI-compatible provider; secrets stay in the environment."""
    if not any((args.provider, args.provider_base_url, args.api_key_env)):
        if (
            args.model_catalog
            or args.model_context_window != DEFAULT_MODEL_CONTEXT_WINDOW
        ):
            raise ValueError(
                "--model-catalog and --model-context-window require a provider"
            )
        return None
    if not all((args.provider, args.provider_base_url, args.api_key_env)):
        raise ValueError(
            "--provider, --provider-base-url and --api-key-env are required together"
        )
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.provider):
        raise ValueError("Provider names use letters, digits, underscore and hyphen")
    url = urlparse(args.provider_base_url)
    if url.scheme not in {"http", "https"} or not url.netloc:
        raise ValueError("--provider-base-url requires an http(s) URL")
    if not args.model:
        raise ValueError("--model is required for an OpenAI-compatible provider")
    if not os.environ.get(args.api_key_env):
        # Report only the name: the value is never recorded or printed.
        raise ValueError(f"Environment variable {args.api_key_env} is unset")
    if args.model_context_window < 1:
        raise ValueError("--model-context-window must be positive")
    catalog = None
    if args.model_catalog:
        path = args.model_catalog.resolve(strict=True)
        document = json.loads(path.read_text())
        if args.model not in [
            entry.get("slug") for entry in document.get("models", [])
        ]:
            raise ValueError("The model catalog does not declare the configured model")
        catalog = str(path)
    return {
        "name": args.provider,
        "base_url": args.provider_base_url.rstrip("/"),
        "api_key_env": args.api_key_env,
        "model_catalog": catalog,
        "context_window": args.model_context_window,
    }


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
    root,
    project,
    goal,
    *,
    resource_file=None,
    environment_file=None,
    model="gpt-6.1-sol",
    effort="medium",
    provider=None,
    delegation=None,
    coordinator="router",
    groups_file=None,
    recover_exp=None,
    input_refs=(),
):
    project, goal = project.resolve(strict=True), goal.resolve(strict=True)
    if not project.is_dir() or not goal.is_file():
        raise ValueError("Provide an existing project directory and goal text file")
    if delegation is None:
        delegation = "host" if provider else "native"
    if delegation not in {"native", "host"}:
        raise ValueError("Delegation mode must be native or host")
    if coordinator not in {"lead", "router"}:
        raise ValueError("Coordinator must be lead or router")
    if recover_exp is not None and (
        groups_file is not None or resource_file is not None
    ):
        raise ValueError(
            "Recovery uses the reference publication Group without resource execution"
        )
    admitted = [Path(ref).resolve(strict=True) for ref in input_refs]
    if any(not ref.is_file() for ref in admitted):
        raise ValueError("Initial artifact references must be existing files")
    definitions = []
    if coordinator == "router" or recover_exp is not None:
        if groups_file is None:
            from .workflow import definitions as reference_workflow

            definitions = reference_workflow(
                resource=resource_file is not None, recovery=recover_exp is not None
            )
        else:
            from .group_router import catalog

            definitions = catalog(groups_file)
    elif groups_file is not None:
        raise ValueError("--groups requires --coordinator router")
    result_authors = [
        d.id for d in definitions if d.settings.get("produces_study_result")
    ]
    if len(result_authors) > 1:
        raise ValueError("Only one Group may author the Study result")
    config = {
        "version": 1,
        "project": str(project),
        "model": model,
        "effort": effort,
        "provider": provider,
        "delegation": delegation,
        "coordinator": coordinator,
        "input_refs": [str(ref) for ref in admitted],
        "environment": environment.read_config(environment_file)
        if environment_file
        else None,
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
    if recover_exp is not None:
        from .group_programs.exp_recovery import build_recovery_bundle
        from .recovery import evidence_packet

        manifest_path, manifest = build_recovery_bundle(
            recover_exp, root / "inputs/exp-recovery"
        )
        evidence_packet(manifest_path)
        config["recovery_bundle"] = str(manifest_path)
        config["input_refs"] = list(
            dict.fromkeys(
                [
                    *config["input_refs"],
                    str(manifest_path),
                    manifest["evaluation_ref"],
                    *[entry["ref"] for entry in manifest["artifacts"]],
                ]
            )
        )
    if definitions:
        import shutil
        from .group_router import definition_record

        programs = root / "groups/programs"
        programs.mkdir(parents=True)
        records = []
        for definition in definitions:
            if (
                definition.program.suffix != ".py"
                or definition.program.stat().st_size > 1024 * 1024
            ):
                raise ValueError("Group program must be a small Python source file")
            source = programs / (definition.id + ".py")
            shutil.copyfile(definition.program, source)
            record = definition_record(definition)
            record["program"] = str(source)
            records.append(record)
        config["groups"] = records
        if result_authors:
            config["study_result_group"] = result_authors[0]
        if groups_file is None:
            config["workflow"] = "research-v1"
    write_json(root / "config.json", config)
    Knowledge(root).prepare()
    write_json(
        root / "status.json",
        {"status": "initialized", "created_at": now(), "thread_id": None},
    )
    return {"root": str(root), **config}


def delegation_mode(config, state=None):
    mode = config.get("delegation")
    if mode in {"native", "host"}:
        return mode
    # Pre-upgrade provider runs: native only after they created their lead thread.
    return (
        "host"
        if config.get("provider") and not (state or {}).get("thread_id")
        else "native"
    )


def inspect(root):
    config = read_config(root)
    adapter = resource(config, root)
    status = root / "status.json"
    state = json.loads(status.read_text()) if status.exists() else {}
    mode = delegation_mode(config, state)
    resource_status = adapter.status() if adapter else None
    tools = [tool.descriptor() for tool in adapter.tools()] if adapter else []
    snapshot = environment.observe(
        config.get("environment"),
        resource_config=config.get("resource"),
        resource_status=resource_status,
        tools=tools,
    )
    policy = instructions(mode, snapshot)
    if config.get("coordinator") == "router":
        from .group_router import POLICY

        policy = POLICY
    result = {
        "config": config,
        "delegation": mode,
        "instructions": policy,
        "output_schema": RESULT_SCHEMA,
        "input": {
            "goal_file": str(root / "goal.md"),
            "project_root": config["project"],
            "work_root": str(root / "work"),
            "knowledge_draft": str(root / "knowledge/draft"),
            "published_knowledge_root": str(root / "knowledge/current"),
            "published_knowledge_index_file": str(root / "knowledge/current/index.md"),
            "resource": resource_status,
            "execution_environment": snapshot,
            "human_request_file": str(root / "inputs/human.md"),
        },
        "tools": [] if config.get("coordinator") == "router" else tools,
        "permissions": {
            "sandbox_mode": "workspace-write",
            "read": [":root"],
            "write": (
                []
                if config.get("coordinator") == "router"
                else [str(root / "work"), str(root / "knowledge/draft")]
            )
            + [":slash_tmp", ":tmpdir"],
            "group_writable_roots": "per-Run work and native scratch"
            if config.get("coordinator") == "router"
            else None,
            "shell_network": False,
            "external_tools": "runtime-only"
            if config.get("coordinator") == "router"
            else "lead-only",
        },
    }
    if config.get("recovery_bundle"):
        from .recovery import evidence_packet

        result["input"]["shared_input"] = evidence_packet(config["recovery_bundle"])
        result["input"]["goal_text"] = (root / "goal.md").read_text()
        result["input"]["input_refs"] = config["input_refs"]
    return result


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
        request = await asyncio.to_thread(human_input.admit, root)
        state["human_request"] = request
        if request:
            write_json(
                iteration_directory(root, state["iteration"])
                / f"human-request-{request['id']}.json",
                request,
            )
        call["input"]["human_request"] = request
        call["output_schema"] = human_input.result_schema(RESULT_SCHEMA, request)
        config = call["config"]
        mode = config.get("delegation")
        if mode not in {"native", "host"}:
            # Older runs default to native unless they created a provider thread.
            mode = (
                "host"
                if config.get("provider") and not state.get("thread_id")
                else "native"
            )
        call["delegation"] = mode
        call["instructions"] = instructions(
            mode, call["input"]["execution_environment"]
        )
        adapter = resource(config, root)
        knowledge = Knowledge(root)
        baseline = knowledge.prepare()
        if config.get("recovery_bundle"):
            from .recovery import stage_evidence

            stage_evidence(root, config["recovery_bundle"])
        if config.get("coordinator") == "router" or config.get("recovery_bundle"):
            from .group_router import GroupCoordinator, POLICY

            call["instructions"] = POLICY
            call["delegation"] = "none"
            call["effective_native_permissions"] = {
                "coordinator": config["coordinator"],
                "router_tools": False,
                "resource_dispatch": "disabled"
                if config.get("recovery_bundle")
                else "runtime-only",
            }
            coordinator_type = GroupCoordinator
            if config.get("coordinator") == "lead":
                from .recovery import RecoveryLeadCoordinator

                coordinator_type = RecoveryLeadCoordinator
            native = coordinator_type(
                root, config, journal, adapter, native_factory=native_factory
            )
        else:
            native_config, effective = configuration(
                root,
                Path(config["project"]),
                [root / "work", knowledge.draft],
                config["model"],
                config["effort"],
                provider=config.get("provider"),
                delegation=mode,
                execution_environment=call["input"]["execution_environment"],
            )
            effective["delegation"] = mode
            call["effective_native_permissions"] = effective
            native = native_factory(
                native_config, journal, adapter.tools() if adapter else []
            )
        call["input"].update(
            iteration=state["iteration"], previous_iteration_result=previous_result
        )
        write_json(root / "call.json", call)
        await native.open()
        watch = asyncio.create_task(observe_stop())
        await native.verify(
            config["model"], config["effort"], provider=config.get("provider")
        )
        if (root / "STOP").exists():
            state.update(status="stopped")
            return state
        result = await native.run(
            call["instructions"],
            call["input"],
            call["output_schema"],
            model=config["model"],
            effort=config["effort"],
            thread_id=state.get("thread_id"),
            on_started=started,
            delegation=mode,
            roles_path=root / "roles.json",
            environment=call["input"]["execution_environment"],
        )
        if result.get("study_action") not in {"continue", "stop"} or not isinstance(
            result.get("stop_reason"), str
        ):
            raise ValueError(
                "Result requires explicit study_action continue/stop and string stop_reason"
            )
        state["result"] = result
        human_input.validate_result(request, result)
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
            await publication.commit(root, state, baseline)
    except PublicationStopped:
        state.update(status="stopped")
    except BaseException as error:
        state.update(
            status="stopped" if (root / "STOP").exists() else "failed",
            error={"type": type(error).__name__, "message": str(error)},
        )
        record_status(root, state)
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
        state = await publication.recover(root, state)
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
                    "human_request",
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
    init.add_argument(
        "--environment", type=Path, help="Fixed preprovisioned Python environment JSON"
    )
    init.add_argument("--model")
    init.add_argument("--effort", default="medium")
    init.add_argument("--coordinator", choices=("lead", "router"), default="router")
    init.add_argument(
        "--groups", type=Path, help="Versioned Group catalog for Router coordination"
    )
    init.add_argument(
        "--recover-exp",
        type=Path,
        help="Reuse a settled Exp Run for recovery/publication only",
    )
    init.add_argument(
        "--input-ref",
        type=Path,
        action="append",
        default=[],
        help="Admit an existing artifact by reference",
    )
    init.add_argument("--provider", help="OpenAI-compatible provider name")
    init.add_argument(
        "--provider-base-url", help="Provider Responses API root, for example /v1"
    )
    init.add_argument(
        "--api-key-env",
        help="Name of the environment variable holding the provider API key",
    )
    init.add_argument(
        "--model-catalog",
        type=Path,
        help="Existing Codex model catalog JSON for the provider model",
    )
    init.add_argument(
        "--model-context-window",
        type=int,
        default=DEFAULT_MODEL_CONTEXT_WINDOW,
        help="Context window declared for a generated provider model catalog",
    )
    init.add_argument(
        "--delegation",
        choices=("native", "host"),
        help="Role delegation backend; host is selected for provider runs",
    )
    experiment = commands.add_parser(
        "experiment",
        help="Inject a user-authorized priority experiment without a model call",
    )
    experiment.add_argument("root", type=Path)
    source = experiment.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="UTF-8 request file")
    source.add_argument(
        "--stdin", action="store_true", help="Read request text from stdin"
    )
    edit_experiment = commands.add_parser(
        "edit-experiment",
        help="Edit the human experiment file under its consumption lock",
    )
    edit_experiment.add_argument("root", type=Path)
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
                    environment_file=args.environment,
                    model=args.model or DEFAULT_MODEL,
                    effort=args.effort,
                    provider=provider_config(args),
                    delegation=args.delegation,
                    coordinator=args.coordinator,
                    groups_file=args.groups,
                    recover_exp=args.recover_exp,
                    input_refs=args.input_ref,
                )
            elif args.command == "inspect":
                result = inspect(root)
            elif args.command in {"experiment", "edit-experiment"}:
                read_config(root)
                if args.command == "edit-experiment":
                    result = human_input.edit(root)
                else:
                    text = (
                        args.file.read_text(encoding="utf-8")
                        if args.file
                        else sys.stdin.read()
                    )
                    result = human_input.submit(root, text)
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
