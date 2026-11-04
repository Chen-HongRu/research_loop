"""Real local child jobs and Git; synthetic agents, no paid/external resources."""

import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from examples import local_acceptance_backend as backend
from examples.runtime_acceptance import _workspace, run_smoke
from research_loop.runtime.files import publish_json


class RuntimeAcceptanceTests(unittest.TestCase):
    def test_recovery_dependencies_retry_capacity_and_stop(self):
        with TemporaryDirectory(prefix="acceptance-restart-") as temporary:
            result = run_smoke(Path(temporary))
            records = Path(temporary) / "study" / "backend"
            checkpoint = json.loads((records / "restart-checkpoint.json").read_text())
            gate = json.loads((records / "recovery-open.json").read_text())
            self.assertIsNone(checkpoint["sqlite_job_id"])
            self.assertGreater(gate["owner_epoch"], checkpoint["owner_epoch"])
            self.assertEqual(gate["operation_id"], checkpoint["operation_id"])
            self.assertEqual(gate["submission_token"], checkpoint["submission_token"])
        self.assertEqual(
            result["tasks"],
            {"A": "failed", "B": "published", "C": "blocked", "D": "published"},
        )
        self.assertEqual(result["max_actual_active_jobs"], 2)
        self.assertGreater(result["overlap_seconds"], 0)
        self.assertEqual(result["new_submits_after_recovery"], 0)
        self.assertTrue(result["recovery_unknown_before_gate"])
        self.assertTrue(result["recovered_original_job"])
        self.assertEqual(result["B_collection_attempts"], 2)
        self.assertEqual(result["B_review_attempts"], 2)
        self.assertEqual(result["B_execute_attempts"], 1)
        self.assertEqual(result["stop_extra_calls"], 0)
        self.assertTrue(result["backend_children_reaped"])

    def test_backend_reaps_actual_child_when_driver_fails(self):
        with TemporaryDirectory(prefix="acceptance-cleanup-") as temporary:
            base = Path(temporary)
            workspace, commit = _workspace(base)
            root = base / "study"
            token = "submit-synthetic-cleanup"
            with self.assertRaisesRegex(RuntimeError, "synthetic driver failure"):
                with backend.managed_backend(root, workspace, commit):
                    publish_json(
                        root / "backend" / "requests" / f"{token}.json",
                        dict(
                            operation_id="synthetic-operation",
                            submission_token=token,
                            task="B",
                            commit=commit,
                        ),
                    )
                    entered = backend._wait(
                        root / "backend" / "jobs" / token / "entered.json"
                    )
                    self.assertGreater(entered["pid"], 0)
                    raise RuntimeError("synthetic driver failure")
            proof = json.loads((root / "backend" / "reaped.json").read_text())
            self.assertEqual(proof["remaining_children"], 0)


if __name__ == "__main__":
    unittest.main()
