"""Frozen synthetic baseline/control inputs on actual SQLite role paths."""

from copy import deepcopy
import json
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from research_loop.runtime.context import ContextPackage
from research_loop.runtime.contexts import RuntimeContexts
from research_loop.runtime.files import publish_artifact
from research_loop.runtime.planning_state import PlanningLedger
from research_loop.runtime.store import ConflictError, StateStore
from research_loop.runtime.updates import CallIdentity, Updater, _identity

ROLES = ["plan", "execute", "review", "scout", "advocate", "judge"]


class OperatorContextTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.body = {
            "files": [
                {
                    "path": "baseline.py",
                    "content": "def baseline(x):\n    return x + 1\n",
                }
            ],
            "contract": "Synthetic fixed input, no dataset/checkpoint access",
            "enabled": True,
        }
        self.ref = publish_artifact(self.root, self.body)
        self.denied = "artifacts/" + "0" * 32 + ".json"
        self.other = publish_artifact(
            self.root, {"contract": "Synthetic judge-only control"}
        )
        identity = _identity(self.root, self.ref)
        self.config = {
            "goal": "Synthetic frozen sources",
            "permissions": {},
            "context_sources": [
                {
                    "name": "baseline",
                    "ref": self.ref,
                    "identity": identity,
                    "revision": "baseline-v1",
                    "roles": ROLES,
                    "allow_feedback": True,
                    "required": True,
                },
                {
                    "name": "denied",
                    "ref": self.denied,
                    "identity": identity,
                    "revision": 1,
                    "roles": ROLES,
                    "allow_feedback": False,
                    "required": True,
                },
                {
                    "name": "optional",
                    "ref": self.ref,
                    "identity": identity,
                    "revision": "contract-v1",
                    "roles": ["scout"],
                    "allow_feedback": True,
                    "required": False,
                },
                {
                    "name": "other-role",
                    "ref": self.other,
                    "identity": _identity(self.root, self.other),
                    "revision": 1,
                    "roles": ["judge"],
                    "allow_feedback": True,
                    "required": False,
                },
            ],
        }
        self.store = StateStore.create(self.root, config=self.config)
        self.session = self.store.coordinator()
        self.owner = self.session.__enter__()
        self.addCleanup(lambda: self.session.__exit__(None, None, None))
        self.updater = Updater(
            self.owner,
            validators={role: lambda output, context: output for role in ROLES},
        )
        self.serial = 0

    def call(self, role, iteration=None):
        self.serial += 1
        attempt = self.owner.begin_attempt(
            role,
            request_id=f"source-{self.serial}",
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

    def accept(self, call, output, package=None):
        package = package or RuntimeContexts(self.owner).build(call)
        self.updater.bind_context(call, package)
        proposal = self.updater.validate_output(call, output, [])
        self.updater.apply_update(proposal, proposal.revisions)
        return package

    def iteration(self, identity):
        return next(
            item
            for item in self.owner.snapshot()["iterations"]
            if item["id"] == identity
        )

    def assert_source(self, package):
        self.assertEqual(package.context["provided:baseline"], self.body)
        self.assertIn("provided:baseline", package.manifest["protected_keys"])
        self.assertIn(self.ref, package.manifest["allowed_evidence"])
        self.assertNotIn("provided:denied", package.context)
        self.assertNotIn(self.denied, package.manifest["allowed_evidence"])
        denied = next(
            source
            for source in package.manifest["sources"]
            if source["key"] == "provided:denied"
        )
        self.assertEqual(denied["disposition"], "omitted")
        self.assertIn("denied", denied["reason"])

    def test_real_frozen_baseline_reaches_all_six_roles_including_planning(self):
        self.owner.create_task("qualitative", {})
        iteration = self.owner.claim_task("qualitative")
        for role, output in (
            ("plan", {"timeout_seconds": 10, "local_evaluation": None}),
            ("execute", {"operations": [], "artifacts": []}),
        ):
            package = self.accept(
                self.call(role, self.iteration(iteration["id"])), output
            )
            self.assert_source(package)
        package = RuntimeContexts(self.owner).build(
            self.call("review", self.iteration(iteration["id"]))
        )
        self.assert_source(package)
        self.assert_source(
            self.accept(
                self.call("scout"), {"knowledge": [], "reviews": [], "corrections": []}
            )
        )
        ledger = PlanningLedger(self.owner)
        ledger.trigger(
            "source-planning", "manual", {"reason": "Synthetic source coverage"}
        )
        round_record = ledger.begin()
        for slot in ("advocate_a", "advocate_b"):
            call = CallIdentity.from_attempt(
                self.owner.snapshot()["study_id"],
                ledger.calls(round_record["id"])[slot],
            )
            package = ledger.context(call)
            self.assert_source(package)
            self.accept(call, {"proposals": []}, package)
        judge = CallIdentity.from_attempt(
            self.owner.snapshot()["study_id"], ledger.reserve_judge(round_record["id"])
        )
        self.assert_source(ledger.context(judge))

    def test_denied_and_other_role_sources_never_read_or_reach_policy(self):
        original = Updater._read_record
        seen = []

        def read(reader, reference, identity):
            self.assertNotIn(reference, (self.denied, self.other))
            # Acquiring another writer proves actual body reads are outside the
            # coordinator writer, including independent Updater verification.
            with self.store._connection() as conn:
                conn.execute("PRAGMA busy_timeout=0")
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
            seen.append(reference)
            return original(reader, reference, identity)

        def policy(inputs, sources, budget):
            self.assertNotIn("provided:denied", inputs)
            self.assertNotIn("provided:other-role", inputs)
            self.assertFalse(
                any(source["ref"] in (self.denied, self.other) for source in sources)
            )
            return {
                "provided:optional": {
                    "action": "omit",
                    "reason": "Optional control omitted explicitly",
                }
            }

        call = self.call("scout")
        with patch.object(Updater, "_read_record", read):
            package = RuntimeContexts(self.owner, policy=policy).build(call)
            self.updater.bind_context(call, package)
        self.assertIn(self.ref, seen)
        self.assertNotIn("provided:optional", package.context)
        role_source = next(
            item
            for item in package.manifest["sources"]
            if item["key"] == "provided:other-role"
        )
        self.assertEqual(role_source["disposition"], "omitted")
        self.assertIn("not assigned", role_source["reason"])
        self.assertNotIn(self.other, package.manifest["allowed_evidence"])
        self.assertIn(self.ref, package.manifest["allowed_evidence"])

    def test_required_source_cannot_be_omitted_by_policy(self):
        def omit(*args):
            return {
                "provided:baseline": {"action": "omit", "reason": "Forbidden omission"}
            }

        with self.assertRaisesRegex(ValueError, "protected"):
            RuntimeContexts(self.owner, policy=omit).build(self.call("scout"))

    def forged(self, package, mutate):
        value = package.to_dict()
        mutate(value)
        value["manifest"]["tokens"]["estimated_input_tokens"] = math.ceil(
            len(json.dumps(value["context"], ensure_ascii=False, allow_nan=False)) / 4
        )
        return ContextPackage.from_dict(value)

    def test_updater_independently_rejects_source_rewrite_metadata_and_access_tampering(
        self,
    ):
        call = self.call("scout")
        package = RuntimeContexts(self.owner).build(call)

        def source(value, name):
            return next(
                item
                for item in value["manifest"]["sources"]
                if item["key"] == "provided:" + name
            )

        def omit_required(value):
            value["context"].pop("provided:baseline")
            value["manifest"]["protected_keys"].remove("provided:baseline")
            source(value, "baseline")["disposition"] = "omitted"

        def allow_denied(value):
            source(value, "denied").update(allow_feedback=True, disposition="provided")
            value["context"]["provided:denied"] = {"forged": True}
            value["manifest"]["allowed_evidence"].append(self.denied)

        def unknown(value):
            item = deepcopy(source(value, "baseline"))
            item.update(id="provided:unknown", key="provided:unknown")
            value["manifest"]["sources"].append(item)
            value["context"]["provided:unknown"] = self.body

        changes = [
            lambda value: value["context"]["provided:baseline"].update(enabled=1),
            lambda value: value["context"]["provided:baseline"].update(
                contract="Changed meaning"
            ),
            lambda value: source(value, "baseline").update(revision="forged-version"),
            lambda value: source(value, "baseline").update(ref=self.other),
            omit_required,
            allow_denied,
            unknown,
        ]
        for mutate in changes:
            with self.subTest(mutate=mutate), self.assertRaises(ConflictError):
                self.updater.bind_context(call, self.forged(package, mutate))

    def test_stale_configured_identity_rejected_before_binding(self):
        call = self.call("scout")
        package = RuntimeContexts(self.owner).build(call)
        metadata = (self.root / self.ref).stat()
        os.utime(
            self.root / self.ref, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1)
        )
        with self.assertRaises(ConflictError):
            self.updater.bind_context(call, package)
        with self.assertRaises(ConflictError):
            RuntimeContexts(self.owner).build(call)

    def test_source_identity_rechecked_at_commit_after_body_validation(self):
        import research_loop.runtime.updates as updates

        call = self.call("scout")
        package = RuntimeContexts(self.owner).build(call)
        publish = updates.publish_artifact

        def mutate(root, body):
            metadata = (self.root / self.ref).stat()
            os.utime(
                self.root / self.ref,
                ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1),
            )
            return publish(root, body)

        with (
            patch.object(updates, "publish_artifact", mutate),
            self.assertRaises(ConflictError),
        ):
            self.updater.bind_context(call, package)
        with self.store._connection(readonly=True) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM contexts").fetchone()[0], 0
            )
