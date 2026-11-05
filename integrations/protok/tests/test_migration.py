"""Synthetic ProTok control migration; no real project, SSH, model or GPU."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from integrations.protok.migration import (
    cleanup_action,
    query_local_receipt,
    translate_legacy,
    validate_cleanup_config,
    with_captured_controls,
)
from research_loop.runtime.legacy_inventory import capture_control_annex, capture_legacy
from research_loop.runtime.migration_records import (
    build_translation,
    install_translation,
    validate_translation,
)
from research_loop.runtime.operations import Operations
from research_loop.runtime.store import InvalidTransition, StateStore


class ProTokMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "study"
        self.root.mkdir()
        for folder in ("iterations", "routes", "tasks"):
            (self.root / folder).mkdir()
        self.write(
            "study.json",
            {
                "version": 3,
                "allow_write": False,
                "global_evaluation": None,
                "max_iterations": None,
                "max_calls": 30,
                "max_seconds": 2000,
            },
        )
        self.write("goal.md", "Synthetic control preservation\n")
        self.write("research.md", "Prior evidence remains legacy evidence\n")
        self.write("routes/index.json", {})
        self.write("frontiers.json", {"global": {}, "local": {}})
        self.states, self.plans = {}, {}
        for number in (1, 2, 3):
            identity = f"{number:04d}"
            plan_dir = f"iterations/{identity}/plan/attempt-001"
            execute = f"iterations/{identity}/execute/attempt-001"
            plan = {
                "decision": "investigate",
                "markdown": "Exact synthetic scientific contract",
                "base_experiment": None,
                "references": [],
                "route_research": "Unknown mechanism",
                "local_evaluation": None,
                "execution": {
                    "mode": "script",
                    "gpu": True,
                    "entrypoint": "research/probe.py",
                    "time_limit_seconds": 30,
                    "inputs": [
                        {"name": "prior", "source": "opaque-original-reference"}
                    ],
                    "outputs": ["observation.json"],
                },
                "timeout_seconds": 60,
                "stop_reason": None,
            }
            self.plans[number] = plan
            for name, value in (
                ("canonical-response.json", plan),
                ("response.json", plan),
                (
                    "completed.json",
                    {"version": 1, "run_id": identity, "response": plan},
                ),
                ("process.json", {"run_id": identity, "status": "succeeded"}),
            ):
                self.write(plan_dir + "/" + name, value)
            self.write(
                execute + "/parameter-budget.json",
                {"deadline": 800.0 if number == 3 else 1000.0},
            )
            self.write(
                execute + "/process.json",
                {"status": "failed" if number == 3 else "succeeded"},
            )
            metadata = {
                "id": identity,
                "status": "planned" if number == 3 else "reviewed",
            }
            self.write(f"iterations/{identity}/iteration.json", metadata)
            state = {
                "iteration": number,
                "phase": "execute" if number == 3 else "done",
                "status": "needs_attention" if number == 3 else "ready",
                "calls": {"plan-0": plan_dir, "execute": execute},
                "accepted_calls": {"plan-0": True, "execute": number != 3},
                "plan_ref": plan_dir + "/canonical-response.json",
                "execution_ref": None if number == 3 else execute + "/response.json",
                "call_count": 11,
                "elapsed_seconds": 6.5,
                "active_attempt": execute if number == 3 else None,
                "stop_reason": None,
            }
            self.states[number] = state
            if number != 3:
                self.write(
                    execute + "/response.json",
                    {
                        "outcome": "completed",
                        "markdown": "Legacy report",
                        "artifacts": [],
                        "measurements": [{"metrics": [{"name": "score", "value": 1}]}],
                    },
                )
                self.write(
                    f"iterations/{identity}/publication.json",
                    {
                        "before": {},
                        "writes": {},
                        "metadata": metadata,
                        "state_after": state,
                    },
                )
        self.write("state.json", self.states[3])
        self.write(
            "tasks/correction/request.json",
            {
                "id": "correction",
                "kind": "investigation",
                "priority": 100,
                "allow_changes": False,
                "instruction": "Qualify a historical claim",
                "source": {"revision": "original-version"},
            },
        )
        self.write("tasks/correction/scheduling.json", {"priority": 2})
        self.control = self.base / "control"
        self.control.mkdir()
        grant = {"grant_id": "original-scope", "gpu_seconds": 200, "deadline": 1500.0}
        self.grant = grant
        self.external("authorization", grant)
        self.external("accounting", {"used_gpu_seconds": 7})
        self.external("resources", {"allocation": {"accelerator": 1, "cpu": 2}})
        self.external("current", {"request": self.states[3]["calls"]["execute"]})
        first = self.receipt(1)
        first.update(
            job_id="job-first",
            terminal_state="COMPLETED",
            exit_code="0:0",
            gpu_seconds=7,
        )
        self.external("job-first", first)
        absent = self.receipt(2)
        absent.update(
            job_id=None, terminal_state=None, exit_code=None, gpu_seconds=None
        )
        self.external("job-second", absent)
        self.external(
            "no-submit",
            {
                "version": 1,
                "effect": "never_submitted",
                "authorization": "historical operator assertion",
                **{
                    key: absent[key]
                    for key in (
                        "study_root",
                        "attempt",
                        "grant_id",
                        "plan",
                        "execution",
                    )
                },
            },
        )
        self.inventory = capture_control_annex(
            capture_legacy(self.root),
            {
                name: str(self.control / (name + ".json"))
                for name in (
                    "authorization",
                    "accounting",
                    "resources",
                    "current",
                    "job-first",
                    "job-second",
                    "no-submit",
                )
            },
        )
        self.selectors = {
            "version": 1,
            "authorization_ref": "annex:authorization",
            "accounting": {
                "settled_gpu_seconds": 7,
                "admitted_operations": 2,
                "source_refs": ["annex:accounting"],
            },
            "jobs": [
                {
                    "iteration_id": "0001",
                    "record_ref": "annex:job-first",
                    "never_submitted_ref": None,
                },
                {
                    "iteration_id": "0002",
                    "record_ref": "annex:job-second",
                    "never_submitted_ref": "annex:no-submit",
                },
            ],
            "current_source_refs": ["annex:current"],
        }
        self.profile = {
            "pools": {"accelerator": 1, "cpu": 2},
            "per_operation": {"accelerator": 1, "cpu": 2},
            "authorization_limits": {"accelerator": 1, "cpu": 2},
            "source_refs": ["annex:resources"],
        }
        self.backend = {"id": "synthetic-protok", "version": "1"}
        self.environment = {"id": "synthetic-environment", "version": "1"}

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        return path

    def external(self, name, value):
        path = self.control / (name + ".json")
        path.write_text(json.dumps(value))
        return path

    def receipt(self, number=3):
        return {
            "study_root": str(self.root),
            "attempt": self.states[number]["calls"]["execute"],
            "plan": self.plans[number],
            "execution": self.plans[number]["execution"],
            "grant_id": "original-scope",
            "seconds": 30,
            "deadline": 800.0 if number == 3 else 900.0,
            "job_id": "job-current",
            "terminal_state": "COMPLETED",
            "exit_code": "0:0",
            "gpu_seconds": 8,
        }

    def translate(self):
        return translate_legacy(
            self.inventory,
            self.selectors,
            backend_identity=self.backend,
            environment=self.environment,
            resource_profile=self.profile,
        )

    def inputs(self):
        result = self.translate()
        operation = result["translation"]["executions"][0]["operations"][0]
        operation = copy.deepcopy(operation)
        operation.update(submit_forbidden=True, authorization_id=self.grant["grant_id"])
        request = copy.deepcopy(operation["request"])
        config = {"inventory": self.inventory, "backend_controls": self.selectors}
        return operation, request, config

    def test_translation_preserves_unknown_hold_deadline_and_archive_only_history(self):
        with patch.object(
            Path, "read_bytes", side_effect=AssertionError("source reread")
        ):
            result = self.translate()
        row = result["translation"]["executions"][0]
        op = row["operations"][0]
        self.assertEqual(len(result["translation"]["executions"]), 1)
        self.assertEqual(row["plan"]["normalized_output"], self.plans[3])
        self.assertIsNone(row["execute"]["normalized_output"])
        self.assertEqual(op["deadline"], 800.0)
        self.assertEqual(op["held_costs"], {"gpu_seconds": 30})
        self.assertEqual(op["actual_costs"], {})
        self.assertEqual(op["held_resources"], self.profile["per_operation"])
        self.assertIsNone(op["job_id"])
        self.assertIsNone(op["submission_token"])
        self.assertEqual(op["state"], "unknown")
        self.assertTrue(op["external_open"])
        self.assertEqual(
            result["translation"]["authorities"][0]["settled_costs"], {"gpu_seconds": 7}
        )
        self.assertIsNone(
            result["runtime_config_patch"]["operation_authorizations"][
                "original-scope"
            ]["max_operations"]
        )
        self.assertEqual(
            result["runtime_config_patch"]["migration_admission_deadline"], 1500.0
        )
        self.assertEqual(
            result["readiness"]["archive_only_iterations"], ["0001", "0002"]
        )
        self.assertEqual(result["readiness"]["pending_human_tasks"], ["correction"])
        self.assertFalse(result["readiness"]["can_review"])
        validate_translation(
            self.inventory, result["translation"], result["runtime_config_patch"]
        )

    def test_real_sqlite_import_is_blocked_and_never_reopens_execute(self):
        result = self.translate()
        root = self.base / "native"
        store = StateStore.create(root, config=result["runtime_config_patch"])
        prepared = build_translation(
            root,
            self.inventory,
            result["translation"],
            result["runtime_config_patch"],
            "test-import",
        )
        with store._connection() as conn:
            conn.execute("UPDATE control SET admission_mode='inspect_only'")
            install_translation(conn, prepared)
            conn.commit()
        with store.coordinator() as owner:
            iteration = owner.snapshot()["iterations"][0]
            self.assertEqual(iteration["status"], "blocked")
            operation = Operations(owner).get(prepared["operations"][0]["id"])
            self.assertTrue(operation["submit_forbidden"])
            with self.assertRaises(InvalidTransition):
                Operations(owner).reserve_submit(operation["id"], operation["revision"])
            call = cleanup_action(
                "inspection",
                operation,
                Operations(owner).request(operation["id"]),
                {"inventory": self.inventory, "backend_controls": self.selectors},
            )
            self.assertFalse(call["data"]["terminal"])
            self.assertEqual(owner.snapshot()["counters"]["calls"], 0)
            operations = Operations(owner)
            config = {
                "inventory": self.inventory,
                "backend_controls": self.selectors,
                "receipt_path": str(self.external("actual-current", self.receipt())),
            }
            for action in ("inspection", "settlement", "collection"):
                operation = operations.get(operation["id"])
                result = cleanup_action(
                    action, operation, operations.request(operation["id"]), config
                )
                operations.record_receipt(
                    operation["id"],
                    operation["revision"],
                    action,
                    result["event_key"],
                    operations.envelope(operation["id"], result["data"]),
                )
            operation = operations.get(operation["id"])
            self.assertEqual(operation["state"], "terminal")
            self.assertEqual(operation["held_resources"], {})
            self.assertEqual(operation["actual_costs"], {"gpu_seconds": 8})
            self.assertEqual(operation["collection_state"], "unavailable")
            operations.finalize_feedback(operation["execute_attempt_id"])
            self.assertEqual(owner.snapshot()["iterations"][0]["status"], "blocked")
            self.assertEqual(owner.snapshot()["counters"]["calls"], 0)
            changed = self.receipt() | {"job_id": "unexpected-other-job"}
            self.external("actual-current", changed)
            observed = cleanup_action(
                "inspection", operation, operations.request(operation["id"]), config
            )
            operations.record_receipt(
                operation["id"],
                operation["revision"],
                "inspection",
                observed["event_key"],
                operations.envelope(operation["id"], observed["data"]),
            )
            quarantined = operations.get(operation["id"])
            self.assertIsNotNone(quarantined["inconsistency"])
            self.assertIn("unexpected-other-job", quarantined["unexpected_job_ids"])
            with self.assertRaisesRegex(ValueError, "same job"):
                cleanup_action(
                    "settlement",
                    quarantined,
                    operations.request(operation["id"]),
                    config,
                )
            self.assertEqual(
                operations.get(operation["id"])["actual_costs"], {"gpu_seconds": 8}
            )

    def test_missing_receipt_is_unknown_and_no_action_can_submit(self):
        op, request, config = self.inputs()
        config["receipt_path"] = str(self.control / "not-created.json")
        result = cleanup_action("inspection", op, request, config)
        self.assertFalse(result["data"]["terminal"])
        self.assertIsNone(result["data"]["not_submitted_proof"])
        self.assertIsNone(result["data"]["job_id"])
        for action in ("prepare", "submit", "submission", "cancellation"):
            with (
                self.subTest(action=action),
                self.assertRaisesRegex(ValueError, "never prepares or submits"),
            ):
                cleanup_action(action, op, request, config)
        with self.assertRaisesRegex(ValueError, "guessed zero"):
            cleanup_action("settlement", op, request, config)

    def test_explicit_original_receipt_closes_known_job_and_preserves_actual_overrun(
        self,
    ):
        op, request, config = self.inputs()
        receipt = self.receipt()
        receipt["gpu_seconds"] = 45
        path = self.external("actual-current", receipt)
        config["receipt_path"] = str(path)
        inspected = cleanup_action("inspection", op, request, config)
        self.assertTrue(inspected["data"]["terminal"])
        self.assertEqual(inspected["data"]["outcome"], "succeeded")
        self.assertTrue(inspected["data"]["physical_released"])
        with self.assertRaisesRegex(ValueError, "prior inspection"):
            cleanup_action("settlement", op, request, config)
        op.update(
            state="terminal", job_id=receipt["job_id"], terminal_outcome="succeeded"
        )
        settled = cleanup_action("settlement", op, request, config)
        self.assertEqual(settled["data"]["final_costs"], {"gpu_seconds": 45})
        self.assertEqual(cleanup_action("inspection", op, request, config), inspected)
        op.update(state="terminal", physical_released=True)
        collection = cleanup_action("collection", op, request, config)
        self.assertEqual(collection["data"]["artifacts"], [])
        self.assertEqual(collection["data"]["evaluations"], [])
        path.unlink()
        with self.assertRaisesRegex(ValueError, "already terminal"):
            cleanup_action("inspection", op, request, config)

    def test_receipt_cannot_change_original_binding_or_renew_deadline(self):
        op, request, config = self.inputs()
        receipt = self.receipt()
        for key, changed in (
            ("attempt", "different-attempt"),
            ("study_root", "/different-study"),
            ("grant_id", "different-scope"),
            ("plan", self.plans[3] | {"markdown": "changed"}),
            ("deadline", 801.0),
        ):
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(ValueError, "binding|deadline"),
            ):
                cleanup_action(
                    "inspection",
                    op,
                    request,
                    config,
                    query_original=lambda _: {
                        "event_key": "actual-observation",
                        "receipt": receipt | {key: changed},
                    },
                )
        self.assertEqual(op["state"], "unknown")
        self.assertEqual(op["held_costs"], {"gpu_seconds": 30})

    def test_positive_no_submit_proof_does_not_invent_zero_fees(self):
        op, request, config = self.inputs()
        absent = self.receipt() | {
            "job_id": None,
            "terminal_state": None,
            "exit_code": None,
            "gpu_seconds": None,
            "not_submitted_proof": {
                "basis": "backend_no_late_create",
                "reference": "trusted-original-controller-proof",
            },
        }

        def query(_):
            return {"event_key": "absence-observation", "receipt": absent}

        result = cleanup_action("inspection", op, request, config, query_original=query)
        self.assertEqual(result["data"]["outcome"], "not_submitted")
        with self.assertRaisesRegex(ValueError, "prior inspection"):
            cleanup_action("settlement", op, request, config, query_original=query)
        op.update(state="terminal", terminal_outcome="not_submitted")
        with self.assertRaisesRegex(ValueError, "existing positive reconciliation"):
            cleanup_action("settlement", op, request, config, query_original=query)
        op["no_submit_proof"] = result["data"]["not_submitted_proof"]
        with self.assertRaisesRegex(ValueError, "actual cumulative GPU seconds"):
            cleanup_action("settlement", op, request, config, query_original=query)
        absent["gpu_seconds"] = 0
        self.assertEqual(
            cleanup_action("settlement", op, request, config, query_original=query)[
                "data"
            ],
            {"final_costs": {"gpu_seconds": 0}},
        )

    def test_prior_job_coverage_cannot_be_omitted_to_reset_costs(self):
        self.selectors["jobs"] = []
        self.selectors["accounting"].update(
            settled_gpu_seconds=0, admitted_operations=1
        )
        with self.assertRaisesRegex(ValueError, "cover every captured"):
            self.translate()

    def test_terminal_primary_job_cannot_hide_unresolved_extra_job_exposure(self):
        record = self.inventory["annex"]["records"]["job-first"]
        original = json.loads(record["text"])
        for field, identities in (
            ("unexpected_job_ids", ["unresolved-extra-job"]),
            ("aggregate_jobs", [{"job_id": "unresolved-extra-job"}]),
        ):
            record["text"] = json.dumps(original | {field: identities})
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(ValueError, "extra or aggregate jobs"),
            ):
                self.translate()

    def test_never_submitted_cannot_cover_positive_or_unbound_history(self):
        reference = self.inventory["annex"]["records"]["job-second"]
        job = json.loads(reference["text"])
        reference["text"] = json.dumps(job | {"job_id": "positive-unresolved-job"})
        with self.assertRaisesRegex(ValueError, "Positive historical jobs"):
            self.translate()
        reference["text"] = json.dumps(job)
        proof_record = self.inventory["annex"]["records"]["no-submit"]
        original = json.loads(proof_record["text"])
        for key in ("study_root", "attempt", "grant_id", "plan", "execution"):
            proof_record["text"] = json.dumps(
                {name: value for name, value in original.items() if name != key}
            )
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(ValueError, "original execution binding"),
            ):
                self.translate()

    def test_bound_structured_operator_authorization_is_retained_without_inference(
        self,
    ):
        record = self.inventory["annex"]["records"]["no-submit"]
        assertion = json.loads(record["text"])
        assertion["authorization"] = {"operator": "synthetic trusted assertion"}
        record["text"] = json.dumps(assertion)
        self.assertEqual(
            self.translate()["readiness"]["no_submission_assertion_refs"],
            ["annex:no-submit"],
        )

    def test_nested_v1_assertion_uses_exact_original_job_and_grant_without_new_proof(
        self,
    ):
        original_job = json.loads(
            self.inventory["annex"]["records"]["job-second"]["text"]
        )
        assertion = {
            "version": 1,
            "effect": "never_submitted",
            "original_job": original_job,
            "authorization": self.grant,
            "response": {"outcome": "incomplete", "artifacts": [], "measurements": []},
        }
        record = self.inventory["annex"]["records"]["no-submit"]
        record["text"] = json.dumps(assertion)
        result = self.translate()
        expected = {
            "source_ref": "annex:no-submit",
            "source_profile": "protok-original-job-v1",
            "binding_pointer": "/original_job",
            "authorization_pointer": "/authorization",
            "semantic_kind": "legacy_operator_assertion",
        }
        self.assertEqual(result["readiness"]["no_submission_scopes"], [expected])
        operation = result["translation"]["executions"][0]["operations"][0]
        self.assertEqual(
            operation["origin"]["metadata"]["historical_no_submission_scopes"],
            [expected],
        )
        self.assertEqual(operation["state"], "unknown")
        self.assertNotIn("no_submit_proof", operation)
        self.assertIsNone(
            result["translation"]["executions"][0]["execute"]["normalized_output"]
        )
        record["text"] = json.dumps(
            assertion | {"original_job": original_job | {"unrecorded_extra": True}}
        )
        with self.assertRaisesRegex(ValueError, "exact captured original job"):
            self.translate()
        record["text"] = json.dumps(
            assertion | {"authorization": self.grant | {"gpu_seconds": 201}}
        )
        with self.assertRaisesRegex(ValueError, "exact original grant"):
            self.translate()

    def test_settlement_requires_matching_inspected_job_and_terminal_classification(
        self,
    ):
        op, request, config = self.inputs()
        receipt = self.receipt()
        config["receipt_path"] = str(self.external("actual-current", receipt))
        op.update(
            state="terminal", job_id="different-known-job", terminal_outcome="succeeded"
        )
        with self.assertRaisesRegex(ValueError, "same job"):
            cleanup_action("settlement", op, request, config)
        op.update(job_id=receipt["job_id"], terminal_outcome="failed")
        with self.assertRaisesRegex(ValueError, "terminal outcome"):
            cleanup_action("settlement", op, request, config)

    def test_cleanup_configuration_is_pure_and_rejects_invalid_binding(self):
        _, _, config = self.inputs()
        config["receipt_path"] = str(self.control / "not-read.json")
        with (
            patch.object(
                Path, "read_bytes", side_effect=AssertionError("external read")
            ),
            patch.object(Path, "stat", side_effect=AssertionError("external stat")),
        ):
            validated = validate_cleanup_config(config)
        self.assertEqual(validated, config)
        for malformed in (
            {},
            config | {"receipt_path": "relative.json"},
            config | {"inventory": {}},
        ):
            with self.subTest(), self.assertRaises(ValueError):
                validate_cleanup_config(malformed)

    def test_local_receipt_reader_rejects_symlink_and_midread_change(self):
        op, _, _ = self.inputs()
        binding = op["origin"]["metadata"]["binding"]
        path = self.external("actual-current", self.receipt())
        link = self.control / "linked.json"
        link.symlink_to(path)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            query_local_receipt(binding, str(link))
        read = Path.read_bytes

        def changed(source):
            result = read(source)
            source.write_text('{"changed": true}')
            return result

        with (
            patch.object(Path, "read_bytes", changed),
            self.assertRaisesRegex(ValueError, "changed during"),
        ):
            query_local_receipt(binding, str(path))

    def test_captured_annex_normalization_preserves_body_and_detaches(self):
        record = self.inventory["annex"]["records"]["authorization"]
        base = copy.deepcopy(self.inventory)
        base.pop("annex")
        capture = {
            "version": 1,
            "source_root": base["source_root"],
            "extra_control_refs": {
                "authorization": {
                    "source_ref": record["source_path"],
                    "identity": record["identity"],
                    "text": record["text"],
                }
            },
        }
        with patch.object(
            Path, "read_bytes", side_effect=AssertionError("transferred source reread")
        ):
            attached = with_captured_controls(base, capture)
        self.assertEqual(attached["annex"]["records"]["authorization"], record)
        self.assertNotIn("annex", base)
        capture["extra_control_refs"]["authorization"]["text"] = "mutated"
        self.assertEqual(
            attached["annex"]["records"]["authorization"]["text"], record["text"]
        )

    def test_outer_completed_plan_without_outer_process_keeps_legacy_semantics(self):
        for number in (1, 2, 3):
            directory = self.states[number]["plan_ref"].rsplit("/", 1)[0]
            (self.root / directory / "process.json").unlink()
            self.write(
                directory + "/completed.json",
                {"version": 1, "run_id": None, "response": self.plans[number]},
            )
        annex = self.inventory["annex"]
        self.inventory = capture_legacy(self.root)
        self.inventory["annex"] = annex
        result = self.translate()
        phase = result["translation"]["executions"][0]["plan"]
        self.assertEqual(phase["normalized_output"], self.plans[3])
        self.assertFalse(
            any(ref.endswith("/process.json") for ref in phase["source_refs"])
        )
        self.assertIsNone(
            result["translation"]["executions"][0]["execute"]["normalized_output"]
        )


if __name__ == "__main__":
    unittest.main()
