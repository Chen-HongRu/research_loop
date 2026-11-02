import copy
import json
import signal
import os
import subprocess
import sys
import textwrap
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from research_loop.harness import HarnessConfig
from research_loop.scout import (
    REPORT_MAX_BYTES,
    ScoutManager,
    retrieval_events,
    snapshot,
    validate_result,
)


def result():
    return {
        "summary": "Test whether conditional information survives depth.",
        "search_status": "completed",
        "search_notes": "Opened the paper and official implementation.",
        "sources": [
            {
                "id": "paper",
                "title": "A primary paper",
                "url": "https://arxiv.org/abs/2302.05442",
                "kind": "paper",
                "accessed": "full_text",
            }
        ],
        "insights": [
            {
                "title": "Condition information and changing state distributions",
                "kind": "explanation",
                "question": "Is condition information lost?",
                "local_observations": ["Readout regressed at longer training."],
                "external_findings": [
                    "A paper distinguishes two conditioning mechanisms."
                ],
                "interpretation": "A conditioning bottleneck is one possible explanation.",
                "applicability": "The paper studies a different model; local cause is unresolved.",
                "relation_to_goal": "Preserve conditional generation quality and diversity.",
                "implications": "Reconsider a readout-only explanation.",
                "source_ids": ["paper"],
                "follow_up": "",
                "execution_conditions": [],
                "scope_requirements": [],
            }
        ],
        "open_questions": ["Which conditions distinguish these explanations?"],
    }


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def read(path):
    return json.loads(path.read_text())


class FakeHarness:
    def __init__(
        self, *, response=None, error=None, release=None, web=True, process=False
    ):
        self.response = result() if response is None else response
        self.error = error
        self.release = release
        self.web = web
        self.process = process
        self.entered = threading.Event()
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        run_dir = kwargs["run_dir"]
        if self.web:
            (run_dir / "stdout.jsonl").write_text(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "web_search", "query": "primary paper"},
                    }
                )
                + "\n"
            )
        if self.process:
            write(run_dir / "process.json", {"status": "running", "pid": 7654321})
        self.entered.set()
        if self.release and not self.release.wait(3):
            raise TimeoutError("Test did not release fake harness")
        if self.error:
            raise self.error
        return copy.deepcopy(self.response)


class ScoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = 1000.0
        self.activate(4)
        write(self.root / "control/continuation.json", {"deadline": 30000})
        write(self.root / "control/ownership.json", {"expires_at": 20000})
        (self.root / "study/goal.md").write_text(
            "Generate diverse conditional structures."
        )
        (self.root / "study/research.md").write_text(
            "Do not repeat the earlier control."
        )
        write(
            self.root / "study/research_inputs.json",
            {"inspiration": "Readout bottleneck"},
        )
        write(self.root / "study/routes/index.json", {"R0001": {"status": "active"}})
        route = self.root / "study/routes/R0001/research.md"
        route.parent.mkdir(parents=True)
        route.write_text("The current route.")
        for number in (1, 2, 3):
            folder = self.root / "study/iterations" / f"{number:04d}"
            write(folder / "iteration.json", {"status": "reviewed"})
            write(folder / "publication.json", {"status": "published"})
            write(
                folder / "insight.json",
                {"observed": f"Observation {number}", "unresolved": "Mechanism"},
            )
            (folder / "review.md").write_text(f"Review {number}")
            write(folder / "candidates.json", {"iteration": number, "score": 2.0})

    def activate(self, iteration, *, phase="execute", job_status="submitted"):
        write(
            self.root / "study/state.json",
            {"iteration": iteration, "phase": phase, "status": "running"},
        )
        write(
            self.root / "control/jobs" / f"{iteration:04d}" / "job.json",
            {"status": job_status},
        )

    def manager(self, harness=None, **options):
        harness = FakeHarness() if harness is None else harness
        manager = ScoutManager(
            self.root,
            config=HarnessConfig(),
            harness_factory=lambda config: harness,
            deadline=options.pop("deadline", 30000),
            clock=lambda: self.now,
            **options,
        )
        self.addCleanup(manager.shutdown)
        return manager

    def complete(self, manager):
        manager._thread.join(3)
        self.assertFalse(manager.running, "Scout test worker did not finish")
        return read(self.root / "control/scout/status.json")

    def test_first_run_is_read_only_and_publishes_advice_without_mutating_study(self):
        before = {
            p: p.read_bytes() for p in (self.root / "study").rglob("*") if p.is_file()
        }
        harness = FakeHarness()
        manager = self.manager(harness)
        self.assertTrue(manager.launch_if_due())
        self.assertEqual(self.complete(manager)["status"], "completed")
        report = read(self.root / "control/scout/latest.json")
        self.assertEqual(report["published_through"], 3)
        self.assertEqual(report["web_tool_events"], 1)
        self.assertFalse(report["source_claims_verified"])
        self.assertEqual(report["result"]["insights"][0]["id"], report["id"] + ":I1")
        call = harness.calls[0]
        self.assertFalse(call["writable"])
        self.assertEqual(call["workspace"], call["run_dir"])
        self.assertEqual(call["timeout_seconds"], 600)
        context = read(call["run_dir"] / "context.json")
        self.assertEqual(len(context["recent_reviews"]), 3)
        self.assertEqual(len(context["recent_candidates"]), 3)
        self.assertEqual(len(context["recent_insights"]), 3)
        self.assertEqual(report["version"], 2)
        self.assertIn("Readout bottleneck", context["inputs"]["text"])
        self.assertEqual(
            before,
            {
                p: p.read_bytes()
                for p in (self.root / "study").rglob("*")
                if p.is_file()
            },
        )

    def test_launch_is_nonblocking_and_only_one_worker_across_managers(self):
        release = threading.Event()
        harness = FakeHarness(release=release)
        manager, other = self.manager(harness), self.manager()
        try:
            start = time.monotonic()
            self.assertTrue(manager.launch_if_due())
            self.assertLess(time.monotonic() - start, 0.5)
            self.assertTrue(harness.entered.wait(1))
            self.assertFalse(manager.launch_if_due())
            self.assertFalse(other.launch_if_due())
            self.assertEqual(len(harness.calls), 1)
        finally:
            release.set()
            self.complete(manager)

    def test_custom_cadence_needs_three_publications_and_thirty_minutes(self):
        manager = self.manager(every_iterations=3, min_interval_seconds=1800)
        self.assertTrue(manager.launch_if_due())
        self.complete(manager)
        self.activate(7)
        self.now = 2799
        self.assertFalse(manager.launch_if_due())
        self.now = 2800
        self.activate(6)
        self.assertFalse(manager.launch_if_due())
        self.activate(7)
        self.assertTrue(manager.launch_if_due())
        self.complete(manager)

    def test_default_cadence_needs_one_publication_and_five_minutes(self):
        manager = self.manager()
        self.assertTrue(manager.launch_if_due())
        self.complete(manager)
        self.activate(5)
        self.now = 1299
        self.assertFalse(manager.launch_if_due())
        self.now = 1300
        self.activate(4)
        self.assertFalse(manager.launch_if_due())
        self.activate(5)
        self.assertTrue(manager.launch_if_due())
        self.complete(manager)

    def test_running_research_phases_do_not_require_submitted_jobs(self):
        manager = self.manager(min_interval_seconds=0)
        for iteration, phase in enumerate(
            ("selection", "plan", "execute", "review"), 4
        ):
            self.activate(iteration, phase=phase, job_status="complete")
            self.assertTrue(manager.launch_if_due(), phase)
            self.complete(manager)
        for phase, status in (("done", "running"), ("execute", "needs_attention")):
            write(
                self.root / "study/state.json",
                {"iteration": 8, "phase": phase, "status": status},
            )
            self.assertFalse(manager.launch_if_due())
        self.activate(8)
        (self.root / "control/HANDOFF").touch()
        self.assertFalse(manager.launch_if_due())
        (self.root / "control/HANDOFF").unlink()
        (self.root / "control/STOP").touch()
        self.assertFalse(manager.launch_if_due())

    def test_timeout_is_clipped_only_by_explicit_host_deadline(self):
        write(self.root / "control/ownership.json", {"expires_at": 1080})
        harness = FakeHarness()
        manager = self.manager(harness, deadline=1080)
        self.assertTrue(manager.launch_if_due())
        self.complete(manager)
        self.assertEqual(harness.calls[0]["timeout_seconds"], 75)
        self.activate(7)
        self.now = 1060
        self.assertFalse(manager.launch_if_due())

    def test_close_does_not_wait_and_late_response_is_not_published(self):
        release = threading.Event()
        harness = FakeHarness(release=release)
        manager = self.manager(harness)
        self.assertTrue(manager.launch_if_due())
        try:
            self.assertTrue(harness.entered.wait(1))
            start = time.monotonic()
            manager.close()
            self.assertLess(time.monotonic() - start, 0.1)
            self.assertFalse(manager.launch_if_due())
        finally:
            release.set()
            self.assertEqual(self.complete(manager)["status"], "cancelled")
        self.assertFalse((self.root / "control/scout/latest.json").exists())

    def test_shutdown_in_subprocess_cleans_spawn_race_and_descendants_before_exit(self):
        executable = self.root / "fake-agent"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
            "pathlib.Path(os.environ['SCOUT_TEST_PIDS']).write_text(json.dumps([os.getpid(), child.pid]))\n"
            "time.sleep(60)\n"
        )
        executable.chmod(0o755)
        pids_file = self.root / "pids.json"
        code = textwrap.dedent(
            """\
            import json, pathlib, sys, time
            from research_loop import harness
            from research_loop.harness import HarnessConfig
            from research_loop.scout import ScoutManager
            root, executable, pids = map(pathlib.Path, sys.argv[1:])
            original_write = harness._write_json
            def delayed_pid_publication(path, value):
                if path.name == 'process.json' and value.get('pid'):
                    time.sleep(0.3)
                original_write(path, value)
            harness._write_json = delayed_pid_publication
            manager = ScoutManager(root, config=HarnessConfig(executable=str(executable)), deadline=time.time()+60)
            assert manager.launch_if_due()
            end = time.monotonic() + 5
            while not pids.exists():
                assert time.monotonic() < end, 'CLI did not spawn'
                time.sleep(0.01)
            manager.shutdown()
            assert not manager.running
            """
        )
        env = dict(os.environ, SCOUT_TEST_PIDS=str(pids_file))
        try:
            parent = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    code,
                    str(self.root),
                    str(executable),
                    str(pids_file),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(parent.returncode, 0, parent.stderr)
            self.assertTrue(pids_file.exists(), parent.stderr)
            for pid in read(pids_file):
                end = time.monotonic() + 2
                while True:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    self.assertLess(
                        time.monotonic(), end, f"CLI child {pid} survived shutdown"
                    )
                    time.sleep(0.01)

        finally:
            if pids_file.exists():
                for pid in read(pids_file):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_stop_marker_terminates_only_owned_cli_asynchronously(self):
        release = threading.Event()
        signalled = threading.Event()
        cleaned = threading.Event()
        harness = FakeHarness(
            release=release, process=True, error=RuntimeError("terminated")
        )
        manager = self.manager(harness)

        def kill_group(pid, sig):
            self.assertEqual(pid, 7654321)
            if sig == signal.SIGTERM:
                signalled.set()
                release.set()
            else:
                self.assertEqual(sig, signal.SIGKILL)
                cleaned.set()

        with patch("research_loop.scout.os.killpg", side_effect=kill_group) as kill:
            self.assertTrue(manager.launch_if_due())
            try:
                self.assertTrue(harness.entered.wait(1))
                (self.root / "control/STOP").touch()
                self.assertTrue(signalled.wait(1))
            finally:
                release.set()
                self.assertEqual(self.complete(manager)["status"], "cancelled")
            self.assertTrue(cleaned.wait(1))
            self.assertEqual(kill.call_count, 2)
        self.assertFalse((self.root / "control/scout/latest.json").exists())

    def test_late_lease_result_is_not_published(self):
        release = threading.Event()
        harness = FakeHarness(release=release)
        manager = self.manager(harness)
        self.assertTrue(manager.launch_if_due())
        try:
            self.assertTrue(harness.entered.wait(1))
            self.now = 30001
        finally:
            release.set()
            self.assertEqual(self.complete(manager)["status"], "cancelled")
        self.assertFalse((self.root / "control/scout/latest.json").exists())

    def test_failure_keeps_previous_report_and_never_escapes_launch(self):
        previous = {"id": "earlier-useful-report"}
        write(self.root / "control/scout/latest.json", previous)
        manager = self.manager(FakeHarness(error=RuntimeError("network unavailable")))
        self.assertTrue(manager.launch_if_due())
        status = self.complete(manager)
        self.assertEqual(status["status"], "failed")
        self.assertIn("network unavailable", status["error"])
        self.assertEqual(read(self.root / "control/scout/latest.json"), previous)
        self.assertFalse(manager.launch_if_due())
        (self.root / "study/state.json").write_text("corrupt")
        self.assertFalse(manager.launch_if_due())
        self.assertIn(
            "JSONDecodeError",
            read(self.root / "control/scout/launch_error.json")["error"],
        )

    def test_unobserved_retrieval_cannot_claim_completed_search(self):
        manager = self.manager(FakeHarness(web=False))
        self.assertTrue(manager.launch_if_due())
        self.assertEqual(self.complete(manager)["status"], "degraded")
        report = read(self.root / "control/scout/latest.json")
        self.assertEqual(report["result"]["search_status"], "partial")
        self.assertIn("No completed web-tool event", report["result"]["search_notes"])
        self.assertEqual(report["web_tool_events"], 0)

    def test_unavailable_search_preserves_explicit_downgrade(self):
        response = result()
        response.update(
            search_status="unavailable",
            search_notes="No web capability",
            sources=[],
            insights=[],
        )
        manager = self.manager(FakeHarness(response=response, web=False))
        previous = {"id": "earlier-useful-report"}
        write(self.root / "control/scout/latest.json", previous)
        self.assertTrue(manager.launch_if_due())
        status = self.complete(manager)
        self.assertEqual(status["status"], "degraded")
        self.assertEqual(read(self.root / "control/scout/latest.json"), previous)
        report = read(Path(status["run_dir"]) / "report.json")
        self.assertEqual(report["result"]["search_status"], "unavailable")
        self.assertEqual(report["result"]["sources"], [])

    def test_host_control_files_are_not_interpreted_by_scout(self):
        (self.root / "control/ownership.json").write_text("not JSON")
        (self.root / "control/continuation.json").write_text("not JSON")
        manager = self.manager()
        self.assertTrue(manager.launch_if_due())
        self.assertEqual(self.complete(manager)["deadline"], 30000)

    def test_preparation_time_consumes_the_same_six_hundred_second_budget(self):
        harness = FakeHarness()
        manager = self.manager(harness)

        def prepare(root, state, **kwargs):
            context = snapshot(root, state, **kwargs)
            self.now += 11
            return context

        with patch("research_loop.scout.snapshot", side_effect=prepare):
            self.assertTrue(manager.launch_if_due())
            self.complete(manager)
        self.assertEqual(harness.calls[0]["timeout_seconds"], 589)

    def test_default_production_harness_enables_web_search(self):
        harness = FakeHarness()
        manager = ScoutManager(
            self.root, config=HarnessConfig(), deadline=30000, clock=lambda: self.now
        )
        self.addCleanup(manager.shutdown)
        with patch("research_loop.scout.CodexHarness", return_value=harness) as factory:
            self.assertTrue(manager.launch_if_due())
            self.complete(manager)
        factory.assert_called_once_with(HarnessConfig(), web_search=True)

    def test_invalid_cadence_and_unbounded_budget_are_rejected(self):
        for options in (
            {"timeout_seconds": 601},
            {"timeout_seconds": True},
            {"every_iterations": 0},
            {"min_interval_seconds": float("nan")},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.manager(**options)

    def test_snapshot_excludes_unpublished_and_bounds_source_context(self):
        folder = self.root / "study/iterations/0004"
        write(folder / "iteration.json", {"status": "executed"})
        (folder / "review.md").write_text("Unpublished result must be omitted")
        workspace = self.root / "study/iterations/0003/workspace"
        source = workspace / "model.py"
        source.parent.mkdir(parents=True)
        source.write_text("x" * 19000)
        write(self.root / "control/active-best.json", {"workspace": str(workspace)})
        context = snapshot(
            self.root, read(self.root / "study/state.json"), source_files=("model.py",)
        )
        self.assertNotIn("Unpublished", json.dumps(context))
        self.assertTrue(context["best_source"][0]["truncated"])
        self.assertEqual(len(context["best_source"][0]["text"]), 18000)
        write(self.root / "control/active-best.json", {"workspace": str(self.root)})
        self.assertEqual(
            snapshot(self.root, read(self.root / "study/state.json"))["best_source"], []
        )

    def test_insights_allow_non_executable_findings_and_separate_prerequisites(self):
        valid = result()
        valid["insights"][0].update(
            execution_conditions=[
                "Independent Plan/Review",
                "Confirm named input exists",
            ],
            scope_requirements=["A different sampling protocol needs authorization"],
        )
        self.assertIs(validate_result(valid), valid)
        for mutate in (
            lambda r: r["insights"].extend(copy.deepcopy(r["insights"]) * 3),
            lambda r: r["sources"][0].update(url="file:///etc/passwd"),
            lambda r: r["sources"][0].update(
                url="https://secret:password@example.com/paper"
            ),
            lambda r: r["insights"][0].update(source_ids=["unknown"]),
            lambda r: r["insights"][0].update(interpretation=""),
            lambda r: r["insights"][0].update(source_ids=[]),
            lambda r: r.update(summary="x" * 2001),
            lambda r: r.update(open_questions=["x"] * 9),
        ):
            with self.subTest(mutation=mutate):
                response = result()
                mutate(response)
                with self.assertRaises(ValueError):
                    validate_result(response)
        valid["sources"][0]["kind"] = "community_code"
        valid["sources"][0]["accessed"] = "code"
        validate_result(valid)

    def test_oversize_utf8_report_is_observable_and_keeps_previous_advice(self):
        previous = {"id": "earlier-useful-report"}
        write(self.root / "control/scout/latest.json", previous)
        response = result()
        insight = response["insights"][0]
        insight["local_observations"] = ["中" * 2000] * 8
        insight["external_findings"] = ["中" * 2000] * 8
        response["insights"] = [copy.deepcopy(insight) for _ in range(3)]
        self.assertGreater(
            len(json.dumps(response, ensure_ascii=False).encode()), REPORT_MAX_BYTES
        )
        validate_result(response)
        manager = self.manager(FakeHarness(response=response))
        self.assertTrue(manager.launch_if_due())
        status = self.complete(manager)
        self.assertEqual(status["status"], "failed")
        self.assertIn("UTF-8 bytes", status["error"])
        self.assertEqual(read(self.root / "control/scout/latest.json"), previous)

    def test_empty_completed_search_does_not_erase_prior_insights(self):
        previous = {"id": "earlier-useful-report"}
        write(self.root / "control/scout/latest.json", previous)
        response = result()
        response["insights"] = []
        manager = self.manager(FakeHarness(response=response))
        self.assertTrue(manager.launch_if_due())
        self.assertEqual(self.complete(manager)["status"], "completed")
        self.assertEqual(read(self.root / "control/scout/latest.json"), previous)
        context = snapshot(self.root, {"iteration": 5})
        self.assertIn(response["open_questions"][0], context["previous_scout"]["text"])
        self.assertTrue(context["previous_scout"]["path"].endswith("/report.json"))

    def test_context_covers_all_routes_rotates_details_and_continues_questions(self):
        routes = {
            f"R{i:04d}": {"title": f"Question {i}", "status": "active"}
            for i in range(1, 21)
        }
        write(self.root / "study/routes/index.json", routes)
        write(
            self.root / "study/iterations/0003/iteration.json",
            {"status": "reviewed", "route_id": "R0020"},
        )
        write(
            self.root / "control/scout/latest.json",
            {
                "id": "previous",
                "result": {"open_questions": ["An unresolved external contradiction"]},
            },
        )
        state = read(self.root / "study/state.json")
        first = snapshot(self.root, state)
        self.assertEqual(set(first["route_index"]), set(routes))
        self.assertIn("R0020", [r["id"] for r in first["routes"]])
        self.assertEqual(len(first["routes"]), 8)
        self.assertIn(
            "unresolved external contradiction", first["previous_scout"]["text"]
        )
        self.assertNotIn(
            str(self.root / "control/scout/latest.json"), first["local_evidence"]
        )
        later = snapshot(self.root, {**state, "iteration": 13})
        self.assertNotEqual(
            [r["id"] for r in first["routes"]], [r["id"] for r in later["routes"]]
        )
        (self.root / "study/iterations/0003/publication.json").unlink()
        self.assertNotIn("Observation 3", json.dumps(snapshot(self.root, state)))

    def test_snapshot_prefers_frozen_goal_and_published_route_reference(self):
        frozen = self.root / "study/iterations/0004/context"
        frozen.mkdir(parents=True)
        (frozen / "global.before.md").write_text(
            "Frozen observation; interpretation unresolved"
        )
        write(
            frozen / "routes.before.json",
            {"R0001": {"research_ref": "iterations/0003/route.after.md"}},
        )
        (self.root / "study/iterations/0003/route.after.md").write_text(
            "Immutable route knowledge"
        )
        context = snapshot(self.root, read(self.root / "study/state.json"))
        self.assertEqual(
            context["research"]["text"], "Frozen observation; interpretation unresolved"
        )
        self.assertEqual(
            context["routes"][0]["research"]["text"], "Immutable route knowledge"
        )

    def test_previous_scout_citations_remain_bound_to_the_immutable_report(self):
        harness = FakeHarness()
        manager = self.manager(harness)
        self.assertTrue(manager.launch_if_due())
        status = self.complete(manager)
        original = Path(status["run_dir"]) / "report.json"
        original_id = read(original)["id"]
        response = result()
        response["sources"].append(
            {
                "id": "previous",
                "title": "Earlier external interpretation",
                "url": str(original),
                "kind": "prior_record",
                "accessed": "context_only",
            }
        )
        response["insights"][0]["source_ids"].append("previous")
        harness.response = response
        self.activate(5)
        self.now = 1300
        self.assertTrue(manager.launch_if_due())
        self.assertEqual(self.complete(manager)["status"], "completed")
        current = read(self.root / "control/scout/latest.json")
        self.assertNotEqual(current["id"], original_id)
        self.assertEqual(current["result"]["sources"][-1]["url"], str(original))
        self.assertEqual(read(original)["id"], original_id)

    def test_unavailable_empty_search_preserves_the_reading_continuation(self):
        harness = FakeHarness()
        manager = self.manager(harness)
        self.assertTrue(manager.launch_if_due())
        original = Path(self.complete(manager)["run_dir"]) / "report.json"
        response = result()
        response.update(
            search_status="unavailable", sources=[], insights=[], open_questions=[]
        )
        harness.response, harness.web = response, False
        self.activate(5)
        self.now = 1300
        self.assertTrue(manager.launch_if_due())
        self.assertEqual(self.complete(manager)["status"], "degraded")
        context = snapshot(self.root, {"iteration": 6})
        self.assertEqual(context["previous_scout"]["path"], str(original))
        self.assertIn(result()["open_questions"][0], context["previous_scout"]["text"])

    def test_frozen_local_citations_preserve_useful_external_sources_and_provenance(
        self,
    ):
        response = result()
        local = str(self.root.resolve() / "study/research_inputs.json")
        response["sources"].insert(
            0,
            {
                "id": "local",
                "title": "Frozen research inputs",
                "url": local,
                "kind": "prior_record",
                "accessed": "context_only",
            },
        )
        response["insights"][0]["source_ids"].append("local")
        manager = self.manager(FakeHarness(response=response))
        self.assertTrue(manager.launch_if_due())
        self.assertEqual(self.complete(manager)["status"], "completed")
        report = read(self.root / "control/scout/latest.json")
        self.assertEqual(len(report["result"]["sources"]), 2)
        self.assertEqual(
            report["source_provenance"][0]["origin"], "frozen_local_snapshot"
        )
        self.assertEqual(report["source_provenance"][1]["origin"], "external_reference")
        for changes in (
            {"url": "/etc/passwd"},
            {"kind": "paper"},
            {"accessed": "full_text"},
        ):
            invalid = copy.deepcopy(response)
            invalid["sources"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_result(invalid, local_evidence=[local])

    def test_source_files_are_explicit_relative_and_bounded(self):
        for paths in (("/etc/passwd",), ("../secret",), ("model.py",) * 9):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                self.manager(source_files=paths)

    def test_failed_web_calls_and_invalid_lines_are_not_retrieval_evidence(self):
        path = self.root / "events.jsonl"
        events = [
            {"type": "item.started", "item": {"type": "web_search"}},
            {
                "type": "item.completed",
                "item": {"type": "web_search", "status": "failed"},
            },
            {
                "type": "item.completed",
                "item": {"type": "mcp_tool_call", "server": "web", "tool": "run"},
            },
            {"type": "item.completed", "item": {"type": "command_execution"}},
        ]
        path.write_text("malformed\n" + "\n".join(json.dumps(e) for e in events))
        self.assertEqual(retrieval_events(path), 1)


if __name__ == "__main__":
    unittest.main()
