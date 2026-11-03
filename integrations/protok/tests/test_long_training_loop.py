from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from integrations.protok import long_training_loop as lane
from integrations.protok import research_backend, server
from research_loop import Study, StudyError, initialize
from research_loop.harness import HarnessError
from research_loop.long_training import LongTrainingManager
from test_core import RecordedHarness, evaluation


class LongTrainingLoopTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.grant = {
            "schema": 1,
            "authorization": "Test-only independent allowance",
            "gpu_seconds_per_run": 7200,
            "total_gpu_seconds": 14400,
            "deadline": time.time() + 10000,
        }
        lane._write(self.root / "control/long-training-policy.json", self.grant)

    def test_recovery_rejects_older_parent_before_effects(self):
        initialize(self.root / "study", goal="Parent")
        config_path = self.root / "study/study.json"
        for version in (1, 2):
            with self.subTest(version=version):
                config = lane._read(config_path)
                config["version"] = version
                lane._write(config_path, config)
                control_files = sorted((self.root / "control").iterdir())
                with (
                    patch.object(lane, "execute") as execute,
                    self.assertRaisesRegex(StudyError, "Unsupported study format"),
                ):
                    lane.recover(self.root, Mock(), "L" + "a" * 16)
                execute.assert_not_called()
                self.assertFalse((self.root / "study/long-runs").exists())
                self.assertEqual(
                    sorted((self.root / "control").iterdir()), control_files
                )
                self.assertEqual(lane._read(config_path), config)

    def test_policy_keeps_all_three_endpoint_metrics(self):
        policy = lane.study_policy(self.root)
        self.assertIsNone(policy["max_runs"])
        self.assertEqual(policy["recipe"]["total_steps"], 10000)
        self.assertEqual(
            {metric["name"] for metric in policy["evaluation"]["objectives"]},
            set(lane.METRICS),
        )

    def test_long_worker_does_not_block_short_loop(self):
        initialize(
            self.root / "study",
            goal="Parallel scheduling",
            global_evaluation=evaluation("short"),
            long_training=lane.study_policy(self.root),
        )
        lane._write(
            self.root / "control/continuation.json",
            {
                "schema": 2,
                "seconds": 86400,
                "deadline": time.time() + 10000,
                "initial_gpu_seconds": 0,
                "additional_gpu_seconds": 86400,
            },
        )
        entered, release = threading.Event(), threading.Event()

        def long_run(**kwargs):
            entered.set()
            release.wait(3)

        long_manager = SimpleNamespace(run_one=long_run)
        scheduler = lane.LongTrainingScheduler(
            self.root, Mock(), lane_manager=long_manager, interval=0.01
        )
        scheduler.start()
        try:
            self.assertTrue(entered.wait(1))
            backend = Mock()
            backend.inspect.return_value = {"outstanding": [], "used_gpu_seconds": 0}
            backend._config.return_value = {"reserve_gpu_seconds": 3600}
            runner = Mock()
            runner.read.return_value = {"status": "ready"}

            def short_run(*args):
                (self.root / "control/STOP").write_text("test stops both lanes")
                return {"status": "ready"}

            runner.run_one.side_effect = short_run
            self.assertEqual(server.drive(self.root, runner, backend), "user_stop")
            self.assertEqual(runner.run_one.call_count, 1)
        finally:
            scheduler.stopped.set()
            release.set()
            scheduler.shutdown()

    def test_reconciled_needs_attention_execute_reaches_review_without_resubmit(self):
        policy = lane.study_policy(self.root)
        initialize(
            self.root / "study",
            goal="Parent",
            global_evaluation=evaluation("short"),
            long_training=policy,
        )
        identity = "L" + "a" * 16
        run = self.root / "study/long-runs" / identity
        request = {
            "id": identity,
            "source_iteration": "0001",
            "candidate": {"id": "0001:candidate"},
            "recipe": policy["recipe"],
            "evaluation": policy["evaluation"],
        }
        lane._write(run / "request.json", request)
        child_root = run / "study"
        initialize(
            child_root,
            goal="Fixed long run",
            global_evaluation=policy["evaluation"],
            max_seconds=10000,
        )
        interrupted = RecordedHarness()

        def fail(call):
            if call["phase"] == "execute":
                raise HarnessError("Interrupted allocation with retained effects")

        interrupted.before = fail
        child = Study(child_root, interrupted)
        with self.assertRaises(HarnessError):
            child.run()
        with self.assertRaises(Exception):
            child.run()
        self.assertEqual(child.status()["status"], "needs_attention")
        execute_attempt = child.status()["calls"]["execute"]
        lane._write(
            self.root / "control/long-training-jobs" / identity / "job.json",
            {
                "run_id": identity,
                "request": request,
                "deadline": self.grant["deadline"],
            },
        )
        receipt = {
            "outcome": "incomplete",
            "markdown": "Settled cancelled allocation; no scores",
            "artifacts": [],
            "measurements": [],
        }
        reviewer = RecordedHarness()

        def no_resubmit(*args, **kwargs):
            self.fail("Reconciled Execute must never submit again")

        manager = LongTrainingManager(self.root / "study", reviewer, no_resubmit)
        with (
            patch.object(lane, "execute", return_value=receipt) as recover,
            patch.object(lane, "manager", return_value=manager),
        ):
            result = lane.recover(self.root, Mock(), identity)
        self.assertEqual(recover.call_count, 1)
        self.assertTrue(recover.call_args.kwargs["recover"])
        self.assertEqual(result["execute"], receipt)
        self.assertEqual([call["phase"] for call in reviewer.calls], ["review"])
        self.assertEqual(
            Study(child_root).status()["calls"]["execute"], execute_attempt
        )
        self.assertEqual(
            Study(self.root / "study").knowledge()["long_training"][0]["id"], identity
        )

    def test_finish_nominations_continues_after_first_withdrawal(self):
        initialize(
            self.root / "study",
            goal="Drain queue",
            global_evaluation=evaluation("short"),
            long_training=lane.study_policy(self.root),
        )
        calls = []
        rows = [{"status": "queued"}, {"status": "queued"}]

        def run_one(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                rows[0]["status"] = "withdrawn"
                return None
            if len(calls) == 2:
                rows[1]["status"] = "reviewed"
                return {"id": "second"}
            return None

        scheduler = lane.LongTrainingScheduler(
            self.root,
            Mock(),
            lane_manager=SimpleNamespace(run_one=run_one),
            interval=0.001,
        )
        scheduler.finish_nominations()
        with patch.object(lane, "snapshot", side_effect=lambda root: rows):
            scheduler.start()
            scheduler.thread.join(2)
        self.assertFalse(scheduler.thread.is_alive())
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            scheduler.status(), {"status": "finished", "reason": "no_pending"}
        )

    def test_research_contract_accepts_reviewed_long_checkpoint_model_input(self):
        identity = "L" + "b" * 16
        source = f"long-checkpoint:{identity}:6000"
        lane._write(
            self.root / "study/long-runs" / identity / "request.json",
            {"source_iteration": "0001"},
        )
        execution = {
            "mode": "script",
            "entrypoint": "research/probe.py",
            "gpu": True,
            "time_limit_seconds": 60,
            "inputs": [{"name": "model", "source": source}],
            "outputs": ["probe.json"],
        }
        self.assertEqual(
            research_backend._execution(execution, {"max_time_limit_seconds": 1800}), 60
        )
        resolved = {
            "source": source,
            "path": "/remote/long-training/train/checkpoint-step-6000.pt",
            "source_identity": "frozen",
            "optimizer_step": 6000,
        }
        with patch(
            "integrations.protok.long_training.resolve_checkpoint",
            return_value=resolved,
        ) as resolver:
            inputs = research_backend._inputs(self.root, {}, execution, "0002")
        resolver.assert_called_once_with(
            self.root, identity, 6000, reference_error=research_backend.ProTokError
        )
        self.assertEqual(inputs[0]["path"], resolved["path"])
        self.assertEqual(inputs[0]["provenance"]["source_identity"], "frozen")
        self.assertEqual(inputs[0]["mount"], "/inputs/model")


if __name__ == "__main__":
    unittest.main()
