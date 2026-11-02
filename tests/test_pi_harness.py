"""Pi's stream contract, tested through a local executable and real pipes."""

import json
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

from research_loop.harness import (
    HarnessConfig,
    HarnessError,
    completed_response,
    make_harness,
)


def assistant(**changes):
    return {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "provider": "openai-codex",
            "model": "gpt-6.1-sol",
            "stopReason": "stop",
            "thinkingLevel": "medium",
            "content": [{"type": "text", "text": '{"ok": true}'}],
            **changes,
        },
    }


SETTLED = {"type": "agent_settled", "aborted": False}


class PiStreamTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()

    def run_stream(
        self, events, *, code="", prompt="Evaluate.", writable=False, timeout=10
    ):
        raw = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)
        executable = self.root / "pi"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, pathlib, sys, time\n"
            "args = sys.argv[1:]\n"
            "if '--list-models' in args:\n"
            "    print('openai-codex gpt-6.1-sol 100k')\n"
            "else:\n"
            "    prompt = sys.stdin.read()\n"
            "    pathlib.Path('invocation.json').write_text(json.dumps({'argv': args, 'prompt': prompt, 'cwd': str(pathlib.Path.cwd())}))\n"
            + "".join("    " + line + "\n" for line in code.splitlines())
            + f"    sys.stdout.write({raw!r})\n"
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        return make_harness(
            HarnessConfig(
                backend="pi", executable=str(executable), timeout_seconds=timeout
            )
        ).run(
            prompt=prompt,
            schema={"type": "object", "description": "合同"},
            workspace=self.workspace,
            run_dir=self.run_dir,
            writable=writable,
        )

    def test_large_prompt_unicode_and_explicit_cli_capabilities(self):
        prompt = "Bounded task\u2028with paragraphs\u2029" + "内容" * 100_000
        response = {"text": "first\u2028second\u2029third"}
        event = assistant(
            content=[{"type": "text", "text": json.dumps(response, ensure_ascii=False)}]
        )
        for writable in (False, True):
            with self.subTest(writable=writable):
                self.assertEqual(
                    self.run_stream([event, SETTLED], prompt=prompt, writable=writable),
                    response,
                )
                invocation = json.loads(
                    (self.workspace / "invocation.json").read_text()
                )
                args = invocation["argv"]
                self.assertEqual(invocation["cwd"], str(self.workspace))
                self.assertEqual(
                    invocation["prompt"], (self.run_dir / "prompt.md").read_text()
                )
                self.assertTrue(invocation["prompt"].startswith(prompt))
                self.assertIn('"description": "合同"', invocation["prompt"])
                self.assertNotIn(prompt, args)
                for flag in (
                    "--no-extensions",
                    "--no-skills",
                    "--no-prompt-templates",
                    "--no-mcp",
                    "--no-approve",
                    "--no-session",
                    "--print",
                ):
                    self.assertIn(flag, args)
                for flag, value in (
                    ("--provider", "openai-codex"),
                    ("--model", "gpt-6.1-sol"),
                    ("--thinking", "medium"),
                    ("--mode", "json"),
                ):
                    self.assertEqual(args[args.index(flag) + 1], value)
                tools = args[args.index("--tools") + 1].split(",")
                self.assertEqual(
                    set(tools),
                    {"read", "grep", "find", "ls"}
                    | ({"bash", "edit", "write"} if writable else set()),
                )

    def test_multiple_runs_accept_only_final_successful_run(self):
        events = [
            {"type": "agent_start"},
            assistant(stopReason="error", errorMessage="retryable"),
            {"type": "agent_settled", "aborted": True},
            {"type": "agent_start"},
            assistant(),
            SETTLED,
        ]
        self.assertEqual(self.run_stream(events), {"ok": True})
        self.assertEqual(completed_response(self.run_dir), {"ok": True})

    def test_automatic_retry_accepts_only_settled_success(self):
        events = [
            {"type": "agent_start"},
            assistant(stopReason="error", errorMessage="retryable"),
            {"type": "agent_end", "willRetry": True},
            {"type": "auto_retry_start", "attempt": 1},
            {"type": "agent_start"},
            assistant(),
            {"type": "auto_retry_end", "success": True},
            {"type": "agent_end", "willRetry": False},
            SETTLED,
        ]
        self.assertEqual(self.run_stream(events), {"ok": True})
        self.assertEqual(completed_response(self.run_dir), {"ok": True})

    def test_invalid_terminal_streams_leave_diagnostics_without_receipt(self):
        cases = {
            "unsettled": [assistant()],
            "unfinished_assistant": [
                assistant(),
                {"type": "message_start", "message": {"role": "assistant"}},
                SETTLED,
            ],
            "unfinished_turn": [assistant(), {"type": "turn_start"}, SETTLED],
            "duplicate_settlement": [assistant(), SETTLED, SETTLED],
            "activity_after_settlement": [
                assistant(),
                SETTLED,
                {"type": "message_update"},
            ],
            "empty_retry": [assistant(), SETTLED, {"type": "agent_start"}, SETTLED],
            "last_message_is_tool": [
                assistant(),
                {"type": "message_end", "message": {"role": "toolResult"}},
                SETTLED,
            ],
            "failure_after_success": [
                assistant(),
                assistant(stopReason="error"),
                SETTLED,
            ],
            "provider": [assistant(provider="openai"), SETTLED],
            "model": [assistant(model="gpt-6-luna"), SETTLED],
            "reasoning": [assistant(thinkingLevel="high"), SETTLED],
            "non_object_event": [[], assistant(), SETTLED],
        }
        for name, events in cases.items():
            with self.subTest(name=name):
                self.run_dir = self.root / name
                self.run_dir.mkdir()
                with self.assertRaises(HarnessError):
                    self.run_stream(events)
                self.assertIsNone(completed_response(self.run_dir))
                process = json.loads((self.run_dir / "process.json").read_text())
                self.assertEqual(process["status"], "failed")
                self.assertEqual(process["returncode"], 0)
                detail = json.loads(
                    (self.run_dir / "failure-diagnostics.json").read_text()
                )
                self.assertTrue(detail["logs"])

    def test_missing_actual_reasoning_is_rejected_and_provider_mapping_is_retained(
        self,
    ):
        missing = assistant()
        del missing["message"]["thinkingLevel"]
        with self.assertRaises(HarnessError):
            self.run_stream([missing, SETTLED])
        self.assertIsNone(completed_response(self.run_dir))
        self.assertEqual(
            self.run_stream(
                [assistant(providerThinkingLevel="provider-mapping"), SETTLED]
            ),
            {"ok": True},
        )

    def test_corrupt_or_truncated_jsonl_is_not_completion(self):
        for raw in ("not-json\n", '{"type": "message_update"}'):
            with self.subTest(raw=raw), self.assertRaises(HarnessError):
                self.run_stream(
                    [assistant(), SETTLED], code=f"sys.stdout.write({raw!r})"
                )
            self.assertIsNone(completed_response(self.run_dir))
            self.assertTrue((self.run_dir / "failure-diagnostics.json").exists())

    def test_preflight_and_request_share_timeout(self):
        executable = self.root / "slow-pi"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import sys, time\n"
            "time.sleep(0.65)\n"
            "if '--list-models' in sys.argv:\n"
            "    print('openai-codex gpt-6.1-sol 100k')\n"
            "else:\n"
            "    sys.stdin.read()\n"
            f"    print({json.dumps(assistant())!r})\n"
            f"    print({json.dumps(SETTLED)!r})\n"
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        started = time.monotonic()
        with self.assertRaises(HarnessError):
            make_harness(
                HarnessConfig(
                    backend="pi", executable=str(executable), timeout_seconds=1
                )
            ).run(
                prompt="Evaluate.",
                schema={},
                workspace=self.workspace,
                run_dir=self.run_dir,
            )
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(
            json.loads((self.run_dir / "process.json").read_text())["status"],
            "timed_out",
        )
        self.assertIsNone(completed_response(self.run_dir))
