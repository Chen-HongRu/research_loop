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
        "candidates": [
            {
                "title": "Condition injection",
                "question": "Is condition information lost?",
                "observations": ["H readout regressed at longer training."],
                "hypothesis": "A conditioning bottleneck causes the regression.",
                "prior_attempts": ["Earlier conditional baseline"],
                "difference_from_prior": "Intervene on repeated conditioning, not the readout.",
                "minimal_experiment": "One matched-budget conditioning ablation.",
                "expected_outcomes": [
                    "Gain supports the bottleneck",
                    "No gain weakens it",
                ],
                "source_ids": ["paper"],
                "scope_requirements": [],
                "ready_to_test": True,
            }
        ],
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
        self.assertEqual(report["result"]["candidates"][0]["id"], report["id"] + ":C1")
        call = harness.calls[0]
        self.assertFalse(call["writable"])
        self.assertEqual(call["workspace"], call["run_dir"])
        self.assertEqual(call["timeout_seconds"], 600)
        context = read(call["run_dir"] / "context.json")
        self.assertEqual(len(context["recent_reviews"]), 3)
        self.assertEqual(len(context["recent_candidates"]), 3)
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

    def test_new_work_needs_three_publications_and_thirty_minutes(self):
        manager = self.manager()
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

    def test_only_active_execute_stage_and_stop_gate_admission(self):
        manager = self.manager()
        self.activate(4, phase="review")
        self.assertFalse(manager.launch_if_due())
        self.activate(4, job_status="complete")
        self.assertTrue(manager.launch_if_due())
        self.complete(manager)
        self.activate(7)
        self.now = 3000
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
            candidates=[],
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

    def test_result_requires_sources_differences_and_explicit_scope_labels(self):
        valid = result()
        valid["candidates"][0].update(
            scope_requirements=["Changing the sampling protocol"], ready_to_test=False
        )
        self.assertIs(validate_result(valid), valid)
        for mutate in (
            lambda r: r["candidates"].extend(copy.deepcopy(r["candidates"]) * 3),
            lambda r: r["sources"][0].update(url="file:///etc/passwd"),
            lambda r: r["sources"][0].update(
                url="https://secret:password@example.com/paper"
            ),
            lambda r: r["candidates"][0].update(source_ids=["unknown"]),
            lambda r: r["candidates"][0].update(difference_from_prior=""),
            lambda r: r["candidates"][0].update(expected_outcomes=[]),
            lambda r: r["candidates"][0].update(
                scope_requirements=["New dataset"], ready_to_test=True
            ),
        ):
            with self.subTest(mutation=mutate):
                response = result()
                mutate(response)
                with self.assertRaises(ValueError):
                    validate_result(response)

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
        response["candidates"][0]["source_ids"].append("local")
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
