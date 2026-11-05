import json
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from research_loop import Study, initialize as initialize_legacy
from research_loop.cli import main
from research_loop.runtime import initialize, publish_artifact
from research_loop.runtime.files import publish_json, validate_artifact_ref
from research_loop.runtime.format import RuntimeFormatError
from research_loop.tasks import TaskQueue


class RuntimeBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "study"

    def test_initialize_publishes_identity_without_running_anything(self):
        store = initialize(self.root, config={"goal": "Synthetic control test"})
        marker = json.loads((self.root / "runtime.json").read_text())
        self.assertEqual(marker["format"], "sqlite-v1")
        self.assertEqual(marker["study_id"], store.snapshot()["study_id"])
        self.assertEqual(store.snapshot()["attempts"], [])
        self.assertFalse((self.root / "state.json").exists())
        with self.assertRaises(ValueError):
            initialize(self.root, config={})

    def test_artifact_publication_never_replaces_a_record(self):
        self.root.mkdir()
        reference = publish_artifact(self.root, {"result": 1})
        path = validate_artifact_ref(self.root, reference)
        with self.assertRaises(FileExistsError):
            publish_json(path, {"result": 2})
        self.assertEqual(json.loads(path.read_text()), {"result": 1})
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_artifact_reference_checks_metadata_not_contents(self):
        self.root.mkdir()
        reference = publish_artifact(self.root, {"result": 1})
        with patch.object(Path, "read_text", side_effect=AssertionError("reread")):
            validate_artifact_ref(self.root, reference)
        path = self.root / reference
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "read-only"):
            validate_artifact_ref(self.root, reference)
        path.unlink()
        path.symlink_to(self.root / "state.json")
        with self.assertRaisesRegex(ValueError, "symlinks"):
            validate_artifact_ref(self.root, reference)
        for invalid in ("../state.json", "state.json", reference + "/../other"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_artifact_ref(self.root, invalid)

    def test_directory_sync_failure_does_not_create_database_evidence(self):
        store = initialize(self.root, config={})
        with store.coordinator() as owner:
            attempt = owner.begin_attempt("scout", request_id="scout-1")
            with (
                patch(
                    "research_loop.runtime.files._sync_directory",
                    side_effect=[None, OSError("injected directory fsync failure")],
                ),
                self.assertRaises(OSError),
            ):
                reference = publish_artifact(self.root, {"result": 1})
                owner.accept_attempt(
                    attempt["id"],
                    attempt["worker_token"],
                    reference,
                    expected_revision=None,
                )
            # A fully written but unaccepted remnant is not a committed result.
            self.assertEqual(len(list((self.root / "artifacts").glob("*.json"))), 1)
            record = store.snapshot()["attempts"][0]
            self.assertEqual(record["status"], "active")
            self.assertIsNone(record["output_ref"])

    def test_legacy_entry_points_refuse_runtime_marker_or_database(self):
        self.root.mkdir()
        for name in ("runtime.json", "runtime.sqlite3"):
            with self.subTest(name=name):
                (self.root / name).write_text("interrupted initialization")
                for constructor in (Study, TaskQueue):
                    with self.assertRaises(RuntimeFormatError):
                        constructor(self.root)
                errors = StringIO()
                with redirect_stderr(errors):
                    self.assertEqual(main(["run", str(self.root)]), 1)
                self.assertIn("SQLite runtime", errors.getvalue())
                (self.root / name).unlink()

    def test_existing_legacy_objects_refuse_a_later_format_switch(self):
        initialize_legacy(self.root, goal="Synthetic boundary check")
        study = Study(self.root)
        queue = TaskQueue(self.root)
        original = (self.root / "state.json").read_bytes()
        (self.root / "runtime.json").write_text('{"format": "sqlite-v1"}')
        with self.assertRaises(RuntimeFormatError), study._lock():
            self.fail("legacy writer admitted")
        with self.assertRaises(RuntimeFormatError):
            study._save({"invalid": True})
        with self.assertRaises(RuntimeFormatError):
            queue.submit({"id": "A", "instruction": "synthetic request"})
        self.assertEqual((self.root / "state.json").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
