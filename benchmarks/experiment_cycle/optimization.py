"""Configure and explicitly launch bounded strategy optimization."""

import argparse
import asyncio
from dataclasses import replace
import fcntl
import json
from pathlib import Path
import shutil
import tempfile
import tomllib

from research_loop import loop
from research_loop.codex import Native, Tool
from research_loop.state import write_json

from .case import load_case as load_numeric_case
from .policy import DEFAULT_PATH, load_policy
from .runner import run as run_case

MODEL = "gpt-6.1-terra"
GOAL = """Optimize only work/strategy.json using evaluate_candidate. Edit no other files.
The strategy schema and fixed evaluator are authoritative. Correctness and complete
usage coverage are hard gates across all cases. Among passing candidates minimize
recorded total tokens first and wall time second. Do not modify the production
runtime, evaluator, cases, or self-repair logic. Do not seek hidden answers.
If evaluate_candidate returns resource_blocked, stop the Study and ask the operator
to resume when resources are available; do not poll or spend model calls waiting.
"""


def case_backend(path):
    value = json.loads(Path(path).read_text())
    if value.get("case_type") == "cactus-dev":
        from . import cactus_case
        from .collection import RemoteExecutor

        return cactus_case.load_case, cactus_case.check, RemoteExecutor()
    return load_numeric_case, None, None


async def resource_preflight(cases):
    from .collection import remote

    seen = set()
    for path in cases:
        loader, _, executor = case_backend(path)
        if executor is None:
            continue
        case = loader(path)
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


def initialize(study, cases, max_evaluations):
    study = Path(study).resolve()
    if type(max_evaluations) is not int or max_evaluations < 1:
        raise ValueError("max_evaluations must be positive")
    if not cases:
        raise ValueError("at least one case is required")
    resolved = [Path(case).resolve(strict=True) for case in cases]
    for case in resolved:
        if study.is_relative_to(case.parent) or case.is_relative_to(study):
            raise ValueError("case directories and study must be disjoint")
        case_backend(case)[0](case)
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
    seed = support / "seed"
    seed.mkdir()
    shutil.copyfile(DEFAULT_PATH, seed / "strategy.json")
    goal = support / "goal.md"
    goal.write_text(GOAL)
    try:
        result = loop.initialize(study, seed, goal, model=MODEL, delegation="host")
        shutil.copyfile(DEFAULT_PATH, study / "work/strategy.json")
        write_json(
            study / "evaluator.json",
            {
                "cases": list(map(str, resolved)),
                "max_evaluations": max_evaluations,
                "support": str(support),
            },
        )
        write_json(study / "evaluation-ledger.json", {"charged": 0})
    except BaseException:
        shutil.rmtree(support, ignore_errors=True)
        raise
    return result


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
        policy = load_policy(self.study / "work/strategy.json")
        blocked = await resource_preflight(self.config["cases"])
        if blocked:
            return blocked
        ledger_path = self.study / "evaluation-ledger.json"
        ledger = json.loads(ledger_path.read_text())
        if ledger["charged"] >= self.config["max_evaluations"]:
            raise ValueError("evaluation budget exhausted")
        ledger["charged"] += 1
        write_json(ledger_path, ledger)
        evaluation = (
            Path(self.config["support"]) / f"evaluation-{ledger['charged']:06d}"
        )
        evaluation.mkdir(mode=0o700)
        snapshot = evaluation / "strategy.json"
        write_json(snapshot, policy)
        reports = []
        try:
            for index, case in enumerate(self.config["cases"]):
                loader, checker, executor = case_backend(case)
                reports.append(
                    await run_case(
                        Path(case),
                        evaluation / f"case-{index:04d}",
                        strategy=snapshot,
                        model=MODEL,
                        case_loader=loader,
                        case_checker=checker,
                        executor=executor,
                    )
                )
        except Exception:
            return {
                "correctness_pass": False,
                "coverage_complete": False,
                "evaluation_failed": True,
                "evaluations_charged": ledger["charged"],
            }
        return {
            "correctness_pass": all(
                r["status"] == "completed" and r["correctness"]["status"] == "pass"
                for r in reports
            ),
            "coverage_complete": all(
                r["usage"]["coverage"]["complete"] for r in reports
            ),
            "total_tokens": sum(r["usage"]["total"]["total_tokens"] for r in reports),
            "wall_seconds": sum(r["wall_seconds"] for r in reports),
            "cases_evaluated": len(reports),
            "evaluations_charged": ledger["charged"],
        }


def make_native_factory(study):
    evaluator = Evaluator(study)
    tool = Tool(
        "evaluate_candidate",
        "Evaluate the fixed work/strategy.json against the configured cases.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        evaluator,
    )

    def factory(config, journal, tools):
        support = Path(evaluator.config["support"]).resolve()
        seed = support / "seed"
        roots = [Path(case).resolve().parent for case in evaluator.config["cases"]]
        if any(
            seed.is_relative_to(root) or root.is_relative_to(support) for root in roots
        ):
            raise ValueError("case directories and evaluator root must be disjoint")
        roots.append(support)
        args = list(config.launch_args_override or ())
        prefix = "permissions.research.filesystem="
        indices = [i for i, arg in enumerate(args) if arg.startswith(prefix)]
        if len(indices) != 1:
            raise ValueError("native filesystem permissions are missing or ambiguous")
        index = indices[0]
        paths = tomllib.loads("paths=" + args[index][len(prefix) :])["paths"]
        paths = {
            path: permission
            for path, permission in paths.items()
            if path.startswith(":")
            or not any(Path(path).is_relative_to(root) for root in roots)
        }
        paths.update({str(root): "deny" for root in roots})
        paths[str(seed)] = "read"
        args[index] = (
            prefix
            + "{"
            + ",".join(
                json.dumps(path) + "=" + json.dumps(permission)
                for path, permission in paths.items()
            )
            + "}"
        )
        return Native(
            replace(config, launch_args_override=tuple(args)), journal, [*tools, tool]
        )

    return factory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init", help="Configure a study without starting any model")
    init.add_argument("--case", type=Path, action="append", required=True)
    init.add_argument("--study", type=Path, required=True)
    init.add_argument("--max-evaluations", type=int, required=True)
    launch = sub.add_parser("run", help="Start the configured optimization")
    launch.add_argument("study", type=Path)
    launch.add_argument("--allow-model", action="store_true")
    launch.add_argument("--allow-gpu", action="store_true")
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
            blocked = asyncio.run(resource_preflight(config["cases"]))
            if blocked:
                print(json.dumps(blocked, indent=2))
                raise SystemExit(75)
            result = asyncio.run(
                loop.run(
                    args.study.resolve(), native_factory=make_native_factory(args.study)
                )
            )
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
