"""Private inputs stay in SQL; whole-library publication never widens authority."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from research_loop.runtime.knowledge import KnowledgeBase
from research_loop.runtime.knowledge_manager import EvidenceIntake, commit_maintenance
from research_loop.runtime.role_access import ROLES
from research_loop.runtime.source_scope import (
    derived_dependencies,
    freeze_source_scope,
    input_dependencies,
    source_reference,
)
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
        version=2,
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
        maintenance_libraries=libraries,
        knowledge_dependencies=[],
        inherited_dependencies=[],
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

    def test_uncited_direct_input_stays_private_in_evidence(self):
        packet = {"assembly": assembly(self.private, self.scope, self.manifest)}
        observation = {"kind": "external", "statement": "secretword", "source_refs": []}
        intake = EvidenceIntake(SimpleNamespace(store=self.store))
        records = {
            "result": {"normalized_output": {"knowledge": [observation]}},
            "dispatch": packet,
        }
        intake.reader = SimpleNamespace(_read_record=lambda ref, identity: records[ref])
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
        with self.assertRaises(PermissionError):
            KnowledgeBase(self.store.root).read(manifest, "study", "observation-1-0")
        record = self.writer().read(manifest, "study", "observation-1-0")
        self.assertIn(self.private_ref, record["source_refs"])
        current = Path(self.kb.live(["study"], role="plan")["entries"][0]["path"])
        self.assertEqual(list((current / "files").iterdir()), [])
        self.assertNotIn(
            "secretword",
            "\n".join(
                path.read_text() for path in current.rglob("*") if path.is_file()
            ),
        )

    def test_manager_uncited_private_input_rejects_shared_file_before_commit(self):
        self.kb.publish_evidence("trigger", "synthetic", "trigger")
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
            files={"notes/rough.txt": "secretword"},
            remove_paths=[],
            processed_through=1,
        )
        with self.store._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            with patch(
                "research_loop.runtime.knowledge_manager.dispatched_input",
                return_value={"assembly": bound, "knowledge_config": self.config},
            ):
                with self.assertRaises(PermissionError):
                    commit_maintenance(
                        SimpleNamespace(store=self.store),
                        conn,
                        SimpleNamespace(attempt_id="manager-call"),
                        output,
                    )
            conn.rollback()
        self.assertEqual(self.kb.head(), 0)
        self.assertEqual(self.kb.status()["processed_through"], 0)
        self.assertFalse(
            self.store.root.joinpath(
                "knowledge/published/current/files/notes/rough.txt"
            ).exists()
        )

    def test_peer_grant_widening_does_not_expand_old_reference_authority(self):
        public_ref = source_reference(self.kb.library_id, self.public)
        private = self.kb.publish_evidence(
            "private", "scout", "secretword", source_refs=[self.private_ref]
        )
        self.kb.publish_evidence(
            "public", "scout", "publicword", source_refs=[public_ref]
        )
        self.writer().commit(
            0,
            [],
            None,
            2,
            "public",
            files={"public.txt": "publicword"},
            file_dependencies=[public_ref],
        )
        config = {
            "knowledge_libraries": [
                dict(alias="origin", path=str(self.store.root), version=1)
            ]
        }
        consumer = KnowledgeBase(self.root / "consumer", config=config)
        live = consumer.live(["origin"], role="plan")
        old_scope = live["source_scope"]
        self.assertEqual(old_scope, [public_ref])
        widened = {
            "context_sources": [dict(self.private, roles=sorted(ROLES)), self.public]
        }
        with self.store._connection() as conn:
            conn.execute(
                "UPDATE control SET config_json=? WHERE singleton=1",
                (json.dumps(widened),),
            )
        for scope in (old_scope, []):
            old_call = KnowledgeBase(consumer.root, config=config, source_scope=scope)
            with self.assertRaises(PermissionError):
                old_call.authorize_reference(
                    private, aliases=["origin"], role="plan", bindings=live["entries"]
                )
        fresh = KnowledgeBase(consumer.root, config=config)
        self.assertEqual(
            fresh.authorize_reference(
                private, aliases=["origin"], role="plan", bindings=live["entries"]
            ),
            private,
        )

    def test_private_peer_dependency_cannot_be_paraphrased_into_shared_root(self):
        peer = KnowledgeBase(self.root / "peer")
        peer.commit(
            0, [], None, 0, "peer", files={"private.txt": "uncited private peer"}
        )
        config = {
            "knowledge_libraries": [dict(alias="peer", path=str(peer.root), version=1)]
        }
        self.kb.configure(config)
        manifest = self.kb.freeze(aliases=["study", "peer"])
        reference = peer.file_catalog(peer._manifest(peer.freeze())[peer.library_id])[
            "private.txt"
        ]
        disclosure = self.kb.disclosure(manifest, [reference])
        self.assertEqual(
            disclosure,
            [
                dict(
                    library_id=peer.library_id, kind="library", id="library", revision=1
                )
            ],
        )
        with self.assertRaises(PermissionError):
            self.kb.commit(
                0,
                [],
                None,
                0,
                "paraphrase",
                files={"summary.txt": "uncited private peer"},
                file_dependencies=disclosure,
                manifest=manifest,
            )
        self.assertEqual(self.kb.head(), 0)

    def test_direct_input_and_whole_base_grants_are_retained_without_citations(self):
        live = self.kb.live(["study"], role="scout")
        same = self.kb.live(["study"], role="plan")
        self.assertEqual(live["entries"][0]["path"], same["entries"][0]["path"])
        bound = assembly(self.private, self.scope, self.manifest)
        bound["knowledge_dependencies"] = live["references"]
        deps = derived_dependencies(bound, self.kb.library_id)
        self.assertIn(self.private_ref, deps)
        self.assertIn(live["references"][0], deps)
        bound["input_sources"][0]["allow_feedback"] = False
        self.assertEqual(
            input_dependencies(bound, self.kb.library_id), [self.private_ref]
        )
        bound["input_sources"][0]["disposition"] = "omitted"
        self.assertEqual(input_dependencies(bound, self.kb.library_id), [])


if __name__ == "__main__":
    unittest.main()
