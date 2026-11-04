"""Small synthetic legacy control documents; no harness, jobs or artifact reads."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research_loop.runtime.legacy_inventory import (
    capture_control_annex,
    capture_legacy,
    verify_inventory,
)


class RuntimeLegacyInventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "legacy"
        self.root.mkdir()
        for directory in ("iterations", "routes", "tasks"):
            (self.root / directory).mkdir()
        self.config = {
            "version": 3,
            "workspace": "/unfollowed/private/workspace",
            "baseline": "unfollowed-git-version",
            "harness": {"backend": "codex", "model": "gpt-6.1-sol"},
            "max_iterations": 10,
            "max_calls": None,
            "max_seconds": 3600,
            "allow_write": False,
            "global_evaluation": None,
            "selection": {"seed": 7, "debate_rounds": 1, "max_resamples": 1},
        }
        self.state = {
            "iteration": 0,
            "phase": "done",
            "status": "ready",
            "calls": {},
            "call_count": 0,
            "elapsed_seconds": 0.0,
            "sampling_queue": [],
            "sample_counter": 0,
            "selection_round": 0,
            "active_attempt": None,
            "debugging": None,
            "stop_reason": None,
            "goal_status": "undetermined",
            "review_ref": None,
        }
        self.write("study.json", self.config)
        self.write("state.json", self.state)
        self.write("routes/index.json", {})
        self.write("frontiers.json", {"global": {}, "local": {}})
        self.write("research.md", "# Existing research\n")
        (self.root / "goal.md").write_bytes(b"Preserve this goal.\r\n\r\n")

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            value if isinstance(value, str) else json.dumps(value, indent=3) + "\n"
        )
        return path

    def attempt(self, phase, *, complete=True, running=False):
        reference = f"iterations/0001/{phase}/attempt-001"
        self.write(
            f"{reference}/context.json",
            {"goal": "Frozen goal", "artifact": "/unfollowed/dataset"},
        )
        self.write(
            f"{reference}/process.json",
            {
                "run_id": phase,
                "status": "running" if running else "succeeded",
                "pid": 999999,
            },
        )
        output = {"markdown": f"Original {phase} output", "opaque": {"extra": 1}}
        self.write(f"{reference}/response.json", output)
        if complete:
            self.write(
                f"{reference}/completed.json",
                {"version": 1, "run_id": phase, "response": output},
            )
        self.write(f"{reference}/parameter-budget.json", {"deadline": 123.5})
        return reference

    def settled(self):
        calls = {phase: self.attempt(phase) for phase in ("plan", "execute", "review")}
        self.write(
            f"{calls['plan']}/canonical-response.json",
            {
                "decision": "investigate",
                "markdown": "Exact original plan",
                "base_experiment": None,
            },
        )
        self.state.update(
            iteration=1,
            status="stopped",
            stop_reason="review",
            calls=calls,
            accepted_calls={key: True for key in calls},
            call_count=19,
            elapsed_seconds=12.75,
            plan_ref=f"{calls['plan']}/canonical-response.json",
            execution_ref=f"{calls['execute']}/response.json",
            review_ref=f"{calls['review']}/response.json",
        )
        self.write("state.json", self.state)
        metadata = {
            "id": "0001",
            "status": "reviewed",
            "workspace": "/unfollowed/worktree",
            "workspace_ref": "original-code-version",
            "output_artifacts": [{"path": "/unfollowed/checkpoint", "version": "v1"}],
        }
        self.write("iterations/0001/iteration.json", metadata)
        self.write("research.md", "Published text\n")
        self.write(
            "iterations/0001/publication.json",
            {
                "before": {"research.md": "Old text\n"},
                "writes": {"research.md": "Published text\n"},
                "metadata": metadata,
                "state_after": self.state,
            },
        )
        return calls

    def test_pristine_preserves_exact_text_null_limits_and_missing_sentinels(self):
        inventory = capture_legacy(self.root)
        self.assertEqual(inventory["version"], 1)
        self.assertEqual(
            inventory["records"]["goal.md"]["text"], "Preserve this goal.\r\n\r\n"
        )
        self.assertEqual(
            inventory["records"]["observations.json"], {"identity": None, "text": None}
        )
        self.assertEqual(
            inventory["counters"], {"iterations": 0, "calls": 0, "elapsed_seconds": 0.0}
        )
        self.assertIsNone(inventory["limits"]["max_calls"])
        self.assertIsNone(inventory["limits"]["admission_deadline"])
        self.assertEqual(inventory["native_state"], self.state)
        self.assertEqual(inventory["blockers"], [])
        self.assertFalse(inventory["authority_fenced"])
        self.assertEqual(json.loads(json.dumps(inventory, allow_nan=False)), inventory)

    def test_settled_history_preserves_original_plan_and_reserved_counter(self):
        calls = self.settled()
        inventory = capture_legacy(self.root)
        self.assertEqual(inventory["counters"]["calls"], 19)
        self.assertEqual(len(inventory["history"]["attempt_refs"]), 3)
        self.assertEqual(inventory["history"]["publications"][0]["status"], "settled")
        self.assertEqual(
            inventory["records"][self.state["plan_ref"]]["text"],
            (self.root / self.state["plan_ref"]).read_text(),
        )
        self.assertIn(
            "scientific_translation_requires_explicit_adapter", inventory["blockers"]
        )
        self.assertEqual(
            json.loads(
                inventory["records"][calls["execute"] + "/parameter-budget.json"][
                    "text"
                ]
            )["deadline"],
            123.5,
        )
        self.assertFalse(
            any(
                "unknown_attempt_retirement" in reason
                for reason in inventory["blockers"]
            )
        )

    def test_partial_publication_and_conflicting_edits_are_not_replayed(self):
        self.settled()
        self.state.update(
            phase="review",
            status="needs_attention",
            active_attempt=self.state["calls"]["review"],
        )
        self.write("state.json", self.state)
        self.write(
            "iterations/0001/iteration.json", {"id": "0001", "status": "executed"}
        )
        inventory = capture_legacy(self.root)
        self.assertEqual(inventory["history"]["publications"][0]["status"], "prepared")
        self.assertIn("partial_publication:iterations/0001", inventory["blockers"])
        self.assertEqual(json.loads((self.root / "state.json").read_text()), self.state)
        self.write("research.md", "Independent human edit\n")
        conflicted = capture_legacy(self.root)
        self.assertEqual(
            conflicted["history"]["publications"][0]["status"], "conflicted"
        )
        self.assertIn("publication_conflict:research.md", conflicted["blockers"])
        self.assertEqual(
            (self.root / "research.md").read_text(), "Independent human edit\n"
        )

    def test_unknown_attempt_and_elapsed_interval_remain_explicit(self):
        reference = self.attempt("execute", complete=False, running=True)
        self.state.update(
            iteration=1,
            phase="execute",
            status="running",
            active_attempt=reference,
            call_count=27,
            elapsed_seconds=9.0,
        )
        self.write("state.json", self.state)
        self.write(
            "iterations/0001/iteration.json", {"id": "0001", "status": "planned"}
        )
        self.write(
            "observations.json",
            {
                "runs": [
                    {"id": "crashed-run", "status": "running", "wall_seconds": None}
                ],
                "intervals": [],
            },
        )
        inventory = capture_legacy(self.root)
        self.assertEqual(inventory["counters"]["calls"], 27)
        self.assertIn(reference, inventory["history"]["attempt_refs"])
        self.assertIn("unknown_attempt_retirement:" + reference, inventory["blockers"])
        self.assertIn("unknown_elapsed_interval:crashed-run", inventory["blockers"])
        self.assertFalse(inventory["history"]["attempts"][0]["completed"])

    def test_superseded_completed_rejection_is_history_not_unresolved_work(self):
        calls = self.settled()
        original = calls["plan"]
        self.write(
            original + "/rejection.json", {"version": 1, "errors": ["old parameter"]}
        )
        replacement = original.replace("attempt-001", "attempt-002")
        for name in (
            "context.json",
            "process.json",
            "response.json",
            "completed.json",
            "canonical-response.json",
        ):
            self.write(
                replacement + "/" + name, (self.root / original / name).read_text()
            )
        self.state["calls"]["plan"] = replacement
        self.state["plan_ref"] = replacement + "/canonical-response.json"
        self.write("state.json", self.state)
        inventory = capture_legacy(self.root)
        self.assertIn(original, inventory["history"]["attempt_refs"])
        self.assertFalse(
            any(
                reason.startswith("rejected_attempt_requires_reconciliation:")
                for reason in inventory["blockers"]
            )
        )

    def test_verification_reads_metadata_only_and_allows_root_staging_and_locks(self):
        inventory = capture_legacy(self.root)
        self.write(".migration-stage/state.json", "staging is not legacy authority")
        self.write(".lock", "")
        self.write("tasks/queue.lock", "")
        with (
            patch.object(Path, "read_bytes", side_effect=AssertionError("body reread")),
            patch.object(Path, "read_text", side_effect=AssertionError("body reread")),
        ):
            verify_inventory(self.root, inventory)
        self.write("state.json", self.state | {"call_count": 1})
        with self.assertRaisesRegex(ValueError, "file changed: state.json"):
            verify_inventory(self.root, inventory)

    def test_missing_file_and_new_control_directory_are_detected(self):
        inventory = capture_legacy(self.root)
        self.write("control/STOP", "Operator stop\n")
        with self.assertRaisesRegex(ValueError, "control/STOP"):
            verify_inventory(self.root, inventory)
        inventory = capture_legacy(self.root)
        (self.root / "iterations/0001").mkdir()
        with self.assertRaisesRegex(ValueError, "directory changed: iterations"):
            verify_inventory(self.root, inventory)

    def test_source_lock_may_create_only_absent_tasks_and_regular_queue_lock(self):
        (self.root / "tasks").rmdir()
        inventory = capture_legacy(self.root)
        self.write("tasks/queue.lock", "")
        verify_inventory(self.root, inventory)
        self.write("tasks/new/request.json", {"id": "new"})
        with self.assertRaisesRegex(ValueError, "directory changed: tasks"):
            verify_inventory(self.root, inventory)
        (self.root / "tasks/new/request.json").unlink()
        (self.root / "tasks/new").rmdir()
        (self.root / "tasks/queue.lock").unlink()
        (self.root / "tasks/queue.lock").symlink_to(self.root / "goal.md")
        with self.assertRaisesRegex(ValueError, "directory changed: tasks"):
            verify_inventory(self.root, inventory)

    def test_runtime_handoff_exception_is_explicit_and_only_skips_two_markers(self):
        inventory = capture_legacy(self.root)
        self.write("runtime.json", {"handoff": "caller authenticates this"})
        self.write("runtime.sqlite3", "caller-owned handoff artifact")
        with self.assertRaisesRegex(ValueError, "file changed: runtime.json"):
            verify_inventory(self.root, inventory)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("body read")):
            verify_inventory(self.root, inventory, allow_runtime_handoff=True)
        self.write("state.json", self.state | {"call_count": 1})
        with self.assertRaisesRegex(ValueError, "file changed: state.json"):
            verify_inventory(self.root, inventory, allow_runtime_handoff=True)

    def test_added_attempt_is_detected_without_scanning_workspace(self):
        self.settled()
        inventory = capture_legacy(self.root)
        (self.root / "iterations/0001/execute/attempt-002").mkdir()
        with self.assertRaisesRegex(ValueError, "directory changed"):
            verify_inventory(self.root, inventory)

    def test_capture_never_reads_workspace_artifact_or_callback_logs(self):
        calls = self.settled()
        self.write("iterations/0001/workspace/checkpoint.json", "not control data")
        self.write(calls["execute"] + "/stdout.jsonl", "not a controller record")
        self.write(calls["execute"] + "/untracked-dataset.json", "do not inspect")
        read = Path.read_bytes
        seen = []

        def controlled(path):
            relative = path.relative_to(self.root).as_posix()
            self.assertNotIn("workspace", path.relative_to(self.root).parts)
            self.assertNotIn(path.name, {"stdout.jsonl", "untracked-dataset.json"})
            seen.append(relative)
            return read(path)

        with patch.object(Path, "read_bytes", controlled):
            inventory = capture_legacy(self.root)
        self.assertIn(self.state["plan_ref"], seen)
        self.assertFalse(any("checkpoint" in path for path in inventory["records"]))

    def test_mutation_during_read_is_rejected(self):
        read = Path.read_bytes

        def mutate(path):
            value = read(path)
            if path.name == "goal.md":
                path.write_bytes(value + b"changed")
            return value

        with (
            patch.object(Path, "read_bytes", mutate),
            self.assertRaisesRegex(ValueError, "changed while reading: goal.md"),
        ):
            capture_legacy(self.root)

    def test_symlinks_and_escaping_control_refs_are_rejected(self):
        external = self.root.parent / "private.txt"
        external.write_text("must not read")
        (self.root / "goal.md").unlink()
        (self.root / "goal.md").symlink_to(external)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            capture_legacy(self.root)
        (self.root / "goal.md").unlink()
        self.write("goal.md", "restored")
        self.state["plan_ref"] = "../private.txt"
        self.write("state.json", self.state)
        with self.assertRaisesRegex(ValueError, "inside the Study"):
            capture_legacy(self.root)

    def test_unsupported_child_study_is_named_without_reading_it(self):
        self.write(
            "tasks/manual/request.json",
            {"id": "manual", "kind": "long_training", "instruction": "original task"},
        )
        self.write("tasks/manual/study/state.json", "not followed")
        inventory = capture_legacy(self.root)
        self.assertIn(
            "unsupported_control_entry:tasks/manual/study", inventory["blockers"]
        )
        self.assertNotIn("tasks/manual/study/state.json", inventory["records"])

    def test_runtime_or_wrong_legacy_version_is_refused(self):
        self.write("study.json", self.config | {"version": 2})
        with self.assertRaisesRegex(ValueError, "version 3"):
            capture_legacy(self.root)
        self.write("study.json", self.config)
        self.write("runtime.sqlite3", "not opened")
        with self.assertRaisesRegex(ValueError, "SQLite"):
            capture_legacy(self.root)

    def test_explicit_annex_detaches_text_and_verifies_metadata_only(self):
        original = capture_legacy(self.root)
        source = self.root.parent / "controller.json"
        source.write_bytes(b'{"scope": "original"}\r\n')
        captured = capture_control_annex(original, {"authorization": str(source)})
        self.assertNotIn("annex", original)
        self.assertEqual(
            captured["annex"]["records"]["authorization"]["text"],
            '{"scope": "original"}\r\n',
        )
        self.assertNotIn("../controller.json", captured["records"])
        captured["native_state"]["call_count"] = 42
        self.assertEqual(original["native_state"]["call_count"], 0)
        with (
            patch.object(
                Path, "read_bytes", side_effect=AssertionError("annex body reread")
            ),
            patch.object(
                Path, "read_text", side_effect=AssertionError("annex body reread")
            ),
        ):
            verify_inventory(self.root, captured)
        source.write_text('{"scope": "changed"}')
        with self.assertRaisesRegex(ValueError, "annex:authorization"):
            verify_inventory(self.root, captured)
        with self.assertRaisesRegex(ValueError, "different binding"):
            capture_control_annex(captured, {"authorization": str(source)})

    def test_annex_missing_sentinel_and_invalid_names_or_symlinks(self):
        source = self.root.parent / "missing.json"
        captured = capture_control_annex(
            capture_legacy(self.root), {"missing": str(source)}
        )
        self.assertIsNone(captured["annex"]["records"]["missing"]["identity"])
        source.write_text("{}")
        with self.assertRaisesRegex(ValueError, "annex:missing"):
            verify_inventory(self.root, captured)
        for name in ("../escape", "a/b", "annex:a", ""):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, "logical names"),
            ):
                capture_control_annex(captured, {name: str(source)})
        link = self.root.parent / "symlink.json"
        link.symlink_to(source)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            capture_control_annex(captured, {"link": str(link)})

    def test_annex_capture_rejects_mutation_during_read(self):
        source = self.root.parent / "controller.json"
        source.write_text("{}")
        original = capture_legacy(self.root)
        read = Path.read_bytes

        def change(path):
            result = read(path)
            path.write_text('{"changed": true}')
            return result

        with (
            patch.object(Path, "read_bytes", change),
            self.assertRaisesRegex(ValueError, "changed during capture"),
        ):
            capture_control_annex(original, {"controller": str(source)})


if __name__ == "__main__":
    unittest.main()
