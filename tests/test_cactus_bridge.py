import json
from pathlib import Path
import tempfile
import time
from unittest.mock import Mock, patch
import unittest

from benchmarks.cactus.bridge import CactusBridge, REPORT_SCHEMA, parse_request
from research_loop.harness import completed_response


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.request = {
            "job_id": "tiny-001",
            "epochs": 1,
            "lr": 0.001,
            "batch_size": 128,
            "seed": 0,
        }

    def test_valid_configuration_is_copied(self):
        parsed = parse_request(self.request)
        self.assertEqual(parsed, self.request)
        self.assertIsNot(parsed, self.request)

    def test_rejects_injection_unknown_fields_and_invalid_ranges(self):
        for field, value in [
            ("job_id", "../outside"),
            ("job_id", "x;touch pwned"),
            ("job_id", "$(whoami)"),
            ("epochs", 0),
            ("epochs", 6),
            ("epochs", True),
            ("batch_size", 31),
            ("batch_size", 513),
            ("batch_size", 128.0),
            ("seed", -1),
            ("seed", True),
            ("lr", float("nan")),
            ("lr", float("inf")),
            ("lr", -0.001),
            ("lr", 0),
            ("lr", 0.11),
            ("lr", True),
            ("command", "whoami"),
        ]:
            request = {**self.request, field: value}
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                parse_request(request)
        del self.request["seed"]
        with self.assertRaises(ValueError):
            parse_request(self.request)


class ExecutionContractTests(unittest.TestCase):
    def test_trusted_result_owns_measurement_and_retains_raw_author_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = CactusBridge.__new__(CactusBridge)
            bridge.deadline = time.monotonic() + 2
            bridge.bridge_errors = []
            artifact = {
                "path": str(root / "checkpoint.pt"),
                "version": "trusted-version",
            }
            bridge.jobs = {
                "job": {
                    "status": "completed",
                    "job_id": "job",
                    "artifact": artifact,
                    "score": 0.75,
                    "evidence": [str(root / "dev-score.json")],
                }
            }
            bridge._watch = lambda workspace, stop: stop.wait()
            raw = {
                "markdown": "Observed development evidence.",
            }

            def delegate(**kwargs):
                self.assertEqual(kwargs["schema"], REPORT_SCHEMA)
                (root / "completed.json").write_text(json.dumps({"response": raw}))
                return raw

            bridge.delegate = Mock()
            bridge.delegate.run.side_effect = delegate
            result = bridge.run(
                prompt="execute",
                schema={},
                workspace=root,
                run_dir=root,
                writable=True,
                timeout_seconds=1,
            )
            self.assertEqual(result["outcome"], "completed")
            self.assertEqual(result["artifacts"], [artifact])
            self.assertEqual(
                result["measurements"][0]["metrics"], [{"name": "auc", "value": 0.75}]
            )
            self.assertEqual(completed_response(root), result)
            self.assertEqual(json.loads((root / "agent-report.json").read_text()), raw)
            self.assertEqual(
                json.loads((root / "agent-completed.json").read_text()),
                {"response": raw},
            )
            self.assertEqual(set(raw), {"markdown"})
            bridge.delegate.run.assert_called_once()

    def test_no_job_cannot_publish_author_claimed_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = CactusBridge.__new__(CactusBridge)
            bridge.deadline = time.monotonic() + 2
            bridge.bridge_errors, bridge.jobs = [], {}
            bridge._watch = lambda workspace, stop: stop.wait()
            bridge.delegate = Mock()
            bridge.delegate.run.return_value = {
                "markdown": "claim",
            }
            result = bridge.run(
                prompt="execute", workspace=root, run_dir=root, writable=True
            )
            self.assertEqual(result["outcome"], "incomplete")
            self.assertEqual(result["measurements"], [])
            self.assertEqual(result["artifacts"], [])

    def test_worker_crash_blocks_retained_completed_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = CactusBridge.__new__(CactusBridge)
            bridge.deadline = time.monotonic() + 2
            bridge.bridge_errors = []
            bridge.jobs = {"retained": {"status": "completed"}}

            def watch(workspace, stop):
                stop.wait()
                raise RuntimeError("unexpected worker failure")

            bridge._watch = watch
            bridge.delegate = Mock()
            bridge.delegate.run.return_value = {
                "markdown": "report",
            }
            with self.assertRaisesRegex(Exception, "effects require reconciliation"):
                bridge.run(
                    prompt="execute", workspace=root, run_dir=root, writable=True
                )
            self.assertIsNone(completed_response(root))
            bridge.delegate.run.assert_called_once()

    def test_live_worker_or_remote_error_blocks_completion(self):
        for alive, errors in [(True, []), (False, ["remote effects unknown"])]:
            with (
                self.subTest(alive=alive, errors=errors),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                bridge = CactusBridge.__new__(CactusBridge)
                bridge.deadline = time.monotonic() + 1
                bridge.bridge_errors = list(errors)
                bridge.jobs = {"retained": {"status": "completed"}}
                bridge.delegate = Mock()
                bridge.delegate.run.return_value = {"markdown": "report"}
                worker = Mock()
                worker.is_alive.return_value = alive
                with patch(
                    "benchmarks.cactus.bridge.threading.Thread", return_value=worker
                ):
                    with self.assertRaisesRegex(Exception, "reconciliation required"):
                        bridge.run(
                            prompt="execute",
                            workspace=root,
                            run_dir=root,
                            writable=True,
                        )
                self.assertIsNone(completed_response(root))
                bridge.delegate.run.assert_called_once()
