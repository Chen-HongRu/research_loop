"""Derived input disclosures stay within the originating grants, including peers."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from research_loop.runtime.knowledge import KnowledgeBase
from research_loop.runtime.knowledge_manager import EvidenceIntake, commit_maintenance
from research_loop.runtime.role_access import ROLES
from research_loop.runtime.source_scope import freeze_source_scope, source_reference
from research_loop.runtime.store import StateStore


def source(name, roles, identity):
    return dict(
        name=name,
        ref=f"artifacts/{identity * 32}.json",
        revision={"version": 1},
        roles=roles,
        allow_feedback=True,
    )


def assembly(source_entry, scope, libraries, **inputs):
    return dict(
        role="knowledge_manager",
        inputs={"provided:private": {"text": "secretword"}, **inputs},
        input_sources=[
            dict(
                key="provided:private",
                ref=source_entry["ref"],
                revision=source_entry["revision"],
                allow_feedback=True,
                disposition="provided",
            )
        ],
        source_scope=scope,
        knowledge_libraries=libraries,
    )


class SourceScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.private = source("private", ["scout", "knowledge_manager"], "a")
        self.public = source("public", sorted(ROLES), "b")
        self.config = {"context_sources": [self.private, self.public]}
        self.store = StateStore.create(self.root / "origin", config=self.config)
        self.kb = KnowledgeBase(self.store.root, config=self.config)
        self.manifest = self.kb.freeze()
        self.scope = freeze_source_scope(
            self.kb, self.config, "knowledge_manager", self.manifest
        )
        self.private_ref = source_reference(self.kb.library_id, self.private)

    def writer(self):
        return KnowledgeBase(
            self.store.root, config=self.config, source_scope=self.scope
        )

    def test_scout_observation_inherits_uncited_direct_input(self):
        packet = {"assembly": assembly(self.private, self.scope, self.manifest)}
        observation = {"kind": "external", "statement": "secretword", "source_refs": []}
        intake = EvidenceIntake(SimpleNamespace(store=self.store))
        records = {
            "result": {"normalized_output": {"knowledge": [observation]}},
            "dispatch": packet,
        }
        intake.reader = SimpleNamespace(_read_record=lambda ref, identity: records[ref])
        with patch(
            "research_loop.runtime.knowledge_manager.consulted_refs", return_value=[]
        ):
            intake._ingest(
                dict(
                    id=1,
                    payload_ref="result",
                    identity={},
                    input_ref="dispatch",
                    input_identity={},
                    role="scout",
                    attempt_id="scout-call",
                )
            )
        manifest = self.kb.freeze()
        denied = KnowledgeBase(self.store.root)
        self.assertEqual(denied.search(manifest, "secretword"), [])
        with self.assertRaises(PermissionError):
            denied.read(manifest, "study", "observation-1-0")
        self.assertTrue(denied.pending(manifest=manifest)["blocked_by_scope"])
        record = self.writer().read(manifest, "study", "observation-1-0")
        self.assertIn(self.private_ref, record["source_refs"])
        self.assertEqual(record["content"], observation)
        with self.assertRaises(PermissionError):
            self.writer().read(manifest, "study", self.private_ref)

    def test_manager_direct_input_taints_pages_and_index_without_tool_reads(self):
        self.kb.publish_evidence("trigger", "synthetic", "Public trigger")
        manifest = self.kb.freeze()
        bound = assembly(
            self.private,
            self.scope,
            manifest,
            maintenance=dict(
                base_version=0,
                processed_through=0,
                evidence_through=1,
                affected_pages=[],
            ),
        )
        output = dict(
            pages=[
                dict(
                    id="secret-page",
                    title="secretword title",
                    markdown="secretword body",
                    dependencies=[],
                )
            ],
            index_markdown="# secretword index",
            processed_through=1,
        )
        with self.store._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            with (
                patch(
                    "research_loop.runtime.knowledge_manager.dispatched_input",
                    return_value={"assembly": bound, "knowledge_config": self.config},
                ),
                patch(
                    "research_loop.runtime.knowledge_manager.consulted_refs",
                    return_value=[],
                ),
            ):
                commit_maintenance(
                    SimpleNamespace(store=self.store),
                    conn,
                    SimpleNamespace(attempt_id="manager-call"),
                    output,
                )
            conn.commit()
        frozen = self.kb.freeze()
        denied = KnowledgeBase(self.store.root)
        self.assertEqual(denied.search(frozen, "secretword"), [])
        self.assertNotIn("secretword", denied.read_index(frozen))
        self.assertNotIn("secret-page", denied.read_index(frozen))
        with self.assertRaises(PermissionError):
            denied.read(frozen, "study", "secret-page")
        self.assertEqual(self.writer().read_index(frozen), "# secretword index")
        self.assertEqual(
            self.writer().read(frozen, "study", "secret-page"), "secretword body"
        )

    def test_peer_mount_permits_only_universally_authorized_sources(self):
        public_ref = source_reference(self.kb.library_id, self.public)
        private = self.kb.publish_evidence(
            "restricted", "scout", "secretword", source_refs=[self.private_ref]
        )
        public = self.kb.publish_evidence(
            "shareable", "scout", "publicword", source_refs=[public_ref]
        )
        self.writer().commit(
            0,
            [
                dict(
                    id="restricted-page",
                    title="secretword title",
                    markdown="secretword",
                    dependencies=[private],
                ),
                dict(
                    id="public-page",
                    title="publicword title",
                    markdown="publicword",
                    dependencies=[public],
                ),
            ],
            "# secretword custom index",
            2,
            "origin",
        )
        config = {
            "knowledge_libraries": [
                dict(alias="origin", path=str(self.store.root), version=1)
            ]
        }
        consumer = KnowledgeBase(self.root / "consumer", config=config)
        frozen = consumer.freeze(aliases=["origin"])
        scoped = freeze_source_scope(consumer, config, "plan", frozen)
        self.assertEqual(scoped, [public_ref])
        reader = KnowledgeBase(consumer.root, config=config, source_scope=scoped)
        self.assertEqual(reader.search(frozen, "secretword"), [])
        self.assertEqual(len(reader.search(frozen, "publicword")), 2)
        self.assertNotIn("secretword", reader.read_index(frozen, "origin"))
        self.assertEqual(reader.read(frozen, "origin", "public-page"), "publicword")
        self.assertIn("public-page", reader.read_index(frozen, "origin"))
        with self.assertRaises(PermissionError):
            reader.read(frozen, "origin", "restricted-page")
        # The sources intentionally do not exist: metadata grants never open them.
        self.assertFalse((self.store.root / self.private["ref"]).exists())

    def test_role_access_narrowing_and_omitted_inputs(self):
        config = self.config | {
            "role_access": {"knowledge_manager": {"context_sources": []}}
        }
        self.assertEqual(
            freeze_source_scope(self.kb, config, "knowledge_manager", self.manifest), []
        )
        from research_loop.runtime.source_scope import input_dependencies

        bound = assembly(self.private, self.scope, self.manifest)
        bound["input_sources"][0]["disposition"] = "omitted"
        self.assertEqual(input_dependencies(bound, self.kb.library_id), [])
        bound["input_sources"][0]["disposition"] = "provided"
        self.assertEqual(
            input_dependencies(bound, self.kb.library_id), [self.private_ref]
        )


if __name__ == "__main__":
    unittest.main()
