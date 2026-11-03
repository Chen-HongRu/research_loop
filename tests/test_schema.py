"""The same model governs CLI schemas and local structured-output acceptance."""

from copy import deepcopy
import json
import unittest

from pydantic import ValidationError

from research_loop.exploration import agent_schema, complete_output
from research_loop.protocol import SCHEMAS, context_schema, validate_response
from research_loop.research_contract import plan_schema
from research_loop.schema import OutputModel, OutputSchema, choices, omit_fields
from test_protocol import execution, plan


class SchemaTests(unittest.TestCase):
    def test_cli_projection_keeps_required_nullable_fields_and_json_only(self):
        schema = plan_schema(SCHEMAS["plan"])
        serialized = json.loads(json.dumps(schema))
        self.assertEqual(serialized, schema)
        self.assertNotIn("model", serialized)
        self.assertIn("execution", serialized["required"])
        self.assertIn({"type": "null"}, serialized["properties"]["execution"]["anyOf"])
        value = plan()
        value["execution"] = None
        schema.validate(value)
        del value["execution"]
        with self.assertRaises(ValidationError):
            schema.validate(value)

    def test_paired_dynamic_choices_are_shared_by_schema_and_validation(self):
        local = plan()["local_evaluation"]
        other = {**local, "id": "other", "comparison_scope": "other-scope"}
        schema = context_schema(
            "execute", {"plan": plan(), "global_evaluation": other}, SCHEMAS["execute"]
        )
        pairs = schema["properties"]["measurements"]["items"]["anyOf"]
        advertised = {
            (
                p["properties"]["evaluation_id"]["enum"][0],
                p["properties"]["comparison_scope"]["enum"][0],
            )
            for p in pairs
        }
        self.assertEqual(
            advertised,
            {(local["id"], local["comparison_scope"]), ("other", "other-scope")},
        )
        value = execution()
        self.assertIs(schema.validate(value), value)
        value["measurements"][0]["comparison_scope"] = "other-scope"
        with self.assertRaises(ValidationError):
            schema.validate(value)

    def test_model_omission_and_controller_completion_do_not_drift(self):
        context = {"selected": {"proposal": {"base_experiment": None}}}
        schema = agent_schema("plan", context, SCHEMAS["plan"])
        value = plan()
        del value["base_experiment"]
        validate_response("plan", value, schema=schema)
        with self.assertRaises(ValidationError):
            validate_response("plan", value)
        complete = complete_output("plan", context, value)
        validate_response("plan", complete)
        with self.assertRaises(ValidationError):
            validate_response("plan", complete, schema=schema)

    def test_strict_literals_do_not_equate_boolean_integer_or_float(self):
        class Markers(OutputModel):
            proposed: choices(True)
            step: choices(1, 2000)

        schema = OutputSchema(Markers)
        before = deepcopy(schema)
        self.assertEqual(schema["properties"]["proposed"]["enum"], [True])
        schema.validate({"proposed": True, "step": 1})
        for value in (
            {"proposed": 1, "step": 1},
            {"proposed": True, "step": True},
            {"proposed": True, "step": 1.0},
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                schema.validate(value)
        reduced = omit_fields(schema, "step")
        reduced.validate({"proposed": True})
        self.assertEqual(schema, before)

    def test_measurements_are_finite_strict_and_keep_recorded_number_types(self):
        for number in (True, "0.8", float("nan"), float("inf"), -float("inf")):
            value = execution()
            value["measurements"][0]["metrics"][0]["value"] = number
            with self.subTest(number=number), self.assertRaises(ValidationError):
                validate_response("execute", value)
        for number in (1, 0.8, 10**400):
            value = execution()
            value["measurements"][0]["metrics"][0]["value"] = number
            self.assertIs(validate_response("execute", value), value)
            self.assertIs(
                type(value["measurements"][0]["metrics"][0]["value"]), type(number)
            )

    def test_experiment_references_remain_full_matches(self):
        for reference in ("0001\n", "prefix0001", "0001suffix"):
            value = plan()
            value["references"] = [reference]
            with self.subTest(reference=reference), self.assertRaises(ValidationError):
                validate_response("plan", value)
