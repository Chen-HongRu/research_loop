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
            self.assertEqual(result["study_name"], root.name)
            self.assertEqual(result["study_root"], str(root))
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
                        "status": "reviewed" if identity == "0001" else "planned",
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
            for identity in ("0001", "0002"):
                put(
                    f"study/iterations/{identity}/review/attempt-001/process.json",
                    {"status": "succeeded"},
                )
            put(
                "study/iterations/0001/publication.json",
                {
                    "state_after": {
                        "review_ref": "iterations/0001/review/attempt-001/response.json"
                    }
                },
            )
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


class AgentOutputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.prefix = "study/iterations/0001"
        self.put(self.prefix + "/iteration.json", {"id": "0001", "status": "planned"})
        self.put(
            "study/state.json", {"iteration": 1, "phase": "plan", "status": "ready"}
        )
        self.put(
            "control/backend-snapshot.json",
            {
                "records": [],
                "used_gpu_seconds": 0,
                "scored_records": 0,
                "outstanding": [],
            },
        )

    def put(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def attempt(
        self,
        suffix,
        output,
        *,
        status="succeeded",
        started=100,
        ended=110,
        receipt=True,
        error=None,
    ):
        directory = self.prefix + "/" + suffix
        self.put(directory + "/response.json", output)
        self.put(
            directory + "/process.json",
            {
                "run_id": suffix,
                "status": status,
                "started": started,
                "ended": ended,
                "error": error,
            },
        )
        if receipt:
            self.put(
                directory + "/completed.json",
                {
                    "version": 1,
                    "run_id": suffix,
                    "response": output,
                    "completed_at": ended,
                },
            )
        return "iterations/0001/" + suffix

    def test_partial_response_is_hidden_and_retries_are_retained_in_order(self):
        self.attempt(
            "plan/attempt-001",
            {"markdown": "PARTIAL MUST NOT APPEAR"},
            status="timed_out",
            receipt=False,
            error="timeout",
            started=100,
        )
        accepted = self.attempt(
            "plan/attempt-002",
            {
                "decision": "reselect",
                "markdown": "Premise cannot be restored",
                "stop_reason": "missing input",
            },
            started=120,
            ended=130,
        )
        self.put(
            "study/state.json",
            {
                "iteration": 1,
                "phase": "selection",
                "status": "ready",
                "calls": {"plan-0": accepted},
                "accepted_calls": {"plan-0": True},
            },
        )
        result = DASH.details(self.root, "0001")
        first, second = result["agents"]
        self.assertIsNone(first["output"])
        self.assertEqual(first["status"], "timed_out")
        self.assertEqual(first["error"], "timeout")
        self.assertFalse(first["accepted"])
        self.assertEqual(second["output"]["decision"], "reselect")
        self.assertTrue(second["accepted"])
        self.assertFalse(second["published"])
        self.assertEqual(second["round"], 1)
        self.assertEqual(second["duration_seconds"], 10)
        self.assertNotIn("PARTIAL MUST NOT APPEAR", json.dumps(result))

    def test_completed_unaccepted_output_is_visible_but_not_published(self):
        self.attempt("review/attempt-001", {"markdown": "Needs controller validation"})
        agent = DASH.details(self.root, "0001")["agents"][0]
        self.assertEqual(agent["status"], "completed")
        self.assertFalse(agent["accepted"])
        self.assertFalse(agent["published"])
        self.assertEqual(agent["output"]["markdown"], "Needs controller validation")

    def test_exact_publication_references_ignore_later_review_attempt(self):
        plan = self.attempt(
            "plan/attempt-001",
            {
                "decision": "investigate",
                "markdown": "Plan",
                "contract": {"action": "Bounded action"},
            },
        )
        review = self.attempt(
            "review/attempt-001",
            {
                "markdown": "## Verified result",
                "goal_status": "not_met",
                "decision": "continue",
                "candidate_reviews": [
                    {"candidate_id": "candidate", "valid": True, "verified": True}
                ],
            },
            started=200,
            ended=220,
        )
        self.attempt(
            "review/attempt-002",
            {
                "markdown": "Later unaccepted output",
                "goal_status": "met",
                "decision": "stop",
            },
            started=240,
            ended=250,
        )
        self.put(self.prefix + "/iteration.json", {"id": "0001", "status": "reviewed"})
        self.put(
            self.prefix + "/publication.json",
            {
                "state_after": {
                    "plan_ref": plan + "/response.json",
                    "review_ref": review + "/response.json",
                }
            },
        )
        self.put(
            "study/state.json", {"iteration": 2, "phase": "done", "status": "ready"}
        )
        self.put(
            "control/jobs/0001/result.json", {"fingerprint": "candidate", "value": 2.0}
        )
        node = DASH.snapshot(self.root, snapshot_only=True)["nodes"][0]
        self.assertEqual(node["action"], "Bounded action")
        self.assertEqual(node["summary"], "Verified result")
        self.assertEqual(node["goal_status"], "not_met")
        self.assertEqual(node["review_decision"], "continue")
        self.assertEqual(node["value"], 2.0)
        agents = DASH.details(self.root, "0001")["agents"]
        self.assertEqual([a["published"] for a in agents], [True, True, False])

    def test_prepared_publication_cannot_make_candidate_eligible(self):
        review = self.attempt(
            "review/attempt-001",
            {
                "candidate_reviews": [
                    {"candidate_id": "candidate", "valid": True, "verified": True}
                ]
            },
        )
        self.put(
            self.prefix + "/publication.json",
            {"state_after": {"review_ref": review + "/response.json"}},
        )
        self.put(
            "control/jobs/0001/result.json", {"fingerprint": "candidate", "value": 2.0}
        )
        result = DASH.snapshot(self.root, snapshot_only=True)
        self.assertFalse(result["nodes"][0]["published"])
        self.assertFalse(result["nodes"][0]["eligible"])
        self.assertIsNone(result["nodes"][0]["value"])
        self.assertFalse(DASH.details(self.root, "0001")["agents"][0]["published"])

    def test_progress_explains_actual_pause_and_error_without_execute_output(self):
        path = self.attempt(
            "execute/attempt-001/agent",
            {"markdown": "EXECUTION SECRET"},
            receipt=False,
            status="failed",
            error="evaluator disconnected",
        )
        execute = path.rsplit("/", 1)[0]
        self.put(
            "study/state.json",
            {
                "iteration": 1,
                "phase": "execute",
                "status": "needs_attention",
                "error": "HarnessError: evaluator disconnected",
                "active_attempt": execute,
                "calls": {"execute": execute},
                "goal_status": "undetermined",
            },
        )
        stop = self.root / "control/STOP"
        stop.write_text("Reconcile the outstanding allocation")
        self.put(
            "control/status.json", {"status": "paused", "error": "retained effects"}
        )
        self.put("control/jobs/0001/job.json", {"status": "submitted"})
        self.put(
            self.prefix + "/selection.json",
            {"rounds": {"0": {"candidates": ["new", "new"]}}},
        )
        (self.root / self.prefix / "execute.md").write_text("EXECUTION TRANSCRIPT")
        details = DASH.details(self.root, "0001")
        self.assertEqual(details["agents"], [])
        self.assertIsInstance(details["selection"], dict)
        self.assertNotIn("execute", details)
        self.assertNotIn("EXECUTION", json.dumps(details))
        snapshot = DASH.snapshot(self.root, snapshot_only=True)
        self.assertEqual(snapshot["state"]["goal_status"], "undetermined")
        progress = snapshot["progress"]
        self.assertEqual(progress["active_role"], "Executor")
        self.assertEqual(progress["title"], "需要处理")
        self.assertEqual(progress["started_at"], 100)
        self.assertEqual(progress["elapsed_seconds"], 10)
        self.assertIn("study/state.json", [r["source"] for r in progress["reasons"]])
        self.assertIn("control/STOP", [r["source"] for r in progress["reasons"]])
        self.assertEqual(progress["stages"][2]["status"], "needs_attention")

    def test_selection_roles_and_rounds_and_judged_action(self):
        a = self.attempt(
            "selection/round-002/a-proposal/attempt-001",
            {
                "markdown": "Proposal",
                "action": "Original",
                "research_design": {"kind": "diagnostic"},
            },
            started=100,
        )
        critique = self.attempt(
            "selection/round-002/1-b-critique/attempt-001",
            {"markdown": "Critique"},
            started=120,
        )
        judge = self.attempt(
            "selection/round-002/judge/attempt-001",
            {"markdown": "Judge", "action": "Revised action"},
            started=140,
        )
        self.put(
            "study/state.json",
            {
                "iteration": 1,
                "phase": "plan",
                "status": "ready",
                "selection_round": 1,
                "calls": {"s1-a": a, "s1-0-b-critique": critique, "s1-judge": judge},
                "accepted_calls": {
                    "s1-a": True,
                    "s1-0-b-critique": True,
                    "s1-judge": True,
                },
                "selected": {
                    "proposal": {"research_design": {"kind": "diagnostic"}},
                    "judge": {"action": "Revised action"},
                },
            },
        )
        agents = DASH.details(self.root, "0001")["agents"]
        self.assertEqual(
            [a["role"] for a in agents], ["Advocate A", "Advocate B", "Judge"]
        )
        self.assertEqual([a["round"] for a in agents], [2, 2, 2])
        self.assertEqual(agents[1]["turn"], 1)
        node = DASH.snapshot(self.root, snapshot_only=True)["nodes"][0]
        self.assertIsNone(node["action"])
        self.assertEqual(node["action_summary"], "Revised action")
        self.assertEqual(node["action_summary_label"], "选定行动（Judge）")
        self.assertEqual(node["inquiry_kind"], "diagnostic")

    def test_action_summary_prefers_proposal_and_keeps_exact_contract(self):
        plan = self.attempt(
            "plan/attempt-001",
            {
                "markdown": "Plan report",
                "contract": {
                    "action": "Detailed execution contract with fixed protocol"
                },
            },
        )
        self.put(
            "study/state.json",
            {
                "iteration": 1,
                "phase": "execute",
                "status": "ready",
                "plan_ref": plan + "/response.json",
                "selected": {
                    "proposal": {"action": "Test a different architecture"},
                    "judge": {"action": "Judge action"},
                },
            },
        )
        node = DASH.snapshot(self.root, snapshot_only=True)["nodes"][0]
        self.assertEqual(
            node["action"], "Detailed execution contract with fixed protocol"
        )
        self.assertEqual(node["action_summary"], "Test a different architecture")
        self.assertEqual(node["action_summary_label"], "研究意图（所选提案）")

    def test_progress_deduplicates_errors_and_does_not_infer_skipped_calls(self):
        error = "HarnessError: failed retained invocation"
        self.put(
            "study/state.json",
            {
                "iteration": 1,
                "phase": "execute",
                "status": "needs_attention",
                "error": error,
            },
        )
        self.put("control/status.json", {"error": error})
        self.put("control/runner.json", {"status": "paused", "error": error})
        self.put("control/continuation.json", {"deadline": 300})
        self.put("control/ownership.json", {"expires_at": 250})
        result = DASH.snapshot(self.root, snapshot_only=True)
        progress = result["progress"]
        self.assertEqual(len(progress["reasons"]), 1)
        self.assertEqual(progress["reasons"][0]["detail"], error)
        self.assertEqual(
            progress["reasons"][0]["source"],
            "study/state.json · control/status.json · control/runner.json",
        )
        self.assertIn("执行实验", progress["message"])
        self.assertNotIn("execute", progress["message"])
        self.assertEqual(progress["stages"][0]["status"], "pending")
        self.assertEqual(progress["stages"][1]["status"], "pending")
        self.assertEqual(result["deadline"], 300)
        self.assertEqual(result["effective_deadline"], 250)
        self.put("control/runner.json", {"status": "paused"})
        progress = DASH.snapshot(self.root, snapshot_only=True)["progress"]
        self.assertEqual(progress["reasons"][-1]["source"], "control/runner.json")

    def test_action_category_uses_the_retained_proposal_prose_as_summary(self):
        self.put(
            "study/state.json",
            {
                "iteration": 1,
                "selected": {
                    "proposal": {
                        "action": "evaluate",
                        "markdown": "Test source-centered messages.\n\nDetails follow.",
                    }
                },
            },
        )
        node = DASH.snapshot(self.root, snapshot_only=True)["nodes"][0]
        self.assertEqual(node["action_summary"], "Test source-centered messages.")
        self.assertIsNone(node["action"])

    def test_scout_exports_only_findings_and_observed_status(self):
        self.put("study/study.json", {"exploration": {"version": 1}})
        self.put(
            "control/scout/status.json",
            {
                "status": "failed",
                "error": "search unavailable",
                "started_at": 50,
                "finished_at": 70,
                "owner_token": "PRIVATE",
            },
        )
        self.put(
            "control/scout/latest.json",
            {
                "evidence_status": "unreviewed_external_advice",
                "finished_at": 30,
                "owner_token": "PRIVATE",
                "result": {
                    "summary": "Earlier published advice",
                    "candidates": [{"id": "idea-1"}],
                    "sources": [],
                    "search_status": "partial",
                },
            },
        )
        scout = DASH.snapshot(self.root, snapshot_only=True)["scout"]
        self.assertEqual(scout["status"], "failed")
        self.assertEqual(scout["summary"], "Earlier published advice")
        self.assertEqual(scout["findings_finished_at"], 30)
        self.assertEqual(scout["finished_at"], 70)
        self.assertNotIn("PRIVATE", json.dumps(scout))


class FrontierTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.definition = {
            "id": DASH.PROTOCOL,
            "comparison_scope": DASH.PROTOCOL,
            "protocol": "fixed protocol",
            "data_role": "development",
            "allow_feedback": True,
            "objectives": [
                {"name": DASH.METRIC, "direction": "minimize", "unit": "Angstrom"}
            ],
            "constraints": [],
        }
        self.put("study/study.json", {"global_evaluation": self.definition})
        self.put(
            "control/backend-snapshot.json",
            {
                "records": [],
                "used_gpu_seconds": 0,
                "scored_records": 0,
                "outstanding": [],
            },
        )
        self.candidates = []
        self.previous = {"global": {}}

    def put(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def views(self, candidates):
        from research_loop.evaluation import build_frontier

        view = build_frontier(self.definition, candidates)
        view["allow_feedback"] = True
        return {"global": {DASH.PROTOCOL: view}, "local": {}}

    def add(self, number, value, *, published=True, imported=False, **overrides):
        identity = f"{number:04d}"
        candidate = {
            "id": identity + ":candidate",
            "experiment_id": identity,
            "artifact": {"path": "/unread/artifact", "version": identity},
            "evaluation_id": DASH.PROTOCOL,
            "comparison_scope": DASH.PROTOCOL,
            "metrics": {DASH.METRIC: value},
            "evidence": ["/unread/evidence"],
            "verified": True,
            "valid": True,
            **overrides,
        }
        prefix = f"study/iterations/{identity}"
        self.put(
            prefix + "/iteration.json",
            {"id": identity, "status": "reviewed" if published else "executed"},
        )
        self.put(
            prefix + "/candidates.json",
            {
                "global_evaluation_id": DASH.PROTOCOL,
                "candidates": [candidate],
                "corrections": [],
            },
        )
        after = self.views(self.candidates + [candidate])
        self.put(
            prefix + "/publication.json",
            {
                "before": {"frontiers.json": json.dumps(self.previous)},
                "writes": {"frontiers.json": json.dumps(after)},
            },
        )
        if imported:
            self.put(
                f"control/jobs/{identity}/result.json",
                {"provenance": "historical-import-no-allocation"},
            )
        if published:
            self.candidates.append(candidate)
            self.previous = after
        self.put("study/frontiers.json", self.previous)
        return candidate

    def test_ties_tiny_strict_improvement_and_negative_result_use_exact_values(self):
        self.add(1, 3.0, imported=True)
        self.add(2, 3.0)
        tiny = 3.0 - 1e-10
        self.add(3, tiny)
        self.add(4, 4.0)
        result = DASH.snapshot(self.root, snapshot_only=True)
        frontier = result["frontier"]
        points = frontier["points"]
        self.assertTrue(frontier["available"])
        self.assertEqual(frontier["unit"], "Å")
        self.assertEqual([p["reached"] for p in points], [True, True, True, False])
        self.assertEqual([p["improved"] for p in points], [False, False, True, False])
        self.assertEqual([p["current"] for p in points], [False, False, True, False])
        self.assertTrue(points[0]["imported"])
        self.assertIsNone(points[0]["previous_best"])
        self.assertEqual(points[1]["delta"], 0)
        self.assertEqual(points[2]["value"], tiny)
        self.assertEqual(points[2]["previous_best"], 3.0)
        self.assertEqual(points[2]["delta"], 3.0 - tiny)
        self.assertGreater(points[2]["delta"], 0)
        self.assertEqual(frontier["current_candidate_ids"], ["0003:candidate"])
        self.assertEqual(frontier["current_experiment_ids"], ["0003"])
        self.assertEqual(
            [n["frontier_reached"] for n in result["nodes"]], [True, True, True, False]
        )
        self.assertEqual(
            [n["frontier_current"] for n in result["nodes"]],
            [False, False, True, False],
        )
        # Equal minima remain distinct current canonical members.
        self.add(5, tiny)
        tied = DASH.frontier_snapshot(self.root)
        self.assertEqual(
            tied["current_candidate_ids"], ["0003:candidate", "0005:candidate"]
        )
        self.assertFalse(tied["points"][-1]["improved"])
        self.assertTrue(tied["points"][-1]["reached"])

    def test_unpublished_unverified_and_incompatible_candidates_are_omitted(self):
        self.add(1, 3.0)
        self.add(2, 1.0, published=False)
        self.add(3, 1.0, verified=False)
        self.add(4, 1.0, comparison_scope="another-scope")
        self.add(5, 1.0, evaluation_id="another-evaluation")
        frontier = DASH.frontier_snapshot(self.root)
        self.assertEqual([p["id"] for p in frontier["points"]], ["0001"])
        self.assertEqual(frontier["current_candidate_ids"], ["0001:candidate"])
        self.assertTrue(frontier["points"][0]["current"])

    def test_current_correction_removes_old_frontier_from_points_and_marks_node(self):
        first = self.add(1, 2.0)
        self.add(2, 3.0)
        self.add(3, 4.0)
        self.put(
            "study/iterations/0003/candidates.json",
            {
                "global_evaluation_id": DASH.PROTOCOL,
                "candidates": [self.candidates[2]],
                "corrections": [
                    {
                        "candidate_id": first["id"],
                        "valid": False,
                        "verified": True,
                        "reason": "New evidence invalidates original eligibility",
                    }
                ],
            },
        )
        first["valid"] = False
        corrected = self.views(self.candidates)
        self.put("study/frontiers.json", corrected)
        publication = DASH.read(self.root / "study/iterations/0003/publication.json")
        publication["writes"]["frontiers.json"] = json.dumps(corrected)
        self.put("study/iterations/0003/publication.json", publication)
        result = DASH.snapshot(self.root, snapshot_only=True)
        self.assertEqual(
            [p["id"] for p in result["frontier"]["points"]], ["0002", "0003"]
        )
        self.assertEqual(
            result["frontier"]["current_candidate_ids"], ["0002:candidate"]
        )
        # A resurrected current member did not historically reach Frontier at its publication.
        self.assertTrue(result["frontier"]["points"][0]["current"])
        self.assertFalse(result["frontier"]["points"][0]["reached"])
        self.assertEqual(result["nodes"][0]["frontier_exclusion_reason"], "invalid")
        self.assertIsNone(result["nodes"][0]["frontier_reached"])

    def test_prepared_candidate_in_canonical_view_is_not_exposed_as_published_member(
        self,
    ):
        self.add(1, 3.0)
        unpublished = self.add(2, 2.0, published=False)
        self.put("study/frontiers.json", self.views(self.candidates + [unpublished]))
        result = DASH.snapshot(self.root, snapshot_only=True)
        frontier = result["frontier"]
        self.assertEqual(frontier["canonical_candidate_ids"], ["0002:candidate"])
        self.assertEqual(frontier["current_candidate_ids"], [])
        self.assertEqual(frontier["current_experiment_ids"], [])
        self.assertFalse(frontier["current_membership_complete"])
        self.assertEqual([point["id"] for point in frontier["points"]], ["0001"])
        self.assertFalse(result["nodes"][1]["frontier_current"])

    def test_canonical_exclusion_is_authoritative_even_if_original_claims_valid(self):
        candidate = self.add(1, 2.0)
        canonical = self.previous
        view = canonical["global"][DASH.PROTOCOL]
        view.update(
            candidate_ids=[],
            candidates=[],
            excluded=[{"id": candidate["id"], "reason": "unverified"}],
        )
        self.put("study/frontiers.json", canonical)
        frontier = DASH.frontier_snapshot(self.root)
        self.assertTrue(frontier["available"])
        self.assertEqual(frontier["points"], [])
        self.assertEqual(frontier["current_candidate_ids"], [])

    def test_missing_history_is_unknown_and_missing_canonical_has_no_best_fallback(
        self,
    ):
        self.add(1, 2.0)
        self.put("study/iterations/0001/publication.json", {})
        point = DASH.frontier_snapshot(self.root)["points"][0]
        self.assertIsNone(point["reached"])
        self.assertIsNone(point["improved"])
        self.assertIsNone(point["previous_best"])
        self.assertTrue(point["current"])
        (self.root / "study/frontiers.json").unlink()
        self.put(
            "control/active-best.json",
            {"experiment_id": "0001", "metrics": {DASH.METRIC: 2.0}},
        )
        result = DASH.snapshot(self.root, snapshot_only=True)
        self.assertFalse(result["frontier"]["available"])
        self.assertEqual(result["frontier"]["points"], [])
        self.assertEqual(result["frontier"]["current_candidate_ids"], [])
        self.assertIsNone(result["nodes"][0]["frontier_current"])


class HistoryViewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.active = self.directory / "active-study"
        self.archive = self.directory / "archive-study"
        self.records = self.directory / "records"
        self.first = "a" * 64
        self.shared = "b" * 64
        self.definition = {
            "id": DASH.PROTOCOL,
            "comparison_scope": DASH.PROTOCOL,
            "protocol": "fixed",
            "data_role": "development",
            "allow_feedback": True,
            "objectives": [
                {"name": DASH.METRIC, "direction": "minimize", "unit": "Angstrom"}
            ],
            "constraints": [],
        }

    def put(self, root, name, data):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def study(self, root, records, *, start, scope=None):
        from research_loop.evaluation import build_frontier

        definition = {**self.definition, "comparison_scope": scope or DASH.PROTOCOL}
        self.put(root, "study/study.json", {"global_evaluation": definition})
        self.put(
            root,
            "control/backend-snapshot.json",
            {
                "records": [],
                "used_gpu_seconds": 13,
                "scored_records": len(records),
                "outstanding": [],
            },
        )
        self.put(
            root,
            "study/state.json",
            {"iteration": len(records), "phase": "done", "status": "ready"},
        )
        self.put(
            root,
            "study/routes/index.json",
            {"R0001": {"title": "A route", "status": "active"}},
        )
        before, candidates = {"global": {}}, []
        for number, (fingerprint, value) in enumerate(records, 1):
            local_id = f"{number:04d}"
            candidate = {
                "id": local_id + ":" + fingerprint,
                "experiment_id": local_id,
                "artifact": {"path": "/never/read", "version": fingerprint},
                "evaluation_id": DASH.PROTOCOL,
                "comparison_scope": definition["comparison_scope"],
                "metrics": {DASH.METRIC: value},
                "evidence": ["/never/read/evidence"],
                "verified": True,
                "valid": True,
            }
            candidates.append(candidate)
            view = build_frontier(definition, candidates)
            view["allow_feedback"] = True
            after = {"global": {DASH.PROTOCOL: view}}
            prefix = "study/iterations/" + local_id
            self.put(
                root,
                prefix + "/iteration.json",
                {
                    "id": local_id,
                    "status": "reviewed",
                    "created_at": start + number - 1,
                    "route_id": "R0001",
                    "route_title": "Route",
                    "base_experiment": f"{number - 1:04d}" if number > 1 else None,
                    "references": ["0001"] if number > 1 else [],
                },
            )
            self.put(
                root,
                prefix + "/candidates.json",
                {
                    "global_evaluation_id": DASH.PROTOCOL,
                    "candidates": [candidate],
                    "corrections": [],
                },
            )
            self.put(
                root,
                prefix + "/publication.json",
                {
                    "before": {"frontiers.json": json.dumps(before)},
                    "writes": {"frontiers.json": json.dumps(after)},
                },
            )
            before = after
        self.put(root, "study/frontiers.json", before)

    def ledger(
        self, fingerprint, created, *, value=None, status="complete", protocol=None
    ):
        prefix = fingerprint + "/"
        self.put(
            self.records,
            prefix + "state.json",
            {
                "fingerprint": fingerprint,
                "created_at": created,
                "status": status,
                "mode": "scored",
                "attempts": [],
                "controller_lease": {"owner_token": "PRIVATE"},
            },
        )
        self.put(
            self.records,
            prefix + "run-config.json",
            {"protocol": protocol or DASH.PROTOCOL, "metric": DASH.METRIC},
        )
        if value is not None:
            self.put(
                self.records,
                prefix + "summary.json",
                {
                    "fingerprint": fingerprint,
                    "protocol": protocol or DASH.PROTOCOL,
                    "objective": DASH.METRIC,
                    "quality_score_eligible": True,
                    "value": value,
                },
            )

    def test_colliding_ids_jobless_archive_reuse_and_only_active_current_membership(
        self,
    ):
        self.study(self.archive, [(self.first, 3.0), (self.shared, 2.0)], start=100)
        self.study(self.active, [(self.shared, 2.0), ("c" * 64, 4.0)], start=200)
        self.put(
            self.active,
            "control/jobs/0001/result.json",
            {
                "provenance": "historical-import-no-allocation",
                "fingerprint": self.shared,
                "value": 2.0,
            },
        )
        result = DASH.view_snapshot(
            self.active, snapshot_only=True, history_roots=[self.archive]
        )
        nodes = {node["id"]: node for node in result["nodes"]}
        self.assertEqual(
            list(nodes),
            ["history-1:0001", "history-1:0002", "active:0001", "active:0002"],
        )
        self.assertEqual(nodes["history-1:0002"]["value"], 2.0)
        self.assertEqual(nodes["history-1:0002"]["fingerprint"], self.shared)
        self.assertEqual(nodes["active:0001"]["duplicate_of"], "history-1:0002")
        self.assertEqual(nodes["active:0001"]["imported_from"], "history-1:0002")
        self.assertIsNone(nodes["active:0001"]["base"])
        self.assertEqual(nodes["active:0002"]["base"], "active:0001")
        self.assertEqual(nodes["active:0002"]["declared_base"], "0001")
        self.assertEqual(nodes["active:0002"]["references"], ["active:0001"])
        self.assertEqual(nodes["history-1:0002"]["route_id"], "history-1:R0001")
        self.assertIn("active:R0001", result["routes"])
        self.assertFalse(nodes["history-1:0002"]["frontier_current"])
        self.assertTrue(nodes["history-1:0002"]["frontier_local_current"])
        self.assertEqual(result["frontier"]["current_experiment_ids"], ["active:0001"])
        points = result["frontier"]["points"]
        self.assertEqual([p["sequence"] for p in points], [1, 2, 3, 4])
        self.assertEqual(points[2]["candidate_id"], "active:0001:" + self.shared)
        self.assertEqual(points[2]["original_candidate_id"], "0001:" + self.shared)
        self.assertEqual(points[2]["duplicate_of"], "history-1:0002")
        self.assertEqual(result["coverage"]["unique_reviewed_result_count"], 3)
        self.assertEqual(result["coverage"]["duplicate_reviewed_result_count"], 1)
        self.assertEqual(result["gpu_seconds"], 13)
        self.assertEqual(result["active_node_id"], "active:0002")

    def test_different_scope_never_crosslinks_identical_fingerprint_or_frontier(self):
        self.study(self.archive, [(self.shared, 2.0)], start=100, scope="old-scope")
        self.study(self.active, [(self.shared, 2.0)], start=200)
        result = DASH.view_snapshot(
            self.active, snapshot_only=True, history_roots=[self.archive]
        )
        self.assertNotIn("duplicate_of", result["nodes"][1])
        self.assertNotIn("imported_from", result["nodes"][1])
        self.assertEqual(
            [point["source_id"] for point in result["frontier"]["points"]], ["active"]
        )
        self.assertTrue(result["coverage"]["limitations"])

    def test_ledger_strict_cutoff_includes_failures_but_no_invented_scope_or_review(
        self,
    ):
        self.study(self.archive, [(self.first, 3.0)], start=100)
        self.study(self.active, [(self.shared, 2.0)], start=200)
        self.ledger(self.first, 10, value=3.0)
        failed = "d" * 64
        self.ledger(failed, 20, status="failed")
        self.ledger("e" * 64, 100, value=1.0)
        self.ledger("f" * 64, None, value=1.0)
        result = DASH.view_snapshot(
            self.active,
            snapshot_only=True,
            history_roots=[self.archive],
            records_root=self.records,
        )
        self.assertEqual(result["coverage"]["ledger_record_count"], 2)
        self.assertEqual(result["coverage"]["ledger_point_count"], 1)
        self.assertEqual(result["coverage"]["ledger_cutoff"], 100)
        self.assertEqual(result["coverage"]["ledger"]["missing_timestamps"], 1)
        self.assertEqual(result["coverage"]["ledger"]["at_or_after_cutoff"], 1)
        ledger = result["frontier"]["legacy_points"][0]
        self.assertIsNone(ledger["comparison_scope"])
        self.assertFalse(ledger["reviewed"])
        self.assertFalse(ledger["frontier_eligible"])
        nodes = {node["id"]: node for node in result["nodes"]}
        self.assertEqual(nodes["ledger:" + failed]["status"], "failed")
        self.assertIsNone(nodes["ledger:" + failed]["value"])
        archive = nodes["history-1:0001"]
        self.assertEqual(archive["same_ledger_result"], "ledger:" + self.first)
        self.assertNotIn("imported_from", archive)
        self.assertNotIn("duplicate_of", archive)
        self.assertEqual(result["coverage"]["unique_observed_fingerprint_count"], 2)
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_ledger_cutoff_uses_active_and_history_even_when_history_is_later(self):
        self.study(self.active, [(self.first, 3.0)], start=100)
        self.study(self.archive, [(self.shared, 2.0)], start=200)
        self.ledger("d" * 64, 150, value=2.5)
        result = DASH.view_snapshot(
            self.active,
            snapshot_only=True,
            history_roots=[self.archive],
            records_root=self.records,
        )
        self.assertEqual(result["coverage"]["ledger_cutoff"], 100)
        self.assertEqual(result["coverage"]["ledger_record_count"], 0)
        self.assertEqual(result["coverage"]["ledger"]["at_or_after_cutoff"], 1)
        self.assertEqual(result["frontier"]["legacy_points"], [])

    def test_history_is_offline_and_wrapper_namespaces_even_without_extra_sources(self):
        self.study(self.archive, [(self.first, 3.0)], start=100)
        self.study(self.active, [(self.shared, 2.0)], start=200)
        backend = Mock()
        backend.inspect.return_value = DASH.read(
            self.active / "control/backend-snapshot.json"
        )
        self.put(self.active, "control/ownership.json", {"project": str(self.active)})
        with patch.object(DASH, "ProTokBackend", return_value=backend) as factory:
            result = DASH.view_snapshot(self.active, history_roots=[self.archive])
        factory.assert_called_once()
        self.assertEqual(len(result["nodes"]), 2)
        standalone = DASH.view_snapshot(self.active, snapshot_only=True)
        self.assertEqual(standalone["nodes"][0]["id"], "active:0001")
        self.assertEqual(
            DASH.snapshot(self.active, snapshot_only=True)["nodes"][0]["id"], "0001"
        )

    def test_unavailable_history_frontier_is_reported_without_membership_fallback(self):
        self.study(self.archive, [(self.first, 3.0)], start=100)
        self.study(self.active, [(self.shared, 2.0)], start=200)
        (self.archive / "study/frontiers.json").unlink()
        result = DASH.view_snapshot(
            self.active, snapshot_only=True, history_roots=[self.archive]
        )
        self.assertEqual(len(result["nodes"]), 2)
        self.assertEqual(
            [p["source_id"] for p in result["frontier"]["points"]], ["active"]
        )
        self.assertFalse(result["frontier"]["sources"][0]["available"])
        self.assertTrue(result["coverage"]["limitations"])

    def test_real_ledger_layout_retains_smokes_without_quality_points_or_aliases(self):
        self.study(self.archive, [(self.first, 3.0)], start=100)
        self.study(self.active, [(self.shared, 2.0)], start=200)
        for number in range(1, 13):
            self.ledger(
                f"{number:064x}",
                number,
                value=3.0 + number if number <= 10 else None,
                status="complete" if number <= 10 else "failed",
            )
        scored_fingerprint = f"{1:064x}"
        for index, fingerprint in enumerate(
            (scored_fingerprint, self.shared, "e" * 64)
        ):
            prefix = "smoke-" + fingerprint
            self.put(
                self.records,
                prefix + "/state.json",
                {
                    "fingerprint": fingerprint,
                    "created_at": 30 + index,
                    "status": "complete" if index < 2 else "failed",
                    "mode": "smoke",
                    "attempts": [],
                },
            )
            self.put(
                self.records,
                prefix + "/run-config.json",
                {"protocol": DASH.PROTOCOL, "metric": DASH.METRIC},
            )
            if index < 2:
                self.put(
                    self.records,
                    prefix + "/summary.json",
                    {"quality_score_eligible": False},
                )
        result = DASH.view_snapshot(
            self.active,
            snapshot_only=True,
            history_roots=[self.archive],
            records_root=self.records,
        )
        nodes = {node["id"]: node for node in result["nodes"]}
        self.assertEqual(result["coverage"]["ledger_record_count"], 15)
        self.assertEqual(result["coverage"]["ledger_point_count"], 10)
        self.assertEqual(result["coverage"]["ledger_smoke_count"], 3)
        self.assertIn("ledger:" + scored_fingerprint, nodes)
        smoke = nodes["ledger:smoke-" + scored_fingerprint]
        self.assertEqual(smoke["record_mode"], "smoke")
        self.assertTrue(smoke["diagnostic"])
        self.assertIsNone(smoke["observed_value"])
        self.assertIn("冒烟", smoke["route"])
        self.assertIn("冒烟", smoke["title"])
        self.assertIn("冒烟", smoke["action_summary"])
        self.assertIsNone(smoke["value"])
        self.assertEqual(nodes["ledger:smoke-" + "e" * 64]["status"], "failed")
        self.assertNotIn("same_ledger_result", nodes["active:0001"])
        self.assertTrue(
            all(
                "smoke-" not in point["id"]
                for point in result["frontier"]["legacy_points"]
            )
        )
        self.assertEqual(
            smoke["detail_url"], "/api/node/ledger/smoke-" + scored_fingerprint
        )
        details = DASH.source_details(
            self.active,
            "ledger",
            "smoke-" + scored_fingerprint,
            history_roots=[self.archive],
            records_root=self.records,
        )
        self.assertEqual(details["ledger"]["mode"], "smoke")
        self.assertIsNone(details["ledger"]["observed_value"])
        self.assertFalse(details["frontier_eligible"])
        # Exercise the actual HTTP route with an in-memory connection, without opening a service.
        from io import BytesIO

        connection = Mock()
        connection.makefile.return_value = BytesIO(
            ("GET " + smoke["detail_url"] + " HTTP/1.0\r\n\r\n").encode()
        )
        response = bytearray()
        connection.sendall.side_effect = response.extend
        with patch.object(DASH, "ThreadingHTTPServer") as factory:
            factory.return_value.server_port = 0
            DASH.serve(
                self.active,
                0,
                snapshot_only=True,
                history_roots=[self.archive],
                records_root=self.records,
            )
            handler = factory.call_args.args[1]
        handler(connection, ("127.0.0.1", 1), Mock())
        self.assertIn(b"200 OK", response)
        self.assertIn(b'"record_mode": "smoke"', response)
        # Either diagnostic mode or an ineligible-quality flag prevents curve admission.
        scored_summary_path = self.records / scored_fingerprint / "summary.json"
        scored_summary = DASH.read(scored_summary_path)
        scored_summary["quality_score_eligible"] = False
        self.put(self.records, scored_fingerprint + "/summary.json", scored_summary)
        self.put(
            self.records,
            "smoke-" + scored_fingerprint + "/summary.json",
            {
                "fingerprint": scored_fingerprint,
                "protocol": DASH.PROTOCOL,
                "objective": DASH.METRIC,
                "quality_score_eligible": True,
                "value": 0.001,
            },
        )
        guarded = DASH.view_snapshot(
            self.active,
            snapshot_only=True,
            history_roots=[self.archive],
            records_root=self.records,
        )
        self.assertEqual(guarded["coverage"]["ledger_point_count"], 9)
        self.assertTrue(
            all(
                "smoke-" not in point["id"]
                for point in guarded["frontier"]["legacy_points"]
            )
        )
        for invalid in (
            "smoke-../" + scored_fingerprint,
            "smoke-smoke-" + scored_fingerprint,
        ):
            with self.assertRaises(ValueError):
                DASH.source_details(
                    self.active, "ledger", invalid, records_root=self.records
                )

    def test_source_details_uses_only_configured_sources_and_keeps_local_refs(self):
        self.study(self.archive, [(self.first, 3.0)], start=100)
        self.study(self.active, [(self.shared, 2.0)], start=200)
        self.ledger(self.first, 10, value=3.0)
        history = DASH.source_details(
            self.active, "history-1", "0001", history_roots=[self.archive]
        )
        self.assertEqual(history["source_id"], "history-1")
        self.assertEqual(history["metadata"]["id"], "0001")
        ledger = DASH.source_details(
            self.active,
            "ledger",
            self.first,
            history_roots=[self.archive],
            records_root=self.records,
        )
        self.assertFalse(ledger["reviewed"])
        self.assertEqual(ledger["ledger"]["attempts"], [])
        self.assertNotIn("agents", ledger)
        self.assertNotIn("plan", ledger)
        self.assertNotIn("PRIVATE", json.dumps(ledger))
        for source, local in [
            ("history-2", "0001"),
            ("../archive", "0001"),
            ("history-1", "../../state"),
            ("active", "abcd"),
        ]:
            with self.assertRaises(ValueError):
                DASH.source_details(
                    self.active, source, local, history_roots=[self.archive]
                )
        with self.assertRaises(ValueError):
            DASH.source_details(
                self.active, "ledger", "../state", records_root=self.records
            )


if __name__ == "__main__":
    unittest.main()
