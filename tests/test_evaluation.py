from copy import deepcopy
import unittest

from research_loop.evaluation import build_frontier, validate_evaluation


def definition():
    return {
        "id": "global-v1",
        "comparison_scope": "test-split-evaluator-v1",
        "protocol": "Fixed checkpoint, seed aggregate, and precision rules.",
        "data_role": "development",
        "allow_feedback": True,
        "objectives": [
            {"name": "accuracy", "direction": "maximize", "unit": "fraction"},
            {"name": "latency", "direction": "minimize", "unit": "ms"},
        ],
        "constraints": [
            {"name": "memory", "operator": "le", "value": 8, "unit": "GB"},
            {"name": "coverage", "operator": "ge", "value": 1, "unit": "fraction"},
        ],
    }


def candidate(name, accuracy=0.9, latency=10, **updates):
    record = {
        "id": name,
        "experiment_id": "0001",
        "artifact": {"path": "checkpoints/model", "version": name},
        "evaluation_id": "global-v1",
        "comparison_scope": "test-split-evaluator-v1",
        "metrics": {
            "accuracy": accuracy,
            "latency": latency,
            "memory": 8,
            "coverage": 1,
        },
        "evidence": ["iterations/0001/execute/response.json"],
        "verified": True,
        "valid": True,
    }
    record.update(updates)
    return record


class EvaluationTests(unittest.TestCase):
    def test_definition_exact_fields_and_semantics(self):
        value = definition()
        self.assertIs(validate_evaluation(value), value)
        invalid = []
        for name in value:
            modified = deepcopy(value)
            del modified[name]
            invalid.append(modified)
        invalid.extend(
            [
                {**value, "extra": True},
                {**value, "id": " "},
                {**value, "comparison_scope": ""},
                {**value, "protocol": None},
                {**value, "data_role": "training"},
                {**value, "allow_feedback": 1},
                {**value, "data_role": "final"},
                {**value, "objectives": []},
                {**value, "constraints": {}},
            ]
        )
        for kind, field, bad in (
            ("objectives", "direction", "up"),
            ("objectives", "unit", ""),
            ("objectives", "name", ""),
            ("objectives", "extra", 1),
            ("constraints", "operator", "lt"),
            ("constraints", "value", float("inf")),
            ("constraints", "value", True),
        ):
            modified = deepcopy(value)
            modified[kind][0][field] = bad
            invalid.append(modified)
        duplicate = deepcopy(value)
        duplicate["constraints"][0]["name"] = "accuracy"
        invalid.append(duplicate)
        for modified in invalid:
            with self.subTest(value=modified), self.assertRaises(ValueError):
                validate_evaluation(modified)
        validate_evaluation({**value, "data_role": "final", "allow_feedback": False})

    def test_mixed_objective_pareto_ties_diagnostics_and_order(self):
        records = [
            candidate("slow", 0.99, 15),
            candidate("dominated", 0.8, 12),
            candidate("fast", 0.9, 8),
            candidate("tie", 0.9, 8),
        ]
        records[-1]["metrics"]["diagnostic"] = -100
        view = build_frontier(definition(), records)
        self.assertEqual(view["candidate_ids"], ["fast", "slow", "tie"])
        self.assertEqual(view["excluded"], [{"id": "dominated", "reason": "dominated"}])
        self.assertEqual(view, build_frontier(definition(), list(reversed(records))))
        self.assertEqual(view["candidates"][0], records[2])
        view["candidates"][0]["artifact"]["version"] = "changed"
        self.assertEqual(records[2]["artifact"]["version"], "fast")

    def test_single_objective_keeps_all_best_ties(self):
        value = definition()
        value["objectives"] = value["objectives"][:1]
        records = [candidate("a"), candidate("b"), candidate("c", 0.8, 1)]
        self.assertEqual(build_frontier(value, records)["candidate_ids"], ["a", "b"])
        value["objectives"][0]["direction"] = "minimize"
        self.assertEqual(build_frontier(value, records)["candidate_ids"], ["c"])

    def test_eligibility_constraints_missing_and_nonfinite_values(self):
        records = [
            candidate("unverified", verified=False),
            candidate("invalid", valid=False),
            candidate("artifact", artifact={"path": "model", "version": ""}),
            candidate("evidence", evidence=[]),
            candidate("status", valid=1),
            candidate("missing", metrics={"accuracy": 1}),
            candidate("fields"),
            candidate("scope", comparison_scope="different-seed-rule"),
            candidate("evaluation", evaluation_id="global-v2"),
            candidate("memory"),
            candidate("coverage"),
        ]
        del records[6]["metrics"]
        records[-2]["metrics"]["memory"] = 9
        records[-1]["metrics"]["coverage"] = 0.99
        for name, result in (
            ("nan", float("nan")),
            ("inf", float("inf")),
            ("negative-inf", -float("inf")),
            ("boolean", True),
            ("string", "1"),
        ):
            record = candidate(name)
            record["metrics"]["accuracy"] = result
            records.append(record)
        view = build_frontier(definition(), records)
        self.assertEqual(view["candidate_ids"], [])
        reasons = {item["id"]: item["reason"] for item in view["excluded"]}
        self.assertEqual(reasons["unverified"], "unverified")
        self.assertEqual(reasons["invalid"], "invalid")
        self.assertEqual(reasons["artifact"], "missing_artifact_version")
        self.assertEqual(reasons["evidence"], "missing_evidence")
        self.assertEqual(reasons["status"], "invalid_status")
        self.assertEqual(reasons["fields"], "missing_fields:metrics")
        self.assertEqual(reasons["missing"], "missing_metrics:coverage,latency,memory")
        self.assertEqual(reasons["scope"], "comparison_scope_mismatch")
        self.assertEqual(reasons["evaluation"], "evaluation_mismatch")
        self.assertEqual(reasons["memory"], "infeasible:memory")
        self.assertEqual(reasons["coverage"], "infeasible:coverage")
        for name in ("nan", "inf", "negative-inf", "boolean", "string"):
            self.assertEqual(reasons[name], "nonfinite_metrics:accuracy")

    def test_scope_and_definition_cannot_supply_missing_checkpoint_metrics(self):
        records = [
            candidate("checkpoint-a", metrics={"accuracy": 1}),
            candidate(
                "checkpoint-b", metrics={"latency": 1, "memory": 1, "coverage": 1}
            ),
            candidate("other-scope", 1, 1, comparison_scope="other"),
            candidate("other-definition", 1, 1, evaluation_id="other"),
            candidate("eligible", 0.8, 10),
        ]
        self.assertEqual(
            build_frontier(definition(), records)["candidate_ids"], ["eligible"]
        )

    def test_invalidation_rebuild_restores_previously_dominated_candidate(self):
        records = [candidate("best", 1, 1), candidate("previous", 0.9, 10)]
        original = build_frontier(definition(), records)
        self.assertEqual(original["candidate_ids"], ["best"])
        records[0]["valid"] = False
        rebuilt = build_frontier(definition(), records)
        self.assertEqual(rebuilt["candidate_ids"], ["previous"])
        self.assertEqual(rebuilt["excluded"], [{"id": "best", "reason": "invalid"}])
        self.assertTrue(original["candidates"][0]["valid"])

    def test_empty_and_ambiguous_identity(self):
        view = build_frontier(definition(), [])
        self.assertEqual(view["candidate_ids"], [])
        self.assertEqual(view["excluded"], [])
        self.assertEqual(view["candidates"], [])
        for records in (
            [candidate("same"), candidate("same", valid=False)],
            [{}],
            [candidate(" ")],
            [None],
            {},
        ):
            with self.subTest(records=records), self.assertRaises(ValueError):
                build_frontier(definition(), records)


if __name__ == "__main__":
    unittest.main()
