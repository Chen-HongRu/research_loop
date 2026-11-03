import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_loop import submission


class RetirementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.archive = self.root / "attempt-001" / "interfaces"
        self.proof = {"never_submitted": True, "proof_ref": "backend/pre-submit.json"}
        submission.submit(self.workspace)
        submission.claim_request(self.workspace)

    def retire(self, proof=None):
        return submission.retire_unstarted_request(
            self.workspace,
            archive=self.archive,
            certify=lambda current: self.proof if proof is None else proof,
        )

    def test_accepted_request_retains_proof_and_allows_one_new_request(self):
        lock = (self.workspace / submission.LOCK).stat().st_ino
        record = self.retire()
        self.assertEqual(record["state_before"]["status"], "accepted")
        self.assertEqual(
            record["interfaces"][submission.RECEIPT], {"action": "evaluate"}
        )
        self.assertEqual(record["certification"], self.proof)
        self.assertEqual(submission.status(self.workspace)["status"], "not_submitted")
        self.assertEqual((self.workspace / submission.LOCK).stat().st_ino, lock)
        submission.submit(self.workspace)
        self.assertEqual(self.retire(), record)
        self.assertEqual(submission.status(self.workspace)["status"], "pending")

    def test_unknown_effects_cannot_retire_accepted_request(self):
        with self.assertRaises(submission.SubmissionError):
            self.retire({"never_submitted": False, "proof_ref": "unknown.json"})
        self.assertEqual(submission.status(self.workspace)["status"], "accepted")
        self.assertFalse(self.archive.exists())

    def test_interrupted_retirement_preserves_proof_and_finishes_without_replay(self):
        publish = submission._publish_retirement

        def interrupted(path, record):
            if record["status"] == "retired":
                raise OSError("simulated publication interruption")
            publish(path, record)

        with patch.object(submission, "_publish_retirement", interrupted):
            with self.assertRaises(submission.SubmissionError):
                self.retire()
        retained = json.loads((self.archive / "retirement.json").read_text())
        self.assertEqual(retained["state_before"]["status"], "accepted")
        self.assertEqual(self.retire()["status"], "retired")

    def test_changed_interface_blocks_partial_retirement(self):
        with patch.object(Path, "unlink", side_effect=OSError("interrupted")):
            with self.assertRaises(submission.SubmissionError):
                self.retire()
        (self.workspace / submission.RECEIPT).write_text('{"action":"research"}')
        with self.assertRaisesRegex(submission.SubmissionError, "changed"):
            self.retire()
        self.assertEqual(submission.status(self.workspace)["action"], "research")
