"""Real local storage, frozen scopes and publication failure boundaries."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from research_loop.runtime.knowledge import KnowledgeBase
from research_loop.runtime.store import ConflictError, StateStore


def page(identity, text, dependencies=None, **changes):
    return (
        dict(
            id=identity, title=identity, markdown=text, dependencies=dependencies or []
        )
        | changes
    )


class KnowledgeLibraryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.kb = KnowledgeBase(self.root / "study")

    def publish(self, kb=None, identity="E1", content="Observed apples", **kwargs):
        return (kb or self.kb).publish_evidence(
            identity, "experiment_receipt", content, **kwargs
        )

    def test_evidence_is_immutable_idempotent_and_frozen(self):
        ref = self.publish()
        self.assertEqual(ref, self.publish())
        frozen = self.kb.freeze()
        self.publish(identity="E2", content="Counterexample")
        self.assertEqual(
            self.kb.read(frozen, "study", ref)["content"], "Observed apples"
        )
        with self.assertRaises(PermissionError):
            self.kb.read(frozen, "study", "E2")
        with self.assertRaises(ConflictError):
            self.publish(content="Changed observation")
        pending = self.kb.pending(manifest=frozen)
        self.assertEqual([row["reference"] for row in pending["evidence"]], [ref])
        self.assertEqual(pending["evidence_through"], 1)
        self.assertNotIn("content", pending["evidence"][0])

    def test_page_evolution_atomic_failure_and_human_markdown(self):
        e1 = self.publish()
        initial = self.kb.freeze()
        p1 = page("K1", "# Apples\n\nObserved result.", [e1])
        first = self.kb.commit(
            0, [p1], "# Topics\n\n[K1](K1)", 1, "first", manifest=initial
        )
        old = self.kb.freeze()
        self.assertEqual(self.kb.read(old, "study", "K1"), p1["markdown"])
        path = next((self.kb.folder / "versions").glob("*/pages/K1.md"))
        self.assertEqual(path.read_text(), self.kb.read(old, "study", "K1"))
        e2 = self.publish(identity="E2", content="Counterexample apples")
        frozen = self.kb.freeze()
        p2 = page("K1", "# Revised apples\n\nCounterexample.", [e1, e2])
        with patch(
            "research_loop.runtime.knowledge._markdown",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaises(OSError):
                self.kb.commit(1, [p2], "# Revised index", 2, "second", manifest=frozen)
        self.assertEqual(self.kb.status()["version"], 1)
        self.assertEqual(self.kb.status()["processed_through"], 1)
        self.assertEqual(len(self.kb.pending()["evidence"]), 1)
        second = self.kb.commit(
            1, [p2], "# Revised index", 2, "second", manifest=frozen
        )
        self.assertEqual(second["version"], 2)
        self.assertEqual(self.kb.read(old, "study", "K1"), p1["markdown"])
        self.assertEqual(self.kb.read(self.kb.freeze(), "study", "K1"), p2["markdown"])
        # Retry remains valid after the current version has advanced.
        self.assertEqual(
            first,
            self.kb.commit(
                0, [p1], "# Topics\n\n[K1](K1)", 1, "first", manifest=initial
            ),
        )

    def test_cas_noop_progress_and_split_merge_archive(self):
        self.publish()
        frozen = self.kb.freeze()
        self.kb.commit(0, [page("K1", "Initial")], "# Index", 1, "one", manifest=frozen)
        with self.assertRaises(ConflictError):
            self.kb.commit(
                0, [page("wrong", "Stale")], "# Stale", 1, "stale", manifest=frozen
            )
        self.publish(identity="E2")
        frozen = self.kb.freeze()
        receipt = self.kb.commit(1, [], None, 2, "noop", manifest=frozen)
        self.assertFalse(receipt["changed"])
        self.assertEqual(receipt["version"], 1)
        self.assertFalse(self.kb.pending()["evidence"])
        self.kb.commit(
            1,
            [
                page("K1", "Initial", archived=True),
                page("K2", "Split left"),
                page("K3", "Split right"),
            ],
            "# Split",
            2,
            "split",
        )
        current = self.kb.freeze()
        self.assertEqual(
            {r["reference"]["id"] for r in self.kb.search(current, "Split")},
            {"K2", "K3"},
        )
        self.kb.commit(
            2,
            [
                page("K2", "Split left", archived=True),
                page("K3", "Split right", archived=True),
                page("K4", "Merged result"),
            ],
            "# Merged",
            2,
            "merge",
        )
        self.assertFalse(self.kb.search(self.kb.freeze(), "Split"))
        self.assertEqual(self.kb.read(current, "study", "K2"), "Split left")

    def test_mount_fixed_versions_and_scope_transitive_dependencies(self):
        secret = KnowledgeBase(self.root / "secret")
        evidence = self.publish(secret, content="private forbidden finding")
        secret.commit(
            0,
            [page("Private", "private forbidden finding", [evidence])],
            "# Private secret",
            1,
            "secret",
        )
        secret_v1 = secret.freeze()[0]
        private_ref = dict(
            library_id=secret.library_id, kind="knowledge", id="Private", revision=1
        )
        config = {
            "knowledge_libraries": [
                dict(alias="secret", path=str(secret.root), version=1)
            ]
        }
        self.kb.configure(config)
        full = self.kb.freeze(aliases=["study", "secret"])
        self.kb.commit(
            0,
            [
                page("Shared", "private forbidden summary", [private_ref]),
                page("Public", "public finding"),
            ],
            "# private forbidden summary",
            0,
            "shared",
            manifest=full,
        )
        restricted = self.kb.freeze(aliases=["study"])
        self.assertNotIn("private", self.kb.read_index(restricted).lower())
        self.assertFalse(self.kb.search(restricted, "forbidden"))
        with self.assertRaises(PermissionError):
            self.kb.read(restricted, "study", "Shared")
        with self.assertRaises(PermissionError):
            self.kb.read(restricted, "secret", private_ref)
        with self.assertRaises(PermissionError):
            self.kb.commit(
                1, [page("Shared", "laundered")], None, 0, "blind", manifest=restricted
            )
        with self.assertRaises(PermissionError):
            self.kb.commit(1, [], "new index", 0, "blind-index", manifest=restricted)
        # Updating the peer cannot change the mounted fixed version or evidence.
        secret.publish_evidence("E2", "external", "future forbidden evidence")
        secret.commit(
            1, [page("Private", "updated private finding")], "# Updated", 2, "update"
        )
        still_fixed = self.kb.freeze(aliases=["study", "secret"])
        peer = next(row for row in still_fixed if row["alias"] == "secret")
        self.assertEqual(peer["version"], secret_v1["version"])
        self.assertEqual(peer["evidence_through"], 1)
        self.assertEqual(
            self.kb.read(still_fixed, "secret", private_ref),
            "private forbidden finding",
        )
        with self.assertRaises(PermissionError):
            self.kb.read(still_fixed, "secret", "E2")
        self.assertNotIn(str(secret.root), str(still_fixed))

    def test_evidence_provenance_is_also_scoped(self):
        secret = KnowledgeBase(self.root / "secret")
        private = self.publish(secret, content="forbidden")
        secret.commit(0, [], "# Secret", 1, "init")
        self.kb.configure(
            {
                "knowledge_libraries": [
                    dict(alias="secret", path=str(secret.root), version=1)
                ]
            }
        )
        self.publish(content="derived forbidden", source_refs=[private])
        restricted = self.kb.freeze()
        self.assertFalse(self.kb.search(restricted, "forbidden"))
        self.assertFalse(self.kb.pending(manifest=restricted)["evidence"])
        self.assertEqual(self.kb.pending(manifest=restricted)["evidence_through"], 0)
        with self.assertRaises(PermissionError):
            self.kb.read(restricted, "study", "E1")
        self.publish(identity="E2", content="Visible later observation")
        blocked = self.kb.pending(manifest=self.kb.freeze())
        self.assertFalse(blocked["evidence"])
        self.assertTrue(blocked["blocked_by_scope"])
        self.assertEqual(blocked["evidence_through"], 0)
        expanded = self.kb.pending(manifest=self.kb.freeze(aliases=["study", "secret"]))
        self.assertEqual(
            [r["reference"]["id"] for r in expanded["evidence"]], ["E1", "E2"]
        )

    def test_fixed_peer_references_reverse_refs_and_noop_ack(self):
        self.kb.commit(0, [page("Upstream", "Initial upstream")], "# Index", 0, "one")
        ref = dict(
            library_id=self.kb.library_id, kind="knowledge", id="Upstream", revision=1
        )
        self.kb.commit(
            1, [page("Downstream", "Dependent result", [ref])], "# Index", 0, "two"
        )
        self.assertEqual(self.kb.reverse_references(ref)[0]["id"], "Downstream")
        self.kb.commit(2, [page("Upstream", "New upstream")], "# Index", 0, "three")
        pending = self.kb.pending()
        self.assertEqual(pending["affected_pages"][0]["reference"]["id"], "Downstream")
        self.assertTrue(self.kb.status()["needs_review"])
        self.kb.commit(3, [], None, 0, "checked")
        self.assertFalse(self.kb.pending()["affected_pages"])
        self.assertFalse(self.kb.status()["needs_review"])
        self.assertEqual(
            self.kb.read(self.kb.freeze(), "study", ref), "Initial upstream"
        )

    def test_runtime_external_transaction_rolls_back_library_and_cursor(self):
        StateStore.create(self.kb.root, config={})
        self.kb = KnowledgeBase(self.kb.root)
        self.publish()
        frozen = self.kb.freeze()
        store = StateStore(self.kb.root)
        with store._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.kb.commit(
                0,
                [page("K1", "Atomic body")],
                "# Index",
                1,
                "external",
                manifest=frozen,
                conn=conn,
            )
            self.assertEqual(self.kb.status()["version"], 0)
            conn.rollback()
        self.assertEqual(self.kb.status()["processed_through"], 0)
        self.assertFalse(self.kb.search(self.kb.freeze(), "Atomic"))
        with store._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.kb.commit(
                0,
                [page("K1", "Atomic body")],
                "# Index",
                1,
                "external",
                manifest=frozen,
                conn=conn,
            )
            conn.commit()
        self.assertEqual(self.kb.status()["version"], 1)
        self.assertEqual(self.kb.status()["processed_through"], 1)

    def test_search_uses_fts_not_page_body_scans_and_can_rebuild(self):
        self.publish(content="Evidence apples")
        self.kb.commit(0, [page("K1", "Knowledge apples")], "# Index", 1, "publish")
        frozen = self.kb.freeze()
        with patch.object(
            KnowledgeBase, "_body", side_effect=AssertionError("body scan")
        ):
            self.assertEqual(len(self.kb.search(frozen, "apples")), 2)
        with self.kb._connection(self.kb.root) as conn:
            conn.execute("DELETE FROM kb_search")
        self.assertFalse(self.kb.search(frozen, "apples"))
        self.kb.rebuild_search_index()
        self.assertEqual(len(self.kb.search(frozen, "apples")), 2)

    def test_legacy_original_identity_and_readonly_markdown(self):
        entry = dict(
            id="Old claim",
            expected_revision=0,
            kind="interpretation",
            statement="Historical apples",
            conditions=["Synthetic conditions"],
            source_refs=[],
            measurement_ids=[],
            interpretation="Unverified",
            limitations=["Not reproduced"],
        )
        row = dict(
            id=entry["id"],
            revision=1,
            kind="interpretation",
            source_role="scout",
            publication_id=7,
            entry_index=0,
        )
        snapshot = dict(version=7, knowledge=[row])
        publications = {7: dict(knowledge=[entry], reviews=[], corrections=[])}
        refs = self.kb.import_legacy(snapshot, publications)
        self.assertEqual(len(refs), 1)
        self.assertEqual(self.kb.import_legacy(snapshot, publications), [])
        frozen = self.kb.freeze()
        body = self.kb.read(frozen, "study", refs[0])
        self.assertIn("Original revision: 1", body)
        self.assertIn("Publication: 7", body)
        self.assertEqual(refs[0]["id"], "Old claim")
        self.assertEqual(self.kb.search(frozen, "Historical")[0]["reference"], refs[0])
        changed = deepcopy(publications)
        changed[7]["knowledge"][0]["statement"] = "Changed identity"
        with self.assertRaises(ConflictError):
            self.kb.import_legacy(snapshot, changed)

    def test_page_named_index_and_write_symlink_rejection(self):
        self.kb.commit(
            0, [page("index", "Page named index")], "# Actual index", 0, "named"
        )
        self.assertEqual(
            self.kb.read(self.kb.freeze(), "study", "index"), "Page named index"
        )
        outside = self.root / "outside"
        outside.mkdir()
        kb = KnowledgeBase(self.root / "linked")
        kb.freeze()
        (kb.folder / "versions").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            kb.commit(0, [page("K1", "No escape")], "# Index", 0, "symlink")
        self.assertFalse(list(outside.iterdir()))

    def test_path_traversal_rejected_and_mount_never_initialized(self):
        with self.assertRaises(ValueError):
            self.kb.read(self.kb.freeze(), "study", "../private")
        missing = self.root / "missing"
        with self.assertRaises(ValueError):
            self.kb.configure(
                {
                    "knowledge_libraries": [
                        dict(alias="missing", path=str(missing), version=0)
                    ]
                }
            )
        self.assertFalse(missing.exists())
        with self.assertRaises(ValueError):
            self.kb.commit(0, [page("../escape", "bad")], None, 0, "invalid")


if __name__ == "__main__":
    unittest.main()
