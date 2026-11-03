import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from research_loop import Study, initialize
from research_loop.exploration import (
    allocation,
    route_candidates,
    stage_schema,
    validate_exploration,
)
from research_loop.harness import HarnessConfig, publish_completed_response
from research_loop.long_training import stage_schema as long_training_schema
from research_loop.protocol import SCHEMAS, validate_response
from research_loop.scout import REPORT_MAX_BYTES, ScoutManager
from test_core import ProcessLost, RecordedHarness, read
from test_scout import FakeHarness


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
        result = super().run(**kwargs)
        validate_response(phase, result, schema=schema)
        return result

    def response(self, phase, context, kwargs):
        result = super().response(phase, context, kwargs)
        slot = context.get("exploration")
        if slot and phase == "proposal":
            result["research_design"] = {
                "question": "Is condition information lost?",
                "structural_change": "Inject the unchanged condition at each layer",
                "prior_attempts": ["Historical architecture comparison"],
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

    def test_model_cannot_supply_controller_allocation_label(self):
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

    def test_allocation_is_constructed_without_mutating_raw_proposal(self):
        s, h = self.study()
        s.run()
        call = next(c for c in h.calls if c["phase"] == "proposal")
        raw = read(call["run_dir"] / "response.json")
        self.assertNotIn("kind", raw["research_design"])
        self.assertNotIn(
            "kind", call["schema"]["properties"]["research_design"]["properties"]
        )
        selected = read(s.root / "iterations/0001/selection.json")
        canonical = selected["rounds"]["0"]["proposals"]["a"]
        self.assertEqual(
            canonical["research_design"]["kind"],
            "architecture",
        )
        self.assertIs(call["schema"].validate(raw), raw)
        full_schema = stage_schema("proposal", call["context"], SCHEMAS)
        self.assertIs(full_schema.validate(canonical), canonical)
        with self.assertRaisesRegex(ValueError, "kind"):
            full_schema.validate(raw)
        with self.assertRaisesRegex(ValueError, "kind"):
            call["schema"].validate(canonical)
        self.assertIn(
            "kind",
            full_schema.model.model_fields["research_design"].annotation.model_fields,
        )

    def test_legacy_completed_allocation_requires_original_kind(self):
        for kind in ("architecture", "refinement"):
            with self.subTest(kind=kind):
                self.root = self.root.parent / kind
                s, h = self.study()
                response = h.response

                def legacy_response(phase, context, kwargs):
                    result = response(phase, context, kwargs)
                    if phase == "proposal":
                        result["research_design"]["kind"] = context["exploration"][
                            "kind"
                        ]
                    return result

                def crash(call):
                    publish_completed_response(
                        call["run_dir"], read(call["run_dir"] / "response.json")
                    )
                    raise ProcessLost()

                def legacy_schema(phase, context):
                    return long_training_schema(
                        phase, context, stage_schema(phase, context, SCHEMAS)
                    )

                h.response, h.after = legacy_response, crash
                with (
                    patch.object(s, "_agent_schema", side_effect=legacy_schema),
                    self.assertRaises(ProcessLost),
                ):
                    s.run()
                state = s.status()
                key = state["last_call_key"]
                attempt = self.root / state["calls"][key]
                receipt = read(attempt / "completed.json")
                receipt["response"]["research_design"]["kind"] = kind
                (attempt / "completed.json").write_text(json.dumps(receipt))
                raw = (attempt / "completed.json").read_bytes()
                context = read(attempt / "context.json")
                resumed = Study(self.root, h)
                if kind == "architecture":
                    result = resumed._call(
                        state, "proposal", key, context, attempt.parent
                    )
                    self.assertEqual(result["research_design"]["kind"], kind)
                else:
                    with self.assertRaises(ValueError):
                        resumed._call(state, "proposal", key, context, attempt.parent)
                self.assertEqual((attempt / "completed.json").read_bytes(), raw)
                self.assertEqual(state["call_count"], 1)
                self.assertEqual(len(h.calls), 1)
                self.assertEqual(h.count("execute"), 0)

    def test_real_scout_publication_review_feedback_and_next_round_freezing(self):
        study, harness = self.study()
        external = FakeHarness()
        scout = ScoutManager(
            self.root.parent,
            config=HarnessConfig(),
            harness_factory=lambda config: external,
            deadline=time.time() + 60,
            min_interval_seconds=0,
        )
        self.addCleanup(scout.shutdown)

        def during_execute(call):
            if call["phase"] == "execute":
                self.assertTrue(scout.launch_if_due())
                scout._thread.join(3)
                self.assertFalse(scout.running)

        harness.before = during_execute
        study.run()
        self.assertTrue(
            all(c["context"]["scout_inputs"] is None for c in harness.calls)
        )
        first = read(self.root.parent / "control/scout/latest.json")
        self.assertIn(":I1", first["result"]["insights"][0]["id"])
        harness.calls.clear()
        study.run()
        self.assertTrue(
            all(c["context"]["scout_inputs"] == first for c in harness.calls)
        )
        second_context = read(external.calls[1]["run_dir"] / "context.json")
        self.assertIn("Mechanism", second_context["recent_insights"][0]["text"])
        self.assertIn(first["id"], second_context["previous_scout"]["text"])

    def test_oversized_scout_is_explicitly_unavailable_without_blocking(self):
        study, harness = self.study()
        path = self.root.parent / "control/scout/latest.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"summary": "x" * REPORT_MAX_BYTES}))
        study.run()
        self.assertEqual(study.graph()[0]["status"], "reviewed")
        self.assertIn(
            "128 KiB", harness.calls[0]["context"]["scout_inputs"]["unavailable"]
        )

    def test_policy_validation(self):
        for policy in ({}, {**POLICY, "cycle": []}, {**POLICY, "version": 9}):
            with self.assertRaises(ValueError):
                validate_exploration(policy)


if __name__ == "__main__":
    unittest.main()
