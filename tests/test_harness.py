import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_loop.diagnostics import read_log_page
from research_loop.harness import (
    HarnessConfig,
    HarnessError,
    completed_response,
    make_harness,
    publish_completed_response,
)


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

    def test_terra_is_preserved_without_model_substitution(self):
        config = HarnessConfig(model="gpt-6.1-terra", reasoning="medium")
        self.assertEqual(config.model, "gpt-6.1-terra")

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
        self.assertEqual(completed_response(self.run_dir), result)

    def test_completion_survives_interrupted_final_bookkeeping(self):
        from research_loop import harness

        write_json = harness._write_json

        def fail_final_status(path, value):
            if path.name == "process.json" and value.get("status") == "succeeded":
                raise OSError("simulated interruption before final status commit")
            write_json(path, value)

        with patch.object(harness, "_write_json", side_effect=fail_final_status):
            with self.assertRaisesRegex(OSError, "simulated interruption"):
                self.run_agent("pi", self.pi_code())
        self.assertEqual(
            json.loads((self.run_dir / "process.json").read_text())["status"],
            "running",
        )
        self.assertEqual(completed_response(self.run_dir), {"ok": True})

    def test_partial_response_is_not_a_receipt_and_legacy_success_is_readable(self):
        response = {"ok": True}
        (self.run_dir / "response.json").write_text(json.dumps(response))
        process = self.run_dir / "process.json"
        for status in ("running", "failed", "timed_out"):
            process.write_text(json.dumps({"status": status}))
            self.assertIsNone(completed_response(self.run_dir))
        process.write_text(json.dumps({"status": "succeeded"}))
        self.assertEqual(completed_response(self.run_dir), response)
        process.write_text(json.dumps({"status": "succeeded", "run_id": "new"}))
        self.assertIsNone(completed_response(self.run_dir))

    def test_explicit_custom_receipt_and_reused_run_directory(self):
        publish_completed_response(self.run_dir, {"accepted": True})
        self.assertEqual(completed_response(self.run_dir), {"accepted": True})
        (self.run_dir / "process.json").write_text(
            json.dumps({"status": "running", "run_id": "next-attempt"})
        )
        self.assertIsNone(completed_response(self.run_dir))

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

    def test_failed_cli_preserves_raw_and_full_public_diagnostics(self):
        secret = "private-owner-value"
        executable = self.executable(
            "import sys\n"
            f"print('first-output ' + {secret!r})\n"
            "print('x' * 20000)\n"
            "print('candidate command failed', file=sys.stderr)\n"
            "sys.exit(9)\n"
        )
        harness = make_harness(HarnessConfig(executable=executable))
        harness.diagnostic_secrets = (secret,)
        with self.assertRaisesRegex(HarnessError, "exited 9"):
            harness.run(
                prompt="Execute within the original workspace.",
                schema={"type": "object"},
                workspace=self.workspace,
                run_dir=self.run_dir,
                writable=True,
            )
        detail = json.loads((self.run_dir / "failure-diagnostics.json").read_text())
        self.assertEqual(detail["owner"], "pipeline")
        self.assertEqual(detail["effects"], "unknown")
        self.assertEqual(detail["returncode"], 9)
        self.assertNotIn(secret, json.dumps(detail))
        self.assertIn(secret, (self.run_dir / "stdout.jsonl").read_text())
        stdout = next(log for log in detail["logs"] if log["stream"] == "stdout.jsonl")
        page = read_log_page(detail["directory"], stdout["log_id"], limit=1024)
        self.assertIn("first-output [REDACTED]", page["text"])
        self.assertFalse(page["eof"])
        self.assertIsNone(completed_response(self.run_dir))

    def test_diagnostic_io_failure_does_not_replace_primary_cli_failure(self):
        with (
            patch(
                "research_loop.harness.CommandDiagnostics.record_files",
                side_effect=OSError("diagnostic disk unavailable"),
            ),
            self.assertRaisesRegex(HarnessError, "exited 7"),
        ):
            self.run_agent("codex", "import sys\nsys.exit(7)\n")
        process = json.loads((self.run_dir / "process.json").read_text())
        self.assertIn("exited 7", process["error"])
        self.assertIn("diagnostic disk unavailable", str(process["diagnostic_error"]))


if __name__ == "__main__":
    unittest.main()
