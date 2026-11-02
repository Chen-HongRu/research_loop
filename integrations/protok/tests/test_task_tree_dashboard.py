"""A task keeps its identity and evidence across execution and publication."""

import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

from integrations.protok import dashboard as dash


class TaskTreeTests(unittest.TestCase):
    run_id = "L0123456789abcdef"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "active"
        self.history = Path(self.temporary.name) / "history"
        for root in (self.root, self.history):
            self.put(
                root,
                "control/backend-snapshot.json",
                {
                    "records": [],
                    "outstanding": [],
                    "used_gpu_seconds": 0,
                    "scored_records": 0,
                },
            )
            self.put(root, "control/ownership.json", {"project": str(root / "project")})
            self.put(
                root,
                "study/iterations/0001/iteration.json",
                {
                    "id": "0001",
                    "status": "reviewed",
                    "route_id": "R1",
                    "route_title": "Shared research question",
                    "created_at": "2026-11-01T00:00:00Z",
                },
            )
            self.put(
                root, "study/iterations/0001/publication.json", {"state_after": {}}
            )
            self.put(
                root,
                "study/state.json",
                {"iteration": 1, "phase": "done", "status": "ready"},
            )

    def put(self, root, name, value):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def lane(self, root=None, run_id=None, *, started=True):
        root, run_id = root or self.root, run_id or self.run_id
        lane = root / "study/long-runs" / run_id
        self.put(
            lane,
            "request.json",
            {
                "id": run_id,
                "source_iteration": "0001",
                "source_workspace_ref": "frozen-source",
                "nomination": {"reason": "Verify the gain"},
            },
        )
        if started:
            self.put(
                lane,
                "study/state.json",
                {"iteration": 1, "status": "running", "phase": "execute"},
            )
            self.put(lane, "study/iterations/0001/iteration.json", {"id": "0001"})
        return lane

    def job(self, *, receipt=True):
        directory = self.root / "control/long-training-jobs" / self.run_id
        self.put(
            directory,
            "job.json",
            {
                "run_id": self.run_id,
                "source_identity": "source-v1",
                "allocated_seconds": 300,
                "owner": {"owner_token": "PRIVATE_OWNER"},
                "policy": {"authorization": "PRIVATE_AUTHORIZATION"},
                "request": {"source_workspace_ref": "frozen-source"},
            },
        )
        self.put(
            directory,
            "state.json",
            {"attempts": [{"status": "running", "owned_job_id": "123"}]},
        )
        if receipt:
            self.put(
                directory,
                "receipt.json",
                {
                    "run_id": self.run_id,
                    "source_identity": "source-v1",
                    "allocated_seconds": 300,
                    "status": "failed",
                    "gpu_seconds": 301,
                    "terminal_state": "TIMEOUT",
                    "owned_job_id": "123",
                    "curve": [
                        {
                            "step": 2000,
                            "ca_rmsd": 2.01,
                            "tm_score": 0.5,
                            "lddt": 0.6,
                            "checkpoint": "same-checkpoint",
                        }
                    ],
                },
            )
        return directory

    def test_concurrent_tasks_have_scoped_ids_and_keep_identity_after_review(self):
        lane = self.lane()
        self.lane(self.history)
        self.lane(run_id="L1111111111111111", started=False)
        self.put(self.root, "study/iterations/0002/iteration.json", {"id": "0002"})
        self.put(
            self.root,
            "study/state.json",
            {"iteration": 2, "phase": "plan", "status": "running"},
        )
        backend = dash.read(self.root / "control/backend-snapshot.json")
        with patch.object(dash.ProTokBackend, "inspect", return_value=backend):
            first = dash.view_snapshot(self.root, history_roots=[self.history])
            by_id = {task["id"]: task for task in first["tasks"]}
            identity = "active:long:" + self.run_id
            self.assertEqual(set(first["active_task_ids"]), {"active:0002", identity})
            self.assertEqual(by_id[identity]["validation_of"], "active:0001")
            self.assertEqual(by_id[identity]["route_id"], "active:R1")
            self.assertTrue(by_id["history-1:long:" + self.run_id]["archived"])
            self.assertEqual(
                by_id["active:long:L1111111111111111"]["task_status"], "queued"
            )
            self.assertNotIn(identity, {node["id"] for node in first["nodes"]})
            self.assertEqual(first["frontier"]["points"], [])
            self.put(
                lane,
                "result.json",
                {
                    "review": {"markdown": "Negative evidence retained"},
                    "result": {"status": "failed"},
                },
            )
            second = dash.view_snapshot(self.root, history_roots=[self.history])
            completed = next(task for task in second["tasks"] if task["id"] == identity)
            self.assertEqual(completed["task_status"], "completed")
            self.assertEqual(completed["outcome"], "failed")
            self.assertEqual(second["active_task_ids"], ["active:0002"])

    def test_selected_child_exposes_original_plan_and_safe_local_job_evidence(self):
        lane = self.lane()
        self.job()
        attempt = "iterations/0001/plan/attempt-001"
        output = {
            "markdown": "Frozen long Plan",
            "contract": {"action": "Train this candidate"},
        }
        self.put(lane, "study/" + attempt + "/response.json", output)
        self.put(
            lane,
            "study/" + attempt + "/process.json",
            {"run_id": "plan", "status": "succeeded", "started": 100, "ended": 110},
        )
        self.put(
            lane,
            "study/" + attempt + "/completed.json",
            {"version": 1, "run_id": "plan", "response": output, "completed_at": 110},
        )
        self.put(
            lane,
            "study/state.json",
            {
                "iteration": 1,
                "phase": "review",
                "status": "running",
                "plan_ref": attempt + "/response.json",
                "calls": {"plan": attempt},
                "accepted_calls": {"plan": True},
                "round_admission": {"authorization": "PRIVATE_ROUND"},
            },
        )
        self.put(
            lane,
            "study/iterations/0001/review/attempt-001/response.json",
            {"markdown": "Unfinished review"},
        )
        self.put(
            self.root,
            "study/iterations/0002/context/long_training_results.json",
            [{"id": self.run_id}],
        )
        detail = dash.long_task_details(self.root, self.run_id, "active", "当前")
        self.assertEqual(
            detail["long_training"][0]["feedback_iterations"], ["active:0002"]
        )
        planner = next(agent for agent in detail["agents"] if agent["phase"] == "plan")
        self.assertTrue(planner["accepted"])
        self.assertEqual(planner["output"], output)
        self.assertTrue(
            planner["source"].startswith("study/long-runs/" + self.run_id + "/study/")
        )
        reviewer = next(
            agent for agent in detail["agents"] if agent["phase"] == "review"
        )
        self.assertIsNone(reviewer["output"])
        self.assertEqual(detail["execution"]["job"]["gpu_seconds"], 301)
        self.assertEqual(
            detail["long_training"][0]["observations"]["curve"][0]["step"], 2000
        )
        self.assertNotIn("result", detail["long_training"][0])
        serialized = json.dumps(detail)
        for secret in ("PRIVATE_OWNER", "PRIVATE_AUTHORIZATION", "PRIVATE_ROUND"):
            self.assertNotIn(secret, serialized)

    def test_mismatched_receipt_is_attention_without_exposing_its_observations(self):
        self.lane()
        directory = self.job()
        receipt = dash.read(directory / "receipt.json")
        self.put(
            directory, "receipt.json", {**receipt, "source_identity": "another-source"}
        )
        run = dash.long_training_snapshot(self.root / "study")[0]
        task = dash.long_task(self.root, run)
        self.assertEqual(dash.task_status(task), "needs_attention")
        self.assertEqual(task["attempts"][0]["curve"], [])
        self.assertIn("不匹配", task["progress"]["reasons"][0]["detail"])

    def test_queued_task_enters_plan_without_changing_its_id(self):
        lane = self.lane(started=False)
        first = dash.long_task(
            self.root, dash.long_training_snapshot(self.root / "study")[0]
        )
        self.put(
            lane,
            "study/state.json",
            {"iteration": 1, "phase": "plan", "status": "running"},
        )
        second = dash.long_task(
            self.root, dash.long_training_snapshot(self.root / "study")[0]
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["status"], "queued")
        self.assertEqual(second["phase"], "plan")
        self.assertEqual(first["attempts"], [])

    def test_http_routes_only_read_configured_task_sources(self):
        self.lane()
        self.lane(self.history)
        with patch.object(dash, "ThreadingHTTPServer") as factory:
            factory.return_value.server_port = 0
            dash.serve(self.root, 0, snapshot_only=True, history_roots=[self.history])
            handler = factory.call_args.args[1]
        for source, identity, expected in (
            ("active", self.run_id, b"200 OK"),
            ("history-1", self.run_id, b"200 OK"),
            ("history-2", self.run_id, b"404 Not Found"),
            ("active", "L2222222222222222", b"404 Not Found"),
            ("active", "../../control/ownership.json", b"404 Not Found"),
        ):
            connection, response = Mock(), bytearray()
            connection.makefile.return_value = BytesIO(
                (f"GET /api/task/{source}/long/{identity} HTTP/1.0\r\n\r\n").encode()
            )
            connection.sendall.side_effect = response.extend
            handler(connection, ("127.0.0.1", 1), Mock())
            self.assertIn(expected, response)
