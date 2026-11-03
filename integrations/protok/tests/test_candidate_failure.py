import json
import time
import unittest
from unittest.mock import Mock

from integrations.protok.backend import METRIC, PROTOCOL, ProTokError, allocation_record
import test_protok
from integrations.protok import run
from research_loop import Study, initialize


class CandidateFailureTests(unittest.TestCase):
    setUp = test_protok.ProTokTests.setUp
    write = staticmethod(test_protok.ProTokTests.write)
    git = staticmethod(test_protok.ProTokTests.git)

    def failed(
        self,
        cause="RuntimeError: Missing first-step active gradients: {'layers.3.qkv.weight'}",
    ):
        prepared = self.backend.prepare_config(self.workspace, self.output)
        record = self.records / prepared["fingerprint"]
        record.mkdir()
        attempt = record / "seed-42-attempt-1"
        attempt.mkdir()
        (attempt / "run.log.tail").write_text(
            'Traceback (most recent call last):\n  File "/remote/current/code/scripts/train_autoresearch_se3.py", line 428, in step\n'
            + cause
            + "\n"
        )
        state = {
            "status": "failed",
            "fingerprint": prepared["fingerprint"],
            "mode": "scored",
            "attempts": [
                {
                    "directory": attempt.name,
                    "remote": "/remote/current",
                    "owned_job_id": "123",
                    "status": "failed",
                    "terminal_state": "FAILED",
                    "exit_code": "1:0",
                    "gpu_seconds": 39,
                }
            ],
        }
        self.write(record / "state.json", state)
        self.write(record / "run-config.json", json.loads(self.output.read_text()))
        return prepared, record, state

    def test_certifies_actual_gradient_failure_without_quality_score(self):
        prepared, record, _ = self.failed()
        before = (record / "state.json").read_bytes()
        failure = self.backend.settled_failure(
            self.workspace, self.output, prepared["fingerprint"]
        )
        self.assertEqual(failure["kind"], "candidate_rejected")
        self.assertEqual(failure["gpu_seconds"], 39)
        self.assertIn("layers.3.qkv.weight", failure["cause"])
        self.assertFalse(failure["quality_score_eligible"])
        self.assertNotIn("value", failure)
        self.assertEqual((record / "state.json").read_bytes(), before)

    def test_runtime_and_configuration_failures_bind_exact_attempt_workspace(self):
        prepared, record, state = self.failed()
        log = record / state["attempts"][0]["directory"] / "run.log.tail"
        for frame, cause in (
            ("protok/model/autoresearch_se3.py", "ValueError: invalid pair dimension"),
            (
                "scripts/train_autoresearch_se3.py",
                "ValueError: Candidate architecture must be a nonempty string",
            ),
        ):
            with self.subTest(cause=cause):
                log.write_text(
                    'Traceback (most recent call last):\n  File "/remote/current/code/'
                    + frame
                    + '", line 12, in forward\n'
                    + cause
                    + "\n"
                )
                failure = self.backend.settled_failure(
                    self.workspace, self.output, prepared["fingerprint"]
                )
                self.assertEqual(failure["cause"], cause)
                self.assertEqual(failure["diagnostics"]["owner"], "workspace")
        for cause in (
            "FileNotFoundError: [Errno 2] No such file or directory: '/shared/train.zarr'",
            "ModuleNotFoundError: No module named 'shared_dependency'",
            "PermissionError: protected dataset",
            "RuntimeError: CUDA driver is unavailable",
        ):
            with self.subTest(cause=cause):
                log.write_text(
                    'Traceback (most recent call last):\n  File "/remote/current/code/protok/model/autoresearch_se3.py", line 12, in forward\n'
                    + cause
                    + "\n"
                )
                with self.assertRaisesRegex(ProTokError, "not certified"):
                    self.backend.settled_failure(
                        self.workspace, self.output, prepared["fingerprint"]
                    )
        log.write_text(
            'Traceback (most recent call last):\n  File "/shared/pipeline.py", line 12, in forward\nRuntimeError: Missing first-step active gradients\n'
        )
        with self.assertRaisesRegex(ProTokError, "not certified"):
            self.backend.settled_failure(
                self.workspace, self.output, prepared["fingerprint"]
            )
        log.write_text(
            'Traceback (most recent call last):\n  File "/remote/other/code/protok/model/autoresearch_se3.py", line 12, in forward\nValueError: invalid shape\n'
        )
        with self.assertRaisesRegex(ProTokError, "not certified"):
            self.backend.settled_failure(
                self.workspace, self.output, prepared["fingerprint"]
            )

    def test_candidate_import_and_editable_target_are_repairable_but_shared_leaf_is_not(
        self,
    ):
        prepared, record, state = self.failed()
        log = record / state["attempts"][0]["directory"] / "run.log.tail"
        candidate = "/remote/current/code/protok/model/autoresearch_se3.py"
        cases = (
            (
                f"  File \"{candidate}\", line 1, in <module>\n    import misspelled_candidate_module\nModuleNotFoundError: No module named 'misspelled_candidate_module'\n",
                True,
            ),
            (
                f"  File \"{candidate}\", line 2, in forward\nPermissionError: [Errno 13] denied: '{candidate}'\n",
                True,
            ),
            (
                f"  File \"{candidate}\", line 2, in forward\nFileNotFoundError: [Errno 2] missing: '{candidate}'\n",
                True,
            ),
            (
                "  File \"/remote/current/code/scripts/train_autoresearch_se3.py\", line 1, in <module>\n    import shared_dependency\nModuleNotFoundError: No module named 'shared_dependency'\n",
                False,
            ),
            (
                f'  File "{candidate}", line 2, in forward\n    library()\n  File "/shared/library.py", line 3, in library\n    import missing_shared_dependency\nModuleNotFoundError: No module named \'missing_shared_dependency\'\n',
                False,
            ),
            (
                f'  File "{candidate}", line 2, in forward\n    library()\n  File "/shared/library.py", line 3, in library\nValueError: corrupt shared input\n',
                False,
            ),
        )
        for trace, reparable in cases:
            with self.subTest(trace=trace):
                log.write_text("Traceback (most recent call last):\n" + trace)
                if reparable:
                    self.assertEqual(
                        self.backend.settled_failure(
                            self.workspace, self.output, prepared["fingerprint"]
                        )["kind"],
                        "candidate_rejected",
                    )
                else:
                    with self.assertRaisesRegex(ProTokError, "not certified"):
                        self.backend.settled_failure(
                            self.workspace, self.output, prepared["fingerprint"]
                        )

    def test_candidate_library_shape_failure_is_repairable_without_owning_library(self):
        prepared, record, state = self.failed()
        log = record / state["attempts"][0]["directory"] / "run.log.tail"
        candidate = "/remote/current/code/protok/model/autoresearch_se3.py"
        callsite = (
            f'  File "{candidate}", line 15, in forward\n    return self.linear(x)\n'
        )
        torch = (
            '  File "/opt/venv/lib/python3.12/site-packages/torch/nn/modules/module.py", line 1747, in _call_impl\n'
            "    return forward_call(*args, **kwargs)\n"
            '  File "/opt/venv/lib/python3.12/site-packages/torch/nn/modules/linear.py", line 125, in forward\n'
            "    return F.linear(input, self.weight, self.bias)\n"
        )
        cases = (
            (
                torch
                + "RuntimeError: mat1 and mat2 shapes cannot be multiplied (8x4 and 6x2)\n",
                True,
            ),
            (
                '  File "/usr/lib/python3.12/functools.py", line 909, in wrapper\nTypeError: invalid argument type\n',
                True,
            ),
            (
                '  File "/shared/environment.py", line 8, in environment\nValueError: shared runtime configuration changed\n',
                False,
            ),
            (
                torch
                + '  File "/remote/current/code/scripts/train_autoresearch_se3.py", line 12, in native_callback\nValueError: corrupt shared input\n',
                False,
            ),
            (torch + "RuntimeError: CUDA driver is unavailable\n", False),
            (
                torch + "ModuleNotFoundError: No module named 'shared_dependency'\n",
                False,
            ),
            (torch + "FileNotFoundError: missing '/shared/train.zarr'\n", False),
            (torch + "PermissionError: denied '/shared/train.zarr'\n", False),
        )
        for suffix, reparable in cases:
            with self.subTest(suffix=suffix):
                log.write_text(
                    "Traceback (most recent call last):\n" + callsite + suffix
                )
                if reparable:
                    failure = self.backend.settled_failure(
                        self.workspace, self.output, prepared["fingerprint"]
                    )
                    self.assertEqual(failure["kind"], "candidate_rejected")
                else:
                    with self.assertRaisesRegex(ProTokError, "not certified"):
                        self.backend.settled_failure(
                            self.workspace, self.output, prepared["fingerprint"]
                        )
        from integrations.protok.debug_backend import candidate_cause

        trace = "Traceback (most recent call last):\n" + callsite + cases[0][0]
        self.assertIsNone(
            candidate_cause(trace, workspace="/remote/current/code", stage="prepare")
        )
        self.assertIsNone(
            candidate_cause(trace, workspace="/remote/other/code", stage="formal")
        )

    def test_accounting_missing_or_infrastructure_failure_cannot_enter_debug(self):
        prepared, record, state = self.failed("RuntimeError: SSH connection lost")
        with self.assertRaisesRegex(ProTokError, "not certified"):
            self.backend.settled_failure(
                self.workspace, self.output, prepared["fingerprint"]
            )
        state["attempts"][0].pop("gpu_seconds")
        self.write(record / "state.json", state)
        with self.assertRaisesRegex(ProTokError, "unsettled"):
            self.backend.settled_failure(
                self.workspace, self.output, prepared["fingerprint"]
            )

    def timed_out(self, *, fresh=False):
        prepared, record, state = self.failed()
        state["baseline"] = False
        state["attempts"][0].update(
            seed=42,
            mode="evaluate",
            budget_seconds=1800,
            include_smoke=True,
            terminal_state="TIMEOUT",
            exit_code="0:0",
            gpu_seconds=1828,
            owned_job_id="790195",
        )
        if fresh:
            state["fresh_attempt"] = "formal-019"
            destination = self.records / allocation_record(
                prepared["fingerprint"], state["fresh_attempt"]
            )
            record.rename(destination)
            record = destination
        self.write(record / "state.json", state)
        return prepared, record, state

    def timeout_receipt(self, prepared, record):
        return self.backend.timed_out_receipt(
            self.workspace,
            self.output,
            prepared["fingerprint"],
            record_name=record.name,
        )

    def test_timeout_is_neutral_idempotent_and_readable_under_stop_expired_lease(self):
        prepared, record, _ = self.timed_out(fresh=True)
        lease = json.loads((self.control / "ownership.json").read_text())
        self.write(self.control / "ownership.json", lease | {"expires_at": 0})
        (self.control / "STOP").write_text("operator stop\n")
        before = {
            p: p.read_bytes()
            for root in (self.records, self.control)
            for p in root.rglob("*")
            if p.is_file()
        }
        receipt = self.timeout_receipt(prepared, record)
        self.assertEqual(receipt, self.timeout_receipt(prepared, record))
        self.assertEqual(receipt["purpose"], "formal")
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["failure_kind"], "allocation_timeout")
        self.assertEqual(receipt["record_name"], record.name)
        self.assertEqual(receipt["job_id"], "790195")
        self.assertEqual(receipt["terminal_state"], "TIMEOUT")
        self.assertEqual(receipt["exit_code"], "0:0")
        self.assertEqual(receipt["gpu_seconds"], 1828)
        self.assertIs(receipt["quality_score_eligible"], False)
        for field in ("metric", "value", "measurements", "artifact", "kind"):
            self.assertNotIn(field, receipt)
        self.assertFalse((self.root / "invoked").exists())
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        with self.assertRaisesRegex(ProTokError, "candidate execution rejection"):
            self.backend.settled_failure(
                self.workspace,
                self.output,
                prepared["fingerprint"],
                record_name=record.name,
            )

    def test_timeout_rejects_other_terminal_states_and_unknown_accounting(self):
        prepared, record, state = self.timed_out()
        original = state["attempts"][0].copy()
        cases = [
            {"terminal_state": terminal}
            for terminal in ("CANCELLED", "FAILED", "OUT_OF_MEMORY", "COMPLETED", "")
        ] + [{"gpu_seconds": cost} for cost in (None, -1, float("inf"))]
        cases += [{"owned_job_id": ""}, {"status": "prepared"}]
        for update in cases:
            with self.subTest(update=update):
                state["attempts"] = [original | update]
                self.write(record / "state.json", state)
                with self.assertRaises(ProTokError):
                    self.timeout_receipt(prepared, record)
        state["attempts"] = [original.copy()]
        state["attempts"][0].pop("gpu_seconds")
        self.write(record / "state.json", state)
        with self.assertRaises(ProTokError):
            self.timeout_receipt(prepared, record)

    def test_timeout_requires_fixed_formal_and_fresh_record_identity(self):
        prepared, record, state = self.timed_out(fresh=True)
        original = state["attempts"][0].copy()
        for key, value in (
            ("seed", 43),
            ("mode", "smoke"),
            ("budget_seconds", 60),
            ("include_smoke", False),
        ):
            with self.subTest(key=key):
                state["attempts"] = [original | {key: value}]
                self.write(record / "state.json", state)
                with self.assertRaises(ProTokError):
                    self.timeout_receipt(prepared, record)
        state["attempts"] = [original]
        for update in (
            {"fresh_attempt": "other"},
            {"baseline": True},
            {"imported_baseline": True},
            {"mode": "smoke"},
            {"fingerprint": "other"},
        ):
            with self.subTest(update=update):
                self.write(record / "state.json", state | update)
                with self.assertRaises(ProTokError):
                    self.timeout_receipt(prepared, record)

    def test_timeout_rejects_source_config_owner_changes_and_other_outstanding(self):
        prepared, record, state = self.timed_out()
        paths = [
            self.control / "ownership.json",
            self.output,
            record / "run-config.json",
        ]
        for path, update in zip(
            paths,
            ({"owner_token": "other"}, {"seeds": [43]}, {"seeds": [43]}),
        ):
            with self.subTest(path=path):
                original = json.loads(path.read_text())
                self.write(path, original | update)
                with self.assertRaises(ProTokError):
                    self.timeout_receipt(prepared, record)
                self.write(path, original)
        source = self.workspace / "protok/model/autoresearch_se3.py"
        original_source = source.read_text()
        source.write_text("# changed source\n")
        with self.assertRaisesRegex(ProTokError, "frozen identity"):
            self.timeout_receipt(prepared, record)
        source.write_text(original_source)
        self.write(self.control / "continuation.json", {"schema": 2})
        state["controller_lease"] = {"owner_token": "other"}
        self.write(record / "state.json", state)
        with self.assertRaisesRegex(ProTokError, "another owner"):
            self.timeout_receipt(prepared, record)
        (self.control / "continuation.json").unlink()
        from research_loop.debug_budget import DebugBudget

        budget = DebugBudget(
            self.control / "debug/0019", scope_id="0019", limit_gpu_seconds=1800
        )
        budget.reserve("smoke-001", 1800)
        with self.assertRaisesRegex(ProTokError, "outstanding debug"):
            self.timeout_receipt(prepared, record)

    def test_timeout_rejects_unacknowledged_owner_and_outstanding_allocations(self):
        prepared, record, state = self.timed_out()
        lease_path = self.control / "ownership.json"
        lease = json.loads(lease_path.read_text())
        self.write(lease_path, lease | {"handoff_acknowledged": False})
        with self.assertRaisesRegex(ProTokError, "acknowledged owner"):
            self.timeout_receipt(prepared, record)
        self.write(lease_path, lease)
        self.write(record / "state.json", state | {"fresh_attempt": "wrong-record"})
        with self.assertRaisesRegex(ProTokError, "identity differs"):
            self.timeout_receipt(prepared, record)
        self.write(record / "state.json", state)
        outstanding = self.records / "unsettled/state.json"
        outstanding.parent.mkdir()
        self.write(outstanding, {"status": "running", "attempts": []})
        with self.assertRaisesRegex(ProTokError, "unsettled"):
            self.timeout_receipt(prepared, record)
        outstanding.unlink()
        # A retained allocation directory without its required state is unknown,
        # not proof that its cost disappeared. Remove this synthetic fixture
        # entirely before testing the independent research-ledger gate.
        self.assertIsNone(self.backend.inspect()["used_gpu_seconds"])
        outstanding.parent.rmdir()
        (self.control / "research-jobs/0018").mkdir(parents=True)
        self.write(
            self.control / "research-jobs/0018/job.json",
            {"allocated_seconds": 60, "source_identity": "research"},
        )
        with self.assertRaisesRegex(ProTokError, "outstanding debug or research"):
            self.timeout_receipt(prepared, record)

    def test_debug_cost_is_separate_but_unknown_debug_blocks_new_formal(self):
        from research_loop.debug_budget import DebugBudget

        budget = DebugBudget(
            self.control / "debug/0009", scope_id="0009", limit_gpu_seconds=1800
        )
        budget.reserve("smoke-001", 1800)
        budget.mark_submitting("smoke-001")
        budget.mark_submitted("smoke-001", "101")
        with self.assertRaisesRegex(ProTokError, "Outstanding debug"):
            self.backend.formal_capacity(self.output, time.time() + 10000)
        budget.settle(
            "smoke-001", gpu_seconds=600, terminal_state="FAILED", exit_code="1:0"
        )
        status = self.backend.inspect()
        self.assertEqual(status["used_gpu_seconds"], 0)
        self.assertEqual(status["debug_gpu_seconds"], 600)
        self.assertEqual(status["total_gpu_seconds"], 600)
        self.assertTrue(
            self.backend.formal_capacity(self.output, time.time() + 10000)["admitted"]
        )

    def test_fresh_completed_record_does_not_inherit_old_failed_receipt(self):
        prepared, original, _ = self.failed()
        fingerprint = prepared["fingerprint"]
        name = allocation_record(fingerprint, "debug-0009-formal-001")
        fresh = self.records / name
        fresh.mkdir()
        summary = {
            "protocol": PROTOCOL,
            "objective": METRIC,
            "fingerprint": fingerprint,
            "quality_score_eligible": True,
            "value": 2.1,
        }
        self.write(
            fresh / "state.json",
            {
                "status": "complete",
                "fingerprint": fingerprint,
                "mode": "scored",
                "fresh_attempt": "debug-0009-formal-001",
                "baseline": False,
                "attempts": [
                    {
                        "seed": 42,
                        "mode": "evaluate",
                        "budget_seconds": 1800,
                        "include_smoke": True,
                        "terminal_state": "COMPLETED",
                        "exit_code": "0:0",
                        "owned_job_id": "102",
                        "gpu_seconds": 1000,
                        "status": "complete",
                    }
                ],
                "summary": summary,
            },
        )
        self.write(fresh / "summary.json", summary)
        self.write(fresh / "run-config.json", json.loads(self.output.read_text()))
        failed_wrapper = self.control / "evaluation-old"
        failed_wrapper.mkdir()
        self.write(
            failed_wrapper / "receipt.json",
            {
                "fingerprint": fingerprint,
                "exit_code": 1,
                "error": "old candidate failure",
            },
        )
        result = self.backend.completed_receipt(
            self.workspace, self.output, fingerprint, record_name=name
        )
        self.assertEqual(result["record_name"], name)
        self.assertEqual(result["value"], 2.1)
        self.assertEqual(
            json.loads((original / "state.json").read_text())["status"], "failed"
        )

    def test_begin_debug_preserves_stop_original_attempt_and_ordinary_budgets(self):
        prepared, _, _ = self.failed()
        initialize(
            self.root / "study",
            goal="Repair the same fixed experiment",
            workspace=self.project,
            allow_write=True,
        )
        study = Study(self.root / "study")
        state = study.status()
        relative = "iterations/0009/execute/attempt-001"
        state.update(
            iteration=9,
            phase="execute",
            status="needs_attention",
            calls={"execute": relative},
            active_attempt=relative,
            call_count=7,
            elapsed_seconds=45,
            error="known failure",
        )
        study._save(state)
        folder = study.root / "iterations/0009"
        run.write(folder / "iteration.json", {"workspace": str(self.workspace)})
        run.write(study.root / relative / "process.json", {"status": "failed"})
        run.write(
            self.control / "debug-policy.json",
            {
                "schema": 1,
                "gpu_seconds_per_investigation": 1800,
                "independent_of_formal_budget": True,
                "authorization": "user",
            },
        )
        job = self.control / "jobs/0009"
        job.mkdir(parents=True)
        commit, signatures = run.freeze_source(
            self.project, self.workspace, job / "source", study.config["baseline"]
        )
        self.backend.prepare_config(job / "source", job / "runtime-config.json")
        run.write(
            job / "job.json",
            {
                "status": "submitted",
                "iteration": "0009",
                "request": {"action": "evaluate"},
                "fingerprint": prepared["fingerprint"],
                "source_commit": commit,
                "source_signatures": signatures,
            },
        )
        error = {
            "status": "error",
            "error": "protected evaluator failed",
            "retry": False,
        }
        run.write(self.workspace / "evaluation-result.json", error)
        stop = self.control / "STOP"
        stop.write_text("Supervisor stopped: attention\n")
        result = run.begin_debug(self.root, self.backend)
        self.assertEqual((result["call_count"], result["elapsed_seconds"]), (7, 45))
        self.assertEqual(
            (result["status"], result["debugging"]),
            ("ready", relative + "/failure.json"),
        )
        self.assertEqual(stop.read_text(), "Supervisor stopped: attention\n")
        self.assertEqual(
            run.read(study.root / relative / "process.json")["status"], "failed"
        )
        self.assertFalse((self.workspace / "evaluation-result.json").exists())
        self.assertEqual(
            run.read(
                self.control
                / "debug/0009/original-interface/0009-evaluation-result.json"
            ),
            error,
        )
        self.assertEqual(run.begin_debug(self.root, self.backend)["call_count"], 7)

    def test_debug_recovery_configuration_is_retained_per_smoke_attempt(self):
        debug = self.control / "debug/0009"
        backend = Mock()
        backend.debug_smoke.return_value = {"status": "failed"}
        for number in (1, 2):
            active = f"smoke-{number:03d}"
            config = {"workspace": str(self.root / f"source-{number}")}
            run.write(debug / "debug-budget.json", {"active_attempt": active})
            run.write(
                debug / "attempts" / active / "debug.json",
                {"base_config": config, "deadline": 9999},
            )
            run.reconcile_debug_allocations(self.root, backend)
            self.assertEqual(
                run.read(debug / "attempts" / active / "recovery-config.json"), config
            )
        self.assertEqual(backend.debug_smoke.call_count, 2)


if __name__ == "__main__":
    unittest.main()
