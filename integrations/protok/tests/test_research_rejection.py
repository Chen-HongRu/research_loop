"""A rejected research submission reaches Review without replay or fake Slurm IDs."""

import copy
from datetime import datetime, timedelta, timezone
import json
import time
import unittest

from integrations.protok import research_rejection as rejection
from integrations.protok.shared_budget import SharedBudget, initialize_shared_budget
from research_loop.harness import HarnessError, completed_response
from test_research_failure import ResearchFailureTests, RUNNER
from test_shared_budget import policy


class ResearchRejectionTests(unittest.TestCase):
    setUp = ResearchFailureTests.setUp
    command = ResearchFailureTests.command
    run_one = ResearchFailureTests.run_one

    def initialize(self):
        ResearchFailureTests.initialize(self)
        now = time.time()
        initialize_shared_budget(self.control, policy(now - 1200))
        self.budget = SharedBudget(self.control)
        self.budget.reserve("manual-existing", "manual", 10800)
        self.budget.reconcile("manual-existing", {"owned_job_id": "794039"})

    def script(self, backend, *, root, iteration, execution, **kwargs):
        self.allocations += 1
        record = self.control / "research-jobs" / iteration
        self.key = json.dumps(["research", str(record), "allocation"])
        self.budget.reserve(self.key, "research", 1200)
        self.original_job = {
            "status": "blocked",
            "source_identity": "original-source",
            "execution_identity": "original-execution",
            "allocated_seconds": 1200,
            "execution": execution,
            "owner": RUNNER.read(self.control / "ownership.json"),
            "continuation": RUNNER.read(self.control / "continuation.json"),
            "remote": "/remote/unique-script",
            "base_config": {"ssh_host": "test-hpc"},
        }
        now = time.time()
        self.original_state = {
            "purpose": "research",
            "status": "prepared",
            "attempts": [
                {
                    "status": "submitting",
                    "seed": 42,
                    "mode": "research",
                    "directory": "allocation",
                    "budget_seconds": 1200,
                    "submitted_at": now - 600,
                    "remote": self.original_job["remote"],
                }
            ],
        }
        stderr = "sbatch: error: default account: timeout: context deadline exceeded\nsbatch: error: Batch job submission failed: Access/permission denied\n"
        logs = [
            {
                "stage": "submit",
                "stream": "stdout",
                "returncode": 1,
                "bytes": 0,
                "tail": "",
                "recorded_at": now - 590,
            },
            {
                "stage": "submit",
                "stream": "stderr",
                "returncode": 1,
                "bytes": len(stderr.encode()),
                "tail": stderr,
                "recorded_at": now - 590,
            },
        ]
        RUNNER.write(record / "job.json", self.original_job)
        RUNNER.write(record / "state.json", self.original_state)
        RUNNER.write(
            record / "command-diagnostics/diagnostic-manifest.json", {"logs": logs}
        )
        blocked = {
            **{
                k: self.original_job[k]
                for k in (
                    "status",
                    "source_identity",
                    "execution_identity",
                    "allocated_seconds",
                )
            },
            "purpose": "research",
            "quality_score_eligible": False,
            "gpu_seconds": None,
            "outputs": [],
            "evidence": [str(record / "state.json")],
            "error": "Unresolved submission",
        }
        RUNNER.write(record / "blocked.json", blocked)
        submitted = (
            datetime.fromtimestamp(now - 1000, timezone(timedelta(hours=8)))
            .replace(tzinfo=None)
            .isoformat(timespec="seconds")
        )
        row = f"794039|testuser|{submitted}|RUNNING\n"
        self.audits = [
            {
                "account": "testuser",
                "observed_at": now + offset,
                "start": now - 700,
                "end": now + offset - 1,
                "remote": {
                    "path": self.original_job["remote"],
                    "job_id_exists": False,
                    "temporary_regular": True,
                    "temporary_size": 0,
                    "output_names": [],
                    "start_artifacts": [],
                },
                **{
                    name: {"returncode": 0, "stdout": row}
                    for name in ("squeue", "sacct", "control")
                },
            }
            for offset in (0, 60)
        ]
        for audit in self.audits:
            audit.update(
                ssh_host="test-hpc",
                control_job_id="794039",
                timezone={
                    "command": "date +%z",
                    "returncode": 0,
                    "stdout": "+0800\n",
                    "stderr": "",
                },
                account_query={
                    "command": "id -un",
                    "returncode": 0,
                    "stdout": "testuser\n",
                    "stderr": "",
                },
                remote_query={
                    "command": rejection.remote_probe_command(
                        self.original_job["remote"]
                    ),
                    "returncode": 0,
                    "stdout": json.dumps(audit["remote"]),
                    "stderr": "",
                },
            )
            for name, command in rejection.scheduler_commands(audit).items():
                audit[name].update(command=command, stderr="")
        return blocked

    def prepare_rejection(self):
        self.initialize()
        with self.assertRaises(HarnessError):
            self.run_one()

    def certify(self, audits=None):
        return rejection.certify(
            self.root,
            self.backend,
            "0002",
            audits=self.audits if audits is None else audits,
            authorization="Explicit operator certification for this rejected submission",
        )

    def test_certified_rejection_preserves_raw_attempt_and_reaches_review(self):
        self.prepare_rejection()
        before = self.study.status()
        old = self.study.root / before["active_attempt"]
        original = (old / "agent/response.json").read_bytes()
        plan = (self.study.root / before["plan_ref"]).read_bytes()
        ledger = RUNNER.read(self.control / "shared-gpu-budget.json")
        receipt = self.certify()
        self.assertEqual(receipt["gpu_seconds"], 0)
        self.assertNotIn("terminal_state", receipt)
        self.assertNotIn("owned_job_id", receipt)
        self.assertEqual(
            RUNNER.read(self.control / "research-jobs/0002/state.json"),
            self.original_state,
        )
        self.assertEqual(self.budget.status()["reserved_gpu_seconds"], 11100)
        self.assertFalse(self.budget.status()["unresolved_submission"])
        after_ledger = RUNNER.read(self.control / "shared-gpu-budget.json")
        self.assertEqual(
            ledger["attempts"]["manual-existing"],
            after_ledger["attempts"]["manual-existing"],
        )
        with self.assertRaises(ValueError):
            self.budget.reserve(self.key, "research", 1200)
        prepared = RUNNER.reconcile_research_execute(
            self.root, self.backend, authorization="Continue original incomplete Review"
        )
        self.assertEqual(prepared["call_count"], before["call_count"])
        self.assertEqual(prepared["elapsed_seconds"], before["elapsed_seconds"])
        self.assertEqual((old / "agent/response.json").read_bytes(), original)
        self.assertEqual((self.study.root / before["plan_ref"]).read_bytes(), plan)
        state = self.run_one()
        self.assertEqual((state["phase"], state["status"]), ("done", "ready"))
        response = completed_response(self.study.root / state["calls"]["execute"])
        self.assertEqual(response["outcome"], "incomplete")
        self.assertEqual(response["measurements"], [])
        self.assertEqual(self.allocations, 1)

    def test_ambiguous_audits_remain_blocked_without_releasing_reservation(self):
        self.prepare_rejection()
        for mutation in (
            "failed-query",
            "unknown-job",
            "job-id",
            "output",
            "truncated-rejection",
            "short-audit",
            "wrong-query",
        ):
            with self.subTest(mutation=mutation):
                audits = copy.deepcopy(self.audits)
                if mutation == "failed-query":
                    audits[0]["sacct"]["returncode"] = 1
                elif mutation == "unknown-job":
                    audits[1]["squeue"]["stdout"] = audits[1]["squeue"][
                        "stdout"
                    ].replace("794039", "999999")
                elif mutation == "job-id":
                    audits[0]["remote"]["job_id_exists"] = True
                elif mutation == "output":
                    audits[1]["remote"]["output_names"] = ["events.jsonl"]
                elif mutation == "short-audit":
                    audits[1]["observed_at"] = audits[0]["observed_at"]
                elif mutation == "wrong-query":
                    audits[0]["sacct"]["command"] = audits[0]["control"]["command"]
                else:
                    # A transport error lacking the positive rejection is never certified.
                    path = (
                        self.control
                        / "research-jobs/0002/command-diagnostics/diagnostic-manifest.json"
                    )
                    original = path.read_bytes()
                    value = RUNNER.read(path)
                    value["logs"][1]["tail"] = "transport lost"
                    RUNNER.write(path, value)
                with self.assertRaises(RUNNER.ProTokError):
                    self.certify(audits)
                if mutation == "truncated-rejection":
                    path.write_bytes(original)
                self.assertTrue(self.budget.status()["unresolved_submission"])
                self.assertFalse(
                    (self.control / "research-jobs/0002/receipt.json").exists()
                )

    def test_changed_source_and_user_stop_refuse_certification(self):
        self.prepare_rejection()
        stop = self.control / "STOP"
        stop.write_text("User STOP\n")
        with self.assertRaises(RUNNER.ProTokError):
            self.certify()
        stop.unlink()
        workspace = self.study.root / "iterations/0002/workspace"
        (workspace / "research/main.py").write_text("# changed after submission\n")
        with self.assertRaises(RUNNER.ProTokError):
            self.certify()
        self.assertTrue(self.budget.status()["unresolved_submission"])

    def test_receipt_and_result_cannot_contradict_certificate(self):
        self.prepare_rejection()
        self.certify()
        for p in (
            self.control / "research-jobs/0002/receipt.json",
            self.control / "research-sources/0002/result.json",
        ):
            value = RUNNER.read(p)
            RUNNER.write(p, {**value, "status": "completed", "gpu_seconds": 17})
        with self.assertRaises(RUNNER.ProTokError):
            self.budget.status()

    def test_changed_blocked_identity_and_protected_source_fail_before_release(self):
        self.prepare_rejection()
        blocked = self.control / "research-jobs/0002/blocked.json"
        original = blocked.read_bytes()
        value = RUNNER.read(blocked)
        RUNNER.write(blocked, {**value, "execution_identity": "other"})
        with self.assertRaises(RUNNER.ProTokError):
            self.certify()
        self.assertTrue(self.budget.status()["unresolved_submission"])
        blocked.write_bytes(original)
        workspace = self.study.root / "iterations/0002/workspace"
        (workspace / "unauthorized.py").write_text("# outside admitted scope\n")
        with self.assertRaises(RUNNER.ProTokError):
            self.certify()
        self.assertTrue(self.budget.status()["unresolved_submission"])

    def test_existing_handoff_is_retained_and_must_match_explicit_expectation(self):
        self.prepare_rejection()
        marker = self.control / "HANDOFF"
        expected = "Upgrade to " + "a" * 40 + "\n"
        marker.write_text(expected)
        with self.assertRaises(RUNNER.ProTokError):
            self.certify()
        with self.assertRaises(RUNNER.ProTokError):
            rejection.certify(
                self.root,
                self.backend,
                "0002",
                audits=self.audits,
                authorization="Explicit recovery",
                expected_handoff="Upgrade to " + "b" * 40 + "\n",
            )
        self.assertTrue(self.budget.status()["unresolved_submission"])
        result = rejection.certify(
            self.root,
            self.backend,
            "0002",
            audits=self.audits,
            authorization="Explicit recovery",
            expected_handoff=expected,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(marker.read_text(), expected)
