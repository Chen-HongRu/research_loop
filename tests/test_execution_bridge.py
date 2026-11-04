"""Exercise the unified launcher with a real Study and a local trusted backend."""

import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from research_loop import Study, StudyError, initialize
from research_loop.cli import main
from research_loop.execution_bridge import initialize_workspace, load_execution_harness
from research_loop.harness import HarnessError, completed_response
from research_loop.runtime.context import ContextPackage
from research_loop.runtime.format import RuntimeFormatError
from research_loop.runtime.store import StateStore
from research_loop.submission import LOCK, RECEIPT, status, submit
from test_core import RecordedHarness, evaluation, read

ADAPTER = """
import json
from pathlib import Path
from research_loop.correction import ParameterError
from research_loop.harness import HarnessError
EVENTS = []
class Controller:
    def admit_iteration(self, iteration):
        EVENTS.append(('admit', iteration))
    def diagnostic_context(self, iteration):
        return {'synthetic': iteration}
    def validate_completed(self, response, *, workspace, run_dir):
        return response
    def close(self):
        EVENTS.append(('close',))
def create_controller(config, *, study_root):
    return Controller()
def create_backend(config, *, study_root, workspace, run_dir, context, stop, deadline):
    class Backend:
        def prepare(self):
            source = workspace / 'research/run.py'
            if not source.is_file():
                raise ParameterError('Missing authored research/run.py')
            try:
                compile(source.read_text(), str(source), 'exec')
            except SyntaxError as error:
                raise ParameterError('Authored script has invalid syntax') from error
            EVENTS.append(('prepare',))
            return source.read_text()
        def start(self, prepared):
            if stop.is_set():
                raise HarnessError('Backend observed stop before allocation')
            EVENTS.append(('start',))
            if config.get('wait_for_stop'):
                if not stop.wait(3):
                    raise HarnessError('Synthetic backend did not receive stop')
                EVENTS.append(('stopped',))
                raise HarnessError('Stopped owned synthetic job; no terminal accounting')
            if config.get('unknown'):
                raise HarnessError('External submission effects unknown')
            if (workspace / 'research/run.py').read_text() != prepared:
                raise HarnessError('Prepared source changed')
            artifact = {'path': str(workspace / 'trusted-result.json'), 'version': 'synthetic-v1'}
            Path(artifact['path']).write_text('{"quality":0.75}')
            result = {
                'outcome': 'completed', 'markdown': 'Trusted synthetic receipt; no real allocation.',
                'artifacts': [artifact], 'measurements': [{
                    'candidate_id': context['experiment']['id'] + ':quality-v1',
                    'artifact': artifact, 'evaluation_id': 'quality-v1',
                    'comparison_scope': 'fixed-split-v1',
                    'metrics': [{'name':'quality','value':0.75}, {'name':'cost','value':1}],
                    'evidence': [str(workspace / 'trusted-result.json')],
                }],
            }
            (run_dir / 'synthetic-receipt.json').write_text(json.dumps(result))
            return result
        def inspect(self):
            EVENTS.append(('inspect',))
            receipt = run_dir / 'synthetic-receipt.json'
            if config.get('unknown') or not receipt.is_file():
                raise HarnessError('Retained effects are not terminal')
            return json.loads(receipt.read_text())
    return Backend()
"""


class SubmissionAgent(RecordedHarness):
    qualitative = False
    repair = False

    def run(self, **kwargs):
        context = read(kwargs["run_dir"] / "context.json")
        if (
            "plan" in context
            and context["plan"].get("execution") is not None
            and set(kwargs["schema"]["properties"]) == {"outcome", "markdown"}
        ):
            workspace = Path(kwargs["workspace"])
            source = workspace / "research/run.py"
            source.parent.mkdir(exist_ok=True)
            source.write_text("invalid !!!" if self.repair else "value = 1\n")
            submit(workspace, "research")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current = status(workspace)
                if current["status"] == "parameter_error":
                    source.write_text("value = 1\n")
                    submit(workspace, "research")
                elif current["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.01)
            else:
                raise HarnessError("Synthetic Agent did not observe terminal result")
        return super().run(**kwargs)

    def response(self, phase, context, kwargs):
        result = super().response(phase, context, kwargs)
        if phase == "plan":
            result["execution"] = (
                None
                if self.qualitative
                else {
                    "mode": "script",
                    "entrypoint": "research/run.py",
                    "gpu": False,
                    "time_limit_seconds": 60,
                    "inputs": [],
                    "outputs": ["trusted-result.json"],
                }
            )
        if phase == "execute":
            result["markdown"] = (
                "Untrusted Agent claims quality=9999; inspect actual receipts."
            )
        return result


class ExecutionBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve()
        self.root = self.folder / "study"
        initialize(
            self.root,
            goal="Measure a bounded synthetic intervention.",
            allow_write=True,
            research_execution={"version": 1},
            max_iterations=4,
            max_calls=30,
            max_seconds=60,
            global_evaluation=evaluation("quality-v1"),
            strategy={"selection": "direct", "review": "full"},
        )
        self.adapter = self.folder / "adapter.py"
        self.adapter.write_text(ADAPTER)
        self.config = self.folder / "backend.json"
        self.config.write_text("{}")
        self.delegate = SubmissionAgent()
        self.harness = load_execution_harness(
            self.root, self.delegate, backend=self.adapter, backend_config=self.config
        )
        self.study = Study(self.root, self.harness)

    def test_trusted_result_reaches_independent_review_with_raw_agent_retained(self):
        state = self.study.run()
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(self.harness.module.EVENTS.count(("start",)), 1)
        attempt = self.root / state["calls"]["execute"]
        raw = read(attempt / "agent/response.json")
        self.assertEqual(set(raw), {"outcome", "markdown"})
        self.assertIn("9999", raw["markdown"])
        response = read(attempt / "response.json")
        self.assertEqual(response["measurements"][0]["metrics"][0]["value"], 0.75)
        self.assertNotIn("9999", response["markdown"])
        review = next(call for call in self.delegate.calls if call["phase"] == "review")
        self.assertEqual(review["context"]["execute"], response)
        self.assertFalse(review["writable"])
        self.assertEqual(completed_response(attempt), response)
        self.assertTrue((self.root / "execution-backend.json").is_file())

    def test_outer_context_manifests_survive_the_bridge(self):
        state = self.study.run()
        for reference in state["calls"].values():
            attempt = self.root / reference
            package = ContextPackage.from_dict(
                {
                    "context": read(attempt / "context.json"),
                    "manifest": read(attempt / "context-manifest.json"),
                }
            )
            self.assertEqual(package.context, read(attempt / "context.json"))
            self.assertEqual(package.manifest["study_id"], str(self.root))

    def test_sqlite_study_rejected_before_adapter_loading_or_workspace_cleanup(self):
        root = self.folder / "sqlite-study"
        StateStore.create(root, config={"goal": "Synthetic SQLite boundary"})
        with patch(
            "research_loop.execution_bridge.importlib.util.spec_from_file_location"
        ) as load:
            with self.assertRaises(RuntimeFormatError):
                load_execution_harness(
                    root,
                    self.delegate,
                    backend=self.adapter,
                    backend_config=self.config,
                )
            with self.assertRaises(RuntimeFormatError):
                initialize_workspace(root, self.root / "workspace", "0001")
        load.assert_not_called()
        self.assertFalse((root / "execution-backend.json").exists())
        self.assertFalse((root / "iterations").exists())

    def test_qualitative_execute_rejects_agent_artifacts(self):
        self.delegate.qualitative = True
        original = self.delegate.response

        def response(phase, context, kwargs):
            result = original(phase, context, kwargs)
            if phase == "execute":
                result["artifacts"] = [
                    {"path": "untrusted-result", "version": "untrusted-v1"}
                ]
            return result

        self.delegate.response = response
        with self.assertRaisesRegex(HarnessError, "Qualitative Execute"):
            self.study.run()
        self.assertNotIn(("start",), self.harness.module.EVENTS)
        self.assertEqual(self.delegate.count("review"), 0)

    def test_live_parameter_correction_starts_only_once(self):
        self.delegate.repair = True
        self.study.run()
        self.assertEqual(self.harness.module.EVENTS.count(("start",)), 1)
        self.assertEqual(self.delegate.count("execute"), 1)
        workspace = Path(
            read(self.root / "iterations/0001/iteration.json")["workspace"]
        )
        self.assertIn(
            "invalid syntax",
            json.dumps(read(workspace / "evaluation-parameter-errors.json")),
        )

    def test_unknown_effects_block_review_and_no_second_start(self):
        self.config.write_text('{"unknown": true}')
        self.harness = load_execution_harness(
            self.root, self.delegate, backend=self.adapter, backend_config=self.config
        )
        self.study = Study(self.root, self.harness)
        with self.assertRaisesRegex(HarnessError, "effects unknown"):
            self.study.run()
        self.assertEqual(self.harness.module.EVENTS.count(("start",)), 1)
        self.assertEqual(self.delegate.count("review"), 0)
        state = self.study.status()
        attempt = self.root / state["calls"]["execute"]
        self.assertIsNone(completed_response(attempt))
        self.assertTrue(
            (
                Path(read(self.root / "iterations/0001/iteration.json")["workspace"])
                / RECEIPT
            ).is_file()
        )
        with self.assertRaises(StudyError):
            self.study.run()
        with self.assertRaises(StudyError):
            self.study.retry()
        self.assertEqual(self.harness.module.EVENTS.count(("start",)), 1)

    def test_factory_failure_reaches_agent_and_blocks_completion(self):
        with patch.object(
            self.harness.module,
            "create_backend",
            side_effect=HarnessError("Backend binding failed"),
        ):
            with self.assertRaisesRegex(HarnessError, "Backend binding failed"):
                self.study.run()
        self.assertEqual(self.delegate.count("review"), 0)
        self.assertNotIn(("start",), self.harness.module.EVENTS)
        state = self.study.status()
        self.assertIsNone(completed_response(self.root / state["calls"]["execute"]))

    def test_agent_interrupt_signals_backend_and_never_publishes_completion(self):
        self.config.write_text('{"wait_for_stop":true}')
        self.harness = load_execution_harness(
            self.root, self.delegate, backend=self.adapter, backend_config=self.config
        )
        self.study = Study(self.root, self.harness)
        original = self.delegate.run

        def interrupt(**kwargs):
            if set(kwargs["schema"]["properties"]) == {"outcome", "markdown"}:
                workspace = Path(kwargs["workspace"])
                source = workspace / "research/run.py"
                source.parent.mkdir(exist_ok=True)
                source.write_text("value = 1\n")
                submit(workspace, "research")
                deadline = time.monotonic() + 3
                while (
                    "start",
                ) not in self.harness.module.EVENTS and time.monotonic() < deadline:
                    time.sleep(0.01)
                raise KeyboardInterrupt()
            return original(**kwargs)

        self.delegate.run = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.study.run()
        self.assertIn(("stopped",), self.harness.module.EVENTS)
        self.assertEqual(self.harness.module.EVENTS.count(("start",)), 1)
        state = self.study.status()
        self.assertIsNone(completed_response(self.root / state["calls"]["execute"]))
        self.assertEqual(self.delegate.count("review"), 0)

    def test_qualitative_execute_is_readonly_and_never_creates_backend(self):
        self.delegate.qualitative = True
        with patch.object(
            self.harness.module,
            "create_backend",
            side_effect=AssertionError("No backend for qualitative evidence"),
        ):
            state = self.study.run()
        execute = next(
            call for call in self.delegate.calls if call["phase"] == "execute"
        )
        self.assertFalse(execute["writable"])
        self.assertEqual(read(self.root / state["execution_ref"])["measurements"], [])
        self.assertNotIn(("start",), self.harness.module.EVENTS)
        review = next(call for call in self.delegate.calls if call["phase"] == "review")
        self.assertNotIn("This is a qualitative, read-only Execute", review["prompt"])

    def test_missing_execution_contract_is_not_silently_qualitative(self):
        root = self.folder / "generic-study"
        initialize(root, goal="Synthetic generic Plan", max_seconds=60)
        delegate = RecordedHarness()
        harness = load_execution_harness(
            root, delegate, backend=self.adapter, backend_config=self.config
        )
        with self.assertRaisesRegex(HarnessError, "explicit Plan.execution contract"):
            Study(root, harness).run()
        self.assertNotIn(("start",), harness.module.EVENTS)
        self.assertEqual(delegate.count("execute"), 0)
        self.assertEqual(delegate.count("review"), 0)

    def test_bare_harness_cannot_authorize_qualitative_null_plan(self):
        self.delegate.qualitative = True
        config = read(self.root / "study.json")
        config["max_calls"] = 4
        (self.root / "study.json").write_text(json.dumps(config))
        state = Study(self.root, self.delegate).run()
        self.assertEqual(state["stop_reason"], "call_budget")
        self.assertEqual(self.delegate.count("execute"), 0)

    def test_cached_execute_inspects_without_start_and_rejects_tampered_receipt(self):
        self.study.run()
        state = self.study.status()
        attempt = self.root / state["calls"]["execute"]
        workspace = Path(read(attempt / "context.json")["workspace"])
        result = read(attempt / "response.json")
        self.assertEqual(
            self.harness.validate_completed(
                result, workspace=workspace, run_dir=attempt
            ),
            result,
        )
        self.assertEqual(self.harness.module.EVENTS.count(("start",)), 1)
        changed = {**result, "markdown": "tampered"}
        with self.assertRaisesRegex(HarnessError, "trusted backend result"):
            self.harness.validate_completed(
                changed, workspace=workspace, run_dir=attempt
            )

    def test_registered_backend_is_loaded_on_rerun_and_config_change_blocks(self):
        self.study.run()
        loaded = load_execution_harness(self.root, self.delegate)
        self.assertEqual(loaded.binding, self.harness.binding)
        self.config.write_text('{"changed": true}')
        with self.assertRaisesRegex(HarnessError, "configuration changed"):
            load_execution_harness(self.root, self.delegate)
        with self.assertRaisesRegex(HarnessError, "supplied together"):
            load_execution_harness(self.root, self.delegate, backend=self.adapter)
        with self.assertRaisesRegex(HarnessError, "absolute"):
            load_execution_harness(
                self.root,
                self.delegate,
                backend=Path("relative.py"),
                backend_config=self.config,
            )

    def test_next_iteration_retires_only_inherited_interfaces_once(self):
        self.study.run()
        self.delegate.base = "0001"
        self.study.resume(reason="Run another independently bounded candidate")
        self.study.run()
        self.assertEqual(self.harness.module.EVENTS.count(("start",)), 2)
        state = self.study.status()
        metadata = read(self.root / "iterations/0002/iteration.json")
        workspace = Path(metadata["workspace"])
        before = (workspace / RECEIPT).read_bytes()
        lock_inode = (workspace / LOCK).stat().st_ino
        initialize_workspace(self.root, workspace, "0002")
        self.assertEqual((workspace / RECEIPT).read_bytes(), before)
        self.assertEqual((workspace / LOCK).stat().st_ino, lock_inode)
        self.assertTrue(
            read(self.root / "iterations/0002/submission-workspace.json")["inherited"]
        )
        self.assertEqual(state["iteration"], 2)

    def test_untracked_or_modified_interface_is_never_deleted(self):
        workspace = self.root / "workspace"
        path = workspace / RECEIPT
        path.write_text('{"action":"research"}')
        with self.assertRaisesRegex(HarnessError, "Unreconciled"):
            initialize_workspace(self.root, workspace, "0001")
        self.assertTrue(path.is_file())
        subprocess.run(
            ["git", "-C", str(workspace), "add", RECEIPT],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=t@x",
                "commit",
                "-m",
                "interface",
            ],
            check=True,
            capture_output=True,
        )
        path.write_text('{"action":"evaluate"}')
        with self.assertRaisesRegex(HarnessError, "Unreconciled"):
            initialize_workspace(self.root, workspace, "0001")
        self.assertEqual(read(path), {"action": "evaluate"})

    def test_cleanup_runs_while_owning_study_lock_and_preserves_original_error(self):
        checked = []

        def close():
            with self.assertRaisesRegex(StudyError, "Another controller"):
                with self.study._lock():
                    self.fail("Cleanup must retain controller ownership")
            checked.append(True)

        self.harness.controller.close = close
        self.study.run()
        self.assertEqual(checked, [True])

    def test_cleanup_error_does_not_replace_original_agent_failure(self):
        def fail(call):
            raise HarnessError("primary Agent failure")

        self.delegate.before = fail
        self.harness.controller.close = lambda: (_ for _ in ()).throw(
            HarnessError("cleanup failure")
        )
        with self.assertRaisesRegex(HarnessError, "primary Agent failure") as raised:
            self.study.run()
        self.assertIn("cleanup failure", str(raised.exception.__cause__))

    def test_duplicate_cli_cannot_close_another_controllers_jobs(self):
        with (
            self.study._lock(),
            patch("research_loop.core.make_harness", return_value=self.delegate),
            patch(
                "research_loop.cli.load_execution_harness", return_value=self.harness
            ),
        ):
            self.assertEqual(
                main(
                    [
                        "run",
                        str(self.root),
                        "--backend",
                        str(self.adapter),
                        "--backend-config",
                        str(self.config),
                    ]
                ),
                1,
            )
        self.assertNotIn(("close",), self.harness.module.EVENTS)
        self.assertNotIn(("start",), self.harness.module.EVENTS)

    def test_cli_loads_backend_and_closes_controller(self):
        with (
            patch("research_loop.core.make_harness", return_value=self.delegate),
            patch(
                "research_loop.cli.load_execution_harness", return_value=self.harness
            ),
        ):
            self.assertEqual(
                main(
                    [
                        "run",
                        str(self.root),
                        "--iterations",
                        "1",
                        "--backend",
                        str(self.adapter),
                        "--backend-config",
                        str(self.config),
                    ]
                ),
                0,
            )
        self.assertIn(("close",), self.harness.module.EVENTS)


if __name__ == "__main__":
    unittest.main()
