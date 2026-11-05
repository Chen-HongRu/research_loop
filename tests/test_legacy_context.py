"""The context facade runs on real serial Study calls and receipt recovery."""

import tempfile
import unittest
from pathlib import Path

from research_loop import Study, initialize
from research_loop.runtime.context import ContextPackage
from research_loop.runtime.legacy_context import capture_call
from test_core import ProcessLost, RecordedHarness, read


class LegacyContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "study"
        initialize(
            self.root,
            goal="Synthetic context provenance check.",
            strategy={"selection": "direct", "review": "full"},
        )

    def test_real_study_calls_capture_exact_inputs_without_extra_calls(self):
        harness = RecordedHarness()
        state = Study(self.root, harness).run()
        self.assertEqual(state["call_count"], 6)
        self.assertEqual(len(harness.calls), 6)
        for call in harness.calls:
            folder = call["run_dir"]
            package = ContextPackage.from_dict(
                {
                    "context": read(folder / "context.json"),
                    "manifest": read(folder / "context-manifest.json"),
                }
            )
            self.assertEqual(package.context, call["context"])
            self.assertEqual(list(package.context), list(call["context"]))
            manifest = package.manifest
            self.assertEqual(manifest["role"], call["phase"])
            self.assertEqual(manifest["policy_ref"], "legacy-context-v1")
            self.assertEqual(manifest["budget"], {})
            self.assertIsNone(manifest["tokens"]["actual_usage"])
            self.assertTrue(
                all(s["disposition"] == "provided" for s in manifest["sources"])
            )
            for source in manifest["sources"]:
                self.assertEqual(source["revision"], str(folder.relative_to(self.root)))
                self.assertIn(source["ref"], manifest["allowed_evidence"])
            if call["phase"] == "review":
                self.assertTrue(
                    manifest["revisions"]["knowledge"].endswith("review.before.json")
                )

    def test_completed_execute_recovery_preserves_original_manifest(self):
        harness = RecordedHarness()

        def interrupted(call):
            if call["phase"] == "execute":
                raise ProcessLost()

        harness.after = interrupted
        study = Study(self.root, harness)
        with self.assertRaises(ProcessLost):
            study.run()
        attempt = self.root / study.status()["calls"]["execute"]
        original = (attempt / "context-manifest.json").read_bytes()
        harness.after = None
        Study(self.root, harness).run()
        self.assertEqual((attempt / "context-manifest.json").read_bytes(), original)
        self.assertEqual(sum(c["phase"] == "execute" for c in harness.calls), 1)

    def test_continuation_records_original_scientific_inputs(self):
        context = {
            "execution_continuation": {"plan_ref": "iterations/0001/plan.json"},
            "research_inputs": {"version": "synthetic"},
        }
        package = capture_call(
            study_id="synthetic",
            iteration_ref="iterations/0002",
            attempt_ref="iterations/0002/execute/attempt-001",
            role="execute",
            context=context,
            state={},
        )
        source = next(
            s for s in package.manifest["sources"] if s["key"] == "research_inputs"
        )
        self.assertEqual(source["id"], "iterations/0001/context/research_inputs.json")
        self.assertEqual(package.context, context)


if __name__ == "__main__":
    unittest.main()
