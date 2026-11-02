"""Both CLI adapters satisfy the same persisted Study contract."""

import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_loop import Study, initialize
from research_loop import harness as harness_module
from research_loop.harness import HarnessConfig, completed_response
from test_core import ProcessLost, read


class HarnessStudyTests(unittest.TestCase):
    def fake_cli(self, root, writable):
        executable = root.parent / "fake-cli"
        log = root.parent / "calls.jsonl"
        # Reuse the established protocol fixture while exercising real CLI processes,
        # stdin, output files, completion receipts and controller recovery.
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, pathlib, sys\n"
            f"sys.path.insert(0, {str(Path(__file__).parent.resolve())!r})\n"
            "from test_core import RecordedHarness, read\n"
            "from research_loop.protocol import SCHEMAS\n"
            f"root = pathlib.Path({str(root)!r})\n"
            "args = sys.argv[1:]\n"
            "if '--list-models' in args:\n"
            "    print('openai-codex gpt-6.1-sol 100k')\n"
            "    sys.exit(0)\n"
            "prompt = sys.stdin.read()\n"
            "if '--output-last-message' in args:\n"
            "    run = pathlib.Path(args[args.index('--output-last-message') + 1]).parent\n"
            "    writable = args[args.index('--sandbox') + 1] == 'workspace-write'\n"
            "else:\n"
            "    run = next(p.parent for p in root.rglob('process.json') if read(p).get('status') == 'running')\n"
            "    writable = 'write' in args[args.index('--tools') + 1].split(',')\n"
            "schema = read(run / 'schema.json')\n"
            "phase = next(name for name, value in SCHEMAS.items() if value == schema)\n"
            "context = read(run / 'context.json')\n"
            "fixture = RecordedHarness()\n"
            "fixture.write_code = writable\n"
            "response = fixture.response(phase, context, {'workspace': pathlib.Path.cwd()})\n"
            f"with pathlib.Path({str(log)!r}).open('a') as handle:\n"
            "    handle.write(json.dumps({'phase': phase, 'writable': writable, 'cwd': str(pathlib.Path.cwd()), 'prompt': prompt, 'run': str(run)}) + '\\n')\n"
            "if '--output-last-message' in args:\n"
            "    (run / 'response.json').write_text(json.dumps(response))\n"
            "    print(json.dumps({'type': 'thread.started'}))\n"
            "else:\n"
            "    print(json.dumps({'type': 'agent_start'}))\n"
            "    print(json.dumps({'type': 'message_end', 'message': {'role': 'assistant', 'provider': 'openai-codex', 'model': 'gpt-6.1-sol', 'thinkingLevel': 'medium', 'stopReason': 'stop', 'content': [{'type': 'text', 'text': json.dumps(response)}]}}))\n"
            "    print(json.dumps({'type': 'agent_settled', 'aborted': False}))\n"
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        return executable, log

    def test_real_cli_study_parity_and_completed_execute_recovery(self):
        outcomes = []
        for backend in ("codex", "pi"):
            for writable in (False, True):
                for interrupt in (False, True):
                    with self.subTest(
                        backend=backend, writable=writable, interrupt=interrupt
                    ):
                        with tempfile.TemporaryDirectory() as directory:
                            root = Path(directory).resolve() / "study"
                            executable, log = self.fake_cli(root, writable)
                            initialize(
                                root,
                                goal="Record a bounded local result.",
                                harness=HarnessConfig(
                                    backend=backend,
                                    executable=str(executable),
                                    timeout_seconds=30,
                                ),
                                allow_write=writable,
                                max_calls=6,
                                max_seconds=120,
                                strategy={"selection": "direct", "review": "full"},
                            )
                            study = Study(root)
                            publish = harness_module.publish_completed_response
                            interrupted = False

                            def crash_after_execute(run_dir, response):
                                nonlocal interrupted
                                publish(run_dir, response)
                                if run_dir.parent.name == "execute" and not interrupted:
                                    interrupted = True
                                    raise ProcessLost()

                            if interrupt:
                                with patch.object(
                                    harness_module,
                                    "publish_completed_response",
                                    side_effect=crash_after_execute,
                                ):
                                    with self.assertRaises(ProcessLost):
                                        study.run()
                                before = study.status()
                                self.assertEqual(before["call_count"], 5)
                                attempt = root / before["active_attempt"]
                                self.assertEqual(
                                    completed_response(attempt)["outcome"], "completed"
                                )
                                state = Study(root).run()
                                self.assertGreaterEqual(
                                    state["elapsed_seconds"], before["elapsed_seconds"]
                                )
                            else:
                                state = study.run()
                            calls = [
                                json.loads(line)
                                for line in log.read_text().splitlines()
                            ]
                            self.assertEqual(
                                [c["phase"] for c in calls],
                                [
                                    "proposal",
                                    "proposal",
                                    "judge",
                                    "plan",
                                    "execute",
                                    "review",
                                ],
                            )
                            self.assertEqual(
                                [c["writable"] for c in calls],
                                [False, False, False, False, writable, False],
                            )
                            self.assertEqual(state["call_count"], 6)
                            self.assertEqual(
                                (state["status"], state["stop_reason"]),
                                ("stopped", "review"),
                            )
                            self.assertLess(state["elapsed_seconds"], 120)
                            self.assertEqual(study.graph()[0]["status"], "reviewed")
                            for call in calls:
                                run = Path(call["run"])
                                self.assertEqual(
                                    call["prompt"], (run / "prompt.md").read_text()
                                )
                                self.assertIsNotNone(completed_response(run))
                                process = read(run / "process.json")
                                self.assertLessEqual(process["timeout_seconds"], 30)
                                expected_cwd = (
                                    root / "iterations/0001/workspace"
                                    if writable and call["phase"] == "execute"
                                    else study.workspace
                                )
                                self.assertEqual(call["cwd"], str(expected_cwd))
                            execution = read(
                                root
                                / "iterations/0001/execute/attempt-001/response.json"
                            )
                            expected = (
                                [{"path": "model.py", "version": "v1"}]
                                if writable
                                else []
                            )
                            self.assertEqual(execution["artifacts"], expected)
                            if writable:
                                self.assertEqual(
                                    (
                                        root / "iterations/0001/workspace/model.py"
                                    ).read_text(),
                                    "version = 1\n",
                                )
                            outcomes.append(
                                (writable, read(root / state["review_ref"]), execution)
                            )
        for writable in (False, True):
            matching = [result for flag, *result in outcomes if flag == writable]
            self.assertTrue(all(result == matching[0] for result in matching))
