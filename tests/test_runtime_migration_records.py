"""Explicit synthetic legacy facts on the real SQLite operational path."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from examples.backend_lifecycle import CONFIG, REQUEST, accept_fixture
from research_loop.runtime.backend_scheduler import BackendScheduler
from research_loop.runtime.backends import BackendDriver
from research_loop.runtime.migration_records import (
    build_translation,
    install_translation,
    validate_translation,
)
from research_loop.runtime.operations import Operations
from research_loop.runtime.store import BudgetExceeded, InvalidTransition, StateStore


def translation():
    return {
        "version": 1,
        "translator": {"id": "synthetic-operator", "version": "1"},
        "source_refs": ["state.json"],
        "blockers": [],
        "authorities": [
            {
                "id": "fixture-grant",
                "admitted_operations": 1,
                "settled_costs": {},
                "blocked_reason": None,
            }
        ],
        "executions": [
            {
                "task": {
                    "id": "old-task",
                    "revision": 1,
                    "specification": {"requirement": "original"},
                    "source": "human",
                    "priority": 10,
                    "dependencies": [],
                    "cancelled": False,
                },
                "iteration": {
                    "id": "old-iteration",
                    "task_id": "old-task",
                    "task_revision": 1,
                    "revision": 1,
                },
                "plan": {
                    "id": "old-plan",
                    "normalized_output": {"timeout_seconds": 15},
                    "source_refs": ["state.json"],
                },
                "execute": {
                    "id": "old-execute",
                    "normalized_output": None,
                    "source_refs": ["state.json"],
                },
                "operations": [
                    {
                        "id": "old-operation",
                        "request": deepcopy(REQUEST),
                        "backend_version": "1",
                        "environment": {"id": "fixture-python", "version": "1"},
                        "job_id": None,
                        "unexpected_job_ids": [],
                        "submission_token": None,
                        "submit_owner_epoch": None,
                        "deadline": 12,
                        "state": "unknown",
                        "terminal_outcome": None,
                        "physical_released": False,
                        "held_resources": {"local-slot": 1},
                        "held_costs": {"fixture-unit": 1},
                        "actual_costs": {},
                        "external_open": True,
                        "collection_state": "pending",
                        "inconsistency": None,
                        "origin": {
                            "original_request_identity": "original-adapter-request",
                            "source_refs": ["state.json"],
                            "metadata": {"checkpoint_ref": "opaque-original-ref"},
                        },
                    }
                ],
            }
        ],
    }


class CleanupBackend:
    version = "1"
    environment = {"id": "fixture-python", "version": "1"}

    def inspect(self, request, operation):
        assert (
            operation["origin"]["original_request_identity"]
            == "original-adapter-request"
        )
        assert operation["submission_token"] is None
        return {
            "event_key": "original-inspection",
            "data": {
                "job_id": "original-job",
                "terminal": True,
                "outcome": "succeeded",
                "physical_released": True,
                "not_submitted_proof": None,
            },
        }

    def settle(self, request, operation):
        return {
            "event_key": "original-fees",
            "data": {
                "final_costs": {"fixture-unit": 1},
            },
        }

    def prepare(self, *args):
        raise AssertionError("No new preparation")

    def submit(self, *args):
        raise AssertionError("No new submission")


class MigrationRecordsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inventory = {
            "source_root": "/synthetic/legacy",
            "source_identity": {"device": 1, "inode": 2},
            "records": {"state.json": {"identity": {"size": 2}, "text": "{}"}},
        }
        self.config = deepcopy(CONFIG)
        self.store = StateStore.create(self.root, config=self.config, clock=lambda: 10)
        with self.store._connection() as conn:
            conn.execute(
                "UPDATE control SET admission_mode='inspect_only',calls=7,iterations=3,elapsed_seconds=4"
            )
            conn.commit()

    def install(self, value=None):
        prepared = build_translation(
            self.root, self.inventory, value or translation(), self.config, "import-one"
        )
        with self.store._connection() as conn:
            with patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("Writer must not read bodies"),
            ):
                result = install_translation(conn, prepared)
            conn.commit()
        return prepared, result

    def test_incomplete_binding_cleanup_preserves_original_identity_and_budgets(self):
        prepared, result = self.install()
        self.assertEqual(result["operations"], ["old-operation"])
        with self.store._connection(readonly=True) as conn:
            self.assertEqual(
                tuple(
                    conn.execute(
                        "SELECT calls,iterations,elapsed_seconds FROM control"
                    ).fetchone()
                ),
                (7, 3, 4),
            )
            self.assertEqual(
                conn.execute("SELECT status FROM iterations").fetchone()[0], "blocked"
            )
            phase = conn.execute(
                "SELECT * FROM attempts WHERE id='old-execute'"
            ).fetchone()
            self.assertEqual(
                (phase["status"], phase["origin_kind"], phase["output_ref"]),
                ("unavailable", "legacy", None),
            )
        binding = json.loads(
            (self.root / prepared["bindings"][0]["output_ref"]).read_text()
        )
        self.assertEqual(binding["origin"]["translator"], translation()["translator"])
        with self.store.coordinator() as owner:
            operations = Operations(owner)
            driver = BackendDriver(owner, {"local-fixture": CleanupBackend()})
            for method in (driver.prepare, driver.submit):
                with self.assertRaises(InvalidTransition):
                    method("old-operation")
            op = operations.get("old-operation")
            self.assertFalse(BackendScheduler._allowed(op, "submit"))
            self.assertTrue(BackendScheduler._allowed(op, "inspection"))
            driver.inspect("old-operation")
            self.assertEqual(operations.get("old-operation")["job_id"], "original-job")
            driver.settle("old-operation")
            op = operations.get("old-operation")
            self.assertEqual(op["held_resources"], {})
            self.assertEqual(op["held_costs"], {})
            self.assertEqual(op["actual_costs"], {"fixture-unit": 1})
            self.assertEqual(owner.snapshot()["counters"]["calls"], 7)

    def test_exact_install_replay_does_not_double_holds_or_counts(self):
        prepared, receipt = self.install()
        with self.store._connection() as conn:
            self.assertEqual(install_translation(conn, prepared), receipt)
            self.assertEqual(
                conn.execute(
                    "SELECT admitted_operations FROM operation_authorities"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT held FROM operation_pool_balances").fetchone()[0],
                "1",
            )
            conn.commit()

    def test_unrepresentable_or_reset_accounting_rejected(self):
        changes = [
            lambda v: v["translator"].update(id=" "),
            lambda v: v.update(source_refs=["uncaptured.json"]),
            lambda v: v["authorities"][0].update(admitted_operations=0),
            lambda v: v["executions"][0]["operations"][0].update(held_costs={}),
            lambda v: v["executions"][0]["operations"][0]["environment"].update(
                version="changed"
            ),
            lambda v: v["executions"][0]["operations"][0].update(
                collection_state="collected"
            ),
        ]
        for change in changes:
            value = translation()
            change(value)
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_translation(self.inventory, value, self.config)

    def test_inventory_blockers_and_missing_execution_cannot_be_waived_by_empty_translation(
        self,
    ):
        value = translation()
        value["executions"] = []
        self.inventory["blockers"] = ["unknown_adapter_control"]
        self.inventory["history"] = {
            "iterations": [
                {
                    "id": "0001",
                    "publication": "absent",
                    "metadata_ref": "iterations/0001/iteration.json",
                }
            ]
        }
        prepared = build_translation(
            self.root, self.inventory, value, self.config, "import-one"
        )
        self.assertEqual(
            prepared["blockers"],
            ["unknown_adapter_control", "untranslated_execution:0001"],
        )

    def test_terminal_unreleased_requires_exposure_and_overrun_blocks_authority(self):
        value = translation()
        op = value["executions"][0]["operations"][0]
        op.update(state="terminal", terminal_outcome="succeeded", external_open=False)
        with self.assertRaises(ValueError):
            validate_translation(self.inventory, value, self.config)
        op.update(external_open=True, actual_costs={"fixture-unit": 2}, held_costs={})
        value["authorities"][0]["settled_costs"] = {"fixture-unit": 2}
        self.config["operation_authorizations"]["fixture-grant"]["cost_limits"] = {
            "fixture-unit": 10
        }
        prepared = build_translation(
            self.root, self.inventory, value, self.config, "overrun"
        )
        authority = prepared["authorities"]["fixture-grant"]
        self.assertEqual(
            authority["blocked_reason"], "actual_cost_exceeded_bound_or_authority"
        )
        self.assertEqual(authority["costs"]["fixture-unit"]["settled"], 2)

    def test_imported_execution_collects_then_native_review_publishes(self):
        from research_loop.runtime.contexts import RuntimeContexts
        from research_loop.runtime.files import publish_artifact
        from research_loop.runtime.measurements import MeasurementRegistry
        from research_loop.runtime.publication import Publisher
        from research_loop.runtime.updates import CallIdentity, Updater

        value = translation()
        execution = value["executions"][0]
        execution["execute"]["normalized_output"] = {"operations": [deepcopy(REQUEST)]}
        self.install(value)
        with self.store._connection() as conn:
            conn.execute("UPDATE control SET admission_mode='active'")
            conn.commit()
        with self.store.coordinator() as owner:
            ops = Operations(owner)
            BackendDriver(owner, {"local-fixture": CleanupBackend()}).inspect(
                "old-operation"
            )
            artifact = {
                "path": "synthetic/original-result.bin",
                "version": "original-v1",
            }
            receipt = publish_artifact(
                self.root,
                {
                    "version": 2,
                    "iteration_id": "old-iteration",
                    "execute_attempt_id": "old-execute",
                    "candidate_id": "original-candidate",
                    "artifact": artifact,
                    "evaluation_id": self.config["global_evaluation"]["id"],
                    "comparison_scope": self.config["global_evaluation"][
                        "comparison_scope"
                    ],
                    "evaluator": {"id": "synthetic-original-evaluator", "version": "1"},
                    "metrics": {"score": 0.6},
                    "operation_id": "old-operation",
                    "collection_event_key": "original-collection",
                },
            )
            op = ops.get("old-operation")
            ops.record_receipt(
                op["id"],
                op["revision"],
                "collection",
                "original-collection",
                ops.envelope(
                    op["id"],
                    {
                        "artifacts": [artifact],
                        "evaluations": [
                            {"receipt_ref": receipt, "allow_feedback": True}
                        ],
                    },
                ),
            )
            measurement = MeasurementRegistry(owner).register("old-iteration", receipt)
            ops.finalize_feedback("old-execute")
            attempt = owner.begin_attempt(
                "review",
                request_id="native-review",
                iteration_id="old-iteration",
                expected_revision=1,
            )
            call = CallIdentity.from_attempt(owner.snapshot()["study_id"], attempt)
            package = RuntimeContexts(owner).build(call)
            self.assertEqual(
                package.context["plan"], execution["plan"]["normalized_output"]
            )
            output = {
                "knowledge": [],
                "reviews": [
                    {
                        "measurement_id": measurement["id"],
                        "valid": True,
                        "verified": True,
                        "reason": "Synthetic trusted original receipt",
                    }
                ],
                "corrections": [],
            }
            updater = Updater(
                owner, validators={"review": lambda result, context: result}
            )
            updater.bind_context(call, package)
            proposal = updater.validate_output(call, output, [receipt])
            updater.apply_update(proposal, proposal.revisions)
            published = Publisher(owner).publish(call)
            self.assertEqual(published["publication_id"], 1)
            self.assertFalse(ops.get("old-operation")["costs_final"])
            with self.store._connection(readonly=True) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM attempts WHERE origin_kind='native' AND role IN ('plan','execute')"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM operation_events WHERE kind IN ('prepare','submission')"
                    ).fetchone()[0],
                    0,
                )

    def test_multiple_original_jobs_quarantine_shared_pool_after_primary_release(self):
        value = translation()
        op = value["executions"][0]["operations"][0]
        op.update(
            job_id="original-job",
            unexpected_job_ids=["second-original-job"],
            inconsistency="conflicting_job_identity",
        )
        self.config["resource_pools"]["local-slot"] = 2
        self.config["operation_authorizations"]["independent-grant"] = deepcopy(
            self.config["operation_authorizations"]["fixture-grant"]
        )
        value["authorities"].append(
            {
                "id": "independent-grant",
                "admitted_operations": 0,
                "settled_costs": {},
                "blocked_reason": None,
            }
        )
        prepared, _ = self.install(value)
        self.assertEqual(
            prepared["pools"]["local-slot"]["blocked_reason"],
            "conflicting_job_identity",
        )
        with self.store._connection() as conn:
            conn.execute(
                "UPDATE control SET admission_mode='active',config_json=?",
                (json.dumps(self.config),),
            )
            conn.commit()
        with self.store.coordinator() as owner:
            operations = Operations(owner)
            BackendDriver(owner, {"local-fixture": CleanupBackend()}).inspect(
                "old-operation"
            )
            current = operations.get("old-operation")
            self.assertEqual(current["unexpected_job_ids"], ["second-original-job"])
            self.assertFalse(current["physical_released"])
            self.assertTrue(current["external_open"])
            self.assertEqual(current["held_resources"], {"local-slot": 1})
            self.assertEqual(current["held_costs"], {"fixture-unit": 1})
            with self.store._connection(readonly=True) as conn:
                pool = conn.execute(
                    "SELECT * FROM operation_pool_balances WHERE pool='local-slot'"
                ).fetchone()
                self.assertEqual(
                    (pool["held"], pool["blocked_reason"]),
                    ("1", "conflicting_job_identity"),
                )
                independent = conn.execute(
                    "SELECT blocked_reason FROM operation_authorities WHERE id='independent-grant'"
                ).fetchone()
                self.assertIsNone(independent[0])
            owner.create_task("independent", {})
            iteration = owner.claim_task("independent")
            accept_fixture(owner, iteration["id"], "plan", {"timeout_seconds": 15})
            request = deepcopy(REQUEST)
            request["authorization_id"] = "independent-grant"
            execute = accept_fixture(
                owner, iteration["id"], "execute", {"operations": [request]}
            )
            native = operations.get(f"{execute.attempt_id}:arithmetic")
            operations.record_receipt(
                native["id"],
                native["revision"],
                "prepare",
                "independent-prepare",
                operations.envelope(
                    native["id"],
                    {
                        "ready": True,
                        "reason": "Synthetic free preparation",
                        "effective_parameters": request["parameters"],
                    },
                ),
            )
            native = operations.get(native["id"])
            with self.assertRaisesRegex(BudgetExceeded, "Physical resource"):
                operations.reserve_submit(native["id"], native["revision"])
            self.assertIsNone(operations.get(native["id"])["submission_token"])

    def test_uncapped_count_preserves_history_and_cost_admission(self):
        value = translation()
        self.config["operation_authorizations"]["fixture-grant"]["max_operations"] = (
            None
        )
        value["authorities"][0]["admitted_operations"] = 50
        original = value["executions"][0]["operations"][0]
        original.update(
            state="terminal",
            terminal_outcome="succeeded",
            physical_released=True,
            external_open=False,
            held_resources={},
            held_costs={},
            actual_costs={"fixture-unit": 0},
        )
        prepared, _ = self.install(value)
        self.assertIsNone(prepared["authorities"]["fixture-grant"]["max_operations"])
        self.assertEqual(prepared["blockers"], [])
        with self.store._connection() as conn:
            self.assertIsNone(
                conn.execute(
                    "SELECT max_operations FROM operation_authorities"
                ).fetchone()[0]
            )
            conn.execute(
                "UPDATE control SET admission_mode='active',config_json=?",
                (json.dumps(self.config),),
            )
            conn.commit()
        with self.store.coordinator() as owner:
            owner.create_task("new-work", {})
            iteration = owner.claim_task("new-work")
            accept_fixture(owner, iteration["id"], "plan", {"timeout_seconds": 15})
            requests = [
                {**deepcopy(REQUEST), "id": identity, "resources": {}}
                for identity in ("first", "second")
            ]
            execute = accept_fixture(
                owner, iteration["id"], "execute", {"operations": requests}
            )
            ops = Operations(owner)
            for request in requests:
                op = ops.get(f"{execute.attempt_id}:{request['id']}")
                ops.record_receipt(
                    op["id"],
                    op["revision"],
                    "prepare",
                    request["id"] + "-ready",
                    ops.envelope(
                        op["id"],
                        {
                            "ready": True,
                            "reason": "Synthetic free preparation",
                            "effective_parameters": request["parameters"],
                        },
                    ),
                )
            first = ops.get(f"{execute.attempt_id}:first")
            ops.reserve_submit(first["id"], first["revision"])
            second = ops.get(f"{execute.attempt_id}:second")
            with self.store._connection() as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT admitted_operations FROM operation_authorities"
                    ).fetchone()[0],
                    51,
                )
                conn.execute("UPDATE operation_authorities SET max_operations='51'")
                conn.commit()
            with self.assertRaisesRegex(BudgetExceeded, "exhausted"):
                ops.reserve_submit(second["id"], second["revision"])
            with self.store._connection() as conn:
                conn.execute("UPDATE operation_authorities SET max_operations=NULL")
                conn.commit()
            with self.assertRaisesRegex(BudgetExceeded, "[Cc]ost"):
                ops.reserve_submit(second["id"], second["revision"])
            self.assertIsNone(ops.get(second["id"])["submission_token"])

    def test_annex_control_references_are_selected_without_external_reads(self):
        self.inventory["annex"] = {
            "version": 1,
            "records": {
                "original-grant.json": {
                    "source_path": "/synthetic/external/grant.json",
                    "identity": {"size": 2},
                    "text": "{}",
                },
            },
        }
        value = translation()
        value["source_refs"].append("annex:original-grant.json")
        value["executions"][0]["operations"][0]["origin"]["source_refs"] = [
            "annex:original-grant.json"
        ]
        with patch.object(
            Path, "read_text", side_effect=AssertionError("No external body read")
        ):
            validated = validate_translation(self.inventory, value, self.config)
        self.assertEqual(validated["source_refs"][-1], "annex:original-grant.json")
        self.inventory["annex"]["records"]["../grant.json"] = self.inventory["annex"][
            "records"
        ]["original-grant.json"]
        with self.assertRaises(ValueError):
            validate_translation(self.inventory, value, self.config)

    def test_known_original_token_and_available_bindings_are_not_worker_receipts(self):
        value = translation()
        execution = value["executions"][0]
        execution["execute"]["normalized_output"] = {"operations": [deepcopy(REQUEST)]}
        execution["operations"][0].update(
            job_id="original-job",
            submission_token="original-token",
            submit_owner_epoch=None,
        )
        self.install(value)
        with self.store._connection(readonly=True) as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM iterations").fetchone()[0], "active"
            )
            row = conn.execute(
                "SELECT * FROM attempts WHERE id='old-execute'"
            ).fetchone()
            self.assertEqual((row["owner_epoch"], row["worker_token"]), (0, ""))
            self.assertEqual(
                json.loads(row["receipt_json"])["kind"], "legacy_phase_binding"
            )
        with self.store._connection() as conn:
            conn.execute("UPDATE control SET admission_mode='active'")
            conn.commit()
        with self.store.coordinator() as owner:
            operations = Operations(owner)
            op = operations.get("old-operation")
            self.assertEqual(op["submission_token"], "original-token")
            with self.assertRaisesRegex(InvalidTransition, "Imported"):
                operations.reserve_submit(op["id"], op["revision"])
            operations.record_receipt(
                op["id"],
                op["revision"],
                "inspection",
                "original-token-inspection",
                operations.envelope(
                    op["id"],
                    {
                        "job_id": "original-job",
                        "terminal": True,
                        "outcome": "succeeded",
                        "physical_released": True,
                        "not_submitted_proof": None,
                    },
                ),
            )
            self.assertEqual(operations.get(op["id"])["terminal_outcome"], "succeeded")
