"""Contract tests for the independent long-training lane, without remote execution."""

import ast
import copy
import json
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import tempfile
import unittest

from integrations.protok import long_training as lane


def request():
    return {
        "id": "L0123456789abcdef",
        "source_project": "/canonical",
        "source_workspace": "/source",
        "source_workspace_ref": "a" * 40,
        "source_iteration": "0002",
        "candidate": {
            "id": "0002:decoder",
            "artifact": {"path": "model", "version": "v1"},
        },
        "nomination": {
            "basis": "explicit",
            "reason": "test",
            "evidence": [],
            "comparison_candidate_id": None,
        },
        "recipe": copy.deepcopy(lane.RECIPE),
        "evaluation": {
            "id": "long10k",
            "comparison_scope": "long-full",
            "protocol": "protok-10k-full",
            "data_role": "development",
            "allow_feedback": True,
            "objectives": [
                {
                    "name": name,
                    "direction": "minimize" if "rmsd" in name else "maximize",
                    "unit": "angstrom" if "rmsd" in name else "ratio",
                }
                for name in lane.METRICS
            ],
            "constraints": [],
        },
    }


def completion(req, identity, remote):
    return {
        "status": "complete",
        "run_id": req["id"],
        "protocol": req["evaluation"]["id"],
        "source_identity": identity,
        "recipe": lane.RECIPE,
        "optimizer_step": 10000,
        "scheduler_horizon": 50000,
        "points": [
            {
                "optimizer_step": step,
                "proteins": 4491,
                "scope": "full",
                "ca_rmsd": 2.0,
                "tm_score": 0.7,
                "lddt_ca": 0.8,
            }
            for step in lane.STEPS
        ],
        "checkpoints": [
            {
                "optimizer_step": step,
                "path": remote + f"/train/checkpoint-step-{step}.pt",
            }
            for step in lane.STEPS
        ],
    }


def setup(tmp_path):
    root = tmp_path / "run"
    control = root / "control"
    control.mkdir(parents=True)
    owner = {
        "owner_token": "test",
        "handoff_acknowledged": True,
        "expires_at": 100000.0,
    }
    lane._write(control / "ownership.json", owner)
    lane._write(
        control / "long-training-policy.json",
        {
            "schema": 1,
            "authorization": "explicit test",
            "gpu_seconds_per_run": 3600,
            "total_gpu_seconds": 7200,
            "deadline": 100000.0,
        },
    )
    project = tmp_path / "canonical"
    project.mkdir()
    req = request()
    req["source_project"] = "/shared-parent-source"
    study = root / "study"
    lane._write(study / "study.json", {"workspace": req["source_project"]})
    metadata = {
        "status": "reviewed",
        "workspace": req["source_workspace"],
        "workspace_ref": req["source_workspace_ref"],
    }
    iteration = study / "iterations" / req["source_iteration"]
    lane._write(iteration / "iteration.json", metadata)
    lane._write(iteration / "publication.json", {"metadata": metadata})
    lane._write(iteration / "candidates.json", {"candidates": [req["candidate"]]})
    lane._write(study / "state.json", {"iteration": 2, "phase": "done"})
    config = {
        "locked_files": {"scripts/autoresearch_se3_eval.py": "test"},
        "cpus": 8,
        "mem": "96G",
        "remote_root": "/remote",
        "ssh_host": "test",
        "remote_python": "/data/user/hongruchen/projects/ProTok/.venv/bin/python",
        "fixed_config": "fixed.json",
        "candidate_config": "candidate.json",
        "cache_root": "/cache",
        "target_root": "/target",
        "workers": 2,
    }
    backend = SimpleNamespace(
        project=project, owner_token="test", _config=lambda: config
    )
    run = root / "study/long-runs" / req["id"]
    token = {
        "version": 1,
        "request": req,
        "child": str((run / "study").resolve()),
        "entered_at": 90.0,
        "cutoff": 100000.0,
        "authorization": {
            "policy": lane._policy(control),
            "owner": owner,
            "continuation": None,
        },
    }
    opaque = {"version": 1, "id": req["id"], "nonce": "test-only"}
    lane._write(run / "request.json", req)
    lane._write(
        control / "long-training-rounds" / (req["id"] + ".json"),
        token | {"token": opaque, "committed_at": 90.0},
    )
    lane._write(
        run / "study/state.json",
        {
            "iteration": 1,
            "phase": "execute",
            "round_admission": opaque,
            "round_admitted_at": 90.0,
        },
    )
    lane._write(
        run / "study/iterations/0001/iteration.json", {"round_admission": opaque}
    )
    return root, control, req, config, backend


class Transport:
    def __init__(self):
        self.submissions = 0
        self.fail_accounting = False

    def ssh(self, *args, **kwargs):
        return ""

    def run(self, *args, **kwargs):
        return ""

    def submit(self, config, record, state, attempt):
        assert (record / "job.json").is_file()
        attempt.update(status="running", owned_job_id="123", submitted_at=100.0)
        lane._write(record / "state.json", state)
        self.submissions += 1

    def wait_for_job(self, config, session, record, state, attempt):
        if self.fail_accounting:
            raise RuntimeError("missing sacct receipt")
        attempt.update(
            status="finished",
            terminal_state="COMPLETED",
            exit_code="0:0",
            gpu_seconds=3000,
            owned_job_id="123",
        )
        lane._write(record / "state.json", state)

    def cancel_interrupted(self, *args):
        pass


def fake_snapshot(backend, request, record, config, fixed):
    source = record / "source"
    source.mkdir()
    lane._write(
        source / "fixed.json",
        {"teacher_checkpoint": "/teacher", "source_manifest": "/source-manifest"},
    )
    return source, "b" * 64


class LongTrainingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp_path = Path(temporary.name)

    def test_recipe_and_complete_curve(self):
        req = request()
        lane._validate_request(req)
        value = completion(req, "identity", "/remote")
        curve = lane._curve(value, req, "identity", "/remote")
        assert [point["step"] for point in curve] == lane.STEPS
        assert curve[-1]["checkpoint"] == "long-checkpoint:L0123456789abcdef:10000"
        assert curve[-1]["lddt"] == 0.8
        for field, replacement in [
            ("proteins", 449),
            ("lddt_ca", None),
            ("optimizer_step", 9999),
        ]:
            invalid = copy.deepcopy(value)
            invalid["points"][2][field] = replacement
            with self.assertRaises(lane.ProTokError):
                lane._curve(invalid, req, "identity", "/remote")
        req["recipe"]["initialization"] = "resume"
        with self.assertRaises(lane.ProTokError):
            lane._validate_request(req)

    def test_long_checkpoint_protocol_set_after_parsing_before_first_save(self):
        driver = Path(lane.__file__).with_name("long_train.py")
        tree = ast.parse(driver.read_text())
        run = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "run"
        )
        parsed = next(
            node.lineno
            for node in ast.walk(run)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and ast.unparse(node.func) == "frozen.parse_args"
        )
        assigned = next(
            node
            for node in ast.walk(run)
            if isinstance(node, ast.Assign)
            and any(ast.unparse(target) == "frozen.PROTOCOL" for target in node.targets)
        )
        saved = min(
            node.lineno
            for node in ast.walk(run)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and ast.unparse(node.func) == "frozen.save_checkpoint"
        )
        self.assertLess(parsed, assigned.lineno)
        self.assertLess(assigned.lineno, saved)
        self.assertEqual(ast.unparse(assigned.value), "config['protocol']")

    def test_low_priority_batch(self):
        text = lane._batch(
            {
                "cpus": 8,
                "mem": "96G",
                "remote_python": "/data/user/hongruchen/projects/ProTok/.venv/bin/python",
            },
            "/remote",
            7200,
        )
        assert "#SBATCH --partition=acd_u" in text
        assert "#SBATCH --time=02:00:00" in text
        assert "ulimit" not in text
        assert '"--unshare-net"' in text
        assert '"--ro-bind"' in text
        assert "GPU-" in text
        assert "/remote/train" in text and "/output" in text

    def test_execution_binds_final_metrics_and_separate_accounting(self):
        tmp_path = self.tmp_path
        root, control, req, config, backend = setup(tmp_path)
        transport = Transport()
        remote = config["remote_root"] + "/long-training/" + req["id"]
        with (
            patch.object(lane, "_load_backend", return_value=(transport, config)),
            patch.object(lane, "_snapshot", side_effect=fake_snapshot),
            patch.object(
                lane,
                "_collect",
                return_value={
                    "completion": completion(req, "b" * 64, remote),
                    "progress": None,
                },
            ),
            patch.object(lane.time, "time", return_value=100.0),
        ):
            envelope = lane.execute(
                backend, root, req, root / "workspace", deadline=100000.0
            )
            assert envelope["outcome"] == "completed"
            launcher = lane._read(
                control / "long-training-jobs" / req["id"] / "source/long-config.json"
            )
            self.assertEqual(launcher["protocol"], req["evaluation"]["id"])
            measurement = envelope["measurements"][0]
            assert measurement["comparison_scope"] == "long-full"
            assert [row["value"] for row in measurement["metrics"]] == [2.0, 0.7, 0.8]
            assert lane.usage(control) == {"gpu_seconds": 3000, "outstanding": []}
            repeated = lane.execute(
                backend, root, req, root / "repeated", deadline=100000.0, recover=True
            )
            assert repeated["measurements"][0]["metrics"] == measurement["metrics"]
            assert transport.submissions == 1
            receipt_path = control / "long-training-jobs" / req["id"] / "receipt.json"
            altered = lane._read(receipt_path)
            altered["source_identity"] = "other-source"
            lane._write(receipt_path, altered)
            with self.assertRaisesRegex(
                lane.ProTokError, "differs from its allocation"
            ):
                lane.execute(
                    backend,
                    root,
                    req,
                    root / "repeated",
                    deadline=100000.0,
                    recover=True,
                )

    def test_unknown_effects_block_until_same_job_recovered(self):
        tmp_path = self.tmp_path
        root, control, req, config, backend = setup(tmp_path)
        transport = Transport()
        transport.fail_accounting = True
        remote = config["remote_root"] + "/long-training/" + req["id"]
        with (
            patch.object(lane, "_load_backend", return_value=(transport, config)),
            patch.object(lane, "_snapshot", side_effect=fake_snapshot),
            patch.object(
                lane,
                "_collect",
                return_value={
                    "completion": completion(req, "b" * 64, remote),
                    "progress": None,
                },
            ),
            patch.object(lane.time, "time", return_value=100.0),
        ):
            with self.assertRaisesRegex(lane.ProTokError, "unresolved"):
                lane.execute(backend, root, req, root / "workspace", deadline=100000.0)
            assert (
                json.loads((root / "workspace/result.json").read_text())["status"]
                == "blocked"
            )
            assert lane.usage(control)["outstanding"]
            with self.assertRaisesRegex(lane.ProTokError, "retained effects"):
                lane.execute(backend, root, req, root / "workspace", deadline=100000.0)
            transport.fail_accounting = False
            recovered = lane.execute(
                backend, root, req, root / "workspace", deadline=100000.0, recover=True
            )
            assert recovered["outcome"] == "completed"
            assert transport.submissions == 1

    def test_admitted_execute_after_cutoff_and_owner_expiry_keeps_real_usage(self):
        root, control, req, config, backend = setup(self.tmp_path)
        transport = Transport()
        original_wait = transport.wait_for_job

        def overrun(*args):
            original_wait(*args)
            args[-1]["gpu_seconds"] = 3610

        transport.wait_for_job = overrun
        with (
            patch.object(lane, "_load_backend", return_value=(transport, config)),
            patch.object(lane, "_snapshot", side_effect=fake_snapshot),
            patch.object(lane.time, "time", return_value=200000.0),
        ):
            result = lane.execute(
                backend, root, req, root / "workspace", deadline=210000.0
            )
        self.assertEqual(result["outcome"], "incomplete")
        self.assertEqual(lane.usage(control), {"gpu_seconds": 3610, "outstanding": []})
        self.assertEqual(transport.submissions, 1)

    def test_new_waiter_has_no_queue_cutoff_and_keeps_frozen_config(self):
        root, control, req, config, backend = setup(self.tmp_path)
        config["queue_timeout_seconds"] = 30
        transport = Transport()
        wait = transport.wait_for_job

        def pending_wait(current, session, record, state, attempt):
            # Match the evaluator's separate PENDING and allocation wall checks.
            self.assertLess(1000000, current["queue_timeout_seconds"])
            self.assertLess(1000000, session["deadline"])
            wait(current, session, record, state, attempt)

        transport.wait_for_job = pending_wait
        remote = config["remote_root"] + "/long-training/" + req["id"]
        with (
            patch.object(lane, "_load_backend", return_value=(transport, config)),
            patch.object(lane, "_snapshot", side_effect=fake_snapshot),
            patch.object(
                lane,
                "_collect",
                return_value={
                    "completion": completion(req, "b" * 64, remote),
                    "progress": None,
                },
            ),
            patch.object(lane.time, "time", return_value=200000.0),
        ):
            lane.execute(backend, root, req, root / "workspace", deadline=200001.0)
        job = lane._read(control / "long-training-jobs" / req["id"] / "job.json")
        self.assertEqual(job["config"]["queue_timeout_seconds"], 30)
        self.assertEqual(config["queue_timeout_seconds"], 30)
        self.assertEqual(job["deadline"], 200001.0)

    def test_prepared_admission_without_real_child_cannot_submit(self):
        root, _, req, _, backend = setup(self.tmp_path)
        (root / "study/long-runs" / req["id"] / "study/state.json").unlink()
        with patch.object(lane, "_load_backend", return_value=(Transport(), {})):
            with self.assertRaisesRegex(lane.ProTokError, "committed child"):
                lane.execute(backend, root, req, root / "workspace", deadline=200000.0)

    def test_checkpoint_requires_review(self):
        tmp_path = self.tmp_path
        root, control, req, config, backend = setup(tmp_path)
        transport = Transport()
        remote = config["remote_root"] + "/long-training/" + req["id"]
        with (
            patch.object(lane, "_load_backend", return_value=(transport, config)),
            patch.object(lane, "_snapshot", side_effect=fake_snapshot),
            patch.object(
                lane,
                "_collect",
                return_value={
                    "completion": completion(req, "b" * 64, remote),
                    "progress": None,
                },
            ),
            patch.object(lane.time, "time", return_value=100.0),
        ):
            lane.execute(
                backend,
                root,
                req,
                root / "study/long-runs" / req["id"] / "study/workspace",
                deadline=100000.0,
            )
        run = root / "study/long-runs" / req["id"]
        child = run / "study"
        publication = child / "iterations/0001/publication.json"
        metadata = {"status": "reviewed"}
        lane._write(publication, {"metadata": metadata})
        lane._write(publication.parent / "iteration.json", metadata)
        lane._write(
            child / "state.json",
            {
                "phase": "done",
                "iteration": 1,
                "review_ref": "review-response.json",
                "execution_ref": "execute-response.json",
            },
        )
        result = lane._read(
            root / "study/long-runs" / req["id"] / "study/workspace/result.json"
        )
        execute = lane._envelope(
            req,
            root / "study/long-runs" / req["id"] / "study/workspace/result.json",
            result,
        )
        review = {"candidate_reviews": []}
        lane._write(child / "review-response.json", review)
        lane._write(child / "execute-response.json", execute)
        wrapper = {
            "id": req["id"],
            "candidate_id": req["candidate"]["id"],
            "publication_ref": str(publication),
            "review": review,
            "execute": execute,
        }
        lane._write(run / "review.json", wrapper)
        with self.assertRaises(lane.ProTokError):
            lane.resolve_checkpoint(root, req["id"], 2000)
        review["candidate_reviews"] = [
            {"candidate_id": req["candidate"]["id"], "valid": True, "verified": True}
        ]
        lane._write(child / "review-response.json", review)
        lane._write(run / "review.json", wrapper)
        assert (
            lane.resolve_checkpoint(root, req["id"], 2000)["path"]
            == remote + "/train/checkpoint-step-2000.pt"
        )
        lane._write(publication.parent / "iteration.json", {"status": "executed"})
        with self.assertRaisesRegex(lane.ProTokError, "not been published"):
            lane.resolve_checkpoint(root, req["id"], 2000)

    def test_settled_invalid_validation_has_no_score_and_keeps_charge(self):
        tmp_path = self.tmp_path
        root, control, req, config, backend = setup(tmp_path)
        transport = Transport()
        remote = config["remote_root"] + "/long-training/" + req["id"]
        invalid = completion(req, "b" * 64, remote)
        invalid["points"][0]["proteins"] = 449
        with (
            patch.object(lane, "_load_backend", return_value=(transport, config)),
            patch.object(lane, "_snapshot", side_effect=fake_snapshot),
            patch.object(
                lane, "_collect", return_value={"completion": invalid, "progress": None}
            ),
            patch.object(lane.time, "time", return_value=100.0),
        ):
            envelope = lane.execute(
                backend, root, req, root / "workspace", deadline=100000.0
            )
        assert envelope["measurements"] == []
        assert lane._read(root / "workspace/result.json")["status"] == "failed"
        assert lane.usage(control) == {"gpu_seconds": 3000, "outstanding": []}

    def test_source_project_and_candidate_must_match_parent_publication(self):
        tmp_path = self.tmp_path
        root, _, req, _, _ = setup(tmp_path)
        lane._published_source(root, req)
        altered = copy.deepcopy(req)
        altered["candidate"]["artifact"]["version"] = "other"
        with self.assertRaisesRegex(lane.ProTokError, "published parent evidence"):
            lane._published_source(root, altered)
        altered = copy.deepcopy(req)
        altered["source_project"] = "/other"
        with self.assertRaisesRegex(lane.ProTokError, "parent Study"):
            lane._published_source(root, altered)

    def test_scored_source_requires_identical_candidate_and_trusted_receipt(self):
        tmp_path = self.tmp_path
        req = request()
        for name in lane.CANDIDATE_FILES:
            path = tmp_path / "source" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("source")
        config = {"records_root": str(tmp_path / "records")}
        fixed = SimpleNamespace(fingerprint=lambda config, candidates: "expected")
        with self.assertRaisesRegex(lane.ProTokError, "scored fingerprint"):
            lane._scored_source(fixed, config, req, tmp_path / "source")
        req["candidate"]["artifact"]["version"] = "expected"
        with self.assertRaisesRegex(lane.ProTokError, "trusted short receipt"):
            lane._scored_source(fixed, config, req, tmp_path / "source")
        receipt = Path(config["records_root"]) / "expected"
        lane._write(
            receipt / "state.json",
            {
                "status": "complete",
                "fingerprint": "expected",
                "attempts": [
                    {
                        "terminal_state": "COMPLETED",
                        "exit_code": "0:0",
                        "gpu_seconds": 100,
                    }
                ],
            },
        )
        lane._write(
            receipt / "summary.json",
            {"fingerprint": "expected", "quality_score_eligible": True},
        )
        assert (
            lane._scored_source(fixed, config, req, tmp_path / "source") == "expected"
        )


if __name__ == "__main__":
    unittest.main()
