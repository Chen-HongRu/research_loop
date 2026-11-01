import json
import tempfile
import unittest
from pathlib import Path

from research_loop import Study, initialize
from research_loop.exploration import allocation, route_candidates, validate_exploration
from research_loop.protocol import SCHEMAS, validate_response
from test_core import RecordedHarness, read


POLICY = {
    "version": 1,
    "start_iteration": 1,
    "cycle": [
        "architecture",
        "diagnostic",
        "architecture",
        "refinement",
        "architecture",
    ],
    "priority_routes": ["R0002", "R0003"],
}


class ExplorationHarness(RecordedHarness):
    def run(self, **kwargs):
        phase = next(p for p in SCHEMAS if f"Stage: {p}\n" in kwargs["prompt"])
        schema = kwargs["schema"]
        result = super().run(**{**kwargs, "schema": SCHEMAS[phase]})
        validate_response(phase, result, schema=schema)
        return result

    def response(self, phase, context, kwargs):
        result = super().response(phase, context, kwargs)
        slot = context.get("exploration")
        if slot and phase == "proposal":
            result["research_design"] = {
                "kind": slot["kind"],
                "question": "Is condition information lost?",
                "structural_change": "Inject the unchanged condition at each layer",
                "prior_attempts": ["Historical X/H comparison"],
                "difference_from_prior": "Change conditioning, not the old readout",
                "contrasting_predictions": [
                    "Better under informative conditions",
                    "No change under shuffled conditions",
                ],
                "minimal_test": "One frozen-protocol candidate",
                "scope_requirements": [],
                "scout_idea_ids": [],
            }
        if slot and phase == "review":
            result["insight"] = {
                "question": "Is conditioning lost?",
                "observed": "A measured comparison",
                "supported": "This bounded intervention",
                "unresolved": "Mechanism",
                "next_discriminating_test": "A matched ablation",
                "actual_kind": slot["kind"],
                "allocation_respected": True,
                "allocation_reason": "Actual diff matches the question",
            }
        return result


class ExplorationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "study"

    def study(self):
        initialize(
            self.root,
            goal="Investigate conditional structures.",
            strategy={"selection": "direct", "review": "concise"},
            exploration=POLICY,
        )
        h = ExplorationHarness()
        h.stop_after = 20
        return Study(self.root, h), h

    def test_allocation_and_underexplored_route_are_not_score_greedy(self):
        self.assertEqual(
            [allocation(POLICY, i)["kind"] for i in range(1, 6)], POLICY["cycle"]
        )
        delayed = {**POLICY, "start_iteration": 4}
        self.assertIsNone(allocation(delayed, 3))
        routes = {r: {"status": "active"} for r in ("R0001", "R0002", "R0003")}
        history = [
            {"id": "0001", "route_id": "R0001"},
            {"id": "0002", "route_id": "R0003"},
        ]
        self.assertEqual(
            route_candidates(allocation(POLICY, 1), routes, history), ["R0002", "new"]
        )

    def test_complete_cycles_keep_review_receipts_and_insights(self):
        s, h = self.study()
        s.run(iterations=5)
        proposals = [c for c in h.calls if c["phase"] == "proposal"]
        self.assertEqual(
            [c["context"]["exploration"]["kind"] for c in proposals[::2]],
            POLICY["cycle"],
        )
        self.assertEqual(len(h.calls), 30)
        for i in range(1, 6):
            folder = self.root / "iterations" / f"{i:04d}"
            self.assertTrue((folder / "publication.json").exists())
            self.assertEqual(
                read(folder / "insight.json")["actual_kind"], POLICY["cycle"][i - 1]
            )

    def test_scout_arriving_mid_iteration_is_frozen_until_next_round(self):
        s, h = self.study()
        p = self.root.parent / "control/scout/latest.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"id": "old"}))

        def incoming(call):
            p.write_text(json.dumps({"id": "new"}))

        h.before = incoming
        s.run()
        self.assertTrue(
            all(c["context"]["scout_inputs"] == {"id": "old"} for c in h.calls)
        )
        h.calls.clear()
        s.run()
        self.assertTrue(
            all(c["context"]["scout_inputs"] == {"id": "new"} for c in h.calls)
        )

    def test_missing_or_corrupt_scout_does_not_block_research(self):
        s, h = self.study()
        p = self.root.parent / "control/scout/latest.json"
        p.parent.mkdir(parents=True)
        p.write_text("incomplete json")
        s.run()
        self.assertEqual(s.graph()[0]["status"], "reviewed")
        self.assertIn("unavailable", h.calls[0]["context"]["scout_inputs"])

    def test_refinement_cannot_satisfy_architecture_envelope(self):
        s, h = self.study()
        response = h.response

        def mislabeled(phase, context, kwargs):
            result = response(phase, context, kwargs)
            if phase == "proposal":
                result["research_design"]["kind"] = "refinement"
            return result

        h.response = mislabeled
        with self.assertRaises(ValueError):
            s.run()
        self.assertEqual(h.count("execute"), 0)

    def test_policy_validation(self):
        for policy in ({}, {**POLICY, "cycle": []}, {**POLICY, "version": 9}):
            with self.assertRaises(ValueError):
                validate_exploration(policy)


if __name__ == "__main__":
    unittest.main()
