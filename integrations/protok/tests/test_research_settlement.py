"""Fresh Execute closure needs a durable, bound submission boundary."""

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from integrations.protok.backend import ProTokError
from integrations.protok.research_backend import _read, _write
from integrations.protok.research_settlement import (
    begin_script_execution,
    close_script_execution,
    mark_script_submission,
    settle_script_execution,
)


class ScriptSettlementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.attempt = "iterations/0002/execute/attempt-001"
        self.control = self.root / "control"
        self.control.mkdir()
        (self.root / "study").mkdir()
        self.plan = {
            "decision": "investigate",
            "markdown": "Measure a bounded development computation.",
            "execution": {"mode": "script", "entrypoint": "research/main.py"},
        }
        self.reference = "iterations/0002/plan/response.json"
        (self.root / "study" / self.reference).parent.mkdir(parents=True)
        _write(self.root / "study" / self.reference, self.plan)
        _write(
            self.root / "study/state.json",
            {"iteration": 2, "plan_ref": self.reference},
        )
        _write(self.control / "ownership.json", {"owner_token": "test-owner"})
        _write(self.control / "continuation.json", {"additional_gpu_seconds": 1800})
        self.journal = self.control / "research-executions/0002/execution.json"

    def begin(self):
        return begin_script_execution(
            self.root, self.workspace, "0002", self.attempt, 1234.0
        )

    def close(self, **kwargs):
        return close_script_execution(
            self.root, self.workspace, "0002", self.attempt, **kwargs
        )

    def settle(self):
        return settle_script_execution(self.root, self.workspace, "0002", self.attempt)

    def test_absent_job_alone_does_not_prove_not_started(self):
        with self.assertRaisesRegex(ProTokError, "durable submission boundary"):
            self.settle()
        self.begin()
        with self.assertRaisesRegex(ProTokError, "stopped closure"):
            self.settle()

    def test_closed_fresh_attempt_certifies_no_allocation_and_preserves_failure(self):
        self.begin()
        self.close(error="trusted preparation failed")
        result = self.settle()
        self.assertEqual(result.status, "not_started")
        self.assertEqual(result.result["gpu_seconds"], 0)
        self.assertIsNone(result.result["owned_job_id"])
        self.assertEqual(result.result["error"], "trusted preparation failed")
        self.assertEqual(result.result["artifact"]["path"], str(self.journal))
        self.assertEqual(self.close(), _read(self.journal))
        self.assertEqual(self.settle(), result)
        self.assertFalse((self.control / "research-jobs").exists())
        with self.assertRaisesRegex(ProTokError, "reconciliation"):
            self.begin()

    def test_changed_plan_attempt_or_lease_cannot_reuse_closure(self):
        self.begin()
        self.close()
        with self.assertRaisesRegex(ProTokError, "stopped closure"):
            settle_script_execution(
                self.root, self.workspace, "0002", "iterations/0002/execute/attempt-002"
            )
        changed = copy.deepcopy(self.plan)
        changed["markdown"] = "A different experiment"
        _write(self.root / "study" / self.reference, changed)
        with self.assertRaisesRegex(ProTokError, "stopped closure"):
            self.settle()
        _write(self.root / "study" / self.reference, self.plan)
        _write(self.control / "continuation.json", {"additional_gpu_seconds": 3600})
        with self.assertRaisesRegex(ProTokError, "stopped closure"):
            self.settle()

    def test_submission_intent_blocks_even_without_job_or_hook_state_update(self):
        self.begin()
        mark_script_submission(self.root, "0002", self.plan["execution"], 1234.0)
        self.close(error="submission transport failed before returning")
        with self.assertRaisesRegex(ProTokError, "unresolved effects"):
            self.settle()
        self.assertIs(_read(self.journal)["submission_intent"], True)
        with self.assertRaisesRegex(ProTokError, "active Execute"):
            mark_script_submission(self.root, "0002", self.plan["execution"], 1234.0)

    def test_prepared_state_without_explicit_boundary_does_not_prove_no_job(self):
        self.begin()
        job = self.control / "research-jobs/0002"
        job.mkdir(parents=True)
        _write(job / "job.json", {})
        _write(job / "state.json", {"attempts": [{"status": "prepared"}]})
        with self.assertRaisesRegex(ProTokError, "may have started"):
            self.close()
        _write(job / "job.json", {"submission_intent": False})
        self.close()
        self.assertEqual(self.settle().status, "not_started")

    def test_closed_attempt_cannot_submit(self):
        self.begin()
        self.close()
        with self.assertRaisesRegex(ProTokError, "active Execute"):
            mark_script_submission(self.root, "0002", self.plan["execution"], 1234.0)

    def test_owner_and_deadline_changes_cannot_enter_submission(self):
        self.begin()
        with self.assertRaisesRegex(ProTokError, "active Execute"):
            mark_script_submission(self.root, "0002", self.plan["execution"], 1235.0)
        _write(self.control / "ownership.json", {"owner_token": "different-owner"})
        with self.assertRaisesRegex(ProTokError, "binding changed"):
            self.close()
        self.assertIs(_read(self.journal)["submission_intent"], False)

    def test_public_closure_contains_no_ownership_credentials(self):
        self.begin()
        self.close(error="trusted pipeline test-owner failed")
        rendered = self.journal.read_text()
        self.assertNotIn("test-owner", rendered)
        self.assertNotIn("owner_token", rendered)
        self.assertNotIn("additional_gpu_seconds", rendered)
        self.assertEqual(self.settle().status, "not_started")

    def test_prepared_job_crash_closure_resolves_accounting_without_job_id(self):
        from integrations.protok.research_execution import research_usage

        self.begin()
        job = self.control / "research-jobs/0002"
        job.mkdir(parents=True)
        _write(
            job / "job.json",
            {
                "submission_intent": False,
                "allocated_seconds": 60,
                "source_identity": "source-v1",
                "execution_identity": "execution-v1",
            },
        )
        _write(job / "state.json", {"attempts": [{"status": "prepared"}]})
        self.assertEqual(research_usage(self.control)["outstanding"], [str(job)])
        self.close(error="owner terminated during staging")
        receipt = _read(job / "receipt.json")
        self.assertEqual(receipt["status"], "not_started")
        self.assertNotIn("owned_job_id", receipt)
        self.assertEqual(
            research_usage(self.control),
            {
                "gpu_seconds": 0,
                "outstanding": [],
                "overruns": [],
            },
        )
        original = (job / "receipt.json").read_bytes()
        self.close()
        self.assertEqual((job / "receipt.json").read_bytes(), original)
        # Historical accounting survives the ordinary move to the next iteration.
        _write(self.root / "study/state.json", {"iteration": 3, "plan_ref": None})
        self.assertEqual(research_usage(self.control)["outstanding"], [])
        receipt["gpu_seconds"] = 1
        _write(job / "receipt.json", receipt)
        report = research_usage(self.control)
        self.assertIsNone(report["gpu_seconds"])
        self.assertEqual(report["outstanding"], [str(job)])
        self.assertIn("No-allocation accounting", report["errors"][0]["error"])

    def test_fixed_mode_has_no_script_journal(self):
        self.plan["execution"]["mode"] = "fixed_evaluation"
        _write(self.root / "study" / self.reference, self.plan)
        self.assertIsNone(
            begin_script_execution(self.root, self.workspace, "0002", None, 1234.0)
        )
        self.assertFalse(self.journal.exists())

    def test_settlement_uses_frozen_validator_and_matching_native_accounting(self):
        self.begin()
        mark_script_submission(self.root, "0002", self.plan["execution"], 1234.0)
        self.close()
        receipt = {
            "status": "completed",
            "owned_job_id": "123",
            "gpu_seconds": 7,
            "terminal_state": "COMPLETED",
            "exit_code": "0:0",
        }
        job = self.control / "research-jobs/0002"
        job.mkdir(parents=True)
        _write(job / "job.json", {"submission_intent": True})
        _write(job / "state.json", {"attempts": [receipt]})
        with patch(
            "integrations.protok.research_execution.settled_script",
            return_value=receipt,
        ) as verified:
            self.assertEqual(self.settle().status, "completed")
            verified.assert_called_once_with(self.root, self.workspace, "0002")
            _write(job / "state.json", {"attempts": [{**receipt, "gpu_seconds": 0}]})
            with self.assertRaisesRegex(ProTokError, "accounting"):
                self.settle()
            for terminal, exit_code in (("RUNNING", "0:0"), ("FAILED", "invalid")):
                receipt.update(
                    status="failed", terminal_state=terminal, exit_code=exit_code
                )
                _write(job / "state.json", {"attempts": [receipt]})
                with self.assertRaisesRegex(ProTokError, "accounting"):
                    self.settle()


if __name__ == "__main__":
    unittest.main()
