#!/usr/bin/env python3
"""Run the skill's locked controller without depending on the caller's checkout."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def runtime_command(uv: str, args: list[str]) -> list[str]:
    return [uv, "run", "--locked", "--project", str(ROOT), "research-loop", *args]


def doctor(uv: str, args: list[str]) -> int:
    if args:
        print("doctor takes no arguments", file=sys.stderr)
        return 2
    checks = []
    for name, argv in (
        ("python", [sys.executable, "--version"]),
        ("uv", [uv, "--version"]),
        ("git", ["git", "--version"]),
        ("codex", ["codex", "--version"]),
        ("codex_login", ["codex", "login", "status"]),
        ("runtime", runtime_command(uv, ["--help"])),
    ):
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
            checks.append(
                {
                    "name": name,
                    "ok": result.returncode == 0,
                    "detail": (result.stdout + result.stderr).strip(),
                }
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks.append({"name": name, "ok": False, "detail": str(exc)})
    print(
        json.dumps(
            {
                "skill_root": str(ROOT),
                "checks": checks,
                "model_access": "unverified",
                "task_backend": "unverified",
            },
            indent=2,
        )
    )
    return 0 if all(check["ok"] for check in checks) else 1


def main(args: list[str] | None = None) -> int:
    args = sys.argv[1:] if args is None else args
    if sys.version_info < (3, 11):
        print("Research Loop requires Python 3.11+", file=sys.stderr)
        return 1
    for path in ("pyproject.toml", "uv.lock", "src/research_loop/cli.py"):
        if not (ROOT / path).is_file():
            print(f"Incomplete skill installation: missing {path}", file=sys.stderr)
            return 1
    uv = shutil.which("uv")
    if uv is None:
        print(
            "uv is required; install it before running Research Loop", file=sys.stderr
        )
        return 1
    if args and args[0] == "doctor":
        return doctor(uv, args[1:])
    # Replace this process so interrupts reach the actual runner. uv preserves cwd.
    os.execv(uv, runtime_command(uv, args))
    return 1  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
