import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock


SPEC = importlib.util.spec_from_file_location(
    "exploration_deploy",
    Path(__file__).resolve().parents[1] / "integrations/protok/deploy_exploration.py",
)
DEPLOY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEPLOY)


class ExplorationDeployTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.write(
            "study/state.json", {"iteration": 6, "status": "ready", "phase": "done"}
        )
        self.write("study/iterations/0006/iteration.json", {"status": "reviewed"})
        self.write("study/iterations/0006/publication.json", {})
        self.write("control/runner.json", {"status": "completed", "iteration": 6})
        self.write("control/jobs/0006/job.json", {"status": "completed"})
        self.backend = Mock()
        self.backend.inspect.return_value = {"outstanding": []}

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def test_only_complete_published_iteration_is_admitted(self):
        self.assertEqual(DEPLOY.check_boundary(self.root, self.backend)["iteration"], 6)
        self.assertFalse((self.root / "control/STOP").exists())

    def test_running_experiment_or_review_cannot_be_stopped(self):
        for phase in ["execute", "review"]:
            with self.subTest(phase=phase):
                self.write(
                    "study/state.json",
                    {"iteration": 6, "status": "running", "phase": phase},
                )
                with self.assertRaisesRegex(RuntimeError, "still active"):
                    DEPLOY.check_boundary(self.root, self.backend)
        self.backend.inspect.assert_not_called()

    def test_unfinished_control_publication_is_rejected(self):
        self.write("control/runner.json", {"status": "running", "iteration": 6})
        with self.assertRaisesRegex(RuntimeError, "control publication"):
            DEPLOY.check_boundary(self.root, self.backend)

    def test_submitted_job_and_outstanding_effects_are_rejected(self):
        self.write("control/jobs/0006/job.json", {"status": "submitted"})
        with self.assertRaisesRegex(RuntimeError, "not completed"):
            DEPLOY.check_boundary(self.root, self.backend)
        self.write("control/jobs/0006/job.json", {"status": "completed"})
        self.backend.inspect.return_value = {"outstanding": [{"fingerprint": "owned"}]}
        with self.assertRaisesRegex(RuntimeError, "Outstanding"):
            DEPLOY.check_boundary(self.root, self.backend)

    def test_existing_stop_is_preserved(self):
        stop = self.root / "control/STOP"
        stop.write_text("Operator intervention")
        with self.assertRaisesRegex(RuntimeError, "Existing STOP"):
            DEPLOY.check_boundary(self.root, self.backend)
        self.assertEqual(stop.read_text(), "Operator intervention")

    def test_inactive_service_requires_explicit_reconciliation(self):
        DEPLOY.check_service_state(self.root, "active", False)
        for state, allowed in [
            ("inactive", False),
            ("inactive", True),
            ("failed", True),
        ]:
            with (
                self.subTest(state=state, allowed=allowed),
                self.assertRaises(RuntimeError),
            ):
                DEPLOY.check_service_state(self.root, state, allowed)
        self.write(
            "control/reconciliations/readonly-proposal-handoff-0007/result.json",
            {"status": "restored_published_boundary"},
        )
        DEPLOY.check_service_state(self.root, "inactive", True)
        with self.assertRaises(RuntimeError):
            DEPLOY.check_service_state(self.root, "inactive", False)


if __name__ == "__main__":
    unittest.main()
