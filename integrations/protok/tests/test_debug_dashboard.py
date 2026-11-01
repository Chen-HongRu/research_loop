import unittest

import test_protok_dashboard as fixtures


class DebugDashboardTests(unittest.TestCase):
    setUp = fixtures.AgentOutputTests.setUp
    put = fixtures.AgentOutputTests.put

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


if __name__ == "__main__":
    unittest.main()
