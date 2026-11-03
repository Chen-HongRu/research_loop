"""Real Study/Git lifecycle with a local receipted allocation stand-in."""

import hashlib
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from research_loop import Study, StudyError, initialize
from research_loop.harness import HarnessError
from research_loop.protocol import SCHEMAS
from research_loop.tasks import TaskQueue
from integrations.protok import tasks, long_training
from integrations.protok.backend import CANDIDATE_FILES
from test_core import RecordedHarness, evaluation


class Author(RecordedHarness):
    def run(self, **kwargs):
        if "observation_reviews" in kwargs["schema"]["properties"]:
            kwargs = {**kwargs, "schema": SCHEMAS["review"]}
        return super().run(**kwargs)

    def response(self, phase, context, kwargs):
        if phase == "execute":
            path = Path(kwargs["workspace"]) / CANDIDATE_FILES[0]
            path.write_text(path.read_text() + "# user requested intervention\n")
            return {
                "outcome": "completed",
                "markdown": "Candidate authored; no score invented.",
            }
        result = super().response(phase, context, kwargs)
        if phase == "plan":
            result.pop("local_evaluation")
            result.pop("references")
            result.pop("timeout_seconds")
        if phase == "review" and context.get("long_training_run"):
            result["observation_reviews"] = [
                {
                    "step": point["step"],
                    "valid": True,
                    "verified": True,
                    "reason": "Checked this complete checkpoint observation.",
                }
                for point in context["long_training_observations"]["curve"]
            ]
        return result


class ManualExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        project = self.root / "canonical"
        for name in (*CANDIDATE_FILES, "scripts/autoresearch_se3_eval.py"):
            path = project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n" if name.endswith("json") else "# original source\n")
        for arguments in (
            ["init", "-q"],
            ["add", "."],
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@localhost",
                "commit",
                "-qm",
                "baseline",
            ],
        ):
            subprocess.run(["git", "-C", str(project), *arguments], check=True)
        initialize(
            self.root / "study",
            goal="Parent automatic exploration",
            workspace=project,
            allow_write=True,
            global_evaluation=evaluation("short"),
        )
        tasks._write(
            self.root / "control/ownership.json",
            {
                "owner_token": "test",
                "handoff_acknowledged": True,
                "expires_at": time.time() + 10000,
            },
        )
        digest = hashlib.sha256(
            (project / "scripts/autoresearch_se3_eval.py").read_bytes()
        ).hexdigest()
        self.config = {
            "locked_files": {"scripts/autoresearch_se3_eval.py": digest},
            "candidate_config": CANDIDATE_FILES[2],
        }
        self.backend = SimpleNamespace(
            project=project, owner_token="test", _config=lambda: self.config
        )
        self.fixed = SimpleNamespace(
            fingerprint=lambda config, contents: hashlib.sha256(
                b"".join(contents[name] for name in CANDIDATE_FILES)
            ).hexdigest()
        )
        self.queue = TaskQueue(self.root / "study")
        self.allocations = []
        self.interrupt = False

    def submit(self, identity="manual-long", **changes):
        return self.queue.submit(
            {
                "id": identity,
                "kind": "long_training",
                "instruction": "Compare the explicitly requested candidate under the fixed long protocol.",
                "budget": {
                    "authorization": "Test-only manual grant",
                    "gpu_seconds_per_run": 3600,
                    "total_gpu_seconds": 3600,
                    "deadline": time.time() + 10000,
                },
                **changes,
            }
        )

    def allocate(self, backend, root, request, workspace, *, deadline, recover=False):
        long_training._manual_source(root, request, self.config, self.fixed)
        job = self.root / "control/manual-training-jobs" / request["id"]
        if not recover:
            self.allocations.append(request)
            tasks._write(job / "job.json", {"deadline": deadline})
        result = {
            "status": "completed",
            "run_id": request["id"],
            "source_identity": "trusted-driver-" + request["candidate"]["id"],
            "source_candidate": request["candidate"],
            "recipe": request["recipe"],
            "evaluation": request["evaluation"],
            "curve": [
                {
                    "step": step,
                    "ca_rmsd": 2.0,
                    "tm_score": 0.7,
                    "lddt": 0.8,
                    "checkpoint": f"long-checkpoint:{request['id']}:{step}",
                    "evidence": [f"/retained/step-{step}.json"],
                }
                for step in long_training.STEPS
            ],
            "endpoint_complete": True,
            "missing_validation_steps": [],
            "gpu_seconds": 120,
        }
        tasks._write(workspace / "result.json", result)
        if self.interrupt and not recover:
            self.interrupt = False
            raise HarnessError("Reporting interrupted after the owned allocation")
        return long_training._envelope(request, workspace / "result.json", result)

    def test_older_parent_format_rejected_before_claim_or_recovery(self):
        request = self.submit()
        author = Author()
        manager = tasks.ManualTrainingManager(self.root, self.backend, delegate=author)
        config_path = self.root / "study/study.json"
        for version in (1, 2):
            for operation in (manager.run_one, lambda: manager.recover(request["id"])):
                with self.subTest(version=version, operation=operation):
                    config = tasks._read(config_path)
                    config["version"] = version
                    tasks._write(config_path, config)
                    before = self.queue.snapshot()
                    control_files = sorted((self.root / "control").iterdir())
                    with (
                        patch.object(TaskQueue, "claim") as claim,
                        patch.object(tasks, "execute") as execute,
                        self.assertRaisesRegex(StudyError, "Unsupported study format"),
                    ):
                        operation()
                    claim.assert_not_called()
                    execute.assert_not_called()
                    self.assertEqual(self.queue.snapshot(), before)
                    self.assertEqual(
                        sorted((self.root / "control").iterdir()), control_files
                    )
                    self.assertFalse(
                        (self.root / "study/tasks" / request["id"] / "study").exists()
                    )
                    self.assertEqual(tasks._read(config_path), config)
                    self.assertEqual(author.calls, [])

    def test_new_code_direct_long_review_and_boundary_knowledge(self):
        request = self.submit()
        author = Author()
        manager = tasks.ManualTrainingManager(self.root, self.backend, delegate=author)
        with (
            patch.object(tasks, "_load_backend", return_value=(self.fixed, None)),
            patch.object(tasks, "execute", side_effect=self.allocate),
        ):
            result = manager.run_one()
            self.assertIsNone(manager.run_one())
        self.assertEqual(
            [call["phase"] for call in author.calls], ["plan", "execute", "review"]
        )
        plan_call = author.calls[0]
        self.assertNotIn("base_experiment", plan_call["schema"]["properties"])
        self.assertNotIn("local_evaluation", plan_call["schema"]["properties"])
        self.assertNotIn("references", plan_call["schema"]["properties"])
        self.assertNotIn(
            "timeout_seconds",
            plan_call["schema"]["properties"],
        )
        raw_plan = tasks._read(plan_call["run_dir"] / "response.json")
        self.assertNotIn("base_experiment", raw_plan)
        self.assertNotIn("timeout_seconds", raw_plan)
        authored = author.calls[1]
        self.assertEqual(set(authored["schema"]["properties"]), {"outcome", "markdown"})
        self.assertEqual(
            set(tasks._read(authored["run_dir"] / "response.json")),
            {"outcome", "markdown"},
        )
        self.assertEqual(len(self.allocations), 1)
        actual = self.allocations[0]
        self.assertEqual(actual["manual_task_id"], request["id"])
        frozen = Path(actual["source_workspace"]) / CANDIDATE_FILES[0]
        self.assertIn("user requested intervention", frozen.read_text())
        self.assertEqual(self.queue.snapshot()[0]["status"], "reviewed")
        self.assertEqual(result["execute"]["outcome"], "completed")
        self.assertEqual(len(Study(self.root / "study").knowledge()["tasks"]), 1)
        parent = Study(self.root / "study", RecordedHarness())
        parent.run()
        boundary = tasks._read(
            self.root / "study/iterations/0001/context/task_results.json"
        )
        self.assertEqual(boundary[0]["id"], request["id"])
        self.assertEqual(parent.frontiers()["global"]["short"]["candidate_ids"], [])

    def test_author_cannot_supply_measurements_or_allocate_after_rejection(self):
        class InvalidAuthor(Author):
            def run(self, **kwargs):
                result = super().run(**kwargs)
                if kwargs["run_dir"].parent.parent.name == "execute":
                    result["measurements"] = []
                    tasks._write(kwargs["run_dir"] / "response.json", result)
                return result

        self.submit()
        author = InvalidAuthor()
        manager = tasks.ManualTrainingManager(self.root, self.backend, delegate=author)
        with (
            patch.object(tasks, "_load_backend", return_value=(self.fixed, None)),
            patch.object(tasks, "execute", side_effect=self.allocate),
            self.assertRaisesRegex(ValueError, "must contain exactly"),
        ):
            manager.run_one()
        self.assertEqual(self.allocations, [])
        call = next(c for c in author.calls if c["phase"] == "execute")
        self.assertIn("measurements", tasks._read(call["run_dir"] / "response.json"))

    def test_unchanged_candidate_needs_no_authoring_or_nomination(self):
        request = self.submit(allow_changes=False)
        author = Author()
        manager = tasks.ManualTrainingManager(self.root, self.backend, delegate=author)
        with (
            patch.object(tasks, "_load_backend", return_value=(self.fixed, None)),
            patch.object(tasks, "execute", side_effect=self.allocate),
        ):
            manager.run_one()
        self.assertEqual([call["phase"] for call in author.calls], ["plan", "review"])
        self.assertNotIn(
            "user requested intervention",
            (
                self.root
                / "study/tasks"
                / request["id"]
                / "source"
                / CANDIDATE_FILES[0]
            ).read_text(),
        )
        self.assertFalse((self.root / "control/long-training-policy.json").exists())

    def test_fixed_candidate_rejects_workspace_edit_before_allocation(self):
        self.submit(allow_changes=False)
        original = tasks.ManualLongHarness.run

        def changed_workspace(harness, **kwargs):
            if kwargs["run_dir"].parent.name == "execute":
                path = Path(kwargs["workspace"]) / CANDIDATE_FILES[0]
                path.write_text(path.read_text() + "# unexpected edit\n")
            return original(harness, **kwargs)

        manager = tasks.ManualTrainingManager(
            self.root, self.backend, delegate=Author()
        )
        with (
            patch.object(tasks.ManualLongHarness, "run", new=changed_workspace),
            patch.object(tasks, "execute") as allocate,
        ):
            with self.assertRaisesRegex(Exception, "differs from its frozen input"):
                manager.run_one()
        allocate.assert_not_called()

    def test_interrupted_report_reconciles_same_allocation(self):
        request = self.submit()
        self.interrupt = True
        manager = tasks.ManualTrainingManager(
            self.root, self.backend, delegate=Author()
        )
        with (
            patch.object(tasks, "_load_backend", return_value=(self.fixed, None)),
            patch.object(tasks, "execute", side_effect=self.allocate),
        ):
            with self.assertRaises(HarnessError):
                manager.run_one()
            child = Study(self.root / "study/tasks" / request["id"] / "study")
            original = child.status()["calls"]["execute"]
            with self.assertRaises(Exception):
                manager.run_one()
            manager.recover(request["id"])
        self.assertEqual(len(self.allocations), 1)
        self.assertEqual(child.status()["calls"]["execute"], original)
        self.assertEqual(self.queue.snapshot()[0]["status"], "reviewed")

    def test_explicit_recovery_finishes_frozen_authoring_before_manifest(self):
        request = self.submit()
        author = Author()
        manager = tasks.ManualTrainingManager(self.root, self.backend, delegate=author)
        with (
            patch.object(
                tasks,
                "_load_backend",
                side_effect=[ValueError("loader interrupted"), (self.fixed, None)],
            ),
            patch.object(tasks, "execute", side_effect=self.allocate),
        ):
            with self.assertRaisesRegex(ValueError, "loader interrupted"):
                manager.run_one()
            folder = self.root / "study/tasks" / request["id"]
            self.assertTrue((folder / "source").exists())
            self.assertFalse(
                (
                    folder / "study/iterations/0001/context/manual-long-request.json"
                ).exists()
            )
            original = Study(folder / "study").status()["calls"]["execute"]
            manager.recover(request["id"])
        self.assertEqual(len(self.allocations), 1)
        self.assertEqual(
            [call["phase"] for call in author.calls], ["plan", "execute", "review"]
        )
        self.assertEqual(Study(folder / "study").status()["calls"]["execute"], original)
        self.assertTrue(list(folder.glob("source.pre-allocation.*")))
        self.assertEqual(self.queue.snapshot()[0]["status"], "reviewed")

    def test_explicit_recovery_finishes_manifest_before_first_allocation(self):
        request = self.submit()
        manager = tasks.ManualTrainingManager(
            self.root, self.backend, delegate=Author()
        )
        failed = False

        def allocate_once(*args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise ValueError("interrupted before backend preparation")
            return self.allocate(*args, **kwargs)

        with (
            patch.object(tasks, "_load_backend", return_value=(self.fixed, None)),
            patch.object(tasks, "execute", side_effect=allocate_once),
        ):
            with self.assertRaisesRegex(ValueError, "backend preparation"):
                manager.run_one()
            self.assertEqual(self.allocations, [])
            manager.recover(request["id"])
        self.assertEqual(len(self.allocations), 1)
        self.assertEqual(self.queue.snapshot()[0]["status"], "reviewed")

    def test_preallocation_recovery_rejects_changed_authored_source(self):
        request = self.submit()
        manager = tasks.ManualTrainingManager(
            self.root, self.backend, delegate=Author()
        )
        with patch.object(
            tasks, "_load_backend", side_effect=ValueError("interrupted")
        ):
            with self.assertRaises(ValueError):
                manager.run_one()
        folder = self.root / "study/tasks" / request["id"]
        source = folder / "study/iterations/0001/workspace" / CANDIDATE_FILES[0]
        source.write_text(source.read_text() + "# later change\n")
        with self.assertRaisesRegex(Exception, "authoring source changed"):
            manager.recover(request["id"])
        self.assertEqual(self.allocations, [])

    def test_expiry_during_clone_leaves_next_manual_task_eligible(self):
        request = self.submit()
        deadline = request["budget"]["deadline"]
        now = [deadline - 1]
        run = subprocess.run

        def clone_then_expire(*args, **kwargs):
            result = run(*args, **kwargs)
            if args[0][0:2] == ["git", "clone"]:
                now[0] = deadline + 1
            return result

        author = Author()
        manager = tasks.ManualTrainingManager(self.root, self.backend, delegate=author)
        with (
            patch.object(tasks.time, "time", side_effect=lambda: now[0]),
            patch.object(tasks.subprocess, "run", side_effect=clone_then_expire),
        ):
            self.assertEqual(manager.run_one()["status"], "expired")
        self.assertEqual(author.calls, [])
        later = self.submit(
            "later-task", budget={**request["budget"], "deadline": deadline + 100}
        )
        with patch.object(tasks.time, "time", return_value=deadline + 1):
            self.assertEqual(self.queue.claim("long_training")["id"], later["id"])

    def test_handoff_does_not_admit_prepared_manual_child(self):
        request = self.submit()
        self.queue.claim("long_training")
        (self.root / "control/HANDOFF").write_text("Drain active tasks")
        author = Author()
        manager = tasks.ManualTrainingManager(self.root, self.backend, delegate=author)
        self.assertIsNone(manager.run_one(allow_new=False))
        self.assertEqual(author.calls, [])
        self.assertIsNone(
            tasks.admitted_round(self.root / "study/tasks" / request["id"])
        )

    def test_manual_and_automatic_long_workers_start_independently(self):
        entered, release = threading.Event(), threading.Event()
        manual_entered = threading.Event()

        def automatic_run():
            entered.set()
            release.wait(2)

        automatic = Mock()
        automatic.start.side_effect = lambda: threading.Thread(
            target=automatic_run
        ).start()
        automatic.status.return_value = {"status": "running", "reason": None}
        manual = tasks.ManualTrainingScheduler(
            self.root,
            self.backend,
            manager=SimpleNamespace(run_one=lambda **kwargs: manual_entered.set()),
            interval=0.01,
        )
        scheduler = tasks.ExperimentScheduler(
            self.root, self.backend, automatic=automatic, manual=manual
        )
        try:
            scheduler.start()
            self.assertTrue(entered.wait(1))
            self.assertTrue(manual_entered.wait(1))
            self.assertEqual(scheduler.status()["status"], "running")
        finally:
            release.set()
            scheduler.shutdown()


if __name__ == "__main__":
    unittest.main()
