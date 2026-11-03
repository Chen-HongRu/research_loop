import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research_loop import Study, StudyError, initialize
from research_loop import core
from research_loop.harness import completed_response, publish_completed_response
from research_loop.submission import RECEIPT, REQUEST, submit
from test_core import RecordedHarness, read


class ExecutionRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "study"
        initialize(
            self.root,
            goal="Preserve the original experiment across a backend interruption.",
            allow_write=True,
            max_iterations=4,
            strategy={"selection": "direct", "review": "full"},
        )
        self.harness = RecordedHarness()
        self.study = Study(self.root, self.harness)

    def interrupt(self):
        def fail(call):
            if call["phase"] == "execute":
                (call["run_dir"] / "response.json").write_text('{"partial":true}\n')
                raise KeyboardInterrupt()

        self.harness.before = fail
        with self.assertRaises(KeyboardInterrupt):
            self.study.run()
        self.harness.before = None
        return self.study.status()

    def certificate(self, state, **changes):
        result = {
            "source_iteration": f"{state['iteration']:04d}",
            "source_attempt": state["calls"]["execute"],
            "plan_ref": state["plan_ref"],
            "action": "retry",
            "effect": "never_submitted",
            "summary": "Backend positively certifies no external execution was submitted.",
            "proof_refs": ["../control/backend-never-submitted.json"],
        }
        result.update(changes)
        return result

    def apply(self, certificate, identity="retry-0001-attempt-001"):
        return self.study.reconcile_execution(
            recovery_id=identity, certify=lambda state: copy.deepcopy(certificate)
        )

    def test_certified_retry_retires_accepted_interface_and_reaches_review(self):
        before = self.interrupt()
        metadata = read(self.root / "iterations/0001/iteration.json")
        workspace = Path(metadata["workspace"])
        (workspace / RECEIPT).write_text('{"action":"evaluate"}\n')
        original = self.root / before["calls"]["execute"] / "response.json"
        original_bytes = original.read_bytes()
        cert = self.certificate(before, protected={"secret": "DO-NOT-PROJECT"})
        prepared = self.apply(cert)
        self.assertEqual(self.apply(cert), prepared)
        for key in (
            "iteration",
            "plan_ref",
            "selected",
            "call_count",
            "elapsed_seconds",
        ):
            self.assertEqual(prepared[key], before[key])
        self.assertNotIn("execute", prepared["calls"])
        self.assertFalse((workspace / RECEIPT).exists())
        self.assertEqual(submit(workspace)["status"], "pending")
        self.assertTrue((workspace / REQUEST).exists())
        self.assertEqual(original.read_bytes(), original_bytes)
        self.study.run()
        self.assertEqual(self.harness.count("execute"), 2)
        self.assertEqual(self.harness.count("plan"), 1)
        review = next(call for call in self.harness.calls if call["phase"] == "review")
        evidence = review["context"]["execution_recovery"]
        self.assertEqual(
            review["context"]["execution_recovery_current_attempt"],
            self.study.status()["calls"]["execute"],
        )
        self.assertEqual(evidence[0]["source_attempt"], before["calls"]["execute"])
        self.assertTrue((self.root / evidence[0]["interface_retirement_ref"]).is_file())
        self.assertNotIn("DO-NOT-PROJECT", json.dumps(review["context"]))
        self.assertIn(
            "Unknown effects", review["context"]["execution_recovery_guidance"]
        )
        self.assertEqual(self.study.status()["call_count"], before["call_count"] + 2)

    def test_unknown_effects_and_unbound_identities_cannot_authorize_retry(self):
        before = self.interrupt()
        for changes in (
            {"effect": "unknown"},
            {"effect": "terminal"},
            {"source_iteration": "0002"},
            {"source_attempt": "iterations/0001/execute/attempt-999"},
            {"plan_ref": "../outside.json"},
            {"summary": "x" * 4097},
            {"proof_refs": []},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(StudyError):
                    self.apply(self.certificate(before, **changes))
                self.assertEqual(self.study.status(), before)
        with self.assertRaisesRegex(StudyError, "trusted reconciliation"):
            self.study.retry()
        self.assertEqual(self.harness.count("execute"), 1)

    def test_live_agent_blocks_certified_recovery(self):
        before = self.interrupt()
        agent = self.root / before["calls"]["execute"] / "agent"
        agent.mkdir()
        (agent / "process.json").write_text(
            json.dumps({"status": "running", "pid": os.getpid()})
        )
        with self.assertRaisesRegex(StudyError, "active CLI"):
            self.apply(self.certificate(before))
        self.assertFalse(
            (self.root / "recoveries/retry-0001-attempt-001.json").exists()
        )

    def test_retry_replays_receipt_after_crash_before_state_publication(self):
        before = self.interrupt()
        cert = self.certificate(before)
        with patch.object(self.study, "_save", side_effect=OSError("lost controller")):
            with self.assertRaises(OSError):
                self.apply(cert)
        record_path = self.root / "recoveries/retry-0001-attempt-001.json"
        record_bytes = record_path.read_bytes()
        self.assertEqual(self.study.status(), before)
        prepared = self.apply(cert)
        self.assertEqual(prepared["status"], "ready")
        self.assertEqual(record_path.read_bytes(), record_bytes)
        self.assertEqual(self.harness.count("execute"), 1)
        with self.assertRaisesRegex(StudyError, "Immutable"):
            self.apply({**cert, "summary": "changed"})

    def test_live_agent_blocks_replay_before_certifier_side_effects(self):
        before = self.interrupt()
        cert = self.certificate(before)
        with patch.object(
            self.study, "_apply_execution_recovery", side_effect=OSError("crash")
        ):
            with self.assertRaises(OSError):
                self.apply(cert)
        attempt = self.root / cert["source_attempt"]
        (attempt / "process.json").write_text(
            json.dumps({"status": "running", "pid": os.getpid()})
        )
        certified = []
        with self.assertRaisesRegex(StudyError, "active CLI"):
            self.study.reconcile_execution(
                recovery_id="retry-0001-attempt-001",
                certify=lambda state: certified.append(state) or cert,
            )
        self.assertEqual(certified, [])
        self.assertEqual(self.study.status(), before)

    def test_missing_controller_receipt_blocks_new_execute(self):
        before = self.interrupt()
        self.apply(self.certificate(before))
        (self.root / "recoveries/retry-0001-attempt-001.json").unlink()
        with self.assertRaisesRegex(StudyError, "controller recovery evidence"):
            self.study.run()
        self.assertEqual(self.harness.count("execute"), 1)

    def test_retry_preserves_original_deadline_and_refuses_expiration(self):
        before = self.interrupt()
        original_budget = (
            self.root / before["calls"]["execute"] / "parameter-budget.json"
        )
        deadline = read(original_budget)["deadline"]
        prepared = self.apply(self.certificate(before))
        self.assertEqual(
            prepared["execution_recovery_ref"], "recoveries/retry-0001-attempt-001.json"
        )
        with patch.object(core.time, "time", return_value=deadline + 1):
            self.assertEqual(self.study.run()["stop_reason"], "time_budget")
        self.assertEqual(self.harness.count("execute"), 1)
        self.assertFalse((original_budget.parent.parent / "attempt-002").exists())

    def test_retry_new_attempt_budget_is_the_original_deadline(self):
        before = self.interrupt()
        deadline = read(
            self.root / before["calls"]["execute"] / "parameter-budget.json"
        )["deadline"]
        self.apply(self.certificate(before))
        self.study.run()
        retried = [call for call in self.harness.calls if call["phase"] == "execute"][
            -1
        ]
        self.assertEqual(
            read(retried["run_dir"] / "parameter-budget.json")["deadline"], deadline
        )

    def test_retry_requires_retained_deadline(self):
        before = self.interrupt()
        (self.root / before["calls"]["execute"] / "parameter-budget.json").unlink()
        with self.assertRaisesRegex(StudyError, "original Execute deadline"):
            self.apply(self.certificate(before))
        self.assertEqual(self.study.status(), before)

    def test_settlement_archive_recovers_from_interrupted_atomic_write(self):
        before = self.interrupt()
        attempt = self.root / before["calls"]["execute"]
        original = (attempt / "response.json").read_bytes()
        result = {
            "outcome": "incomplete",
            "markdown": "Retained backend settlement",
            "artifacts": [],
            "measurements": [],
        }
        cert = self.certificate(before, action="settle", response=result)
        write = Path.write_bytes
        failed = False

        def interrupt(path, data):
            nonlocal failed
            if path.name == "response.json.tmp" and not failed:
                failed = True
                write(path, data[:3])
                raise OSError("interrupted archive write")
            return write(path, data)

        with patch.object(Path, "write_bytes", interrupt):
            with self.assertRaises(OSError):
                self.apply(cert, "settle-atomic")
        self.apply(cert, "settle-atomic")
        self.assertEqual(
            (
                self.root / "recoveries/settle-atomic.previous/response.json"
            ).read_bytes(),
            original,
        )
        self.assertEqual(completed_response(attempt), result)

    def test_settlement_preserves_original_output_and_uses_no_new_execute(self):
        before = self.interrupt()
        attempt = self.root / before["calls"]["execute"]
        rejected = {
            "outcome": "incomplete",
            "markdown": "Rejected Agent claim",
            "artifacts": [],
            "measurements": [],
        }
        publish_completed_response(attempt, rejected)
        previous = {
            name: (attempt / name).read_bytes()
            for name in ("response.json", "completed.json")
        }
        result = {
            **rejected,
            "markdown": "Backend settled the retained failure with no measurements.",
        }
        cert = self.certificate(
            before, action="settle", effect="terminal", response=result
        )
        with patch.object(
            core, "publish_completed_response", side_effect=OSError("crash")
        ):
            with self.assertRaises(OSError):
                self.apply(cert, "settle-0001")
        prepared = self.apply(cert, "settle-0001")
        self.assertEqual(prepared["call_count"], before["call_count"])
        self.assertEqual(completed_response(attempt), result)
        for name, value in previous.items():
            self.assertEqual(
                (self.root / "recoveries/settle-0001.previous" / name).read_bytes(),
                value,
            )
        self.study.run()
        self.assertEqual(self.harness.count("execute"), 1)
        self.assertEqual(self.harness.count("review"), 1)
        review = self.harness.calls[-1]["context"]
        self.assertEqual(review["execute"], result)
        self.assertEqual(review["execution_recovery"][0]["action"], "settle")

    def test_settlement_of_never_submitted_effects_does_not_grant_retry(self):
        before = self.interrupt()
        result = {
            "outcome": "incomplete",
            "markdown": "No job was started.",
            "artifacts": [],
            "measurements": [],
        }
        prepared = self.apply(
            self.certificate(before, action="settle", response=result)
        )
        self.assertEqual(prepared["calls"]["execute"], before["calls"]["execute"])
        self.study.run()
        self.assertEqual(self.harness.count("execute"), 1)

    def test_plan_content_cannot_change_before_or_after_recovery(self):
        before = self.interrupt()
        cert = self.certificate(before)
        plan_path = self.root / before["plan_ref"]
        raw_path = plan_path.parent / "response.json"
        original_plan = plan_path.read_bytes()
        changed = read(plan_path)
        changed["markdown"] = "Different experiment"
        plan_path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(StudyError, "accepted content"):
            self.apply(cert)
        plan_path.write_bytes(original_plan)
        self.apply(cert)
        raw = read(raw_path)
        raw["markdown"] = "Mutated both accepted evidence and Plan"
        raw_path.write_text(json.dumps(raw))
        publish_completed_response(plan_path.parent, raw)
        plan_path.write_text(json.dumps({**raw, "base_experiment": None}))
        with self.assertRaisesRegex(StudyError, "Immutable"):
            self.apply(cert)

    def test_mutated_plan_after_certification_blocks_normal_run(self):
        before = self.interrupt()
        self.apply(self.certificate(before))
        plan_path = self.root / before["plan_ref"]
        plan = read(plan_path)
        plan["markdown"] = "Changed experiment after certification"
        plan_path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(StudyError, "accepted content"):
            self.study.run()
        self.assertEqual(self.harness.count("execute"), 1)

    def test_certificate_runs_with_study_lock_and_verified_noop_is_empty(self):
        before = self.interrupt()

        def check(state):
            self.assertEqual(state, before)
            with self.assertRaisesRegex(StudyError, "Another controller"):
                self.study.run()
            return None

        self.assertEqual(
            self.study.reconcile_execution(recovery_id="noop", certify=check), before
        )
        self.assertFalse((self.root / "recoveries").exists())

    def test_retry_obeys_existing_call_budget(self):
        before = self.interrupt()
        self.apply(self.certificate(before))
        state = self.study.status()
        state["call_count"] = self.study.config["max_calls"]
        (self.root / "state.json").write_text(json.dumps(state))
        self.assertEqual(self.study.run()["stop_reason"], "call_budget")
        self.assertEqual(self.harness.count("execute"), 1)

    def test_published_continuation_projects_source_recovery_only_to_same_plan(self):
        original_response = self.harness.response

        def response(phase, context, kwargs):
            result = original_response(phase, context, kwargs)
            if phase == "execute":
                result.update(outcome="incomplete", artifacts=[], measurements=[])
            return result

        self.harness.response = response
        state = self.study.run()
        self.assertEqual(state["status"], "stopped")
        cert = self.certificate(state, action="prepare_continuation")
        prepared = self.apply(cert, "continue-0001")
        metadata = read(self.root / "iterations/0001/iteration.json")
        continuation = {
            "source_iteration": "0001",
            "source_workspace_ref": metadata["workspace_ref"],
            "plan_ref": state["plan_ref"],
            "authorization": "adapter authority",
            "reconciliation": "backend proof",
            "directive": "Retain the accepted Plan.",
            "source_signatures": {},
        }
        with self.study._lock():
            self.study._continue_execution(
                prepared, source_iteration="0001", continuation=continuation
            )
        self.study.run()
        continued = [
            call
            for call in self.harness.calls
            if call["phase"] in {"execute", "review"}
        ][-2:]
        for call in continued:
            self.assertEqual(
                call["context"]["execution_recovery"][0]["recovery_id"], "continue-0001"
            )
        self.study.resume(reason="Another bounded question")
        self.study.run()
        next_plan = [call for call in self.harness.calls if call["phase"] == "plan"][-1]
        self.assertNotIn("execution_recovery", next_plan["context"])


if __name__ == "__main__":
    unittest.main()
