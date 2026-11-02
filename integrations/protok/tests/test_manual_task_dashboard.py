"""Operator requests stay visible without exposing authorization or duplicating work."""

import json
from io import BytesIO
from unittest.mock import Mock, patch

from integrations.protok import dashboard as dash
from integrations.protok.tests.test_task_tree_dashboard import TaskTreeTests


class ManualTaskDashboardTests(TaskTreeTests):
    def request(self, root=None, identity="manual-check", kind="investigation"):
        root = root or self.root
        request = {
            "version": 1,
            "id": identity,
            "kind": kind,
            "title": "Check proposed mechanism",
            "instruction": "Investigate the observed behavior",
            "route_id": "R1",
            "base_experiment": "0001",
            "priority": 10,
            "allow_changes": True,
            "submitted_at": "2026-11-01T00:00:00Z",
            "source": {
                "project": "/project",
                "workspace": "/frozen",
                "revision": "commit",
                "experiment": "0001",
                "token": "PRIVATE_SOURCE",
            },
            "budget": {
                "authorization": "PRIVATE_AUTHORIZATION",
                "gpu_seconds_per_run": 300,
                "total_gpu_seconds": 600,
                "deadline": 9999999999,
            },
            "owner": "PRIVATE_OWNER",
        }
        self.put(root, f"study/tasks/{identity}/request.json", request)
        return request

    def view(self):
        backend = dash.read(self.root / "control/backend-snapshot.json")
        with patch.object(dash.ProTokBackend, "inspect", return_value=backend):
            return dash.view_snapshot(self.root, history_roots=[self.history])

    def test_manual_investigation_keeps_identity_and_bound_graph_relations(self):
        request = self.request()
        first = self.view()
        identity = "active:manual:" + request["id"]
        queued = next(task for task in first["tasks"] if task["id"] == identity)
        self.assertEqual(queued["task_status"], "queued")
        self.assertEqual(queued["detail_url"], "/api/task/active/manual/manual-check")
        self.put(
            self.root,
            "study/tasks/manual-check/binding.json",
            {
                "study": str(self.root / "study"),
                "iteration": "0001",
            },
        )
        self.put(
            self.root,
            "study/iterations/0001/publication.json",
            {
                "metadata": dash.read(
                    self.root / "study/iterations/0001/iteration.json"
                ),
                "state_after": {},
            },
        )
        second = self.view()
        bound = next(task for task in second["tasks"] if task["id"] == identity)
        self.assertEqual(bound["experiment_id"], "active:0001")
        self.assertEqual(bound["detail_url"], "/api/node/active/0001")
        self.assertEqual(bound["task_status"], "completed")
        self.assertNotIn("active:0001", {task["id"] for task in second["tasks"]})
        self.assertIn("active:0001", {node["id"] for node in second["nodes"]})
        self.assertEqual(second["active_node_id"], identity)
        detail = dash.source_details(self.root, "active", "0001")
        self.assertEqual(detail["manual_request"]["id"], request["id"])
        for payload in (first, second, detail):
            for secret in ("PRIVATE_AUTHORIZATION", "PRIVATE_OWNER", "PRIVATE_SOURCE"):
                self.assertNotIn(secret, json.dumps(payload))

    def test_started_then_reviewed_manual_long_uses_manual_receipts(self):
        self.request(kind="long_training")
        lane = self.root / "study/tasks/manual-check"
        self.put(
            lane, "binding.json", {"study": str(lane / "study"), "iteration": "0001"}
        )
        self.put(
            lane,
            "study/state.json",
            {"iteration": 1, "status": "running", "phase": "execute"},
        )
        self.put(lane, "study/iterations/0001/iteration.json", {"id": "0001"})
        self.put(
            lane,
            "study/iterations/0001/context/manual-long-request.json",
            {
                "id": self.run_id,
                "manual_task_id": "manual-check",
                "recipe": {"total_steps": 10000},
                "evaluation": {"id": "full4491"},
                "authorization": "PRIVATE_WIRE",
            },
        )
        directory = self.job()
        manual_dir = self.root / "control/manual-training-jobs" / self.run_id
        manual_dir.parent.mkdir(parents=True)
        directory.rename(manual_dir)
        for name in ("job.json", "receipt.json"):
            value = dash.read(manual_dir / name)
            value.update(manual_task_id="manual-check", budget_pool="manual")
            if name == "job.json":
                value["request"]["manual_task_id"] = "manual-check"
            self.put(manual_dir, name, value)
        task = next(task for task in self.view()["tasks"] if task.get("manual_task_id"))
        self.assertEqual(task["task_origin"], "user")
        self.assertEqual(task["budget_pool"], "manual")
        self.assertEqual(task["attempts"][0]["gpu_seconds"], 301)
        detail = dash.manual_task_details(self.root, "manual-check", "active", "当前")
        self.assertEqual(
            detail["long_training"][0]["observations"]["curve"][0]["step"], 2000
        )
        self.assertNotIn("PRIVATE_WIRE", json.dumps(detail))
        row = dash.manual_rows(self.root)[0]
        row.update(
            status="reviewed",
            phase="done",
            result={
                "request": row["request"],
                "review": {"markdown": "Accepted failure"},
                "execute": {"artifacts": []},
            },
        )
        with patch.object(dash, "manual_rows", return_value=[row]):
            reviewed = dash.manual_task(self.root, row)
            detail = dash.manual_task_details(
                self.root, "manual-check", "active", "当前"
            )
        self.assertEqual(reviewed["id"], task["id"])
        self.assertEqual(dash.task_status(reviewed), "completed")
        self.assertEqual(
            detail["long_training"][0]["result"]["review"]["markdown"],
            "Accepted failure",
        )
        self.assertNotIn("PRIVATE_AUTHORIZATION", json.dumps(detail))

    def test_manual_http_only_configured_sources_and_safe_ids(self):
        self.request()
        self.request(self.history)
        with patch.object(dash, "ThreadingHTTPServer") as factory:
            factory.return_value.server_port = 0
            dash.serve(self.root, 0, snapshot_only=True, history_roots=[self.history])
            handler = factory.call_args.args[1]
        for source, identity, expected in (
            ("active", "manual-check", b"200 OK"),
            ("history-1", "manual-check", b"200 OK"),
            ("history-2", "manual-check", b"404 Not Found"),
            ("active", "absent", b"404 Not Found"),
            ("active", "../../control/ownership.json", b"404 Not Found"),
        ):
            connection, response = Mock(), bytearray()
            connection.makefile.return_value = BytesIO(
                (f"GET /api/task/{source}/manual/{identity} HTTP/1.0\r\n\r\n").encode()
            )
            connection.sendall.side_effect = response.extend
            handler(connection, ("127.0.0.1", 1), Mock())
            self.assertIn(expected, response)
            self.assertNotIn(b"PRIVATE_AUTHORIZATION", response)

    def test_cancelled_and_expired_status_and_separate_accounting(self):
        self.request()
        self.put(self.root, "study/tasks/manual-check/cancelled.json", {})
        self.assertEqual(
            next(task for task in self.view()["tasks"] if task.get("manual_task_id"))[
                "task_status"
            ],
            "cancelled",
        )
        request = self.request(identity="expired-long", kind="long_training")
        request["budget"]["deadline"] = 1
        self.put(self.root, "study/tasks/expired-long/request.json", request)
        self.assertEqual(
            next(
                task
                for task in self.view()["tasks"]
                if task.get("manual_task_id") == "expired-long"
            )["task_status"],
            "expired",
        )
        backend = dash.read(self.root / "control/backend-snapshot.json")
        backend.update(
            manual_training_gpu_seconds=301,
            long_training_gpu_seconds=200,
            total_gpu_seconds=501,
        )
        with patch.object(dash.ProTokBackend, "inspect", return_value=backend):
            result = dash.snapshot(self.root)
        self.assertEqual(result["manual_training_gpu_seconds"], 301)
        self.assertEqual(result["long_training_gpu_seconds"], 200)
        self.assertEqual(result["gpu_seconds"], 501)
