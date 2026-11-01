import hashlib
import fcntl
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from integrations.protok import debug_backend as debug
from research_loop.debug_budget import DebugBudget


class Scheduler:
    def __init__(self):
        self.jobs = {}
        self.commands = []
        self.elapsed = 600
        self.terminal = "FAILED"
        self.unknown = False
        self.missing_accounting = False
        self.stop_on_poll = None
        self.control = None
        self.failure_log = (
            "Traceback (most recent call last):\n"
            '  File "/remote/code/scripts/train_autoresearch_se3.py", line 428, in step\n'
            "RuntimeError: Missing first-step active gradients: {'readout.weight'}\n"
        )

    def ssh(self, config, command, **kwargs):
        self.commands.append(command)
        if command == "sbatch":
            job = str(100 + len(self.jobs))
            self.jobs[job] = {"state": self.terminal, "elapsed": self.elapsed}
            if self.unknown:
                raise RuntimeError(
                    "Submission transport lost; job-ID receipt unavailable"
                )
            return job
        if command == "recover-job":
            if self.unknown:
                raise RuntimeError("Job-ID receipt still unavailable")
            return str(99 + len(self.jobs))
        if command.startswith("squeue"):
            job = command.split()[3]
            if self.stop_on_poll:
                self.stop_on_poll()
                self.stop_on_poll = None
                return "RUNNING"
            return "" if self.jobs[job]["state"] != "RUNNING" else "RUNNING"
        if command.startswith("scancel"):
            self.jobs[command.split()[1]]["state"] = "CANCELLED"
            return ""
        if command.startswith("sacct"):
            if self.missing_accounting:
                raise RuntimeError("Missing accounting")
            return self.jobs[command.split()[1]]
        raise AssertionError(command)


class FakeNative:
    """Protected-backend interface with a fake Slurm transport, no GPU/data access."""

    def __init__(self, scheduler):
        self.ssh = scheduler.ssh
        self.scheduler = scheduler

    @staticmethod
    def load_config(path):
        return debug.read(path)

    @staticmethod
    def config_identity(config):
        return "scientific-fixed"

    @staticmethod
    def source_boundary(config):
        return {
            "candidate.py": (Path(config["workspace"]) / "candidate.py").read_bytes()
        }

    @staticmethod
    def fingerprint(config, candidates):
        return hashlib.sha256(candidates["candidate.py"]).hexdigest()

    @staticmethod
    def batch_script(config, remote, seed, *, smoke_only, include_smoke):
        assert smoke_only and not include_smoke
        return (
            "#!/bin/bash\n#SBATCH --time=00:30:00\n"
            "python scripts/train_autoresearch_se3.py --smoke --no-compile --check-compiled\n"
            f"python write-wall.py 1800 {remote}/wall-time.json\n"
        )

    def prepare(self, config, candidates, record, attempt):
        local = record / attempt["directory"]
        local.mkdir()
        (local / "smoke.sbatch").write_text(
            self.batch_script(config, attempt["remote"], 42, smoke_only=True)
        )

    @staticmethod
    def save_state(record, state):
        debug.write(record / "state.json", state)

    def submit(self, config, record, state, attempt):
        attempt.update(status="submitting", submitted_at=time.time())
        self.save_state(record, state)
        attempt.update(status="running", owned_job_id=self.ssh(config, "sbatch"))
        self.save_state(record, state)

    def wait_for_job(self, config, session, record, state, attempt):
        if attempt["status"] == "submitting":
            attempt.update(
                status="running", owned_job_id=self.ssh(config, "recover-job")
            )
            self.save_state(record, state)
        job = attempt["owned_job_id"]
        while self.ssh(config, f"squeue -h -j {job} -o %T"):
            if time.time() >= session["deadline"]:
                self.ssh(config, "scancel " + job)
        accounting = self.ssh(config, "sacct " + job)
        completed = accounting["state"] == "COMPLETED"
        attempt.update(
            status="finished" if completed else "failed",
            terminal_state=accounting["state"],
            gpu_seconds=accounting["elapsed"],
            exit_code="0:0" if completed else "1:0",
        )
        self.save_state(record, state)
        if not completed:
            raise RuntimeError("Protected native smoke allocation failed")

    def cancel_interrupted(self, config, record, state):
        for attempt in state["attempts"]:
            if attempt.get("owned_job_id"):
                self.ssh(config, "scancel " + attempt["owned_job_id"])

    @staticmethod
    def collect(config, record, attempt):
        pass

    def collect_logs(self, config, record, attempt):
        (record / attempt["directory"] / "run.log.tail").write_text(
            self.scheduler.failure_log
        )

    @staticmethod
    def summarize_seed(config, record, attempt):
        return {
            "quality_score_eligible": False,
            "mode": "smoke",
            "completion": {"optimizer_step": 3},
            "gpu_seconds": attempt["gpu_seconds"],
        }


class DebugBackendTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.workspace = self.root / "frozen"
        self.control = self.root / "control"
        self.formal = self.root / "formal-records"
        for folder in (self.project, self.workspace, self.control, self.formal):
            folder.mkdir()
        (self.workspace / "candidate.py").write_text("# frozen candidate\n")
        self.config = {
            "workspace": str(self.workspace),
            "records_root": str(self.formal),
            "remote_root": "/remote/formal",
            "compile": True,
            "time_limit": "00:30:00",
        }
        self.config_path = self.control / "runtime.json"
        debug.write(self.config_path, self.config)
        self.deadline = time.time() + 7200
        self.owner = {
            "owner_token": "owner",
            "handoff_acknowledged": True,
            "expires_at": self.deadline,
        }
        self.lease = {
            "owner_token": "owner",
            "deadline": self.deadline,
            "config_identity": "scientific-fixed",
        }
        debug.write(self.control / "ownership.json", self.owner)
        debug.write(self.control / "continuation.json", self.lease)
        debug.write(
            self.control / "debug-policy.json",
            {
                "schema": 1,
                "gpu_seconds_per_investigation": 1800,
                "independent_of_formal_budget": True,
                "authorization": "User confirmed per failed investigation",
            },
        )
        (self.formal / "session.json").write_text(
            '{"gpu_seconds":86400,"deadline":1}\n'
        )
        (self.project / "autoresearch.jsonl").write_text("original ledger\n")
        self.formal_bytes = (self.formal / "session.json").read_bytes()
        self.ledger_bytes = (self.project / "autoresearch.jsonl").read_bytes()
        self.debug_root = self.root / "debug-iteration-0002"
        self.scheduler = Scheduler()
        validate = patch.object(
            debug,
            "validate_continuation",
            side_effect=lambda *a, **k: (
                FakeNative(self.scheduler),
                dict(self.config),
                self.lease | {"effective_deadline": self.deadline},
            ),
        )
        load = patch.object(
            debug,
            "_load_backend",
            side_effect=lambda *a: (FakeNative(self.scheduler), dict(self.config)),
        )
        validate.start()
        load.start()
        self.addCleanup(validate.stop)
        self.addCleanup(load.stop)

    def run_attempt(self, name="debug-1", *, recover=False):
        return debug.run_debug(
            self.project,
            self.control,
            "owner",
            self.config_path,
            self.debug_root,
            name,
            self.deadline,
            recover=recover,
        )

    def assert_formal_unchanged(self):
        self.assertEqual((self.formal / "session.json").read_bytes(), self.formal_bytes)
        self.assertEqual(
            (self.project / "autoresearch.jsonl").read_bytes(), self.ledger_bytes
        )
        self.assertEqual(list(self.formal.iterdir()), [self.formal / "session.json"])
        self.assertEqual(debug.read(self.config_path), self.config)

    def test_multiple_failed_smokes_charge_cumulatively_and_cap_each_sbatch_and_receipt(
        self,
    ):
        for index, expected in enumerate((1800, 1200, 600), 1):
            result = self.run_attempt(f"debug-{index}")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["allocated_gpu_seconds"], expected)
            self.assertEqual(result["budget"]["used_gpu_seconds"], 600 * index)
            attempt = (
                self.debug_root / "attempts" / f"debug-{index}" / "seed-42-attempt-1"
            )
            script = (attempt / "smoke.sbatch").read_text()
            self.assertIn(f"#SBATCH --time=00:{expected // 60:02d}:00", script)
            self.assertIn(f" {expected} /remote/formal/research-loop-debug/", script)
            self.assertIn("--smoke --no-compile --check-compiled", script)
            self.assertNotIn("METRIC", script)
            self.assertFalse(result["quality_score_eligible"])
        with self.assertRaisesRegex(ValueError, "No whole Slurm minute"):
            self.run_attempt("debug-4")
        self.assertEqual(self.scheduler.commands.count("sbatch"), 3)
        self.assert_formal_unchanged()

    def test_success_is_unscored_and_settled_receipt_is_idempotent(self):
        self.scheduler.terminal = "COMPLETED"
        result = self.run_attempt()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["verified"])
        self.assertFalse(result["quality_score_eligible"])
        self.assertEqual(result["purpose"], "debug")
        self.assertEqual(result, self.run_attempt(recover=True))
        self.assertEqual(self.scheduler.commands.count("sbatch"), 1)
        self.assert_formal_unchanged()

    def test_producer_certifies_accounted_first_step_failure_for_debug_flow(self):
        result = self.run_attempt()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["purpose"], "debug")
        self.assertIs(result["quality_score_eligible"], False)
        self.assertEqual(result["failure_kind"], "candidate")
        self.assertEqual(
            result["cause"],
            "RuntimeError: Missing first-step active gradients: {'readout.weight'}",
        )
        self.assertIn(result["cause"], result["error"])
        self.assertEqual(result["failure_detail"], self.scheduler.failure_log)
        self.assertEqual(result["budget"]["used_gpu_seconds"], 600)
        self.assertIsNone(result["budget"]["active_attempt"])
        # These exact producer fields are debug_flow's prerequisite for retry.
        self.assertTrue(result["failure_kind"] == "candidate" and result["cause"])

    def test_same_failed_accounting_with_infrastructure_or_unknown_log_is_not_candidate(
        self,
    ):
        for index, text in enumerate(
            (
                "ConnectionRefusedError: SSH transport failed\n",
                "slurmstepd: error: compute node stopped without a Python exception\n",
            ),
            1,
        ):
            with self.subTest(text=text):
                self.scheduler.failure_log = text
                result = self.run_attempt(f"debug-{index}")
                self.assertEqual(result["terminal_state"], "FAILED")
                self.assertEqual(result["status"], "failed")
                self.assertNotEqual(result["failure_kind"], "candidate")
                self.assertTrue(result["cause"])

    def test_failure_detail_is_bounded_and_uses_final_exception_not_an_earlier_candidate(
        self,
    ):
        self.scheduler.failure_log = (
            "x" * 40000 + "\n"
            "Traceback (most recent call last):\n"
            '  File "/remote/protok/model/autoresearch_se3.py", line 10, in forward\n'
            "ValueError: handled candidate diagnostic\n"
            "Traceback (most recent call last):\n"
            '  File "/usr/lib/transport.py", line 20, in connect\n'
            "ConnectionRefusedError: node unavailable\n"
        )
        result = self.run_attempt()
        self.assertLessEqual(len(result["failure_detail"].encode("utf-8")), 32768)
        self.assertEqual(result["cause"], "ConnectionRefusedError: node unavailable")
        self.assertEqual(result["failure_kind"], "infrastructure")
        self.assertIn(result["cause"], result["error"])

    def test_editable_traceback_requires_accounting_and_timeout_is_not_candidate(self):
        text = (
            "Traceback (most recent call last):\n"
            '  File "/remote/protok/model/autoresearch_se3_loss.py", line 12, in loss\n'
            "ValueError: invalid loss shape\n"
        )
        self.assertEqual(debug.candidate_cause(text), "ValueError: invalid loss shape")
        self.assertEqual(
            debug.candidate_cause(
                text.replace("ValueError: invalid loss shape", "AssertionError")
            ),
            "AssertionError",
        )
        self.scheduler.failure_log = text
        self.scheduler.terminal = "TIMEOUT"
        self.scheduler.elapsed = 1800
        result = self.run_attempt()
        self.assertEqual(result["failure_kind"], "infrastructure")
        self.assertEqual(result["budget"]["remaining_gpu_seconds"], 0)

    def test_residual_is_rounded_down_to_a_hard_slurm_minute_and_all_receipts_match(
        self,
    ):
        self.scheduler.elapsed = 610
        self.run_attempt("debug-1")
        result = self.run_attempt("debug-2")
        self.assertEqual(result["allocated_gpu_seconds"], 1140)
        record = self.debug_root / "attempts/debug-2"
        self.assertEqual(
            debug.read(record / "state.json")["attempts"][0]["budget_seconds"], 1140
        )
        script = (record / "seed-42-attempt-1/smoke.sbatch").read_text()
        self.assertIn("#SBATCH --time=00:19:00", script)
        self.assertIn(" 1140 /remote/formal/research-loop-debug/", script)
        self.assertEqual(result["budget"]["used_gpu_seconds"], 1220)
        self.assert_formal_unchanged()

    def test_formal_allocation_lock_blocks_debug_submission(self):
        with (self.control / "protok.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.run_attempt()
        self.assertNotIn("sbatch", self.scheduler.commands)
        self.assert_formal_unchanged()

    def test_actual_accounting_over_cap_is_retained_and_blocks_further_allocations(
        self,
    ):
        self.scheduler.terminal = "COMPLETED"
        self.scheduler.elapsed = 1801
        result = self.run_attempt()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["verified"])
        self.assertEqual(result["budget"]["used_gpu_seconds"], 1801)
        self.assertTrue(result["budget"]["breached"])
        with self.assertRaisesRegex(ValueError, "No whole Slurm minute"):
            self.run_attempt("debug-2")
        self.assertEqual(self.scheduler.commands.count("sbatch"), 1)
        self.assert_formal_unchanged()

    def test_unknown_submission_blocks_new_attempt_and_recovers_without_resubmission(
        self,
    ):
        self.scheduler.unknown = True
        self.assertEqual(self.run_attempt()["status"], "blocked")
        with self.assertRaisesRegex(ValueError, "Outstanding debug"):
            self.run_attempt("debug-2")
        self.scheduler.unknown = False
        recovered = self.run_attempt(recover=True)
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["budget"]["used_gpu_seconds"], 600)
        self.assertEqual(self.scheduler.commands.count("sbatch"), 1)
        self.assert_formal_unchanged()

    def test_missing_accounting_stays_blocked_then_same_attempt_settles(self):
        self.scheduler.missing_accounting = True
        self.assertEqual(self.run_attempt()["status"], "blocked")
        self.assertEqual(
            DebugBudget(
                self.debug_root,
                scope_id=self.debug_root.name,
                limit_gpu_seconds=1800,
            ).remaining(),
            0,
        )
        self.scheduler.missing_accounting = False
        (self.control / "STOP").touch()
        with patch.object(debug.time, "time", return_value=self.deadline + 1):
            self.assertEqual(
                self.run_attempt(recover=True)["budget"]["used_gpu_seconds"], 600
            )
        self.assertEqual(self.scheduler.commands.count("sbatch"), 1)

    def test_stop_cancels_only_owned_job_and_reconciles_actual_spend(self):
        self.scheduler.terminal = "RUNNING"
        self.scheduler.stop_on_poll = lambda: (self.control / "STOP").touch()
        result = self.run_attempt()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["terminal_state"], "CANCELLED")
        self.assertEqual(result["budget"]["used_gpu_seconds"], 600)
        self.assertEqual(
            [c for c in self.scheduler.commands if c.startswith("scancel")],
            ["scancel 100"],
        )
        self.assertTrue((self.control / "STOP").exists())
        self.assert_formal_unchanged()

    def test_changed_candidate_or_extended_deadline_cannot_recover(self):
        self.scheduler.missing_accounting = True
        self.run_attempt()
        (self.workspace / "candidate.py").write_text("# changed\n")
        with self.assertRaisesRegex(ValueError, "candidate changed"):
            self.run_attempt(recover=True)
        (self.workspace / "candidate.py").write_text("# frozen candidate\n")
        with self.assertRaisesRegex(ValueError, "frozen contract"):
            debug.run_debug(
                self.project,
                self.control,
                "owner",
                self.config_path,
                self.debug_root,
                "debug-1",
                self.deadline + 1,
                recover=True,
            )
        self.assertEqual(self.scheduler.commands.count("sbatch"), 1)


if __name__ == "__main__":
    unittest.main()
