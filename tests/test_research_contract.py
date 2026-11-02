import copy
import json
from pathlib import Path
import tempfile
import unittest

from research_loop import Study, initialize
from research_loop.exploration import guidance, stage_schema, validate_design
from research_loop.protocol import SCHEMAS, validate_response
from research_loop.research_contract import (
    validate_contract,
    validate_research_execution,
)
from research_loop.submission import claim_request, submit
from test_core import RecordedHarness, read
from test_protocol import plan


def contract():
    return {
        "purpose": "mechanism",
        "question": "Does conditioning control the measured representation?",
        "explanations": [
            {
                "id": "signal",
                "statement": "Representation uses conditioning",
                "prediction": "Shuffling changes alignment",
            },
            {
                "id": "shortcut",
                "statement": "Representation uses a shortcut",
                "prediction": "Shuffling preserves alignment",
            },
        ],
        "observables": [
            {
                "id": "alignment",
                "definition": "Paired alignment difference",
                "method": "Matched conditioning shuffle",
                "inputs": "One frozen checkpoint and held-out samples",
                "timing": "Before any training",
                "aggregation": "Paired mean with uncertainty",
                "cost": "One minute",
            }
        ],
        "interpretation": [
            {
                "explanation_id": "signal",
                "observable_ids": ["alignment"],
                "supports": "Difference interval above 0.1",
                "refutes": "Difference interval inside [-0.01, 0.01]",
            },
            {
                "explanation_id": "shortcut",
                "observable_ids": ["alignment"],
                "supports": "Difference interval inside [-0.01, 0.01]",
                "refutes": "Difference interval above 0.1",
            },
        ],
        "inconclusive": "Intervals cross the predefined decision regions",
        "execution": {
            "mode": "script",
            "entrypoint": "research/conditioning.py",
            "gpu": False,
            "time_limit_seconds": 60,
            "inputs": [
                {"name": "checkpoint", "source": "frozen-model", "role": "analysis"}
            ],
            "outputs": ["research/alignment.json"],
        },
    }


class ResearchHarness(RecordedHarness):
    def run(self, **kwargs):
        phase = next(p for p in SCHEMAS if f"Stage: {p}\n" in kwargs["prompt"])
        result = super().run(**kwargs)
        validate_response(phase, result, schema=kwargs["schema"])
        return result

    def response(self, phase, context, kwargs):
        result = super().response(phase, context, kwargs)
        if phase == "plan":
            result["research_contract"] = contract()
        elif phase == "execute":
            path = Path(kwargs["workspace"]) / "research/alignment.json"
            path.parent.mkdir(exist_ok=True)
            path.write_text('{"paired_difference":0.2}')
            result["artifacts"] = [
                {"path": "research/alignment.json", "version": "analysis-v1"}
            ]
            result["measurements"] = []
        return result


class ResearchContractTests(unittest.TestCase):
    def test_legacy_schemas_are_unchanged_and_only_optin_plan_extends(self):
        original = copy.deepcopy(SCHEMAS)
        for phase in SCHEMAS:
            self.assertIs(stage_schema(phase, {}, SCHEMAS), SCHEMAS[phase])
            enhanced = stage_schema(
                phase, {"research_execution": {"version": 1}}, SCHEMAS
            )
            if phase == "plan":
                self.assertIn("research_contract", enhanced["required"])
            else:
                self.assertIs(enhanced, SCHEMAS[phase])
        self.assertEqual(SCHEMAS, original)

    def test_policy_validation(self):
        for value in (
            {},
            {"version": True},
            {"version": 2},
            {"version": 1, "other": 1},
        ):
            with self.assertRaises(ValueError):
                validate_research_execution(value)
        self.assertIsNone(validate_research_execution(None))
        self.assertEqual(validate_research_execution({"version": 1}), {"version": 1})

    def test_mechanism_requires_contract_distinct_explanations_and_references(self):
        context = {"research_execution": {"version": 1}}
        value = plan()
        with self.assertRaises(ValueError):
            validate_design("plan", value, context)
        value["research_contract"] = contract()
        validate_design("plan", value, context)
        for mutate in (
            lambda c: c.update(explanations=[]),
            lambda c: c["explanations"][1].update(
                prediction=c["explanations"][0]["prediction"]
            ),
            lambda c: c.update(observables=[]),
            lambda c: c["interpretation"][0].update(observable_ids=["unknown"]),
            lambda c: c["interpretation"][0].update(explanation_id="unknown"),
            lambda c: c["interpretation"][0].update(supports=" "),
        ):
            invalid = {**value, "research_contract": contract()}
            mutate(invalid["research_contract"])
            with self.assertRaises(ValueError):
                validate_contract(invalid)

    def test_observation_theory_performance_need_evidence_not_competing_explanations(
        self,
    ):
        for purpose in ("performance", "observation", "theory"):
            value = contract()
            value.update(purpose=purpose, explanations=[], interpretation=[])
            validate_contract({"decision": "investigate", "research_contract": value})
        for decision in ("stop", "reselect"):
            value = {**plan(), "decision": decision, "research_contract": None}
            validate_response(
                "plan",
                value,
                schema=stage_schema(
                    "plan", {"research_execution": {"version": 1}}, SCHEMAS
                ),
            )
            validate_contract(value)

    def test_execution_bounds_and_paths(self):
        for change in (
            {"entrypoint": "../analysis.py"},
            {"entrypoint": "model.py"},
            {"outputs": ["../result.json"]},
            {"outputs": []},
            {"time_limit_seconds": 61},
            {"time_limit_seconds": 1860},
            {
                "inputs": [
                    {"name": "../checkpoint", "source": "frozen", "role": "analysis"}
                ]
            },
        ):
            value = contract()
            value["execution"].update(change)
            with self.assertRaises(ValueError):
                validate_contract(
                    {"decision": "investigate", "research_contract": value}
                )
        value = contract()
        value["execution"].update(
            mode="fixed_evaluation",
            entrypoint=None,
            inputs=[],
            outputs=[],
            gpu=True,
            time_limit_seconds=1800,
        )
        validate_contract({"decision": "investigate", "research_contract": value})
        for change in (
            {"gpu": False},
            {"time_limit_seconds": 60},
            {"outputs": ["fake.json"]},
        ):
            mismatch = copy.deepcopy(value)
            mismatch["execution"].update(change)
            with self.assertRaisesRegex(ValueError, "Fixed evaluation"):
                validate_contract(
                    {"decision": "investigate", "research_contract": mismatch}
                )

    def test_observation_proposal_does_not_require_competing_mechanisms(self):
        proposal = {
            "research_design": {
                "question": "Measure the geometry",
                "structural_change": "Add a measurement",
                "difference_from_prior": "Previously unmeasured",
                "minimal_test": "One frozen example",
                "contrasting_predictions": [],
            }
        }
        validate_design(
            "proposal",
            proposal,
            {
                "exploration": {"kind": "diagnostic"},
                "research_execution": {"version": 1},
            },
        )
        with self.assertRaisesRegex(ValueError, "contrasting predictions"):
            validate_design(
                "proposal", proposal, {"exploration": {"kind": "diagnostic"}}
            )

    def test_guidance_reserves_scientific_judgment_and_legacy_field(self):
        context = {"research_execution": {"version": 1}}
        self.assertIn("structural_change", guidance("proposal", context))
        for phase in ("judge", "review"):
            self.assertIn("not independent random seeds", guidance(phase, context))
            self.assertIn("RMSD-only", guidance(phase, context))
            self.assertIn("does not certify", guidance(phase, context))

    def test_artifact_only_research_runs_without_frontier_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "study"
            initialize(
                root,
                goal="Inspect conditioning evidence",
                research_execution={"version": 1},
                strategy={"selection": "direct", "review": "concise"},
            )
            harness = ResearchHarness()
            study = Study(root, harness)
            before = study.frontiers()
            study.run()
            self.assertEqual(study.graph()[0]["status"], "reviewed")
            self.assertEqual(study.frontiers(), before)
            folder = root / "iterations/0001"
            frozen = read(folder / "context/study.before.json")
            self.assertEqual(frozen["research_execution"], {"version": 1})
            self.assertTrue(
                all(
                    call["context"]["research_execution"] == {"version": 1}
                    for call in harness.calls
                )
            )
            execute = read(root / study.status()["execution_ref"])
            self.assertEqual(execute["measurements"], [])
            self.assertEqual(execute["artifacts"][0]["path"], "research/alignment.json")
            self.assertEqual(
                read(root / study.status()["plan_ref"])["research_contract"], contract()
            )

    def test_research_submission_is_exact_and_never_republished(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                submit(directory, "research"),
                {"status": "pending", "action": "research"},
            )
            request = Path(directory) / "evaluation-request.json"
            self.assertEqual(json.loads(request.read_text()), {"action": "research"})
            self.assertEqual(claim_request(directory), {"action": "research"})
            for _ in range(2):
                self.assertEqual(submit(directory, "research")["status"], "accepted")
                self.assertFalse(request.exists())


if __name__ == "__main__":
    unittest.main()
