import unittest

import test_protok_dashboard as fixtures


class DebugDashboardTests(unittest.TestCase):
    setUp = fixtures.AgentOutputTests.setUp
    put = fixtures.AgentOutputTests.put
    attempt = fixtures.AgentOutputTests.attempt

    def test_current_submitted_job_is_waiting_normally_not_missing_accounting(self):
        identity = "f" * 64
        self.put(
            "control/jobs/0001/job.json",
            {"status": "submitted", "fingerprint": identity},
        )
        state = {"iteration": 1, "phase": "execute", "status": "running"}
        row = {
            "record": identity,
            "mode": "scored",
            "status": "reserved",
            "attempts": [
                {"owned_job_id": "789230", "status": "running"},
            ],
        }
        backend = {"outstanding": [row]}
        progress = fixtures.DASH.progress(
            self.root, state, backend, {"status": "running"}, []
        )
        self.assertEqual(progress["title"], "正式评估进行中（记录状态）")
        self.assertEqual(progress["reasons"][0]["title"], "本轮正式评估等待完成")
        self.assertIn("789230", progress["reasons"][0]["detail"])
        self.assertNotIn("缺少", progress["reasons"][0]["detail"])
        self.assertEqual(backend["outstanding"], [row])

    def test_ambiguous_unbound_or_paused_job_keeps_the_reconciliation_warning(self):
        identity = "f" * 64
        self.put(
            "control/jobs/0001/job.json",
            {"status": "submitted", "fingerprint": identity},
        )
        state = {"iteration": 1, "phase": "execute", "status": "running"}
        attempt = {"owned_job_id": "789230", "status": "running"}
        for changes, record_id, runner in (
            ({"terminal_state": "COMPLETED"}, identity, {"status": "running"}),
            ({"owned_job_id": None}, identity, {"status": "running"}),
            ({"status": "submitting"}, identity, {"status": "running"}),
            ({}, "a" * 64, {"status": "running"}),
            ({}, identity, {"status": "paused"}),
        ):
            with self.subTest(changes=changes, record_id=record_id, runner=runner):
                backend = {
                    "outstanding": [
                        {
                            "record": record_id,
                            "mode": "scored",
                            "status": "reserved",
                            "attempts": [attempt | changes],
                        }
                    ]
                }
                progress = fixtures.DASH.progress(self.root, state, backend, runner, [])
                self.assertIn(
                    "评估记录未结束", [r["title"] for r in progress["reasons"]]
                )
                self.assertNotEqual(progress["title"], "正式评估进行中（记录状态）")

    def test_budget_view_separates_debug_from_formal_and_shows_reserved_allowance(self):
        self.put(
            "study/state.json",
            {
                "iteration": 1,
                "phase": "execute",
                "status": "running",
                "debugging": "failure.json",
            },
        )
        self.put(
            "control/backend-snapshot.json",
            {
                "records": [],
                "used_gpu_seconds": 100,
                "debug_gpu_seconds": 300,
                "total_gpu_seconds": 400,
                "scored_records": 1,
                "outstanding": [],
            },
        )
        self.put(
            "control/debug/0001/debug-budget.json",
            {
                "used_gpu_seconds": 300,
                "limit_gpu_seconds": 1800,
                "active_attempt": "smoke-002",
                "attempts": {"smoke-002": {"allocated_gpu_seconds": 1200}},
            },
        )
        self.put("control/debug/0001/flow.json", {"phase": "smoke"})
        data = fixtures.DASH.snapshot(self.root, snapshot_only=True)
        self.assertEqual(
            (
                data["gpu_seconds"],
                data["formal_gpu_seconds"],
                data["debug_gpu_seconds"],
            ),
            (400, 100, 300),
        )
        self.assertEqual(data["debug"]["remaining_gpu_seconds"], 300)
        self.assertEqual(data["debug"]["reserved_gpu_seconds"], 1200)
        self.assertEqual(data["progress"]["title"], "候选调试中")

    def test_completed_continuation_repairs_and_execute_are_visible_without_promoting_score(
        self,
    ):
        repair = self.attempt(
            "execute/recovery/repairs/repair-001-try-001/attempt-001",
            {
                "outcome": "incomplete",
                "markdown": "Retained repair",
                "artifacts": [],
                "measurements": [],
            },
        )
        final = self.attempt(
            "execute/recovery",
            {
                "outcome": "completed",
                "markdown": "Fresh formal result",
                "artifacts": [],
                "measurements": [{"value": 2.0}],
            },
            started=200,
            ended=210,
        )
        self.attempt(
            "execute/attempt-001/agent",
            {"markdown": "PARTIAL HIDDEN"},
            status="interrupted",
            receipt=False,
        )
        self.put(
            "study/state.json",
            {
                "iteration": 1,
                "phase": "review",
                "status": "ready",
                "calls": {"debug:repair-001-try-001": repair, "execute": final},
                "accepted_calls": {"debug:repair-001-try-001": True, "execute": True},
            },
        )
        detail = fixtures.DASH.details(self.root, "0001")
        self.assertEqual(
            [a["role"] for a in detail["agents"]], ["Debug repair", "Executor"]
        )
        self.assertTrue(
            all(a["phase"] == "execute" and a["accepted"] for a in detail["agents"])
        )
        self.assertNotIn("PARTIAL HIDDEN", str(detail))
        node = fixtures.DASH.snapshot(self.root, snapshot_only=True)["nodes"][0]
        self.assertFalse(node["eligible"])
        self.assertIsNone(node["value"])

    def test_same_investigation_includes_all_debug_attempts_and_exact_fresh_formal_record(
        self,
    ):
        fingerprint = "f" * 64
        record_name = fingerprint + "--debug-0001-formal-001"
        directory = self.root / "control/debug/0001/formal-001"
        ledger = self.root / "ledger"
        self.put("control/jobs/0001/recovery.json", {"job_directory": str(directory)})
        self.put(
            "control/debug/0001/formal-001/job.json",
            {
                "status": "submitted",
                "fingerprint": fingerprint,
                "record_name": record_name,
            },
        )
        self.put(
            "control/debug/0001/formal-001/runtime-config.json",
            {"records_root": str(ledger)},
        )
        self.put(
            "ledger/" + record_name + "/state.json",
            {"attempts": [{"owned_job_id": "202", "status": "running", "seed": 42}]},
        )
        self.put(
            "control/backend-snapshot.json",
            {
                "records": [
                    {
                        "record": fingerprint,
                        "fingerprint": fingerprint,
                        "attempts": [{"owned_job_id": "101"}],
                    },
                    {
                        "record": record_name,
                        "fingerprint": fingerprint,
                        "attempts": [{"owned_job_id": "202"}],
                    },
                ],
                "used_gpu_seconds": 39,
                "scored_records": 1,
                "outstanding": [],
            },
        )
        self.put(
            "control/debug/0001/debug-budget.json",
            {
                "limit_gpu_seconds": 1800,
                "used_gpu_seconds": 169,
                "active_attempt": None,
                "attempts": {
                    "smoke-001": {
                        "allocated_gpu_seconds": 1800,
                        "job_id": "150",
                        "receipt": {"gpu_seconds": 32},
                    },
                    "smoke-002": {
                        "allocated_gpu_seconds": 1740,
                        "job_id": "151",
                        "receipt": {"gpu_seconds": 137},
                    },
                },
            },
        )
        self.put("control/debug/0001/flow.json", {"phase": "formal"})
        self.put(
            "control/debug/0001/failure.json",
            {"cause": "Missing active gradient", "job_id": "101", "gpu_seconds": 39},
        )
        for attempt_id, status in (("smoke-001", "failed"), ("smoke-002", "completed")):
            self.put(
                f"control/debug/0001/attempts/{attempt_id}/receipt.json",
                {
                    "status": status,
                    "verified": status == "completed",
                    "quality_score_eligible": False,
                },
            )
        detail = fixtures.DASH.details(self.root, "0001")["execution"]
        self.assertEqual(detail["formal_attempts"][0]["owned_job_id"], "202")
        self.assertEqual(
            [a["owned_job_id"] for a in detail["debug_attempts"]], ["150", "151"]
        )
        self.assertEqual(
            [a["gpu_seconds"] for a in detail["debug_attempts"]], [32, 137]
        )
        self.assertEqual(detail["debug"]["remaining_gpu_seconds"], 1631)
        self.assertEqual(detail["original_failure"]["owned_job_id"], "101")
        node = fixtures.DASH.snapshot(self.root, snapshot_only=True)["nodes"][0]
        self.assertEqual(node["attempts"][0]["owned_job_id"], "202")
        self.assertIsNone(node["value"])


if __name__ == "__main__":
    unittest.main()
