"""Configure and explicitly launch bounded strategy optimization."""

import argparse
import asyncio
from dataclasses import replace
import fcntl
import json
from pathlib import Path
import shutil
import tempfile
from time import monotonic, time

from research_loop import loop
from research_loop.codex import Native, Tool
from research_loop.state import write_json

from .accounting import summarize_usage
from .case import load_case as load_numeric_case
from .orchestration_program import DEFAULT_PATH, load_orchestration
from .runner import run as run_case

MODEL = "gpt-6.1-terra"
GOAL = """The only optimizable product file is work/orchestration.py; do not create or
edit other candidate files. Ordinary Study knowledge/draft files may be used for
records. Optimize using evaluate_candidate.

The candidate defines COMMON and async orchestrate(team). Use awaited
team.call(phase, role=..., context=..., instructions=..., handoff=None) for phase
JSON; team.message with the same arguments returns {"message": str} and is read-only.
team.execute() executes once and returns a receipt. Identical context names reuse a
context; current phase instructions are supplied on each turn. If branches are
allowed; imports and loops are not. Phase order is plan, prepare, execute, evaluate;
three delivery calls are required and at most eight model calls are allowed.

The program controls calls, handoffs, context reuse, role counts and bounded feedback
within the fixed plan, prepare, execute and evaluate phases. The host enforces an
eight-call cap and fixes the model to gpt-6.1-terra at medium effort. Correctness,
complete usage coverage and the phase protocol across every case are hard gates.
Among passing candidates minimize input plus output tokens first and batch wall time
second. Read baseline.json for the host-measured initial program and test each
candidate before making claims. Read best.json and evaluation-history.json for
measured results. Evaluations have no count cap unless an operator explicitly
configures one. If run-window.json exists, keep iterating until its deadline; one
passing improvement does not finish the window. Record the best measured result. Never modify the evaluator or cases,
supply paths or commands to it, or seek hidden answers. If resource_blocked, stop
and ask the operator to resume when resources are available; do not poll.
"""


def case_backend(path):
    value = json.loads(Path(path).read_text())
    if value.get("case_type") == "cactus-dev":
        from . import cactus_case
        from .collection import RemoteExecutor

        return cactus_case.load_case, cactus_case.check, RemoteExecutor()
    return load_numeric_case, None, None


def validate_case(path):
    loader, checker, executor = case_backend(path)
    case = loader(path)
    if executor is not None:
        intervention = case.get("intervention_id")
        if type(intervention) is not int or not 1 <= intervention <= 10:
            raise ValueError(
                "Cactus optimization cases require intervention_id from 1 to 10"
            )
    return case, loader, checker, executor


async def resource_preflight(cases):
    from .collection import remote

    validated = [validate_case(path) for path in cases]
    seen = set()
    for case, _, _, executor in validated:
        if executor is None:
            continue
        resource = case["remote"]
        identity = (resource["ssh_host"], resource["gpu_uuid"])
        if identity in seen:
            continue
        seen.add(identity)
        status = await remote(resource, "preflight")
        if not status.get("ready"):
            return {
                "resource_blocked": True,
                "model_calls": 0,
                "reason": status["reason"],
            }
    return None


def initialize(study, cases, max_evaluations=None):
    study = Path(study).resolve()
    if max_evaluations is not None and (
        type(max_evaluations) is not int or max_evaluations < 1
    ):
        raise ValueError("max_evaluations must be positive")
    if not cases:
        raise ValueError("at least one case is required")
    resolved = [Path(case).resolve(strict=True) for case in cases]
    for case in resolved:
        if study.is_relative_to(case.parent) or case.is_relative_to(study):
            raise ValueError("case directories and study must be disjoint")
        validate_case(case)
    if len(set(resolved)) != len(resolved):
        raise ValueError("duplicate cases are not allowed")
    if study.exists():
        raise ValueError("study already exists")
    # The seed is a separate, read-only project; evaluator outputs stay outside
    # the study so they are not exposed through its normal artifact directory.
    support = Path(tempfile.mkdtemp(prefix=".cycle-optimization-", dir=study.parent))
    if any(
        support.is_relative_to(case.parent) or case.parent.is_relative_to(support)
        for case in resolved
    ):
        shutil.rmtree(support)
        raise ValueError("case directories and evaluator root must be disjoint")
    seed = support.with_name(support.name + "-seed")
    seed.mkdir()
    shutil.copyfile(DEFAULT_PATH, seed / "orchestration.py")
    goal = support / "goal.md"
    goal.write_text(GOAL)
    try:
        frozen_cases = []
        for index, original in enumerate(resolved):
            case, _, _, _ = validate_case(original)
            target = support / "cases" / f"case-{index:04d}"
            target.mkdir(parents=True)
            manifest = json.loads(original.read_text())
            for path in case["workspace"].rglob("*"):
                if (
                    path.is_symlink()
                    or path.name == ".git"
                    or not (path.is_file() or path.is_dir())
                ):
                    raise ValueError(
                        "starting workspace contains links, Git history or special files"
                    )
            shutil.copytree(case["workspace"], target / "workspace")
            shutil.copyfile(case["task"], target / "task.md")
            manifest.update(task="task.md", workspace="workspace")
            if "oracle" in manifest:
                shutil.copyfile(case["oracle"], target / "oracle.json")
                manifest["oracle"] = "oracle.json"
            frozen = target / "case.json"
            write_json(frozen, manifest)
            validate_case(frozen)
            frozen_cases.append(frozen)
        result = loop.initialize(study, seed, goal, model=MODEL, delegation="host")
        shutil.copyfile(DEFAULT_PATH, study / "work/orchestration.py")
        write_json(
            study / "evaluator.json",
            {
                "cases": list(map(str, frozen_cases)),
                "source_case_roots": list(map(str, {case.parent for case in resolved})),
                "max_evaluations": max_evaluations,
                "support": str(support),
            },
        )
        write_json(study / "evaluation-ledger.json", {"charged": 0})
        write_json(study / "pending-operations.json", [])
    except BaseException:
        shutil.rmtree(support, ignore_errors=True)
        shutil.rmtree(seed, ignore_errors=True)
        raise
    return result


def terminal_receipt(receipt):
    return (
        isinstance(receipt, dict)
        and type(receipt.get("exit_code")) is int
        and not receipt.get("cleanup_uncertain")
    )


def record_pending(study, config, evaluation, existing):
    pending = list(existing)
    resolved_path = study / "reconciled-operations.json"
    resolved = json.loads(resolved_path.read_text()) if resolved_path.exists() else []
    for index, case_path in enumerate(config["cases"]):
        operation_path = evaluation / f"case-{index:04d}/execute/remote-operation.json"
        if not operation_path.exists():
            continue
        operation = json.loads(operation_path.read_text())
        if terminal_receipt(operation.get("receipt")):
            continue
        case, _, _, executor = validate_case(case_path)
        if executor is None:
            continue
        entry = {"remote": case["remote"], "operation_id": operation["operation_id"]}
        if entry not in pending and entry not in resolved:
            pending.append(entry)
    write_json(study / "pending-operations.json", pending)
    return pending


async def reconcile_pending(study):
    from .collection import remote

    path = study / "pending-operations.json"
    pending = json.loads(path.read_text()) if path.exists() else []
    remaining = []
    resolved_path = study / "reconciled-operations.json"
    resolved = json.loads(resolved_path.read_text()) if resolved_path.exists() else []
    for entry in pending:
        try:
            receipt = await remote(entry["remote"], "reconcile", entry["operation_id"])
        except Exception:
            remaining.append(entry)
            continue
        if not terminal_receipt(receipt):
            remaining.append(entry)
        elif entry not in resolved:
            resolved.append(entry)
    write_json(resolved_path, resolved)
    write_json(path, remaining)
    if remaining:
        return {
            "resource_blocked": True,
            "reconciliation_required": True,
            "model_calls": 0,
        }
    return None


async def reconcile_evaluations(study, config, charged):
    support = Path(config["support"])
    pending_path = study / "pending-operations.json"
    pending = json.loads(pending_path.read_text()) if pending_path.exists() else []
    for number in range(1, charged + 1):
        pending = record_pending(
            study, config, support / f"evaluation-{number:06d}", pending
        )
    return await reconcile_pending(study)


def recover_partial(output):
    """Recover observed receipts after an interrupted case, never imply coverage."""
    report_path = output / "report.json"
    try:
        if report_path.is_file():
            report = json.loads(report_path.read_text())
            return report["usage"]["total"]["total_tokens"]
        tasks_path = output / "tasks.json"
        if tasks_path.is_file():
            tasks = json.loads(tasks_path.read_text())["tasks"]
            return summarize_usage(tasks)["total"]["total_tokens"]
        stages_path = output / "stages.json"
        if stages_path.is_file():
            stages = json.loads(stages_path.read_text())
            return sum(
                stage.get("tokens", {}).get("total_tokens", 0) for stage in stages
            )
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return 0


def aggregate_reports(reports):
    by_stage, by_role = {}, {}
    fields = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "total_tokens",
        "responses",
    )
    for report in reports:
        usage = report.get("usage", {})
        for stage, bucket in usage.get("by_stage", {}).items():
            target = by_stage.setdefault(
                stage, {**dict.fromkeys(fields, 0), "wall_seconds": 0}
            )
            for field in fields:
                target[field] += bucket.get(field, 0)
        for row in report.get("stages", []):
            stage = row.get("stage")
            if stage:
                target = by_stage.setdefault(
                    stage, {**dict.fromkeys(fields, 0), "wall_seconds": 0}
                )
                target["wall_seconds"] += row.get("wall_seconds") or 0
        for role, bucket in usage.get("by_role", {}).items():
            target = by_role.setdefault(
                role, {**dict.fromkeys(fields, 0), "calls": 0, "contexts": 0}
            )
            for field in fields:
                target[field] += bucket.get(field, 0)
        contexts = {}
        for call in report.get("calls", []):
            role = call.get("role")
            if not role:
                continue
            target = by_role.setdefault(
                role, {**dict.fromkeys(fields, 0), "calls": 0, "contexts": 0}
            )
            target["calls"] += 1
            if call.get("context"):
                contexts.setdefault(role, set()).add(call["context"])
        for role, names in contexts.items():
            by_role[role]["contexts"] += len(names)
    return by_stage, by_role


class BoundedNative:
    """Settle the lead response before enforcing host iteration limits."""

    def __init__(self, native, evaluator):
        self.native = native
        self.evaluator = evaluator

    def __getattr__(self, name):
        return getattr(self.native, name)

    async def run(self, *args, **kwargs):
        result = await self.native.run(*args, **kwargs)
        ledger = json.loads(
            (self.evaluator.study / "evaluation-ledger.json").read_text()
        )
        state = json.loads((self.evaluator.study / "status.json").read_text())
        limit = self.evaluator.config["max_evaluations"]
        if (
            limit is not None
            and result.get("study_action") == "continue"
            and (ledger["charged"] >= limit or state.get("iteration", 1) >= limit)
        ):
            result = {
                **result,
                "study_action": "stop",
                "stop_reason": "Host optimization budget exhausted.",
            }
        return result


def window_closed(study):
    path = Path(study) / "run-window.json"
    return path.exists() and time() >= json.loads(path.read_text())["deadline_unix"]


class Evaluator:
    def __init__(self, study):
        self.study = Path(study).resolve(strict=True)
        self.config = json.loads((self.study / "evaluator.json").read_text())
        self.lock = asyncio.Lock()

    async def __call__(self, arguments):
        if arguments != {}:
            raise ValueError("evaluate_candidate accepts no arguments")
        async with self.lock:
            # Lock also serializes separate optimizer processes sharing this study.
            with (self.study / "evaluation.lock").open("a") as lock:
                await asyncio.to_thread(fcntl.flock, lock.fileno(), fcntl.LOCK_EX)
                try:
                    return await self._evaluate()
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    async def _evaluate(self):
        if window_closed(self.study):
            return {
                "resource_blocked": True,
                "time_window_finished": True,
                "model_calls": 0,
            }
        program = load_orchestration(self.study / "work/orchestration.py")
        ledger_path = self.study / "evaluation-ledger.json"
        ledger = json.loads(ledger_path.read_text())
        limit = self.config["max_evaluations"]
        if limit is not None and ledger["charged"] >= limit:
            raise ValueError("evaluation budget exhausted")
        if ledger["charged"] == 0:
            seed = (
                Path(json.loads((self.study / "config.json").read_text())["project"])
                / "orchestration.py"
            )
            if program.source != load_orchestration(seed).source:
                raise ValueError(
                    "first evaluation requires the unmodified seed program"
                )
        blocked = await reconcile_evaluations(
            self.study, self.config, ledger["charged"]
        )
        if blocked:
            return blocked
        blocked = await resource_preflight(self.config["cases"])
        if blocked:
            write_json(Path(self.config["support"]) / "resource-blocked.json", blocked)
            return blocked
        groups = {}
        for index, case in enumerate(self.config["cases"]):
            loader, checker, executor = case_backend(case)
            if executor is None:
                identity = ("cpu",)
            else:
                resource = loader(case)["remote"]
                identity = (resource["ssh_host"], resource["gpu_uuid"])
            groups.setdefault(identity, []).append(
                (index, case, loader, checker, executor)
            )
        evaluation = (
            Path(self.config["support"]) / f"evaluation-{ledger['charged'] + 1:06d}"
        )
        evaluation.mkdir(mode=0o700)
        snapshot = evaluation / "orchestration.py"
        snapshot.write_text(program.source, encoding="utf-8")
        ledger["charged"] += 1
        write_json(ledger_path, ledger)
        reports = []
        partial_tokens = []
        stopped = False
        started = monotonic()
        status_path = self.study / "evaluation-status.json"

        def progress():
            write_json(
                status_path,
                {
                    "status": "running",
                    "evaluation": ledger["charged"],
                    "cases_total": len(self.config["cases"]),
                    "cases_completed": len(reports) + len(partial_tokens),
                },
            )

        progress()

        async def worker(items):
            nonlocal stopped
            for index, case, loader, checker, executor in items:
                if (
                    stopped
                    or (self.study / "STOP").exists()
                    or window_closed(self.study)
                ):
                    stopped = True
                    break
                try:
                    if hasattr(executor, "can_dispatch"):
                        executor.can_dispatch = lambda: (
                            not (self.study / "STOP").exists()
                            and not window_closed(self.study)
                        )
                    report = await run_case(
                        Path(case),
                        evaluation / f"case-{index:04d}",
                        orchestration=snapshot,
                        model=MODEL,
                        case_loader=loader,
                        case_checker=checker,
                        executor=executor,
                    )
                    write_json(evaluation / f"report-{index:04d}.json", report)
                    reports.append(report)
                    if not (
                        report.get("status") == "completed"
                        and report.get("correctness", {}).get("status") == "pass"
                        and report.get("usage", {}).get("coverage", {}).get("complete")
                        and report.get("phase_protocol_pass") is True
                    ):
                        stopped = True
                except Exception:
                    stopped = True
                    partial_tokens.append(
                        recover_partial(evaluation / f"case-{index:04d}")
                    )
                progress()

        await asyncio.gather(*(worker(items) for items in groups.values()))
        pending_path = self.study / "pending-operations.json"
        pending = json.loads(pending_path.read_text()) if pending_path.exists() else []
        record_pending(self.study, self.config, evaluation, pending)
        stopped = stopped or (self.study / "STOP").exists()
        complete = len(reports) == len(self.config["cases"]) and not stopped
        by_stage, by_role = aggregate_reports(reports)
        result = {
            "by_stage": by_stage,
            "by_role": by_role,
            "correctness_pass": complete
            and all(
                r["status"] == "completed" and r["correctness"]["status"] == "pass"
                for r in reports
            ),
            "coverage_complete": complete
            and all(r["usage"]["coverage"]["complete"] for r in reports),
            "phase_protocol_pass": complete
            and all(r.get("phase_protocol_pass", False) for r in reports),
            "evaluation_failed": not complete,
            "total_tokens": sum(r["usage"]["total"]["total_tokens"] for r in reports)
            + sum(partial_tokens),
            "wall_seconds": monotonic() - started,
            "cases_evaluated": sum(r.get("status") == "completed" for r in reports),
            "partial_cases": len(partial_tokens),
            "evaluations_charged": ledger["charged"],
        }
        write_json(evaluation / "result.json", result)
        write_json(
            status_path,
            {
                "status": "completed" if complete else "failed",
                "evaluation": ledger["charged"],
                "cases_total": len(self.config["cases"]),
                "cases_completed": len(reports) + len(partial_tokens),
                "result": result,
            },
        )
        history_path = self.study / "evaluation-history.json"
        history = json.loads(history_path.read_text()) if history_path.exists() else []
        history.append(result)
        write_json(history_path, history)
        if ledger["charged"] == 1:
            write_json(self.study / "baseline.json", result)
        if (
            result["correctness_pass"]
            and result["coverage_complete"]
            and result["phase_protocol_pass"]
        ):
            best_path = self.study / "best.json"
            best = json.loads(best_path.read_text()) if best_path.exists() else None
            if best is None or (result["total_tokens"], result["wall_seconds"]) < (
                best["total_tokens"],
                best["wall_seconds"],
            ):
                write_json(best_path, {**result, "orchestration": str(snapshot)})
        return result


def make_native_factory(study):
    evaluator = Evaluator(study)
    tool = Tool(
        "evaluate_candidate",
        "Evaluate the fixed work/orchestration.py against the configured cases.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        evaluator,
    )

    def factory(config, journal, tools):
        args = list(config.launch_args_override or ())
        args = [
            arg.replace('web_search="live"', 'web_search="disabled"') for arg in args
        ]
        args[-3:-3] = ["--config", "project_doc_max_bytes=0"]
        return BoundedNative(
            Native(
                replace(config, launch_args_override=tuple(args)),
                journal,
                [*tools, tool],
            ),
            evaluator,
        )

    return factory


def passes(result):
    return all(
        result.get(key) is True
        for key in ("correctness_pass", "coverage_complete", "phase_protocol_pass")
    ) and not result.get("evaluation_failed", True)


async def run_study(study, *, continue_run=False, preserve_stop=False):
    study = Path(study).resolve(strict=True)
    evaluator = Evaluator(study)
    ledger = json.loads((study / "evaluation-ledger.json").read_text())
    baseline_path = study / "baseline.json"
    if ledger["charged"] == 0:
        baseline = await evaluator({})
        if baseline.get("resource_blocked"):
            return baseline
    else:
        if not baseline_path.exists():
            raise ValueError("charged study is missing baseline.json")
        baseline = json.loads(baseline_path.read_text())
    if not passes(baseline):
        return {"status": "baseline_failed", "baseline": baseline}
    ledger = json.loads((study / "evaluation-ledger.json").read_text())
    limit = evaluator.config["max_evaluations"]
    if limit is not None and ledger["charged"] >= limit:
        return {"status": "budget_exhausted", "baseline": baseline}
    blocked = await reconcile_evaluations(study, evaluator.config, ledger["charged"])
    if blocked:
        return blocked
    return await loop.run(
        study,
        continue_run=continue_run,
        native_factory=make_native_factory(study),
        clear_stop=not preserve_stop,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init", help="Configure a study without starting any model")
    init.add_argument("--case", type=Path, action="append", required=True)
    init.add_argument("--study", type=Path, required=True)
    init.add_argument("--max-evaluations", type=int)
    launch = sub.add_parser("run", help="Start the configured optimization")
    launch.add_argument("study", type=Path)
    launch.add_argument("--allow-model", action="store_true")
    launch.add_argument("--allow-gpu", action="store_true")
    launch.add_argument(
        "--continue",
        dest="continue_run",
        action="store_true",
        help="Explicitly resume a stopped/completed Study without resetting its budget",
    )
    launch.add_argument(
        "--preserve-stop",
        action="store_true",
        help="Continue a Study without reopening an operator STOP",
    )
    args = parser.parse_args()
    try:
        if args.action == "init":
            result = initialize(args.study, args.case, args.max_evaluations)
        else:
            if not args.allow_model:
                parser.error("run requires --allow-model")
            config = json.loads((args.study / "evaluator.json").read_text())
            if (
                any(case_backend(path)[2] is not None for path in config["cases"])
                and not args.allow_gpu
            ):
                parser.error("GPU evaluation cases require --allow-gpu")
            result = asyncio.run(
                run_study(
                    args.study,
                    continue_run=args.continue_run,
                    preserve_stop=args.preserve_stop,
                )
            )
            if (
                result.get("resource_blocked")
                or result.get("status") == "baseline_failed"
            ):
                print(json.dumps(result, indent=2))
                raise SystemExit(75 if result.get("resource_blocked") else 1)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
