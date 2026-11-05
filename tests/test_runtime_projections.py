"""Rebuild and publish disposable projections from real accepted SQLite state."""

from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from examples.runtime_publication import EVALUATION, SyntheticSession, make_store
from research_loop.runtime import projections
from research_loop.runtime.projections import ProjectionWriter
from research_loop.runtime.publication import Publisher
from research_loop.runtime.store import ConflictError


class RuntimeProjectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = make_store(self.root)
        context = self.store.coordinator()
        self.owner = context.__enter__()
        self.addCleanup(lambda: context.__exit__(None, None, None))
        self.demo = SyntheticSession(self.owner)
        self.publisher = Publisher(self.owner)
        iteration, self.measurement = self.demo.trial("baseline", 1.0)
        self.publisher.publish(self.demo.review(iteration, self.measurement))
        self.writer = ProjectionWriter(self.owner)
        self.pointer = self.writer.rebuild()

    def documents(self, pointer):
        return (
            json.loads((self.root / pointer["frontiers_ref"]).read_text()),
            (self.root / pointer["research_ref"]).read_text(),
        )

    def late_scout(self):
        return self.demo.accept(
            "scout",
            dict(
                knowledge=[
                    dict(
                        id="late-source",
                        expected_revision=0,
                        kind="external",
                        statement="Synthetic late source",
                        conditions=["Fixture only"],
                        source_refs=["https://example.com/synthetic"],
                        measurement_ids=[],
                        interpretation="",
                        limitations=[
                            "No actual source retrieval or scientific validation"
                        ],
                    )
                ],
                reviews=[],
                corrections=[],
            ),
        )

    def test_current_pointer_targets_one_complete_consistent_generation(self):
        pointer = json.loads((self.root / "views/current.json").read_text())
        self.assertEqual(pointer, self.pointer)
        frontiers, research = self.documents(pointer)
        self.assertEqual(frontiers["version"], pointer["version"])
        self.assertIn(f"Publication version: {pointer['version']}", research)
        view = frontiers["global"][EVALUATION["id"]]
        self.assertEqual(view["candidate_ids"], [self.measurement["id"]])
        self.assertIn("Synthetic receipt recorded for baseline.", research)
        self.assertEqual(
            Path(pointer["frontiers_ref"]).parent, Path(pointer["research_ref"]).parent
        )

    def test_corrupt_or_deleted_cache_reconstructs_without_changing_authority(self):
        expected = self.documents(self.pointer)
        control_before, ledger_before = self.store.snapshot(), self.publisher.snapshot()
        for mode in ("corrupt", "delete"):
            if mode == "corrupt":
                for path in (
                    self.root / "views/current.json",
                    self.root / self.pointer["frontiers_ref"],
                    self.root / self.pointer["research_ref"],
                ):
                    path.chmod(0o644)
                    path.write_text("corrupt derived cache")
            else:
                shutil.rmtree(self.root / "views")
            with self.subTest(mode=mode):
                rebuilt = self.writer.rebuild()
                self.assertEqual(self.documents(rebuilt), expected)
                self.assertEqual(self.store.snapshot(), control_before)
                self.assertEqual(self.publisher.snapshot(), ledger_before)
                self.assertEqual(
                    json.loads((self.root / "views/current.json").read_text()), rebuilt
                )
                self.pointer = rebuilt

    def test_pointer_replace_failure_retains_previous_complete_generation(self):
        previous = (self.root / "views/current.json").read_bytes()
        documents = self.documents(self.pointer)
        with patch.object(
            projections.os,
            "replace",
            side_effect=OSError("injected pointer replacement failure"),
        ):
            with self.assertRaisesRegex(OSError, "pointer replacement"):
                self.writer.rebuild()
        self.assertEqual((self.root / "views/current.json").read_bytes(), previous)
        self.assertEqual(self.documents(self.pointer), documents)
        self.assertEqual(self.publisher.snapshot()["version"], self.pointer["version"])

    def test_publication_during_render_rejects_stale_generation_and_keeps_pointer(self):
        call = self.late_scout()
        previous = (self.root / "views/current.json").read_bytes()
        original_render = projections.render_research
        errors = []

        def render_after_publication(snapshot, publications):
            def publish():
                try:
                    self.publisher.publish(call)
                except BaseException as error:
                    errors.append(error)

            thread = threading.Thread(target=publish)
            thread.start()
            thread.join(5)
            self.assertFalse(
                thread.is_alive(), "render held the coordinator mutation lock"
            )
            self.assertEqual(errors, [])
            return original_render(snapshot, publications)

        with patch.object(projections, "render_research", render_after_publication):
            with self.assertRaisesRegex(ConflictError, "Publication advanced"):
                self.writer.rebuild()
        self.assertEqual((self.root / "views/current.json").read_bytes(), previous)
        self.assertEqual(self.publisher.snapshot()["version"], 2)
        rebuilt = self.writer.rebuild()
        self.assertEqual(rebuilt["version"], 2)
        self.assertIn("Synthetic late source", self.documents(rebuilt)[1])

    def test_payload_reads_and_rendering_are_outside_write_transactions(self):
        inside = [False]
        original_transaction = self.owner._transaction
        original_read = Path.read_text
        original_build, original_render = (
            projections.build_views,
            projections.render_research,
        )

        @contextmanager
        def tracked():
            with original_transaction() as transaction:
                inside[0] = True
                try:
                    yield transaction
                finally:
                    inside[0] = False

        def read_outside(path, *args, **kwargs):
            self.assertFalse(
                inside[0], "projection read payload inside write transaction"
            )
            return original_read(path, *args, **kwargs)

        def build_outside(*args, **kwargs):
            self.assertFalse(inside[0], "frontier built inside write transaction")
            return original_build(*args, **kwargs)

        def render_outside(*args, **kwargs):
            self.assertFalse(inside[0], "research rendered inside write transaction")
            return original_render(*args, **kwargs)

        before = self.store.snapshot()
        with (
            patch.object(self.owner, "_transaction", tracked),
            patch.object(Path, "read_text", read_outside),
            patch.object(projections, "build_views", build_outside),
            patch.object(projections, "render_research", render_outside),
        ):
            rebuilt = self.writer.rebuild()
        self.assertEqual(rebuilt["version"], 1)
        self.assertEqual(self.store.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
