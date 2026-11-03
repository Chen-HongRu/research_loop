import json
from pathlib import Path
import tempfile
import threading
import unittest

from research_loop.scheduling import (
    AsyncStudyScheduler,
    child_blocked,
    child_run,
)


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "control").mkdir()

    def row(self, identity):
        return {"id": identity, "folder": self.root / identity}

    def test_child_lock_is_keyed_and_contention_is_not_a_failure(self):
        first, second = self.root / "a", self.root / "b"
        with child_run(first), child_run(second):
            with self.assertRaises(BlockingIOError):
                with child_run(first):
                    self.fail("Duplicate child owner")
        self.assertIsNone(child_blocked(first))
        self.assertIsNone(child_blocked(second))

    def test_preinitialization_error_survives_restart_and_new_work_runs(self):
        rows = [self.row("a"), self.row("b")]
        completed = set()
        calls = []
        done = threading.Event()

        def pending(*, allow_new):
            return [row for row in rows if row["id"] not in completed]

        def run(identity, *, allow_new):
            calls.append(identity)
            if identity == "a":
                raise ValueError("Clone failed before child initialization")
            completed.add(identity)
            done.set()

        def scheduler():
            return AsyncStudyScheduler(
                self.root, pending=pending, run_one=run, name="test", interval=0.001
            )

        first = scheduler()
        first.finish_nominations()
        first.start()
        first.thread.join(2)
        self.assertFalse(first.thread.is_alive())
        self.assertTrue(done.is_set())
        self.assertEqual(first.status()["status"], "blocked")
        self.assertEqual(first.status()["blocked"][0]["id"], "a")
        rows.append(self.row("c"))
        restarted = scheduler()
        restarted.finish_nominations()
        restarted.start()
        restarted.thread.join(2)
        self.assertFalse(restarted.thread.is_alive())
        self.assertEqual(calls.count("a"), 1)
        self.assertIn("c", completed)
        # Explicitly advancing this exact child clears its scheduler marker.
        with child_run(self.root / "a"):
            pass
        self.assertIsNone(child_blocked(self.root / "a"))

    def test_handoff_drains_active_children_and_does_not_start_third(self):
        rows = [self.row("a"), self.row("b")]
        entered = {identity: threading.Event() for identity in ("a", "b")}
        release, done = threading.Event(), set()
        calls = []

        def pending(*, allow_new):
            return [row for row in rows if row["id"] not in done] if allow_new else []

        def run(identity, *, allow_new):
            calls.append(identity)
            entered[identity].set()
            release.wait(3)
            done.add(identity)

        scheduler = AsyncStudyScheduler(
            self.root, pending=pending, run_one=run, name="test", interval=0.001
        )
        scheduler.start()
        try:
            self.assertTrue(entered["a"].wait(1))
            self.assertTrue(entered["b"].wait(1))
            (self.root / "control/HANDOFF").touch()
            rows.append(self.row("c"))
            closer = threading.Thread(target=scheduler.shutdown)
            closer.start()
            self.assertTrue(closer.is_alive())
            release.set()
            closer.join(2)
            self.assertFalse(closer.is_alive())
            self.assertEqual(set(calls), {"a", "b"})
            self.assertEqual(done, {"a", "b"})
        finally:
            release.set()
            scheduler.shutdown()

    def test_state_retry_releases_marker_but_unknown_running_child_stays_blocked(self):
        folder = self.root / "a"
        (folder / "study").mkdir(parents=True)
        state = folder / "study/state.json"
        state.write_text(json.dumps({"status": "running", "phase": "execute"}))
        with self.assertRaisesRegex(ValueError, "Unknown"):
            with child_run(folder):
                raise ValueError("Unknown effects")
        self.assertIn("Unknown", child_blocked(folder))
        state.write_text(json.dumps({"status": "ready", "phase": "execute"}))
        self.assertIsNone(child_blocked(folder))

    def test_dispatcher_does_not_attach_old_error_to_an_explicit_retry(self):
        folder = self.root / "a"
        (folder / "study").mkdir(parents=True)
        state = folder / "study/state.json"
        state.write_text(json.dumps({"status": "needs_attention", "phase": "execute"}))

        def run(identity, *, allow_new):
            try:
                with child_run(folder):
                    raise RuntimeError("Old child failure")
            except RuntimeError:
                # Explicit recovery wins ownership before the dispatcher catches
                # the already-recorded failure on its outer stack.
                state.write_text(json.dumps({"status": "ready", "phase": "execute"}))
                raise

        scheduler = AsyncStudyScheduler(
            self.root, pending=lambda **kwargs: [], run_one=run, name="test"
        )
        scheduler._run_child(self.row("a"))
        self.assertIsNone(child_blocked(folder))
