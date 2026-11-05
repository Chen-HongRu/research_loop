"""Research audience projections from real SQLite and immutable acceptance."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from examples.runtime_publication import EVALUATION, SyntheticSession, make_store
from research_loop.runtime.overview import _frontier, overview_snapshot
from research_loop.runtime.planning import PlanningRunner
from research_loop.runtime.planning_state import PlanningLedger
from research_loop.runtime.publication import Publisher
from research_loop.runtime.store import ConflictError, StateStore
from tests.test_runtime_frontiers import (
    definition,
    eligibility,
    measurement,
    snapshot,
)


class RuntimeOverviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = make_store(self.root)
        self.session = self.store.coordinator()
        self.owner = self.session.__enter__()
        self.addCleanup(lambda: self.session.__exit__(None, None, None))
        self.driver = SyntheticSession(self.owner)
        self.publisher = Publisher(self.owner)

    def test_publication_order_correction_and_detached_cached_results(self):
        first, a = self.driver.trial("a", 1.0)
        second, b = self.driver.trial("b", 2.0)
        review_a = self.driver.review(first, a)
        review_b = self.driver.review(second, b)
        self.publisher.publish(review_b)
        self.publisher.publish(review_a)
        before = self.store.snapshot()
        initial = overview_snapshot(self.root)
        self.assertEqual(
            [point["best_value"] for point in initial["frontier"]["points"]],
            [2.0, 2.0],
        )
        self.assertEqual(
            initial["frontier"]["points"][0]["frontier"][0]["candidate_id"], "b"
        )
        self.assertEqual(self.store.snapshot(), before)
        initial["frontier"]["current"][0]["metrics"]["score"] = -10
        initial["knowledge"][0]["conditions"].append("Caller mutation")
        repeated = overview_snapshot(self.root)
        self.assertEqual(repeated["frontier"]["current"][0]["metrics"]["score"], 2.0)
        self.assertNotIn("Caller mutation", repeated["knowledge"][0]["conditions"])
        third, _ = self.driver.trial("correction")
        correction = dict(
            measurement_id=b["id"],
            expected_revision=1,
            valid=False,
            verified=True,
            reason="Withdraw the synthetic evaluation",
            evidence_refs=[b["receipt_ref"]],
        )
        self.publisher.publish(self.driver.review(third, corrections=[correction]))
        result = overview_snapshot(self.root)
        self.assertEqual(
            [point["best_value"] for point in result["frontier"]["points"]],
            [2.0, 2.0, 1.0],
        )
        self.assertEqual(result["frontier"]["current"][0]["candidate_id"], "a")
        observation = next(
            row for row in result["knowledge"] if row["id"] == "observation-b"
        )
        self.assertFalse(observation["measurement_status"][0]["valid"])

    def test_empty_research_does_not_acquire_owner_or_open_arbitrary_paths(self):
        self.owner.create_task(
            "safe-task",
            dict(
                instruction="Synthetic investigation",
                hypothesis="A bounded claim",
                intervention="Change one setting",
                secret="PRIVATE_SPEC",
                input_path="PRIVATE_PATH",
            ),
        )
        before = self.store.snapshot()
        with (
            patch.object(
                StateStore, "coordinator", side_effect=AssertionError("Owner acquired")
            ),
            patch.object(
                Path, "read_text", side_effect=AssertionError("Unexpected body read")
            ),
        ):
            result = overview_snapshot(self.root)
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(result["goal"], "Exercise local publication")
        self.assertEqual(result["frontier"]["points"], [])
        self.assertEqual(result["tasks"][0]["hypothesis"], "A bounded claim")
        self.assertNotIn("PRIVATE_", json.dumps(result, allow_nan=False))

    def test_immutable_payload_cache_keeps_identity_checks(self):
        iteration, value = self.driver.trial("measured", 0.5)
        self.publisher.publish(self.driver.review(iteration, value))
        result = overview_snapshot(self.root)
        with (
            patch.object(
                Path, "read_text", side_effect=AssertionError("Repeated body read")
            ),
            patch(
                "research_loop.runtime.overview.build_views",
                side_effect=AssertionError("Repeated history replay"),
            ),
        ):
            self.assertEqual(overview_snapshot(self.root), result)
        payload = self.publisher.snapshot()["publications"][0]["payload_ref"]
        path = self.root / payload
        path.chmod(0o644)
        path.write_text("{}")
        path.chmod(0o444)
        with self.assertRaises(ConflictError):
            overview_snapshot(self.root)

    def test_recorded_dependency_and_correction_have_separate_edge_types(self):
        self.owner.create_task("prior", {"instruction": "Earlier investigation"})
        self.owner.create_task(
            "followup",
            {"instruction": "Follow up", "corrects_tasks": ["prior", "missing"]},
            dependencies=["prior"],
        )
        result = overview_snapshot(self.root)
        self.assertEqual(
            result["graph"]["edges"],
            [
                dict(source="prior", target="followup", kind="task_dependency"),
                dict(source="prior", target="followup", kind="task_correction"),
            ],
        )
        self.assertIn("parentage is not inferred", result["graph"]["message"])

    def test_accepted_advocate_proposals_and_explicit_judge_round_association(self):
        spec = dict(
            instruction="A synthetic experiment",
            hypothesis="A testable claim",
            intervention="One bounded change",
            secret="PRIVATE_PROPOSAL",
        )
        proposal = dict(
            id="suggested",
            reason="Synthetic research value",
            priority=10,
            dependencies=[],
            specification=spec,
            raw_transcript="PRIVATE_RAW",
        )
        ledger = PlanningLedger(self.owner)
        ledger.trigger("start", "manual", {})

        def invoke(call, package):
            if call.role == "advocate":
                return dict(output={"proposals": [proposal]}, evidence_refs=[])
            return dict(
                output={
                    "changes": [
                        dict(
                            action="create",
                            id="chosen",
                            specification=spec,
                            priority=10,
                            dependencies=[],
                        )
                    ]
                },
                evidence_refs=[],
            )

        runner = PlanningRunner(
            self.owner,
            invoke,
            validators={
                "advocate": lambda output, _: output,
                "judge": lambda output, _: output,
            },
        )
        record = runner.run_once()
        self.assertEqual(record["status"], "applied")
        result = overview_snapshot(self.root)
        self.assertEqual(len(result["proposals"]), 2)
        self.assertEqual(result["proposals"][0]["reason"], proposal["reason"])
        self.assertEqual(result["proposals"][0]["hypothesis"], spec["hypothesis"])
        self.assertEqual(result["proposals"][0]["round_id"], record["id"])
        task = next(row for row in result["tasks"] if row["id"] == "chosen")
        self.assertEqual(task["origin_kind"], "judge")
        self.assertEqual(task["round_id"], record["id"])
        self.assertNotIn("PRIVATE_", json.dumps(result, allow_nan=False))

    def test_budget_uses_scoped_durable_ledger_and_actual_unit(self):
        with self.store._connection() as conn:
            conn.execute(
                "INSERT INTO operation_authorities VALUES(?,?,?,?,?,?)",
                ("gpu-grant", "[]", None, None, 0, None),
            )
            conn.execute(
                "INSERT INTO operation_scope_costs VALUES(?,?,?,?,?)",
                ("gpu-grant", "gpu_seconds", "100", "30", "20"),
            )
            conn.execute(
                "INSERT INTO operation_authorities VALUES(?,?,?,?,?,?)",
                ("other", "[]", None, None, 0, None),
            )
            conn.execute(
                "INSERT INTO operation_scope_costs VALUES(?,?,?,?,?)",
                ("other", "credits", "10", "0", "3"),
            )
        budgets = overview_snapshot(self.root)["budgets"]
        self.assertEqual(
            budgets,
            [
                dict(
                    authorization_id="gpu-grant",
                    unit="gpu_seconds",
                    limit=100,
                    held=30,
                    settled=20,
                    remaining=50,
                ),
                dict(
                    authorization_id="other",
                    unit="credits",
                    limit=10,
                    held=0,
                    settled=3,
                    remaining=7,
                ),
            ],
        )
        with self.store._connection() as conn:
            conn.execute(
                "UPDATE operation_scope_costs SET settled='40',held='10' WHERE authorization_id='gpu-grant'"
            )
        self.assertEqual(overview_snapshot(self.root)["budgets"][0]["settled"], 40)

    def test_frontier_scope_feasibility_and_unreviewed_exclusions(self):
        fixed = definition()
        fixed["constraints"] = [
            dict(name="cost", operator="le", value=10, unit="seconds")
        ]
        rows = [
            measurement("global", 0.5, evaluation=fixed),
            measurement("unreviewed", 999, evaluation=fixed),
            measurement("infeasible", 99, evaluation=fixed),
            measurement(
                "local", 999, level="local", evaluation=definition("local-eval")
            ),
        ]
        for row in rows:
            row["metrics"]["cost"] = 11 if row["id"] == "infeasible" else 1
        state = snapshot(
            rows,
            [
                eligibility("global"),
                eligibility("infeasible", index=1),
                eligibility("local", index=2),
            ],
        )
        _, view = _frontier(state, fixed)
        self.assertEqual(view["points"][0]["best_value"], 0.5)
        self.assertEqual(
            view["excluded"],
            [dict(measurement_id="infeasible", reason="infeasible:cost")],
        )
        self.assertNotIn("unreviewed", json.dumps(view))
        self.assertNotIn("local", json.dumps(view))

    def test_pareto_has_no_scalar_best_and_minimize_keeps_direction(self):
        fixed = definition()
        fixed["objectives"].append(
            dict(name="cost", direction="minimize", unit="seconds")
        )
        rows = [
            measurement("a", 1, evaluation=fixed),
            measurement("b", 2, evaluation=fixed),
        ]
        rows[0]["metrics"]["cost"] = 1
        rows[1]["metrics"]["cost"] = 2
        state = snapshot(rows, [eligibility("a"), eligibility("b", index=1)])
        _, view = _frontier(state, fixed)
        self.assertEqual(view["mode"], "pareto")
        self.assertIsNone(view["points"][0]["best_value"])
        self.assertEqual(len(view["current"]), 2)
        fixed = definition()
        fixed["objectives"][0]["direction"] = "minimize"
        for row in rows:
            row["definition"] = fixed
        _, view = _frontier(state, fixed)
        self.assertEqual(view["points"][0]["best_value"], 1)

    def test_guarded_and_final_metrics_are_hidden(self):
        fixed = deepcopy(EVALUATION)
        for role in ("development", "final"):
            fixed.update(data_role=role, allow_feedback=False)
            _, view = _frontier(snapshot(version=0), fixed)
            self.assertEqual(view["status"], "feedback_hidden")
            self.assertEqual(view["points"], [])
        fixed = definition()
        hidden = measurement("hidden", 999)
        hidden["definition"] = dict(
            definition("hidden"), data_role="final", allow_feedback=False
        )
        hidden["evaluation_id"] = "hidden"
        state = snapshot(
            [measurement("visible", 1), hidden],
            [eligibility("visible"), eligibility("hidden", index=1)],
        )
        _, view = _frontier(state, fixed)
        self.assertNotIn("999", json.dumps(view))
        self.assertNotIn("hidden", json.dumps(view))


if __name__ == "__main__":
    unittest.main()
