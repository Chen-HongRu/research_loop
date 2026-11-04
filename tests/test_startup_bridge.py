import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from benchmarks.startup import client
from benchmarks.startup.backend import FixedStartupBackend
from benchmarks.startup.evaluate import read_request
from benchmarks.startup.study_bridge import StartupBridge
from research_loop.harness import HarnessConfig
from research_loop import submission, telemetry


class StartupBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()

    def test_one_callback_restores_trusted_receipt_after_agent_overwrite(self):
        receipt = {
            "status": "completed",
            "completed_steps": 3,
            "synthetic": True,
            "device": "cuda:0",
            "seed": 43,
            "loss": 0.5,
            "telemetry_events": [
                telemetry.emit(
                    "train/loss",
                    0.5,
                    path=self.root / "events.jsonl",
                    step=3,
                    tags=("dashboard", "training"),
                )
            ],
        }
        callback = Mock(return_value=receipt)
        backend = FixedStartupBackend(
            run_id="synthetic-unique-run",
            state_dir=self.root / "backend",
            source_identity={
                "baseline_commit": "synthetic-commit",
                "source_tree": "synthetic-source-tree",
            },
            prepare_remote=Mock(
                return_value={"status": "prepared", "run_id": "synthetic-unique-run"}
            ),
            run_gpu=callback,
        )
        backend.setup()
        bridge = StartupBridge(
            config=HarnessConfig(timeout_seconds=5),
            backend=backend,
            study_root=self.root / "study",
        )

        def delegate(**kwargs):
            submission.submit(self.workspace, "research")
            result = self.workspace / submission.RESULT
            deadline = time.monotonic() + 3
            while not result.exists():
                if time.monotonic() >= deadline:
                    self.fail("Trusted callback did not publish its result")
                time.sleep(0.01)
            self.assertEqual(
                submission.submit(self.workspace, "research")["status"], "completed"
            )
            result.write_text('{"status": "completed", "loss": -1000}')
            return {"markdown": "Synthetic delegate report."}

        bridge.delegate = SimpleNamespace(run=delegate)
        result = bridge.run(
            workspace=self.workspace,
            run_dir=self.run_dir,
            writable=True,
            prompt="Synthetic diagnostic",
            schema={},
        )
        callback.assert_called_once_with()
        self.assertEqual(backend.inspect(), receipt)
        with self.assertRaisesRegex(RuntimeError, "identity already retained"):
            backend.start(backend.prepare())
        callback.assert_called_once_with()
        self.assertEqual(bridge.receipt, receipt)
        self.assertEqual(bridge.bridge_errors, [])
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(result["measurements"], [])
        self.assertEqual(
            result["artifacts"],
            [
                {
                    "path": str(self.workspace.resolve() / submission.RESULT),
                    "version": "synthetic-unique-run",
                }
            ],
        )
        self.assertEqual(
            json.loads((self.workspace / submission.RESULT).read_text()), receipt
        )

    def test_malformed_and_boolean_seed_requests_never_call_gpu(self):
        for index, raw in enumerate(
            (
                "{",
                '{"seed": true}',
                '{"seed": 43.0}',
                '{"seed": 42}',
                '{"seed":43,"command":"forbidden"}',
            )
        ):
            with self.subTest(request=raw):
                workspace = self.root / f"invalid-{index}"
                workspace.mkdir()
                (workspace / submission.REQUEST).write_text(raw)
                callback = Mock()
                backend = FixedStartupBackend(
                    run_id="synthetic-reject",
                    state_dir=workspace / "backend",
                    source_identity={
                        "baseline_commit": "synthetic",
                        "source_tree": "synthetic",
                    },
                    prepare_remote=Mock(),
                    run_gpu=callback,
                )
                bridge = StartupBridge(
                    config=HarnessConfig(),
                    backend=backend,
                    study_root=self.root / "study",
                )
                bridge._service(workspace)
                callback.assert_not_called()
                self.assertTrue(bridge.started)
                self.assertIsNone(bridge.receipt)
                self.assertTrue(bridge.bridge_errors)
                self.assertEqual(
                    json.loads((workspace / submission.RESULT).read_text())["status"],
                    "error",
                )

    def test_empty_client_goal_rejected_before_request_publication(self):
        goal = self.workspace / "goal.md"
        previous = Path.cwd()
        os.chdir(self.workspace)
        self.addCleanup(os.chdir, previous)
        for text in ("", " \n\t"):
            with self.subTest(goal=text):
                goal.write_text(text)
                with (
                    patch("sys.argv", ["client.py", "launch", "--goal", "goal.md"]),
                    self.assertRaisesRegex(ValueError, "empty"),
                ):
                    client.main()
                self.assertFalse(Path("launch-request.json").exists())
                self.assertFalse(Path("launch-request.json.tmp").exists())

    def test_launch_request_rejects_arbitrary_commands_and_extra_fields(self):
        valid = {
            "goal_text": "Synthetic diagnostic only",
            "task": None,
            "selection_policy": None,
        }
        path = self.workspace / "launch-request.json"
        path.write_text(json.dumps(valid))
        self.assertEqual(read_request(path), valid)
        for request in (
            {**valid, "command": "forbidden"},
            {**valid, "extra": True},
            {**valid, "task": {"id": "smoke", "command": "forbidden"}},
        ):
            with self.subTest(request=request):
                path.write_text(json.dumps(request))
                with self.assertRaisesRegex(ValueError, "fields"):
                    read_request(path)

    def test_backend_prepare_is_pure_and_unknown_effects_never_restart(self):
        prepare_remote = Mock(
            return_value={"status": "prepared", "run_id": "synthetic-retained"}
        )
        execution = self.root / "backend/execution.json"

        def unknown_effects():
            retained = json.loads(execution.read_text())
            self.assertEqual(retained["status"], "accepted")
            self.assertEqual(retained["execution_identity"], "synthetic-retained")
            raise TimeoutError("Synthetic unknown remote effects")

        callback = Mock(side_effect=unknown_effects)
        backend = FixedStartupBackend(
            run_id="synthetic-retained",
            state_dir=self.root / "backend",
            source_identity={"baseline_commit": "synthetic", "source_tree": "source"},
            prepare_remote=prepare_remote,
            run_gpu=callback,
        )
        with self.assertRaises(FileNotFoundError):
            backend.prepare()
        prepare_remote.assert_not_called()
        backend.setup()
        backend.setup()
        prepare_remote.assert_called_once_with()
        prepared = backend.prepare()
        self.assertEqual(backend.prepare(), prepared)
        callback.assert_not_called()
        with self.assertRaisesRegex(TimeoutError, "unknown remote effects"):
            backend.start(prepared)
        self.assertEqual(backend.inspect()["status"], "accepted")
        with self.assertRaisesRegex(RuntimeError, "identity already retained"):
            backend.start(prepared)
        callback.assert_called_once_with()

    def test_client_prepare_uses_explicit_setup_operation(self):
        with (
            patch("sys.argv", ["client.py", "prepare"]),
            patch.object(
                client, "exchange", return_value={"status": "prepared"}
            ) as exchange,
        ):
            self.assertEqual(client.main(), 0)
        exchange.assert_called_once_with("setup", {}, 120)


if __name__ == "__main__":
    unittest.main()
