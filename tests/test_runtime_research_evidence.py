"""Unscored synthetic reports reach own Review without becoming measurements."""

from copy import deepcopy
import json
import math
import unittest
from unittest.mock import patch

import test_runtime_operation_measurements as measurement_fixtures
from research_loop.runtime.context import ContextPackage
from research_loop.runtime.contexts import RuntimeContexts
from research_loop.runtime.publication import Publisher
from research_loop.runtime.updates import CallIdentity, Updater, _identity
from research_loop.runtime.files import publish_artifact
from research_loop.runtime.store import ConflictError


class RuntimeResearchEvidenceTests(unittest.TestCase):
    # Reuse only the existing synthetic SQLite setup/control helpers, not tests.
    setUp = measurement_fixtures.RuntimeOperationMeasurementTests.setUp
    rows = measurement_fixtures.RuntimeOperationMeasurementTests.rows
    executed = measurement_fixtures.RuntimeOperationMeasurementTests.executed
    event = measurement_fixtures.RuntimeOperationMeasurementTests.event
    terminal = measurement_fixtures.RuntimeOperationMeasurementTests.terminal

    def report(self, artifact, *, allow=True, name="observations"):
        body = {
            "observations": ["Synthetic script reports an incomplete attempt"],
            "limitations": ["Protected evaluator was not executed"],
            "unverified": True,
        }
        reference = publish_artifact(self.root, body)
        return body, {
            "name": name,
            "artifact": artifact,
            "ref": reference,
            "identity": _identity(self.root, reference),
            "allow_feedback": allow,
        }

    def collected(self):
        iteration, execute, artifact, ids = self.executed()
        body, entry = self.report(artifact)
        denied = {
            **entry,
            "name": "denied",
            "ref": "artifacts/" + "0" * 32 + ".json",
            "allow_feedback": False,
        }
        self.terminal(ids[0])
        self.event(
            ids[0],
            "collection",
            {
                "artifacts": [artifact],
                "evaluations": [],
                "research_evidence": [entry, denied],
            },
        )
        self.operations.finalize_feedback(execute.attempt_id)
        return iteration, execute, ids[0], body, entry, denied

    def review(self, iteration):
        attempt = self.owner.begin_attempt(
            "review",
            request_id="research-review-" + iteration["id"],
            iteration_id=iteration["id"],
            expected_revision=iteration["revision"],
        )
        return CallIdentity.from_attempt(self.owner.snapshot()["study_id"], attempt)

    def test_own_unverified_report_in_context_and_interpretation_publication_only(self):
        iteration, _, op, body, entry, denied = self.collected()
        call = self.review(iteration)
        original = Updater._read_record

        def read(reader, reference, identity):
            self.assertNotEqual(reference, denied["ref"])
            with self.store._connection() as conn:
                conn.execute("PRAGMA busy_timeout=0")
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
            return original(reader, reference, identity)

        with patch.object(Updater, "_read_record", read):
            package = RuntimeContexts(self.owner).build(call)
            key = f"evidence:{op}:observations"
            self.assertEqual(package.context[key], body)
            self.assertNotIn(f"evidence:{op}:denied", package.context)
            self.assertIn(entry["ref"], package.manifest["allowed_evidence"])
            self.assertNotIn(denied["ref"], package.manifest["allowed_evidence"])
            source = next(
                item for item in package.manifest["sources"] if item["key"] == key
            )
            self.assertIn("Unverified", source["reason"])
            updater = Updater(
                self.owner, validators={"review": lambda output, context: output}
            )
            updater.bind_context(call, package)
            knowledge = {
                "id": "unscored-report",
                "expected_revision": 0,
                "kind": "interpretation",
                "statement": "The synthetic script did not run protected evaluation",
                "conditions": ["Synthetic control receipt"],
                "source_refs": [entry["ref"]],
                "measurement_ids": [],
                "interpretation": "No score can be inferred",
                "limitations": ["Unverified backend observation"],
            }
            output = {"knowledge": [knowledge], "reviews": [], "corrections": []}
            for kind in ("external", "local_measurement"):
                invalid = deepcopy(output)
                invalid["knowledge"][0]["kind"] = kind
                with (
                    self.subTest(kind=kind),
                    self.assertRaisesRegex(ValueError, "interpretation"),
                ):
                    updater.validate_output(call, invalid, [entry["ref"]])
            proposal = updater.validate_output(call, output, [entry["ref"]])
            updater.apply_update(proposal, proposal.revisions)
            published = Publisher(self.owner).publish(call)
        self.assertEqual(
            published["knowledge"], [{"id": "unscored-report", "revision": 1}]
        )
        self.assertEqual(self.rows("measurements"), [])
        self.assertEqual(self.rows("eligibility"), [])
        self.assertEqual(self.rows("evaluation_intake"), [])

    def test_other_iterations_and_roles_never_receive_report_body(self):
        _, _, op, _, entry, _ = self.collected()
        other, execute, artifact, ids = self.executed()
        self.terminal(ids[0])
        self.event(ids[0], "collection", {"artifacts": [], "evaluations": []})
        self.operations.finalize_feedback(execute.attempt_id)
        original = Updater._read_record

        def read(reader, reference, identity):
            self.assertNotEqual(reference, entry["ref"])
            return original(reader, reference, identity)

        with patch.object(Updater, "_read_record", read):
            package = RuntimeContexts(self.owner).build(self.review(other))
            self.assertNotIn(f"evidence:{op}:observations", package.context)
            scout = self.owner.begin_attempt("scout", request_id="report-free-scout")
            scout = CallIdentity.from_attempt(self.owner.snapshot()["study_id"], scout)
            self.assertNotIn(
                entry["ref"],
                RuntimeContexts(self.owner).build(scout).manifest["allowed_evidence"],
            )

    def test_collection_rejects_nonmember_artifact_bad_identity_and_duplicate_name(
        self,
    ):
        _, _, artifact, ids = self.executed()
        self.terminal(ids[0])
        _, entry = self.report(artifact)

        def conflicting_alias(value):
            alias = deepcopy(value["research_evidence"][0])
            alias["name"] = "alias"
            value["research_evidence"][0]["identity"]["size"] = 1
            value["research_evidence"].append(alias)

        changes = [
            conflicting_alias,
            lambda value: value["research_evidence"][0]["artifact"].update(
                version="uncollected"
            ),
            lambda value: value["research_evidence"].append(
                deepcopy(value["research_evidence"][0])
            ),
            lambda value: value["research_evidence"][0]["identity"].update(size=1),
        ]
        for mutate in changes:
            data = {
                "artifacts": [deepcopy(artifact)],
                "evaluations": [],
                "research_evidence": [deepcopy(entry)],
            }
            mutate(data)
            with (
                self.subTest(mutate=mutate),
                self.assertRaises((ValueError, ConflictError)),
            ):
                self.event(ids[0], "collection", data)
        self.assertEqual(self.operations.get(ids[0])["collection_state"], "pending")

    def test_updater_rejects_rewrite_deny_upgrade_and_cross_iteration_forgery(self):
        iteration, _, op, _, entry, denied = self.collected()
        call = self.review(iteration)
        package = RuntimeContexts(self.owner).build(call)

        def rewrite(value):
            value["context"][f"evidence:{op}:observations"]["unverified"] = False

        def upgrade(value):
            key = f"evidence:{op}:denied"
            source = next(
                item for item in value["manifest"]["sources"] if item["key"] == key
            )
            source.update(allow_feedback=True, disposition="provided")
            value["context"][key] = {"forged": True}
            value["manifest"]["allowed_evidence"].append(denied["ref"])

        def unknown(value):
            key = "evidence:other-iteration:report"
            source = deepcopy(
                next(
                    item
                    for item in value["manifest"]["sources"]
                    if item["key"] == f"evidence:{op}:observations"
                )
            )
            source.update(id=key, key=key)
            value["context"][key] = {"forged": True}
            value["manifest"]["sources"].append(source)

        for mutate in (rewrite, upgrade, unknown):
            value = package.to_dict()
            mutate(value)
            value["manifest"]["tokens"]["estimated_input_tokens"] = math.ceil(
                len(json.dumps(value["context"], ensure_ascii=False)) / 4
            )
            forged = ContextPackage.from_dict(value)
            with self.subTest(mutate=mutate), self.assertRaises(ConflictError):
                Updater(self.owner, validators={}).bind_context(call, forged)

    def test_collection_commit_rechecks_report_identity_without_reading_body(self):
        import os
        import research_loop.runtime.files as files

        _, _, artifact, ids = self.executed()
        self.terminal(ids[0])
        _, entry = self.report(artifact)
        publish = files.publish_artifact

        def replace(root, payload):
            metadata = (self.root / entry["ref"]).stat()
            os.utime(
                self.root / entry["ref"],
                ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1),
            )
            return publish(root, payload)

        with (
            patch.object(files, "publish_artifact", replace),
            self.assertRaises(ConflictError),
        ):
            self.event(
                ids[0],
                "collection",
                {
                    "artifacts": [artifact],
                    "evaluations": [],
                    "research_evidence": [entry],
                },
            )
        self.assertIsNone(self.operations.get(ids[0])["collection_ref"])

    def test_report_identity_is_checked_before_body_read(self):
        import os

        iteration, _, _, _, entry, _ = self.collected()
        path = self.root / entry["ref"]
        metadata = path.stat()
        os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1))
        with self.assertRaises(ConflictError):
            RuntimeContexts(self.owner).build(self.review(iteration))
