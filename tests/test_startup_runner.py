import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from benchmarks.startup.runner import run_case


class StartupRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.case = self.root / "case"
        self.case.mkdir()
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("Synthetic benchmark prompt")
        self.output = self.root / "output"

    def fake(self, source):
        executable = self.root / "fake-codex"
        executable.write_text(f"#!{sys.executable}\n" + source)
        executable.chmod(0o755)
        return str(executable)

    def test_fresh_restricted_invocation_and_event_totals(self):
        executable = self.fake(
            "import json, os, pathlib, sys, tempfile\n"
            "assert sys.stdin.read() == 'Synthetic benchmark prompt'\n"
            "args = sys.argv\n"
            "assert pathlib.Path.cwd() == pathlib.Path(args[args.index('-C') + 1])\n"
            "assert pathlib.Path(os.environ['TMPDIR']) == pathlib.Path.cwd() / '.tmp'\n"
            "with tempfile.NamedTemporaryFile() as temporary:\n"
            "    assert pathlib.Path(temporary.name).parent == pathlib.Path.cwd() / '.tmp'\n"
            "pathlib.Path(args[args.index('-o') + 1]).write_text('Done')\n"
            "for event in [\n"
            " {'type': 'item.started', 'item': {'id': '1', 'type': 'command_execution'}},\n"
            " {'type': 'item.completed', 'item': {'id': '1', 'type': 'command_execution'}},\n"
            " {'type': 'item.completed', 'item': {'id': '2', 'type': 'command_execution'}},\n"
            " {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 3, 'cached_input_tokens': 2}},\n"
            " {'type': 'turn.completed', 'usage': {'input_tokens': 5, 'output_tokens': 1}},\n"
            "]: print(json.dumps(event), flush=True)\n"
            "print('invalid event')\n"
            "print('diagnostic', file=sys.stderr)\n"
        )
        result = run_case(
            self.case, self.prompt, self.output, codex_executable=executable
        )
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["timed_out"])
        self.assertFalse(result["model_identity_verified"])
        self.assertEqual(result["requested_model"], "gpt-6.1-terra")
        self.assertEqual(result["tool_command_count"], 2)
        self.assertEqual(result["token_usage"]["input_tokens"], 15)
        self.assertEqual(result["token_usage"]["output_tokens"], 4)
        self.assertEqual(result["token_usage"]["cached_input_tokens"], 2)
        self.assertEqual(result["invalid_event_lines"], 1)
        self.assertEqual((self.output / "final.txt").read_text(), "Done")
        self.assertEqual((self.output / "stderr.txt").read_text(), "diagnostic\n")
        self.assertEqual(json.loads((self.output / "result.json").read_text()), result)
        argv = result["argv"]
        for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
        self.assertIn("sandbox_workspace_write.network_access=false", argv)
        self.assertIn("sandbox_workspace_write.exclude_slash_tmp=true", argv)
        self.assertIn("sandbox_workspace_write.exclude_tmpdir_env_var=true", argv)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)

    def test_timeout_kills_term_resistant_process_group(self):
        executable = self.fake(
            "import json, pathlib, signal, subprocess, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "signal.signal(signal.SIGCHLD, signal.SIG_IGN)\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_DFL); time.sleep(60)'])\n"
            "pathlib.Path('child.pid').write_text(str(child.pid))\n"
            "print(json.dumps({'type': 'item.started', 'item': {'id': 'active', 'type': 'command_execution'}}), flush=True)\n"
            "time.sleep(60)\n"
        )
        result = run_case(
            self.case,
            self.prompt,
            self.output,
            timeout_seconds=0.5,
            codex_executable=executable,
        )
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["exit_code"], -9)
        self.assertEqual(result["tool_command_count"], 1)
        self.assertLess(result["duration_seconds"], 6)
        child_pid = int((self.case / "child.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_launch_failure_is_recorded(self):
        result = run_case(
            self.case,
            self.prompt,
            self.output,
            codex_executable=str(self.root / "nonexistent"),
        )
        self.assertIsNone(result["exit_code"])
        self.assertIsNotNone(result["launch_error"])
        self.assertEqual(result["token_usage"], {})

    def test_rejects_inputs_inside_case_and_existing_output(self):
        internal_prompt = self.case / "prompt.txt"
        internal_prompt.write_text("Synthetic")
        with self.assertRaisesRegex(ValueError, "outside"):
            run_case(self.case, internal_prompt, self.output)
        with self.assertRaisesRegex(ValueError, "outside"):
            run_case(self.case, self.prompt, self.case / "output")
        self.output.mkdir()
        with self.assertRaises(FileExistsError):
            run_case(self.case, self.prompt, self.output)


if __name__ == "__main__":
    unittest.main()
