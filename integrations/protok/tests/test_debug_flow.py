import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from integrations.protok import run
from integrations.protok.backend import (
    CANDIDATE_FILES,
    METRIC,
    PROTOCOL,
    ProTokError,
    allocation_record,
)
from integrations.protok.debug_flow import REPAIR, _RepairDelegate, recover_execution
from research_loop import Study, initialize
from research_loop.debug_budget import DebugBudget
from research_loop.harness import (
    HarnessError,
    completed_response,
    publish_completed_response,
)


class ProcessLost(BaseException):
    pass


class CpuBackend:
    def __init__(self, project):
        self.project = project
        self.smokes = ["failed", "completed"]
        self.smoke_calls = []
        self.formals = ["completed"]
        self.formal_calls = []
        self.receipts = {}
        self.smoke_seconds = 120
        self.capacity = True
        self.interrupt_smoke = False
        self.interrupt_formal = False

    def prepare_config(self, workspace, output):
        value = hashlib.sha256(
            json.dumps(
                run.signatures(run.candidate_bytes(workspace)), sort_keys=True
            ).encode()
        ).hexdigest()
        run.write(output, {"workspace": str(workspace), "seeds": [42]})
        return {"fingerprint": value, "config_path": str(output)}

    def inspect(self):
        return {"outstanding": []}

    def formal_capacity(self, config_path, deadline):
        return {
            "admitted": self.capacity and time.time() < deadline,
            "reason": "formal budget unavailable",
        }

    def debug_smoke(
        self, workspace, config, debug_root, attempt_id, deadline, *, recover=False
    ):
        self.smoke_calls.append((attempt_id, recover, deadline))
        path = debug_root / "attempts" / attempt_id / "cpu-receipt.json"
        if path.exists():
            assert recover
            return run.read(path)
        assert not recover
        budget = DebugBudget(debug_root, scope_id="0009", limit_gpu_seconds=1800)
        budget.reserve(attempt_id, requested_seconds=self.smoke_seconds)
        budget.mark_submitting(attempt_id)
        budget.mark_submitted(attempt_id, "job-" + attempt_id)
        outcome = self.smokes.pop(0)
        if outcome != "blocked":
            budget.settle(
                attempt_id,
                gpu_seconds=self.smoke_seconds,
                terminal_state="COMPLETED" if outcome == "completed" else "FAILED",
                exit_code="0:0" if outcome == "completed" else "1:0",
            )
        result = {
            "purpose": "debug",
            "quality_score_eligible": False,
            "fingerprint": self.prepare_config(workspace, config)["fingerprint"],
            "status": outcome,
            "verified": outcome == "completed",
            "error": "RuntimeError: native active-gradient failure"
            if outcome == "failed"
            else None,
            "failure_kind": "candidate" if outcome == "failed" else None,
            "cause": "RuntimeError: native active-gradient failure"
            if outcome == "failed"
            else None,
            "evidence": [str(path)],
            "budget": budget.status(),
        }
        run.write(path, result)
        if self.interrupt_smoke:
            self.interrupt_smoke = False
            raise ProcessLost()
        return result

    def evaluate(
        self,
        workspace,
        config,
        timeout_seconds,
        *,
        recover,
        fresh_attempt,
        cancel_event,
    ):
        self.formal_calls.append((fresh_attempt, recover))
        if fresh_attempt in self.receipts:
            assert recover
            result = self.receipts[fresh_attempt]
        else:
            assert not recover
            outcome = self.formals.pop(0)
            result = {
                "status": outcome,
                "verified": outcome == "completed",
                "fingerprint": self.prepare_config(workspace, config)["fingerprint"],
                "metric": METRIC,
                "value": 2.5,
                "evidence": [str(config)],
                "fresh_attempt": fresh_attempt,
            }
            result["record_name"] = allocation_record(
                result["fingerprint"], fresh_attempt
            )
            self.receipts[fresh_attempt] = result
        if self.interrupt_formal:
            self.interrupt_formal = False
            raise ProcessLost()
        if result["status"] != "completed":
            raise ProTokError("formal candidate rejected")
        return result

    def settled_failure(self, workspace, config, fingerprint, *, record_name):
        if not any(record_name.endswith("--" + name) for name in self.receipts):
            raise ProTokError("no retained formal allocation")
        if any(value["status"] == "unknown" for value in self.receipts.values()):
            raise ProTokError("unsettled allocation")
        return {
            "kind": "candidate_rejected",
            "cause": "RuntimeError: active gradient missing",
            "fingerprint": fingerprint,
            "record_name": record_name,
            "purpose": "formal",
            "quality_score_eligible": False,
            "evidence": [str(config)],
        }


class Delegate:
    def __init__(self):
        self.calls = []
        self.unchanged = False
        self.interface = False

    def run(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs["prompt"].startswith(REPAIR)
        if not self.unchanged:
            candidate = kwargs["workspace"] / CANDIDATE_FILES[0]
            candidate.write_text(
                candidate.read_text() + f"# repair {len(self.calls)}\n"
            )
        if self.interface:
            (kwargs["workspace"] / "evaluation-request.json").write_text(
                '{"action":"evaluate"}'
            )
        return {
            "outcome": "incomplete",
            "markdown": "Implementation repaired; no allocation requested.",
            "artifacts": [],
            "measurements": [],
        }


class DebugFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name).resolve()
        self.project = directory / "canonical"
        self.project.mkdir()
        for name in CANDIDATE_FILES:
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n" if name.endswith("json") else "# original\n")
        subprocess.run(["git", "init", "--quiet", str(self.project)], check=True)
        run.git(self.project, "add", ".")
        run.git(
            self.project,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "--quiet",
            "-m",
            "baseline",
        )
        baseline = run.git(self.project, "rev-parse", "HEAD")
        self.root = directory / "takeover"
        self.workspace = self.root / "study/iterations/0009/workspace"
        self.workspace.parent.mkdir(parents=True)
        subprocess.run(
            [
                "git",
                "clone",
                "--shared",
                "--quiet",
                str(self.project),
                str(self.workspace),
            ],
            check=True,
        )
        run.write(self.root / "study/study.json", {"baseline": baseline})
        run.write(
            self.root / "control/debug-policy.json",
            {
                "schema": 1,
                "gpu_seconds_per_investigation": 1800,
                "independent_of_formal_budget": True,
                "authorization": "user approved cumulative independent grant",
            },
        )
        self.failure = {
            "kind": "candidate_rejected",
            "iteration": "0009",
            "source_signatures": run.signatures(run.candidate_bytes(self.workspace)),
            "source_commit": baseline,
            "fingerprint": "original-failed",
            "cause": "RuntimeError: active gradients missing",
            "evidence": ["original-terminal.log"],
            "job_directory": str(self.root / "control/jobs/0009"),
        }
        self.backend = CpuBackend(self.project)
        self.delegate = Delegate()
        self.harness = SimpleNamespace(
            root=self.root,
            backend=self.backend,
            delegate=self.delegate,
            deadline=time.time() + 21600,
        )
        self.invocations = []

    def invoke(self, key, context, directory, harness):
        attempt = directory / "attempt-001"
        if completed_response(attempt) is not None:
            return completed_response(attempt)
        attempt.mkdir(parents=True)
        self.invocations.append(key)
        run.write(attempt / "context.json", context)
        result = harness.run(
            prompt="original Execute text", workspace=self.workspace, run_dir=attempt
        )
        run.write(attempt / "response.json", result)
        publish_completed_response(attempt, result)
        return result

    def recover(self):
        return recover_execution(
            self.harness,
            self.failure,
            workspace=self.workspace,
            run_dir=self.workspace.parent / "execute/recovery",
            context={"plan": {"scientific_action": "unchanged"}},
            invoke=self.invoke,
        )

    def test_failed_smoke_then_repair_success_uses_new_full_formal(self):
        result = self.recover()
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(
            result["measurements"][0]["metrics"], [{"name": METRIC, "value": 2.5}]
        )
        self.assertEqual(len(self.invocations), 2)
        self.assertEqual(self.backend.formal_calls, [("debug-0009-formal-001", False)])
        debug = self.root / "control/debug/0009"
        self.assertEqual(run.read(debug / "debug-budget.json")["used_gpu_seconds"], 240)
        self.assertEqual(
            run.read(self.root / "control/jobs/0009/recovery.json"),
            {"job_directory": str(debug / "formal-001")},
        )
        self.assertEqual(run.read(debug / "decision.json")["status"], "completed")
        self.assertEqual(self.recover(), result)
        self.assertEqual(len(self.backend.formal_calls), 1)

    def test_exhausted_grant_cannot_reset_or_publish_measurement(self):
        self.backend.smoke_seconds = 1800
        result = self.recover()
        self.assertEqual(
            (result["outcome"], result["measurements"]), ("incomplete", [])
        )
        self.assertEqual(len(self.invocations), 1)
        self.assertEqual(self.backend.formal_calls, [])
        self.recover()
        self.assertEqual(len(self.invocations), 1)

    def test_certified_submission_receipt_survives_debug_and_blocks_resubmission(self):
        from research_loop.submission import RECEIPT, submit

        receipt = {"action": "evaluate"}
        run.write(self.workspace / RECEIPT, receipt)
        self.failure["submission"] = receipt
        result = self.recover()
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(run.read(self.workspace / RECEIPT), receipt)
        self.assertEqual(submit(self.workspace)["status"], "accepted")
        self.assertEqual(len(self.backend.formal_calls), 1)
        (self.workspace / RECEIPT).unlink()
        with self.assertRaisesRegex(HarnessError, "certified original submission"):
            self.recover()

    def test_smoke_interruption_recovers_same_attempt_without_second_charge(self):
        self.backend.smokes = ["completed"]
        self.backend.interrupt_smoke = True
        with self.assertRaises(ProcessLost):
            self.recover()
        result = self.recover()
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(
            self.backend.smoke_calls,
            [
                ("smoke-001", False, self.harness.deadline),
                ("smoke-001", True, self.harness.deadline),
            ],
        )
        self.assertEqual(len(self.invocations), 1)
        self.assertEqual(
            run.read(self.root / "control/debug/0009/debug-budget.json")[
                "used_gpu_seconds"
            ],
            120,
        )

    def test_formal_interruption_recovers_same_fresh_allocation(self):
        self.backend.smokes = ["completed"]
        self.backend.interrupt_formal = True
        with self.assertRaises(ProcessLost):
            self.recover()
        result = self.recover()
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(
            self.backend.formal_calls,
            [("debug-0009-formal-001", False), ("debug-0009-formal-001", True)],
        )

    def test_formal_candidate_failure_requires_distinct_next_formal_identity(self):
        self.backend.smokes = ["completed", "completed"]
        self.backend.formals = ["failed", "completed"]
        result = self.recover()
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(
            self.backend.formal_calls,
            [("debug-0009-formal-001", False), ("debug-0009-formal-002", False)],
        )
        self.assertEqual(len(self.invocations), 2)
        self.assertTrue(
            (self.root / "control/debug/0009/formal-001/failure.json").exists()
        )

    def test_unknown_formal_failure_blocks_without_reallocation(self):
        self.backend.smokes = ["completed"]
        self.backend.formals = ["unknown"]
        with self.assertRaisesRegex(HarnessError, "unknown/unsettled"):
            self.recover()
        with self.assertRaisesRegex(HarnessError, "unknown/unsettled"):
            self.recover()
        self.assertEqual(self.backend.formal_calls[-1], ("debug-0009-formal-001", True))
        self.assertEqual(len(self.invocations), 1)
        self.assertFalse((self.root / "control/debug/0009/decision.json").exists())

    def test_settled_fresh_formal_timeout_finishes_without_repair_or_resubmission(self):
        self.backend.smokes = ["completed"]
        self.backend.formals = ["timeout"]
        self.failure["submission"] = {"action": "evaluate"}
        run.write(
            self.workspace / "evaluation-submission.json", self.failure["submission"]
        )

        def rejected(*args, **kwargs):
            raise ProTokError("TIMEOUT is not a candidate-code rejection")

        def timeout(workspace, config, fingerprint, *, record_name):
            return {
                "purpose": "formal",
                "status": "failed",
                "failure_kind": "allocation_timeout",
                "fingerprint": fingerprint,
                "record_name": record_name,
                "job_id": "790195",
                "terminal_state": "TIMEOUT",
                "exit_code": "0:0",
                "gpu_seconds": 1828,
                "quality_score_eligible": False,
                "cause": "Slurm time limit reached.",
                "evidence": [str(config)],
            }

        self.backend.settled_failure = rejected
        self.backend.timed_out_receipt = timeout
        result = self.recover()
        self.assertEqual(result["outcome"], "incomplete")
        self.assertEqual(result["artifacts"], [])
        self.assertEqual(result["measurements"], [])
        self.assertEqual(len(self.invocations), 1)
        self.assertEqual(self.backend.formal_calls, [("debug-0009-formal-001", False)])
        decision = run.read(self.root / "control/debug/0009/decision.json")
        self.assertEqual(decision["status"], "failed")
        self.assertEqual(decision["failures"][-1]["gpu_seconds"], 1828)
        self.assertEqual(self.recover(), result)
        self.assertEqual(len(self.backend.formal_calls), 1)
        self.assertEqual(len(self.invocations), 1)

    def test_blocked_smoke_preserves_unaccounted_allocation_and_stops(self):
        self.backend.smokes = ["blocked"]
        with self.assertRaisesRegex(HarnessError, "unknown/unaccounted"):
            self.recover()
        self.assertTrue(
            run.read(self.root / "control/debug/0009/debug-budget.json")[
                "active_attempt"
            ]
        )
        self.assertEqual(self.backend.formal_calls, [])

    def test_no_full_formal_capacity_returns_incomplete(self):
        self.backend.smokes = ["completed"]
        self.backend.capacity = False
        result = self.recover()
        self.assertEqual(
            (result["outcome"], result["measurements"]), ("incomplete", [])
        )
        self.assertEqual(self.backend.formal_calls, [])

    def test_repair_must_change_failed_source_and_cannot_request_evaluation(self):
        self.delegate.unchanged = True
        self.assertEqual(self.recover()["outcome"], "incomplete")
        self.assertEqual(self.backend.smoke_calls, [])

    def test_repair_interface_request_is_preserved_and_blocks(self):
        self.delegate.interface = True
        with self.assertRaisesRegex(HarnessError, "interface"):
            self.recover()
        self.assertTrue((self.workspace / "evaluation-request.json").exists())
        self.assertEqual(self.backend.smoke_calls, [])

    def test_deadline_or_policy_cannot_reset(self):
        self.backend.smokes = ["blocked"]
        with self.assertRaises(HarnessError):
            self.recover()
        self.harness.deadline += 1
        with self.assertRaisesRegex(HarnessError, "cannot change or reset"):
            self.recover()

    def test_partial_repair_restarts_only_after_stopped_cli_and_no_interfaces(self):
        self.backend.smokes = ["completed"]
        invoke = self.invoke

        def interrupt(key, context, directory, harness):
            attempt = directory / "attempt-001"
            attempt.mkdir(parents=True)
            self.invocations.append(key)
            run.write(attempt / "process.json", {"status": "interrupted"})
            (attempt / "stdout.jsonl").write_text("partial repair evidence\n")
            raise KeyboardInterrupt()

        self.invoke = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.recover()
        self.invoke = invoke
        self.assertEqual(self.recover()["outcome"], "completed")
        self.assertEqual(self.invocations, ["repair-001-try-001", "repair-001-try-002"])
        self.assertTrue(
            (
                self.root
                / "study/iterations/0009/execute/recovery/repairs/repair-001-try-001/attempt-001/stdout.jsonl"
            ).exists()
        )

    def test_partial_repair_with_live_cli_cannot_restart(self):
        def interrupt(key, context, directory, harness):
            attempt = directory / "attempt-001"
            attempt.mkdir(parents=True)
            run.write(
                attempt / "process.json", {"status": "running", "pid": os.getpid()}
            )
            raise KeyboardInterrupt()

        self.invoke = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.recover()
        with self.assertRaisesRegex(HarnessError, "active or unknown"):
            self.recover()
        self.assertEqual(self.backend.smoke_calls, [])

    def test_formal_submission_intent_never_reallocates_on_ambiguous_restart(self):
        self.backend.smokes = ["completed"]
        write = run.write
        interrupted = False

        def crash(path, value):
            nonlocal interrupted
            write(path, value)
            if (
                Path(path).name == "job.json"
                and value.get("status") == "submitted"
                and not interrupted
            ):
                interrupted = True
                raise ProcessLost()

        with patch.object(run, "write", side_effect=crash):
            with self.assertRaises(ProcessLost):
                self.recover()
        with self.assertRaisesRegex(HarnessError, "unknown/unsettled"):
            self.recover()
        self.assertEqual(self.backend.formal_calls, [("debug-0009-formal-001", True)])
        self.assertEqual(self.backend.receipts, {})

    def test_infrastructure_smoke_failure_never_allocates_another_repair(self):
        smoke = self.backend.debug_smoke

        def infrastructure(*args, **kwargs):
            receipt = smoke(*args, **kwargs)
            return {
                **receipt,
                "failure_kind": "infrastructure",
                "cause": "node unavailable",
            }

        self.backend.debug_smoke = infrastructure
        with self.assertRaisesRegex(HarnessError, "infrastructure cause"):
            self.recover()
        self.assertEqual(len(self.invocations), 1)
        self.assertEqual(len(self.backend.smoke_calls), 1)
        self.assertEqual(self.backend.formal_calls, [])

    def test_unclassified_failed_smoke_stays_blocked_even_at_full_grant(self):
        smoke = self.backend.debug_smoke
        self.backend.smoke_seconds = 1800

        def unclassified(*args, **kwargs):
            receipt = smoke(*args, **kwargs)
            receipt.pop("failure_kind", None)
            return receipt

        self.backend.debug_smoke = unclassified
        with self.assertRaisesRegex(HarnessError, "unknown or infrastructure"):
            self.recover()
        self.assertFalse((self.root / "control/debug/0009/decision.json").exists())
        self.assertEqual(len(self.invocations), 1)
        self.assertEqual(self.backend.formal_calls, [])

    def test_preadmission_failure_keeps_cause_and_requires_explicit_reconciliation(
        self,
    ):
        self.backend.debug_smoke = lambda *args, **kwargs: {
            "purpose": "debug",
            "quality_score_eligible": False,
            "status": "blocked",
            "verified": False,
            "cause": "ValueError: Debug records overlap the candidate workspace",
        }
        with self.assertRaisesRegex(
            HarnessError, "unbound blocked receipt: ValueError:"
        ):
            self.recover()
        debug_root = self.root / "control/debug/0009"
        self.assertTrue(run.read(debug_root / "flow.json")["smoke_submitted"])
        self.assertEqual(run.read(debug_root / "debug-budget.json")["attempts"], {})
        self.assertFalse((debug_root / "decision.json").exists())
        self.assertEqual(len(self.invocations), 1)
        self.assertEqual(self.backend.formal_calls, [])

    def test_repair_delegate_clips_timeout_to_original_deadline_and_refuses_expiry(
        self,
    ):
        delegate = SimpleNamespace(run=lambda **kwargs: kwargs)
        wrapper = _RepairDelegate(delegate, self.root, 161.0)
        with patch("integrations.protok.debug_flow.time.time", return_value=100.0):
            result = wrapper.run(prompt="repair", timeout_seconds=600)
        self.assertEqual(result["timeout_seconds"], 61)
        with patch("integrations.protok.debug_flow.time.time", return_value=161.0):
            with self.assertRaisesRegex(HarnessError, "deadline"):
                wrapper.run(prompt="repair", timeout_seconds=600)

    def test_real_study_and_protok_harness_complete_repair_and_review(self):
        spec = importlib.util.spec_from_file_location(
            "debug_flow_core_fixture",
            Path(__file__).resolve().parents[3] / "tests/test_core.py",
        )
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        live = self.root / "real"
        base = live / "baseline"
        base.parent.mkdir(parents=True)
        subprocess.run(
            ["git", "clone", "--shared", "--quiet", str(self.project), str(base)],
            check=True,
        )
        definition = {
            "id": PROTOCOL,
            "comparison_scope": PROTOCOL,
            "protocol": "Fixed native protocol.",
            "data_role": "development",
            "allow_feedback": True,
            "objectives": [
                {"name": METRIC, "direction": "minimize", "unit": "angstrom"}
            ],
            "constraints": [],
        }
        initialize(
            live / "study",
            goal="Preserve the scientific Plan while repairing candidate code.",
            workspace=base,
            allow_write=True,
            max_iterations=None,
            global_evaluation=definition,
            strategy={"selection": "direct", "review": "full"},
        )
        state = run.read(live / "study/state.json")
        state["iteration"] = 8
        run.write(live / "study/state.json", state)
        run.write(
            live / "control/debug-policy.json",
            run.read(self.root / "control/debug-policy.json"),
        )
        backend = self.backend
        backend.smokes = ["completed"]
        evaluate, settled = backend.evaluate, backend.settled_failure

        def initial_failure(
            source,
            config,
            timeout_seconds,
            *,
            recover=False,
            fresh_attempt=None,
            cancel_event=None,
        ):
            if fresh_attempt is None:
                raise ProTokError("known terminal candidate rejection")
            return evaluate(
                source,
                config,
                timeout_seconds,
                recover=recover,
                fresh_attempt=fresh_attempt,
                cancel_event=cancel_event,
            )

        def classify(source, config, fingerprint, *, record_name=None):
            if record_name is not None:
                return settled(source, config, fingerprint, record_name=record_name)
            return {
                "kind": "candidate_rejected",
                "purpose": "formal",
                "quality_score_eligible": False,
                "cause": "RuntimeError: missing active gradient",
                "fingerprint": fingerprint,
                "record_name": fingerprint,
                "evidence": [str(config)],
                "job_id": "original-terminal-job",
                "gpu_seconds": 30,
            }

        def completed(source, config, fingerprint, *, record_name=None):
            result = next(
                value
                for value in backend.receipts.values()
                if value["record_name"] == record_name
            )
            self.assertEqual(result["fingerprint"], fingerprint)
            return result

        backend.evaluate, backend.settled_failure, backend.completed_receipt = (
            initial_failure,
            classify,
            completed,
        )

        class Agent(fixture.RecordedHarness):
            def response(agent, phase, context, kwargs):
                if phase != "execute":
                    return super().response(phase, context, kwargs)
                candidate = kwargs["workspace"] / CANDIDATE_FILES[0]
                candidate.write_text(
                    candidate.read_text() + "# implementation revision\n"
                )
                if not kwargs["prompt"].startswith(REPAIR):
                    run.write(
                        kwargs["workspace"] / "evaluation-request.json",
                        {"action": "evaluate"},
                    )
                    until = time.monotonic() + 5
                    while not (kwargs["workspace"] / "evaluation-result.json").exists():
                        if time.monotonic() >= until:
                            raise AssertionError(
                                "Bridge did not return original terminal failure"
                            )
                        time.sleep(0.01)
                return {
                    "outcome": "incomplete",
                    "markdown": "Candidate implementation needs controller validation.",
                    "artifacts": [],
                    "measurements": [],
                }

        agent = Agent()
        harness = run.ProTokHarness(backend, live, agent, self.harness.deadline)
        study = Study(live / "study", harness)
        with patch.object(run, "admission", return_value=self.harness.deadline):
            result = study.run()
        self.assertEqual((result["status"], result["phase"]), ("stopped", "done"))
        self.assertEqual(result["call_count"], 7)
        self.assertEqual(agent.count("execute"), 2)
        self.assertEqual(agent.count("review"), 1)
        self.assertEqual(backend.formal_calls, [("debug-0009-formal-001", False)])
        repair = next(call for call in agent.calls if call["prompt"].startswith(REPAIR))
        self.assertTrue(repair["run_dir"].is_relative_to(live / "study"))
        self.assertTrue((repair["run_dir"] / "process.json").exists())
        self.assertTrue((live / "control/debug/0009/decision.json").exists())
        self.assertTrue((live / "study/iterations/0009/publication.json").exists())
        measurement = run.read(live / "study" / result["execution_ref"])[
            "measurements"
        ][0]
        self.assertEqual(
            study.frontiers()["global"][PROTOCOL]["candidate_ids"],
            ["0009:" + measurement["candidate_id"]],
        )


if __name__ == "__main__":
    unittest.main()
