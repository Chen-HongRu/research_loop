"""Serial specialist tasks, deterministic execution, and explicit stage accounting."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sys
from time import monotonic
from uuid import uuid4

from research_loop.codex import Native, configuration
from research_loop.state import Journal, write_json

from .accounting import summarize_usage
from .case import build_workspace, check, load_case, validate_output_directory
from .policy import load_policy

STAGES = ("plan", "prepare", "execute", "evaluate")
MODEL = "gpt-6.1-terra"
EFFORT = "medium"


def utc():
    return datetime.now(timezone.utc).isoformat()


def schema(properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


TEXT = {"type": "string"}
STRINGS = {"type": "array", "items": TEXT}
SCHEMAS = {
    "plan": schema(
        {
            key: TEXT
            for key in (
                "hypothesis",
                "control",
                "change",
                "evaluation_rule",
                "outcomes",
            )
        }
    ),
    "prepare": schema({"command": STRINGS, "smoke": TEXT, "artifacts": STRINGS}),
    "evaluate": schema(
        {
            "disposition": {
                "type": "string",
                "enum": ["supported", "contradicted", "insufficient_evidence"],
            },
            "metrics": schema(
                {
                    key: {"type": "number"}
                    for key in ("control_mse", "candidate_mse", "delta_mse")
                }
            ),
            "limitations": TEXT,
            "evidence": STRINGS,
        }
    ),
}
GUARD = """Execute only the supplied stage of this predefined experiment. The host owns
execution, scoring, schemas, accounting and permissions. Never change the case,
input data, hidden checker, stage order or measurement. No nested delegation.
"""


def inline_context(workspace, stage):
    """Only small host-owned control records, never datasets or predictions."""
    names = {
        "plan": (),
        "prepare": ("plan",),
        "evaluate": ("plan", "prepare", "execute"),
    }[stage]
    context = {}
    for name in names:
        path = workspace / ".cycle" / f"{name}.json"
        if path.stat().st_size <= 16 * 1024:
            context[name] = json.loads(path.read_text())
        else:
            context[name] = {"path": str(path), "inline": False}
    return context


def output_schema(stage, case):
    if stage == "prepare":
        return {
            **SCHEMAS[stage],
            "properties": {
                **SCHEMAS[stage]["properties"],
                "artifacts": {
                    "type": "array",
                    "minItems": 1,
                    "description": "Existing prepared source only; exclude future experiment results.",
                    "items": {"type": "string", "enum": [case["command"][1]]},
                },
            },
        }
    if stage != "evaluate":
        return SCHEMAS[stage]
    return {
        **SCHEMAS[stage],
        "properties": {
            **SCHEMAS[stage]["properties"],
            "metrics": schema(
                {
                    name: {"type": "number"}
                    for name in case.get(
                        "metric_names", ("control_mse", "candidate_mse", "delta_mse")
                    )
                }
            ),
            "evidence": {
                "type": "array",
                "description": "Exact workspace-relative filenames only; include result_file. Put explanations in limitations.",
                "items": {
                    "type": "string",
                    "enum": list(
                        dict.fromkeys(
                            [
                                case["result_file"],
                                ".cycle/plan.json",
                                ".cycle/prepare.json",
                                ".cycle/execute.json",
                                "inputs.json",
                                case["command"][1],
                            ]
                        )
                    ),
                },
            },
        },
    }


def launch_config(root, workspace, *, writable, model, effort, protected=()):
    (root / "work/tmp").mkdir(parents=True, exist_ok=True)
    config, effective = configuration(
        root, workspace, writable, model, effort, delegation="host"
    )
    argv = list(config.launch_args_override)
    argv = [item.replace('web_search="live"', 'web_search="disabled"') for item in argv]
    paths = dict(effective["filesystem"])
    for name in ("inputs", "data", "inputs.json", ".cycle"):
        paths[str((workspace / name).resolve())] = "read"
    for path in protected:
        paths[str(Path(path).resolve())] = "read"
    from research_loop.codex import _toml

    argv[-3:-3] = [
        "--config",
        "permissions.research.filesystem=" + _toml(paths),
        "--config",
        "project_doc_max_bytes=0",
    ]
    effective.update(
        filesystem=paths,
        web_search=False,
        native_multi_agent=False,
        project_doc_max_bytes=0,
    )
    return replace(config, launch_args_override=tuple(argv)), effective


class NativeAgent:
    """One fresh runtime/context per task, without installing delegation tools."""

    def __init__(self, policy=None):
        self.policy = policy or load_policy()

    async def __call__(self, stage, case, workspace, root, identity, model, effort):
        writable = [root / "work"]
        if stage == "prepare":
            writable.append(workspace)
        config, effective = launch_config(
            root, workspace, writable=writable, model=model, effort=effort
        )
        write_json(root / "effective.json", effective)
        journal = Journal(root / "events.jsonl")
        native = Native(config, journal)
        try:
            async with asyncio.timeout(case["limits"]["stage_seconds"]):
                await native.open()
                await native.verify(model, effort)
                payload = {
                    "case_id": case["case_id"],
                    "stage": stage,
                    "task": case["task_text"],
                    "workspace": str(workspace),
                    "artifacts": str(workspace / ".cycle"),
                    "command": [
                        sys.executable if arg == "{python}" else arg
                        for arg in case["command"]
                    ],
                    "result_file": case["result_file"],
                }
                if self.policy["stages"][stage]["context_mode"] == "inline":
                    payload["control_artifacts"] = inline_context(workspace, stage)
                result = await native.run(
                    GUARD
                    + self.policy["common"]
                    + self.policy["stages"][stage]["instructions"],
                    payload,
                    output_schema(stage, case),
                    model=model,
                    effort=effort,
                    on_started=lambda tid: identity(tid, None),
                )
                if native.children:
                    raise ValueError("Specialist attempted nested delegation")
                await native.settle()
                return result
        finally:
            primary_failure = sys.exc_info()[0] is not None
            cleanup_errors = []
            for operation in (native.stop, native.settle, native.close):
                try:
                    async with asyncio.timeout(35):
                        await operation()
                except Exception as error:
                    cleanup_errors.append(
                        f"{operation.__name__}: {type(error).__name__}: {error}"
                    )
            try:
                for message in cleanup_errors:
                    journal.emit("benchmark.cleanup.error", message=message)
            finally:
                journal.close()
            # Settlement emits authoritative history paths even on failed turns.
            for line in (root / "events.jsonl").read_text().splitlines():
                row = json.loads(line)
                if (
                    row.get("event") == "codex.history.reference"
                    and row.get("thread_id") == native.thread_id
                ):
                    identity(native.thread_id, row.get("path"))
            if cleanup_errors and not primary_failure:
                raise RuntimeError("; ".join(cleanup_errors))


async def execute(case, workspace, root, command, model, effort):
    """Run the candidate inside the pinned CLI filesystem/network sandbox."""
    config, effective = launch_config(
        root,
        workspace,
        writable=[root / "work", workspace],
        model=model,
        effort=effort,
        protected=[workspace / case["command"][1]],
    )
    argv = list(config.launch_args_override[:-3])
    argv += [
        "sandbox",
        "--permission-profile",
        "research",
        "--cd",
        str(workspace),
        "--",
        *command,
    ]
    write_json(root / "effective.json", effective)
    with (root / "command.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workspace,
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            async with asyncio.timeout(case["limits"]["execute_seconds"]):
                code = await process.wait()
        except BaseException:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            raise
    return {"exit_code": code, "log_path": str(root / "command.log")}


def verify_delivery(stage, delivery, workspace, command, result_file):
    # The native schema is also checked locally for injected clients and failures.
    from pydantic import TypeAdapter

    if not isinstance(delivery, dict) or set(delivery) != set(
        SCHEMAS[stage]["properties"]
    ):
        raise ValueError(f"Invalid {stage} delivery fields")
    if stage == "prepare":
        if (
            not isinstance(delivery["command"], list)
            or [sys.executable if a == "{python}" else a for a in delivery["command"]]
            != command
        ):
            raise ValueError("Prepared command differs from frozen case command")
        if not isinstance(delivery["artifacts"], list) or not delivery["artifacts"]:
            raise ValueError("Preparation requires artifact references")
        for item in delivery["artifacts"]:
            path = (workspace / item).resolve()
            if not path.is_relative_to(workspace.resolve()) or not path.is_file():
                raise ValueError("Prepared artifact missing or outside workspace")
        if (workspace / result_file).exists():
            raise ValueError("Substantive output exists before Execute")
    for key, spec in SCHEMAS[stage]["properties"].items():
        if spec.get("type") == "string" and (
            not isinstance(delivery[key], str) or not delivery[key].strip()
        ):
            raise ValueError(f"Missing {stage} {key}")
    if stage == "evaluate":
        TypeAdapter(dict[str, float]).validate_python(delivery["metrics"])


async def run(
    case_path,
    output,
    *,
    agent=None,
    executor=None,
    model=MODEL,
    effort=EFFORT,
    measurement_kind="native",
    strategy=None,
    case_loader=None,
    case_checker=None,
):
    begin = monotonic()
    policy = load_policy(strategy)
    case = (case_loader or load_case)(case_path)
    output = Path(output).resolve()
    validate_output_directory(case, output)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    workspace = output / "workspace"
    build_workspace(case, workspace)
    dossier = workspace / ".cycle"
    dossier.mkdir()
    command = [sys.executable if arg == "{python}" else arg for arg in case["command"]]
    write_json(output / "strategy.json", policy)
    agent = agent or NativeAgent(policy)
    executor = executor or execute
    roster = []
    records = []
    failure = None
    evaluation = None
    attempt = str(uuid4())
    for stage in STAGES:
        if failure:
            records.append(
                {
                    "stage": stage,
                    "status": "not-run",
                    "wall_seconds": 0,
                    "started_at": None,
                    "ended_at": None,
                }
            )
            continue
        root = output / stage
        root.mkdir()
        stamp = monotonic()
        record = {
            "stage": stage,
            "status": "running",
            "started_at": utc(),
            "wall_seconds": None,
        }
        records.append(record)
        row = None
        if stage != "execute":
            row = {
                "stage": stage,
                "thread_id": None,
                "history_path": None,
                "invoked": True,
            }
            roster.append(row)
        write_json(
            output / "tasks.json",
            {"case_id": case["case_id"], "attempt_id": attempt, "tasks": roster},
        )

        def identity(tid, path, row=row):
            row.update(thread_id=tid, history_path=path or row["history_path"])
            write_json(
                output / "tasks.json",
                {"case_id": case["case_id"], "attempt_id": attempt, "tasks": roster},
            )

        try:
            if stage == "execute":
                script = workspace / case["command"][1]
                metadata = script.stat()
                execution_identity = {
                    "command": command,
                    "candidate_path": str(script),
                    "candidate_size": metadata.st_size,
                    "candidate_mtime_ns": metadata.st_mtime_ns,
                    "case_id": case["case_id"],
                    "source": case["source"],
                }
                write_json(root / "dispatch.json", execution_identity)
                delivery = await executor(case, workspace, root, command, model, effort)
                delivery.update(execution_identity)
                write_json(root / "receipt.json", delivery)
                if delivery["exit_code"] != 0:
                    raise ValueError(f"Experiment exited {delivery['exit_code']}")
                if not (workspace / case["result_file"]).is_file():
                    raise ValueError("Experiment produced no result")
            else:
                delivery = await agent(
                    stage, case, workspace, root, identity, model, effort
                )
                verify_delivery(
                    stage, delivery, workspace, command, case["result_file"]
                )
                if stage == "evaluate":
                    evaluation = delivery
            write_json(dossier / f"{stage}.json", delivery)
            record.update(status="completed", artifact=str(dossier / f"{stage}.json"))
        except Exception as error:
            failure = f"{type(error).__name__}: {error}"
            record.update(status="failed", error=failure)
        finally:
            record.update(ended_at=utc(), wall_seconds=monotonic() - stamp)
            write_json(
                output / "tasks.json",
                {"case_id": case["case_id"], "attempt_id": attempt, "tasks": roster},
            )
            write_json(output / "stages.json", records)
    try:
        correctness = (
            (case_checker or check)(case, workspace, evaluation)
            if not failure
            else {"status": "not-evaluable", "checks": [], "limitations": [failure]}
        )
    except (ValueError, OSError) as error:
        correctness = {
            "status": "not-evaluable",
            "checks": [],
            "limitations": [str(error)],
        }
    usage = summarize_usage(roster)
    for record in records:
        record["tokens"] = usage["by_stage"][record["stage"]]
    wall = monotonic() - begin
    report = {
        "schema_version": 1,
        "runtime": {
            "codex_version": "0.167.0",
            "source_baseline": "8ff7ad34a4f76f76f303a64a48f9f017c8797d45",
        },
        "case_id": case["case_id"],
        "attempt_id": attempt,
        "measurement_kind": measurement_kind,
        "source": case["source"],
        "configuration": {
            "model": model,
            "effort": effort,
            "topology": "direct-specialists",
            "device": case["device"],
            "strategy": "strategy.json",
        },
        "status": "failed" if failure else "completed",
        "error": failure,
        "stages": records,
        "usage": usage,
        "correctness": correctness,
        "wall_seconds": wall,
        "runner_and_check_seconds": wall - sum(r["wall_seconds"] for r in records),
        "limitations": [
            "Stage wall time includes model scheduling and tool work; it is not inference time.",
            "Synthetic fixtures validate instrumentation, not research benefit.",
            "A complete trace audit cannot prove that the upstream omitted no requests.",
        ],
    }
    write_json(output / "report.json", report)
    lines = [
        f"# Experiment cycle: {case['case_id']}",
        "",
        f"Mode: {measurement_kind}; correctness: {correctness['status']}; coverage complete: {usage['coverage']['complete']}",
        "",
        "| Stage | Status | Recorded tokens | Requests | Wall seconds |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for record in records:
        lines.append(
            f"| {record['stage']} | {record['status']} | {record['tokens']['total_tokens']} | {record['tokens']['responses']} | {record['wall_seconds']:.3f} |"
        )
    lines += [
        "",
        f"Coordinator: absent. Total recorded tokens: {usage['total']['total_tokens']}. Cycle wall seconds: {wall:.3f}.",
        "",
        "Recorded totals are partial when coverage is incomplete; fixture tokens are synthetic.",
    ]
    (output / "report.md").write_text("\n".join(lines) + "\n")
    return report
