import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from research_loop.harness import HarnessConfig, HarnessError, make_harness


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()

    def executable(self, code):
        path = self.root / "fake-agent"
        path.write_text(f"#!{sys.executable}\n" + code)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def run_agent(self, backend, code, **kwargs):
        config = HarnessConfig(
            backend=backend, executable=self.executable(code), **kwargs
        )
        return make_harness(config).run(
            prompt="Evaluate candidate.",
            schema={"type": "object"},
            workspace=self.workspace,
            run_dir=self.run_dir,
        )

    def pi_code(self, *, available=True, stop_reason="stop", aborted=False):
        message = {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "provider": "openai-codex",
                "model": "gpt-6.1-sol",
                "responseModel": "service-reported-alias",
                "stopReason": stop_reason,
                "thinkingLevel": "medium",
                "content": [{"type": "text", "text": '{"ok": true}'}],
            },
        }
        settled = {"type": "agent_settled", "aborted": aborted}
        catalog_row = (
            "openai-codex gpt-6.1-sol 100k" if available else "openai gpt-6.1-sol 100k"
        )
        return (
            "import json, pathlib, sys\n"
            "if '--list-models' in sys.argv:\n"
            f"    print({catalog_row!r})\n"
            "else:\n"
            "    pathlib.Path('request-started').write_text('yes')\n"
            f"    print(json.dumps({message!r}))\n"
            f"    print(json.dumps({settled!r}))\n"
        )

    def test_configuration_rejects_fallback_and_unknown_values(self):
        for changes in (
            {"model": "gpt-5"},
            {"backend": "auto"},
            {"reasoning": "ultra"},
            {"timeout_seconds": True},
            {"timeout_seconds": 0},
            {"executable": ""},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                HarnessConfig(**changes)

    def test_codex_uses_stdin_and_preserves_evidence(self):
        result = self.run_agent(
            "codex",
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "assert sys.stdin.read() == 'Evaluate candidate.'\n"
            "assert args[-1] == '-'\n"
            "assert args[args.index('--model') + 1] == 'gpt-6.1-sol'\n"
            "assert args[args.index('--sandbox') + 1] == 'read-only'\n"
            "pathlib.Path(args[args.index('--output-last-message') + 1]).write_text('{\"ok\":true}')\n"
            "print(json.dumps({'type': 'thread.started'}))\n",
        )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            json.loads((self.run_dir / "stdout.jsonl").read_text()),
            {"type": "thread.started"},
        )
        process = json.loads((self.run_dir / "process.json").read_text())
        self.assertEqual(process["status"], "succeeded")
        self.assertEqual(process["returncode"], 0)
        self.assertEqual(process["requested_model"], "gpt-6.1-sol")
        self.assertEqual(process["requested_reasoning"], "medium")
        self.assertEqual(process["model"], process["requested_model"])
        self.assertEqual(process["model_verification"], "unverified")
        self.assertEqual(process["reasoning_verification"], "unverified")
        self.assertIsInstance(process["pid"], int)
        self.assertIsNotNone(process["ended"])

    def test_pi_success_requires_catalog_and_settled(self):
        self.assertEqual(self.run_agent("pi", self.pi_code()), {"ok": True})
        self.assertTrue((self.workspace / "request-started").exists())
        self.assertTrue((self.run_dir / "preflight.stdout.jsonl").exists())
        process = json.loads((self.run_dir / "process.json").read_text())
        self.assertEqual(
            process["model_verification"],
            {
                "status": "matched",
                "source": "Pi assistant message.provider and message.model",
                "provider": "openai-codex",
                "model": "gpt-6.1-sol",
            },
        )
        self.assertEqual(process["response_model"], "service-reported-alias")
        self.assertEqual(process["response_model_verification"], "unverified")
        self.assertEqual(
            process["reasoning_verification"],
            {"thinkingLevel": "medium", "providerThinkingLevel": "unverified"},
        )

    def test_pi_error_or_abort_with_exit_zero_is_failure(self):
        for fields in ({"stop_reason": "error"}, {"aborted": True}):
            with self.subTest(fields=fields), self.assertRaises(HarnessError):
                self.run_agent("pi", self.pi_code(**fields))
        process = json.loads((self.run_dir / "process.json").read_text())
        self.assertEqual(process["status"], "failed")
        self.assertEqual(process["returncode"], 0)

    def test_pi_missing_exact_model_does_not_start_request(self):
        with self.assertRaisesRegex(HarnessError, "no request was started"):
            self.run_agent("pi", self.pi_code(available=False))
        self.assertFalse((self.workspace / "request-started").exists())

    def test_timeout_records_local_termination(self):
        with self.assertRaisesRegex(HarnessError, "remote request state is unknown"):
            self.run_agent(
                "codex",
                "import time\nprint('started', flush=True)\ntime.sleep(30)\n",
                timeout_seconds=1,
            )
        process = json.loads((self.run_dir / "process.json").read_text())
        self.assertEqual(process["status"], "timed_out")
        self.assertLess(process["returncode"], 0)
        self.assertEqual((self.run_dir / "stdout.jsonl").read_text(), "started\n")
        self.assertIsNotNone(process["ended"])


if __name__ == "__main__":
    unittest.main()
