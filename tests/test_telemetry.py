"""Event/cursor contracts use only local temporary files and stdlib concurrency."""

import concurrent.futures
from contextlib import redirect_stdout
import io
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research_loop import telemetry


def source(**changes):
    return {
        "kind": "long-training",
        "experiment_id": "0002",
        "source_identity": "source-hash",
        "execution_identity": "execution-hash",
        "run_id": "L0123456789abcdef",
        **changes,
    }


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.log = self.root / "remote/events.jsonl"
        self.source = source()

    def event_bytes(self, name="loss", value=1.0, **kwargs):
        before = self.log.stat().st_size if self.log.exists() else 0
        event = telemetry.emit(name, value, path=self.log, **kwargs)
        with self.log.open("rb") as handle:
            handle.seek(before)
            return event, handle.read()

    def register(self):
        return telemetry.register(self.root / "study", "long-run", self.source)

    def test_explicit_path_default_internal_nonfinite_and_size_limit(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "RESEARCH_LOOP_EVENT_FILE"):
                telemetry.emit("loss", 1.0)
        with patch.dict(os.environ, {"RESEARCH_LOOP_EVENT_FILE": str(self.log)}):
            event = telemetry.emit(
                "loss", {"value": math.nan, "gradient": [math.inf, -math.inf]}, step=2
            )
        self.assertEqual(event["tags"], ["internal"])
        self.assertEqual(
            event["value"],
            {
                "value": {"nonfinite": "NaN"},
                "gradient": [{"nonfinite": "Infinity"}, {"nonfinite": "-Infinity"}],
            },
        )
        self.assertEqual(json.loads(self.log.read_bytes()), event)
        before = self.log.read_bytes()
        with self.assertRaisesRegex(ValueError, "8192"):
            telemetry.emit("too-large", "x" * 8192, path=self.log)
        with self.assertRaisesRegex(ValueError, "integer"):
            telemetry.emit("bad-step", 1.0, step=True, path=self.log)
        self.assertEqual(self.log.read_bytes(), before)

    def test_concurrent_writers_emit_whole_unique_lines(self):
        def write(index):
            return telemetry.emit("loss", index, path=self.log, step=index)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            emitted = list(pool.map(write, range(80)))
        lines = self.log.read_bytes().splitlines()
        self.assertEqual(len(lines), 80)
        records = [json.loads(line) for line in lines]
        self.assertEqual({row["id"] for row in records}, {row["id"] for row in emitted})
        self.assertEqual({row["step"] for row in records}, set(range(80)))

    def test_writer_repairs_only_missing_delimiter_in_one_append(self):
        self.log.parent.mkdir()
        fragment = b'{"killed":"hidden-fragment"'
        self.log.write_bytes(fragment)
        with (
            patch.object(telemetry.os, "write", wraps=os.write) as write,
            patch.object(
                telemetry.os,
                "fsync",
                side_effect=AssertionError("Per-event fsync perturbs training"),
            ),
        ):
            event = telemetry.emit("resumed", 3.0, path=self.log)
        self.assertEqual(write.call_count, 1)
        self.assertTrue(self.log.read_bytes().startswith(fragment + b"\n"))
        self.register()
        raw = self.log.read_bytes()
        telemetry.append_chunk(
            self.root / "study", "long-run", self.source, raw, offset=0
        )
        page = telemetry.read_events(self.root / "study", "long-run")
        self.assertEqual([row["id"] for row in page["events"]], [event["id"]])
        self.assertEqual(page["events"][0]["offset"], len(fragment) + 1)
        self.assertEqual(len(page["errors"]), 1)
        self.assertNotIn("hidden-fragment", json.dumps(page["errors"]))

    def test_actual_size_recovers_append_before_metadata_publication(self):
        self.register()
        _, first = self.event_bytes()
        with patch.object(
            telemetry, "_write", side_effect=OSError("publication interrupted")
        ):
            with self.assertRaisesRegex(OSError, "interrupted"):
                telemetry.append_chunk(
                    self.root / "study", "long-run", self.source, first, offset=0
                )
        current = telemetry.info(self.root / "study", "long-run")
        self.assertEqual(current["byte_end"], len(first))
        with self.assertRaisesRegex(ValueError, f"expected {len(first)}"):
            telemetry.append_chunk(
                self.root / "study", "long-run", self.source, first, offset=0
            )
        _, second = self.event_bytes(value=2.0)
        telemetry.append_chunk(
            self.root / "study", "long-run", self.source, second, offset=len(first)
        )
        self.assertEqual(Path(current["log_path"]).read_bytes(), first + second)
        self.assertEqual(
            telemetry.register(self.root / "study", "long-run", self.source)[
                "byte_end"
            ],
            len(first + second),
        )

    def test_unknown_or_changed_identity_never_rewrites_evidence(self):
        with self.assertRaisesRegex(ValueError, "Unknown"):
            telemetry.info(self.root / "study", "unknown")
        self.register()
        _, raw = self.event_bytes()
        telemetry.append_chunk(
            self.root / "study", "long-run", self.source, raw, offset=0
        )
        changed = source(source_identity="different")
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            telemetry.register(self.root / "study", "long-run", changed)
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            telemetry.append_chunk(
                self.root / "study", "long-run", changed, b"other", offset=len(raw)
            )
        self.assertEqual(
            Path(
                telemetry.info(self.root / "study", "long-run")["log_path"]
            ).read_bytes(),
            raw,
        )
        with self.assertRaisesRegex(ValueError, "Owner tokens"):
            telemetry.register(
                self.root / "study", "unsafe", source(owner_token="never-log-this")
            )
        for invalid in ("../escape", "with.dot"):
            with self.assertRaisesRegex(ValueError, "stream ID"):
                telemetry.info(self.root / "study", invalid)

    def test_partial_last_line_does_not_advance_and_resumes_exactly(self):
        self.register()
        emitted, raw = self.event_bytes()
        half = len(raw) // 2
        telemetry.append_chunk(
            self.root / "study", "long-run", self.source, raw[:half], offset=0
        )
        page = telemetry.read_events(self.root / "study", "long-run")
        self.assertEqual(page["events"], [])
        self.assertEqual(page["next_offset"], 0)
        telemetry.append_chunk(
            self.root / "study", "long-run", self.source, raw[half:], offset=half
        )
        page = telemetry.read_events(
            self.root / "study", "long-run", after=page["next_offset"]
        )
        self.assertEqual(page["events"][0]["id"], emitted["id"])
        self.assertEqual(page["next_offset"], len(raw))
        self.assertEqual(page["events"][0]["source"], self.source)
        self.assertEqual(page["events"][0]["end_offset"], len(raw))

    def test_dashboard_filter_and_all_tag_frozen_pages_ignore_late_appends(self):
        self.register()
        self.event_bytes("internal-observation", 1.0)
        visible, _ = self.event_bytes("visible", 2.0, tags=("dashboard", "custom"))
        self.event_bytes("custom-observation", 3.0, tags=("custom",))
        raw = self.log.read_bytes()
        telemetry.append_chunk(
            self.root / "study", "long-run", self.source, raw, offset=0
        )
        shown = telemetry.snapshot(self.root / "study", tag="dashboard")
        self.assertEqual(
            [row["id"] for row in shown["streams"][0]["events"]], [visible["id"]]
        )
        self.assertEqual(
            len(telemetry.snapshot(self.root / "study")["streams"][0]["events"]), 3
        )
        destination = self.root / "frozen/telemetry.json"
        frozen = telemetry.freeze(self.root / "study", "0002", destination)
        before = destination.read_bytes()
        self.assertEqual(len(frozen["streams"][0]["preview"]["events"]), 3)
        self.assertEqual(frozen["streams"][0]["byte_end"], len(raw))
        _, late = self.event_bytes("late", 4.0, tags=("dashboard",))
        telemetry.append_chunk(
            self.root / "study", "long-run", self.source, late, offset=len(raw)
        )
        self.assertEqual(
            telemetry.freeze(self.root / "study", "0002", destination), frozen
        )
        self.assertEqual(destination.read_bytes(), before)
        bound = frozen["streams"][0]["byte_end"]
        first = telemetry.read_events(
            self.root / "study", "long-run", until=bound, limit=1
        )
        next_page = telemetry.read_events(
            self.root / "study", "long-run", after=first["next_offset"], until=bound
        )
        self.assertEqual(len(first["events"] + next_page["events"]), 3)
        self.assertEqual(next_page["next_offset"], bound)
        output = io.StringIO()
        with redirect_stdout(output):
            telemetry.main(
                [
                    "--study",
                    str(self.root / "study"),
                    "--stream",
                    "long-run",
                    "--until",
                    str(bound),
                ]
            )
        self.assertEqual(len(json.loads(output.getvalue())["events"]), 3)

    def test_malformed_complete_records_are_flagged_without_raw_contents(self):
        self.register()
        first, one = self.event_bytes("first")
        last, two = self.event_bytes("last", math.nan)
        raw = one + b"bad\rhidden-secret\n" + two
        telemetry.append_chunk(
            self.root / "study", "long-run", self.source, raw, offset=0
        )
        page = telemetry.read_events(self.root / "study", "long-run")
        self.assertEqual(
            [row["id"] for row in page["events"]], [first["id"], last["id"]]
        )
        self.assertEqual(page["events"][-1]["value"], {"nonfinite": "NaN"})
        self.assertEqual(page["next_offset"], len(raw))
        self.assertEqual(page["errors"][0]["offset"], len(one))
        self.assertNotIn("hidden-secret", json.dumps(page["errors"]))

    def test_read_apis_do_not_create_directories_or_acquire_mutation_locks(self):
        absent = self.root / "absent-study"
        with patch.object(
            telemetry,
            "_locked",
            side_effect=AssertionError("Read attempted mutation lock"),
        ):
            self.assertEqual(telemetry.snapshot(absent), {"version": 1, "streams": []})
            with self.assertRaisesRegex(ValueError, "Unknown"):
                telemetry.info(absent, "unknown")
            self.assertFalse(absent.exists())
        self.register()
        _, raw = self.event_bytes()
        telemetry.append_chunk(
            self.root / "study", "long-run", self.source, raw, offset=0
        )
        with patch.object(
            telemetry,
            "_locked",
            side_effect=AssertionError("Read attempted mutation lock"),
        ):
            self.assertEqual(
                telemetry.info(self.root / "study", "long-run")["byte_end"], len(raw)
            )
            self.assertEqual(
                len(telemetry.read_events(self.root / "study", "long-run")["events"]), 1
            )
            self.assertEqual(len(telemetry.snapshot(self.root / "study")["streams"]), 1)

    def test_status_and_experiment_filter_do_not_change_log_or_source(self):
        current = self.register()
        updated = telemetry.update_status(
            self.root / "study",
            "long-run",
            status="running",
            remote_size=20,
            remote_offset=10,
            remote_identity=[1, 2],
            error=None,
        )
        self.assertEqual(updated["source"], current["source"])
        self.assertEqual(updated["remote_identity"], [1, 2])
        self.assertEqual(updated["remote_offset"], 10)
        self.assertEqual(updated["byte_end"], 0)
        telemetry.register(self.root / "study", "another", source(experiment_id="0003"))
        self.assertEqual(
            len(
                telemetry.snapshot(self.root / "study", experiment_id="0002")["streams"]
            ),
            1,
        )
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            telemetry.update_status(self.root / "study", "long-run", source=source())

    def test_oversized_line_pages_progress_to_following_valid_record(self):
        self.register()
        valid, event = self.event_bytes("after-oversized")
        raw = b"x" * 131073 + b"\n" + event
        telemetry.append_chunk(
            self.root / "study", "long-run", self.source, raw, offset=0
        )
        first = telemetry.read_events(self.root / "study", "long-run")
        self.assertEqual(first["events"], [])
        self.assertEqual(first["next_offset"], 131072)
        self.assertTrue(first["errors"])
        second = telemetry.read_events(
            self.root / "study", "long-run", after=first["next_offset"]
        )
        self.assertEqual([row["id"] for row in second["events"]], [valid["id"]])
        self.assertEqual(second["events"][0]["offset"], 131074)
        self.assertEqual(second["next_offset"], len(raw))
        with self.assertRaisesRegex(ValueError, "page limits"):
            telemetry.read_events(self.root / "study", "long-run", max_bytes=32)

    def test_discarded_oversized_suffix_cannot_be_parsed_as_a_new_event(self):
        self.register()
        fake, suffix = self.event_bytes("not-a-new-record")
        valid, event = self.event_bytes("actual-next-record")
        raw = b"x" * 131072 + suffix + event
        telemetry.append_chunk(
            self.root / "study", "long-run", self.source, raw, offset=0
        )
        first = telemetry.read_events(self.root / "study", "long-run")
        second = telemetry.read_events(
            self.root / "study", "long-run", after=first["next_offset"]
        )
        self.assertEqual([row["id"] for row in second["events"]], [valid["id"]])
        self.assertNotIn(fake["id"], [row["id"] for row in second["events"]])
        self.assertEqual(second["next_offset"], len(raw))
        self.assertEqual(
            Path(
                telemetry.info(self.root / "study", "long-run")["log_path"]
            ).read_bytes(),
            raw,
        )

    def test_tail_preview_selects_newest_matching_events_not_first_raw_lines(self):
        self.register()
        for step in range(300):
            self.event_bytes("loss", step / 100, step=step, tags=("dashboard",))
        raw = self.log.read_bytes()
        telemetry.append_chunk(
            self.root / "study", "long-run", self.source, raw, offset=0
        )
        shown = telemetry.snapshot(self.root / "study", tag="dashboard")["streams"][0]
        self.assertEqual(
            [row["step"] for row in shown["events"]], list(range(100, 300))
        )
        self.assertEqual(shown["omitted_events"], 100)
        self.assertEqual(shown["next_offset"], len(raw))
        self.assertEqual(shown["events"][-1]["end_offset"], len(raw))
        page = telemetry.read_events(self.root / "study", "long-run", tag="dashboard")
        self.assertEqual(page["events"][-1]["step"], 199)
        self.assertLess(page["next_offset"], len(raw))

    def test_internal_records_do_not_hide_matching_tail_and_errors_stay_bounded(self):
        self.register()
        visible, _ = self.event_bytes("visible", 1.0, tags=("dashboard",))
        for step in range(300):
            self.event_bytes("internal", step, step=step)
        raw = self.log.read_bytes() + b"\n" * 1000
        telemetry.append_chunk(
            self.root / "study", "long-run", self.source, raw, offset=0
        )
        shown = telemetry.snapshot(self.root / "study", tag="dashboard")["streams"][0]
        self.assertEqual([row["id"] for row in shown["events"]], [visible["id"]])
        self.assertEqual(len(shown["errors"]), 200)
        self.assertEqual(shown["omitted_errors"], 800)
        self.assertEqual(shown["next_offset"], len(raw))
        frozen = telemetry.freeze(
            self.root / "study", "0002", self.root / "bounded-prefix.json"
        )
        preview = frozen["streams"][0]["preview"]
        self.assertEqual(preview["events"][-1]["step"], 299)
        self.assertEqual(len(preview["errors"]), 200)
        self.assertEqual(preview["omitted_errors"], 800)

    def test_snapshot_tail_is_bounded_and_offsets_preserve_complete_records(self):
        self.register()
        _, raw = self.event_bytes("sample", "x" * 4000, tags=("dashboard",))
        content = raw * 40
        telemetry.append_chunk(
            self.root / "study", "long-run", self.source, content, offset=0
        )
        shown = telemetry.snapshot(self.root / "study", tag="dashboard")
        stream = shown["streams"][0]
        self.assertGreater(stream["preview_after"], 0)
        self.assertLessEqual(len(stream["events"]), 131072 // len(raw))
        self.assertEqual(stream["next_offset"], len(content))
        self.assertEqual(stream["errors"], [])
        frozen = telemetry.freeze(
            self.root / "study", "0002", self.root / "prefix.json"
        )
        self.assertEqual(frozen["streams"][0]["byte_end"], len(content))
        self.assertTrue(Path(frozen["streams"][0]["log_path"]).is_absolute())


if __name__ == "__main__":
    unittest.main()
