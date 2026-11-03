import json
import os
import tempfile
import unittest
from pathlib import Path

from research_loop import Study, StudyError, initialize
from research_loop.harness import HarnessError
from test_core import ProcessLost, RecordedHarness, read


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "study"

    def study(self, **options):
        initialize(
            self.root,
            goal="Retain bounded evidence across interruption.",
            strategy={"selection": "direct", "review": "full"},
            **options,
        )
        harness = RecordedHarness()
        return Study(self.root, harness), harness

    def interrupt_partial(self, harness, phase, *, pid=None, interrupt=ProcessLost):
        def fail(call):
            if call["phase"] != phase:
                return
            # Even a complete parseable response is only partial evidence until
            # the harness durably certifies completion.
            response = harness.response(phase, call["context"], call)
            (call["run_dir"] / "response.json").write_text(json.dumps(response))
            (call["run_dir"] / "process.json").write_text(
                json.dumps({"status": "running", "pid": pid})
            )
            raise interrupt()

        harness.before = fail

    def test_partial_read_only_response_is_retained_and_restarted(self):
        study, harness = self.study()
        self.interrupt_partial(harness, "proposal", interrupt=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            study.run()
        attempt = self.root / study.status()["active_attempt"]
        partial = (attempt / "response.json").read_text()
        harness.before = None
        state = Study(self.root, harness).run()
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(harness.count("proposal"), 3)
        self.assertEqual(state["call_count"], 7)
        self.assertEqual((attempt / "response.json").read_text(), partial)
        self.assertTrue((attempt.parent / "attempt-002/process.json").exists())

    def test_completed_receipt_replays_before_final_process_bookkeeping(self):
        study, harness = self.study()

        def crash(call):
            result = read(call["run_dir"] / "response.json")
            (call["run_dir"] / "completed.json").write_text(
                json.dumps({"version": 1, "response": result})
            )
            (call["run_dir"] / "process.json").write_text(
                json.dumps({"status": "running"})
            )
            raise ProcessLost()

        harness.after = crash
        with self.assertRaises(ProcessLost):
            study.run()
        harness.after = None
        self.assertEqual(Study(self.root, harness).run()["status"], "stopped")
        self.assertEqual(harness.count("proposal"), 2)
        self.assertEqual(study.status()["call_count"], 6)

    def test_completed_receipt_survives_keyboard_interrupt_without_new_call(self):
        study, harness = self.study()

        def interrupt(call):
            raise KeyboardInterrupt()

        harness.after = interrupt
        with self.assertRaises(KeyboardInterrupt):
            study.run()
        harness.after = None
        self.assertEqual(study.run()["status"], "stopped")
        self.assertEqual(harness.count("proposal"), 2)

    def test_partial_execute_never_repeats_and_preserves_git_workspace(self):
        study, harness = self.study(allow_write=True)
        self.interrupt_partial(harness, "execute")
        with self.assertRaises(ProcessLost):
            study.run()
        workspace = self.root / "iterations/0001/workspace"
        (workspace / "unreconciled.py").write_text("external_effect = 'unknown'\n")
        attempt = self.root / study.status()["active_attempt"]
        harness.before = None
        with self.assertRaisesRegex(StudyError, "no action was repeated"):
            Study(self.root, harness).run()
        self.assertEqual(harness.count("execute"), 1)
        self.assertTrue((attempt / "response.json").exists())
        self.assertEqual(
            (workspace / "unreconciled.py").read_text(),
            "external_effect = 'unknown'\n",
        )
        self.assertFalse((attempt.parent / "attempt-002").exists())

    def test_live_read_only_cli_blocks_both_resume_and_explicit_retry(self):
        study, harness = self.study()
        self.interrupt_partial(harness, "proposal", pid=os.getpid())
        with self.assertRaises(ProcessLost):
            study.run()
        harness.before = None
        with self.assertRaisesRegex(StudyError, "active CLI"):
            study.run()
        with self.assertRaisesRegex(StudyError, "active CLI"):
            study.retry()
        self.assertEqual(len(harness.calls), 1)

    def test_live_execute_delegate_blocks_resume_and_explicit_retry(self):
        study, harness = self.study(allow_write=True)

        def interrupt(call):
            if call["phase"] == "execute":
                agent = call["run_dir"] / "agent"
                agent.mkdir()
                (agent / "process.json").write_text(
                    json.dumps({"status": "running", "pid": os.getpid()})
                )
                raise ProcessLost()

        harness.before = interrupt
        with self.assertRaises(ProcessLost):
            study.run()
        harness.before = None
        with self.assertRaisesRegex(StudyError, "active CLI"):
            study.run()
        with self.assertRaisesRegex(StudyError, "active CLI"):
            study.retry()
        self.assertEqual(harness.count("execute"), 1)
        self.assertFalse((self.root / "iterations/0001/execute/attempt-002").exists())

    def test_unaccepted_completed_execute_is_revalidated_without_reexecution(self):
        study, harness = self.study(allow_write=True)

        def crash(call):
            if call["phase"] == "execute":
                raise ProcessLost()

        harness.after = crash
        with self.assertRaises(ProcessLost):
            study.run()
        attempt = self.root / study.status()["active_attempt"]
        checked = []

        def reject(result, *, workspace, run_dir):
            checked.append((workspace, run_dir))
            raise HarnessError("Trusted execution evidence has not been reconciled")

        harness.validate_completed = reject
        harness.after = None
        with self.assertRaisesRegex(HarnessError, "not been reconciled"):
            study.run()
        with self.assertRaisesRegex(StudyError, "explicitly retry"):
            study.run()
        self.assertEqual(harness.count("execute"), 1)
        self.assertEqual(harness.count("review"), 0)
        self.assertFalse(study.status()["accepted_calls"].get("execute"))
        self.assertEqual(checked, [(self.root / "iterations/0001/workspace", attempt)])

        def accept(result, *, workspace, run_dir):
            checked.append((workspace, run_dir))
            return result

        harness.validate_completed = accept
        study.retry()
        self.assertEqual(study.run()["status"], "stopped")
        self.assertEqual(harness.count("execute"), 1)
        self.assertEqual(len(checked), 2)
        self.assertTrue(study.status()["accepted_calls"]["execute"])

    def test_readonly_report_rejection_is_corrected_with_retained_receipt(self):
        study, harness = self.study()
        response = harness.response
        first = [True]

        def invalid(phase, context, kwargs):
            result = response(phase, context, kwargs)
            if phase == "proposal" and first:
                first.pop()
                result["markdown"] = ""
            return result

        harness.response = invalid
        self.assertEqual(study.run()["status"], "stopped")
        self.assertEqual(harness.count("proposal"), 3)
        attempt = harness.calls[0]["run_dir"]
        self.assertEqual(read(attempt / "rejection.json")["response"]["markdown"], "")
        self.assertEqual(read(attempt / "response.json")["markdown"], "")
        self.assertIn("Stage report must be nonempty", harness.calls[1]["prompt"])

    def test_restarted_read_only_attempt_obeys_existing_call_budget(self):
        study, harness = self.study(max_calls=1)
        self.interrupt_partial(harness, "proposal")
        with self.assertRaises(ProcessLost):
            study.run()
        harness.before = None
        self.assertEqual(study.run()["stop_reason"], "call_budget")
        self.assertEqual(len(harness.calls), 1)

    def test_restarted_read_only_attempt_obeys_remaining_time_budget(self):
        study, harness = self.study(max_seconds=10)
        self.interrupt_partial(harness, "proposal")
        with self.assertRaises(ProcessLost):
            study.run()
        state = study.status()
        state["elapsed_seconds"] = 10.0
        (self.root / "state.json").write_text(json.dumps(state))
        harness.before = None
        self.assertEqual(study.run()["stop_reason"], "time_budget")
        self.assertEqual(len(harness.calls), 1)


if __name__ == "__main__":
    unittest.main()
