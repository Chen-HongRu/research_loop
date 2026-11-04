import json
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from benchmarks.startup import evaluate


class StartupApiScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.runtime = self.root / "candidate"
        for directory in ("benchmarks/startup", "src/research_loop"):
            (self.runtime / directory).mkdir(parents=True)
        self.marker = self.root / "candidate-executed"
        self.payload = (
            f"from pathlib import Path\nPath({str(self.marker)!r}).touch()\n"
            "raise RuntimeError('Candidate code executed on host')\n"
        ).encode()
        for name in ("benchmarks/startup/client.py", "src/research_loop/submission.py"):
            (self.runtime / name).write_bytes(self.payload)
        self.case = self.root / "case"
        self.baseline = self.root / "baseline"
        self.case.mkdir()
        self.baseline.mkdir()

    def test_api_copies_actual_candidate_bytes_without_execution(self):
        evaluate.copy_actor_inputs(self.runtime, self.case, self.baseline, "api")
        self.assertEqual((self.case / "client.py").read_bytes(), self.payload)
        self.assertEqual((self.baseline / "submission.py").read_bytes(), self.payload)
        self.assertEqual(
            (self.baseline / "research_loop_events.py").read_bytes(),
            (evaluate.FIXED_ROOT / "src/research_loop/telemetry.py").read_bytes(),
        )
        self.assertEqual(
            (self.case / "API_CONTRACT.md").read_bytes(),
            (self.baseline / "API_CONTRACT.md").read_bytes(),
        )
        self.assertFalse(self.marker.exists())

    def test_docs_scope_uses_fixed_actor_modules(self):
        evaluate.copy_actor_inputs(self.runtime, self.case, self.baseline, "docs")
        self.assertEqual(
            (self.case / "client.py").read_bytes(),
            (evaluate.FIXED_ROOT / "benchmarks/startup/client.py").read_bytes(),
        )
        self.assertEqual(
            (self.baseline / "submission.py").read_bytes(),
            (evaluate.FIXED_ROOT / "src/research_loop/submission.py").read_bytes(),
        )
        self.assertFalse((self.case / "API_CONTRACT.md").exists())
        self.assertFalse(self.marker.exists())

    def test_candidate_symlink_is_rejected_before_actor_copy(self):
        client = self.runtime / "benchmarks/startup/client.py"
        client.unlink()
        client.symlink_to(self.runtime / "src/research_loop/submission.py")
        with self.assertRaises(OSError):
            evaluate.copy_actor_inputs(self.runtime, self.case, self.baseline, "api")
        self.assertFalse(self.marker.exists())

    def test_host_imports_are_pinned_even_with_candidate_on_python_path(self):
        (self.runtime / "src/research_loop/__init__.py").write_bytes(self.payload)
        for module in ("cli", "core", "harness", "tasks", "telemetry"):
            (self.runtime / f"src/research_loop/{module}.py").write_bytes(self.payload)
        source = """
import json
from pathlib import Path
import sys
from benchmarks.startup import evaluate
sys.path.insert(0, sys.argv[1])
evaluate.pin_host_imports()
from research_loop import cli, core, harness, submission, tasks, telemetry
from benchmarks.startup import backend, study_bridge, verify_dashboard
print(json.dumps([str(Path(module.__file__).resolve()) for module in (
    cli, core, harness, submission, tasks, telemetry,
    backend, study_bridge, verify_dashboard,
)]))
"""
        result = subprocess.run(
            [sys.executable, "-c", source, str(self.runtime / "src")],
            cwd=evaluate.FIXED_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        paths = [Path(value) for value in json.loads(result.stdout)]
        self.assertEqual(len(paths), 9)
        self.assertTrue(all(path.is_relative_to(evaluate.FIXED_ROOT) for path in paths))
        self.assertFalse(self.marker.exists())

    def test_real_evaluator_setup_selects_scope_without_model_or_gpu_calls(self):
        prepared = self.root / "prepared"
        (prepared / "source").mkdir(parents=True)
        (prepared / "source/model.py").write_text("# Fixed source fixture\n")
        (prepared / "gpu_smoke.py").write_text("# Not executed\n")
        for scope in ("docs", "api"):
            with self.subTest(scope=scope):
                output = self.root / f"run-{scope}"
                argv = [
                    "evaluate.py",
                    "--root",
                    str(output),
                    "--runtime-root",
                    str(self.runtime),
                    "--codex-wrapper",
                    str(self.root / "unused-wrapper"),
                    "--prepared-source",
                    str(prepared),
                    "--host",
                    "unused-host",
                    "--remote-root",
                    "/unused-remote",
                    "--remote-python",
                    "/unused-python",
                    "--source-revision",
                    "fixed-test-source",
                ]
                if scope == "api":
                    argv += ["--candidate-scope", scope]

                def no_actor(case, prompt, actor_output, timeout_seconds):
                    self.assertEqual(case, output / "case")
                    if scope == "api":
                        self.assertIn("client.py --help", prompt.read_text())
                        self.assertEqual(
                            (case / "client.py").read_bytes(), self.payload
                        )
                        self.assertEqual(
                            (output / "baseline/submission.py").read_bytes(),
                            self.payload,
                        )
                    else:
                        self.assertNotEqual(
                            (case / "client.py").read_bytes(), self.payload
                        )
                    return {"exit_code": 0, "timed_out": False, "launch_error": None}

                with (
                    patch("sys.argv", argv),
                    patch(
                        "benchmarks.startup.runner.run_case", side_effect=no_actor
                    ) as actor,
                    patch(
                        "benchmarks.startup.backend.FixedStartupBackend.setup"
                    ) as setup,
                    patch(
                        "benchmarks.startup.backend.FixedStartupBackend.start"
                    ) as gpu,
                ):
                    self.assertEqual(evaluate.main(), 1)
                actor.assert_called_once()
                setup.assert_not_called()
                gpu.assert_not_called()
                result = json.loads((output / "benchmark-result.json").read_text())
                self.assertEqual(result["candidate_scope"], scope)
                self.assertFalse(result["gpu_submitted"])
                self.assertFalse(result["host_worker_unresolved"])
        self.assertFalse(self.marker.exists())

    def test_api_instructions_require_discovery_without_freezing_cli(self):
        with (
            patch("sys.argv", ["evaluate.py", "--help"]),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as stopped,
        ):
            evaluate.main()
        self.assertEqual(stopped.exception.code, 0)
        self.assertIn("client.py --help", evaluate.API_FIXTURE)
        self.assertIn("submission.py --help", evaluate.API_BASELINE)
        self.assertNotIn("python3 client.py prepare", evaluate.API_FIXTURE)
        self.assertNotIn("python3 submission.py submit", evaluate.API_BASELINE)


if __name__ == "__main__":
    unittest.main()
