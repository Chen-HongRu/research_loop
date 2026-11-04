"""Default concurrent role contexts retain real receipt provenance and boundaries."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from examples.runtime_publication import EVALUATION, SyntheticSession
from research_loop.runtime.contexts import RuntimeContexts
from research_loop.runtime.publication import Publisher
from research_loop.runtime.store import StateStore
from research_loop.runtime.updates import CallIdentity, Updater


class RuntimeContextTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = StateStore.create(
            Path(temporary.name),
            config={
                "goal": "Synthetic role inputs",
                "permissions": {},
                "global_evaluation": EVALUATION,
            },
        )
        session = self.store.coordinator()
        self.owner = session.__enter__()
        self.addCleanup(lambda: session.__exit__(None, None, None))
        self.demo = SyntheticSession(self.owner)
        self.serial = 0

    def call(self, role, iteration=None):
        self.serial += 1
        scope = (
            {}
            if iteration is None
            else {
                "iteration_id": iteration["id"],
                "expected_revision": iteration["revision"],
            }
        )
        attempt = self.owner.begin_attempt(
            role, request_id=f"runtime-context-{self.serial}", **scope
        )
        return CallIdentity.from_attempt(self.owner.snapshot()["study_id"], attempt)

    def test_review_protects_own_unpublished_receipts_excludes_other_unpublished(self):
        own, measurement = self.demo.trial("own", 0.5)
        _, other = self.demo.trial("other", 0.7)
        call = self.call("review", own)
        package = RuntimeContexts(self.owner).build(call)
        key = f"measurement:{measurement['id']}"
        self.assertEqual(package.context[key]["metrics"], {"score": 0.5})
        self.assertIn(key, package.manifest["protected_keys"])
        self.assertIn(measurement["receipt_ref"], package.manifest["allowed_evidence"])
        self.assertNotIn(f"measurement:{other['id']}", package.context)
        self.assertNotIn(other["receipt_ref"], package.manifest["allowed_evidence"])
        self.assertIn("plan", package.manifest["protected_keys"])
        self.assertIn("execute", package.manifest["protected_keys"])
        Updater(self.owner, validators={}).bind_context(call, package)

    def test_policy_cannot_remove_own_review_measurement(self):
        iteration, measurement = self.demo.trial("own", 0.5)
        call = self.call("review", iteration)

        def omit(inputs, sources, budget):
            return {
                f"measurement:{measurement['id']}": {
                    "action": "omit",
                    "reason": "Bad fixture policy",
                }
            }

        with self.assertRaisesRegex(ValueError, "protected"):
            RuntimeContexts(self.owner, policy=omit).build(call)

    def test_scout_reads_published_measurement_provenance(self):
        iteration, measurement = self.demo.trial("published", 0.5)
        review = self.demo.review(iteration, measurement)
        Publisher(self.owner).publish(review)
        _, unpublished = self.demo.trial("unpublished", 0.8)
        package = RuntimeContexts(self.owner).build(self.call("scout"))
        self.assertIn(f"measurement:{measurement['id']}", package.context)
        self.assertIn(measurement["receipt_ref"], package.manifest["allowed_evidence"])
        self.assertNotIn(f"measurement:{unpublished['id']}", package.context)
        self.assertEqual(package.manifest["revisions"]["knowledge"], 1)

    def test_bound_context_ignores_new_publication_and_later_policy(self):
        call = self.call("scout")
        original = RuntimeContexts(self.owner).build(call)
        Updater(self.owner, validators={}).bind_context(call, original)
        iteration, measurement = self.demo.trial("later", 0.5)
        Publisher(self.owner).publish(self.demo.review(iteration, measurement))

        def must_not_run(*args):
            raise AssertionError("A bound context must not run a new selection policy")

        restored = RuntimeContexts(self.owner, policy=must_not_run).build(call)
        self.assertEqual(restored.to_dict(), original.to_dict())
        self.assertEqual(restored.manifest["revisions"]["knowledge"], 0)


if __name__ == "__main__":
    unittest.main()
