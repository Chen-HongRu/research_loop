import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location(
    "dashboard",
    Path(__file__).resolve().parents[3] / "integrations/protok/dashboard.py",
)
DASH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DASH)


class DashboardTests(unittest.TestCase):
    def test_offline_snapshot_needs_neither_owner_token_nor_canonical_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "control").mkdir()
            (root / "control/backend-snapshot.json").write_text(
                json.dumps(
                    {
                        "observed_at": 123,
                        "records": [],
                        "used_gpu_seconds": 19084,
                        "scored_records": 19,
                        "outstanding": [],
                    }
                )
            )
            with patch.object(
                DASH, "ProTokBackend", side_effect=AssertionError("must stay offline")
            ):
                result = DASH.snapshot(root, snapshot_only=True)
            self.assertTrue(result["snapshot_only"])
            self.assertEqual(result["backend_observed_at"], 123)
            self.assertEqual(result["gpu_seconds"], 19084)

    def test_only_published_matching_verified_measurement_is_plotted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def put(name, value):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value))

            put("control/ownership.json", {"project": temporary})
            put(
                "study/state.json",
                {"iteration": 2, "status": "running", "phase": "execute"},
            )
            for identity in ("0001", "0002"):
                prefix = f"study/iterations/{identity}"
                put(
                    prefix + "/iteration.json",
                    {
                        "id": identity,
                        "status": "planned",
                        "base_experiment": "0001" if identity == "0002" else None,
                    },
                )
                put(
                    f"control/jobs/{identity}/result.json",
                    {"value": 2.4, "fingerprint": identity},
                )
                put(f"control/jobs/{identity}/job.json", {"fingerprint": identity})
                put(
                    prefix + "/review/attempt-001/response.json",
                    {
                        "candidate_reviews": [
                            {"candidate_id": identity, "valid": True, "verified": True}
                        ]
                    },
                )
            put("study/iterations/0001/publication.json", {})
            backend = Mock()
            backend.inspect.return_value = {
                "records": [{"fingerprint": "0002", "summary": {"value": 2.3}}],
                "used_gpu_seconds": 10,
                "scored_records": 2,
                "outstanding": [],
            }
            with patch.object(DASH, "ProTokBackend", return_value=backend):
                nodes = DASH.snapshot(root)["nodes"]
                self.assertEqual(nodes[0]["value"], 2.4)
                self.assertIsNone(nodes[1]["value"])
                self.assertEqual(nodes[1]["provisional_value"], 2.3)
                self.assertEqual(
                    (nodes[1]["status"], nodes[1]["phase"]), ("running", "execute")
                )
                put("study/iterations/0002/publication.json", {})
                put(
                    "study/iterations/0002/review/attempt-001/response.json",
                    {
                        "candidate_reviews": [
                            {"candidate_id": "other", "valid": True, "verified": True}
                        ]
                    },
                )
                self.assertIsNone(DASH.snapshot(root)["nodes"][1]["value"])


if __name__ == "__main__":
    unittest.main()
