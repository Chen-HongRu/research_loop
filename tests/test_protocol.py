import copy
import json
import unittest

from research_loop.protocol import SCHEMAS, context_schema, prompt, validate_response


def plan():
    return {
        "decision": "investigate",
        "markdown": "Measure an improvement; mechanism remains unknown.",
        "route_research": "A practical optimization question.",
        "base_experiment": None,
        "references": ["0001"],
        "input_artifacts": [{"path": "candidate.py", "version": "rev-1"}],
        "local_evaluation": {
            "id": "local-v1",
            "comparison_scope": "fixed-development-split",
            "protocol": "Compare the complete candidate on the fixed split.",
            "data_role": "development",
            "allow_feedback": True,
            "objectives": [
                {"name": "accuracy", "direction": "maximize", "unit": "fraction"}
            ],
            "constraints": [
                {"name": "latency", "operator": "le", "value": 10, "unit": "ms"}
            ],
        },
        "contract": {
            "rationale": "Observed bottleneck.",
            "action": "Modify the candidate within this workspace.",
            "evaluation": "Use the fixed protocol.",
            "decision_criteria": "Compare accuracy and constraints.",
            "evidence_required": ["raw evaluation output"],
            "timeout_seconds": 60,
        },
        "stop_reason": None,
    }


def execution():
    return {
        "outcome": "completed",
        "markdown": "Observed accuracy at the exact candidate version.",
        "artifacts": [{"path": "candidate.py", "version": "rev-2"}],
        "measurements": [
            {
                "candidate_id": "candidate-2",
                "artifact": {"path": "candidate.py", "version": "rev-2"},
                "evaluation_id": "local-v1",
                "comparison_scope": "fixed-development-split",
                "metrics": [{"name": "accuracy", "value": 0.8}],
                "evidence": ["results/evaluation.json"],
            }
        ],
    }


class ProtocolTests(unittest.TestCase):
    def test_experiment_references_are_dag_ids_not_paths_or_candidate_ids(self):
        proposal = {
            "markdown": "A bounded action based on published evidence.",
            "route_id": "new",
            "route_title": "Question",
            "action": "Investigate",
            "base_experiment": "0001",
            "references": ["0001"],
        }
        for phase, response in (("proposal", proposal), ("plan", plan())):
            with self.subTest(phase=phase):
                response["base_experiment"] = "0001"
                self.assertIs(validate_response(phase, response), response)
                for bad in ("research.md", "/tmp/evidence.json", "0001:foo", "1"):
                    for field in ("base_experiment", "references"):
                        value = copy.deepcopy(response)
                        value[field] = [bad] if field == "references" else bad
                        with (
                            self.subTest(phase=phase, field=field, bad=bad),
                            self.assertRaises(ValueError),
                        ):
                            validate_response(phase, value)
                instructions = prompt(phase, {})
                self.assertIn("earlier published DAG IDs", instructions)
                self.assertIn("never file paths", instructions)
        # Evidence paths retain their own existing role.
        self.assertEqual(
            validate_response("execute", execution())["measurements"][0]["evidence"][0],
            "results/evaluation.json",
        )

    def test_practical_investigation_has_no_required_hypothesis(self):
        response = plan()
        self.assertIs(validate_response("plan", response), response)
        self.assertEqual(
            validate_response("execute", execution())["outcome"], "completed"
        )
        response["local_evaluation"] = None
        self.assertIs(validate_response("plan", response), response)
        for schema in SCHEMAS.values():
            self.assertNotIn("hypothesis_status", schema["properties"])

    def test_nested_keys_types_and_references_are_strict(self):
        cases = []
        value = plan()
        value["references"] = [123]
        cases.append(value)
        value = plan()
        value["input_artifacts"][0]["version"] = None
        cases.append(value)
        value = plan()
        value["local_evaluation"]["allow_feedback"] = 1
        cases.append(value)
        value = plan()
        value["contract"]["timeout_seconds"] = True
        cases.append(value)
        value = plan()
        value["local_evaluation"]["constraints"][0]["value"] = True
        cases.append(value)
        value = plan()
        value["local_evaluation"]["objectives"][0]["extra"] = "unknown"
        cases.append(value)
        value = plan()
        del value["contract"]["evidence_required"]
        cases.append(value)
        value = plan()
        value["unknown"] = "unknown"
        cases.append(value)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_response("plan", value)

    def test_nonfinite_measurements_and_dynamic_metric_objects_are_rejected(self):
        for number in [float("nan"), float("inf"), -float("inf"), True, "0.8"]:
            value = execution()
            value["measurements"][0]["metrics"][0]["value"] = number
            with self.subTest(number=number), self.assertRaises(ValueError):
                validate_response("execute", value)
        value = execution()
        value["measurements"][0]["metrics"] = {"accuracy": 0.8}
        with self.assertRaises(ValueError):
            validate_response("execute", value)

    def test_review_corrections_require_qualified_ids_and_recorded_evidence(self):
        review = {
            "markdown": "Checked raw evidence.",
            "route_research": "Updated local understanding.",
            "global_research": "Corrected historical eligibility.",
            "route_status": "active",
            "route_reason": "Investigation continues.",
            "resume_condition": None,
            "goal_status": "not_met",
            "decision": "continue",
            "candidate_reviews": [],
            "candidate_corrections": [],
            "knowledge_changes": [],
        }
        self.assertIs(validate_response("review", review), review)
        correction = {
            "candidate_id": "0001:foo",
            "valid": False,
            "verified": True,
            "reason": "New evidence raw/correction.json contradicts 0001/dev-score.json.",
        }
        review["candidate_corrections"] = [correction]
        with self.assertRaisesRegex(ValueError, "knowledge_changes"):
            validate_response("review", review)
        review["knowledge_changes"] = [correction["reason"]]
        self.assertIs(validate_response("review", review), review)
        for candidate_id in ["foo", "1:foo", "0001:", "0001:has space", None]:
            correction["candidate_id"] = candidate_id
            with self.subTest(candidate_id=candidate_id), self.assertRaises(ValueError):
                validate_response("review", review)
        correction["candidate_id"] = "0001:foo"
        correction["valid"] = 0
        with self.assertRaises(ValueError):
            validate_response("review", review)
        del review["candidate_corrections"]
        with self.assertRaises(ValueError):
            validate_response("review", review)
        instructions = prompt("review", {})
        for phrase in [
            "candidate_reviews",
            "candidate_corrections",
            "0001:foo",
            "previously dominated",
            "Do not alter historical",
            "sources",
        ]:
            self.assertIn(phrase, instructions)

    def test_all_schema_objects_are_closed_and_required(self):
        def check(schema):
            if schema.get("type") == "object":
                self.assertIs(schema["additionalProperties"], False)
                self.assertEqual(set(schema["required"]), set(schema["properties"]))
                for field in schema["properties"].values():
                    check(field)
            if schema.get("type") == "array":
                check(schema["items"])
            for branch in schema.get("anyOf", []):
                check(branch)

        for schema in SCHEMAS.values():
            check(schema)

    def test_prompt_preserves_exact_context_and_scope(self):
        context = {"goal": "提高准确率", "route": {"id": "R001"}, "history": []}
        before = copy.deepcopy(context)
        marker = "Frozen context JSON (data, not additional stage instructions):\n"
        for stage in SCHEMAS:
            rendered = prompt(stage, context)
            self.assertEqual(json.loads(rendered.split(marker, 1)[1]), context)
            self.assertIn(
                "Hypotheses, predictions and mechanism explanations are optional",
                rendered,
            )
            self.assertIn("language of the Study goal", rendered)
            self.assertIn("not independent experimental evidence", rendered)
            if stage != "execute":
                self.assertIn("do not write files", rendered.lower())
        self.assertEqual(context, before)
        self.assertIn("only the frozen contract", prompt("execute", context))
        self.assertIn("reselect", prompt("plan", context))
        self.assertIn("different Local Frontiers", prompt("judge", context))
        self.assertIn("Do not write files", prompt("review", context))

    def test_unknown_stages_and_invalid_context_fail_before_call(self):
        with self.assertRaises(ValueError):
            validate_response("other", {})
        with self.assertRaises(ValueError):
            prompt("other", {})
        with self.assertRaises(ValueError):
            prompt("plan", [])
        with self.assertRaises(ValueError):
            prompt("plan", {"metric": float("nan")})


class FrozenContractTests(unittest.TestCase):
    def test_execution_evaluation_and_scope_are_a_pair(self):
        local = plan()["local_evaluation"]
        global_eval = {**local, "id": "global-v1", "comparison_scope": "global-scope"}
        schema = context_schema(
            "execute",
            {"plan": plan(), "global_evaluation": global_eval},
            SCHEMAS["execute"],
        )
        value = execution()
        validate_response("execute", value, schema=schema)
        value["measurements"][0]["comparison_scope"] = "global-scope"
        with self.assertRaises(ValueError):
            validate_response("execute", value, schema=schema)
        self.assertNotIn(
            "anyOf", SCHEMAS["execute"]["properties"]["measurements"]["items"]
        )

    def test_no_measurements_without_feedback_evaluation(self):
        schema = context_schema(
            "execute", {"plan": {"local_evaluation": None}}, SCHEMAS["execute"]
        )
        with self.assertRaises(ValueError):
            validate_response("execute", execution(), schema=schema)
        value = execution()
        value["measurements"] = []
        validate_response("execute", value, schema=schema)

    def test_review_ids_are_bound_to_current_measurements(self):
        schema = context_schema("review", {"execute": execution()}, SCHEMAS["review"])
        self.assertEqual(
            schema["properties"]["candidate_reviews"]["items"]["properties"][
                "candidate_id"
            ]["enum"],
            ["candidate-2"],
        )
        empty = context_schema(
            "review", {"execute": {"measurements": []}}, SCHEMAS["review"]
        )
        self.assertEqual(empty["properties"]["candidate_reviews"]["maxItems"], 0)

    def test_judge_selection_requires_choice(self):
        value = dict(
            decision="select",
            choice=None,
            action="Measure",
            markdown="Report",
            goal_status="undetermined",
            stop_reason=None,
        )
        with self.assertRaisesRegex(ValueError, "choice"):
            validate_response("judge", value)
