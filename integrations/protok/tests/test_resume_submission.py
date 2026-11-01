import copy
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from research_loop import Study, initialize
from test_core import RecordedHarness

from integrations.protok import resume_submission as resume
from integrations.protok.backend import CANDIDATE_FILES, ProTokError
from integrations.protok.run import read, write


class IncompleteHarness(RecordedHarness):
    def response(self, phase, context, kwargs):
        if phase == "execute":
            return {
                "outcome": "incomplete",
                "markdown": "Static checks passed; not submitted.",
                "artifacts": [],
                "measurements": [],
            }
        return super().response(phase, context, kwargs)


class ResumeSubmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "run"
        self.project = Path(temporary.name) / "project"
        self.project.mkdir()
        for name in CANDIDATE_FILES:
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n" if name.endswith("json") else "# candidate\n")
        self.git("init", "--quiet")
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--quiet",
            "-m",
            "candidate",
        )
        initialize(
            self.root / "study",
            goal="Evaluate the fixed candidate.",
            workspace=self.project,
            allow_write=True,
            max_seconds=36000,
        )
        harness = IncompleteHarness()
        study = Study(self.root / "study", harness=harness)
        state = study.run()
        self.assertEqual(state["status"], "stopped")
        self.control = self.root / "control"
        self.control.mkdir()
        self.backend = Mock()
        self.backend.project = self.project
        self.backend.owner_token = "owner"
        self.backend.inspect.return_value = {
            "outstanding": [],
            "debug_outstanding": [],
            "used_gpu_seconds": 1000,
        }
        self.backend._config.return_value = {"reserve_gpu_seconds": 3600}
        now = time.time()
        write(
            self.control / "ownership.json",
            {
                "owner_token": "owner",
                "project": str(self.project),
                "handoff_acknowledged": True,
                "expires_at": now + 20000,
            },
        )
        write(
            self.control / "continuation.json",
            {
                "schema": 2,
                "seconds": 86400,
                "owner_token": "owner",
                "authorization": "original budget",
                "max_new_candidates": None,
                "started_at": now - 100,
                "deadline": now + 20000,
                "initial_gpu_seconds": 1000,
                "additional_gpu_seconds": 86400,
            },
        )
        write(self.control / "runner.json", {"status": "stopped", "iteration": 1})
        (self.control / "STOP").write_text("Review requested stop\n")
        self.before = copy.deepcopy(state)
        self.source = self.root / "study/iterations/0001"
        self.workspace = Path(read(self.source / "iteration.json")["workspace"])

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.project), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def prepare(self):
        return resume.prepare_resume(
            self.root,
            self.backend,
            authorization="User authorized same candidate first evaluation",
        )

    def test_prepare_preserves_history_budget_and_counts_and_is_repeatable(self):
        original = {
            p: p.read_bytes()
            for p in self.source.rglob("*")
            if p.is_file() and "workspace" not in p.relative_to(self.source).parts
        }
        budgets = {
            p: p.read_bytes()
            for p in (
                self.control / "ownership.json",
                self.control / "continuation.json",
                self.root / "study/study.json",
            )
        }
        pending = self.prepare()
        self.assertEqual(self.prepare(), pending)
        state = resume.validate_prepared(self.root, self.backend)
        self.assertEqual(state, {**self.before, "status": "ready", "stop_reason": None})
        self.assertEqual((self.control / "STOP").read_text(), pending["stop_before"])
        for path, contents in {**original, **budgets}.items():
            self.assertEqual(path.read_bytes(), contents)
        self.backend.evaluate.assert_not_called()

    def test_begin_creates_one_operator_plan_and_next_normal_stage_is_execute_then_review(
        self,
    ):
        pending = self.prepare()
        (self.control / "STOP").unlink()
        harness = IncompleteHarness()
        study = Study(self.root / "study", harness=harness)
        resume.begin_pending(study, self.backend)
        state = study.status()
        self.assertEqual((state["iteration"], state["phase"]), (2, "execute"))
        self.assertEqual(state["call_count"], self.before["call_count"])
        self.assertEqual(state["elapsed_seconds"], self.before["elapsed_seconds"])
        metadata = read(self.root / "study/iterations/0002/iteration.json")
        self.assertEqual(metadata["base_experiment"], "0001")
        self.assertEqual(
            metadata["operator_resume"]["source_signatures"],
            pending["source_signatures"],
        )
        self.assertEqual(
            metadata["route_id"], read(self.source / "iteration.json")["route_id"]
        )
        self.assertEqual(
            read(self.root / "study/iterations/0002/operator-plan/operator.json")[
                "source"
            ],
            "operator",
        )
        resume.begin_pending(study, self.backend)
        self.assertEqual(study.status(), state)
        study.run()
        self.assertEqual(
            [call["phase"] for call in harness.calls], ["execute", "review"]
        )
        self.assertTrue((self.root / "study/iterations/0002/publication.json").exists())
        self.assertEqual(read(self.source / "candidates.json")["candidates"], [])

    def test_reject_existing_submission_request_result_or_job(self):
        for name in (
            "evaluation-request.json",
            "evaluation-result.json",
            "evaluation-submission.json",
        ):
            with self.subTest(name=name):
                path = self.workspace / name
                path.write_text("{}")
                with self.assertRaises(ProTokError):
                    self.prepare()
                path.unlink()
        (self.control / "jobs/0001").mkdir(parents=True)
        with self.assertRaises(ProTokError):
            self.prepare()

    def test_reject_unknown_effects_or_insufficient_original_budget(self):
        for changes in (
            {"outstanding": ["unknown"]},
            {"debug_outstanding": ["unknown"]},
            {"used_gpu_seconds": 85000},
        ):
            with self.subTest(changes=changes):
                original = self.backend.inspect.return_value.copy()
                self.backend.inspect.return_value.update(changes)
                with self.assertRaises(ProTokError):
                    self.prepare()
                self.backend.inspect.return_value = original
        lease = read(self.control / "continuation.json")
        lease["deadline"] = time.time() - 1
        write(self.control / "continuation.json", lease)
        with self.assertRaises(ProTokError):
            self.prepare()
        self.assertEqual(read(self.root / "study/state.json"), self.before)

    def test_reject_candidate_identity_and_changed_stop(self):
        path = self.workspace / CANDIDATE_FILES[0]
        original = path.read_text()
        path.write_text(original + "# unauthorized\n")
        with self.assertRaises(ProTokError):
            self.prepare()
        path.write_text(original)
        self.prepare()
        (self.control / "STOP").write_text("new user stop")
        with self.assertRaises(ProTokError):
            resume.validate_prepared(self.root, self.backend)

    def test_partial_prepare_can_finish_without_replacing_evidence(self):

        def fail(study, state):
            raise OSError("interrupted before live-state publication")

        with patch.object(Study, "_save", fail):
            with self.assertRaises(OSError):
                self.prepare()
        evidence = self.control / "reconciliations/resume-submission-0001.json"
        original = evidence.read_bytes()
        self.prepare()
        self.assertEqual(evidence.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
