"""Long-training presentation stays separate from short-protocol scoring."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from integrations.protok import dashboard as dash
from integrations.protok import explanation as ex


class LongTrainingDashboardTests(unittest.TestCase):
    def test_real_long_run_metadata_is_read_from_the_child_study_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lane = root / "study/long-runs/L0123456789abcdef"
            lane.mkdir(parents=True)
            (lane / "request.json").write_text(
                json.dumps({"id": lane.name, "source_iteration": 1})
            )
            pending = dash.details(root, "0001")["long_training"]
            self.assertEqual(pending[0]["status"], "queued")
            self.assertNotIn("result", pending[0])
            (lane / "result.json").write_text(
                json.dumps(
                    {"review": {"markdown": "reviewed"}, "result": {"curve": []}}
                )
            )
            reviewed = dash.details(root, "0001")["long_training"]
            self.assertEqual(reviewed[0]["status"], "reviewed")
            self.assertEqual(reviewed[0]["result"]["review"]["markdown"], "reviewed")

    def test_snapshot_and_details_include_independent_runs_without_frontier_points(
        self,
    ):
        runs = [
            {
                "id": "L0123456789abcdef",
                "request": {"source_iteration": 1},
                "status": "running",
                "phase": "execute",
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "control").mkdir()
            (root / "control/backend-snapshot.json").write_text(
                json.dumps(
                    {
                        "records": [],
                        "used_gpu_seconds": 0,
                        "scored_records": 0,
                        "outstanding": [],
                    }
                )
            )
            with patch.object(dash, "long_training_snapshot", return_value=runs):
                snapshot = dash.snapshot(root, snapshot_only=True)
                self.assertEqual(snapshot["long_training"], runs)
                self.assertEqual(snapshot["frontier"]["points"], [])
                self.assertEqual(snapshot["nodes"], [])
                self.assertEqual(dash.details(root, "0001")["long_training"], runs)
                self.assertEqual(dash.details(root, "0002")["long_training"], [])

    def test_explanation_uses_frozen_reviewed_evidence_and_invalidates_on_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "study/iterations/0001"
            (folder / "context").mkdir(parents=True)
            (folder / "iteration.json").write_text(json.dumps({"status": "reviewed"}))
            (folder / "publication.json").write_text(json.dumps({"state_after": {}}))
            frozen = folder / "context/long_training_results.json"
            frozen.write_text(
                json.dumps(
                    [
                        {
                            "id": "L0123456789abcdef",
                            "review": {"markdown": "reviewed knowledge"},
                        }
                    ]
                )
            )
            material = ex.collect_material(root, "0001")
            evidence = [
                source
                for source in material["sources"]
                if source["kind"] == "long_training_results"
            ]
            self.assertEqual(len(evidence), 1)
            self.assertIn("reviewed knowledge", evidence[0]["text"])
            self.assertIn(
                str(frozen.relative_to(root)),
                [dep[0] for dep in material["dependencies"]],
            )
            cache = ex._cache(root, "0001")
            cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps(material))
            frozen.write_text("[]")
            self.assertEqual(ex._cached_state(root, "0001")["status"], "stale")

    def test_new_frozen_long_evidence_invalidates_older_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "study/iterations/0001"
            (folder / "context").mkdir(parents=True)
            (folder / "iteration.json").write_text(json.dumps({"status": "reviewed"}))
            (folder / "publication.json").write_text(json.dumps({"state_after": {}}))
            material = ex.collect_material(root, "0001")
            cache = ex._cache(root, "0001")
            cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps(material))
            (folder / "context/long_training_results.json").write_text("[]")
            self.assertEqual(ex._cached_state(root, "0001")["status"], "stale")
