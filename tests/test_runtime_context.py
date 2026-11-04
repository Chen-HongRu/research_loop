"""Focused checks of captured context, policy boundaries, and persisted reload."""

import copy
import math
import unittest

from research_loop.runtime.context import ContextPackage, ContextSnapshot, build_context


def source(key, allow=True):
    return dict(
        id=f"source-{key}",
        key=key,
        ref=f"evidence/{key}",
        revision=1,
        reason="controller supplied",
        allow_feedback=allow,
    )


class RuntimeContextTests(unittest.TestCase):
    def capture(self, **changes):
        values = dict(
            study_id="synthetic-study",
            revisions={"state": 3},
            inputs={
                "notes": "abcdef",
                "goal": {"text": "fixed"},
                "final": "private result",
            },
            sources=[source("goal"), source("notes"), source("final", False)],
            protected_keys=[],
            allowed_evidence=["evidence/notes"],
        )
        values.update(changes)
        return ContextSnapshot.capture(**values)

    def build(self, snapshot=None, policy=None):
        return build_context(
            snapshot or self.capture(),
            "plan",
            {"id": "task"},
            "policy/v1",
            {"calls": 4},
            policy=policy,
        )

    def test_default_exact_context_order_traceability_and_roundtrip(self):
        snapshot = self.capture()
        package = self.build(snapshot)
        self.assertEqual(
            package.context, {"notes": "abcdef", "goal": {"text": "fixed"}}
        )
        self.assertEqual(list(package.context), ["notes", "goal"])
        self.assertEqual(package.manifest["policy_ref"], "policy/v1")
        self.assertEqual(package.manifest["budget"], {"calls": 4})
        self.assertEqual(package.manifest["revisions"], {"state": 3})
        self.assertEqual(package.manifest["task"], {"id": "task"})
        self.assertIsNone(package.manifest["tokens"]["actual_usage"])
        self.assertEqual(ContextPackage.from_dict(package.to_dict()), package)
        self.assertEqual(self.build(snapshot), package)

    def test_default_preserves_input_order_independently_of_source_order(self):
        inputs = {"last_source": "first input", "first_source": "last input"}
        snapshot = self.capture(
            inputs=inputs,
            sources=[source("first_source"), source("last_source")],
            allowed_evidence=[],
        )
        package = self.build(snapshot)
        self.assertEqual(list(package.context.items()), list(inputs.items()))
        self.assertEqual(
            [entry["key"] for entry in package.manifest["sources"]],
            ["first_source", "last_source"],
        )

    def test_identity_and_provenance_strings_must_be_nonblank(self):
        for blank in ("", " \t\n"):
            with (
                self.subTest(field="study_id", blank=blank),
                self.assertRaises(ValueError),
            ):
                self.capture(study_id=blank)
            for field in ("id", "key", "ref", "reason"):
                with (
                    self.subTest(field=field, blank=blank),
                    self.assertRaises(ValueError),
                ):
                    self.capture(
                        inputs={"notes": "text"},
                        sources=[dict(source("notes"), **{field: blank})],
                        allowed_evidence=[],
                    )
            for field in ("study_id", "role", "policy_ref"):
                package = self.build().to_dict()
                package["manifest"][field] = blank
                with (
                    self.subTest(field=field, blank=blank),
                    self.assertRaises(ValueError),
                ):
                    ContextPackage.from_dict(package)
            with self.assertRaises(ValueError):
                build_context(self.capture(), blank, None, "policy/v1", {})
            with self.assertRaises(ValueError):
                build_context(self.capture(), "plan", None, blank, {})

    def test_original_and_exposed_values_cannot_mutate_capture(self):
        inputs = {"goal": {"text": ["fixed"]}}
        sources = [source("goal")]
        snapshot = self.capture(inputs=inputs, sources=sources, allowed_evidence=[])
        inputs["goal"]["text"].append("changed")
        sources[0]["ref"] = "changed"
        snapshot.to_dict()["inputs"]["goal"]["text"].clear()
        package = self.build(snapshot)
        expected = package.to_dict()
        package.context["goal"]["text"].clear()
        package.manifest["task"]["id"] = "changed"
        package.to_dict()["manifest"]["sources"].clear()
        self.assertEqual(package.to_dict(), expected)
        self.assertEqual(package.context["goal"]["text"], ["fixed"])

    def test_policy_sees_only_eligible_detached_inputs_and_sources(self):
        def policy(inputs, sources, budget):
            self.assertNotIn("final", inputs)
            self.assertEqual({s["key"] for s in sources}, {"goal", "notes"})
            inputs["goal"]["text"] = "rewritten"
            sources[0]["allow_feedback"] = False
            return {"notes": {"action": "omit", "reason": "irrelevant"}}

        package = self.build(policy=policy)
        self.assertEqual(package.context, {"goal": {"text": "fixed"}})
        self.assertEqual(package.manifest["allowed_evidence"], ["evidence/notes"])
        notes = next(s for s in package.manifest["sources"] if s["key"] == "notes")
        self.assertEqual(
            (notes["disposition"], notes["selection_reason"]), ("omitted", "irrelevant")
        )
        self.assertEqual(notes["provided_characters"], 0)

    def test_truncation_is_exact_prefix_and_deterministic(self):
        def policy(inputs, sources, budget):
            return {
                "notes": {
                    "action": "truncate",
                    "reason": "shorten optional notes",
                    "characters": 3,
                }
            }

        package = self.build(policy=policy)
        self.assertEqual(package, self.build(policy=policy))
        self.assertEqual(package.context["notes"], "abc")
        notes = next(s for s in package.manifest["sources"] if s["key"] == "notes")
        self.assertEqual(notes["disposition"], "truncated")
        self.assertEqual(
            (notes["original_characters"], notes["provided_characters"]), (6, 3)
        )

    def test_policy_receives_detached_selection_budget(self):
        budget = {"selection": {"characters": 3}}
        snapshot = self.capture()
        before = snapshot.to_dict()

        def policy(inputs, sources, selection_budget):
            self.assertEqual(selection_budget, budget)
            count = selection_budget["selection"]["characters"]
            selection_budget["selection"]["characters"] = 999
            return {
                "notes": {
                    "action": "truncate",
                    "reason": "selection budget",
                    "characters": count,
                }
            }

        package = build_context(
            snapshot, "plan", None, "policy/v1", budget, policy=policy
        )
        self.assertEqual(package.context["notes"], "abc")
        self.assertEqual(package.manifest["budget"], {"selection": {"characters": 3}})
        self.assertEqual(budget, {"selection": {"characters": 3}})
        self.assertEqual(snapshot.to_dict(), before)

    def test_protected_fields_cannot_be_selected_away(self):
        for key in (
            "goal",
            "plan",
            "global_evaluation",
            "permissions",
            "budget",
            "manual_task",
            "manual_task_constraints",
            "custom",
        ):
            snapshot = self.capture(
                inputs={key: "fixed"},
                sources=[source(key)],
                protected_keys=[key] if key == "custom" else [],
                allowed_evidence=[],
            )
            for action in ("omit", "truncate"):
                choice = {"action": action, "reason": "try changing"}
                if action == "truncate":
                    choice["characters"] = 1
                with (
                    self.subTest(key=key, action=action),
                    self.assertRaises(ValueError),
                ):
                    self.build(snapshot, lambda *_: {key: choice})
            with self.assertRaises(ValueError):
                self.capture(
                    inputs={key: "fixed"},
                    sources=[source(key, False)],
                    protected_keys=[key] if key == "custom" else [],
                    allowed_evidence=[],
                )

    def test_unknown_denied_or_rewriting_policy_selections_rejected(self):
        for key, choice in [
            ("unknown", {"action": "omit", "reason": "x"}),
            ("final", {"action": "include", "reason": "x"}),
            ("notes", {"action": "include", "reason": "x", "value": "new"}),
            ("notes", {"action": "truncate", "reason": "x", "characters": True}),
            ("goal", {"action": "truncate", "reason": "x", "characters": 1}),
        ]:
            with self.subTest(key=key, choice=choice), self.assertRaises(ValueError):
                self.build(policy=lambda *_: {key: choice})

    def test_policy_errors_propagate(self):
        def policy(*_):
            raise RuntimeError("policy failed")

        with self.assertRaisesRegex(RuntimeError, "policy failed"):
            self.build(policy=policy)

    def test_snapshot_metadata_and_finite_json_validation(self):
        for changes in [
            dict(allowed_evidence=["evidence/unknown"]),
            dict(allowed_evidence=["evidence/final"]),
            dict(protected_keys=["unknown"]),
            dict(inputs={"x": math.nan}),
            dict(inputs={1: "non-string key"}),
            dict(inputs={"x": (1, 2)}),
            dict(sources=[source("goal"), source("goal")]),
            dict(sources=[dict(source("notes"), allow_feedback="yes")]),
            dict(sources=[dict(source("notes"), extra="unknown")]),
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.capture(**changes)

    def test_reload_rejects_manifest_context_inconsistencies(self):
        original = self.build().to_dict()
        mutations = [
            lambda p: p["manifest"].update(version=2),
            lambda p: p["manifest"].update(protected_keys=[]),
            lambda p: p["manifest"].update(allowed_evidence=["unknown"]),
            lambda p: p["manifest"]["tokens"].update(actual_usage={"input_tokens": 2}),
            lambda p: p["context"].update(final="leaked"),
            lambda p: p["context"].pop("goal"),
            lambda p: p["context"].update(notes="wrong count"),
            lambda p: p["manifest"]["sources"][1].update(disposition="invented"),
        ]
        for mutate in mutations:
            value = copy.deepcopy(original)
            mutate(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                ContextPackage.from_dict(value)


if __name__ == "__main__":
    unittest.main()
