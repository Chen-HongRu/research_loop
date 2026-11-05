"""Real local storage, frozen scopes and publication failure boundaries."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from research_loop.runtime.knowledge import KnowledgeBase, LivePublicationError
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
        live = self.kb.live(["study"], role="scout")
        files = Path(live["entries"][0]["path"]) / "files/pages"
        self.assertEqual({path.stem for path in files.glob("*.md")}, {"K2", "K3"})
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
        self.assertEqual({path.stem for path in files.glob("*.md")}, {"K4"})
        self.assertEqual(self.kb.read(current, "study", "K2"), "Split left")

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
        self.assertNotIn("forbidden", self.kb.read_index(restricted))
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
        with self.assertRaises(PermissionError):
            self.kb.read(self.kb.freeze(), "study", "K1")
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

    def test_arbitrary_file_delta_binary_history_and_missing_index(self):
        self.kb.commit(0, [], "# Actual index", 0, "initial")
        entry = self.kb.live(["study"], role="plan")["entries"][0]
        current = Path(entry["path"])
        stable = Path(entry["isolation_root"]).stat().st_ino
        self.kb.commit(
            1,
            [],
            None,
            0,
            "files",
            files={
                "notes/rough/observation.txt": "plain\n",
                "payload.bin": {"base64": "AP8="},
            },
            remove_paths=["index.md"],
        )
        self.assertEqual(
            (current / "files/notes/rough/observation.txt").read_bytes(), b"plain\n"
        )
        self.assertEqual((current / "files/payload.bin").read_bytes(), b"\x00\xff")
        self.assertFalse((current / "files/index.md").exists())
        self.assertEqual(Path(entry["isolation_root"]).stat().st_ino, stable)
        citation = f"kb:{self.kb.library_id}:file:notes/rough/observation.txt@1"
        self.assertEqual(
            json.loads((current / "revisions.json").read_text())[
                "notes/rough/observation.txt"
            ],
            citation,
        )
        self.assertEqual(
            self.kb.read(self.kb.history(1), "study", "index.md"), "# Actual index"
        )
        with self.assertRaises(PermissionError):
            self.kb.read(self.kb.history(2), "study", "index.md")
        self.kb.commit(
            2, [], None, 0, "delete", remove_paths=["notes/rough/observation.txt"]
        )
        self.assertFalse((current / "files/notes/rough/observation.txt").exists())
        self.assertEqual(
            self.kb.read(self.kb.history(2), "study", "notes/rough/observation.txt"),
            "plain\n",
        )
        self.assertEqual(
            self.kb.read(self.kb.history(3), "study", "payload.bin"), {"base64": "AP8="}
        )
        self.kb.commit(
            3, [], None, 0, "recreate", files={"notes/rough/observation.txt": "again"}
        )
        self.assertTrue(
            json.loads((current / "revisions.json").read_text())[
                "notes/rough/observation.txt"
            ].endswith("@2")
        )

    def test_private_evidence_stays_outside_complete_ordinary_live_tree(self):
        self.kb.publish_evidence("private", "synthetic", "private sentinel")
        entry = self.kb.live(["study"], role="plan")["entries"][0]
        current = Path(entry["path"])
        self.assertEqual(list((current / "files").iterdir()), [])
        self.assertNotIn(
            "private sentinel",
            "\n".join(
                path.read_text() for path in current.rglob("*") if path.is_file()
            ),
        )
        with patch.object(
            KnowledgeBase,
            "freeze",
            side_effect=AssertionError("ordinary open froze content"),
        ):
            same = self.kb.live(["study"], role="scout")
        self.assertEqual(same["entries"][0]["path"], str(current))
        self.assertEqual(
            same["references"],
            [
                dict(
                    library_id=self.kb.library_id,
                    kind="library",
                    id="library",
                    revision=0,
                )
            ],
        )

    def test_exchange_failure_keeps_old_complete_files_and_retry_activates(self):
        from research_loop.runtime import live_knowledge

        self.kb.commit(0, [], None, 0, "baseline", files={"old.txt": "old"})
        entry = self.kb.live(["study"], role="plan")["entries"][0]
        current, allowed = Path(entry["path"]), Path(entry["isolation_root"])
        self.assertEqual(self.kb.published_version(), 1)
        old = current.stat().st_ino

        def fail(staged, target):
            self.assertFalse(staged.is_relative_to(allowed))
            self.assertEqual(target, current)
            self.assertFalse((current / "files/nested/data.csv").exists())
            raise OSError("synthetic exchange failure")

        with patch.object(live_knowledge, "exchange", side_effect=fail) as operation:
            with self.assertRaises(LivePublicationError):
                self.kb.commit(
                    1, [], None, 0, "accepted", files={"nested/data.csv": "a,b\n1,2\n"}
                )
            reopened = self.kb.live(["study"], role="plan")["entries"][0]
            self.assertEqual(
                (Path(reopened["path"]) / "files/old.txt").read_text(), "old"
            )
            self.assertFalse(
                (Path(reopened["path"]) / "files/nested/data.csv").exists()
            )
        self.assertEqual(operation.call_count, 2)
        self.assertEqual(current.stat().st_ino, old)
        self.assertEqual({path.name for path in allowed.iterdir()}, {"current"})
        self.assertTrue(self.kb.live_status()["publication_pending"])
        self.assertEqual(self.kb.head(), 2)
        self.assertEqual(self.kb.published_version(), 1)
        receipt = self.kb.commit(
            1, [], None, 0, "accepted", files={"nested/data.csv": "a,b\n1,2\n"}
        )
        self.assertEqual(receipt["version"], 2)
        self.assertEqual((current / "files/nested/data.csv").read_text(), "a,b\n1,2\n")
        self.assertFalse(self.kb.live_status()["publication_pending"])

    def test_external_rollback_cannot_activate_or_publish_files(self):
        StateStore.create(self.kb.root, config={})
        self.kb = KnowledgeBase(self.kb.root)
        current = Path(self.kb.live(["study"], role="plan")["entries"][0]["path"])
        store = StateStore(self.kb.root)
        with store._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.kb.commit(
                0, [], None, 0, "rolled-back", files={"future.txt": "future"}, conn=conn
            )
            self.kb.refresh_live()
            self.assertFalse((current / "files/future.txt").exists())
            self.assertEqual(self.kb.published_version(), 0)
            conn.rollback()
        self.assertEqual(self.kb.head(), 0)
        self.assertFalse((current / "files/future.txt").exists())

    def test_cas_same_base_file_managers_and_staging_failure(self):
        self.kb.live(["study"], role="plan")
        base = self.kb.freeze()
        self.kb.commit(0, [], None, 0, "first", files={"a.txt": "A"}, manifest=base)
        with self.assertRaises(ConflictError):
            self.kb.commit(
                0, [], None, 0, "second", files={"b.txt": "B"}, manifest=base
            )
        with patch(
            "research_loop.runtime.knowledge._blob",
            side_effect=OSError("synthetic disk failure"),
        ):
            with self.assertRaises(OSError):
                self.kb.commit(1, [], None, 0, "failed", files={"b.txt": "B"})
        self.assertEqual(self.kb.head(), 1)
        self.assertEqual(self.kb.published_version(), 1)

    def test_filesystem_case_collision_rejected_before_acceptance(self):
        with TemporaryDirectory(dir=self.root) as folder:
            (Path(folder) / "Probe").write_text("")
            try:
                with (Path(folder) / "probe").open("x"):
                    pass
            except FileExistsError:
                pass
            else:
                self.skipTest("This filesystem permits case-distinct names")
        current = Path(self.kb.live(["study"], role="plan")["entries"][0]["path"])
        self.kb.commit(0, [], None, 0, "old", files={"note.md": "old"})
        with self.assertRaises(ValueError):
            self.kb.commit(1, [], None, 0, "collision", files={"Note.md": "new"})
        self.assertEqual(self.kb.head(), 1)
        self.assertEqual((current / "files/note.md").read_text(), "old")
        self.kb.commit(
            1, [], None, 0, "rename", files={"Note.md": "new"}, remove_paths=["note.md"]
        )
        self.assertEqual(self.kb.head(), 2)
        self.assertEqual(self.kb.read(self.kb.history(1), "study", "note.md"), "old")

    def test_file_directory_collision_is_rejected_and_next_commit_succeeds(self):
        self.kb.live(["study"], role="plan")
        self.kb.commit(0, [], None, 0, "old", files={"note": "old"})
        with self.assertRaises(ValueError):
            self.kb.commit(1, [], None, 0, "collision", files={"note/child": "new"})
        self.assertEqual(self.kb.head(), 1)
        self.kb.commit(
            1,
            [],
            None,
            0,
            "replace",
            files={"note/child": "new"},
            remove_paths=["note"],
        )
        self.assertEqual(self.kb.read(self.kb.history(2), "study", "note/child"), "new")

    def test_peer_fixed_tree_identity_and_its_own_history_boundary(self):
        peer = KnowledgeBase(self.root / "peer")
        replacement = KnowledgeBase(self.root / "replacement")
        for library, data in ((peer, "A"), (replacement, "B")):
            library.commit(0, [], None, 0, "initial", files={"notes/data": data})
        config = {
            "knowledge_libraries": [dict(alias="peer", path=str(peer.root), version=1)]
        }
        consumer = KnowledgeBase(self.kb.root, config=config)
        live = consumer.live(["peer"], role="plan")
        current = Path(live["entries"][0]["path"])
        self.assertEqual((current / "files/notes/data").read_text(), "A")
        peer.commit(1, [], None, 0, "next", files={"notes/data": "A2"})
        self.assertEqual((current / "files/notes/data").read_text(), "A")
        self.assertEqual(
            consumer.read(consumer.history(1, alias="peer"), "peer", "notes/data"), "A"
        )
        with self.assertRaises(PermissionError):
            consumer.history(2, alias="peer")
        peer.root.rename(self.root / "saved-peer")
        replacement.root.rename(peer.root)
        with self.assertRaises(PermissionError):
            KnowledgeBase(consumer.root, config=config).live(["peer"], role="plan")
        self.assertEqual((current / "files/notes/data").read_text(), "A")
        consumer.live(["study"], role="scout")

    def test_post_exchange_error_is_an_activated_complete_publication(self):
        from research_loop.runtime import live_knowledge

        entry = self.kb.live(["study"], role="plan")["entries"][0]
        actual = live_knowledge.exchange

        def after(staged, current):
            actual(staged, current)
            raise OSError("synthetic error after successful exchange")

        with patch.object(live_knowledge, "exchange", side_effect=after):
            receipt = self.kb.commit(
                0, [], None, 0, "active", files={"complete.txt": "complete"}
            )
        self.assertEqual(receipt["version"], 1)
        self.assertEqual(self.kb.published_version(), 1)
        self.assertEqual(
            (Path(entry["path"]) / "files/complete.txt").read_text(), "complete"
        )
        self.kb.refresh_live()
        self.assertFalse(self.kb.live_status()["publication_pending"])


if __name__ == "__main__":
    unittest.main()
