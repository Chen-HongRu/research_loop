"""Real local writers and bounded fake SSH, without allocation or GPU access."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from integrations.protok import telemetry as transport
from research_loop import telemetry as store


class LocalSSH:
    def __init__(self):
        self.calls = []
        self.disconnect = False

    def __call__(self, config, command, *, timeout):
        self.calls.append((command, timeout))
        if self.disconnect:
            raise OSError("Disconnected")
        return subprocess.check_output(
            shlex.split(command), text=True, timeout=timeout, stderr=subprocess.PIPE
        )


class LiveTelemetryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.remote = self.root / "events.jsonl"
        self.study = self.root / "study"
        self.source = {
            "kind": "research",
            "experiment_id": "0001",
            "source_identity": "source",
            "execution_identity": "execution",
        }
        self.config = {"remote_python": sys.executable}
        self.ssh = LocalSSH()

    def relay(self, cls=transport.RemoteRelay, **kwargs):
        return cls(
            self.study,
            "run",
            self.source,
            self.ssh,
            self.config,
            str(self.remote),
            **kwargs,
        )

    def events(self):
        return store.read_events(self.study, "run")["events"]

    def test_standalone_writer_arrives_before_process_terminal(self):
        data, identity = transport.writer_source()
        transport.stage_writer(self.root, data)
        self.assertEqual(len(identity), 64)
        environment = {**os.environ, "RESEARCH_LOOP_EVENT_FILE": str(self.remote)}
        code = "from research_loop_events import emit; import time; emit('train/loss',.5,tags=('dashboard',),step=10); time.sleep(.8); emit('train/loss',.4,step=20)"
        process = subprocess.Popen(
            [sys.executable, "-c", code], cwd=self.root, env=environment
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        relay = self.relay()
        background = transport.BackgroundRelay(relay.poll, interval=0.03)
        self.addCleanup(background.close)
        deadline = time.monotonic() + 5
        while not self.events() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNone(
            process.poll(), "first observation must be visible while writer runs"
        )
        self.assertEqual(self.events()[0]["step"], 10)
        process.wait(timeout=5)
        background.close()
        self.assertEqual([row["step"] for row in self.events()], [10, 20])
        self.assertEqual(
            Path(store.info(self.study, "run")["log_path"]).read_bytes(),
            self.remote.read_bytes(),
        )
        self.assertTrue(
            all(timeout == transport.SSH_TIMEOUT for _, timeout in self.ssh.calls)
        )

    def test_disconnect_resume_partial_tail_and_stale_cursor(self):
        store.emit("loss", 1, path=self.remote)
        relay = self.relay()
        self.assertTrue(relay.poll())
        first = self.remote.read_bytes()
        self.ssh.disconnect = True
        store.emit("loss", 2, path=self.remote)
        self.assertFalse(relay.poll())
        self.assertEqual(len(self.events()), 1)
        self.ssh.disconnect = False
        resumed = self.relay()
        self.assertTrue(resumed.poll())
        self.assertFalse(resumed.poll())
        self.assertEqual([event["value"] for event in self.events()], [1, 2])
        with self.remote.open("ab") as handle:
            handle.write(b'{"unfinished":')
        self.assertFalse(resumed.poll())
        self.assertEqual(
            store.info(self.study, "run")["byte_end"],
            len(first)
            + len(self.remote.read_bytes()[len(first) :].split(b'{"unfinished":')[0]),
        )
        with self.assertRaisesRegex(ValueError, "offset mismatch"):
            store.append_chunk(self.study, "run", self.source, first, offset=0)
        store.emit("loss", 3, path=self.remote)
        resumed.poll()
        self.assertEqual([event["value"] for event in self.events()], [1, 2, 3])
        self.assertEqual(len(store.read_events(self.study, "run")["errors"]), 1)

    def test_truncation_and_replacement_never_rewrite_prefix(self):
        store.emit("loss", 1, path=self.remote)
        relay = self.relay()
        relay.poll()
        retained = Path(store.info(self.study, "run")["log_path"]).read_bytes()
        self.remote.write_bytes(b"")
        self.assertFalse(relay.poll())
        self.assertEqual(store.info(self.study, "run")["status"], "integrity_error")
        store.emit("new", "x" * 500, path=self.remote)
        self.assertFalse(self.relay().poll())
        self.assertEqual(
            Path(store.info(self.study, "run")["log_path"]).read_bytes(), retained
        )
        # A separate stream demonstrates identity replacement even at equal size.
        second = transport.RemoteRelay(
            self.study, "other", self.source, self.ssh, self.config, str(self.remote)
        )
        second.poll()
        old = self.remote.with_suffix(".old")
        self.remote.rename(old)
        self.remote.write_bytes(old.read_bytes())
        self.assertFalse(second.poll())
        self.assertIn("identity changed", store.info(self.study, "other")["error"])

    def test_identity_mismatch_rejected_before_remote_read(self):
        self.relay()
        self.source = {**self.source, "execution_identity": "different"}
        with self.assertRaisesRegex(ValueError, "source identity mismatch"):
            self.relay()
        self.assertEqual(self.ssh.calls, [])

    def test_final_drain_covers_more_than_two_pages_and_marks_partial_tail(self):
        # Source bytes are arbitrary evidence; parsing is a separate store view.
        self.remote.write_bytes((b"x" * 999 + b"\n") * 700)
        relay = self.relay()
        relay.finish(time.monotonic() + 10)
        current = store.info(self.study, "run")
        self.assertEqual(current["byte_end"], self.remote.stat().st_size)
        self.assertEqual(current["status"], "synced")
        self.assertGreater(len(self.ssh.calls), 2)
        with self.remote.open("ab") as handle:
            handle.write(b"partial")
        relay.finish(time.monotonic() + 10)
        current = store.info(self.study, "run")
        self.assertEqual(current["status"], "sync_incomplete")
        self.assertGreater(current["remote_size"], current["byte_end"])

    def test_oversized_line_does_not_hide_later_valid_events(self):
        store.emit("loss", 1, path=self.remote)
        with self.remote.open("ab") as handle:
            handle.write(b"x" * (3 * transport.MAX_POLL_BYTES) + b"\n")
        store.emit("loss", 2, path=self.remote)
        relay = self.relay()
        relay.finish(time.monotonic() + 10)
        current = store.info(self.study, "run")
        self.assertEqual(
            Path(current["log_path"]).read_bytes(), self.remote.read_bytes()
        )
        cursor, values = 0, []
        while cursor < current["byte_end"]:
            result = store.read_events(self.study, "run", after=cursor)
            self.assertGreater(result["next_offset"], cursor)
            cursor = result["next_offset"]
            values.extend(event["value"] for event in result["events"])
        self.assertEqual(values, [1, 2])

    def test_native_csv_resume_retains_scope_step_and_unreviewed(self):
        self.remote.write_text(
            "step,optimizer_step,train/loss,train/aux\n10,10,.5,2\n20,20,.4,3"
        )
        relay = self.relay(transport.NativeCSVRelay, scope="training")
        relay.poll()
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0]["step"], 10)
        self.assertIn("unreviewed", self.events()[0]["tags"])
        self.assertEqual(self.events()[0]["context"]["scope"], "training")
        with self.remote.open("a") as handle:
            handle.write("\n")
        resumed = self.relay(transport.NativeCSVRelay, scope="training")
        resumed.poll()
        resumed.poll()
        self.assertEqual([event["step"] for event in self.events()], [10, 20])
        self.assertEqual(self.events()[-1]["value"]["train/loss"], 0.4)

    def test_native_csv_bad_rows_preserve_good_rows_and_recovery_offsets(self):
        header = b"optimizer_step,loss\n"
        prefix = header + b"10,1.0\nbad,row,extra\nno-step,0.7\n"
        complete = prefix + b"20,0.5\n"
        self.remote.write_bytes(complete + b"30,0.4")
        relay = self.relay(transport.NativeCSVRelay, scope="training")
        self.assertTrue(relay.poll())
        events = self.events()
        self.assertEqual([event["step"] for event in events], [10, None, None, 20])
        errors = events[1:3]
        self.assertTrue(all(event["name"] == "native/csv_error" for event in errors))
        self.assertTrue(all(isinstance(event["value"], str) for event in errors))
        self.assertTrue(all("dashboard" not in event["tags"] for event in errors))
        self.assertTrue(all("unreviewed" in event["tags"] for event in errors))
        self.assertEqual(
            errors[0]["context"]["remote_start_offset"], len(header + b"10,1.0\n")
        )
        self.assertEqual(errors[-1]["context"]["remote_end_offset"], len(prefix))
        resumed = self.relay(transport.NativeCSVRelay, scope="training")
        self.assertEqual(resumed.cursor(), len(complete))
        self.assertFalse(resumed.poll())
        with self.remote.open("ab") as handle:
            handle.write(b"\n40,0.3\n")
        self.assertTrue(resumed.poll())
        recovered = self.relay(transport.NativeCSVRelay, scope="training")
        self.assertFalse(recovered.poll())
        self.assertEqual(
            [event["step"] for event in self.events()], [10, None, None, 20, 30, 40]
        )
        self.assertEqual(recovered.cursor(), self.remote.stat().st_size)

    def test_native_csv_oversized_fragment_resumes_at_next_complete_row(self):
        self.remote.write_bytes(
            b"optimizer_step,loss\n10,1\n"
            + b"x" * (3 * transport.MAX_POLL_BYTES)
            + b"30,9\n20,.5\n"
        )
        relay = self.relay(transport.NativeCSVRelay, scope="training")
        self.assertTrue(relay.poll())
        self.assertTrue(relay._discard_csv_fragment)
        resumed = self.relay(transport.NativeCSVRelay, scope="training")
        self.assertTrue(resumed._discard_csv_fragment)
        for _ in range(5):
            if not resumed.poll():
                break
        events = self.events()
        metrics = [event for event in events if "dashboard" in event["tags"]]
        self.assertEqual([event["step"] for event in metrics], [10, 20])
        self.assertEqual(resumed.cursor(), self.remote.stat().st_size)
        self.assertTrue(
            all(
                len(json.dumps(event).encode()) < transport.MAX_LINE_BYTES
                for event in events
            )
        )
        self.assertFalse(self.relay(transport.NativeCSVRelay, scope="training").poll())

    def test_native_json_only_complete_scoped_marker_has_dashboard_tag(self):
        rows = [
            "compiler noise",
            json.dumps({"evaluation": {"optimizer_step": 1000, "ca_rmsd": 2}}),
            json.dumps(
                {
                    "evaluation": {
                        "optimizer_step": 1000,
                        "scope": "subset",
                        "proteins": 449,
                        "ca_rmsd": 2,
                    }
                }
            ),
        ]
        self.remote.write_text("\n".join(rows) + "\n")
        relay = self.relay(transport.NativeEvaluationRelay, scope="evaluation")
        relay.poll()
        visible = [event for event in self.events() if "dashboard" in event["tags"]]
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["context"]["scope"], "subset")
        self.assertEqual(visible[0]["step"], 1000)
        recovered = self.relay(transport.NativeEvaluationRelay, scope="evaluation")
        self.assertFalse(recovered.poll())
        self.assertEqual(len(self.events()), 3)

    def test_fixed_monitor_uses_owned_record_and_exact_iteration(self):
        record = self.root / "records/fingerprint"
        record.mkdir(parents=True)
        remote = self.root / "remote/job"
        (remote / "train").mkdir(parents=True)
        (remote / "train/train.csv").write_text("optimizer_step,train/loss\n10,.3\n")
        (remote / "run.log").write_text("noise\n")
        state = {
            "fingerprint": "fingerprint",
            "attempts": [{"owned_job_id": "123", "remote": str(remote), "seed": 42}],
        }
        (record / "state.json").write_text(json.dumps(state))
        monitor = transport.FixedMonitor(
            self.study,
            "0007",
            record,
            "fingerprint",
            {**self.config, "remote_root": str(self.root / "remote")},
            self.ssh,
        )
        self.assertTrue(monitor.poll())
        streams = store.snapshot(self.study)["streams"]
        self.assertEqual(
            {stream["source"]["experiment_id"] for stream in streams}, {"0007"}
        )
        self.assertTrue(
            all(stream["source"]["owned_job_id"] == "123" for stream in streams)
        )
        self.assertIsNone(
            transport.fixed_relay(
                self.root / "control",
                self.root / "ambiguous/source",
                "record",
                "identity",
                {},
            )
        )
        (record / "state.json").write_text(
            json.dumps({**state, "fingerprint": "other"})
        )
        with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
            monitor.poll()

    def test_fixed_factory_accepts_only_owned_source_layouts(self):
        control = self.root / "control"
        config = {"records_root": str(self.root / "records")}
        for relative in ("jobs/9999/source", "debug/9999/formal-001/source"):
            with (
                self.subTest(relative=relative),
                patch.object(transport, "BackgroundRelay") as factory,
            ):
                transport.fixed_relay(
                    control, control / relative, "record", "fingerprint", config
                )
                owner = factory.call_args.args[0].__self__
                self.assertEqual(owner.experiment_id, "9999")
                self.assertEqual(owner.root, (control.parent / "study").resolve())
        for workspace in (
            self.root / "elsewhere/9999/source",
            control / "debug/9999/revision/source",
            control / "jobs/not-numeric/source",
        ):
            with (
                self.subTest(workspace=workspace),
                patch.object(transport, "BackgroundRelay") as factory,
            ):
                self.assertIsNone(
                    transport.fixed_relay(
                        control, workspace, "record", "fingerprint", config
                    )
                )
                factory.assert_not_called()

    def test_pending_fixed_close_is_sticky_and_covers_unread_source(self):
        record = self.root / "records/fingerprint"
        record.mkdir(parents=True)
        remote = self.root / "remote/job"
        (remote / "train").mkdir(parents=True)
        (remote / "train/train.csv").write_text("optimizer_step,train/loss\n10,.3\n")
        (remote / "run.log").write_text("noise\n")
        (record / "state.json").write_text(
            json.dumps(
                {
                    "fingerprint": "fingerprint",
                    "attempts": [
                        {"owned_job_id": "123", "remote": str(remote), "seed": 42}
                    ],
                }
            )
        )
        entered, release = threading.Event(), threading.Event()

        def blocking_ssh(config, command, *, timeout):
            entered.set()
            release.wait(timeout=5)
            return self.ssh(config, command, timeout=timeout)

        monitor = transport.FixedMonitor(
            self.study,
            "0007",
            record,
            "fingerprint",
            {**self.config, "remote_root": str(self.root / "remote")},
            blocking_ssh,
        )
        background = transport.BackgroundRelay(monitor.poll)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(timeout=2))
        # Simulate a bounded join expiring while the first source is pending.
        with patch.object(background._thread, "join", return_value=None):
            background.close()
        before = store.snapshot(self.study)["streams"]
        self.assertEqual(len(before), 2, "the unread evaluation source must be visible")
        self.assertTrue(all(stream["status"] == "sync_incomplete" for stream in before))
        self.assertTrue(all(stream["byte_end"] == 0 for stream in before))
        frozen = store.freeze(self.study, "0007", self.root / "frozen.json")
        release.set()
        background._thread.join(timeout=2)
        self.assertFalse(background._thread.is_alive())
        after = store.snapshot(self.study)["streams"]
        self.assertEqual(
            [
                (stream["stream_id"], stream["status"], stream["byte_end"])
                for stream in after
            ],
            [
                (stream["stream_id"], stream["status"], stream["byte_end"])
                for stream in before
            ],
        )
        self.assertTrue(
            all(stream["status"] == "sync_incomplete" for stream in frozen["streams"])
        )
        self.assertEqual(
            len(self.ssh.calls), 1, "stop must prevent the second live SSH call"
        )

    def test_fixed_final_sources_share_one_deadline(self):
        monitor = transport.FixedMonitor(
            self.study, "0007", self.root / "record", "fingerprint", self.config
        )
        first, second = (
            self.relay(),
            transport.RemoteRelay(
                self.study,
                "second",
                self.source,
                self.ssh,
                self.config,
                str(self.remote),
            ),
        )
        deadline = time.monotonic() + 30
        with (
            patch.object(monitor, "_discover", return_value=(first, second)),
            patch.object(first, "finish") as one,
            patch.object(second, "finish") as two,
        ):
            monitor.finish(deadline)
        one.assert_called_once_with(deadline)
        two.assert_called_once_with(deadline)

    def test_partial_converted_append_preserves_fragment_and_replays_row_once(self):
        self.remote.write_text("optimizer_step,train/loss\n10,.5\n20,.4\n")
        relay = self.relay(transport.NativeCSVRelay, scope="training")
        original = store.append_chunk

        def append_partial_then_fail(root, stream_id, source, data, *, offset):
            first, second = data.splitlines(keepends=True)
            original(
                root,
                stream_id,
                source,
                first + second[: len(second) // 2],
                offset=offset,
            )
            raise OSError("partial append")

        with patch.object(store, "append_chunk", side_effect=append_partial_then_fail):
            self.assertFalse(relay.poll())
        current = store.info(self.study, "run")
        retained = Path(current["log_path"]).read_bytes()
        recovered = self.relay(transport.NativeCSVRelay, scope="training")
        self.assertEqual(recovered.offset, len("optimizer_step,train/loss\n10,.5\n"))
        recovered.poll()
        result = store.read_events(self.study, "run")
        self.assertEqual([event["step"] for event in result["events"]], [10, 20])
        self.assertEqual(len(result["errors"]), 1)
        self.assertTrue(
            Path(current["log_path"]).read_bytes().startswith(retained + b"\n")
        )
        self.assertFalse(recovered.poll())

    def test_complete_json_without_delimiter_is_confirmed_only_after_repair(self):
        self.remote.write_text("optimizer_step,train/loss\n10,.5\n20,.4\n")
        relay = self.relay(transport.NativeCSVRelay, scope="training")
        original = store.append_chunk

        def append_without_delimiter_then_fail(
            root, stream_id, source, data, *, offset
        ):
            original(root, stream_id, source, data[:-1], offset=offset)
            raise OSError("missing delimiter")

        with patch.object(
            store, "append_chunk", side_effect=append_without_delimiter_then_fail
        ):
            self.assertFalse(relay.poll())
        recovered = self.relay(transport.NativeCSVRelay, scope="training")
        self.assertEqual(recovered.offset, len("optimizer_step,train/loss\n10,.5\n"))
        recovered.poll()
        self.assertEqual([event["step"] for event in self.events()], [10, 20])
        self.assertEqual(store.read_events(self.study, "run")["errors"], [])

    def test_native_append_before_metadata_error_does_not_duplicate(self):
        self.remote.write_text("optimizer_step,train/loss\n10,.5\n")
        relay = self.relay(transport.NativeCSVRelay, scope="training")
        original = store.append_chunk

        def append_then_fail(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("metadata publication interrupted")

        with patch.object(store, "append_chunk", side_effect=append_then_fail):
            self.assertFalse(relay.poll())
        self.assertFalse(relay.poll())
        self.assertEqual(len(self.events()), 1)

    def test_receipt_only_drain_resumes_retained_stream(self):
        store.emit("loss", 1, path=self.remote)
        self.relay().poll()
        store.emit("loss", 2, path=self.remote)
        transport.drain_existing(
            self.study, "run", self.source, self.config, str(self.remote), self.ssh
        )
        self.assertEqual([event["value"] for event in self.events()], [1, 2])

    def test_transport_failure_does_not_escape_background(self):
        def poll():
            raise OSError("offline")

        background = transport.BackgroundRelay(poll, interval=0.02)
        background.close()
        with patch.object(
            transport, "RemoteRelay", side_effect=ValueError("bad metadata")
        ):
            self.assertIsNone(
                transport.job_relay(
                    self.study,
                    "run",
                    self.source,
                    self.ssh,
                    self.config,
                    str(self.remote),
                )
            )


if __name__ == "__main__":
    unittest.main()
