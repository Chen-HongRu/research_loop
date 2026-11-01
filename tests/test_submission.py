import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from research_loop import submission


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)

    def write(self, name, value):
        (self.workspace / name).write_text(json.dumps(value))

    def test_prepublication_failure_can_be_retried(self):
        with patch.object(submission.os, "fsync", side_effect=OSError("disk error")):
            with self.assertRaises(submission.SubmissionError):
                submission.submit(self.workspace)
        self.assertFalse((self.workspace / submission.STAGING).exists())
        self.assertFalse((self.workspace / submission.REQUEST).exists())
        self.assertEqual(submission.status(self.workspace), {"status": "not_submitted"})
        self.assertEqual(submission.submit(self.workspace)["status"], "pending")

    def test_postpublication_failure_retains_request(self):
        with patch.object(
            submission, "_sync_directory", side_effect=OSError("disk error")
        ):
            with self.assertRaises(submission.SubmissionError):
                submission.submit(self.workspace)
        request = self.workspace / submission.REQUEST
        original_inode = request.stat().st_ino
        self.assertEqual(submission.submit(self.workspace)["status"], "pending")
        self.assertEqual(request.stat().st_ino, original_inode)

    def test_claim_sync_failure_retains_accepted_receipt(self):
        submission.submit(self.workspace)
        with patch.object(
            submission, "_sync_directory", side_effect=OSError("disk error")
        ):
            with self.assertRaises(submission.SubmissionError):
                submission.claim_request(self.workspace)
        self.assertEqual(submission.submit(self.workspace)["status"], "accepted")
        self.assertIsNone(submission.claim_request(self.workspace))
        self.assertFalse((self.workspace / submission.REQUEST).exists())

    def test_repeated_and_concurrent_submission_is_idempotent(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(lambda _: submission.submit(self.workspace), range(24))
            )
        self.assertEqual(results, [{"status": "pending", "action": "evaluate"}] * 24)
        request = self.workspace / submission.REQUEST
        original_inode = request.stat().st_ino
        self.assertEqual(json.loads(request.read_text()), {"action": "evaluate"})
        self.assertEqual(submission.submit(self.workspace), results[0])
        self.assertEqual(request.stat().st_ino, original_inode)
        self.assertFalse((self.workspace / submission.STAGING).exists())

    def test_claim_races_duplicate_submission_and_is_returned_once(self):
        submission.submit(self.workspace)
        barrier = threading.Barrier(12)

        def operation(index):
            barrier.wait()
            if index % 2:
                return "claim", submission.claim_request(self.workspace)
            return "submit", submission.submit(self.workspace)

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(operation, range(12)))
        claims = [
            value for kind, value in results if kind == "claim" and value is not None
        ]
        self.assertEqual(claims, [{"action": "evaluate"}])
        self.assertFalse((self.workspace / submission.REQUEST).exists())
        self.assertEqual(
            submission.status(self.workspace),
            {"status": "accepted", "action": "evaluate"},
        )
        self.assertEqual(submission.submit(self.workspace)["status"], "accepted")
        self.assertIsNone(submission.claim_request(self.workspace))

    def test_receipt_and_result_retain_terminal_state(self):
        for result_status, state in (
            ("completed", "completed"),
            ("error", "failed"),
            ("candidate_failed", "failed"),
            ("failed", "failed"),
        ):
            with self.subTest(result_status=result_status):
                with tempfile.TemporaryDirectory() as directory:
                    submission.submit(directory, "import")
                    self.assertEqual(
                        submission.claim_request(directory), {"action": "import"}
                    )
                    result = {"status": result_status, "detail": "original result"}
                    Path(directory, submission.RESULT).write_text(json.dumps(result))
                    expected = {"status": state, "action": "import", "result": result}
                    self.assertEqual(submission.status(directory), expected)
                    self.assertEqual(submission.submit(directory, "import"), expected)
                    self.assertIsNone(submission.claim_request(directory))
                    self.assertTrue(Path(directory, submission.RECEIPT).exists())
                    self.assertFalse(Path(directory, submission.REQUEST).exists())

    def test_result_without_receipt_never_publishes(self):
        self.write(submission.RESULT, {"status": "completed"})
        self.assertEqual(submission.submit(self.workspace)["status"], "completed")
        self.assertFalse((self.workspace / submission.REQUEST).exists())

    def test_conflicting_action_rejected_before_and_after_claim(self):
        submission.submit(self.workspace)
        for claimed in (False, True):
            if claimed:
                submission.claim_request(self.workspace)
            with self.assertRaisesRegex(submission.SubmissionError, "Conflicting"):
                submission.submit(self.workspace, "import")
        with self.assertRaises(submission.SubmissionError):
            submission.submit(self.workspace, "other")

    def test_malformed_files_and_ambiguous_states_rejected(self):
        for name, content in (
            (submission.REQUEST, "{"),
            (submission.REQUEST, "[]"),
            (submission.REQUEST, '{"action":"evaluate","extra":true}'),
            (submission.RECEIPT, '{"action":[]}'),
            (submission.RESULT, '{"status":"running"}'),
            (submission.STAGING, "unfinished"),
        ):
            with self.subTest(name=name, content=content):
                path = self.workspace / name
                path.write_text(content)
                try:
                    with self.assertRaises(submission.SubmissionError):
                        submission.submit(self.workspace)
                finally:
                    path.unlink()
        self.write(submission.REQUEST, {"action": "evaluate"})
        self.write(submission.RECEIPT, {"action": "evaluate"})
        with self.assertRaises(submission.SubmissionError):
            submission.claim_request(self.workspace)

    def test_symlinked_coordination_files_rejected(self):
        target = self.workspace / "outside"
        target.write_text('{"action":"evaluate"}')
        for name in submission.INTERFACE_FILES:
            with self.subTest(name=name):
                link = self.workspace / name
                if name == submission.LOCK:
                    link.unlink(missing_ok=True)
                link.symlink_to(target)
                try:
                    with self.assertRaises(submission.SubmissionError):
                        submission.submit(self.workspace)
                finally:
                    link.unlink()
        self.assertEqual(target.read_text(), '{"action":"evaluate"}')

    def test_oversized_request_rejected_before_reading(self):
        request = self.workspace / submission.REQUEST
        request.write_text('{"action":"evaluate"}' + " " * 1024)
        with patch.object(submission.json, "load") as load:
            with self.assertRaisesRegex(
                submission.SubmissionError, "exceeds 1024 bytes"
            ):
                submission.claim_request(self.workspace)
            load.assert_not_called()
        self.assertTrue(request.exists())
        self.assertFalse((self.workspace / submission.RECEIPT).exists())

    def test_nonregular_interface_rejected_without_blocking(self):
        os.mkfifo(self.workspace / submission.REQUEST)
        with self.assertRaises(submission.SubmissionError):
            submission.submit(self.workspace)

    def test_cli_direct_file_from_independent_workspace(self):
        script = Path(submission.__file__).resolve()
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        for command, state in (
            ("status", "not_submitted"),
            ("submit", "pending"),
            ("submit", "pending"),
            ("status", "pending"),
        ):
            process = subprocess.run(
                [sys.executable, str(script), command, "--workspace", "."],
                cwd=self.workspace,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(json.loads(process.stdout)["status"], state)
        (self.workspace / submission.REQUEST).write_text("invalid")
        process = subprocess.run(
            [sys.executable, str(script), "submit", "--workspace", "."],
            cwd=self.workspace,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(process.returncode, 1)
        self.assertIn("error", json.loads(process.stderr))


if __name__ == "__main__":
    unittest.main()
