"""Run with python -m benchmarks.experiment_cycle from the repository checkout."""

import argparse
import asyncio
import json
from pathlib import Path

from .case import load_case
from .runner import run


def main():
    parser = argparse.ArgumentParser(
        description="Coordinator-free experiment-cycle benchmark"
    )
    sub = parser.add_subparsers(dest="action", required=True)
    inspect = sub.add_parser(
        "inspect", help="Validate a frozen case without a model call"
    )
    inspect.add_argument("case", type=Path)
    launch = sub.add_parser(
        "run", help="Run three bounded specialist tasks and one local command"
    )
    launch.add_argument("case", type=Path)
    launch.add_argument("--output", type=Path, required=True)
    launch.add_argument(
        "--allow-model",
        action="store_true",
        help="Authorize the three model tasks for this case",
    )
    launch.add_argument(
        "--orchestration", type=Path, help="Editable Python Agent orchestration"
    )
    launch.add_argument("--model", default="gpt-6.1-terra")
    args = parser.parse_args()
    try:
        if args.action == "inspect":
            case = load_case(args.case)
            print(
                json.dumps(
                    {
                        key: case[key]
                        for key in ("case_id", "source", "device", "limits", "command")
                    },
                    indent=2,
                )
            )
            return
        if not args.allow_model:
            parser.error("run requires --allow-model; inspect makes no model calls")
        report = asyncio.run(
            run(
                args.case,
                args.output,
                orchestration=args.orchestration,
                model=args.model,
            )
        )
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(
        json.dumps(
            {
                "status": report["status"],
                "correctness": report["correctness"]["status"],
                "report": str(args.output / "report.json"),
            },
            indent=2,
        )
    )
    if (
        report["status"] != "completed"
        or report["correctness"]["status"] != "pass"
        or not report["usage"]["coverage"]["complete"]
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
