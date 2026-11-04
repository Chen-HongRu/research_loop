"""Run one private, fresh-context Codex startup benchmark (no model fallback)."""

import argparse
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time


MODEL = "gpt-6.1-terra"


def summarize_events(path: Path) -> dict:
    commands = set()
    usage = {}
    invalid_lines = 0
    with path.open() as events:
        for line in events:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                invalid_lines += 1
                continue
            if not isinstance(event, dict):
                invalid_lines += 1
                continue
            item = event.get("item", {})
            if isinstance(item, dict) and item.get("type") == "command_execution":
                commands.add(item.get("id", json.dumps(item, sort_keys=True)))
            if event.get("type") == "turn.completed":
                for key, value in event.get("usage", {}).items():
                    if isinstance(value, int) and not isinstance(value, bool):
                        usage[key] = usage.get(key, 0) + value
    return {
        "tool_command_count": len(commands),
        "token_usage": usage,
        "invalid_event_lines": invalid_lines,
    }


def run_case(
    case: Path,
    prompt: Path,
    output: Path,
    timeout_seconds: float = 240,
    *,
    codex_executable: str = "codex",
) -> dict:
    """Executable injection is for offline tests; the CLI always invokes Codex."""
    case, prompt, output = case.resolve(), prompt.resolve(), output.resolve()
    if not case.is_dir():
        raise ValueError("case must be an existing workspace directory")
    if not prompt.is_file():
        raise ValueError("prompt must be an existing file")
    if prompt.is_relative_to(case) or output.is_relative_to(case):
        raise ValueError("prompt and output must be outside the writable workspace")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout must be positive and finite")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    temporary = case / ".tmp"
    if temporary.is_symlink():
        raise ValueError("Workspace temporary directory must not be a symlink")
    temporary.mkdir(mode=0o700, exist_ok=True)
    environment = {**os.environ, "TMPDIR": str(temporary)}
    argv = [
        codex_executable,
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--model",
        MODEL,
        "-c",
        'model_reasoning_effort="medium"',
        "-c",
        'approval_policy="never"',
        "-c",
        "sandbox_workspace_write.network_access=false",
        "-c",
        "sandbox_workspace_write.exclude_tmpdir_env_var=true",
        "-c",
        "sandbox_workspace_write.exclude_slash_tmp=true",
        "-c",
        'web_search="disabled"',
        "--sandbox",
        "workspace-write",
        "--json",
        "-C",
        str(case),
        "-o",
        str(output / "final.txt"),
        "-",
    ]
    started = time.monotonic()
    timed_out = False
    launch_error = None
    returncode = None
    with (
        prompt.open("rb") as stdin,
        (output / "stdout.jsonl").open("wb") as stdout,
        (output / "stderr.txt").open("wb") as stderr,
    ):
        try:
            process = subprocess.Popen(
                argv,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                cwd=case,
                env=environment,
                start_new_session=True,
            )
        except OSError as error:
            launch_error = str(error)
        else:
            try:
                returncode = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                # Also kill descendants if the leader exited during the grace.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                returncode = process.wait(timeout=2)
    result = {
        "argv": argv,
        "requested_model": MODEL,
        "model_identity_verified": False,
        "duration_seconds": time.monotonic() - started,
        "exit_code": returncode,
        "timed_out": timed_out,
        "launch_error": launch_error,
        **summarize_events(output / "stdout.jsonl"),
    }
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=240)
    args = parser.parse_args()
    result = run_case(args.case, args.prompt, args.output, args.timeout_seconds)
    print(json.dumps(result))
    return 0 if result["exit_code"] == 0 and not result["timed_out"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
