import copy
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from research_loop import Study, initialize
from research_loop.harness import HarnessError, completed_response
from research_loop.submission import claim_request, submit
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

    def test_begin_preserves_original_plan_and_next_stage_is_execute_then_review(
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
        original_plan = read(self.root / "study" / self.before["plan_ref"])
        self.assertEqual(metadata["base_experiment"], original_plan["base_experiment"])
        self.assertEqual(
            metadata["continuation"]["source_signatures"],
            pending["source_signatures"],
        )
        self.assertEqual(
            metadata["route_id"], read(self.source / "iteration.json")["route_id"]
        )
        self.assertEqual(state["plan_ref"], self.before["plan_ref"])
        self.assertEqual(state["selected"], self.before["selected"])
        self.assertEqual(state["calls"], {})
        self.assertEqual(metadata["continuation"]["plan_ref"], self.before["plan_ref"])
        self.assertFalse((self.root / "study/iterations/0002/operator-plan").exists())
        self.assertFalse((self.root / "study/iterations/0002/selection.json").exists())
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
            {"research_outstanding": ["unknown"]},
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

    def assert_published_fault_refused(self, name, content, message):
        path = self.workspace / name
        path.write_bytes(content)
        subprocess.run(
            ["git", "-C", str(self.workspace), "add", "--", name],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.workspace),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "--quiet",
                "-m",
                "Published static candidate fault fixture",
            ],
            check=True,
            capture_output=True,
        )
        version = subprocess.run(
            ["git", "-C", str(self.workspace), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        metadata = read(self.source / "iteration.json")
        metadata["workspace_ref"] = version
        publication = read(self.source / "publication.json")
        publication["metadata"] = metadata
        write(self.source / "iteration.json", metadata)
        write(self.source / "publication.json", publication)
        previous_evidence = self.control / "reconciliations/previous.json"
        write(previous_evidence, {"source": "retained operator evidence"})
        protected = {
            candidate: candidate.read_bytes()
            for candidate in (
                self.source / "iteration.json",
                self.source / "publication.json",
                self.root / "study/state.json",
                self.root / "study/study.json",
                self.root / "study" / self.before["plan_ref"],
                path,
                self.control / "STOP",
                previous_evidence,
            )
        }
        with self.assertRaisesRegex(ProTokError, message):
            self.prepare()
        for candidate, contents in protected.items():
            self.assertEqual(candidate.read_bytes(), contents)
        self.assertFalse((self.control / "resume-submission.json").exists())
        self.assertEqual(
            list((self.control / "reconciliations").iterdir()), [previous_evidence]
        )
        self.backend.evaluate.assert_not_called()

    def test_published_syntax_fault_cannot_resume_as_transport_interruption(self):
        self.assert_published_fault_refused(
            CANDIDATE_FILES[0], b"def rejected_candidate(:\n", "static source fault"
        )

    def test_published_invalid_json_cannot_resume_as_transport_interruption(self):
        self.assert_published_fault_refused(
            CANDIDATE_FILES[2], b'{"invalid":', "static source fault"
        )

    def test_published_unverified_parsing_cannot_resume_as_transport_interruption(self):
        self.assert_published_fault_refused(
            CANDIDATE_FILES[2], b"\xff", "static parsing is unverified"
        )

    def test_same_iteration_retains_static_fault_for_original_execute_to_repair(self):
        study = self.interrupt()
        path = self.workspace / CANDIDATE_FILES[0]
        source = b"def unfinished_candidate(:\n"
        path.write_bytes(source)
        pending = self.prepare()
        self.assertEqual(pending["mode"], "same_iteration")
        self.assertEqual(path.read_bytes(), source)
        self.assertEqual(study.status()["plan_ref"], self.before["plan_ref"])
        self.assertEqual(study.status()["iteration"], self.before["iteration"])

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

    def interrupt(self, *, legacy_completed=False, script=False):
        previous_control = self.control
        self.root = self.root.parent / "interrupted"
        initialize(
            self.root / "study",
            goal="Evaluate the fixed candidate.",
            workspace=self.project,
            allow_write=True,
            max_seconds=36000,
            research_execution={"version": 1} if script else None,
        )
        self.control = self.root / "control"
        self.control.mkdir()
        for name in ("ownership.json", "continuation.json"):
            shutil.copyfile(previous_control / name, self.control / name)

        class InterruptHarness(IncompleteHarness):
            def run(self, **kwargs):
                if not kwargs["writable"]:
                    return super().run(**kwargs)
                if not legacy_completed:
                    outer = kwargs["run_dir"]
                    agent = outer / "agent"
                    agent.mkdir()
                    shutil.copyfile(outer / "context.json", agent / "context.json")
                    kwargs["run_dir"] = agent
                super().run(**kwargs)
                raise HarnessError("Unsubmitted transport interruption")

            def response(self, phase, context, kwargs):
                if phase == "execute":
                    (Path(kwargs["workspace"]) / CANDIDATE_FILES[0]).write_text(
                        "# legitimate partial candidate intervention\n"
                    )
                result = super().response(phase, context, kwargs)
                if phase == "plan" and script:
                    from test_research_contract import contract

                    result.update(contract())
                return result

        study = Study(self.root / "study", harness=InterruptHarness())
        with self.assertRaisesRegex(HarnessError, "transport interruption"):
            study.run()
        self.before = study.status()
        self.source = self.root / "study/iterations/0001"
        self.workspace = Path(read(self.source / "iteration.json")["workspace"])
        write(self.control / "runner.json", {"status": "failed", "iteration": 1})
        (self.control / "STOP").write_text("Execute interrupted; reconcile first\n")
        return study

    def test_script_execute_cannot_be_retired_or_replayed_by_unsubmitted_resume(self):
        study = self.interrupt(script=True)
        journal = self.control / "research-executions/0001/execution.json"
        write(journal, {"status": "closed", "submission_intent": False})
        attempt = study.root / self.before["calls"]["execute"]
        protected = {
            path: path.read_bytes()
            for path in (
                study.root / "state.json",
                study.root / self.before["plan_ref"],
                attempt / "agent/response.json",
                attempt / "agent/process.json",
                self.control / "STOP",
                journal,
            )
        }
        with self.assertRaisesRegex(ProTokError, "--reconcile-execute"):
            self.prepare()
        for path, contents in protected.items():
            self.assertEqual(path.read_bytes(), contents)
        self.assertFalse((self.control / "resume-submission.json").exists())
        self.assertFalse((self.control / "reconciliations").exists())
        self.backend.evaluate.assert_not_called()

    def test_research_execution_records_prevent_formal_resume(self):
        self.interrupt()
        for namespace in ("research-executions", "research-sources", "research-jobs"):
            with self.subTest(namespace=namespace):
                directory = self.control / namespace / "0001"
                directory.mkdir(parents=True)
                with self.assertRaisesRegex(ProTokError, "execution record"):
                    self.prepare()
                self.assertEqual(read(self.root / "study/state.json"), self.before)
                self.assertFalse((self.control / "resume-submission.json").exists())
                directory.rmdir()

    def test_same_iteration_keeps_partial_source_plan_receipts_and_budgets(self):
        study = self.interrupt()
        attempt = study.root / self.before["calls"]["execute"]
        plan_path = study.root / self.before["plan_ref"]
        protected = {
            path: path.read_bytes()
            for path in (
                plan_path,
                attempt / "agent/response.json",
                attempt / "agent/process.json",
                self.source / "iteration.json",
                self.workspace / CANDIDATE_FILES[0],
                self.control / "ownership.json",
                self.control / "continuation.json",
                study.root / "study.json",
            )
        }
        self.assertIsNone(completed_response(attempt))
        pending = self.prepare()
        self.assertEqual(pending["mode"], "same_iteration")
        self.assertEqual(pending["source_iteration"], pending["target_iteration"])
        self.assertEqual(self.prepare(), pending)
        prepared = resume.validate_prepared(self.root, self.backend)
        expected = copy.deepcopy(self.before)
        expected["calls"].pop("execute")
        expected["accepted_calls"].pop("execute", None)
        expected.update(
            status="ready", active_attempt=None, error=None, stop_reason=None
        )
        self.assertEqual(prepared, expected)
        (self.control / "STOP").unlink()
        with patch.object(Study, "_begin", side_effect=AssertionError("new iteration")):
            resume.begin_pending(study, self.backend)
            resume.begin_pending(study, self.backend)
            self.assertEqual(study.status(), prepared)
            self.assertEqual(self.prepare()["status"], "begun")
            harness = IncompleteHarness()
            study.harness = harness
            result = study.run()
        self.assertEqual(result["iteration"], 1)
        self.assertEqual(
            [call["phase"] for call in harness.calls], ["execute", "review"]
        )
        self.assertEqual(result["call_count"], self.before["call_count"] + 2)
        for path, value in protected.items():
            if path == self.source / "iteration.json":
                continue  # Normal completion updates this record after preparation.
            self.assertEqual(path.read_bytes(), value)
        self.assertEqual(result["plan_ref"], self.before["plan_ref"])
        self.assertEqual(result["selected"], self.before["selected"])
        self.backend.evaluate.assert_not_called()

    def test_legacy_completed_unaccepted_receipt_is_retired_without_replay(self):
        study = self.interrupt(legacy_completed=True)
        attempt = study.root / self.before["calls"]["execute"]
        self.assertIsNotNone(completed_response(attempt))
        original = (attempt / "response.json").read_bytes()
        self.prepare()
        (self.control / "STOP").unlink()
        resume.begin_pending(study, self.backend)
        harness = IncompleteHarness()
        study.harness = harness
        final = study.run()
        self.assertEqual(final["iteration"], 1)
        self.assertEqual(harness.count("execute"), 1)
        self.assertNotEqual(final["calls"]["execute"], self.before["calls"]["execute"])
        self.assertEqual((attempt / "response.json").read_bytes(), original)

    def test_same_iteration_multiple_failures_have_distinct_immutable_events(self):
        study = self.interrupt()
        first = self.prepare()
        first_bytes = Path(first["reconciliation"]).read_bytes()
        (self.control / "STOP").unlink()
        resume.begin_pending(study, self.backend)
        with self.assertRaisesRegex(HarnessError, "transport interruption"):
            study.run()
        (self.control / "STOP").write_text("second interruption\n")
        second = self.prepare()
        self.assertEqual(second["target_iteration"], "0001")
        self.assertNotEqual(first["source_attempt"], second["source_attempt"])
        self.assertNotEqual(first["reconciliation"], second["reconciliation"])
        self.assertEqual(Path(first["reconciliation"]).read_bytes(), first_bytes)
        self.assertEqual(self.prepare(), second)

    def test_same_iteration_refuses_pending_accepted_jobs_and_live_cli(self):
        study = self.interrupt()
        for accepted in (False, True):
            submit(self.workspace)
            if accepted:
                claim_request(self.workspace)
            with self.assertRaises(ProTokError):
                self.prepare()
            for name in ("evaluation-request.json", "evaluation-submission.json"):
                (self.workspace / name).unlink(missing_ok=True)
        (self.control / "jobs/0001").mkdir(parents=True)
        with self.assertRaises(ProTokError):
            self.prepare()
        (self.control / "jobs/0001").rmdir()
        attempt = study.root / self.before["calls"]["execute"]
        write(attempt / "agent/process.json", {"status": "running", "pid": os.getpid()})
        with self.assertRaisesRegex(Exception, "active CLI"):
            self.prepare()
        self.assertEqual(study.status(), self.before)

    def test_postpublication_continuation_reuses_original_plan_across_recoveries(self):
        original_plan = self.before["plan_ref"]
        self.prepare()
        (self.control / "STOP").unlink()
        study = Study(self.root / "study", harness=IncompleteHarness())
        resume.begin_pending(study, self.backend)
        study.run()
        (self.control / "STOP").write_text("next reviewed stop\n")
        second = self.prepare()
        self.assertEqual(second["source_iteration"], "0002")
        self.assertEqual(second["plan_ref"], original_plan)
        (self.control / "STOP").unlink()
        resume.begin_pending(study, self.backend)
        self.assertEqual(study.status()["iteration"], 3)
        self.assertEqual(study.status()["plan_ref"], original_plan)
        self.assertEqual(study.status()["calls"], {})

    def test_legacy_operator_chain_resolves_published_original_plan(self):
        original_plan = self.before["plan_ref"]
        first = self.prepare()
        (self.control / "STOP").unlink()
        study = Study(self.root / "study", harness=IncompleteHarness())
        resume.begin_pending(study, self.backend)
        study.run()
        folder = study.root / "iterations/0002"
        metadata = read(folder / "iteration.json")
        metadata.pop("continuation")
        metadata["operator_resume"] = {
            "source_iteration": "0001",
            "source_workspace_ref": first["source_workspace_ref"],
        }
        state = study.status()
        operator = folder / "operator-plan/response.json"
        plan = read(study.root / original_plan)
        plan["markdown"] = "Legacy operator execution directive"
        write(operator, plan)
        state["plan_ref"] = str(operator.relative_to(study.root))
        state["calls"]["plan-0"] = str(operator.parent.relative_to(study.root))
        state["accepted_calls"]["plan-0"] = True
        publication = read(folder / "publication.json")
        publication.update(metadata=metadata, state_after=state)
        write(folder / "iteration.json", metadata)
        write(folder / "publication.json", publication)
        write(study.root / "state.json", state)
        legacy = read(Path(first["reconciliation"]))
        for key in (
            "mode",
            "prepared_state",
            "plan_ref",
            "directive",
            "source_attempt",
        ):
            legacy.pop(key)
        legacy_path = self.control / "reconciliations/legacy-resume.json"
        legacy["reconciliation"] = str(legacy_path)
        write(legacy_path, legacy)
        pointer = self.control / "resume-submission.json"
        write(pointer, {**legacy, "status": "begun"})
        previous = pointer.read_bytes()
        resume.begin_pending(study, self.backend)
        self.assertEqual(pointer.read_bytes(), previous)
        legacy_bytes = legacy_path.read_bytes()
        (self.control / "STOP").write_text("published legacy operator stop\n")
        second = self.prepare()
        self.assertEqual(second["plan_ref"], original_plan)
        self.assertEqual(legacy_path.read_bytes(), legacy_bytes)
        source_bytes = (folder / "publication.json").read_bytes()
        (self.control / "STOP").unlink()
        resume.begin_pending(study, self.backend)
        self.assertEqual(study.status()["plan_ref"], original_plan)
        self.assertEqual(study.status()["calls"], {})
        self.assertEqual((folder / "publication.json").read_bytes(), source_bytes)

    def test_same_iteration_prepare_crash_reuses_immutable_event(self):
        self.interrupt()
        with patch.object(Study, "_save", side_effect=OSError("prepare interrupted")):
            with self.assertRaisesRegex(OSError, "prepare interrupted"):
                self.prepare()
        pending = read(self.control / "resume-submission.json")
        evidence = Path(pending["reconciliation"])
        previous = evidence.read_bytes()
        self.assertEqual(self.prepare(), pending)
        self.assertEqual(evidence.read_bytes(), previous)

    def test_postpublication_begin_crash_reuses_reserved_execution(self):
        pending = self.prepare()
        (self.control / "STOP").unlink()
        study = Study(self.root / "study")
        original = Study._continue_execution

        def interrupted(study, *args, **kwargs):
            original(study, *args, **kwargs)
            raise OSError("begin interrupted after execution reservation")

        with patch.object(Study, "_continue_execution", interrupted):
            with self.assertRaisesRegex(OSError, "begin interrupted"):
                resume.begin_pending(study, self.backend)
        reserved = study.status()
        self.assertEqual((reserved["iteration"], reserved["phase"]), (2, "execute"))
        resume.begin_pending(study, self.backend)
        self.assertEqual(study.status(), reserved)
        self.assertEqual(
            read(self.control / "resume-submission.json"),
            {**pending, "status": "begun"},
        )

    def test_capability_upgrade_preserves_original_execution_policy_and_plan(self):
        config_path = self.root / "study/study.json"
        config = read(config_path)
        config["research_execution"] = {"version": 1}
        write(config_path, config)
        original_plan = (self.root / "study" / self.before["plan_ref"]).read_bytes()
        self.prepare()
        (self.control / "STOP").unlink()
        harness = IncompleteHarness()
        study = Study(self.root / "study", harness=harness)
        resume.begin_pending(study, self.backend)
        self.assertEqual(study.status()["plan_ref"], self.before["plan_ref"])
        self.assertNotIn(
            "research_execution", study._context(study._folder(study.status()))
        )
        study.run()
        self.assertEqual(
            [call["phase"] for call in harness.calls], ["execute", "review"]
        )
        self.assertTrue(
            all("research_execution" not in call["context"] for call in harness.calls)
        )
        self.assertEqual(
            (study.root / self.before["plan_ref"]).read_bytes(), original_plan
        )

    def test_capability_upgrade_does_not_allow_budget_or_goal_changes(self):
        config_path = self.root / "study/study.json"
        original = read(config_path)
        for key, value in (
            ("max_seconds", original["max_seconds"] + 1),
            ("max_calls", 1000),
        ):
            with self.subTest(key=key):
                write(
                    config_path,
                    {**original, "research_execution": {"version": 1}, key: value},
                )
                with self.assertRaisesRegex(ProTokError, "frozen scientific protocol"):
                    self.prepare()
        write(config_path, {**original, "research_execution": {"version": 1}})
        (self.root / "study/goal.md").write_text("Changed scientific question")
        with self.assertRaisesRegex(ProTokError, "frozen scientific protocol"):
            self.prepare()

    def test_same_iteration_capability_upgrade_reaches_original_execute_and_review(
        self,
    ):
        study = self.interrupt()
        attempt = study.root / self.before["calls"]["execute"]
        raw = (attempt / "agent/response.json").read_bytes()
        plan_path = study.root / self.before["plan_ref"]
        original_plan = plan_path.read_bytes()
        config_path = study.root / "study.json"
        config = read(config_path)
        config["research_execution"] = {"version": 1}
        write(config_path, config)
        pending = self.prepare()
        self.assertEqual(pending["mode"], "same_iteration")
        (self.control / "STOP").unlink()
        harness = IncompleteHarness()
        study = Study(self.root / "study", harness=harness)
        resume.begin_pending(study, self.backend)
        completed = study.run()
        self.assertEqual((completed["iteration"], completed["phase"]), (1, "done"))
        self.assertEqual(
            [call["phase"] for call in harness.calls], ["execute", "review"]
        )
        self.assertTrue(
            all("research_execution" not in call["context"] for call in harness.calls)
        )
        self.assertEqual(completed["plan_ref"], self.before["plan_ref"])
        self.assertEqual(completed["selected"], self.before["selected"])
        self.assertEqual(completed["call_count"], self.before["call_count"] + 2)
        self.assertEqual(plan_path.read_bytes(), original_plan)
        self.assertEqual((attempt / "agent/response.json").read_bytes(), raw)
        self.assertTrue((self.source / "publication.json").exists())
        self.backend.evaluate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
