import contextlib
import io
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from benchmarks.startup import research
from research_loop import submission
from research_loop.core import BudgetExhausted
from research_loop.harness import HarnessConfig, HarnessError
from test_core import RecordedHarness, read


class CampaignDelegate(RecordedHarness):
    def __init__(self, config):
        super().__init__()
        self.stop_after = 1000

    def response(self, phase, context, kwargs):
        if phase == "execute":
            workspace = Path(kwargs["workspace"])
            task = context["manual_task"]
            if task["title"] == "baseline-control":
                revision = (
                    task["instruction"].split("workspace commit ", 1)[1].split()[0]
                )
                mode = (
                    "api"
                    if context["global_evaluation"]["id"] == research.API_SCOPE
                    else "docs"
                )
                for name in research.editable_for(mode):
                    (workspace / name).write_text(
                        research.git(workspace, "show", f"{revision}:{name}") + "\n"
                    )
            elif task["title"] == "optimize":
                path = workspace / "SKILL.md"
                path.write_text(path.read_text() + f"\n{task['id']}\n")
            submission.submit(workspace, "evaluate")
            end = time.monotonic() + 5
            while not (workspace / submission.RESULT).exists():
                if time.monotonic() > end:
                    raise AssertionError("Synthetic trusted evaluator did not finish")
                time.sleep(0.01)
            result = read(workspace / submission.RESULT)
            if result["status"] != "completed":
                return {"markdown": "Synthetic prelaunch failure; no GPU or models."}
            return {
                "markdown": "Synthetic local evaluation completed; no GPU or models."
            }
        result = super().response(phase, context, kwargs)
        if phase == "plan":
            result["timeout_seconds"] = 36000
        return result


def synthetic_evaluate(harness, workspace, iteration, stop, deadline):
    current = harness.rounds[iteration]
    controller = harness.args.root / "controller" / f"{iteration:04d}"
    candidate = research.freeze_candidate(
        harness.args.source,
        harness.args.revision,
        workspace,
        harness.baseline,
        controller / "runtime",
        unchanged=current["control"],
        change_base=current["source_revision"],
        require_change=current["kind"] == "optimize",
        candidate_scope=harness.candidate_scope,
    )
    mean = 200 if current["control"] else 100 - iteration
    cases = []
    for number in range(1, research.REPEATS + 1):
        receipt = controller / f"case-{number:02d}.json"
        receipt.write_text("{}")
        cases.append(
            {"receipt": str(receipt), "success": True, "startup_seconds": mean}
        )
    harness.case_count += research.REPEATS
    return {
        "status": "completed",
        "candidate": candidate,
        "cases": cases,
        "comparison_scope": harness.scope,
        "baseline_control": current["control"],
        "metrics": {
            "mean_startup_seconds": mean,
            "success_rate": 1,
            "scalar_visibility_rate": 1,
        },
    }


class StartupResearchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "references").mkdir()
        (self.source / "SKILL.md").write_text("Original instructions.\n")
        (self.source / "references/operations.md").write_text("Original operations.\n")
        (self.source / "frozen.py").write_text("original = True\n")
        for name in research.API_EDITABLE[len(research.EDITABLE) :]:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("original_api = True\n")
        research.git(self.source, "init", "-q")
        research.git(self.source, "add", ".")
        research.git(
            self.source,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "Frozen fixture",
        )

    def argv(self, root):
        return [
            "--root",
            str(root),
            "--source",
            str(self.source),
            "--prepared-source",
            str(self.source),
            "--codex-wrapper",
            "/unused",
            "--host",
            "unused",
            "--remote-root",
            "/unused",
            "--remote-python",
            "/unused",
        ]

    def campaign(self, root, *extra):
        with (
            patch.object(research, "CodexHarness", CampaignDelegate),
            patch.object(
                research.StartupResearchHarness, "evaluate", synthetic_evaluate
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = research.main([*self.argv(root), *extra])
        summary = read(root / "summary.json")
        self.assertEqual(code, 0, summary)
        return summary

    def test_defaults_caps_and_budget_admission(self):
        args = research.parse_args(self.argv(self.root / "study"))
        self.assertEqual(
            (args.budget_seconds, args.max_iterations, args.baseline_every),
            (3600, 2, 5),
        )
        harness = research.StartupResearchHarness(
            args, HarnessConfig(), "unused", deadline=0
        )
        with patch.object(research.time, "monotonic", return_value=0):
            with self.assertRaisesRegex(BudgetExhausted, "campaign_budget"):
                harness.admit_iteration("0001")
            self.assertEqual(harness.call_allowance(10000), 0)
        for option, value in (
            ("--budget-seconds", "36001"),
            ("--max-iterations", "61"),
            ("--baseline-every", "1"),
        ):
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                research.parse_args([*self.argv(self.root / "invalid"), option, value])

    def test_dynamic_queue_runs_six_full_reviewed_rounds_and_uses_best_base(self):
        root = self.root / "campaign"
        summary = self.campaign(
            root, "--budget-seconds", "36000", "--max-iterations", "6"
        )
        self.assertEqual(summary["completed_rounds"], 6)
        self.assertEqual(summary["evaluation_cases_started"], 18)
        self.assertEqual(summary["stop_reason"], "iteration_budget")
        self.assertEqual(set(summary["task_statuses"].values()), {"reviewed"})
        self.assertEqual(summary["rounds"]["5"]["kind"], "baseline-control")
        self.assertEqual(summary["rounds"]["6"]["base_experiment"], "0004")
        self.assertEqual(summary["results"]["5"]["candidate"]["changed_files"], [])
        self.assertEqual(summary["best_experiment"], "0006")
        prior_before = (root / "study/state.json").read_bytes()
        follow = self.root / "follow"
        follow_summary = self.campaign(follow, "--prior-study", str(root))
        self.assertEqual(follow_summary["completed_rounds"], 2)
        self.assertEqual(
            follow_summary["rounds"]["1"]["kind"], "prior-best-reproduction"
        )
        self.assertEqual(follow_summary["rounds"]["2"]["base_experiment"], "0001")
        self.assertEqual((root / "study/state.json").read_bytes(), prior_before)
        previous = Path(summary["results"]["6"]["candidate"]["runtime_snapshot"])
        reproduced = Path(
            follow_summary["results"]["1"]["candidate"]["runtime_snapshot"]
        )
        for name in research.EDITABLE:
            self.assertEqual(
                (previous / name).read_bytes(), (reproduced / name).read_bytes()
            )
        self.assertEqual(
            read(follow / "prior-evidence.json")["best"]["iteration"], "0006"
        )

    def test_too_little_budget_is_graceful_and_launches_nothing(self):
        summary = self.campaign(
            self.root / "short", "--budget-seconds", "1", "--max-iterations", "60"
        )
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["stop_reason"], "campaign_budget")
        self.assertEqual(summary["evaluation_cases_started"], 0)
        self.assertEqual(summary["completed_rounds"], 0)

    def test_reproduction_and_protected_code_cannot_change(self):
        baseline = research.git(self.source, "rev-parse", "HEAD")
        (self.source / "SKILL.md").write_text("Changed instructions.\n")
        with self.assertRaisesRegex(ValueError, "reproduction"):
            research.freeze_candidate(
                self.source,
                baseline,
                self.source,
                baseline,
                self.root / "bad",
                unchanged=False,
                require_change=False,
            )
        (self.source / "frozen.py").write_text("original = False\n")
        with self.assertRaisesRegex(ValueError, "protected"):
            research.freeze_candidate(
                self.source,
                baseline,
                self.source,
                baseline,
                self.root / "bad",
                unchanged=False,
            )

    def test_late_evaluation_is_incomplete_and_unranked_without_launch(self):
        args = research.parse_args(self.argv(self.root / "late"))
        args.root.mkdir()
        args.revision = research.git(self.source, "rev-parse", "HEAD")
        harness = research.StartupResearchHarness(
            args, HarnessConfig(), args.revision, deadline=100
        )
        (args.root / "controller/0001").mkdir(parents=True)
        harness.rounds[1] = {
            "control": True,
            "kind": "baseline-control",
            "source_revision": args.revision,
            "base_experiment": None,
        }
        with (
            patch.object(research.time, "monotonic", return_value=99),
            patch.object(research.subprocess, "Popen") as popen,
            patch.object(research, "freeze_candidate", return_value={"id": "test"}),
        ):
            receipt = harness.evaluate(
                self.source, 1, SimpleNamespace(is_set=lambda: False), 10000
            )
        popen.assert_not_called()
        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(receipt["cases"], [])
        self.assertEqual(receipt["metrics"]["mean_startup_seconds"], 420)
        self.assertFalse(research.eligible(receipt, {}, "0001"))
        self.assertEqual(harness.case_count, 0)

    def test_campaign_deadline_overrides_short_plan_timeout(self):
        args = research.parse_args(self.argv(self.root / "allowance"))
        (args.root / "study").mkdir(parents=True)
        (args.root / "study/state.json").write_text('{"iteration": 1}')
        harness = research.StartupResearchHarness(
            args, HarnessConfig(), "unused", deadline=5000
        )
        harness.delegate = Mock()
        with patch.object(research.time, "monotonic", return_value=100):
            harness.admit_iteration("0001")
            harness.run(
                run_dir=args.root / "study/iterations/0001/plan/attempt-001",
                timeout_seconds=5,
                prompt="x",
                schema={},
                writable=False,
            )
        self.assertEqual(harness.delegate.run.call_args.kwargs["timeout_seconds"], 4900)

    def test_expiry_during_any_stage_stops_normally_without_ranking(self):
        original = CampaignDelegate.response
        for expired_phase in ("plan", "execute", "review"):
            with self.subTest(phase=expired_phase):
                root = self.root / f"expires-{expired_phase}"

                def expire(delegate, phase, context, kwargs):
                    if phase == expired_phase:
                        raise BudgetExhausted("campaign_budget")
                    return original(delegate, phase, context, kwargs)

                with patch.object(CampaignDelegate, "response", expire):
                    summary = self.campaign(root)
                self.assertEqual(summary["status"], "completed")
                self.assertEqual(summary["stop_reason"], "campaign_budget")
                self.assertEqual(summary["completed_rounds"], 0)
                self.assertEqual(summary["unreviewed_rounds"], [1])
                self.assertEqual(read(root / "study/frontiers.json")["global"], {})

    def test_timeout_at_deadline_is_budget_but_earlier_failure_is_error(self):
        args = research.parse_args(self.argv(self.root / "timeout"))
        (args.root / "study").mkdir(parents=True)
        (args.root / "study/state.json").write_text('{"iteration": 1}')
        harness = research.StartupResearchHarness(
            args, HarnessConfig(), "unused", deadline=5000
        )
        harness.delegate = Mock()
        harness.delegate.run.side_effect = HarnessError("model timeout")
        kwargs = dict(
            run_dir=args.root / "study/iterations/0001/plan/attempt-001",
            timeout_seconds=5,
            prompt="x",
            schema={},
            writable=False,
        )
        with patch.object(research.time, "monotonic", side_effect=[100, 100, 4999.5]):
            with self.assertRaisesRegex(BudgetExhausted, "campaign_budget"):
                harness.run(**kwargs)
        with patch.object(research.time, "monotonic", return_value=100):
            with self.assertRaisesRegex(HarnessError, "model timeout"):
                harness.run(**kwargs)

    def test_recovery_settles_retained_candidate_without_replaying_execute(self):
        root = self.root / "recovery"
        calls = []

        def fail_second(harness, workspace, iteration, stop, deadline):
            calls.append(iteration)
            if iteration == 2:
                raise HarnessError(
                    "Insufficient Execute budget for all three fixed cases"
                )
            return synthetic_evaluate(harness, workspace, iteration, stop, deadline)

        with (
            patch.object(research, "CodexHarness", CampaignDelegate),
            patch.object(research.StartupResearchHarness, "evaluate", fail_second),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(research.main(self.argv(root)), 1)
        before = read(root / "study/state.json")
        self.assertEqual(before["iteration"], 2)
        workspace = Path(
            read(root / "study/iterations/0002/iteration.json")["workspace"]
        )
        candidate_before = (workspace / "SKILL.md").read_bytes()
        config_before = (root / "study/study.json").read_bytes()
        goal_before = (root / "study/goal.md").read_bytes()
        deadline = read(root / "summary.json")["deadline_unix"]
        original_argv = self.argv(root)
        argv = [
            "--resume-root",
            str(root),
            *original_argv[2:],
            "--deadline-unix",
            str(deadline),
        ]
        with (
            patch.object(research, "CodexHarness", CampaignDelegate),
            patch.object(
                research.StartupResearchHarness, "evaluate", synthetic_evaluate
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = research.main(argv)
        summary = read(root / "summary.json")
        self.assertEqual(code, 0, summary)
        self.assertEqual(summary["completed_rounds"], 2)
        self.assertEqual(summary["evaluation_cases_started"], 6)
        self.assertEqual(summary["state"]["call_count"], before["call_count"] + 1)
        self.assertEqual((workspace / "SKILL.md").read_bytes(), candidate_before)
        self.assertEqual((root / "study/study.json").read_bytes(), config_before)
        self.assertEqual((root / "study/goal.md").read_bytes(), goal_before)
        self.assertEqual(summary["deadline_unix"], deadline)
        self.assertEqual(summary["task_statuses"]["round-0002"], "reviewed")
        self.assertTrue(
            (
                root / "study/recoveries/campaign-deadline-0002.previous/response.json"
            ).exists()
        )

    def test_api_scope_freezes_only_four_allowed_files(self):
        baseline = research.git(self.source, "rev-parse", "HEAD")
        api = self.source / "benchmarks/startup/client.py"
        api.write_text("improved_api = True\n")
        with self.assertRaisesRegex(ValueError, "protected"):
            research.freeze_candidate(
                self.source,
                baseline,
                self.source,
                baseline,
                self.root / "docs-rejected",
                unchanged=False,
            )
        candidate = research.freeze_candidate(
            self.source,
            baseline,
            self.source,
            baseline,
            self.root / "api-accepted",
            unchanged=False,
            candidate_scope="api",
        )
        self.assertEqual(candidate["editable_scope"], list(research.API_EDITABLE))
        self.assertEqual(
            (
                Path(candidate["runtime_snapshot"]) / api.relative_to(self.source)
            ).read_text(),
            api.read_text(),
        )
        (self.source / "frozen.py").write_text("protected_change = True\n")
        with self.assertRaisesRegex(ValueError, "protected"):
            research.freeze_candidate(
                self.source,
                baseline,
                self.source,
                baseline,
                self.root / "api-rejected",
                unchanged=False,
                candidate_scope="api",
            )

    def test_api_history_links_partial_study_without_importing_scores(self):
        previous = self.root / "docs"
        self.campaign(previous)
        state_path = previous / "study/state.json"
        state = read(state_path)
        state.update(iteration=3, phase="execute", status="needs_attention")
        state_path.write_text(json.dumps(state))
        before = state_path.read_bytes()
        current = self.root / "api"
        result = self.campaign(
            current, "--candidate-scope", "api", "--history-study", str(previous)
        )
        self.assertEqual(result["comparison_scope"], research.API_SCOPE)
        self.assertEqual(result["rounds"]["1"]["kind"], "baseline-control")
        self.assertEqual(result["rounds"]["1"]["base_experiment"], None)
        self.assertEqual(result["results"]["1"]["candidate"]["changed_files"], [])
        self.assertEqual(
            set(read(current / "study/frontiers.json")["global"]), {research.API_SCOPE}
        )
        history = read(current / "history-evidence.json")["histories"][0]
        self.assertEqual(len(history["results"]), 2)
        self.assertEqual(history["results"][0]["comparison_scope"], research.SCOPE)
        self.assertEqual(state_path.read_bytes(), before)
        goal = (current / "study/goal.md").read_text()
        for name in research.API_EDITABLE:
            self.assertIn(name, goal)
        self.assertIn(
            str(research.FIXED_ROOT / "src/research_loop/submission.py"), goal
        )

    def test_api_evaluator_flag_and_cooperative_stop(self):
        args = research.parse_args(
            [*self.argv(self.root / "evaluation"), "--candidate-scope", "api"]
        )
        args.root.mkdir()
        args.revision = research.git(self.source, "rev-parse", "HEAD")
        harness = research.StartupResearchHarness(
            args, HarnessConfig(), args.revision, deadline=time.monotonic() + 3000
        )
        harness.rounds[1] = {
            "kind": "baseline-control",
            "control": True,
            "source_revision": args.revision,
            "base_experiment": None,
        }
        candidate = research.freeze_candidate(
            self.source,
            args.revision,
            self.source,
            args.revision,
            args.root / "frozen",
            unchanged=True,
            candidate_scope="api",
        )
        (args.root / "controller/0001").mkdir(parents=True)

        def launch(command, **kwargs):
            self.assertEqual(command[-2:], ["--candidate-scope", "api"])
            case = Path(command[command.index("--root") + 1])
            case.mkdir()
            (case / "benchmark-result.json").write_text(
                json.dumps(
                    {
                        "outer": {
                            "exit_code": 0,
                            "timed_out": False,
                            "launch_error": None,
                        },
                        "display_verification": {"ok": True},
                        "display_ready_seconds": 150,
                        "host_worker_unresolved": False,
                    }
                )
            )
            return SimpleNamespace(returncode=0, wait=Mock())

        with (
            patch.object(research, "freeze_candidate", return_value=candidate),
            patch.object(research.subprocess, "Popen", side_effect=launch) as launched,
        ):
            result = harness.evaluate(
                self.source,
                1,
                SimpleNamespace(is_set=lambda: False),
                time.monotonic() + 600,
            )
        self.assertEqual(launched.call_count, 3)
        self.assertEqual(result["comparison_scope"], research.API_SCOPE)
        original = CampaignDelegate.response

        def stop_after_review(delegate, phase, context, kwargs):
            response = original(delegate, phase, context, kwargs)
            if phase == "review":
                (Path(kwargs["run_dir"]).parents[4] / "STOP_AFTER_ROUND").touch()
            return response

        with patch.object(CampaignDelegate, "response", stop_after_review):
            summary = self.campaign(self.root / "stop", "--max-iterations", "6")
        self.assertEqual(summary["stop_reason"], "operator_scope_change")
        self.assertEqual(summary["completed_rounds"], 1)
        self.assertEqual(set(summary["task_statuses"].values()), {"reviewed"})


if __name__ == "__main__":
    unittest.main()
