"""Installed dispatch must not depend on the developer checkout or caller cwd."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/research_loop.py"
spec = importlib.util.spec_from_file_location("skill_launcher", SCRIPT)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class SkillLauncherTests(unittest.TestCase):
    def test_relocated_dispatch_preserves_arguments_and_cwd(self):
        with tempfile.TemporaryDirectory(prefix="installed skill ") as folder:
            root = Path(folder)
            for name in ("pyproject.toml", "uv.lock", "src/research_loop/cli.py"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            cwd = Path.cwd()
            with (
                patch.object(launcher, "ROOT", root),
                patch.object(launcher.shutil, "which", return_value="/bin/uv"),
                patch.object(launcher.os, "execv") as execute,
            ):
                launcher.main(["status", "/path with spaces/study"])
            execute.assert_called_once_with(
                "/bin/uv",
                [
                    "/bin/uv",
                    "run",
                    "--locked",
                    "--project",
                    str(root),
                    "research-loop",
                    "status",
                    "/path with spaces/study",
                ],
            )
            self.assertEqual(Path.cwd(), cwd)

    def test_incomplete_install_fails_before_start(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(launcher, "ROOT", Path(folder)):
                with patch.object(launcher.os, "execv") as execute:
                    self.assertEqual(launcher.main(["run", "/study"]), 1)
                    execute.assert_not_called()

    def test_doctor_reports_login_failure_without_a_model_call(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv,
                1 if argv == ["codex", "login", "status"] else 0,
                stdout="",
                stderr="not logged in" if argv[-1] == "status" else "",
            )

        with (
            patch.object(launcher.subprocess, "run", side_effect=run),
            patch("builtins.print") as output,
        ):
            self.assertEqual(launcher.doctor("/bin/uv", []), 1)
        report = json.loads(output.call_args.args[0])
        self.assertFalse(
            next(c for c in report["checks"] if c["name"] == "codex_login")["ok"]
        )
        self.assertEqual(report["model_access"], "unverified")
        self.assertFalse(any("exec" in command for command in calls))


if __name__ == "__main__":
    unittest.main()
