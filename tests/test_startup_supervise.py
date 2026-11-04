import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from benchmarks.startup.supervise import supervise


class SupervisorTests(unittest.TestCase):
    def test_completed_study_survives_mirror_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "study"
            code = f"from pathlib import Path; p=Path({str(root)!r}); p.mkdir(); (p/'summary.json').write_text('{{\"status\":\"completed\"}}')"
            with patch(
                "benchmarks.startup.supervise.mirror", side_effect=OSError("offline")
            ):
                result = supervise(
                    root, 5, "unused", "/unused", [sys.executable, "-c", code]
                )
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["artifact_mirror_status"], "failed")
            self.assertEqual(
                json.loads(root.with_name("study-supervisor.json").read_text()), result
            )

    def test_timeout_reaps_owned_process_and_records_unknown_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "study"
            result = supervise(
                root,
                0.1,
                "unused",
                "/unused",
                [sys.executable, "-c", "import time; time.sleep(20)"],
            )
            self.assertEqual(result["status"], "timeout_needs_reconciliation")
            self.assertIsNotNone(result["exit_code"])

    def test_status_write_failure_reaps_live_child(self):
        from benchmarks.startup import supervise as module
        import os

        original = module.publish
        calls = []

        def fail_once(path, value):
            calls.append(dict(value))
            if len(calls) == 2:
                raise OSError("status write interrupted")
            original(path, value)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "study"
            with patch.object(module, "publish", side_effect=fail_once):
                result = supervise(
                    root,
                    5,
                    "unused",
                    "/unused",
                    [sys.executable, "-c", "import time; time.sleep(20)"],
                )
            self.assertEqual(result["status"], "supervisor_error")
            with self.assertRaises(ProcessLookupError):
                os.kill(result["child_pid"], 0)

    def test_cli_sigterm_records_terminal_and_reaps_child(self):
        import os
        import signal
        import subprocess
        import time
        from benchmarks.startup import supervise as module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "study"
            receipt = root.with_name("study-supervisor.json")
            process = subprocess.Popen(
                [
                    sys.executable,
                    module.__file__,
                    "--root",
                    str(root),
                    "--budget-seconds",
                    "10",
                    "--host",
                    "unused",
                    "--remote-archive",
                    "/unused",
                    "--",
                    sys.executable,
                    "-c",
                    "import time; time.sleep(20)",
                ],
                stdout=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if receipt.exists() and json.loads(receipt.read_text()).get(
                        "child_pid"
                    ):
                        break
                    time.sleep(0.01)
                self.assertTrue(receipt.exists())
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=5)
                result = json.loads(receipt.read_text())
                self.assertEqual(result["status"], "supervisor_error")
                with self.assertRaises(ProcessLookupError):
                    os.kill(result["child_pid"], 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_recovery_keeps_previous_supervisor_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "study"
            root.mkdir()
            (root / "summary.json").write_text(json.dumps({"status": "completed"}))
            original = root.with_name("study-supervisor.json")
            original.write_text(json.dumps({"status": "failed", "pid": 123}))
            with patch("benchmarks.startup.supervise.mirror"):
                result = supervise(
                    root,
                    5,
                    "unused",
                    "/unused",
                    [sys.executable, "-c", "pass"],
                    attempt_id="recovery-001",
                )
            self.assertEqual(result["status"], "completed")
            self.assertEqual(
                json.loads(original.read_text()), {"status": "failed", "pid": 123}
            )
            self.assertTrue(
                root.with_name("study-recovery-001-supervisor.json").exists()
            )
