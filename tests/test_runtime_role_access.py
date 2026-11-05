"""Configured role access narrows normal runtime inputs without losing own evidence."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from examples.runtime_publication import EVALUATION, SyntheticSession
from research_loop.runtime.context import ContextPackage
from research_loop.runtime.contexts import RuntimeContexts
from research_loop.runtime.files import publish_artifact
from research_loop.runtime.operations import Operations
from research_loop.runtime.planning_state import PlanningLedger
from research_loop.runtime.publication import Publisher
from research_loop.runtime.store import ConflictError, StateStore
from research_loop.runtime.updates import CallIdentity, Updater, _identity


class RuntimeRoleAccessTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.serial = 0

    def open_store(self, **overrides):
        config = {
            "goal": "Synthetic role access boundaries",
            "permissions": {"read": True, "write": False},
            "global_evaluation": EVALUATION,
            "role_access": {
                "review": {
                    "published_history": False,
                    "permissions": [],
                    "context_sources": [],
                }
            },
        }
        config.update(overrides)
        self.store = StateStore.create(self.root, config=config)
        session = self.store.coordinator()
        self.owner = session.__enter__()
        self.addCleanup(lambda: session.__exit__(None, None, None))
        self.demo = SyntheticSession(self.owner)
        self.updater = Updater(
            self.owner,
            validators={
                role: lambda output, context: output
                for role in ("plan", "execute", "review", "scout", "advocate", "judge")
            },
        )

    def call(self, role, iteration=None):
        self.serial += 1
        attempt = self.owner.begin_attempt(
            role,
            request_id=f"role-access-{self.serial}",
            **(
                {}
                if iteration is None
                else {
                    "iteration_id": iteration["id"],
                    "expected_revision": iteration["revision"],
                }
            ),
        )
        return CallIdentity.from_attempt(self.owner.snapshot()["study_id"], attempt)

    def accept(self, role, output, iteration=None, refs=()):
        call = self.call(role, iteration)
        package = RuntimeContexts(self.owner).build(call)
        self.updater.bind_context(call, package)
        proposal = self.updater.validate_output(call, output, list(refs))
        self.updater.apply_update(proposal, proposal.revisions)
        return call

    def publish_trial(self):
        iteration, measurement = self.demo.trial("published", 0.7)
        review = self.accept(
            "review",
            {
                "knowledge": [],
                "reviews": [
                    {
                        "measurement_id": measurement["id"],
                        "valid": True,
                        "verified": True,
                        "reason": "Synthetic receipt matches its candidate",
                    }
                ],
                "corrections": [],
            },
            iteration,
            refs=[measurement["receipt_ref"]],
        )
        Publisher(self.owner).publish(review)
        return measurement

    def test_review_retains_own_inputs_but_cannot_read_published_history(self):
        self.open_store()
        published = self.publish_trial()
        iteration, own = self.demo.trial("own", 0.5)
        _, other = self.demo.trial("other-unpublished", 0.8)
        call = self.call("review", iteration)
        package = RuntimeContexts(self.owner).build(call)
        self.assertEqual(package.context["permissions"], {})
        for key in (
            "research",
            "frontiers",
            f"measurement:{published['id']}",
            f"measurement:{other['id']}",
        ):
            self.assertNotIn(key, package.context)
        for measurement in (published, other):
            self.assertNotIn(
                measurement["receipt_ref"], package.manifest["allowed_evidence"]
            )
        for key in ("plan", "execute", f"measurement:{own['id']}"):
            self.assertIn(key, package.context)
            self.assertIn(key, package.manifest["protected_keys"])
        self.assertEqual(
            package.context[f"measurement:{own['id']}"]["metrics"], {"score": 0.5}
        )
        self.assertIn(own["receipt_ref"], package.manifest["allowed_evidence"])
        forged = package.to_dict()
        # Reusing an in-scope key must not grant the previous trial's receipt.
        goal = next(
            item for item in forged["manifest"]["sources"] if item["key"] == "goal"
        )
        goal["ref"] = published["receipt_ref"]
        forged["manifest"]["allowed_evidence"].append(published["receipt_ref"])
        with self.assertRaisesRegex(ConflictError, "iteration scope"):
            self.updater.bind_context(call, ContextPackage.from_dict(forged))
        self.updater.bind_context(call, package)

    def test_unconfigured_scout_retains_history_and_study_permissions(self):
        self.open_store()
        published = self.publish_trial()
        call = self.call("scout")
        package = RuntimeContexts(self.owner).build(call)
        self.assertIn("research", package.context)
        self.assertIn("frontiers", package.context)
        self.assertIn(f"measurement:{published['id']}", package.context)
        self.assertIn(published["receipt_ref"], package.manifest["allowed_evidence"])
        self.assertEqual(package.context["permissions"], {"read": True, "write": False})
        self.updater.bind_context(call, package)

    def test_permission_allowlist_intersects_study_permissions(self):
        self.open_store(role_access={"review": {"permissions": ["read", "missing"]}})
        iteration, _ = self.demo.trial("own", 0.5)
        call = self.call("review", iteration)
        package = RuntimeContexts(self.owner).build(call)
        self.assertEqual(package.context["permissions"], {"read": True})
        self.updater.bind_context(call, package)

    def test_source_allowlist_denies_review_and_preserves_unconfigured_scout(self):
        body = {"contract": "Synthetic operator baseline"}
        reference = publish_artifact(self.root, body)
        self.open_store(
            context_sources=[
                {
                    "name": "baseline",
                    "ref": reference,
                    "identity": _identity(self.root, reference),
                    "revision": 1,
                    "roles": ["plan", "execute", "review", "scout"],
                    "allow_feedback": True,
                    "required": True,
                }
            ]
        )
        self.owner.create_task("source-trial", {})
        iteration = self.owner.claim_task("source-trial")
        self.accept("plan", {"local_evaluation": None}, iteration)
        iteration = self.demo.iteration(iteration["id"])
        self.accept("execute", {"artifacts": []}, iteration)
        iteration = self.demo.iteration(iteration["id"])
        call = self.call("review", iteration)
        read_record = Updater._read_record

        def read(reader, ref, identity):
            self.assertNotEqual(ref, reference, "Denied source body must not be read")
            return read_record(reader, ref, identity)

        with patch.object(Updater, "_read_record", read):
            package = RuntimeContexts(self.owner).build(call)
            self.assertNotIn("provided:baseline", package.context)
            self.assertNotIn(reference, package.manifest["allowed_evidence"])
            self.assertNotIn("provided:baseline", package.manifest["protected_keys"])
            self.updater.bind_context(call, package)
        scout = self.call("scout")
        scout_package = RuntimeContexts(self.owner).build(scout)
        self.assertEqual(scout_package.context["provided:baseline"], body)
        self.assertIn(reference, scout_package.manifest["allowed_evidence"])
        self.updater.bind_context(scout, scout_package)

    def test_execute_without_actions_rejects_operations_before_admission(self):
        self.open_store(role_access={"execute": {"actions": []}})
        self.owner.create_task("denied-operation", {})
        iteration = self.owner.claim_task("denied-operation")
        self.accept("plan", {"local_evaluation": None}, iteration)
        iteration = self.demo.iteration(iteration["id"])
        call = self.call("execute", iteration)
        self.updater.bind_context(call, RuntimeContexts(self.owner).build(call))
        output = {
            "artifacts": [],
            "operations": [
                {
                    "id": "denied-work",
                    "backend": "synthetic",
                    "authorization_id": "synthetic-grant",
                    "resources": {"cpu": 1},
                    "cost_bounds": {"ticks": 1},
                    "timeout_seconds": 10,
                    "input_artifacts": [],
                    "parameters": {},
                }
            ],
        }
        with patch.object(Operations, "prepare_execution") as admission:
            with self.assertRaisesRegex(ValueError, "permission.*operations"):
                self.updater.validate_output(call, output, [])
            admission.assert_not_called()
        self.assertEqual(Operations(self.owner).list(iteration_id=iteration["id"]), [])
        self.assertEqual(self.demo.iteration(iteration["id"]), iteration)

    def test_planning_context_respects_advocate_history_rule_and_judge_default(self):
        self.open_store(role_access={"advocate": {"published_history": False}})
        self.publish_trial()
        ledger = PlanningLedger(self.owner)
        ledger.trigger("access-planning", "manual", {"reason": "Synthetic access test"})
        planning = ledger.begin()
        knowledge_ref = planning["knowledge_ref"]
        original_read = Updater._read_record

        def read(reader, ref, identity):
            self.assertNotEqual(
                ref, knowledge_ref, "Denied planning history must not be read"
            )
            return original_read(reader, ref, identity)

        for slot in ("advocate_a", "advocate_b"):
            call = CallIdentity.from_attempt(
                self.owner.snapshot()["study_id"], ledger.calls(planning["id"])[slot]
            )
            with patch.object(Updater, "_read_record", read):
                package = RuntimeContexts(self.owner).build(call)
                self.assertIn("planning", package.context)
                self.assertIn("planning", package.manifest["protected_keys"])
                self.assertNotIn("research", package.context)
                self.assertNotIn("frontiers", package.context)
                self.assertNotIn(knowledge_ref, package.manifest["allowed_evidence"])
                self.updater.bind_context(call, package)
            proposal = self.updater.validate_output(call, {"proposals": []}, [])
            self.updater.apply_update(proposal, proposal.revisions)
        judge = CallIdentity.from_attempt(
            self.owner.snapshot()["study_id"], ledger.reserve_judge(planning["id"])
        )
        package = RuntimeContexts(self.owner).build(judge)
        self.assertIn("research", package.context)
        self.assertIn("frontiers", package.context)
        self.assertIn(knowledge_ref, package.manifest["allowed_evidence"])
        self.assertEqual(package.context["frontiers"]["version"], 1)
        self.assertEqual(package.context["advocate_a"]["output"], {"proposals": []})
        self.updater.bind_context(judge, package)

    def test_configured_scout_cannot_cite_omitted_operator_source(self):
        ref = publish_artifact(self.root, {"claim": "Synthetic optional context"})
        self.open_store(
            role_access={"scout": {}},
            context_sources=[
                dict(
                    name="optional",
                    ref=ref,
                    identity=_identity(self.root, ref),
                    revision=1,
                    roles=["scout"],
                    allow_feedback=True,
                    required=False,
                )
            ],
        )
        call = self.call("scout")
        contexts = RuntimeContexts(
            self.owner,
            policy=lambda *_: {
                "provided:optional": {
                    "action": "omit",
                    "reason": "Irrelevant to request",
                }
            },
        )
        package = contexts.build(call)
        self.assertNotIn(ref, package.manifest["allowed_evidence"])
        self.updater.bind_context(call, package)
        with self.assertRaisesRegex(ValueError, "outside its frozen allowlist"):
            self.updater.validate_output(call, {}, [ref])

    def test_explicit_role_actions_cannot_expand_study_permissions(self):
        self.open_store(
            permissions={"trusted_backend_execution": False, "source_edits": True},
            role_access={
                "execute": {
                    "permissions": ["trusted_backend_execution"],
                    "actions": ["operations", "source_edits"],
                }
            },
        )
        self.owner.create_task("trial", {})
        iteration = self.owner.claim_task("trial")
        self.accept("plan", {"local_evaluation": None}, iteration)
        call = self.call("execute", self.demo.iteration(iteration["id"]))
        package = RuntimeContexts(self.owner).build(call)
        self.assertEqual(package.context["allowed_actions"], [])
        self.updater.bind_context(call, package)
        with self.assertRaisesRegex(ValueError, "lacks permission for operations"):
            self.updater.validate_output(call, {"operations": [{"id": "blocked"}]}, [])

    def test_create_rejects_unknown_role_and_unknown_access_rule(self):
        for index, role_access in enumerate(
            ({"reviewer": {}}, {"review": {"unknown_rule": False}})
        ):
            with self.subTest(role_access=role_access):
                with self.assertRaises(ValueError):
                    StateStore.create(
                        self.root / str(index), config={"role_access": role_access}
                    )


if __name__ == "__main__":
    unittest.main()
