"""Knowledge predecessor authority follows the immutable selected role context."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from integrations.native_runtime import policy
from research_loop.runtime.context import ContextSnapshot, build_context
from research_loop.runtime.contexts import RuntimeContexts
from research_loop.runtime.publication import Publisher
from research_loop.runtime.store import StateStore
from research_loop.runtime.updates import CallIdentity, Updater


def knowledge(identity, revision=0, **changes):
    return (
        dict(
            id=identity,
            expected_revision=revision,
            kind="interpretation",
            statement="Synthetic attributed claim",
            conditions=["Synthetic protocol"],
            source_refs=[],
            measurement_ids=[],
            interpretation="",
            limitations=["Not independently reproduced"],
        )
        | changes
    )


class RuntimeKnowledgeScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.serial = 0

    def open_store(self, rule=None):
        config = {"goal": "Synthetic knowledge visibility", "permissions": {}}
        if rule is not None:
            config["role_access"] = {"scout": rule}
        self.store = StateStore.create(self.root, config=config, max_calls=100)
        session = self.store.coordinator()
        self.owner = session.__enter__()
        self.addCleanup(lambda: session.__exit__(None, None, None))
        self.publisher = Publisher(self.owner)
        self.updater = Updater(
            self.owner,
            validators={
                "scout": lambda output, ctx: policy.validate_output(
                    "scout", output, ctx
                )
            },
        )

    def bind(self, *, selection=None, legacy=False):
        self.serial += 1
        attempt = self.owner.begin_attempt(
            "scout", request_id=f"knowledge-scope-{self.serial}"
        )
        call = CallIdentity.from_attempt(self.owner.snapshot()["study_id"], attempt)
        if legacy:
            record = self.owner._feedback_snapshot(call.to_dict())
            inputs = dict(
                goal="Synthetic knowledge visibility",
                permissions={},
                task=None,
                budget=self.store.snapshot()["limits"],
            )
            snapshot = ContextSnapshot.capture(
                study_id=call.study_id,
                revisions=dict(
                    study=1,
                    task=None,
                    iteration=None,
                    knowledge=record["knowledge_version"],
                ),
                inputs=inputs,
                sources=[
                    dict(
                        id=key,
                        key=key,
                        ref=f"control:{key}",
                        revision=1,
                        reason="Synthetic controller input",
                        allow_feedback=True,
                    )
                    for key in inputs
                ],
                protected_keys=list(inputs),
                allowed_evidence=[],
            )
            package = build_context(snapshot, "scout", None, "legacy-policy", {})
        else:
            package = RuntimeContexts(
                self.owner,
                policy=(lambda *_: {"research": selection}) if selection else None,
            ).build(call)
        self.updater.bind_context(call, package)
        return call, package

    def accept(self, call, *entries):
        proposal = self.updater.validate_output(
            call, dict(knowledge=list(entries), reviews=[], corrections=[]), []
        )
        self.updater.apply_update(proposal, proposal.revisions)
        return call

    def publish(self, *entries):
        call, _ = self.bind()
        return self.publisher.publish(self.accept(call, *entries))

    def assert_hidden_update_rejected(self, call):
        result = self.publisher.publish(
            self.accept(call, knowledge("existing", 1), knowledge("independent"))
        )
        self.assertEqual(result["knowledge"], [{"id": "independent", "revision": 1}])
        self.assertEqual(len(result["conflicts"]), 1)
        conflict = result["conflicts"][0]
        self.assertEqual(
            (
                conflict["target_id"],
                conflict["expected_revision"],
                conflict["actual_revision"],
            ),
            ("existing", 1, 1),
        )
        self.assertIn("snapshot_revision_mismatch", conflict["reason"])
        self.assertEqual(
            [
                row["revision"]
                for row in self.publisher.snapshot()["knowledge"]
                if row["id"] == "existing"
            ],
            [1],
        )
        self.assertEqual(self.publisher.publish(call), result)

    def test_history_denied_cannot_guess_existing_revision_but_can_create(self):
        self.open_store({"published_history": False})
        self.publish(knowledge("existing"))
        call, package = self.bind()
        self.assertEqual(package.manifest["revisions"]["knowledge"], 1)
        self.assertNotIn("research", package.context)
        self.assert_hidden_update_rejected(call)

    def test_omitted_research_does_not_gain_authority_from_shared_frontiers_ref(self):
        self.open_store({})
        self.publish(knowledge("existing"))
        call, package = self.bind(
            selection={"action": "omit", "reason": "Outside this call's input"}
        )
        research = next(
            s for s in package.manifest["sources"] if s["key"] == "research"
        )
        self.assertNotIn("research", package.context)
        self.assertIn("frontiers", package.context)
        self.assertIn(research["ref"], package.manifest["allowed_evidence"])
        self.assert_hidden_update_rejected(call)

    def test_truncated_research_cannot_authorize_existing_revision(self):
        self.open_store({})
        self.publish(knowledge("existing"))
        call, package = self.bind(
            selection={"action": "truncate", "characters": 100, "reason": "Prefix only"}
        )
        self.assertEqual(len(package.context["research"]), 100)
        self.assert_hidden_update_rejected(call)

    def test_complete_visible_research_authorizes_expected_predecessor(self):
        self.open_store({})
        self.publish(knowledge("existing"))
        call, package = self.bind()
        self.assertIn("## existing", package.context["research"])
        result = self.publisher.publish(self.accept(call, knowledge("existing", 1)))
        self.assertEqual(result["knowledge"], [{"id": "existing", "revision": 2}])
        self.assertEqual(result["conflicts"], [])

    def test_frozen_visible_predecessor_keeps_global_concurrency_check(self):
        self.open_store({})
        self.publish(knowledge("existing"))
        first, _ = self.bind()
        second, second_package = self.bind()
        self.accept(first, knowledge("existing", 1))
        self.accept(second, knowledge("existing", 1), knowledge("independent"))
        self.publisher.publish(first)
        self.assertEqual(second_package.manifest["revisions"]["knowledge"], 1)
        result = self.publisher.publish(second)
        self.assertEqual(result["knowledge"], [{"id": "independent", "revision": 1}])
        self.assertEqual(result["conflicts"][0]["reason"], "revision_mismatch")
        self.assertEqual(result["conflicts"][0]["actual_revision"], 2)

    def test_visible_snapshot_does_not_authorize_guessed_newer_revision(self):
        self.open_store({})
        self.publish(knowledge("existing"))
        stale, _ = self.bind()
        self.publish(knowledge("existing", 1))
        result = self.publisher.publish(self.accept(stale, knowledge("existing", 2)))
        self.assertEqual(result["knowledge"], [])
        self.assertEqual(result["conflicts"][0]["actual_revision"], 2)
        self.assertIn("snapshot_revision_mismatch", result["conflicts"][0]["reason"])

    def test_unconfigured_legacy_context_retains_revision_compatibility(self):
        self.open_store()
        self.publish(knowledge("existing"))
        call, package = self.bind(legacy=True)
        self.assertNotIn("research", package.context)
        result = self.publisher.publish(self.accept(call, knowledge("existing", 1)))
        self.assertEqual(result["knowledge"], [{"id": "existing", "revision": 2}])
        self.assertEqual(result["conflicts"], [])

    def test_configured_historical_receipt_does_not_require_new_scope_authority(self):
        self.open_store({})
        call, _ = self.bind()
        result = self.publisher.publish(self.accept(call, knowledge("existing")))
        self.publish(knowledge("existing", 1))
        with patch.object(
            Updater,
            "_load_binding",
            side_effect=AssertionError(
                "Historical receipt must not request new authority"
            ),
        ):
            self.assertEqual(self.publisher.publish(call), result)


if __name__ == "__main__":
    unittest.main()
