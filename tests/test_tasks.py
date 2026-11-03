"""Exercise durable requests through the real Study publication path."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_loop import Study, StudyError, initialize
from research_loop.harness import HarnessError, publish_completed_response
from research_loop.tasks import TaskQueue, completed_results
from test_core import ProcessLost, RecordedHarness


def read(path):
    return json.loads(path.read_text())


class TaskTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "study"
        initialize(self.root, goal="Record useful evidence")
        self.queue = TaskQueue(self.root)
        self.workspace = self.root / "workspace"
        self.git("init", "-q")
        self.git("commit", "--allow-empty", "-qm", "Initial readonly input")

    def git(self, *arguments):
        return subprocess.check_output(
            [
                "git",
                "-C",
                str(self.workspace),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@localhost",
                *arguments,
            ],
            text=True,
            stderr=subprocess.PIPE,
        ).strip()

    def commit_source(self, text):
        (self.workspace / "model.py").write_text(text)
        self.git("add", "model.py")
        self.git("commit", "-qm", "Update readonly input")
        return self.git("rev-parse", "HEAD")

    def submit(self, identity="manual", **kwargs):
        return self.queue.submit(
            {"id": identity, "instruction": "Inspect the fixed question", **kwargs}
        )

    def test_idempotency_conflict_cancel_and_priority(self):
        request = self.submit()
        self.assertEqual(self.submit(), request)
        with self.assertRaisesRegex(ValueError, "different request"):
            self.submit(instruction="Another question")
        self.submit("first", priority=1)
        self.queue.set_priority("manual", 0)
        self.assertEqual(self.queue.claim("investigation", iteration=1), request)
        self.assertEqual(self.queue.claim("investigation", iteration=1), request)
        with self.assertRaises(ValueError):
            self.queue.cancel("manual")
        with self.assertRaises(ValueError):
            self.queue.set_priority("manual", -1)
        self.queue.cancel("first")
        self.assertIsNone(self.queue.claim("investigation", iteration=2))

    def test_manual_direct_path_freezes_current_plan_and_uses_no_selection(self):
        request = self.submit()
        harness = RecordedHarness()
        harness.stop_after = 99
        study = Study(self.root, harness)
        state = study.status()
        study._begin(state)
        self.submit("next", priority=-100)
        self.assertEqual(study._context(study._folder(state))["manual_task"], request)
        self.assertEqual(state["phase"], "plan")
        study.run()
        self.assertEqual(
            [call["phase"] for call in harness.calls], ["plan", "execute", "review"]
        )
        self.assertEqual(self.queue.snapshot()[1]["status"], "reviewed")
        self.assertEqual(
            read(self.root / "iterations/0001/iteration.json")["task_id"], "manual"
        )
        self.assertEqual(
            read(self.root / "iterations/0001/context/task_results.json"), []
        )

    def test_long_binding_restart_and_reviewed_knowledge(self):
        # A long task freezes a real Git revision without executing a model or training.
        workspace = self.root.parent / "code"
        workspace.mkdir()
        subprocess.run(["git", "init", str(workspace)], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "--allow-empty",
                "-m",
                "baseline",
            ],
            check=True,
            capture_output=True,
        )
        config = read(self.root / "study.json")
        config.update(
            workspace=str(workspace),
            baseline=subprocess.check_output(
                ["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True
            ).strip(),
        )
        (self.root / "study.json").write_text(json.dumps(config))
        request = self.submit(
            kind="long_training",
            budget={
                "authorization": "operator",
                "gpu_seconds_per_run": 60,
                "total_gpu_seconds": 120,
                "deadline": 9999999999,
            },
        )
        self.assertEqual(self.queue.claim("long_training"), request)
        child = self.root / "tasks/manual/study"
        initialize(child, goal=request["instruction"])
        child_config = read(child / "study.json")
        (child / "study.json").write_text(
            json.dumps(child_config | {"manual_task": request})
        )
        self.assertEqual(
            TaskQueue(self.root).claim("long_training", allow_new=False), request
        )
        self.assertEqual(completed_results(self.root), [])
        Study(child, RecordedHarness()).run()
        results = completed_results(self.root)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "manual")
        self.assertEqual(Study(self.root).knowledge()["tasks"], results)
        self.assertIsNone(self.queue.claim("long_training", allow_new=False))

    def test_base_requires_publication_and_source_stays_frozen(self):
        folder = self.root / "iterations/0001"
        folder.mkdir()
        (folder / "iteration.json").write_text(json.dumps({"status": "reviewed"}))
        with self.assertRaisesRegex(ValueError, "fully published"):
            self.submit(base_experiment="0001")
        request = self.submit()
        config = read(self.root / "study.json")
        config["baseline"] = "later-revision"
        (self.root / "study.json").write_text(json.dumps(config))
        self.assertEqual(self.submit()["source"], request["source"])

    def test_writable_task_executes_frozen_git_baseline(self):
        root = self.root.parent / "writable"
        initialize(root, goal="Inspect frozen source", allow_write=True)
        queue = TaskQueue(root)
        request = queue.submit(
            {
                "id": "frozen",
                "instruction": "Inspect the original source",
                "allow_changes": False,
            }
        )
        config = read(root / "study.json")
        workspace = Path(config["workspace"])
        (workspace / "model.py").write_text("later = True\n")
        subprocess.run(
            ["git", "-C", str(workspace), "add", "model.py"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "later source",
            ],
            check=True,
            capture_output=True,
        )
        config["baseline"] = subprocess.check_output(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True
        ).strip()
        (root / "study.json").write_text(json.dumps(config))
        harness = RecordedHarness()
        Study(root, harness).run()
        self.assertEqual(harness.execute_inputs, [None])
        execute = next(call for call in harness.calls if call["phase"] == "execute")
        self.assertTrue(execute["writable"])
        self.assertTrue(execute["context"]["execution_writes_enabled"])
        self.assertFalse(execute["context"]["manual_task"]["allow_changes"])
        self.assertNotEqual(request["source"]["revision"], config["baseline"])
        metadata = read(root / "iterations/0001/iteration.json")
        self.assertEqual(metadata["task_id"], "frozen")

    def test_expired_long_request_does_not_block_ready_work(self):
        config = read(self.root / "study.json")
        config["baseline"] = "fixed-revision"
        (self.root / "study.json").write_text(json.dumps(config))
        budget = {
            "authorization": "operator",
            "gpu_seconds_per_run": 60,
            "total_gpu_seconds": 60,
            "deadline": 1,
        }
        self.submit("expired", kind="long_training", budget=budget, priority=0)
        ready = self.submit(
            "ready", kind="long_training", budget=budget | {"deadline": 9999999999}
        )
        self.assertEqual(self.queue.snapshot()[0]["status"], "expired")
        self.assertIsNone(self.queue.claim("long_training", allow_new=False))
        self.assertEqual(self.queue.claim("long_training"), ready)
        self.assertEqual(self.queue.claim("long_training", allow_new=False), ready)

    def test_expired_bound_unadmitted_task_releases_next_request(self):
        for initialized in (False, True, "cutoff"):
            with self.subTest(initialized=initialized):
                root = self.root.parent / f"expiry-{initialized}"
                initialize(root, goal="Inspect")
                config = read(root / "study.json")
                (root / "study.json").write_text(
                    json.dumps(config | {"baseline": "fixed"})
                )
                queue = TaskQueue(root)
                budget = {
                    "authorization": "operator",
                    "gpu_seconds_per_run": 60,
                    "total_gpu_seconds": 60,
                    "deadline": 100,
                }
                expired = queue.submit(
                    {
                        "id": "first",
                        "instruction": "Inspect",
                        "kind": "long_training",
                        "budget": budget,
                    }
                )
                ready = queue.submit(
                    {
                        "id": "next",
                        "instruction": "Inspect",
                        "kind": "long_training",
                        "budget": budget | {"deadline": 1000},
                    }
                )
                with patch("research_loop.tasks.time.time", return_value=50):
                    self.assertEqual(queue.claim("long_training"), expired)
                if initialized:
                    child = root / "tasks/first/study"
                    initialize(child, goal="Inspect")
                    if initialized == "cutoff":
                        state = read(child / "state.json")
                        (child / "state.json").write_text(
                            json.dumps(
                                state
                                | {
                                    "status": "needs_attention",
                                    "error": "ProTokError: Long round admission closed",
                                }
                            )
                        )
                with patch("research_loop.tasks.time.time", return_value=150):
                    row = next(row for row in queue.snapshot() if row["id"] == "first")
                    self.assertEqual((row["status"], row["phase"]), ("expired", "done"))
                    self.assertIsNone(queue.claim("long_training", allow_new=False))
                    self.assertEqual(queue.claim("long_training"), ready)

    def test_admitted_or_attempted_bound_task_survives_deadline(self):
        config = read(self.root / "study.json")
        (self.root / "study.json").write_text(
            json.dumps(config | {"baseline": "fixed"})
        )
        budget = {
            "authorization": "operator",
            "gpu_seconds_per_run": 60,
            "total_gpu_seconds": 60,
            "deadline": 100,
        }
        request = self.submit(kind="long_training", budget=budget)
        with patch("research_loop.tasks.time.time", return_value=50):
            self.queue.claim("long_training")
        child = self.root / "tasks/manual/study"
        initialize(child, goal="Inspect")
        original = read(child / "state.json")
        for change in (
            {"round_admission": "committed"},
            {"status": "needs_attention", "iteration": 1, "phase": "execute"},
            {"calls": {"execute": "attempt"}, "call_count": 1},
            {"iteration": 1, "phase": "execute"},
        ):
            with self.subTest(change=change):
                (child / "state.json").write_text(json.dumps(original | change))
                with patch("research_loop.tasks.time.time", return_value=150):
                    claimed = self.queue.claim("long_training", allow_new=False)
                    self.assertEqual(
                        claimed,
                        None if change.get("status") == "needs_attention" else request,
                    )
                    self.assertEqual(
                        self.queue.claim(
                            "long_training", identity="manual", allow_new=False
                        ),
                        request,
                    )
                    self.assertNotEqual(self.queue.snapshot()[0]["status"], "expired")

    def test_exact_long_claim_never_rebinds_or_selects_another_task(self):
        budget = {
            "authorization": "operator",
            "gpu_seconds_per_run": 60,
            "total_gpu_seconds": 60,
            "deadline": 9999999999,
        }
        first = self.submit("first", kind="long_training", budget=budget)
        second = self.submit("second", kind="long_training", budget=budget, priority=-1)
        self.assertIsNone(
            self.queue.claim("long_training", identity="first", allow_new=False)
        )
        self.assertIsNone(self.queue.claim("long_training", identity="missing"))
        self.assertEqual(self.queue.claim("long_training", identity="first"), first)
        binding = read(self.root / "tasks/first/binding.json")
        (self.root / "tasks/first/scheduler-error.json").write_text(
            json.dumps({"error": "clone failed", "state": None})
        )
        self.assertEqual(self.queue.claim("long_training"), second)
        self.assertEqual(
            self.queue.claim("long_training", identity="first", allow_new=False), first
        )
        self.assertEqual(read(self.root / "tasks/first/binding.json"), binding)
        self.assertEqual(
            next(row for row in self.queue.snapshot() if row["id"] == "first")[
                "scheduling_error"
            ],
            "clone failed",
        )

    def test_invalid_long_budget_and_readonly_fields(self):
        with self.assertRaisesRegex(
            ValueError, "investigations use parent Study grant"
        ):
            self.submit(
                budget={
                    "authorization": "operator",
                    "gpu_seconds_per_run": 60,
                    "total_gpu_seconds": 60,
                    "deadline": 9999999999,
                }
            )
        for changes in (
            {"kind": "long_training"},
            {"source": {}},
            {"id": "../bad"},
            {"allow_changes": 1},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.submit(**changes)

    def test_unversioned_readonly_investigation_is_rejected(self):
        root = self.root.parent / "unversioned"
        initialize(root, goal="Inspect unversioned source")
        workspace = root / "workspace"
        (workspace / "model.py").write_text("version = 1\n")
        with self.assertRaisesRegex(ValueError, "frozen Git source revision"):
            TaskQueue(root).submit(
                {"id": "manual", "instruction": "Inspect", "allow_changes": False}
            )
        self.assertFalse((root / "tasks/manual/request.json").exists())

    def test_readonly_task_uses_submitted_commit_after_workspace_changes(self):
        revision = self.commit_source("version = 1\n")
        request = self.submit(allow_changes=False)
        self.assertEqual(request["source"]["revision"], revision)
        self.assertIsNone(read(self.root / "study.json")["baseline"])
        self.commit_source("version = 2\n")
        harness = RecordedHarness()
        Study(self.root, harness).run()
        self.assertEqual(harness.execute_inputs, ["version = 1\n"])
        execute = next(call for call in harness.calls if call["phase"] == "execute")
        self.assertFalse(execute["writable"])
        self.assertFalse(execute["context"]["execution_writes_enabled"])
        self.assertEqual((self.workspace / "model.py").read_text(), "version = 2\n")
        frozen = Path(read(self.root / "iterations/0001/iteration.json")["workspace"])
        self.assertEqual(frozen, (self.root / "iterations/0001/workspace").resolve())
        self.assertEqual(
            read(self.root / "iterations/0001/iteration.json")["workspace_ref"],
            revision,
        )

    def test_readonly_restart_and_published_base_keep_original_source(self):
        revision = self.commit_source("version = 1\n")
        self.submit(allow_changes=False)
        first = RecordedHarness()
        first.stop_after = 99
        study = Study(self.root, first)
        state = study.status()
        study._begin(state)
        study._step(state)
        self.assertEqual(state["phase"], "execute")
        self.commit_source("version = 2\n")
        resumed = RecordedHarness()
        resumed.stop_after = 99
        Study(self.root, resumed).run()
        self.assertEqual(
            [call["phase"] for call in resumed.calls], ["execute", "review"]
        )
        self.assertEqual(resumed.execute_inputs, ["version = 1\n"])
        request = self.submit(
            "from-published", base_experiment="0001", allow_changes=False
        )
        self.assertEqual(request["source"]["revision"], revision)
        self.commit_source("version = 3\n")
        next_run = RecordedHarness()
        Study(self.root, next_run).run()
        self.assertEqual(next_run.execute_inputs, ["version = 1\n"])

    def test_legacy_unversioned_readonly_request_stops_before_execute(self):
        request = self.submit(allow_changes=False)
        request["source"]["revision"] = None
        (self.root / "tasks/manual/request.json").write_text(json.dumps(request))
        harness = RecordedHarness()
        with self.assertRaisesRegex(Exception, "no recoverable Git version"):
            Study(self.root, harness).run()
        self.assertEqual(harness.count("execute"), 0)

    def test_legacy_planned_readonly_task_is_blocked_without_repeating_effects(self):
        self.commit_source("version = 1\n")
        request = self.submit(allow_changes=False)
        study = Study(self.root, RecordedHarness())
        state = study.status()
        study._begin(state)
        study._step(state)
        folder = self.root / "iterations/0001"
        self.git("worktree", "remove", str(folder / "workspace"))
        # Retain the pre-freezing Plan boundary: old requests had no revision,
        # and their planned iteration pointed directly at the original workspace.
        request["source"]["revision"] = None
        for path in (
            self.root / "tasks/manual/request.json",
            folder / "context/task.json",
        ):
            path.write_text(json.dumps(request))
        metadata = read(folder / "iteration.json") | {
            "workspace": str(self.workspace),
            "workspace_ref": None,
        }
        (folder / "iteration.json").write_text(json.dumps(metadata))
        self.commit_source("version = 2\n")
        attempt = folder / "execute/attempt-001"
        attempt.mkdir(parents=True)
        for retained in ("unstarted", "unknown", "completed"):
            with self.subTest(retained=retained):
                restored = json.loads(json.dumps(state))
                if retained != "unstarted":
                    restored["calls"]["execute"] = str(attempt.relative_to(self.root))
                    restored["active_attempt"] = restored["calls"]["execute"]
                    (attempt / "process.json").write_text(
                        json.dumps({"status": "running", "run_id": "retained-job"})
                    )
                if retained == "completed":
                    publish_completed_response(
                        attempt,
                        {
                            "outcome": "completed",
                            "markdown": "retained result",
                            "artifacts": [],
                            "measurements": [],
                        },
                    )
                (self.root / "state.json").write_text(json.dumps(restored))
                preserved = {
                    path: path.read_bytes()
                    for path in (
                        folder / "iteration.json",
                        folder / "context/task.json",
                        self.root / "tasks/manual/request.json",
                        *attempt.iterdir(),
                    )
                }
                harness = RecordedHarness()
                with self.assertRaisesRegex(StudyError, "no verifiable frozen source"):
                    Study(self.root, harness).run()
                self.assertEqual(harness.calls, [])
                self.assertEqual(Study(self.root).status()["phase"], "execute")
                self.assertFalse((folder / "publication.json").exists())
                self.assertFalse((folder / "workspace").exists())
                self.assertEqual(
                    (self.workspace / "model.py").read_text(), "version = 2\n"
                )
                for path, content in preserved.items():
                    self.assertEqual(path.read_bytes(), content)

    def test_readonly_resume_checks_request_metadata_and_worktree_revision(self):
        revision = self.commit_source("version = 1\n")
        self.submit(allow_changes=False)
        study = Study(self.root, RecordedHarness())
        state = study.status()
        study._begin(state)
        study._step(state)
        folder = self.root / "iterations/0001"
        metadata = read(folder / "iteration.json")
        later = self.commit_source("version = 2\n")
        request_path = self.root / "tasks/manual/request.json"
        request = read(request_path)
        for mismatch in ("request", "metadata", "workspace", "head"):
            with self.subTest(mismatch=mismatch):
                request_path.write_text(json.dumps(request))
                changed = dict(metadata)
                if mismatch == "request":
                    request_path.write_text(
                        json.dumps(
                            request
                            | {"source": request["source"] | {"revision": later}}
                        )
                    )
                elif mismatch == "metadata":
                    changed["workspace_ref"] = later
                elif mismatch == "workspace":
                    changed["workspace"] = str(self.workspace)
                elif mismatch == "head":
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(folder / "workspace"),
                            "checkout",
                            "--detach",
                            later,
                        ],
                        check=True,
                        capture_output=True,
                    )
                (folder / "iteration.json").write_text(json.dumps(changed))
                (self.root / "state.json").write_text(json.dumps(state))
                harness = RecordedHarness()
                with self.assertRaisesRegex(StudyError, "no verifiable frozen source"):
                    Study(self.root, harness).run()
                self.assertEqual(harness.calls, [])
                self.assertFalse((folder / "publication.json").exists())
        self.assertNotEqual(revision, later)

    def test_readonly_completed_execute_receipt_resumes_without_reexecution(self):
        revision = self.commit_source("version = 1\n")
        self.submit(allow_changes=False)
        first = RecordedHarness()

        def lose_controller(call):
            if call["phase"] == "execute":
                attempt = call["run_dir"]
                publish_completed_response(attempt, read(attempt / "response.json"))
                raise ProcessLost()

        first.after = lose_controller
        with self.assertRaises(ProcessLost):
            Study(self.root, first).run()
        self.assertEqual(first.execute_inputs, ["version = 1\n"])
        self.assertEqual(Study(self.root).status()["phase"], "execute")
        self.commit_source("version = 2\n")
        resumed = RecordedHarness()
        Study(self.root, resumed).run()
        self.assertEqual([call["phase"] for call in resumed.calls], ["review"])
        metadata = read(self.root / "iterations/0001/publication.json")["metadata"]
        self.assertEqual(metadata["workspace_ref"], revision)
        self.assertEqual(metadata["status"], "reviewed")

    def test_published_readonly_base_without_revision_is_rejected(self):
        harness = RecordedHarness()
        harness.stop_after = 99
        Study(self.root, harness).run()
        self.assertIsNone(
            read(self.root / "iterations/0001/iteration.json")["workspace_ref"]
        )
        with self.assertRaisesRegex(ValueError, "frozen Git source revision"):
            self.submit(base_experiment="0001", allow_changes=False)


class FixedTaskBoundaryTests(unittest.TestCase):
    """Controller checks adapters even when Execute was previously accepted."""

    setUp = TaskTests.setUp
    git = TaskTests.git
    commit_source = TaskTests.commit_source
    submit = TaskTests.submit

    def prepare(self):
        self.commit_source("version = 1\n")
        self.submit(allow_changes=False)
        harness = RecordedHarness()
        harness.validations = []
        harness.identity_valid = True

        def validate(result, *, workspace, run_dir):
            harness.validations.append(run_dir)
            if not harness.identity_valid:
                raise HarnessError("Actual evaluated candidate differs from frozen A")
            return result

        harness.validate_completed = validate
        return Study(self.root, harness), harness

    def test_fixed_task_validates_fresh_execute_review_and_publication(self):
        study, harness = self.prepare()
        study.run()
        self.assertEqual(len(harness.validations), 3)
        self.assertEqual(harness.count("execute"), 1)
        self.assertEqual(harness.count("review"), 1)

    def test_accepted_completed_receipt_does_not_skip_fixed_identity(self):
        study, harness = self.prepare()
        step = study._step

        def interrupt(state):
            if state["phase"] == "execute":
                original_save = study._save

                def lose_controller(saved):
                    original_save(saved)
                    if saved.get("accepted_calls", {}).get("execute"):
                        raise ProcessLost()

                with patch.object(study, "_save", side_effect=lose_controller):
                    step(state)
            else:
                step(state)

        with patch.object(study, "_step", side_effect=interrupt):
            with self.assertRaises(ProcessLost):
                study.run()
        self.assertTrue(study.status()["accepted_calls"]["execute"])
        harness.identity_valid = False
        with self.assertRaisesRegex(HarnessError, "frozen A"):
            Study(self.root, harness).run()
        self.assertEqual(harness.count("execute"), 1)
        self.assertEqual(harness.count("review"), 0)

    def test_review_restart_checks_actual_candidate_before_review_call(self):
        study, harness = self.prepare()
        state = study.status()
        study._begin(state)
        study._step(state)  # Plan.
        study._step(state)  # Execute accepted.
        self.assertEqual(state["phase"], "review")
        harness.identity_valid = False
        with self.assertRaisesRegex(HarnessError, "frozen A"):
            Study(self.root, harness).run()
        self.assertEqual(harness.count("review"), 0)
        self.assertFalse((self.root / "iterations/0001/publication.json").exists())

    def test_pending_publication_replay_checks_actual_candidate(self):
        study, harness = self.prepare()
        with patch.object(study, "_publish", side_effect=ProcessLost):
            with self.assertRaises(ProcessLost):
                study.run()
        self.assertTrue((self.root / "iterations/0001/publication.json").exists())
        harness.identity_valid = False
        with self.assertRaisesRegex(HarnessError, "frozen A"):
            Study(self.root, harness).run()
        self.assertEqual(harness.count("review"), 1)
        self.assertEqual(self.queue.snapshot()[0]["status"], "needs_attention")
