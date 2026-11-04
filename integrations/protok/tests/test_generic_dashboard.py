"""Synthetic published Study fixtures; no ProTok project, jobs or data required."""

import json
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from integrations.protok import dashboard as dash
from research_loop.evaluation import build_frontier


def definition(
    name="accuracy", direction="maximize", unit="fraction", identity="generic-v1"
):
    return {
        "id": identity,
        "comparison_scope": identity,
        "protocol": "Synthetic fixed evaluation",
        "data_role": "development",
        "allow_feedback": True,
        "objectives": [{"name": name, "direction": direction, "unit": unit}],
        "constraints": [],
    }


def put(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def published_study(root, evaluation, values, *, start=1):
    put(root, "study/study.json", {"global_evaluation": evaluation})
    put(
        root,
        "study/state.json",
        {"iteration": start + len(values) - 1, "phase": "done", "status": "ready"},
    )
    candidates = []
    prior = (
        json.loads((root / "study/frontiers.json").read_text())
        if (root / "study/frontiers.json").exists()
        else {"global": {}}
    )
    for number, value in enumerate(values, start):
        identity = f"{number:04d}"
        candidate = {
            "id": identity + ":candidate",
            "experiment_id": identity,
            "artifact": {"path": "candidate", "version": identity},
            "evaluation_id": evaluation["id"],
            "comparison_scope": evaluation["comparison_scope"],
            "metrics": {evaluation["objectives"][0]["name"]: value},
            "verified": True,
            "valid": True,
            "evidence": ["measurement.json"],
        }
        candidates.append(candidate)
        view = build_frontier(evaluation, candidates)
        view["allow_feedback"] = evaluation["allow_feedback"]
        after = {"global": {**prior["global"], evaluation["id"]: view}}
        folder = "study/iterations/" + identity
        put(
            root,
            folder + "/iteration.json",
            {"id": identity, "status": "reviewed", "created_at": number},
        )
        put(
            root,
            folder + "/candidates.json",
            {
                "global_evaluation_id": evaluation["id"],
                "definitions": [evaluation],
                "candidates": [candidate],
                "corrections": [],
            },
        )
        put(
            root,
            folder + "/publication.json",
            {
                "before": {"frontiers.json": json.dumps(prior)},
                "writes": {"frontiers.json": json.dumps(after)},
            },
        )
        prior = after
    put(root, "study/frontiers.json", prior)


class GenericDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_maximize_uses_published_candidates_without_backend_or_export(self):
        published_study(self.root, definition(), [0.6, 0.8, 0.7])
        with patch.object(
            dash, "ProTokBackend", side_effect=AssertionError("ProTok accessed")
        ):
            result = dash.view_snapshot(self.root)
            archived = dash.view_snapshot(self.root, snapshot_only=True)
        frontier = result["frontier"]
        self.assertTrue(frontier["available"])
        self.assertEqual(
            (frontier["metric"], frontier["unit"], frontier["direction"]),
            ("accuracy", "fraction", "maximize"),
        )
        self.assertEqual(
            [p["improved"] for p in frontier["points"]], [False, True, False]
        )
        self.assertAlmostEqual(frontier["points"][1]["delta"], 0.2)
        self.assertEqual(frontier["current_experiment_ids"], ["active:0002"])
        self.assertEqual([n["value"] for n in result["nodes"]], [0.6, 0.8, 0.7])
        self.assertEqual(result["accounting_status"], "not_applicable")
        self.assertFalse(result["accounting_available"])
        self.assertFalse(result["capabilities"]["protok"])
        self.assertEqual(result["long_training"], [])
        self.assertEqual(archived["frontier"]["points"], frontier["points"])

    def test_minimize_preserves_custom_units_and_tiny_exact_gain(self):
        published_study(
            self.root, definition("latency", "minimize", "ms"), [10, 10 - 1e-10, 11]
        )
        frontier = dash.frontier_snapshot(self.root)
        self.assertEqual(frontier["unit"], "ms")
        self.assertTrue(frontier["points"][1]["improved"])
        self.assertGreater(frontier["points"][1]["delta"], 0)
        self.assertFalse(frontier["points"][2]["improved"])

    def test_other_protocol_history_is_available_separately(self):
        archive = self.root / "archive"
        published_study(
            archive, definition("error", "minimize", "mm", "older-v1"), [5, 4]
        )
        published_study(self.root, definition(), [0.8])
        result = dash.view_snapshot(self.root, history_roots=[archive])
        self.assertEqual([p["value"] for p in result["frontier"]["points"]], [0.8])
        groups = {view["source_id"]: view for view in result["frontiers"]}
        self.assertEqual(groups["history-1"]["metric"], "error")
        self.assertEqual([p["value"] for p in groups["history-1"]["points"]], [5, 4])
        self.assertEqual(
            groups["history-1"]["current_experiment_ids"], ["history-1:0002"]
        )
        self.assertEqual(groups["active"]["unit"], "fraction")
        self.assertTrue(groups["history-1"]["points"][-1]["current"])

    def test_equal_ids_with_changed_units_do_not_merge(self):
        archive = self.root / "archive"
        published_study(archive, definition(unit="percent"), [80])
        published_study(self.root, definition(), [0.8])
        result = dash.view_snapshot(self.root, history_roots=[archive])
        self.assertEqual(len(result["frontier"]["points"]), 1)
        self.assertFalse(result["frontier"]["sources"][0]["compatible"])
        self.assertEqual(len(result["frontiers"]), 2)

    def test_missing_frontier_unpublished_and_feedback_boundaries(self):
        published_study(self.root, definition(), [0.8])
        put(
            self.root,
            "study/iterations/0001/iteration.json",
            {"id": "0001", "status": "executed"},
        )
        result = dash.snapshot(self.root)
        self.assertEqual(result["frontier"]["points"], [])
        self.assertIsNone(result["nodes"][0]["value"])
        self.assertFalse(result["frontier"]["current_membership_complete"])
        blocked = definition()
        blocked["allow_feedback"] = False
        put(self.root, "study/study.json", {"global_evaluation": blocked})
        self.assertFalse(dash.frontier_snapshot(self.root)["available"])
        self.assertEqual(dash.frontier_snapshot(self.root)["points"], [])
        put(self.root, "study/study.json", {"global_evaluation": definition()})
        (self.root / "study/frontiers.json").unlink()
        self.assertFalse(dash.frontier_snapshot(self.root)["available"])

    def test_multiobjective_has_no_invented_scalar_best(self):
        evaluation = definition()
        evaluation["objectives"].append(
            {"name": "latency", "direction": "minimize", "unit": "ms"}
        )
        put(self.root, "study/study.json", {"global_evaluation": evaluation})
        frontier = dash.frontier_snapshot(self.root)
        self.assertFalse(frontier["available"])
        self.assertEqual(frontier["objectives"], evaluation["objectives"])
        self.assertIsNone(frontier["metric"])
        self.assertIn("多目标", frontier["reason"])

    def test_http_snapshot_real_handler_needs_no_protok_control_directory(self):
        published_study(self.root, definition(), [0.7, 0.8])
        with patch.object(dash, "ThreadingHTTPServer") as server:
            server.return_value.server_port = 0
            dash.serve(self.root, 0)
            handler = server.call_args.args[1]
        connection = Mock()
        connection.makefile.return_value = BytesIO(
            b"GET /api/snapshot HTTP/1.0\r\n\r\n"
        )
        response = bytearray()
        connection.sendall.side_effect = response.extend
        handler(connection, ("127.0.0.1", 1), Mock())
        header, body = bytes(response).split(b"\r\n\r\n", 1)
        self.assertIn(b"200 OK", header)
        result = json.loads(body)
        self.assertEqual(result["frontier"]["current_experiment_ids"], ["active:0002"])
        self.assertIsNone(result["backend_error"])

    def test_retained_protocol_in_same_study_does_not_mark_active_membership(self):
        published_study(self.root, definition("error", "minimize", "mm", "old-v1"), [5])
        published_study(self.root, definition(), [0.8], start=2)
        result = dash.view_snapshot(self.root)
        self.assertEqual(
            [view["evaluation_id"] for view in result["frontiers"]],
            ["generic-v1", "old-v1"],
        )
        old = result["nodes"][0]
        self.assertEqual((old["metric"], old["unit"], old["value"]), ("error", "mm", 5))
        self.assertFalse(old["frontier_current"])
        self.assertEqual(result["frontier"]["current_experiment_ids"], ["active:0002"])
        self.assertTrue(result["frontiers"][1]["points"][0]["current"])

    def test_protok_id_with_multiobjective_definition_does_not_reuse_receipt_scalar(
        self,
    ):
        evaluation = definition("error", "minimize", "mm", dash.PROTOCOL)
        published_study(self.root, evaluation, [5])
        put(
            self.root,
            "control/backend-snapshot.json",
            {
                "records": [],
                "used_gpu_seconds": 0,
                "scored_records": 0,
                "outstanding": [],
            },
        )
        evaluation["objectives"].append(
            {"name": "accuracy", "unit": "fraction", "direction": "maximize"}
        )
        put(self.root, "study/study.json", {"global_evaluation": evaluation})
        result = dash.snapshot(self.root, snapshot_only=True)
        self.assertFalse(result["frontier"]["available"])
        self.assertIsNone(result["nodes"][0]["value"])
        self.assertIsNone(result["nodes"][0]["provisional_value"])
