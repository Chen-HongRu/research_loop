"""File client for operator preparation and subsequent real Study launch."""

import argparse
import json
from pathlib import Path
import time


def local_file(value: str) -> Path:
    path = Path(value).resolve()
    if not path.is_relative_to(Path.cwd().resolve()) or not path.is_file():
        raise ValueError("inputs must be existing files inside the current workspace")
    if path.stat().st_size > 20000:
        raise ValueError("input must be <= 20000 bytes")
    return path


def exchange(name: str, request: dict, timeout: int) -> dict:
    destination = Path(name + "-request.json")
    if destination.exists() or destination.is_symlink():
        raise ValueError(
            "Operation already requested; inspect retained result, no retries"
        )
    temporary = destination.with_suffix(".json.tmp")
    with temporary.open("x") as stream:
        json.dump(request, stream, ensure_ascii=False, allow_nan=False)
    temporary.replace(destination)
    deadline = time.monotonic() + timeout
    response = Path(name + "-result.json")
    while time.monotonic() < deadline:
        if response.exists():
            if response.is_symlink() or response.stat().st_size > 1048576:
                raise ValueError("Invalid host response file")
            result = json.loads(response.read_text())
            print(json.dumps(result, ensure_ascii=False))
            return result
        time.sleep(0.2)
    raise TimeoutError(
        "Host operation did not finish; inspect retained identity, no retries"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("prepare", help="Stage the fresh remote environment/source")
    launch = commands.add_parser(
        "launch", help="Launch a real Plan Execute Review Study"
    )
    launch.add_argument("--goal", required=True, help="Local goal text file")
    launch.add_argument("--task", help="Optional local TaskQueue request JSON file")
    launch.add_argument("--selection-policy", choices=["direct", "debate"])
    args = parser.parse_args()
    if args.operation == "prepare":
        return 0 if exchange("setup", {}, 120).get("status") == "prepared" else 1
    goal_text = local_file(args.goal).read_text()
    if not goal_text.strip():
        raise ValueError("Goal must not be empty")
    request = {
        "goal_text": goal_text,
        "task": json.loads(local_file(args.task).read_text()) if args.task else None,
        "selection_policy": args.selection_policy,
    }
    return 0 if exchange("launch", request, 360).get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
