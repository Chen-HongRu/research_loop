"""Manual allocations use frozen task authority, without short-score evidence."""

import copy
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from integrations.protok import long_training as lane
from integrations.protok.backend import CANDIDATE_FILES, ProTokBackend
from integrations.protok.tests.test_long_training import (
    Transport,
    completion,
    setup,
)
from research_loop.long_training import admission_path, admitted_round


def git(folder, *args):
    return subprocess.check_output(
        ["git", "-C", str(folder), *args], text=True, stderr=subprocess.DEVNULL
    ).strip()


class ManualTransport(Transport):
    @staticmethod
    def fingerprint(config, files):
        return hashlib.sha256(
            b"".join(files[name] for name in CANDIDATE_FILES)
        ).hexdigest()


class ManualLongBackendTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root, self.control, self.req, self.config, self.backend = setup(
            Path(temporary.name).resolve()
        )
        self.task_id = "operator-long"
        self.folder = self.root / "study/tasks" / self.task_id
        baseline = self.folder / "baseline"
        baseline.mkdir(parents=True)
        git(baseline, "init", "--quiet")
        self.config["candidate_config"] = CANDIDATE_FILES[-1]
        files = {name: b"# candidate\n" for name in CANDIDATE_FILES}
        files.update(
            {
                "fixed.json": b'{"teacher_checkpoint":"/teacher","source_manifest":"/manifest"}',
                "scripts/autoresearch_se3_eval.py": b"# trusted evaluator\n",
            }
        )
        for name, value in files.items():
            path = baseline / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        self.config["locked_files"] = {
            name: hashlib.sha256(files[name]).hexdigest()
            for name in ("fixed.json", "scripts/autoresearch_se3_eval.py")
        }
        self.config["records_root"] = str(self.root / "short-records")
        git(baseline, "add", ".")
        git(
            baseline,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        )
        revision = git(baseline, "rev-parse", "HEAD")
        self.source = self.folder / "source"
        git(
            self.folder, "clone", "--shared", "--quiet", str(baseline), str(self.source)
        )
        # The baseline remains frozen; edited code has no short-score receipt.
        files[CANDIDATE_FILES[0]] = b"# edited candidate\n"
        (self.source / CANDIDATE_FILES[0]).write_bytes(files[CANDIDATE_FILES[0]])
        git(self.source, "add", CANDIDATE_FILES[0])
        git(
            self.source,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "--quiet",
            "-m",
            "new candidate",
        )
        self.manual = {
            "version": 1,
            "id": self.task_id,
            "kind": "long_training",
            "title": "Explicit new decoder long run",
            "instruction": "Run the requested frozen candidate directly for 10k steps",
            "route_id": "new",
            "base_experiment": None,
            "priority": 100,
            "allow_changes": True,
            "budget": {
                key: value
                for key, value in lane._policy(self.control).items()
                if key != "schema"
            },
            "submitted_at": "2026-11-02T00:00:00+00:00",
            "source": {
                "project": str(baseline),
                "workspace": str(baseline),
                "revision": revision,
                "experiment": None,
            },
        }
        self.req.update(
            manual_task_id=self.task_id,
            source_project=str(baseline),
            source_workspace=str(self.source),
            source_workspace_ref=git(self.source, "rev-parse", "HEAD"),
            source_iteration="0001",
        )
        self.req.pop("nomination")
        self.transport = ManualTransport()
        fingerprint = self.transport.fingerprint(self.config, files)
        self.req["candidate"] = {
            "id": fingerprint,
            "artifact": {
                "path": self.config["candidate_config"],
                "version": fingerprint,
            },
        }
        self.child = self.folder / "study"
        self.context = self.child / "iterations/0001/context"
        lane._write(self.folder / "request.json", self.manual)
        lane._write(
            self.folder / "binding.json",
            {"study": str(self.child), "iteration": "0001"},
        )
        lane._write(
            self.child / "study.json",
            {
                "manual_task": self.manual,
                "workspace": str(baseline),
                "baseline": revision,
            },
        )
        lane._write(self.context / "task.json", self.manual)
        lane._write(self.context / "manual-long-request.json", self.req)
        opaque = {"version": 1, "id": self.task_id, "nonce": "test-only"}
        self.token = {
            "token": opaque,
            "child": str(self.child),
            "request": self.manual,
            "entered_at": 90.0,
            "committed_at": 90.0,
            "cutoff": 100000.0,
            "authorization": None,
        }
        with patch.object(lane.time, "time", return_value=100.0):
            self.token["authorization"] = lane.round_authorization(
                self.backend, self.root, {"manual_task_id": self.task_id}
            )
        lane._write(admission_path(self.folder), self.token)
        lane._write(
            self.child / "state.json",
            {
                "iteration": 1,
                "phase": "execute",
                "round_admission": opaque,
                "round_admitted_at": 90.0,
            },
        )
        lane._write(
            self.child / "iterations/0001/iteration.json", {"round_admission": opaque}
        )
        self.workspace = self.child / "iterations/0001/workspace"
        self.record = self.control / "manual-training-jobs" / self.req["id"]

    def execute(self, *, recover=False):
        def collect(fixed, config, remote):
            job = lane._read(self.record / "job.json")
            return {"completion": completion(self.req, job["source_identity"], remote)}

        with (
            patch.object(
                lane, "_load_backend", return_value=(self.transport, self.config)
            ),
            patch.object(lane, "_collect", side_effect=collect),
            patch.object(lane.time, "time", return_value=100.0),
            patch("integrations.protok.telemetry.job_relay", return_value=None),
            patch("integrations.protok.telemetry.drain_existing"),
        ):
            return lane.execute(
                self.backend,
                self.root,
                self.req,
                self.workspace,
                deadline=100000.0,
                recover=recover,
            )

    def ledger(self, pool, run_id, seconds, *, task_id=None):
        folder = (
            self.control
            / ("manual-training-jobs" if pool == "manual" else "long-training-jobs")
            / run_id
        )
        label = (
            {"manual_task_id": task_id, "budget_pool": "manual"}
            if pool == "manual"
            else {}
        )
        lane._write(
            folder / "job.json",
            {
                "run_id": run_id,
                "source_identity": "fixture",
                "request": label,
                **label,
            },
        )
        lane._write(
            folder / "receipt.json",
            {
                "run_id": run_id,
                "source_identity": "fixture",
                "status": "completed",
                "gpu_seconds": seconds,
                **label,
            },
        )

    def test_new_manual_candidate_runs_without_short_receipt_and_automatic_budget(self):
        self.ledger("automatic", "Lfedcba9876543210", 7200)
        (self.control / "long-training-policy.json").unlink()
        self.assertEqual(self.execute()["outcome"], "completed")
        self.assertFalse(Path(self.config["records_root"]).exists())
        job = lane._read(self.record / "job.json")
        self.assertEqual(job["remote"], "/remote/manual-training/" + self.req["id"])
        self.assertEqual(job["manual_task_id"], self.task_id)
        self.assertEqual(lane.usage(self.control)["gpu_seconds"], 7200)
        self.assertEqual(
            lane.usage(self.control, pool="manual"),
            {"gpu_seconds": 3000, "outstanding": []},
        )
        self.assertEqual(self.transport.submissions, 1)

    def test_manual_exhaustion_does_not_consume_automatic_or_another_task_grant(self):
        self.ledger("manual", "Lfedcba9876543210", 7200, task_id=self.task_id)
        with patch.object(lane.time, "time", return_value=100.0):
            self.assertEqual(
                lane.round_authorization(self.backend, self.root, {})["policy"],
                lane._policy(self.control),
            )
            with self.assertRaisesRegex(lane.ProTokError, "exhausted"):
                lane.round_authorization(
                    self.backend, self.root, {"manual_task_id": self.task_id}
                )
        other = copy.deepcopy(self.manual)
        other["id"] = "other-task"
        lane._write(self.root / "study/tasks/other-task/request.json", other)
        with patch.object(lane.time, "time", return_value=100.0):
            lane.round_authorization(
                self.backend, self.root, {"manual_task_id": "other-task"}
            )
        self.assertEqual(
            lane.usage(self.control), {"gpu_seconds": 0, "outstanding": []}
        )

    def test_inspect_charges_manual_only_to_total(self):
        self.execute()
        self.ledger("automatic", "Lfedcba9876543210", 17)
        backend = ProTokBackend(self.backend.project, self.control)
        with patch.object(backend, "_config", return_value=self.config):
            status = backend.inspect()
        self.assertEqual(status["used_gpu_seconds"], 0)
        self.assertEqual(status["long_training_gpu_seconds"], 17)
        self.assertEqual(status["manual_training_gpu_seconds"], 3000)
        self.assertEqual(status["manual_training_outstanding"], [])
        self.assertEqual(status["total_gpu_seconds"], 3017)

    def test_source_tamper_and_fingerprint_change_rejected_before_submission(self):
        path = self.source / CANDIDATE_FILES[0]
        original = path.read_bytes()
        path.write_bytes(b"# tampered\n")
        with self.assertRaisesRegex(lane.ProTokError, "frozen Git revision"):
            self.execute()
        path.write_bytes(original)
        self.req["candidate"]["id"] = "wrong"
        lane._write(self.context / "manual-long-request.json", self.req)
        with self.assertRaisesRegex(lane.ProTokError, "fingerprint"):
            self.execute()
        self.assertEqual(self.transport.submissions, 0)

    def test_request_budget_changes_and_binding_replacement_rejected(self):
        modified = copy.deepcopy(self.manual)
        modified["budget"]["total_gpu_seconds"] *= 2
        lane._write(self.folder / "request.json", modified)
        with self.assertRaisesRegex(lane.ProTokError, "binding"):
            self.execute()
        lane._write(self.folder / "request.json", self.manual)
        lane._write(
            self.folder / "binding.json",
            {"study": str(self.root / "another-child"), "iteration": "0001"},
        )
        with self.assertRaisesRegex(lane.ProTokError, "binding"):
            self.execute()
        self.assertEqual(self.transport.submissions, 0)

    def test_frozen_revision_and_protected_source_changes_rejected(self):
        original_revision = self.req["source_workspace_ref"]
        git(self.source, "checkout", "--detach", self.manual["source"]["revision"])
        with self.assertRaisesRegex(lane.ProTokError, "Git revision changed"):
            self.execute()
        git(self.source, "checkout", "--detach", original_revision)
        (self.source / "scripts/autoresearch_se3_eval.py").write_text(
            "# altered evaluator\n"
        )
        with self.assertRaisesRegex(lane.ProTokError, "locked source"):
            self.execute()
        self.assertEqual(self.transport.submissions, 0)

    def test_manual_admission_rejects_changed_original_authority(self):
        modified = copy.deepcopy(self.manual)
        modified["budget"]["total_gpu_seconds"] *= 2
        lane._write(self.folder / "request.json", modified)
        with self.assertRaisesRegex(lane.ProTokError, "request identity changed"):
            lane.round_authorization(
                self.backend,
                self.root,
                {"manual_task_id": self.task_id},
                self.token,
                reconcile=True,
            )

    def test_unknown_allocation_recovers_same_job_without_resubmission(self):
        self.transport.fail_accounting = True
        with self.assertRaisesRegex(lane.ProTokError, "unresolved"):
            self.execute()
        self.assertEqual(len(lane.usage(self.control, pool="manual")["outstanding"]), 1)
        with self.assertRaisesRegex(lane.ProTokError, "retained effects"):
            self.execute()
        self.transport.fail_accounting = False
        self.assertEqual(self.execute(recover=True)["outcome"], "completed")
        self.assertEqual(self.execute(recover=True)["outcome"], "completed")
        self.assertEqual(self.transport.submissions, 1)

    def test_prepared_allocation_never_resubmits_and_original_request_remains_required(
        self,
    ):
        self.transport.fail_accounting = True
        with self.assertRaises(lane.ProTokError):
            self.execute()
        state = lane._read(self.record / "state.json")
        state["attempts"][0]["status"] = "prepared"
        lane._write(self.record / "state.json", state)
        with self.assertRaisesRegex(lane.ProTokError, "prepared"):
            self.execute(recover=True)
        self.req["source_workspace_ref"] = "a" * 40
        with self.assertRaisesRegex(lane.ProTokError, "binding"):
            self.execute(recover=True)
        self.assertEqual(self.transport.submissions, 1)

    def test_committed_manual_admission_is_separate_and_required(self):
        self.assertEqual(
            admission_path(self.folder),
            self.control / "manual-task-rounds/operator-long.json",
        )
        self.assertIsNotNone(admitted_round(self.folder))
        admission_path(self.folder).unlink()
        with self.assertRaisesRegex(lane.ProTokError, "binding"):
            self.execute()
        self.assertEqual(self.transport.submissions, 0)

    def test_manual_partial_checkpoint_requires_published_per_point_acceptance(self):
        self.execute()
        receipt = lane._read(self.record / "receipt.json")
        receipt.update(
            status="failed",
            error="fixture timeout after a complete 2k full-validation point",
            curve=receipt["curve"][:1],
            endpoint_complete=False,
            missing_validation_steps=lane.STEPS[1:],
        )
        lane._write(self.record / "receipt.json", receipt)
        lane._write(self.workspace / "result.json", receipt)
        execute = lane._envelope(self.req, self.workspace / "result.json", receipt)
        review = {
            "candidate_reviews": [],
            "observation_reviews": [
                {
                    "step": 2000,
                    "valid": True,
                    "verified": True,
                    "reason": "Complete full4491 point",
                }
            ],
        }
        state = lane._read(self.child / "state.json")
        state.update(
            phase="done",
            review_ref="review-response.json",
            execution_ref="execute-response.json",
        )
        metadata = lane._read(self.child / "iterations/0001/iteration.json")
        metadata.update(status="reviewed", workspace=str(self.workspace))
        publication = self.child / "iterations/0001/publication.json"
        lane._write(self.child / "state.json", state)
        lane._write(publication.parent / "iteration.json", metadata)
        lane._write(publication, {"metadata": metadata, "state_after": state})
        lane._write(self.child / "review-response.json", review)
        lane._write(self.child / "execute-response.json", execute)
        lane._write(
            self.child / "context.json", {"long_training_observations": receipt}
        )
        point = lane.resolve_checkpoint(self.root, self.req["id"], 2000)
        self.assertEqual(
            point["path"],
            "/remote/manual-training/"
            + self.req["id"]
            + "/train/checkpoint-step-2000.pt",
        )
        self.assertEqual(point["optimizer_step"], 2000)
        with self.assertRaisesRegex(lane.ProTokError, "accepting Review"):
            lane.resolve_checkpoint(self.root, self.req["id"], 4000)
        review["observation_reviews"][0]["verified"] = False
        lane._write(self.child / "review-response.json", review)
        with self.assertRaisesRegex(lane.ProTokError, "accepting Review"):
            lane.resolve_checkpoint(self.root, self.req["id"], 2000)
        review["observation_reviews"][0]["verified"] = True
        lane._write(self.child / "review-response.json", review)
        lane._write(
            publication.parent / "iteration.json", metadata | {"status": "executed"}
        )
        with self.assertRaisesRegex(lane.ProTokError, "not been published"):
            lane.resolve_checkpoint(self.root, self.req["id"], 2000)
