"""Real local collection and settled partial-allocation regressions; no GPU."""

import copy
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from integrations.protok import long_training as lane
from integrations.protok.long_train import progress_record
from integrations.protok import research_backend
from integrations.protok.tests import test_long_training as fixture


def bundle(request, remote, steps=3, *, actual_step=8000):
    whole = fixture.completion(request, "b" * 64, remote)
    progress = progress_record(
        {
            "run_id": request["id"],
            "protocol": request["evaluation"]["id"],
            "source_identity": "b" * 64,
            "recipe": request["recipe"],
        },
        actual_step,
        "validation" if actual_step else "initializing",
        whole["points"][:steps],
        whole["checkpoints"][:steps],
        {"optimizer_step": actual_step, "train/loss": 0.3} if actual_step else None,
    )
    return {
        "completion": None,
        "progress": progress,
        "training_log_tail": "optimizer_step,train/loss\n8000,0.3\n",
        "stdout_tail": "starting full validation at8000\n",
        "errors": [],
    }


class LocalTransport:
    def ssh(self, config, command, **kwargs):
        return subprocess.check_output(shlex.split(command), text=True)


class PartialResultTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def execute_timeout(self, *, steps=3, invalid=False, unknown=False):
        root, control, request, config, backend = fixture.setup(self.tmp)
        remote = config["remote_root"] + "/long-training/" + request["id"]
        transport = fixture.Transport()
        transport.fail_accounting = unknown
        wait = transport.wait_for_job

        def timeout(*args):
            wait(*args)
            args[-1].update(
                terminal_state="TIMEOUT", exit_code="0:15", gpu_seconds=3610
            )

        transport.wait_for_job = timeout
        collected = bundle(request, remote, steps)
        if invalid:
            collected["progress"]["source_identity"] = "different"
        workspace = root / "study/long-runs" / request["id"] / "study/workspace"
        with (
            patch.object(lane, "_load_backend", return_value=(transport, config)),
            patch.object(lane, "_snapshot", side_effect=fixture.fake_snapshot),
            patch.object(lane, "_collect", return_value=collected) as collect,
            patch.object(lane.time, "time", return_value=100.0),
        ):
            if unknown:
                with self.assertRaisesRegex(lane.ProTokError, "unresolved"):
                    lane.execute(backend, root, request, workspace, deadline=100000.0)
                self.assertEqual(collect.call_count, 0)
                return
            execute = lane.execute(backend, root, request, workspace, deadline=100000.0)
            replay = lane.execute(
                backend, root, request, workspace, deadline=100000.0, recover=True
            )
        self.assertEqual(replay, execute)
        self.assertEqual(transport.submissions, 1)
        self.assertEqual(lane.usage(control), {"gpu_seconds": 3610, "outstanding": []})
        return root, request, execute, lane._read(workspace / "result.json")

    def test_timeout_during_8k_validation_keeps_2k_4k_6k_and_actual_overrun(self):
        _, _, execute, result = self.execute_timeout()
        self.assertEqual(execute["outcome"], "incomplete")
        self.assertEqual(execute["measurements"], [])
        self.assertFalse(result["endpoint_complete"])
        self.assertEqual([row["step"] for row in result["curve"]], [2000, 4000, 6000])
        self.assertEqual(result["missing_validation_steps"], [8000, 10000])
        self.assertEqual(result["progress"]["optimizer_step"], 8000)
        self.assertEqual(result["progress"]["phase"], "validation")
        self.assertEqual(result["curve"][-1]["tm_score"], 0.7)
        self.assertEqual(result["curve"][-1]["lddt"], 0.8)
        self.assertIn("starting full validation", result["stdout_tail"])

    def test_no_full_point_keeps_training_and_log_signal(self):
        _, _, execute, result = self.execute_timeout(steps=0)
        self.assertEqual(execute["outcome"], "incomplete")
        self.assertEqual(result["curve"], [])
        self.assertEqual(result["missing_validation_steps"], lane.STEPS)
        self.assertIn("train/loss", result["training_log_tail"])
        self.assertTrue(result["progress"])

    def test_unbound_progress_is_rejected_without_losing_logs_or_charge(self):
        _, _, execute, result = self.execute_timeout(invalid=True)
        self.assertEqual(execute["outcome"], "incomplete")
        self.assertEqual(result["curve"], [])
        self.assertEqual(result["progress"], {})
        self.assertTrue(result["evidence_errors"])
        self.assertTrue(result["stdout_tail"])

    def test_unknown_effects_never_collect_or_publish_observations(self):
        self.execute_timeout(unknown=True)

    def test_real_collector_reads_atomic_record_and_bounded_tails_only(self):
        request = fixture.request()
        remote = self.tmp / "remote"
        train = remote / "train"
        train.mkdir(parents=True)
        collected = bundle(request, str(remote))
        lane._write(train / "long-curve.json", collected["progress"])
        (train / "long-curve.json.tmp").write_text('{"points": [')
        (train / "train.csv").write_text("x" * 70000 + "last complete training row")
        (remote / "run.log").write_text("observed timeout")
        # These files must never be opened by result collection.
        (train / "checkpoint-step-6000.pt").mkdir()
        (train / "full-validation/step-8000").mkdir(parents=True)
        (train / "full-validation/step-8000/eval.csv").write_text("partial row")
        result = lane._collect(
            LocalTransport(), {"remote_python": sys.executable}, str(remote)
        )
        self.assertIsNone(result["completion"])
        self.assertEqual(result["progress"], collected["progress"])
        self.assertEqual(len(result["training_log_tail"]), 65536)
        points = lane._curve(
            result["progress"], request, "b" * 64, str(remote), complete=False
        )
        self.assertEqual([point["step"] for point in points], [2000, 4000, 6000])

    def test_incomplete_or_nonfinite_validation_never_becomes_observation(self):
        request = fixture.request()
        record = bundle(request, "/remote")["progress"]
        for field, value in [
            ("proteins", 449),
            ("tm_score", float("nan")),
            ("optimizer_step", 8000),
        ]:
            invalid = copy.deepcopy(record)
            invalid["points"][-1][field] = value
            with self.subTest(field=field), self.assertRaises(lane.ProTokError):
                lane._curve(invalid, request, "b" * 64, "/remote", complete=False)

    def test_partial_checkpoint_requires_acceptance_of_that_exact_step(self):
        root, request, execute, result = self.execute_timeout()
        run = root / "study/long-runs" / request["id"]
        child = run / "study"
        publication = child / "iterations/0001/publication.json"
        metadata = {"status": "reviewed"}
        review = {
            "candidate_reviews": [],
            "observation_reviews": [
                {
                    "step": point["step"],
                    "valid": True,
                    "verified": True,
                    "reason": "Checked exact full-validation checkpoint",
                }
                for point in result["curve"]
            ],
        }
        lane._write(publication, {"metadata": metadata})
        lane._write(publication.parent / "iteration.json", metadata)
        lane._write(
            child / "state.json",
            {
                "phase": "done",
                "iteration": 1,
                "review_ref": "review.json",
                "execution_ref": "execute.json",
            },
        )
        lane._write(child / "review.json", review)
        lane._write(
            child / "context.json",
            {"long_training_observations": copy.deepcopy(result)},
        )
        lane._write(child / "execute.json", execute)
        wrapper = {
            "id": request["id"],
            "candidate_id": request["candidate"]["id"],
            "publication_ref": str(publication),
            "review": review,
            "execute": execute,
        }
        lane._write(run / "review.json", wrapper)
        resolved = lane.resolve_checkpoint(root, request["id"], 6000)
        self.assertTrue(resolved["path"].endswith("checkpoint-step-6000.pt"))
        tampered = copy.deepcopy(result)
        tampered["curve"][-1]["tm_score"] = 0.9
        receipt_path = (
            root / "control/long-training-jobs" / request["id"] / "receipt.json"
        )
        artifact_path = child / "workspace/result.json"
        lane._write(receipt_path, tampered)
        lane._write(artifact_path, tampered)
        with self.assertRaisesRegex(lane.ProTokError, "frozen Review"):
            lane.resolve_checkpoint(root, request["id"], 6000)
        lane._write(receipt_path, result)
        lane._write(artifact_path, result)
        with self.assertRaises(lane.ProTokError):
            lane.resolve_checkpoint(root, request["id"], 8000)
        review["observation_reviews"][-1]["verified"] = False
        lane._write(child / "review.json", review)
        lane._write(run / "review.json", wrapper)
        with self.assertRaises(lane.ProTokError):
            lane.resolve_checkpoint(root, request["id"], 6000)

    def test_real_performance_process_retains_completed_subtests_when_terminated(self):
        remote = self.tmp / "performance"
        output = remote / "output"
        output.mkdir(parents=True)
        script = """import json,sys
from pathlib import Path
path=Path(sys.argv[1]); temporary=path.with_suffix(".tmp")
completed=[]
for name in ["A","B"]:
 completed.append({"configuration":name,"measured_steps":100,"throughput":20 if name=="A" else 22})
 temporary.write_text(json.dumps({"completed":completed,"pending":["C"]}));temporary.replace(path)
print("ready",flush=True)
sys.stdin.read()
"""
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", script, str(output / "observations.json")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
        finally:
            process.terminate()
            process.communicate(timeout=5)
        (remote / "run.log").write_text("subtest C incomplete")
        record = self.tmp / "collected"
        record.mkdir()
        paths, errors = research_backend._collect(
            LocalTransport(),
            {"remote_python": sys.executable},
            str(remote),
            ["observations.json"],
            record,
        )
        self.assertEqual(errors, [])
        evidence = json.loads(Path(paths[0]).read_text())
        self.assertEqual(
            [item["configuration"] for item in evidence["completed"]], ["A", "B"]
        )
        self.assertEqual(evidence["pending"], ["C"])


if __name__ == "__main__":
    unittest.main()
