"""Metadata transport failure closes through real Review without native execution."""

import copy
import json
from pathlib import Path
import subprocess
import time
import unittest
from unittest.mock import patch

from integrations.protok import preparation_failure as preparation
from integrations.protok.backend import ProTokError
from integrations.protok.research_execution import completed_script, research_usage
from integrations.protok.round_admission import freeze_execution_deadline
from research_loop.harness import HarnessError, completed_response
import test_research_failure as failure_fixture

RUNNER = failure_fixture.RUNNER


class PreparationFailureTests(unittest.TestCase):
    setUp = failure_fixture.ResearchFailureTests.setUp
    command = failure_fixture.ResearchFailureTests.command
    run_one = failure_fixture.ResearchFailureTests.run_one

    def script(self, backend, *, root, iteration, **kwargs):
        freeze_execution_deadline(
            root,
            time.time() - 1,
            owner_token=backend.owner_token,
            attempt=f"research/{iteration}",
        )
        raise subprocess.CalledProcessError(255, ["ssh", "test-hpc"])

    def prepare_failure(self):
        failure_fixture.ResearchFailureTests.initialize(self)
        response = self.delegate.response

        def bounded_response(phase, context, kwargs):
            value = response(phase, context, kwargs)
            if phase == "plan":
                value["research_contract"]["execution"]["time_limit_seconds"] = 300
            return value

        self.delegate.response = bounded_response
        with self.assertRaises(HarnessError):
            self.run_one()
        self.before = self.study.status()
        self.workspace = Path(
            RUNNER.read(self.study.root / "iterations/0002/iteration.json")["workspace"]
        )
        self.interface = RUNNER.read(self.workspace / "evaluation-result.json")
        outer_path = (
            self.study.root / self.before["active_attempt"] / "failure-diagnostics.json"
        )
        outer = RUNNER.read(outer_path)
        self.assertEqual(outer["server"], self.interface["diagnostics"])
        # Trusted controller frames are synthetic because this fixture replaces
        # run_script; no SSH or native backend is invoked by the test.
        self.interface["diagnostics"]["cause_chain"][0]["frames"] = [
            {
                "file": "/runtime/research_execution.py",
                "function": "execute_script",
                "line": 1,
            },
            {
                "file": "/runtime/research_backend.py",
                "function": "run_script",
                "line": 2,
            },
            {
                "file": "/runtime/research_backend.py",
                "function": "_input_metadata",
                "line": 3,
            },
        ]
        outer["server"] = copy.deepcopy(self.interface["diagnostics"])
        RUNNER.write(outer_path, outer)
        RUNNER.write(self.workspace / "evaluation-result.json", self.interface)
        self.folder = self.control / "research-sources/0002"
        self.directory = self.control / "research-jobs/0002"

    def certify(self):
        return preparation.certify(
            self.root,
            self.backend,
            "0002",
            authorization="Explicitly close the retained metadata transport failure",
        )

    def test_zero_allocation_reaches_old_completed_script_and_real_review(self):
        self.prepare_failure()
        previous = self.study.root / self.before["active_attempt"]
        original_agent = (previous / "agent/response.json").read_bytes()
        outer_diagnostics = (previous / "failure-diagnostics.json").read_bytes()
        plan = (self.study.root / self.before["plan_ref"]).read_bytes()
        source_record = (self.folder / "record.json").read_bytes()
        source = (self.folder / "source/research/main.py").read_bytes()
        result = self.certify()
        certificate = RUNNER.read(self.directory / "preparation-failure.json")
        self.assertEqual(
            certificate["attempt_files"]["failure-diagnostics.json"],
            json.loads(outer_diagnostics),
        )
        self.assertEqual(self.study.status(), self.before)
        self.assertEqual(result["requested_seconds"], 300)
        self.assertEqual(result["allocated_seconds"], 0)
        self.assertEqual(result["gpu_seconds"], 0)
        self.assertEqual(result["cpu_seconds"], 0)
        self.assertEqual(result["identity_kind"], "preparation_certificate")
        self.assertIsNone(result["native_execution_identity"])
        self.assertTrue(result["execution_identity"].startswith("preparation:"))
        self.assertFalse(result["quality_score_eligible"])
        self.assertEqual(result["outputs"], [])
        self.assertEqual(
            set(p.name for p in self.directory.iterdir()),
            {"job.json", "receipt.json", "preparation-failure.json"},
        )
        for name in ("state.json", "allocation", "remote", "inputs.json"):
            self.assertFalse((self.directory / name).exists())
        for name in (
            "owned_job_id",
            "job_id",
            "terminal_state",
            "exit_code",
            "remote",
            "inputs",
        ):
            self.assertNotIn(name, result)
        self.assertEqual(completed_script(self.root, self.workspace, "0002"), result)
        self.assertEqual(
            research_usage(self.control),
            {"gpu_seconds": 0, "outstanding": [], "overruns": []},
        )
        self.assertEqual(self.certify(), result)
        state = RUNNER.reconcile_research_execute(
            self.root,
            self.backend,
            authorization="Accept the preparation failure for Review",
        )
        response = completed_response(self.study.root / state["calls"]["execute"])
        self.assertEqual(response["outcome"], "incomplete")
        self.assertEqual(response["measurements"], [])
        self.assertEqual(
            preparation.validate(self.root, self.workspace, "0002"), result
        )
        self.assertEqual(
            (previous / "agent/response.json").read_bytes(), original_agent
        )
        self.assertEqual(
            (previous / "failure-diagnostics.json").read_bytes(), outer_diagnostics
        )
        self.assertEqual((self.study.root / self.before["plan_ref"]).read_bytes(), plan)
        self.assertEqual((self.folder / "record.json").read_bytes(), source_record)
        self.assertEqual((self.folder / "source/research/main.py").read_bytes(), source)
        state = self.run_one()
        self.assertEqual(
            (state["iteration"], state["phase"], state["status"]), (2, "done", "ready")
        )
        self.assertEqual(self.allocations, 0)
        self.assertEqual(self.delegate.count("execute"), 1)
        self.assertEqual(self.delegate.count("review"), 1)

    def test_only_exact_metadata_failure_stage_is_accepted(self):
        self.prepare_failure()
        for mutate in (
            lambda row: row.update(status="completed"),
            lambda row: row.update(value=0),
            lambda row: row["diagnostics"].update(returncode=1),
            lambda row: row["diagnostics"].update(stage="submit"),
            lambda row: row["diagnostics"]["cause_chain"][0].update(frames=[]),
        ):
            changed = copy.deepcopy(self.interface)
            mutate(changed)
            RUNNER.write(self.workspace / "evaluation-result.json", changed)
            with self.assertRaises(ProTokError):
                self.certify()
            self.assertFalse(self.directory.exists())

    def test_native_or_reserved_effects_prevent_publication(self):
        self.prepare_failure()
        for namespace in ("research-jobs", "jobs", "debug"):
            path = self.control / namespace / "0002"
            path.mkdir(parents=True)
            RUNNER.write(path / "job.json", {"allocated_seconds": 300})
            with self.assertRaises(ProTokError):
                self.certify()
            (path / "job.json").unlink()
            path.rmdir()
        key = json.dumps(["research", str(self.directory), "allocation"])
        RUNNER.write(
            self.control / "shared-gpu-budget.json",
            {"attempts": {key: {"phase": "submitting"}}},
        )
        with self.assertRaisesRegex(ProTokError, "shared reservation"):
            self.certify()
        self.assertFalse(self.directory.exists())

    def test_changed_source_and_operator_stop_prevent_publication(self):
        self.prepare_failure()
        stop = self.control / "STOP"
        stop.write_text("operator stop\n")
        with self.assertRaises(ProTokError):
            self.certify()
        stop.unlink()
        (self.workspace / "research/main.py").write_text("changed = True\n")
        with self.assertRaisesRegex(ProTokError, "source changed"):
            self.certify()
        self.assertFalse(self.directory.exists())

    def test_terminal_directory_is_published_together_and_corruption_rejected(self):
        self.prepare_failure()
        rename = Path.rename
        observations = []

        def observe(path, target):
            if Path(target) == self.directory:
                observations.append(set(p.name for p in path.iterdir()))
                self.assertFalse(self.directory.exists())
                self.assertNotIn("research-jobs", path.parts)
            return rename(path, target)

        with patch.object(Path, "rename", observe):
            result = self.certify()
        self.assertEqual(
            observations, [{"job.json", "receipt.json", "preparation-failure.json"}]
        )
        receipt = RUNNER.read(self.directory / "receipt.json")
        RUNNER.write(self.directory / "receipt.json", {**receipt, "gpu_seconds": 1})
        with self.assertRaisesRegex(
            ProTokError, "records acquired native effects or changed"
        ):
            preparation.validate(self.root, self.workspace, "0002")
        RUNNER.write(self.directory / "receipt.json", receipt)
        RUNNER.write(self.folder / "result.json", {**result, "requested_seconds": 301})
        with self.assertRaisesRegex(ProTokError, "trusted result changed"):
            preparation.validate(self.root, self.workspace, "0002")
        RUNNER.write(self.folder / "result.json", result)
        key = json.dumps(["research", str(self.directory), "allocation"])
        RUNNER.write(
            self.control / "shared-gpu-budget.json",
            {"attempts": {key: {"phase": "submitting"}}},
        )
        with self.assertRaisesRegex(ProTokError, "shared reservation"):
            preparation.validate(self.root, self.workspace, "0002")

    def test_crash_after_terminal_directory_publication_can_finish_without_replay(self):
        self.prepare_failure()
        publish = preparation._publish

        def interrupted(path, data):
            if path == self.folder / "result.json":
                raise OSError("simulated result publication interruption")
            return publish(path, data)

        with patch.object(preparation, "_publish", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "publication interruption"):
                self.certify()
        self.assertTrue((self.directory / "job.json").exists())
        self.assertTrue((self.directory / "receipt.json").exists())
        self.assertFalse((self.folder / "result.json").exists())
        original = (self.directory / "preparation-failure.json").read_bytes()
        result = self.certify()
        self.assertEqual(
            (self.directory / "preparation-failure.json").read_bytes(), original
        )
        self.assertEqual(completed_script(self.root, self.workspace, "0002"), result)
        self.assertEqual(self.allocations, 0)

    def test_existing_source_result_blocks_first_terminal_publication(self):
        self.prepare_failure()
        result_path = self.folder / "result.json"
        original = {"status": "error", "retained": "existing research result"}
        RUNNER.write(result_path, original)
        with self.assertRaisesRegex(ProTokError, "existing research result"):
            self.certify()
        self.assertFalse(self.directory.exists())
        self.assertEqual(RUNNER.read(result_path), original)

    def test_workspace_diagnostics_cannot_replace_controller_evidence(self):
        self.prepare_failure()
        changed = copy.deepcopy(self.interface)
        changed["diagnostics"]["cause_chain"][0]["frames"][0]["line"] += 1
        RUNNER.write(self.workspace / "evaluation-result.json", changed)
        with self.assertRaisesRegex(ProTokError, "diagnostics differ"):
            self.certify()
        self.assertFalse(self.directory.exists())

    def test_null_server_is_limited_to_pinned_legacy_controller(self):
        self.prepare_failure()
        path = (
            self.study.root / self.before["active_attempt"] / "failure-diagnostics.json"
        )
        outer = RUNNER.read(path)
        outer["server"] = None
        outer["returncode"] = None
        outer["cause_chain"][0]["frames"] = [
            {
                "file": "/runtime" + preparation.LEGACY_RELEASE + suffix,
                "function": function,
                "line": 1,
            }
            for function, suffix in (
                ("_call", "src/research_loop/core.py"),
                ("run", "integrations/protok/run.py"),
            )
        ]
        wrong = copy.deepcopy(outer)
        wrong["cause_chain"][0]["frames"][0]["file"] = wrong["cause_chain"][0][
            "frames"
        ][0]["file"].replace(preparation.LEGACY_RELEASE, "/releases/other-revision/")
        RUNNER.write(path, wrong)
        with self.assertRaisesRegex(ProTokError, "exact legacy controller release"):
            self.certify()
        self.assertFalse(self.directory.exists())
        RUNNER.write(path, outer)
        result = self.certify()
        certificate = RUNNER.read(self.directory / "preparation-failure.json")
        self.assertEqual(
            certificate["controller_error_binding"], "pinned_legacy_write_ahead_order"
        )
        self.assertEqual(result["allocated_seconds"], 0)
        self.assertEqual(
            preparation.validate(self.root, self.workspace, "0002"), result
        )
