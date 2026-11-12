"""Run the same real repository audit on Linux or macOS without path edits."""

import argparse
import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import platform
import subprocess

from research_loop.codex import Native
from research_loop import application
from research_loop.controller import run_foreground


async def execute(study, *, timeout, max_calls):
    calls = 0

    class BoundedNative(Native):
        async def run(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls > max_calls:
                raise RuntimeError("Audit model-call limit reached; inspect artifacts")
            kwargs.setdefault("max_output_chars", 32768)
            return await super().run(*args, **kwargs)

    task = asyncio.create_task(run_foreground(study, native_factory=BoundedNative))
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if task not in done:
        application.stop(study, identity="audit-wall-time-limit")
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=40)
        except (asyncio.CancelledError, TimeoutError):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        raise TimeoutError("Audit stopped at its wall-time limit")
    return await task


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study", type=Path, help="New private Study directory")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "research.yaml",
        help="Explicit declarative workflow and OpenAI model profiles",
    )
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--max-model-calls", type=int, default=18)
    parser.add_argument(
        "--run", action="store_true", help="Start authorized model calls and CPU audit"
    )
    args = parser.parse_args()
    if args.timeout <= 0 or args.max_model_calls <= 0:
        parser.error("Limits must be positive")
    project = Path(__file__).resolve().parents[2]
    revision = subprocess.check_output(
        ["git", "-C", str(project), "rev-parse", "HEAD"], text=True
    ).strip()
    study = args.study.absolute()
    study.parent.mkdir(parents=True, exist_ok=True)
    goal = Path(__file__).with_name("goal.md")
    inspected = application.prepare(args.config, study, project=project, goal=goal)
    application.intervene(
        study,
        kind="experiment",
        payload={"text": f"Source revision: {revision}\n\n" + goal.read_text()},
        identity="portability-audit",
    )
    (study / "portability-input.json").write_text(
        json.dumps(
            {
                "source_revision": revision,
                "platform": platform.system(),
                "prepared_at": datetime.now(UTC).isoformat(),
                "model_calls_started": args.run,
                "config_revision": inspected["config"]["revision"],
                "profiles": inspected["config"]["profiles"],
                "timeout_seconds": args.timeout,
                "max_model_calls": args.max_model_calls,
            },
            indent=2,
        )
        + "\n"
    )
    if not args.run:
        print(json.dumps({"study": str(study), "status": "prepared", "model_calls": 0}))
        return
    result = asyncio.run(
        execute(study, timeout=args.timeout, max_calls=args.max_model_calls)
    )
    print(json.dumps({"study": str(study), "status": result["status"]}))
    if result["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
