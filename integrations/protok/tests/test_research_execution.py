"""Real local bridge/freezing with a settled, non-computing backend receipt."""

import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from integrations.protok.backend import CANDIDATE_FILES, ProTokBackend, ProTokError
from integrations.protok.continuation import install_hooks
from integrations.protok.research_execution import research_usage
from research_loop.harness import HarnessError
from research_loop.submission import claim_request, submit
import test_protok_runner as runner_tests
from test_research_contract import contract

RUNNER = runner_tests.RUNNER


class ResearchExecutionTests(unittest.TestCase):
    command = runner_tests.ProTokRunnerTests.command

    def setUp(self):
        runner_tests.ProTokRunnerTests.setUp(self)
        parent = self.directory / "0002"
        parent.mkdir()
        self.workspace.rename(parent / "workspace")
        self.workspace = parent / "workspace"
        self.iteration = "0002"
        self.contract = contract()
        self.contract["execution"].update(
            entrypoint="research/main.py", gpu=True, inputs=[], outputs=["result.json"]
        )
        self.plan_path = self.root / "study/iterations/0002/plan/response.json"
        self.write_plan()
        RUNNER.write(
            self.root / "study/state.json",
            {"iteration": 2, "plan_ref": "iterations/0002/plan/response.json"},
        )
        RUNNER.write(
            self.root / "study/study.json",
            {"baseline": self.baseline, "research_execution": {"version": 1}},
        )
        RUNNER.write(
            self.control / "research-policy.json",
            {
                "version": 1,
                "authorization": "Bounded research test",
                "max_time_limit_seconds": 1800,
            },
        )
        source = self.workspace / "research/main.py"
        source.parent.mkdir()
        source.write_text('print("paired conditioning analysis")\n')
        self.script_source = source.read_bytes()
        self.backend_runs = 0
        self.receipt_status = "completed"
        self.run_script = self.enterContext(
            patch(
                "integrations.protok.research_backend.run_script",
                side_effect=self.settled_script,
            )
        )

    def write_plan(self):
        RUNNER.write(
            self.plan_path,
            {"decision": "investigate", "research_contract": self.contract},
        )

    def settled_script(
        self, backend, *, root, iteration, source, execution, deadline, cancel_event
    ):
        self.backend_runs += 1
        self.assertIs(backend, self.backend)
        self.assertFalse(cancel_event.is_set())
        self.assertEqual(deadline, self.deadline)
        self.assertEqual(execution, self.contract["execution"])
        self.assertEqual((source / "research/main.py").read_bytes(), self.script_source)
        for name in CANDIDATE_FILES:
            self.assertEqual(
                (source / name).read_bytes(), (self.workspace / name).read_bytes()
            )
        identity = hashlib.sha256(
            json.dumps(
                RUNNER.signatures(RUNNER.candidate_bytes(source)), sort_keys=True
            ).encode()
        ).hexdigest()
        job = root / "control/research-jobs" / iteration
        RUNNER.write(
            job / "job.json",
            {
                "allocated_seconds": 60,
                "source_identity": identity,
                "execution_identity": "frozen-plan-input-execution-v1",
            },
        )
        receipt = {
            "status": self.receipt_status,
            "purpose": "research",
            "quality_score_eligible": False,
            "source_identity": identity,
            "execution_identity": "frozen-plan-input-execution-v1",
            "gpu_seconds": 30,
            "outputs": [{"path": "result.json", "bytes": 24}],
            "evidence": [str(job / "receipt.json")],
        }
        RUNNER.write(job / "receipt.json", receipt)
        return receipt

    def evaluate(self):
        return self.harness.evaluate_request(
            self.workspace,
            self.iteration,
            {"action": "research"},
            self.stop,
            self.deadline,
        )

    def accepted_response(self):
        submit(self.workspace, "research")
        self.assertEqual(claim_request(self.workspace), {"action": "research"})
        trusted = self.evaluate()
        RUNNER.write(self.workspace / "evaluation-result.json", trusted)
        response = {
            "outcome": "completed"
            if trusted["status"] == "completed"
            else "incomplete",
            "markdown": "Only predeclared research evidence; no formal score.",
            "artifacts": [trusted["artifact"]],
            "measurements": [],
        }
        return trusted, response

    def validate(self, response):
        return self.harness.validate_completed(
            response, workspace=self.workspace, run_dir=self.directory / "attempt"
        )

    def test_freezes_exact_source_and_accepts_only_unscored_artifact(self):
        with patch.object(
            self.backend,
            "evaluate",
            side_effect=AssertionError("formal evaluate forbidden"),
        ):
            trusted, response = self.accepted_response()
            self.assertEqual(self.validate(response), response)
        source = Path(trusted["frozen_source"])
        self.assertEqual((source / "research/main.py").read_bytes(), self.script_source)
        self.assertEqual(trusted["contract"], self.contract)
        self.assertEqual(RUNNER.git(source, "status", "--porcelain"), "")
        self.assertFalse(trusted["quality_score_eligible"])
        self.assertEqual(response["measurements"], [])
        self.assertEqual(len(response["artifacts"]), 1)
        self.assertEqual(
            response["artifacts"][0]["version"], "frozen-plan-input-execution-v1"
        )
        self.assertNotEqual(
            response["artifacts"][0]["version"], trusted["source_identity"]
        )
        self.assertEqual(self.backend.evaluations, 0)
        self.assertEqual(self.backend_runs, 1)

    def test_settled_debug_failure_cannot_hide_unknown_research_job(self):
        response = {
            "outcome": "incomplete",
            "markdown": "Settled failed candidate repair",
            "artifacts": [],
            "measurements": [],
        }
        RUNNER.write(
            self.root / "control/debug-policy.json",
            {
                "schema": 1,
                "gpu_seconds_per_investigation": 1800,
                "independent_of_formal_budget": True,
                "authorization": "test",
            },
        )
        RUNNER.write(
            self.root / "control/debug" / self.iteration / "decision.json",
            {
                "status": "failed",
                "response": response,
                "source_signatures": RUNNER.signatures(
                    RUNNER.candidate_bytes(self.workspace)
                ),
            },
        )
        self.assertEqual(self.validate(response), response)
        status = self.backend.inspect()
        with patch.object(
            self.backend,
            "inspect",
            return_value=status | {"research_outstanding": ["unknown-owned-job"]},
        ):
            with self.assertRaisesRegex(HarnessError, "unresolved effects"):
                self.validate(response)

    def test_fixed_plan_cannot_submit_research(self):
        self.contract["execution"].update(mode="fixed_evaluation", entrypoint=None)
        self.write_plan()
        with self.assertRaisesRegex(ProTokError, "execution mode"):
            self.evaluate()
        self.run_script.assert_not_called()

    def test_identical_repeat_reuses_settled_script_without_compute(self):
        first = self.evaluate()
        self.assertEqual(self.evaluate(), first)
        self.assertEqual(self.backend_runs, 1)

    def test_changed_source_rejected_on_repeat_and_completed_validation(self):
        _, response = self.accepted_response()
        (self.workspace / "research/main.py").write_text("print('changed')\n")
        with self.assertRaisesRegex(ProTokError, "replace a submitted"):
            self.evaluate()
        with self.assertRaisesRegex(ProTokError, "changed after submission"):
            self.validate(response)
        self.assertEqual(self.backend_runs, 1)

    def test_fabricated_formal_measurement_and_extra_artifact_rejected(self):
        _, response = self.accepted_response()
        altered = copy.deepcopy(response)
        altered["measurements"] = [
            {
                "candidate_id": "invented",
                "artifact": response["artifacts"][0],
                "evaluation_id": "se3-search-v2-2k-subset-seed42-v1",
                "comparison_scope": "se3-search-v2-2k-subset-seed42-v1",
                "metrics": [{"name": "subset_ca_rmsd_2000_seed42", "value": 0.01}],
                "evidence": [response["artifacts"][0]["path"]],
            }
        ]
        with self.assertRaisesRegex(HarnessError, "unscored research receipt"):
            self.validate(altered)
        altered = copy.deepcopy(response)
        altered["artifacts"].append({"path": "fabricated.json", "version": "v1"})
        with self.assertRaisesRegex(HarnessError, "unscored research receipt"):
            self.validate(altered)

    def test_settled_failed_script_is_incomplete_with_its_own_artifact(self):
        self.receipt_status = "failed"
        trusted, response = self.accepted_response()
        self.assertEqual(response["outcome"], "incomplete")
        self.assertEqual(self.validate(response), response)
        response["outcome"] = "completed"
        with self.assertRaisesRegex(HarnessError, "unscored research receipt"):
            self.validate(response)
        self.assertEqual(trusted["status"], "failed")

    def test_research_usage_charges_shared_budget_and_unknown_job_blocks_admission(
        self,
    ):
        self.evaluate()
        self.assertEqual(
            research_usage(self.control),
            {"gpu_seconds": 30, "outstanding": [], "overruns": []},
        )
        real_backend = ProTokBackend(self.project, self.control, "test-owner")
        with patch.object(
            real_backend,
            "_config",
            return_value={"records_root": str(self.control / "formal-records")},
        ):
            status = real_backend.inspect()
            self.assertEqual(status["research_gpu_seconds"], 30)
            self.assertEqual(status["used_gpu_seconds"], 30)
            self.assertEqual(status["total_gpu_seconds"], 30)
            unknown = self.control / "research-jobs/0003/job.json"
            RUNNER.write(
                unknown, {"allocated_seconds": 60, "source_identity": "unknown"}
            )
            self.assertEqual(
                research_usage(self.control)["outstanding"], [str(unknown.parent)]
            )
            with self.assertRaisesRegex(ProTokError, "Outstanding"):
                RUNNER.admission(self.root, real_backend)

    def test_symlink_research_source_is_rejected_without_compute(self):
        source = self.workspace / "research/main.py"
        source.unlink()
        source.symlink_to(self.project / CANDIDATE_FILES[0])
        with self.assertRaisesRegex(ProTokError, "regular source"):
            self.evaluate()
        self.run_script.assert_not_called()

    def formal_hooks(self):
        backend = Mock()
        backend.duration.return_value = 60
        backend.used_gpu_seconds.return_value = 1000
        backend.session_state.return_value = {}
        config = {
            "records_root": str(self.control / "formal-records"),
            "time_limit": "00:01:00",
            "reserve_gpu_seconds": 10,
        }
        control = {
            "schema": 2,
            "owner_token": "owner",
            "effective_deadline": self.deadline,
            "initial_gpu_seconds": 1000,
            "additional_gpu_seconds": 100,
        }
        original_submit = backend.submit
        install_hooks(backend, config, control, lambda: None, control_dir=self.control)
        return backend, config, control, original_submit

    def test_formal_hook_counts_research_cost_and_rejects_insufficient_increment(self):
        self.evaluate()
        backend, config, control, original_submit = self.formal_hooks()
        self.assertEqual(backend.budget_check(config, {}, None, 1), 1030)
        control["additional_gpu_seconds"] = 99
        with self.assertRaisesRegex(RuntimeError, "Insufficient incremental GPU"):
            backend.submit(config, None, {}, {})
        original_submit.assert_not_called()
        # Slurm may settle above --time; charge every second to the shared budget.
        receipt_path = self.control / "research-jobs/0002/receipt.json"
        receipt = RUNNER.read(receipt_path)
        receipt.update(status="failed", terminal_state="TIMEOUT", gpu_seconds=61)
        RUNNER.write(receipt_path, receipt)
        usage = research_usage(self.control)
        self.assertEqual(usage["gpu_seconds"], 61)
        self.assertEqual(usage["overruns"][0]["overrun_seconds"], 1)
        control["additional_gpu_seconds"] = 131
        self.assertEqual(backend.budget_check(config, {}, None, 1), 1061)
        control["additional_gpu_seconds"] = 130
        with self.assertRaisesRegex(RuntimeError, "Insufficient incremental GPU"):
            backend.submit(config, None, {}, {})
        original_submit.assert_not_called()
        control["additional_gpu_seconds"] = 131
        backend.submit(config, None, {}, {})
        original_submit.assert_called_once()

    def test_mismatched_execution_identity_still_blocks_accounting(self):
        self.evaluate()
        path = self.control / "research-jobs/0002/receipt.json"
        receipt = RUNNER.read(path)
        receipt["execution_identity"] = "another-job"
        RUNNER.write(path, receipt)
        with self.assertRaisesRegex(ProTokError, "inconsistent"):
            research_usage(self.control)

    def test_formal_hook_refuses_new_job_with_unknown_research_accounting(self):
        RUNNER.write(
            self.control / "research-jobs/0003/job.json",
            {"allocated_seconds": 60, "source_identity": "unknown"},
        )
        backend, config, _, original_submit = self.formal_hooks()
        with self.assertRaisesRegex(RuntimeError, "Unsettled research allocation"):
            backend.submit(config, None, {}, {})
        original_submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
