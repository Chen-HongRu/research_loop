"""Synthetic native script transport tests; no scheduler, data or model calls."""

import ast
import base64
from copy import deepcopy
import json
from pathlib import Path
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from integrations.script_runtime import backend


class Transport:
    def __init__(self):
        self.calls = []
        self.ack_lost = False
        self.rows = ""
        self.outputs = []

    def run(self, command, *, stdin=None, timeout=120):
        self.calls.append((command, stdin, timeout))
        if command.startswith("sbatch "):
            if self.ack_lost:
                raise TimeoutError("synthetic lost acknowledgement")
            return "123;synthetic-cluster\n"
        if command.startswith("sacct "):
            return self.rows
        if command.startswith("scancel "):
            return ""
        args = shlex.split(command)
        source = args[2]
        if "checks=" in source:
            tree = ast.parse(source)
            return json.dumps(ast.literal_eval(tree.body[1].value))
        if "result=[];total=0" in source:
            return json.dumps(self.outputs)
        return ""


class NativeScriptBackendTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        source = self.root / "baseline"
        (source / "research").mkdir(parents=True)
        (source / "research/base.py").write_text("print('baseline')\n")
        (source / "protected.py").write_text("FIXED = True\n")
        gate = self.root / "clearance.json"
        gate.write_text(json.dumps({"shared_exposure_reconciled": True}))
        self.config = {
            "journal_root": str(self.root / "journal"),
            "study_root": str(self.root / "study"),
            "baseline_root": str(source),
            "baseline_ref": "synthetic-revision",
            "source_paths": ["research/base.py", "protected.py"],
            "protected_paths": ["protected.py"],
            "edit_roots": ["research"],
            "runtime_files": {},
            "shared_exposure_gate": {
                "path": str(gate),
                "identity": backend._identity(gate),
            },
            "resource_pool": "gpu",
            "cost_unit": "gpu_seconds",
            "ssh_target": "example.invalid",
            "slurm_user": "synthetic-user",
            "slurm_timezone": "UTC",
            "remote_root": "/synthetic/jobs",
            "partition": "synthetic-debug",
            "account": "synthetic-account",
            "cpus": 4,
            "memory": "48G",
            "remote_python": "/synthetic/bin/python",
            "launcher_source": "# Operator provides the verified sandbox launcher.\nassert RUN['execution']['gpu']\n",
            "input_mappings": {
                "cache": {
                    "path": "/synthetic/cache",
                    "mount": "/inputs/cache",
                    "kind": "directory",
                }
            },
            "metadata_checks": [],
            "report_suffixes": [".json", ".csv"],
            "max_report_bytes": 262144,
            "max_collection_bytes": 1048576,
            "command_timeout_seconds": 30,
            "cleanup_timeout_seconds": 30,
            "staging_headroom_seconds": 300,
        }
        self.request = {
            "id": "script",
            "backend": "script",
            "authorization_id": "grant",
            "resources": {"gpu": 1},
            "cost_bounds": {"gpu_seconds": 120},
            "timeout_seconds": 600,
            "input_artifacts": [],
            "parameters": {
                "baseline_ref": "synthetic-revision",
                "source_files": [
                    {"path": "research/run.py", "content": "print('candidate')\n"}
                ],
                "execution": {
                    "mode": "script",
                    "entrypoint": "research/run.py",
                    "gpu": True,
                    "time_limit_seconds": 61,
                    "inputs": [{"name": "cache", "source": "cache"}],
                    "outputs": ["report.json", "latest.pt"],
                },
            },
        }
        self.op = {
            "id": "native-operation",
            "logical_id": "script",
            "plan_attempt_id": "plan",
            "execute_attempt_id": "execute",
            "backend": "script",
            "backend_version": "1",
            "environment": {"id": "synthetic", "version": "1"},
            "deadline": time.time() + 3600,
            "submission_token": None,
            "submit_owner_epoch": None,
            "job_id": None,
            "state": "pending",
            "physical_released": False,
            "inconsistency": None,
        }
        self.transport = Transport()

    def invoke(self, action):
        return backend.invoke(
            action, self.op, self.request, self.config, transport=self.transport
        )

    def prepare_submit(self, *, lost=False):
        self.invoke("prepare")
        self.op.update(submission_token="one-use-token", submit_owner_epoch=1)
        self.transport.ack_lost = lost
        return self.invoke("submission")

    def journal(self):
        return next((self.root / "journal").glob("*/journal.json"))

    def row(
        self,
        *,
        job_id="123",
        name=None,
        comment=None,
        state="COMPLETED",
        elapsed="100",
        tres="gres/gpu=1",
        end="2026-01-01T12:00:00",
    ):
        record = json.loads(self.journal().read_text())
        return (
            "|".join(
                [
                    job_id,
                    record["job_name"] if name is None else name,
                    record["comment"] if comment is None else comment,
                    "synthetic-user",
                    "synthetic-account",
                    state,
                    "0:0",
                    elapsed,
                    tres,
                    end,
                ]
            )
            + "|\n"
        )

    def mark_terminal(self):
        self.op.update(
            job_id="123",
            state="terminal",
            physical_released=True,
            terminal_outcome="succeeded",
        )

    def test_prepare_freezes_independent_source_and_checks_metadata_only(self):
        result = self.invoke("prepare")
        self.assertTrue(result["data"]["ready"])
        record = json.loads(self.journal().read_text())
        frozen = self.journal().parent / "source"
        self.assertEqual((frozen / "protected.py").read_text(), "FIXED = True\n")
        self.assertEqual(
            (Path(self.config["baseline_root"]) / "research/base.py").read_text(),
            "print('baseline')\n",
        )
        self.assertIsNone(record["token"])
        self.assertEqual(len(self.transport.calls), 1)
        self.assertNotIn("read_bytes", self.transport.calls[0][0])
        self.assertNotIn("sbatch", self.transport.calls[0][0])

    def test_rejects_scope_protected_paths_bad_compile_and_understated_cost(self):
        for edit in [
            {"path": "../escape.py", "content": "pass"},
            {"path": "protected.py", "content": "pass"},
            {"path": "unapproved/file.py", "content": "pass"},
            {"path": "research/run.py", "content": "def broken("},
        ]:
            with self.subTest(edit=edit):
                request = deepcopy(self.request)
                request["parameters"]["source_files"] = [edit]
                with self.assertRaises((ValueError, SyntaxError)):
                    backend.invoke(
                        "prepare",
                        self.op,
                        request,
                        self.config,
                        transport=self.transport,
                    )
        self.request["cost_bounds"] = {"gpu_seconds": 61}
        with self.assertRaisesRegex(ValueError, "rounded"):
            self.invoke("prepare")
        self.assertEqual(self.transport.calls, [])

    def test_gate_is_required_before_prepare_and_submission(self):
        path = Path(self.config["shared_exposure_gate"]["path"])
        self.invoke("prepare")
        path.write_text(json.dumps({"shared_exposure_reconciled": False}))
        self.op.update(submission_token="one-use-token", submit_owner_epoch=1)
        with self.assertRaisesRegex(ValueError, "reconciled"):
            self.invoke("submission")
        self.assertFalse(
            any(call[0].startswith("sbatch") for call in self.transport.calls)
        )

    def test_minimum_timeout_uses_real_core_acceptance_deadline_after_staging(self):
        # Reuse the real SQLite admission fixture, not a hand-constructed deadline.
        from tests.test_runtime_operations import (
            RuntimeOperationsTests,
            configuration as core_configuration,
        )

        config = core_configuration()
        config.update(
            execution_backends={
                "script": {"version": "1", "environment": self.op["environment"]}
            },
            resource_pools={"gpu": 1},
            operation_authorizations={
                "grant": {
                    "backends": ["script"],
                    "max_operations": 1,
                    "resource_limits": {"gpu": 1},
                    "cost_limits": {"gpu_seconds": 86400},
                    "deadline": None,
                }
            },
        )
        fixture = RuntimeOperationsTests()
        with patch("tests.test_runtime_operations.configuration", return_value=config):
            fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.request["timeout_seconds"] = 120 + self.config["staging_headroom_seconds"]
        fixture.owner.create_task(
            "script-task", {"instruction": "Synthetic bounded script"}
        )
        iteration = fixture.owner.claim_task("script-task")
        fixture.accept(
            "plan",
            {
                "timeout_seconds": self.request["timeout_seconds"],
                "local_evaluation": None,
            },
            iteration,
        )
        accepted_at = fixture.now
        _, receipt = fixture.accept(
            "execute",
            {"artifacts": [], "operations": [self.request]},
            fixture.iteration(iteration["id"]),
        )
        identity = receipt.to_dict()["operation_ids"][0]
        self.op = fixture.ops.get(identity)
        self.assertEqual(
            self.op["deadline"], accepted_at + self.request["timeout_seconds"]
        )
        with patch.object(backend.time, "time", side_effect=lambda: fixture.now):
            fixture.now += 20
            prepared = self.invoke("prepare")
            fixture.event(identity, "prepare", prepared["data"])
            fixture.ops.reserve_submit(identity, fixture.ops.get(identity)["revision"])
            self.op = fixture.ops.get(identity)
            original_run = self.transport.run

            def run(command, **kwargs):
                if not command.startswith("sbatch "):
                    fixture.now += 40
                return original_run(command, **kwargs)

            self.transport.run = run
            result = self.invoke("submission")
        self.assertEqual(result["data"]["outcome"], "accepted")
        self.assertEqual(self.request["cost_bounds"], {"gpu_seconds": 120})
        self.assertEqual(
            sum(call[0].startswith("sbatch ") for call in self.transport.calls), 1
        )

    def test_staging_cannot_consume_remaining_allocation_safety_window(self):
        now = [1000.0]
        self.op["deadline"] = now[0] + self.request["timeout_seconds"]
        with patch.object(backend.time, "time", side_effect=lambda: now[0]):
            self.invoke("prepare")
            self.op.update(submission_token="one-use-token", submit_owner_epoch=1)
            original_run = self.transport.run

            def run(command, **kwargs):
                now[0] = self.op["deadline"] - 120 - backend.SUBMISSION_MARGIN_SECONDS
                return original_run(command, **kwargs)

            self.transport.run = run
            result = self.invoke("submission")
        self.assertEqual(result["data"]["outcome"], "unknown")
        self.assertFalse(
            any(call[0].startswith("sbatch ") for call in self.transport.calls)
        )

    def test_synthetic_lost_ack_never_replays_and_recovers_readonly(self):
        result = self.prepare_submit(lost=True)
        self.assertEqual(result["data"]["outcome"], "unknown")
        record = json.loads(self.journal().read_text())
        self.assertEqual(record["status"], "submission_intent")
        self.assertEqual(record["token"], "one-use-token")
        self.assertEqual(self.invoke("submission")["data"]["outcome"], "unknown")
        self.transport.rows = self.row(state="RUNNING", end="Unknown")
        inspection = self.invoke("inspection")
        self.assertEqual(inspection["data"]["job_id"], "123")
        self.assertFalse(inspection["data"]["terminal"])
        self.assertEqual(
            sum(call[0].startswith("sbatch ") for call in self.transport.calls), 1
        )
        self.assertTrue(self.transport.calls[-1][0].startswith("sacct "))

    def test_journal_is_durable_before_first_effect_and_binds_original_owner(self):
        self.invoke("prepare")
        self.op.update(submission_token="one-use-token", submit_owner_epoch=1)
        real_run = self.transport.run

        def run(command, **kwargs):
            record = json.loads(self.journal().read_text())
            self.assertEqual(record["token"], "one-use-token")
            self.assertIn(record["status"], {"effect_intent", "submission_intent"})
            self.assertEqual(record["binding"]["plan_attempt_id"], "plan")
            return real_run(command, **kwargs)

        self.transport.run = run
        self.invoke("submission")
        self.transport.run = real_run
        self.op["submit_owner_epoch"] = 2
        with self.assertRaisesRegex(ValueError, "owner"):
            self.invoke("inspection")

    def test_replay_returns_original_job_without_submission_or_inspection(self):
        result = self.prepare_submit()
        calls = len(self.transport.calls)
        replay = self.invoke("submission")
        self.assertEqual(result["data"]["job_id"], replay["data"]["job_id"])
        self.assertEqual(len(self.transport.calls), calls)
        self.assertEqual(
            len(
                [call for call in self.transport.calls if call[0].startswith("sbatch ")]
            ),
            1,
        )

    def test_missing_accounting_is_unknown_and_not_final_zero(self):
        self.prepare_submit()
        result = self.invoke("inspection")
        self.assertFalse(result["data"]["terminal"])
        self.assertFalse(result["data"]["physical_released"])
        self.mark_terminal()
        self.assertEqual(self.invoke("settlement")["data"], {"final_costs": {}})

    def test_partial_identity_retains_original_job_and_never_authorizes_effects(self):
        self.prepare_submit()
        self.transport.rows = self.row(comment="", state="RUNNING", end="Unknown")
        inspection = self.invoke("inspection")["data"]
        self.assertEqual(inspection["job_id"], "123")
        self.assertFalse(inspection["terminal"])
        self.assertFalse(inspection["physical_released"])
        self.assertFalse(self.invoke("cancellation")["data"]["acknowledged"])
        self.assertEqual(self.invoke("settlement")["data"], {"final_costs": {}})
        self.assertEqual(self.invoke("submission")["data"]["job_id"], "123")
        self.assertEqual(
            sum(call[0].startswith("sbatch ") for call in self.transport.calls), 1
        )
        self.assertFalse(
            any(call[0].startswith("scancel ") for call in self.transport.calls)
        )
        self.assertEqual(json.loads(self.journal().read_text())["job_id"], "123")

    def test_missing_identity_cannot_confirm_terminal_or_hide_contradiction(self):
        self.prepare_submit()
        original = self.row().strip().split("|")
        for index in range(1, 5):
            with self.subTest(missing=index):
                row = original.copy()
                row[index] = ""
                self.transport.rows = "|".join(row) + "\n"
                data = self.invoke("inspection")["data"]
                self.assertFalse(data["terminal"])
                self.assertFalse(data["physical_released"])
        for index in [0, 1, 3, 4]:
            with self.subTest(contradiction=index):
                row = original.copy()
                row[2] = ""
                row[index] = "999" if index == 0 else "contradiction"
                self.transport.rows = "|".join(row) + "\n"
                with self.assertRaisesRegex(ValueError, "identity"):
                    self.invoke("inspection")

    def test_duplicate_partial_identity_rows_still_contradict_one_submission(self):
        self.prepare_submit()
        self.transport.rows = self.row(comment="") * 2
        with self.assertRaisesRegex(ValueError, "Multiple scheduler jobs"):
            self.invoke("inspection")

    def test_contradictory_identity_never_cancels_or_settles(self):
        self.prepare_submit()
        for action in ["inspection", "cancellation", "settlement"]:
            self.transport.rows = self.row(comment="different-token")
            with self.assertRaisesRegex(ValueError, "identity"):
                self.invoke(action)
        self.assertFalse(
            any(call[0].startswith("scancel ") for call in self.transport.calls)
        )

    def test_cancellation_intersects_job_name_user_account_and_id(self):
        self.prepare_submit()
        self.transport.rows = self.row(state="RUNNING", end="Unknown")
        self.assertTrue(self.invoke("cancellation")["data"]["acknowledged"])
        command = self.transport.calls[-1][0]
        self.assertTrue(command.startswith("scancel "))
        self.assertIn("--name=native-", command)
        self.assertIn("--user=synthetic-user", command)
        self.assertIn("--account=synthetic-account", command)
        self.assertTrue(command.endswith(" 123"))
        self.transport.rows = self.row()
        self.assertFalse(self.invoke("cancellation")["data"]["acknowledged"])
        self.assertTrue(self.transport.calls[-1][0].startswith("sacct "))

    def test_same_terminal_job_release_precedes_final_allocated_fees(self):
        self.prepare_submit()
        self.transport.rows = self.row()
        result = self.invoke("inspection")
        self.assertTrue(result["data"]["physical_released"])
        self.assertEqual(self.invoke("settlement")["data"], {"final_costs": {}})
        self.mark_terminal()
        self.assertEqual(
            self.invoke("settlement")["data"], {"final_costs": {"gpu_seconds": 100}}
        )
        self.transport.rows = self.row(tres="")
        self.assertEqual(self.invoke("settlement")["data"], {"final_costs": {}})

    def test_pending_accounting_then_same_terminal_job_final_fees(self):
        self.prepare_submit()
        self.transport.rows = self.row(state="RUNNING", end="Unknown")
        self.assertEqual(self.invoke("settlement")["data"], {"final_costs": {}})
        self.transport.rows = self.row()
        self.assertEqual(self.invoke("settlement")["data"], {"final_costs": {}})
        self.mark_terminal()
        for missing in [{"end": "Unknown"}, {"elapsed": ""}, {"tres": ""}]:
            with self.subTest(missing=missing):
                self.transport.rows = self.row(**missing)
                self.assertEqual(self.invoke("settlement")["data"], {"final_costs": {}})
        self.transport.rows = self.row(elapsed="43")
        self.assertEqual(
            self.invoke("settlement")["data"], {"final_costs": {"gpu_seconds": 43}}
        )

    def test_accounting_contradictions_remain_errors(self):
        self.prepare_submit()
        self.mark_terminal()
        for values, message in [
            ({"state": "FAILED"}, "outcome"),
            ({"tres": "gres/gpu=2"}, "GPU count"),
            ({"elapsed": "invalid"}, "elapsed"),
        ]:
            with self.subTest(values=values):
                self.transport.rows = self.row(**values)
                with self.assertRaisesRegex(ValueError, message):
                    self.invoke("settlement")

    def test_failed_job_missing_outputs_is_final_unavailable_collection(self):
        self.prepare_submit()
        self.mark_terminal()
        self.op["terminal_outcome"] = "failed"
        self.transport.outputs = [
            {"name": "report.json", "missing": True},
            {"name": "latest.pt", "missing": True},
        ]
        result = self.invoke("collection")["data"]
        self.assertEqual(result["artifacts"], [])
        self.assertEqual(result["research_evidence"], [])
        self.assertEqual(result["evaluations"], [])
        self.assertTrue(result["unavailable_reason"])
        calls = len(self.transport.calls)
        self.assertEqual(self.invoke("collection")["data"], result)
        self.assertEqual(len(self.transport.calls), calls)

    def test_collect_declared_reports_only_and_checkpoint_metadata_no_measurement(self):
        self.prepare_submit()
        self.mark_terminal()
        content = json.dumps({"metric": 42, "verified": True}).encode()
        self.transport.outputs = [
            {
                "name": "report.json",
                "size": len(content),
                "mtime_ns": 100,
                "content": base64.b64encode(content).decode(),
            },
            {"name": "latest.pt", "size": 999999999, "mtime_ns": 101},
        ]
        result = self.invoke("collection")["data"]
        self.assertEqual(result["evaluations"], [])
        self.assertEqual(len(result["research_evidence"]), 2)
        bodies = [
            json.loads((Path(self.config["study_root"]) / e["ref"]).read_text())
            for e in result["research_evidence"]
        ]
        self.assertFalse(bodies[0]["verified_evaluation"])
        self.assertEqual(bodies[0]["text"], content.decode())
        self.assertFalse(bodies[1]["content_collected"])
        self.assertNotIn("text", bodies[1])
        calls = len(self.transport.calls)
        self.assertEqual(self.invoke("collection")["data"], result)
        self.assertEqual(len(self.transport.calls), calls)
        for evidence in result["research_evidence"]:
            self.assertEqual(
                evidence["identity"],
                backend._identity(Path(self.config["study_root"]) / evidence["ref"]),
            )

    def test_collection_rejects_unrequested_or_oversized_payload(self):
        self.prepare_submit()
        self.mark_terminal()
        self.transport.outputs = [{"name": "unrequested.json", "content": "e30="}]
        with self.assertRaisesRegex(ValueError, "exact declared"):
            self.invoke("collection")

    def test_changed_frozen_source_never_submits(self):
        self.invoke("prepare")
        (self.journal().parent / "source/research/run.py").write_text(
            "print('changed')\n"
        )
        self.op.update(submission_token="one-use-token", submit_owner_epoch=1)
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.invoke("submission")
        self.assertFalse(
            any(call[0].startswith("sbatch ") for call in self.transport.calls)
        )

    def test_real_local_python_stage_and_collection_smoke_without_scheduler(self):
        class LocalTransport(Transport):
            def run(inner, command, *, stdin=None, timeout=120):
                if command.startswith(("sbatch ", "sacct ", "scancel ")):
                    return super().run(command, stdin=stdin, timeout=timeout)
                inner.calls.append((command, stdin, timeout))
                return subprocess.run(
                    shlex.split(command),
                    input=stdin,
                    text=True,
                    capture_output=True,
                    check=True,
                    timeout=timeout,
                ).stdout

        self.transport = LocalTransport()
        self.config["remote_python"] = sys.executable
        self.config["remote_root"] = str(self.root / "remote")
        self.request["parameters"]["execution"]["inputs"] = []
        self.config["launcher_source"] = (
            "from pathlib import Path\n"
            "out=Path(RUN['remote'])/'output'\n"
            "(out/'report.json').write_text('{\"synthetic\":true}')\n"
            "(out/'latest.pt').write_bytes(b'synthetic checkpoint; metadata only')\n"
        )
        result = self.prepare_submit()
        self.assertEqual(result["data"]["outcome"], "accepted")
        record = json.loads(self.journal().read_text())
        remote = Path(record["remote"])
        self.assertEqual(
            (remote / "code/research/run.py").read_text(), "print('candidate')\n"
        )
        subprocess.run(
            [sys.executable, str(remote / "launcher.py")], check=True, timeout=5
        )
        self.mark_terminal()
        data = self.invoke("collection")["data"]
        self.assertEqual(data["evaluations"], [])
        bodies = [
            json.loads((Path(self.config["study_root"]) / item["ref"]).read_text())
            for item in data["research_evidence"]
        ]
        self.assertEqual(json.loads(bodies[0]["text"]), {"synthetic": True})
        self.assertFalse(bodies[1]["content_collected"])

    def test_config_validation_does_not_touch_files_or_transport(self):
        with patch.object(
            Path, "stat", side_effect=AssertionError("filesystem access")
        ):
            backend.validate_config(self.config)


if __name__ == "__main__":
    unittest.main()
