"""Publication projections retain reviewed history and evidence attribution."""

from copy import deepcopy
import unittest

from research_loop.runtime.publication_views import build_views, render_research


def definition(name="global-eval", protocol="fixed protocol"):
    return dict(
        id=name,
        comparison_scope=f"scope/{name}",
        protocol=protocol,
        data_role="development",
        allow_feedback=True,
        objectives=[dict(name="score", direction="maximize", unit="fraction")],
        constraints=[],
    )


def measurement(name, score, *, level="global", route="route-a", evaluation=None):
    evaluation = evaluation or definition()
    return dict(
        id=name,
        iteration_id=f"iteration-{name}",
        candidate_id=f"candidate-{name}",
        route_id=route,
        level=level,
        evaluation_id=evaluation["id"],
        definition=evaluation,
        artifact=dict(path=f"artifacts/{name}", version="v1"),
        evaluator=dict(id="evaluator", version="v1"),
        metrics=dict(score=score),
        receipt_ref=f"receipts/{name}.json",
        identity={},
        plan_attempt_id=f"plan-{name}",
        execute_attempt_id=f"execute-{name}",
    )


def eligibility(
    name, revision=1, publication=1, valid=1, verified=1, index=0, kind="reviews"
):
    return dict(
        measurement_id=name,
        revision=revision,
        valid=valid,
        verified=verified,
        publication_id=publication,
        decision_kind=kind,
        decision_index=index,
    )


def publication(identity):
    return dict(
        id=identity,
        attempt_id=f"review-{identity}",
        payload_ref=f"publications/{identity}.json",
        identity={},
        receipt={},
    )


def snapshot(
    measurements=None, eligibility_rows=None, knowledge=None, conflicts=None, version=1
):
    return dict(
        version=version,
        publications=[publication(i) for i in range(1, version + 1)],
        knowledge=knowledge or [],
        eligibility=eligibility_rows or [],
        conflicts=conflicts or [],
        measurements=measurements or [],
    )


def entry(identity, kind="external", revision=0, measurement_ids=None):
    return dict(
        id=identity,
        expected_revision=revision,
        kind=kind,
        statement=f"Statement {identity}",
        conditions=["Only the recorded setting"],
        source_refs=[f"evidence/{identity}"],
        measurement_ids=measurement_ids or [],
        interpretation="A possible explanation, not established.",
        limitations=["Small sample; further evidence required."],
    )


def knowledge(value, revision=1, publication=1, index=0, role="scout"):
    return dict(
        id=value["id"],
        revision=revision,
        kind=value["kind"],
        source_role=role,
        publication_id=publication,
        entry_index=index,
    )


def batch(entries=None, reviews=None, corrections=None):
    return dict(
        knowledge=entries or [], reviews=reviews or [], corrections=corrections or []
    )


class RuntimeFrontierTests(unittest.TestCase):
    def test_global_local_routes_and_protocols_are_separate(self):
        rows = [
            measurement("global", 0.5),
            measurement(
                "local-a", 0.8, level="local", evaluation=definition("local-eval")
            ),
            measurement(
                "local-b",
                0.9,
                level="local",
                route="route-b",
                evaluation=definition("local-eval", "route-b-specific protocol"),
            ),
            measurement(
                "other-protocol",
                0.99,
                level="local",
                evaluation=definition("other-eval", "different protocol"),
            ),
            measurement("unreviewed", 1),
        ]
        state = snapshot(
            rows, [eligibility(row["id"], index=i) for i, row in enumerate(rows[:-1])]
        )
        before = deepcopy(state)
        views = build_views(state, definition())
        self.assertEqual(views["global"]["global-eval"]["candidate_ids"], ["global"])
        self.assertEqual(
            views["local"]["route-a"]["local-eval"]["candidate_ids"], ["local-a"]
        )
        self.assertEqual(
            views["local"]["route-b"]["local-eval"]["candidate_ids"], ["local-b"]
        )
        self.assertEqual(
            views["local"]["route-a"]["other-eval"]["candidate_ids"], ["other-protocol"]
        )
        self.assertEqual(state, before)
        candidate = views["global"]["global-eval"]["candidates"][0]
        self.assertEqual(candidate["artifact"], rows[0]["artifact"])
        self.assertEqual(candidate["evidence"], [rows[0]["receipt_ref"]])
        self.assertIs(candidate["valid"], True)
        candidate["metrics"]["score"] = -1
        self.assertEqual(state, before)

    def test_correction_restores_dominated_history_without_changing_measurements(self):
        rows = [measurement("old", 0.5), measurement("winner", 0.9)]
        initial = snapshot(rows, [eligibility("old"), eligibility("winner", index=1)])
        self.assertEqual(
            build_views(initial, definition())["global"]["global-eval"][
                "candidate_ids"
            ],
            ["winner"],
        )
        corrected = deepcopy(initial)
        corrected["version"] = 2
        corrected["publications"].append(publication(2))
        corrected["eligibility"].append(
            eligibility(
                "winner", revision=2, publication=2, valid=0, kind="corrections"
            )
        )
        view = build_views(corrected, definition())["global"]["global-eval"]
        self.assertEqual(view["candidate_ids"], ["old"])
        self.assertEqual(view["excluded"], [dict(id="winner", reason="invalid")])
        self.assertEqual(corrected["measurements"], initial["measurements"])
        self.assertEqual(view["candidates"][0]["metrics"], {"score": 0.5})

    def test_rejects_aliases_conflicting_definitions_and_malformed_references(self):
        for rows in [
            [measurement("local", 0.8, level="local")],
            [measurement("global", 0.5, evaluation=definition("different-global"))],
            [
                measurement("a", 0.5, level="local", evaluation=definition("local")),
                measurement(
                    "b",
                    0.6,
                    level="local",
                    evaluation=definition("local", "conflicting"),
                ),
            ],
            [dict(measurement("a", 0.5), evaluation_id="unknown")],
        ]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                build_views(
                    snapshot(
                        rows,
                        [
                            eligibility(row["id"], index=index)
                            for index, row in enumerate(rows)
                        ],
                    ),
                    definition(),
                )
        for change in [
            lambda s: s.update(version=2),
            lambda s: s["eligibility"][0].update(publication_id=99),
            lambda s: s["eligibility"][0].update(measurement_id="unknown"),
            lambda s: s["eligibility"][0].update(valid=True),
            lambda s: s["eligibility"].append(deepcopy(s["eligibility"][0])),
        ]:
            state = snapshot([measurement("a", 0.5)], [eligibility("a")])
            change(state)
            with self.subTest(state=state), self.assertRaises(ValueError):
                build_views(state, definition())

    def test_unordered_arrays_produce_identical_views(self):
        state = snapshot(
            [measurement("b", 0.5), measurement("a", 0.9)],
            [
                eligibility("a"),
                eligibility("b"),
                eligibility("a", 2, 2, valid=0, kind="corrections"),
            ],
            version=2,
        )
        expected = build_views(state, definition())
        for field in ("measurements", "eligibility", "publications"):
            state[field].reverse()
        self.assertEqual(build_views(state, definition()), expected)

    def test_research_renders_latest_revisions_categories_caveats_and_conflicts(self):
        external = entry("external")
        local = entry("local", "local_measurement", measurement_ids=["measurement"])
        interpretation = entry("interpretation", "interpretation")
        revised = dict(
            external, expected_revision=1, statement="Corrected external statement"
        )
        records = [
            knowledge(external),
            knowledge(local, index=1, role="review"),
            knowledge(interpretation, index=2),
            knowledge(revised, 2, 2, role="review"),
        ]
        conflict = dict(
            id=1,
            publication_id=2,
            target_kind="knowledge",
            target_id="external",
            expected_revision=0,
            actual_revision=2,
            reason="Stale revision",
            entry_index=1,
        )
        state = snapshot(
            [measurement("measurement", 0.8)],
            knowledge=records,
            conflicts=[conflict],
            version=2,
        )
        payloads = {
            1: batch([external, local, interpretation]),
            2: batch([revised, external]),
        }
        before = deepcopy(state)
        text = render_research(state, payloads)
        self.assertIn("Corrected external statement", text)
        self.assertNotIn("Statement external", text)
        for expected in [
            "Attribution: external evidence.",
            "Attribution: local measurement records.",
            "Interpretation status: unverified.",
            "source role: review; kind: local_measurement",
            "previous revision: 1; publication: 2",
            "Only the recorded setting",
            "Small sample; further evidence required.",
            "evidence/local",
            "- measurement",
            "Pending publication conflicts",
            "Stale revision",
        ]:
            self.assertIn(expected, text)
        self.assertEqual(state, before)
        for field in ("knowledge", "publications", "measurements", "conflicts"):
            state[field].reverse()
        self.assertEqual(
            render_research(state, dict(reversed(list(payloads.items())))), text
        )

    def test_research_rejects_unknown_or_malformed_payload_references(self):
        value = entry("external")
        state = snapshot(knowledge=[knowledge(value)])
        for payload in [
            {},
            {1: batch([value]), 2: batch()},
            {1: batch()},
            {1: batch([dict(value, id="other")])},
            {1: batch([dict(value, measurement_ids=["unknown"])])},
        ]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                render_research(state, payload)
        state["eligibility"] = [eligibility("m")]
        state["measurements"] = [measurement("m", 0.5)]
        with self.assertRaises(ValueError):
            render_research(
                state, {1: batch([value], reviews=[dict(measurement_id="other")])}
            )

    def test_research_checks_review_and_correction_against_eligibility(self):
        state = snapshot(
            [measurement("m", 0.5)],
            [eligibility("m"), eligibility("m", 2, 2, valid=0, kind="corrections")],
            version=2,
        )
        decision = dict(
            measurement_id="m", valid=True, verified=True, reason="reviewed"
        )
        correction = dict(
            measurement_id="m",
            expected_revision=1,
            valid=False,
            verified=True,
            reason="corrected",
            evidence_refs=["receipts/m.json"],
        )
        payloads = {1: batch(reviews=[decision]), 2: batch(corrections=[correction])}
        self.assertIn("Publication version: 2", render_research(state, payloads))
        payloads[2]["corrections"][0]["valid"] = True
        with self.assertRaises(ValueError):
            render_research(state, payloads)

    def test_research_keeps_measurement_statement_with_current_correction_reason(self):
        local = entry("observation", "local_measurement", measurement_ids=["cited"])
        local["statement"] = "Recorded score was 0.8 in the original evaluation."
        state = snapshot(
            [measurement("cited", 0.8), measurement("uncited", 0.7)],
            [
                eligibility("cited"),
                eligibility("uncited", index=1),
                eligibility("cited", 2, 2, valid=0, kind="corrections"),
            ],
            knowledge=[knowledge(local, role="review")],
            version=2,
        )
        reviewed = [
            dict(
                measurement_id=name,
                valid=True,
                verified=True,
                reason=f"Initial qualification for {name}",
            )
            for name in ("cited", "uncited")
        ]
        correction = dict(
            measurement_id="cited",
            expected_revision=1,
            valid=False,
            verified=True,
            reason="Evaluation input was contaminated.\nOriginal score retained.",
            evidence_refs=["receipts/cited.json"],
        )
        payloads = {
            1: batch([local], reviews=reviewed),
            2: batch(corrections=[correction]),
        }
        before = deepcopy(state)
        text = render_research(state, payloads)
        self.assertIn(local["statement"], text)
        self.assertIn("[Measurement eligibility](#measurement-eligibility)", text)
        cited = text.split("### cited\n", 1)[1].split("### uncited\n", 1)[0]
        self.assertIn("Valid: false; verified: true", cited)
        self.assertIn(
            "Decision: corrections; eligibility revision: 2; publication: 2", cited
        )
        self.assertIn(correction["reason"], cited)
        self.assertNotIn("Initial qualification for cited", text)
        uncited = text.split("### uncited\n", 1)[1]
        self.assertIn("Valid: true; verified: true", uncited)
        self.assertIn(
            "Decision: reviews; eligibility revision: 1; publication: 1", uncited
        )
        self.assertIn("Initial qualification for uncited", uncited)
        self.assertEqual(text.count("### cited\n"), 1)
        self.assertEqual(state, before)
        for field in ("knowledge", "measurements", "eligibility", "publications"):
            state[field].reverse()
        self.assertEqual(
            render_research(state, dict(reversed(list(payloads.items())))), text
        )

    def test_unreviewed_local_registration_does_not_change_publication_version_view(
        self,
    ):
        state = snapshot([measurement("published", 0.5)], [eligibility("published")])
        expected = build_views(state, definition())
        state["measurements"].append(
            measurement(
                "unreviewed", 999, level="local", evaluation=definition("new-local")
            )
        )
        self.assertEqual(build_views(state, definition()), expected)
        self.assertEqual(expected["local"], {})
        self.assertEqual(expected["version"], state["version"])
        self.assertNotIn("unreviewed", str(expected))
        empty = snapshot(version=0)
        empty_expected = build_views(empty, definition())
        empty["measurements"] = state["measurements"]
        self.assertEqual(build_views(empty, definition()), empty_expected)

    def test_empty_snapshot_has_empty_fixed_global_view(self):
        state = snapshot(version=0)
        self.assertEqual(
            build_views(state, definition())["global"]["global-eval"]["candidate_ids"],
            [],
        )
        self.assertIn("Publication version: 0", render_research(state, {}))


if __name__ == "__main__":
    unittest.main()
