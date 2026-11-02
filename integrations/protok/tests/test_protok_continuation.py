"""Operator-only continuation hooks, using a tiny network-free backend."""

import fcntl
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from integrations.protok.continuation import (
    install_hooks,
    launch,
    validate_continuation,
)


BACKEND = """
import fcntl, json
from pathlib import Path

def load_config(path):
    return json.loads(Path(path).read_text())

def source_boundary(config):
    return {}

def fingerprint(config, candidates):
    return "owned"

def config_identity(config):
    return "same-score-identity"

def session_state(config):
    records = Path(config["records_root"])
    for name in ("protocol-amendment.json", "resource-amendment.json"):
        path = records / name
        if path.exists() and json.loads(path.read_text())["previous_config"]["records_root"] != str(records):
            raise RuntimeError("Configuration differs from the authorized amendment")
    return json.loads((records / "session.json").read_text())

def records_state(records):
    return [(p.parent, json.loads(p.read_text())) for p in records.glob("*/state.json")]

def save_state(record, state):
    (record / "state.json").write_text(json.dumps(state))

def used_gpu_seconds(records):
    total = 0
    for _, state in records_state(records):
        if not state.get("attempts"):
            total += state.get("gpu_seconds", 0)
        for attempt in state.get("attempts", []):
            if attempt.get("status") == "prepared":
                continue
            if attempt.get("gpu_seconds") is None or not attempt.get("terminal_state"):
                raise RuntimeError("Unreconciled allocation receipt")
            total += attempt["gpu_seconds"]
    return total

def duration(value):
    assert value == "00:30:00"
    return 1800

def budget_check(config, session, records, allocations, *, now=None, smoke=False):
    now = 1000 if now is None else now
    if session["deadline"] - now < allocations * 1800:
        raise RuntimeError("whole allocation does not fit")
    used = used_gpu_seconds(records)
    if used + allocations * 1800 + config["reserve_gpu_seconds"] > config["gpu_budget_seconds"]:
        raise RuntimeError("cumulative GPU budget exhausted")
    return used

def reserve_record(config, identity, baseline_identity, *, smoke=False):
    records = Path(config["records_root"])
    record = records / identity
    if record.exists():
        return record, json.loads((record / "state.json").read_text())
    if sum(s.get("mode") == "scored" for _, s in records_state(records)) >= config["max_evaluations"]:
        raise RuntimeError("count exhausted")
    record.mkdir()
    state = {"mode": "scored", "status": "reserved", "attempts": []}
    save_state(record, state)
    return record, state

def ssh(config, command, *, timeout=120):
    if globals().get("after_poll"):
        after_poll()
    return "RUNNING"

def wait_for_job(config, session, record, state, attempt):
    if globals().get("exercise_owned"):
        for _ in range(2):
            ssh(config, "squeue -h -j 42 -o %T")
        attempt.update(status="finished", terminal_state="COMPLETED", gpu_seconds=15)
        save_state(record, state)
    return session["deadline"]

def submit(config, record, state, attempt):
    if globals().get("exercise_owned"):
        globals()["submissions"] = globals().get("submissions", 0) + 1
        attempt.update(status="running", owned_job_id="42")
        save_state(record, state)
        if globals().get("after_submit"):
            after_submit()
    return "original-submit"

def cancel_interrupted(config, record, state):
    globals()["cancellations"] = globals().get("cancellations", 0) + 1
    ssh(config, "squeue -h -j 42 -o %T")
    state["attempts"][0]["cancel_requested"] = True
    save_state(record, state)

def main(argv):
    config = load_config(argv[1])
    with (Path(config["records_root"]) / ".eval.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if globals().get("exercise_owned"):
            record, state = reserve_record(config, "owned", "baseline")
            if not state["attempts"]:
                attempt = {"status": "prepared"}
                state["attempts"].append(attempt)
                submit(config, record, state, attempt)
            try:
                wait_for_job(config, session_state(config), record, state, state["attempts"][0])
            except KeyboardInterrupt:
                cancel_interrupted(config, record, state)
                state["status"] = "incomplete"
                save_state(record, state)
                return 1
        return 0
"""


class ContinuationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.project, self.control, self.records = [
            root / p for p in ("project", "control", "records")
        ]
        for p in (self.project / "scripts", self.control, self.records):
            p.mkdir(parents=True)
        backend = self.project / "scripts/autoresearch_se3_eval.py"
        backend.write_text(BACKEND)
        self.sha = hashlib.sha256(backend.read_bytes()).hexdigest()
        self.config = {
            "workspace": str(self.project),
            "records_root": str(self.records),
            "locked_files": {"scripts/autoresearch_se3_eval.py": self.sha},
            "max_evaluations": 12,
            "gpu_budget_seconds": 86400,
            "reserve_gpu_seconds": 3600,
            "time_limit": "00:30:00",
        }
        self.write(self.project / "autoresearch-run.json", self.config)
        self.runtime = root / "runtime.json"
        self.write(self.runtime, self.config | {"workspace": str(root / "isolated")})
        self.session = {"started_at": 0, "deadline": 86400}
        self.write(self.records / "session.json", self.session)
        for n in range(12):
            path = self.records / str(n)
            path.mkdir()
            self.write(
                path / "state.json",
                {
                    "status": "complete",
                    "mode": "scored",
                    "attempts": [],
                    "gpu_seconds": 11660 if n == 0 else 0,
                },
            )
        self.owner = {
            "schema": 1,
            "project": str(self.project),
            "owner_token": "opaque",
            "handoff_acknowledged": True,
            "acknowledged_at": 1,
            "expires_at": 22600,
            "previous_owner": "old-thread",
            "evidence": "ack.json",
        }
        self.cont = {
            "schema": 1,
            "project": str(self.project),
            "owner_token": "opaque",
            "authorization": "6h, no count cap",
            "started_at": 1000,
            "deadline": 22600,
            "seconds": 21600,
            "max_new_candidates": None,
            "original_session": self.session,
            "initial_scored_records": 12,
            "initial_gpu_seconds": 11660,
            "config_identity": "same-score-identity",
            "evaluator_sha256": self.sha,
            "previous_study": "closed-study.json",
            "prior_off": None,
            "prior_off_stat": None,
        }
        self.sync_control()

    def write(self, path, value):
        path.write_text(json.dumps(value))

    def sync_control(self):
        self.write(self.control / "ownership.json", self.owner)
        self.write(self.control / "continuation.json", self.cont)

    def validate(self):
        return validate_continuation(
            self.project, self.control, "opaque", self.runtime, now=1000
        )

    def hooked(self):
        backend, config, control = self.validate()
        install_hooks(backend, config, control, lambda: None)
        return backend, config

    def test_uncapped_new_records_keep_original_budget_and_history(self):
        before = {p: p.read_bytes() for p in self.records.glob("*/state.json")}
        session_before = (self.records / "session.json").read_bytes()
        backend, config = self.hooked()
        for n in range(13, 16):
            record, state = backend.reserve_record(config, f"new-{n}", "baseline")
            self.assertEqual(state["controller_lease"]["cumulative_scored_records"], n)
            state["status"] = "complete"
            backend.save_state(record, state)
        self.assertEqual(config["max_evaluations"], 12)
        self.assertEqual((self.records / "session.json").read_bytes(), session_before)
        for p, data in before.items():
            self.assertEqual(p.read_bytes(), data)

    def test_expired_window_allows_cached_receipt_but_not_new_record(self):
        self.owner["expires_at"] = 1001
        self.sync_control()
        backend, config = self.hooked()
        backend.reserve_record(config, "0", "baseline")
        with self.assertRaisesRegex(RuntimeError, "whole allocation"):
            backend.reserve_record(config, "new", "baseline")
        self.assertFalse((self.records / "new").exists())
        self.assertEqual(backend.wait_for_job(config, self.session, None, {}, {}), 1001)

    def test_same_owned_fingerprint_recovery_after_expiry(self):
        self.owner["expires_at"] = 1001
        self.sync_control()
        backend, config = self.hooked()
        active = {
            "mode": "scored",
            "status": "incomplete",
            "attempts": [{"status": "running", "owned_job_id": "42"}],
            "gpu_seconds": 11660,
        }
        self.write(self.records / "0/state.json", active)
        record, state = backend.reserve_record(config, "0", "baseline")
        self.assertEqual(state, active)
        self.assertEqual(
            backend.wait_for_job(
                config, self.session, record, state, active["attempts"][0]
            ),
            1001,
        )
        self.assertEqual(self.session["deadline"], 86400)

    def test_cumulative_gpu_budget_never_resets(self):
        backend, config = self.hooked()
        self.write(
            self.records / "0/state.json",
            {"status": "complete", "mode": "scored", "gpu_seconds": 83000},
        )
        with self.assertRaisesRegex(RuntimeError, "cumulative GPU"):
            backend.reserve_record(config, "new", "baseline")

    def test_reject_decreased_gpu_accounting(self):
        backend, config = self.hooked()
        self.write(
            self.records / "0/state.json",
            {"status": "complete", "mode": "scored", "gpu_seconds": 0},
        )
        with self.assertRaisesRegex(RuntimeError, "decreased"):
            backend.reserve_record(config, "new", "baseline")

    def test_outstanding_other_fingerprint_blocks_without_reservation(self):
        backend, config = self.hooked()
        self.write(
            self.records / "0/state.json", {"status": "incomplete", "mode": "scored"}
        )
        with self.assertRaisesRegex(RuntimeError, "outstanding"):
            backend.reserve_record(config, "new", "baseline")
        self.assertFalse((self.records / "new").exists())

    def test_lease_config_source_session_and_stop_guards(self):
        for mutation, message in [
            (lambda: self.owner.update(owner_token="other"), "ownership"),
            (lambda: self.cont.update(config_identity="other"), "identity"),
            (lambda: self.cont.update(evaluator_sha256="0" * 64), "SHA"),
            (lambda: self.cont.update(original_session={}), "session"),
        ]:
            owner, cont = dict(self.owner), dict(self.cont)
            mutation()
            self.sync_control()
            with self.assertRaisesRegex(ValueError, message):
                self.validate()
            self.owner, self.cont = owner, cont
        self.sync_control()
        self.write(self.runtime, self.config | {"records_root": "other"})
        with self.assertRaisesRegex(ValueError, "only"):
            self.validate()
        self.write(self.runtime, self.config)
        (self.control / "STOP").touch()
        with self.assertRaisesRegex(ValueError, "paused"):
            self.validate()

    def test_historical_off_preserved_but_replaced_marker_rejected(self):
        off = self.project / ".autoresearch-off"
        self.write(off, {"reason": "old exhausted"})
        stat = off.stat()
        self.cont.update(
            prior_off={"reason": "old exhausted"},
            prior_off_stat={
                key: getattr(stat, "st_" + key) for key in ("mtime_ns", "size", "ino")
            },
        )
        self.sync_control()
        self.validate()
        self.write(off, {"reason": "new stop"})
        with self.assertRaisesRegex(ValueError, "stop marker"):
            self.validate()

    def test_submit_rechecks_whole_allocation_after_preparation(self):
        backend, config = self.hooked()
        with patch.object(backend, "session_state", return_value={"deadline": 1001}):
            with self.assertRaisesRegex(RuntimeError, "whole allocation"):
                backend.submit(config, None, {}, {})

    def test_stop_after_submit_or_during_poll_cancels_owned_once(self):
        for when in ("after_submit", "after_poll"):
            with self.subTest(when=when):
                stop = self.control / "STOP"
                stop.unlink(missing_ok=True)
                owned = self.records / "owned"
                if owned.exists():
                    (owned / "state.json").unlink()
                    owned.rmdir()
                backend, config, control = self.validate()
                backend.exercise_owned = True
                setattr(backend, when, stop.touch)

                def validate():
                    if stop.exists():
                        raise ValueError("paused")

                install_hooks(backend, config, control, validate)
                self.assertEqual(backend.main(["--config", str(self.runtime)]), 1)
                state = json.loads((owned / "state.json").read_text())
                self.assertEqual(backend.submissions, 1)
                self.assertEqual(backend.cancellations, 1)
                self.assertTrue(state["attempts"][0]["cancel_requested"])
                self.assertEqual(state["attempts"][0]["owned_job_id"], "42")

    def test_stopped_receipt_recovery_only_matches_owned_fingerprint(self):
        owned = self.records / "owned"
        owned.mkdir()
        active = {
            "mode": "scored",
            "status": "incomplete",
            "controller_lease": {"owner_token": "opaque"},
            "attempts": [
                {"status": "running", "owned_job_id": "42", "cancel_requested": True}
            ],
        }
        self.write(owned / "state.json", active)
        (self.control / "STOP").touch()
        backend, config, control = validate_continuation(
            self.project, self.control, "opaque", self.runtime, now=1000, recover=True
        )
        backend.exercise_owned = True
        install_hooks(
            backend,
            config,
            control,
            lambda: None,
            validate_poll=lambda: (_ for _ in ()).throw(ValueError("paused")),
            recover=True,
        )
        self.assertEqual(backend.main(["--config", str(self.runtime)]), 0)
        self.assertFalse(hasattr(backend, "submissions"))
        state = json.loads((owned / "state.json").read_text())
        self.assertEqual(state["attempts"][0]["gpu_seconds"], 15)
        with self.assertRaisesRegex(ValueError, "cannot reserve"):
            backend.reserve_record(config, "new", "baseline")
        with self.assertRaisesRegex(ValueError, "cannot submit"):
            backend.submit(config, owned, state, {})
        active["controller_lease"]["owner_token"] = "other"
        self.write(owned / "state.json", active)
        with self.assertRaisesRegex(ValueError, "matching fingerprint"):
            validate_continuation(
                self.project,
                self.control,
                "opaque",
                self.runtime,
                now=1000,
                recover=True,
            )

    def test_command_diagnostics_are_scoped_to_successfully_reserved_record(self):
        backend, config = self.hooked()
        record, _ = backend.reserve_record(config, "owned", "baseline")
        self.assertEqual(backend.ssh(config, "echo ready"), "RUNNING")
        capture = backend.command_diagnostics.view()
        self.assertEqual(Path(capture["directory"]).name, record.name)
        self.assertEqual(Path(capture["directory"]).parent.name, "command-diagnostics")
        self.assertEqual(capture["log_count"], 2)
        self.assertEqual(capture["logs"][0]["tail"], "RUNNING")

    def test_full_log_fetch_survives_tail_collection_error(self):
        backend, config, control = self.validate()
        failure = OSError("tail unavailable")
        backend.collect_logs = lambda *args: (_ for _ in ()).throw(failure)
        install_hooks(backend, config, control, lambda: None)
        attempt = {
            "remote": "/owned/run",
            "terminal_state": "FAILED",
            "gpu_seconds": 12,
        }
        with patch("research_loop.diagnostics.fetch_remote_log") as fetch:
            with self.assertRaises(OSError) as caught:
                backend.collect_logs(config, self.records / "owned", attempt)
            self.assertIs(caught.exception, failure)
            fetch.assert_called_once_with(backend, config, "/owned/run")

    def test_original_main_enforces_global_lock(self):
        with (self.records / ".eval.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch("integrations.protok.continuation.time.time", return_value=1000):
                with self.assertRaises(BlockingIOError):
                    launch(self.project, self.control, "opaque", self.runtime)


class IncrementalContinuationTest(unittest.TestCase):
    write = ContinuationTest.write
    sync_control = ContinuationTest.sync_control
    hooked = ContinuationTest.hooked

    def setUp(self):
        ContinuationTest.setUp(self)
        self.anchor = {}
        for n in range(19):
            path = self.records / str(n)
            path.mkdir(exist_ok=True)
            seconds = 18961 if n == 0 else 0
            self.write(
                path / "state.json",
                self.receipt(seconds, mode="scored"),
            )
            self.anchor[str(n)] = seconds
        for name, mode, seconds in (
            ("old-smoke", "smoke", 123),
            ("import", "import", 0),
        ):
            path = self.records / name
            path.mkdir()
            self.write(path / "state.json", self.receipt(seconds, mode=mode))
            self.anchor[name] = seconds
        self.cont.update(
            schema=2,
            author="user-handoff-20261101",
            authorization="additional 86400 GPU seconds and at most 86400 wall seconds",
            started_at=100000,
            deadline=186400,
            seconds=86400,
            additional_gpu_seconds=86400,
            initial_scored_records=19,
            initial_gpu_seconds=19084,
            initial_record_gpu_seconds=self.anchor,
        )
        self.owner["expires_at"] = self.cont["deadline"]
        self.sync_control()
        clock = patch("integrations.protok.continuation.time.time", return_value=100000)
        clock.start()
        self.addCleanup(clock.stop)

    def receipt(self, seconds, *, mode="scored", owner=None):
        state = {
            "mode": mode,
            "status": "complete",
            "attempts": [
                {
                    "status": "complete",
                    "terminal_state": "COMPLETED",
                    "gpu_seconds": seconds,
                }
            ],
        }
        if owner is not None:
            state["controller_lease"] = {"owner_token": owner}
        return state

    def validate(self, *, now=100000, recover=False):
        return validate_continuation(
            self.project,
            self.control,
            "opaque",
            self.runtime,
            now=now,
            recover=recover,
        )

    def charge(self, seconds):
        path = self.records / "new-charge"
        path.mkdir(exist_ok=True)
        self.write(path / "state.json", self.receipt(seconds, owner="opaque"))

    def test_isolated_cli_loads_shared_research_accounting(self):
        # Production starts this file with -I from the control directory. An
        # in-process import would hide a missing entry-point package bootstrap.
        evaluator = self.project / "scripts/autoresearch_se3_eval.py"
        evaluator.write_text(
            BACKEND
            + """
def main(argv):
    config = load_config(argv[1])
    used = budget_check(config, session_state(config), Path(config["records_root"]), 1)
    print(f"ADMITTED_GPU_SECONDS={used}")
    return 0
"""
        )
        self.sha = hashlib.sha256(evaluator.read_bytes()).hexdigest()
        self.config["locked_files"]["scripts/autoresearch_se3_eval.py"] = self.sha
        self.write(self.project / "autoresearch-run.json", self.config)
        self.write(self.runtime, self.config)
        now = datetime.now(timezone.utc).timestamp()
        self.cont.update(
            started_at=now - 10, deadline=now - 10 + 86400, evaluator_sha256=self.sha
        )
        self.owner["expires_at"] = self.cont["deadline"]
        self.sync_control()
        research = self.control / "research-jobs/0001"
        research.mkdir(parents=True)
        self.write(
            research / "job.json",
            {"allocated_seconds": 60, "source_identity": "frozen-research"},
        )
        self.write(
            research / "receipt.json",
            {
                "purpose": "research",
                "quality_score_eligible": False,
                "status": "completed",
                "gpu_seconds": 37,
                "source_identity": "frozen-research",
            },
        )
        entrypoint = Path(__file__).resolve().parents[1] / "continuation.py"
        command = [
            sys.executable,
            "-B",
            "-I",
            str(entrypoint),
            "--project",
            str(self.project),
            "--control",
            str(self.control),
            "--owner-token",
            "opaque",
            "--config",
            str(self.runtime),
            "--evaluate",
        ]
        result = subprocess.run(
            command, cwd=self.control, capture_output=True, text=True, timeout=15
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "ADMITTED_GPU_SECONDS=19121")
        # Unknown research effects must still block this same real entry point.
        (research / "receipt.json").unlink()
        blocked = subprocess.run(
            command, cwd=self.control, capture_output=True, text=True, timeout=15
        )
        self.assertEqual(blocked.returncode, 1)
        self.assertNotIn("ADMITTED_GPU_SECONDS", blocked.stdout)
        self.assertIn("RuntimeError", blocked.stdout)

    def test_expired_original_session_is_preserved_with_new_deadline(self):
        historical = {p: p.read_bytes() for p in self.records.glob("*/state.json")}
        session = (self.records / "session.json").read_bytes()
        backend, config = self.hooked()
        record, state = backend.reserve_record(config, "new", "baseline")
        self.assertEqual(state["controller_lease"]["cumulative_scored_records"], 20)
        self.assertEqual(
            backend.wait_for_job(config, self.session, record, state, {}), 186400
        )
        self.assertEqual(config["gpu_budget_seconds"], 86400)
        self.assertEqual(config["max_evaluations"], 12)
        self.assertEqual((self.records / "session.json").read_bytes(), session)
        for path, before in historical.items():
            self.assertEqual(path.read_bytes(), before)

    def test_committed_round_crosses_cutoff_with_same_gpu_budget_and_wait(self):
        from integrations.protok.round_admission import (
            admit,
            commit,
            freeze_execution_deadline,
        )

        root = self.control.parent
        token = admit(root, "0001", owner_token="opaque")
        committed_at = commit(root, "0001", token, owner_token="opaque")
        metadata = root / "study/iterations/0001/iteration.json"
        metadata.parent.mkdir(parents=True)
        self.write(metadata, {"round_admission": token})
        self.write(
            root / "study/state.json",
            {
                "iteration": 1,
                "phase": "execute",
                "status": "ready",
                "round_admission": token,
                "round_admitted_at": committed_at,
            },
        )
        original_session = (self.records / "session.json").read_text()
        original_control = (self.control / "continuation.json").read_text()
        now = self.cont["deadline"] + 1
        with patch("integrations.protok.continuation.time.time", return_value=now):
            freeze_execution_deadline(
                root, now + 1800, owner_token="opaque", attempt="formal/owned"
            )
            backend, config, control = validate_continuation(
                self.project,
                self.control,
                "opaque",
                self.runtime,
                execution_deadline=now + 1800,
            )
            install_hooks(
                backend, config, control, lambda: None, control_dir=self.control
            )
            used = backend.budget_check(
                config, backend.session_state(config), self.records, 1
            )
            self.assertEqual(used, self.cont["initial_gpu_seconds"])
            self.assertEqual(
                backend.wait_for_job(
                    config, backend.session_state(config), None, {}, {}
                ),
                now + 1800,
            )
            self.charge(81001)
            with self.assertRaisesRegex(RuntimeError, "incremental GPU"):
                backend.budget_check(
                    config, backend.session_state(config), self.records, 1
                )
        self.assertEqual((self.records / "session.json").read_text(), original_session)
        self.assertEqual(
            (self.control / "continuation.json").read_text(), original_control
        )

    def test_formal_cli_deadline_is_bound_to_exact_attempt_and_token(self):
        from integrations.protok.round_admission import (
            admit,
            commit,
            freeze_execution_deadline,
        )

        root = self.control.parent
        token = admit(root, "0001", owner_token="opaque")
        committed_at = commit(root, "0001", token, owner_token="opaque")
        metadata = root / "study/iterations/0001/iteration.json"
        metadata.parent.mkdir(parents=True)
        self.write(metadata, {"round_admission": token})
        self.write(
            root / "study/state.json",
            {
                "iteration": 1,
                "phase": "execute",
                "status": "ready",
                "round_admission": token,
                "round_admitted_at": committed_at,
            },
        )
        window = freeze_execution_deadline(
            root, 101800, owner_token="opaque", attempt="formal/owned"
        )

        def validate(**kwargs):
            return validate_continuation(
                self.project,
                self.control,
                "opaque",
                self.runtime,
                execution_deadline=window,
                **kwargs,
            )

        self.assertEqual(validate()[2]["effective_deadline"], window)
        with self.assertRaisesRegex(ValueError, "differs from its frozen"):
            validate_continuation(
                self.project,
                self.control,
                "opaque",
                self.runtime,
                execution_deadline=window + 1,
            )
        with self.assertRaisesRegex(ValueError, "frozen attempt window"):
            validate(fresh_attempt="another")
        window_path = next((root / "control/round-admissions/0001").glob("*.json"))
        saved = json.loads(window_path.read_text())
        self.write(window_path, saved | {"attempt": "formal/wrong"})
        with self.assertRaisesRegex(ValueError, "frozen attempt window"):
            validate()
        self.write(window_path, saved | {"token": "another-round"})
        with self.assertRaisesRegex(ValueError, "frozen attempt window"):
            validate()
        self.write(window_path, saved)
        owned = self.records / "owned"
        owned.mkdir()
        self.write(
            owned / "state.json",
            {
                "status": "incomplete",
                "mode": "scored",
                "controller_lease": {"owner_token": "opaque"},
                "attempts": [{"status": "running", "owned_job_id": "42"}],
            },
        )
        with patch("integrations.protok.continuation.time.time", return_value=102000):
            self.assertEqual(validate(recover=True)[2]["effective_deadline"], window)
        # The legacy CLI without an override retains its original lease deadline.
        self.assertEqual(
            self.validate()[2]["effective_deadline"], self.cont["deadline"]
        )

    def test_incremental_budget_crosses_original_cap_but_retains_reserve(self):
        backend, config = self.hooked()
        self.charge(81000)
        self.assertEqual(
            backend.budget_check(config, self.session, self.records, 1), 100084
        )
        self.charge(81001)
        with self.assertRaisesRegex(RuntimeError, "incremental GPU"):
            backend.submit(config, None, {}, {})
        self.assertEqual(config["gpu_budget_seconds"], 86400)

    def test_whole_allocation_and_lease_deadline(self):
        self.owner["expires_at"] = 101800
        self.sync_control()
        backend, config = self.hooked()
        self.assertEqual(
            backend.wait_for_job(config, self.session, None, {}, {}), 101800
        )
        backend.budget_check(config, self.session, self.records, 1, now=100000)
        with self.assertRaisesRegex(RuntimeError, "whole allocation"):
            backend.budget_check(config, self.session, self.records, 1, now=100001)
        with self.assertRaisesRegex(RuntimeError, "whole allocation"):
            backend.budget_check(config, self.session, self.records, 2, now=100000)
        with self.assertRaisesRegex(ValueError, "expired"):
            self.validate(now=101800)

    def test_reject_invalid_grants_and_windows(self):
        for field, value in [
            ("author", ""),
            ("additional_gpu_seconds", None),
            ("additional_gpu_seconds", True),
            ("additional_gpu_seconds", float("nan")),
            ("additional_gpu_seconds", float("inf")),
            ("additional_gpu_seconds", 86401),
            ("additional_gpu_seconds", 0),
            ("seconds", 21600),
            ("deadline", 186401),
            ("deadline", float("inf")),
            ("started_at", float("nan")),
            ("max_new_candidates", 1),
        ]:
            with self.subTest(field=field, value=value):
                original = self.cont[field]
                self.cont[field] = value
                self.sync_control()
                with self.assertRaises(ValueError):
                    self.validate()
                self.cont[field] = original
        del self.cont["additional_gpu_seconds"]
        self.sync_control()
        with self.assertRaises(ValueError):
            self.validate()

    def test_anchor_includes_all_modes_and_rejects_omissions_or_forgery(self):
        self.validate()
        for field, value in [
            ("initial_scored_records", 21),
            ("initial_gpu_seconds", 19085),
            ("initial_gpu_seconds", float("nan")),
            ("initial_record_gpu_seconds", self.anchor | {"missing": 0}),
            ("initial_record_gpu_seconds", self.anchor | {"0": 18960, "1": 1}),
            (
                "initial_record_gpu_seconds",
                {k: v for k, v in self.anchor.items() if k != "import"},
            ),
            ("initial_record_gpu_seconds", self.anchor | {"0": float("inf")}),
        ]:
            with self.subTest(field=field):
                original = self.cont[field]
                self.cont[field] = value
                self.sync_control()
                with self.assertRaises(ValueError):
                    self.validate()
                self.cont[field] = original
        self.sync_control()
        (self.records / "import/state.json").unlink()
        with self.assertRaisesRegex(ValueError, "record anchor"):
            self.validate()

    def test_anchor_and_runtime_accounting_cannot_decrease(self):
        backend, config = self.hooked()
        self.charge(100)
        backend.budget_check(config, self.session, self.records, 1)
        self.charge(99)
        with self.assertRaisesRegex(RuntimeError, "decreased"):
            backend.budget_check(config, self.session, self.records, 1)
        self.write(self.records / "0/state.json", self.receipt(18960))
        with self.assertRaisesRegex(ValueError, "decreased"):
            self.validate()

    def test_unanchored_records_require_current_owner_and_reconciled_cost(self):
        path = self.records / "alien"
        path.mkdir()
        self.write(path / "state.json", self.receipt(0, owner="previous-owner"))
        with self.assertRaisesRegex(ValueError, "another continuation"):
            self.validate()
        state = self.receipt(0, owner="opaque")
        state["attempts"][0] = {"status": "submitting"}
        self.write(path / "state.json", state)
        backend, config = self.hooked()
        with self.assertRaisesRegex(RuntimeError, "Unreconciled"):
            backend.budget_check(config, self.session, self.records, 1)
        with self.assertRaisesRegex(RuntimeError, "outstanding"):
            backend.reserve_record(config, "new", "baseline")

    def test_v2_keeps_source_config_session_and_stop_guards(self):
        for field, value in [
            ("original_session", self.session | {"deadline": 100000}),
            ("evaluator_sha256", "0" * 64),
            ("config_identity", "other"),
        ]:
            original = self.cont[field]
            self.cont[field] = value
            self.sync_control()
            with self.assertRaises(ValueError):
                self.validate()
            self.cont[field] = original
        self.sync_control()
        for field in ("records_root", "ssh_host", "gpu_budget_seconds", "scorer"):
            self.write(self.runtime, self.config | {field: "escape"})
            with self.assertRaisesRegex(ValueError, "only"):
                self.validate()
        self.write(self.runtime, self.config)
        (self.control / "STOP").touch()
        with self.assertRaisesRegex(ValueError, "paused"):
            self.validate()

    def test_stop_during_poll_and_expired_owned_recovery(self):
        backend, config, control = self.validate()
        stop = self.control / "STOP"
        backend.exercise_owned = True
        backend.after_poll = stop.touch

        def validate():
            self.validate()

        install_hooks(backend, config, control, validate)
        self.assertEqual(backend.main(["--config", str(self.runtime)]), 1)
        self.assertEqual(backend.submissions, 1)
        self.assertEqual(backend.cancellations, 1)
        backend, config, control = self.validate(now=186401, recover=True)
        backend.exercise_owned = True
        install_hooks(
            backend,
            config,
            control,
            lambda: None,
            validate_poll=lambda: (_ for _ in ()).throw(ValueError("expired")),
            recover=True,
        )
        self.assertEqual(backend.main(["--config", str(self.runtime)]), 0)
        self.assertFalse(hasattr(backend, "submissions"))
        with self.assertRaisesRegex(ValueError, "cannot submit"):
            backend.submit(config, self.records / "owned", {}, {})
        with self.assertRaisesRegex(ValueError, "cannot reserve"):
            backend.reserve_record(config, "new", "baseline")

    def test_expired_unmarked_recovery_accounts_terminal_or_cancels_owned_active(self):
        for terminal, initially_active, now in (
            ("COMPLETED", False, 186401),
            ("CANCELLED", False, 186401),
            ("CANCELLED", True, 186401),
            ("CANCELLED", True, 100000),
        ):
            with self.subTest(
                terminal=terminal, initially_active=initially_active, now=now
            ):
                record = self.records / "owned"
                record.mkdir(exist_ok=True)
                state = {
                    "mode": "scored",
                    "status": "incomplete",
                    "controller_lease": {"owner_token": "opaque"},
                    "attempts": [{"status": "running", "owned_job_id": "42"}],
                }
                self.write(record / "state.json", state)
                (self.control / "STOP").touch()
                backend, config, control = self.validate(now=now, recover=True)
                commands = []

                def transport(current, command, *, timeout=120):
                    commands.append(command)
                    if command == "squeue -h -j 42 -o %T":
                        return (
                            "RUNNING" if initially_active and len(commands) == 1 else ""
                        )
                    if command == "scancel 42":
                        return ""
                    if command == "sacct -j 42":
                        return f"42|{terminal}|0:0|77"
                    self.fail(f"Unexpected recovery transport command: {command}")

                # Preserve the pinned backend's queue-first wait semantics in this
                # tiny fixture, including charging failed terminal allocations.
                def receipt_wait(current, session, record, state, attempt):
                    for _ in range(2):
                        active = backend.ssh(current, "squeue -h -j 42 -o %T")
                        if not active:
                            _, terminal, exit_code, seconds = backend.ssh(
                                current, "sacct -j 42"
                            ).split("|")
                            attempt.update(
                                terminal_state=terminal,
                                exit_code=exit_code,
                                gpu_seconds=int(seconds),
                                status="finished"
                                if terminal == "COMPLETED"
                                else "failed",
                            )
                            backend.save_state(record, state)
                            if terminal != "COMPLETED":
                                raise RuntimeError("Owned allocation cancelled")
                            return
                        self.assertLessEqual(session["deadline"], now)
                        if not attempt.get("cancel_requested"):
                            backend.ssh(current, "scancel " + attempt["owned_job_id"])
                            attempt["cancel_requested"] = True
                            backend.save_state(record, state)
                    self.fail("Receipt recovery failed to reach terminal accounting")

                backend.ssh = transport
                backend.wait_for_job = receipt_wait
                install_hooks(
                    backend,
                    config,
                    control,
                    lambda: self.validate(now=now, recover=True),
                    validate_poll=lambda: self.validate(now=now),
                    recover=True,
                )
                with self.assertRaisesRegex(ValueError, "cannot reserve"):
                    backend.reserve_record(config, "new", "baseline")
                with patch(
                    "integrations.protok.continuation.time.time", return_value=now
                ):
                    if terminal == "COMPLETED":
                        backend.wait_for_job(
                            config, self.session, record, state, state["attempts"][0]
                        )
                    else:
                        with self.assertRaisesRegex(RuntimeError, "cancelled"):
                            backend.wait_for_job(
                                config,
                                self.session,
                                record,
                                state,
                                state["attempts"][0],
                            )
                saved = json.loads((record / "state.json").read_text())
                self.assertEqual(saved["attempts"][0]["terminal_state"], terminal)
                self.assertEqual(backend.used_gpu_seconds(self.records), 19084 + 77)
                self.assertEqual(commands[0], "squeue -h -j 42 -o %T")
                self.assertEqual(commands[-1], "sacct -j 42")
                self.assertEqual(commands.count("scancel 42"), int(initially_active))
                self.assertFalse(hasattr(backend, "submissions"))
                with self.assertRaisesRegex(ValueError, "cannot submit"):
                    backend.submit(config, record, saved, {})

    def relocate(self):
        previous = "/Users/old-host/original-evaluations"
        self.cont["relocation"] = {
            "previous_records_root": previous,
            "current_records_root": str(self.records),
        }
        self.session.update(
            protocol="old-legacy-protocol",
            config_fingerprint="preserved-old-budget-identity",
            formal_config_fingerprint="preserved-old-formal-identity",
        )
        self.write(self.records / "session.json", self.session)
        for name in ("protocol-amendment.json", "resource-amendment.json"):
            self.write(
                self.records / name,
                {
                    "original_session": self.session,
                    "previous_config": {"records_root": previous},
                    "approved_config_fingerprint": self.cont["config_identity"],
                },
            )
        self.sync_control()

    def test_relocated_legacy_session_uses_readonly_memory_deadline(self):
        self.relocate()
        names = ("session.json", "protocol-amendment.json", "resource-amendment.json")
        historical = {name: (self.records / name).read_bytes() for name in names}
        backend, config, control = self.validate()
        with self.assertRaisesRegex(RuntimeError, "amendment"):
            backend.session_state(config)
        install_hooks(backend, config, control, lambda: None)
        self.assertEqual(
            backend.session_state(config), self.session | {"deadline": 186400}
        )
        backend.exercise_owned = True
        self.assertEqual(backend.main(["--config", str(self.runtime)]), 0)
        self.assertEqual(backend.submissions, 1)
        with self.assertRaisesRegex(ValueError, "cannot create"):
            backend.session_state(config, create=True)
        with self.assertRaisesRegex(ValueError, "cannot create"):
            backend.session_state(config, started_at=100000)
        for name, before in historical.items():
            self.assertEqual((self.records / name).read_bytes(), before)
        self.write(self.records / "session.json", self.session | {"deadline": 186400})
        with self.assertRaisesRegex(ValueError, "session changed"):
            backend.session_state(config)

    def test_relocation_requires_matching_explicit_roots_and_amendments(self):
        self.relocate()
        declaration = self.cont.pop("relocation")
        self.sync_control()
        with self.assertRaisesRegex(RuntimeError, "amendment"):
            self.validate()
        for value in [
            {},
            declaration | {"previous_records_root": "relative"},
            declaration | {"previous_records_root": str(self.records)},
            declaration | {"previous_records_root": "/other-records"},
            declaration | {"current_records_root": "/other-records"},
            declaration | {"ignore_identity": True},
        ]:
            self.cont["relocation"] = value
            self.sync_control()
            with self.assertRaises(ValueError):
                self.validate()
        self.cont["relocation"] = declaration
        self.sync_control()
        for name in ("protocol-amendment.json", "resource-amendment.json"):
            path = self.records / name
            amendment = json.loads(path.read_text())
            self.write(path, amendment | {"original_session": {}})
            with self.assertRaisesRegex(ValueError, "amendment metadata"):
                self.validate()
            self.write(path, amendment)
        path = self.records / "protocol-amendment.json"
        amendment = json.loads(path.read_text())
        self.write(path, amendment | {"approved_config_fingerprint": "different"})
        with self.assertRaisesRegex(ValueError, "amendment metadata"):
            self.validate()


class FreshContinuationTest(unittest.TestCase):
    write = ContinuationTest.write
    sync_control = ContinuationTest.sync_control
    receipt = IncrementalContinuationTest.receipt

    def setUp(self):
        IncrementalContinuationTest.setUp(self)
        baseline = self.records / "baseline"
        baseline.mkdir()
        self.write(baseline / "state.json", self.receipt(0, owner="opaque"))

    def validate(self, *, fresh_attempt="postdebug", now=100000, recover=False):
        return validate_continuation(
            self.project,
            self.control,
            "opaque",
            self.runtime,
            now=now,
            recover=recover,
            fresh_attempt=fresh_attempt,
        )

    def hooked(self, *, fresh_attempt="postdebug", recover=False):
        backend, config, control = self.validate(
            fresh_attempt=fresh_attempt, recover=recover
        )
        install_hooks(
            backend,
            config,
            control,
            lambda: None,
            recover=recover,
            fresh_attempt=fresh_attempt,
        )
        return backend, config

    def test_fresh_allocation_preserves_original_complete_and_failed_records(self):
        for status in ("complete", "failed"):
            with self.subTest(original_status=status):
                original = self.records / "owned"
                original.mkdir(exist_ok=True)
                state = self.receipt(80, owner="opaque")
                state.update(status=status, fingerprint="owned")
                state["attempts"][0]["status"] = status
                self.write(original / "state.json", state)
                before = {
                    path: path.read_bytes()
                    for path in self.records.glob("*/state.json")
                }
                backend, config = self.hooked(fresh_attempt=status)
                backend.exercise_owned = True
                self.assertEqual(backend.main(["--config", str(self.runtime)]), 0)
                self.assertEqual(backend.main(["--config", str(self.runtime)]), 0)
                fresh = self.records / f"owned--{status}"
                saved = json.loads((fresh / "state.json").read_text())
                self.assertEqual(saved["fingerprint"], "owned")
                self.assertEqual(saved["fresh_attempt"], status)
                self.assertIs(saved["baseline"], False)
                self.assertEqual(saved["mode"], "scored")
                self.assertEqual(saved["controller_lease"]["owner_token"], "opaque")
                self.assertEqual(len(saved["attempts"]), 1)
                self.assertEqual(backend.submissions, 1)
                for path, content in before.items():
                    self.assertEqual(path.read_bytes(), content)
                saved["status"] = "complete"
                saved["attempts"][0]["status"] = "complete"
                self.write(fresh / "state.json", saved)

    def test_fresh_identity_equal_to_baseline_still_is_scored_nonbaseline(self):
        backend, config = self.hooked()
        record, state = backend.reserve_record(config, "baseline", "baseline")
        self.assertEqual(record.name, "baseline--postdebug")
        self.assertEqual(state["fingerprint"], "baseline")
        self.assertIs(state["baseline"], False)
        self.assertEqual(state["mode"], "scored")

    def test_failed_fresh_id_stays_failed_without_another_attempt(self):
        backend, config = self.hooked()
        record, state = backend.reserve_record(config, "owned", "baseline")
        state.update(
            status="failed",
            attempts=[
                {"status": "failed", "gpu_seconds": 7, "terminal_state": "FAILED"}
            ],
        )
        backend.save_state(record, state)
        before = (record / "state.json").read_bytes()
        restarted, config = self.hooked()
        same, preserved = restarted.reserve_record(config, "owned", "baseline")
        self.assertEqual(same, record)
        self.assertEqual(preserved["status"], "failed")
        self.assertEqual(len(preserved["attempts"]), 1)
        self.assertEqual((record / "state.json").read_bytes(), before)
        self.assertFalse(hasattr(restarted, "submissions"))

    def test_fresh_admission_needs_full_wall_and_global_formal_budget(self):
        for seconds, charged, error in (
            (1799, 0, "whole allocation"),
            (1800, 81001, "incremental GPU"),
        ):
            with self.subTest(seconds=seconds, charged=charged):
                self.owner["expires_at"] = 100000 + seconds
                self.sync_control()
                extra = self.records / "extra"
                extra.mkdir(exist_ok=True)
                self.write(extra / "state.json", self.receipt(charged, owner="opaque"))
                backend, config = self.hooked()
                with self.assertRaisesRegex(RuntimeError, error):
                    backend.reserve_record(config, "owned", "baseline")
                self.assertFalse((self.records / "owned--postdebug").exists())
        self.owner["expires_at"] = 101800
        self.sync_control()
        self.write(extra / "state.json", self.receipt(81000, owner="opaque"))
        backend, config = self.hooked()
        record, state = backend.reserve_record(config, "owned", "baseline")
        self.assertEqual(record.parent, self.records)
        self.assertEqual(state["mode"], "scored")
        with self.assertRaisesRegex(ValueError, "expired"):
            self.validate(now=101800)

    def test_fresh_charge_counts_toward_subsequent_formal_allocations(self):
        backend, config = self.hooked()
        record, state = backend.reserve_record(config, "owned", "baseline")
        state.update(
            status="complete",
            attempts=[
                {
                    "status": "complete",
                    "terminal_state": "COMPLETED",
                    "gpu_seconds": 1800,
                }
            ],
        )
        backend.save_state(record, state)
        self.assertEqual(backend.used_gpu_seconds(self.records), 19084 + 1800)
        extra = self.records / "extra"
        extra.mkdir()
        self.write(extra / "state.json", self.receipt(79201, owner="opaque"))
        restarted, config = self.hooked(fresh_attempt="another")
        with self.assertRaisesRegex(RuntimeError, "incremental GPU"):
            restarted.reserve_record(config, "owned", "baseline")
        self.assertFalse((self.records / "owned--another").exists())

    def test_fresh_requires_baseline_and_excludes_other_outstanding_records(self):
        baseline = self.records / "baseline/state.json"
        self.write(baseline, self.receipt(0, owner="opaque") | {"status": "failed"})
        backend, config = self.hooked()
        with self.assertRaisesRegex(RuntimeError, "baseline must complete"):
            backend.reserve_record(config, "owned", "baseline")
        self.write(baseline, self.receipt(0, owner="opaque"))
        original = self.records / "owned"
        original.mkdir()
        self.write(
            original / "state.json",
            self.receipt(0, owner="opaque") | {"status": "incomplete"},
        )
        with self.assertRaisesRegex(RuntimeError, "outstanding"):
            backend.reserve_record(config, "owned", "baseline")
        self.assertFalse((self.records / "owned--postdebug").exists())

    def test_recovery_uses_only_matching_custom_path_and_never_submits_again(self):
        backend, config = self.hooked()
        record, state = backend.reserve_record(config, "owned", "baseline")
        state.update(status="incomplete", attempts=[{"status": "submitting"}])
        backend.save_state(record, state)
        (self.control / "STOP").touch()
        backend, config, control = self.validate(recover=True, now=186401)
        backend.exercise_owned = True
        install_hooks(
            backend,
            config,
            control,
            lambda: None,
            recover=True,
            fresh_attempt="postdebug",
        )
        self.assertEqual(backend.main(["--config", str(self.runtime)]), 0)
        self.assertFalse(hasattr(backend, "submissions"))
        self.assertEqual(
            len(json.loads((record / "state.json").read_text())["attempts"]), 1
        )
        for wrong in (None, "other"):
            with (
                self.subTest(wrong=wrong),
                self.assertRaisesRegex(ValueError, "Recovery requires"),
            ):
                self.validate(recover=True, fresh_attempt=wrong, now=186401)
        with self.assertRaisesRegex(ValueError, "cannot submit"):
            backend.submit(config, record, state, state["attempts"][0])

    def test_custom_identity_and_owner_tampering_rejected(self):
        backend, config = self.hooked()
        record, state = backend.reserve_record(config, "owned", "baseline")
        for changes in (
            {"fingerprint": "different"},
            {"fresh_attempt": "different"},
            {"baseline": True},
            {"mode": "import"},
            {"controller_lease": {"owner_token": "other"}},
        ):
            with self.subTest(changes=changes):
                backend.save_state(record, state | changes)
                with self.assertRaises(ValueError):
                    self.validate()
        backend.save_state(record, state)
        with self.assertRaisesRegex(ValueError, "Recovery requires"):
            self.validate(recover=True)

    def test_safe_ids_and_launcher_forward_fresh_namespace_at_every_check(self):
        for bad in ("", "../record", "a/b", ".", "x" * 65, 3):
            with (
                self.subTest(bad=bad),
                self.assertRaisesRegex(ValueError, "safe explicit"),
            ):
                self.validate(fresh_attempt=bad)
        backend, config, control = self.validate()

        def exercise_checks(backend, config, control, validate, **options):
            self.assertEqual(options["fresh_attempt"], "postdebug")
            validate()
            options["validate_poll"]()

        with (
            patch(
                "integrations.protok.continuation.validate_continuation",
                return_value=(backend, config, control),
            ) as validate,
            patch(
                "integrations.protok.continuation.install_hooks",
                side_effect=exercise_checks,
            ),
        ):
            self.assertEqual(
                launch(
                    self.project,
                    self.control,
                    "opaque",
                    self.runtime,
                    fresh_attempt="postdebug",
                ),
                0,
            )
        self.assertEqual(validate.call_count, 3)
        for call in validate.call_args_list:
            self.assertEqual(call.kwargs["fresh_attempt"], "postdebug")


if __name__ == "__main__":
    unittest.main()
