import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from research_loop import Study, initialize
from research_loop.cli import main
from research_loop.harness import HarnessConfig


class CliInterruptTests(unittest.TestCase):
    def test_sigterm_cleans_owned_agent_group_and_read_only_run_resumes(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve()
            agent = folder / "fake-agent"
            agent.write_text(
                f"#!{sys.executable}\n"
                "import json, os, pathlib, signal, subprocess, sys, time\n"
                "sys.stdin.read()\n"
                "if not pathlib.Path('resume').exists():\n"
                "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "    def terminate(signum, frame):\n"
                "        child.wait(timeout=5)\n"
                "        pathlib.Path('child-reaped').write_text(str(child.returncode))\n"
                "        sys.exit(0)\n"
                "    signal.signal(signal.SIGTERM, terminate)\n"
                "    pathlib.Path('agent-started').write_text(json.dumps({'pid': os.getpid(), 'child': child.pid, 'pgid': os.getpgrp()}))\n"
                "    time.sleep(60)\n"
                "else:\n"
                "    args = sys.argv\n"
                "    schema = json.loads(pathlib.Path(args[args.index('--output-schema') + 1]).read_text())\n"
                "    if 'route_id' in schema['properties']:\n"
                "        result = dict(markdown='Bounded evidence.', route_id='new', route_title='Question', action='Measure', base_experiment=None, references=[])\n"
                "    else:\n"
                "        result = dict(markdown='No further call needed.', decision='stop', choice=None, action='Stop', goal_status='undetermined', stop_reason='fixture complete')\n"
                "    pathlib.Path(args[args.index('--output-last-message') + 1]).write_text(json.dumps(result))\n"
            )
            agent.chmod(0o755)
            root = folder / "study"
            initialize(
                root,
                goal="Retain interrupted read-only evidence.",
                harness=HarnessConfig(executable=str(agent), timeout_seconds=60),
                strategy={"selection": "direct", "review": "full"},
                max_seconds=60,
            )
            workspace = root / "workspace"
            command = [sys.executable, "-m", "research_loop", "run", str(root)]
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            group = None
            try:
                started = workspace / "agent-started"
                deadline = time.monotonic() + 10
                while not started.exists():
                    if process.poll() is not None or time.monotonic() >= deadline:
                        self.fail(
                            f"Agent did not start; controller status {process.poll()}"
                        )
                    time.sleep(0.01)
                group = json.loads(started.read_text())
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 130, (stdout, stderr))
                self.assertIn("Interrupted; run again", stderr)
                self.assertEqual((workspace / "child-reaped").read_text(), "-15")
                for pid in (group["pid"], group["child"]):
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
                with self.assertRaises(ProcessLookupError):
                    os.killpg(group["pgid"], 0)

                state = Study(root).status()
                self.assertEqual(
                    (state["status"], state["phase"]), ("needs_attention", "selection")
                )
                attempt = root / state["active_attempt"]
                receipt = json.loads((attempt / "process.json").read_text())
                self.assertEqual(receipt["status"], "interrupted")
                self.assertEqual(receipt["pid"], group["pid"])
                self.assertFalse((attempt / "completed.json").exists())
                self.assertTrue((attempt / "prompt.md").exists())

                (workspace / "resume").touch()
                resumed = subprocess.run(
                    command, capture_output=True, text=True, timeout=10
                )
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                final = json.loads(resumed.stdout)
                self.assertEqual(final["stop_reason"], "fixture complete")
                self.assertEqual(final["call_count"], 4)
                self.assertTrue(
                    (attempt.parent / "attempt-002/completed.json").exists()
                )
                self.assertTrue((attempt / "process.json").exists())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
                if group:
                    try:
                        os.killpg(group["pgid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_main_restores_sigterm_handler_after_run(self):
        original = signal.getsignal(signal.SIGTERM)
        with patch("research_loop.cli.Study") as study:
            study.return_value.run.return_value = {"status": "stopped"}
            self.assertEqual(main(["run", "/unused"]), 0)
        self.assertIs(signal.getsignal(signal.SIGTERM), original)

    def test_main_in_worker_thread_does_not_change_signal_handler(self):
        result = []
        with (
            patch("research_loop.cli.Study") as study,
            patch("research_loop.cli.signal.signal") as set_signal,
        ):
            study.return_value.run.return_value = {"status": "stopped"}
            worker = threading.Thread(
                target=lambda: result.append(main(["run", "/unused"]))
            )
            worker.start()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(result, [0])
        set_signal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
