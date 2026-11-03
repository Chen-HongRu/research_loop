import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from integrations.protok.backend import (
    CANDIDATE_FILES,
    EVALUATOR,
    METRIC,
    PROTOCOL,
    ProTokBackend,
    ProTokError,
)


STUB = """import hashlib, json, math, pathlib, sys
def load_config(path):
    return json.loads(pathlib.Path(path).read_text())
def session_state(config):
    return json.loads((pathlib.Path(config['records_root'])/'session.json').read_text())
def records_state(records):
    return [(p.parent, json.loads(p.read_text())) for p in records.glob('*/state.json')]
def source_boundary(config):
    return {n:(pathlib.Path(config['workspace'])/n).read_bytes() for n in NAMES}
def config_identity(config):
    return hashlib.sha256(json.dumps({k:v for k,v in config.items() if k != 'workspace'},sort_keys=True).encode()).hexdigest()
def fingerprint(config, candidates):
    return hashlib.sha256(b'\\0'.join([config_identity(config).encode(), *[v for n in NAMES for v in (n.encode(), candidates[n])]])).hexdigest()
NAMES = %r
if __name__ == '__main__':
    config = load_config(sys.argv[sys.argv.index('--config')+1])
    root = pathlib.Path(config['records_root'])
    (root.parent/'invoked').write_text('yes')
    mode = (root.parent/'mode').read_text()
    if mode == 'sleep':
        import signal, time
        def terminate(*args):
            (root.parent/'terminated').write_text('yes')
            raise SystemExit(23)
        signal.signal(signal.SIGTERM, terminate)
        (root.parent/'sleeping').write_text('yes')
        time.sleep(60)
    identity = fingerprint(config, source_boundary(config))
    record = root/identity
    if not record.exists():
        (root.parent/'launch-count').write_text(str(int((root.parent/'launch-count').read_text())+1) if (root.parent/'launch-count').exists() else '1')
    record.mkdir(exist_ok=True)
    summary = dict(protocol=config['protocol'], objective=config['metric'], value=2.5,
                   quality_score_eligible=True, fingerprint=identity)
    if mode == 'wrong-fingerprint': summary['fingerprint'] = 'wrong'
    state = dict(status='complete', mode='scored', fingerprint=identity, summary=summary, attempts=[])
    for name, value in [('summary.json',summary),('state.json',state),('run-config.json',config)]:
        (record/name).write_text(json.dumps(value))
    if mode == 'wrong-config':
        config['seeds'] = [43]
        (record/'run-config.json').write_text(json.dumps(config))
    metric = 'wrong' if mode == 'wrong-metric' else config['metric']
    if mode == 'stderr-only':
        print('METRIC '+metric+'=2.5', file=sys.stderr)
        raise SystemExit(0)
    print('METRIC '+metric+'='+('nan' if mode == 'nonfinite' else '2.5'))
    if mode == 'duplicate': print('METRIC '+metric+'=2.5')
    if mode == 'nonzero':
        print('server diagnostic owner '+('z'*20000), file=sys.stderr)
        raise SystemExit(7)
""" % (CANDIDATE_FILES,)


class ProTokTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / ".gitignore").write_text(".ignored\n")
        self.control = self.root / "control"
        self.control.mkdir()
        self.records = self.root / "records"
        self.records.mkdir()
        self.workspace = self.root / "candidate"
        self.output = self.control / "runtime.json"
        for name in CANDIDATE_FILES:
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}" if name.endswith("json") else "# candidate\n")
        script = self.project / EVALUATOR
        script.parent.mkdir()
        script.write_text(STUB)
        self.config = dict(
            workspace=str(self.project),
            template=str(self.project),
            records_root=str(self.records),
            protocol=PROTOCOL,
            metric=METRIC,
            seeds=[42],
            launch_enabled=True,
            max_evaluations=12,
            reserve_gpu_seconds=3600,
            gpu_budget_seconds=86400,
            locked_files={EVALUATOR: hashlib.sha256(STUB.encode()).hexdigest()},
        )
        self.write(self.project / "autoresearch-run.json", self.config)
        self.write(self.records / "session.json", {"deadline": time.time() + 10000})
        self.write(
            self.control / "ownership.json",
            dict(
                schema=1,
                project=str(self.project),
                owner_token="owner",
                handoff_acknowledged=True,
                acknowledged_at=time.time() - 1,
                expires_at=time.time() + 10000,
                previous_owner="old",
                evidence="ack-message",
            ),
        )
        (self.root / "mode").write_text("success")
        self.git(self.project, "init", "-q")
        self.git(self.project, "add", ".")
        self.git(
            self.project,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        )
        self.git(self.project, "worktree", "add", "--detach", "-q", str(self.workspace))
        binary = self.root / "bin"
        binary.mkdir()
        uv = binary / "uv"
        uv.write_text(
            f"#!{sys.executable}\nimport os,sys\na=sys.argv[sys.argv.index('python')+1:]\nos.execv(sys.executable,[sys.executable,*a])\n"
        )
        uv.chmod(0o755)
        environment = patch.dict(
            os.environ, {"PATH": str(binary) + os.pathsep + os.environ["PATH"]}
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.backend = ProTokBackend(self.project, self.control, "owner")

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value))

    @staticmethod
    def git(root, *args):
        subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)

    def test_partial_short_accounting_preserves_healthy_costs_and_blocks_allocations(
        self,
    ):
        from integrations.protok.long_training import _write

        _write(
            self.records / "healthy/state.json",
            {
                "fingerprint": "healthy",
                "status": "complete",
                "attempts": [
                    {
                        "status": "complete",
                        "terminal_state": "COMPLETED",
                        "gpu_seconds": 7,
                    }
                ],
            },
        )
        _write(self.records / "unknown/state.json", {})
        (self.records / "unknown/state.json").write_text("{")
        _write(
            self.control / "research-jobs/0001/job.json",
            {"allocated_seconds": 60, "source_identity": "synthetic-source"},
        )
        _write(
            self.control / "research-jobs/0001/receipt.json",
            {
                "purpose": "research",
                "quality_score_eligible": False,
                "status": "completed",
                "gpu_seconds": 6,
                "source_identity": "synthetic-source",
            },
        )
        report = self.backend.inspect()
        self.assertIsNone(report["used_gpu_seconds"])
        self.assertIsNone(report["formal_gpu_seconds"])
        self.assertIsNone(report["total_gpu_seconds"])
        self.assertEqual(report["known_gpu_seconds"], 13)
        self.assertEqual(report["research_gpu_seconds"], 6)
        self.assertEqual(report["long_training_gpu_seconds"], 0)
        self.assertEqual(report["manual_training_gpu_seconds"], 0)
        self.assertEqual(report["accounting_status"], "partial")
        with self.assertRaisesRegex(ProTokError, "Outstanding"):
            self.backend.evaluate(self.workspace, self.output, 10)
        self.assertFalse((self.root / "invoked").exists())

    def test_retargets_only_workspace_and_allows_candidate_change(self):
        (self.workspace / CANDIDATE_FILES[0]).write_text("# new candidate\n")
        prepared = self.backend.prepare_config(self.workspace, self.output)
        self.assertEqual(
            json.loads(self.output.read_text()),
            self.config | {"workspace": str(self.workspace)},
        )
        self.assertEqual(len(prepared["fingerprint"]), 64)
        self.assertFalse((self.root / "invoked").exists())

    def test_v2_gate_uses_incremental_budget_and_new_deadline(self):
        self.write(self.control / "continuation.json", {"schema": 2})
        status = self.backend.inspect() | {
            "session": {"deadline": time.time() - 10},
            "used_gpu_seconds": 100084,
        }
        control = {
            "schema": 2,
            "effective_deadline": time.time() + 3600,
            "initial_gpu_seconds": 19084,
            "additional_gpu_seconds": 86400,
        }
        with (
            patch.object(self.backend, "inspect", return_value=status),
            patch(
                "integrations.protok.continuation.validate_continuation",
                return_value=(None, None, control),
            ),
        ):
            self.backend._gate("candidate")
            status["used_gpu_seconds"] += 1
            with self.assertRaisesRegex(ProTokError, "Additional GPU"):
                self.backend._gate("candidate")

    def test_migrated_cache_keeps_scoring_and_resource_fields_exact(self):
        old = self.config | {
            "records_root": "/old/records",
            "template": "/old/template",
        }
        self.write(
            self.control / "continuation.json",
            {
                "schema": 2,
                "relocation": {
                    "previous_records_root": "/old/records",
                    "current_records_root": str(self.records),
                },
            },
        )
        self.write(self.records / "protocol-amendment.json", {"previous_config": old})
        self.assertTrue(self.backend._receipt_config_matches(old, self.config))
        self.assertFalse(
            self.backend._receipt_config_matches(old | {"seeds": [43]}, self.config)
        )
        self.assertFalse(
            self.backend._receipt_config_matches(
                old | {"gpu_budget_seconds": 999999}, self.config
            )
        )
        self.assertFalse(
            self.backend._receipt_config_matches(
                old | {"template": "/unexpected"}, self.config
            )
        )

    def test_rejects_extra_tracked_committed_and_ignored_changes(self):
        for name in ("extra.py", ".ignored"):
            with self.subTest(name=name):
                path = self.workspace / name
                path.write_text("extra")
                with self.assertRaisesRegex(ProTokError, "outside"):
                    self.backend.prepare_config(self.workspace, self.output)
                path.unlink()
        (self.workspace / EVALUATOR).write_text("# changed")
        self.git(self.workspace, "add", EVALUATOR)
        self.git(
            self.workspace,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "outside boundary",
        )
        with self.assertRaisesRegex(ProTokError, "outside"):
            self.backend.prepare_config(self.workspace, self.output)

    def test_gate_blocks_before_evaluator_invocation(self):
        cases = ("off", "lease", "count", "wall", "gpu", "outstanding")
        for case in cases:
            with self.subTest(case=case):
                off = self.project / ".autoresearch-off"
                lease_path = self.control / "ownership.json"
                lease = json.loads(lease_path.read_text())
                config = dict(self.config)
                self.write(
                    self.records / "session.json", {"deadline": time.time() + 10000}
                )
                if case == "off":
                    off.touch()
                if case == "lease":
                    self.write(lease_path, lease | {"handoff_acknowledged": False})
                if case == "count":
                    config["max_evaluations"] = 0
                if case == "wall":
                    self.write(
                        self.records / "session.json", {"deadline": time.time() - 1}
                    )
                if case == "gpu":
                    config["gpu_budget_seconds"] = 3600
                if case == "outstanding":
                    (self.records / "other").mkdir()
                    self.write(
                        self.records / "other/state.json",
                        dict(
                            status="running",
                            mode="scored",
                            fingerprint="other",
                            attempts=[],
                        ),
                    )
                self.write(self.project / "autoresearch-run.json", config)
                with self.assertRaises(ProTokError):
                    self.backend.evaluate(self.workspace, self.output, 10)
                self.assertFalse((self.root / "invoked").exists())
                off.unlink(missing_ok=True)
                self.write(lease_path, lease)
                self.write(self.project / "autoresearch-run.json", self.config)

    def test_nonzero_evaluator_exposes_full_redacted_stdout_and_stderr(self):
        from research_loop.diagnostics import read_log_page

        (self.root / "mode").write_text("nonzero")
        with self.assertRaises(ProTokError) as rejected:
            self.backend.evaluate(self.workspace, self.output, 10)
        diagnostics = rejected.exception.diagnostics
        self.assertEqual(diagnostics["returncode"], 7)
        self.assertEqual(
            {row["stream"] for row in diagnostics["logs"]}, {"stdout", "stderr"}
        )
        stderr = next(row for row in diagnostics["logs"] if row["stream"] == "stderr")
        page = read_log_page(diagnostics["directory"], stderr["log_id"], limit=65536)
        self.assertIn("server diagnostic [REDACTED]", page["text"])
        self.assertGreater(len(page["text"]), 20000)
        self.assertTrue(page["eof"])
        raw = Path(diagnostics["directory"]) / "raw" / (stderr["log_id"] + ".log")
        self.assertIn("server diagnostic owner", raw.read_text())

    def test_capture_failure_preserves_original_evaluator_failure_and_stderr(self):
        (self.root / "mode").write_text("nonzero")
        with patch(
            "integrations.protok.backend.CommandDiagnostics.record_files",
            side_effect=PermissionError("diagnostic directory unavailable"),
        ):
            with self.assertRaisesRegex(
                ProTokError, "Protected evaluator failed"
            ) as rejected:
                self.backend.evaluate(self.workspace, self.output, 10)
        self.assertEqual(rejected.exception.diagnostics["returncode"], 7)
        self.assertEqual(
            rejected.exception.diagnostics["capture_errors"][0]["owner"], "pipeline"
        )
        receipts = list(self.control.glob("evaluation-*/receipt.json"))
        receipt = json.loads(receipts[0].read_text())
        self.assertIn("server diagnostic owner", Path(receipt["stderr"]).read_text())

    def test_success_receipt_binds_evidence_and_real_exit(self):
        receipt = self.backend.evaluate(self.workspace, self.output, 10)
        self.assertTrue(receipt["verified"])
        self.assertEqual(receipt["value"], 2.5)
        self.assertEqual(receipt["exit_code"], 0)
        self.assertTrue(all(Path(path).exists() for path in receipt["evidence"]))
        self.assertEqual(json.loads(Path(receipt["receipt"]).read_text()), receipt)
        self.assertEqual(self.backend.inspect()["scored_records"], 1)
        repeated = self.backend.evaluate(self.workspace, self.output, 10)
        self.assertEqual(repeated["fingerprint"], receipt["fingerprint"])
        self.assertEqual((self.root / "launch-count").read_text(), "1")
        self.assertNotEqual(repeated["stdout"], receipt["stdout"])

    def test_rejects_duplicate_nonfinite_failed_exit_and_unbound_evidence(self):
        for mode in (
            "duplicate",
            "nonfinite",
            "nonzero",
            "wrong-fingerprint",
            "wrong-config",
            "wrong-metric",
            "stderr-only",
        ):
            with self.subTest(mode=mode):
                (self.root / "mode").write_text(mode)
                with self.assertRaises(ProTokError):
                    self.backend.evaluate(self.workspace, self.output, 10)
                receipts = [
                    json.loads(path.read_text())
                    for path in self.control.glob("evaluation-*/receipt.json")
                ]
                self.assertTrue(all(not receipt["verified"] for receipt in receipts))
                if mode == "nonzero":
                    self.assertIn(7, [receipt["exit_code"] for receipt in receipts])

    def test_matching_outstanding_requires_explicit_recovery(self):
        prepared = self.backend.prepare_config(self.workspace, self.output)
        record = self.records / prepared["fingerprint"]
        record.mkdir()
        self.write(
            record / "state.json",
            dict(
                status="incomplete",
                mode="scored",
                fingerprint=prepared["fingerprint"],
                attempts=[{"seed": 42, "status": "running", "owned_job_id": "123"}],
            ),
        )
        with self.assertRaisesRegex(ProTokError, "explicit"):
            self.backend.evaluate(self.workspace, self.output, 10)
        self.assertFalse((self.root / "invoked").exists())
        self.assertTrue(
            self.backend.evaluate(self.workspace, self.output, 10, recover=True)[
                "verified"
            ]
        )

    def test_recovery_does_not_submit_prepared_allocation(self):
        prepared = self.backend.prepare_config(self.workspace, self.output)
        record = self.records / prepared["fingerprint"]
        record.mkdir()
        self.write(
            record / "state.json",
            dict(
                status="incomplete",
                mode="scored",
                fingerprint=prepared["fingerprint"],
                attempts=[{"seed": 42, "status": "prepared"}],
            ),
        )
        with self.assertRaisesRegex(ProTokError, "no new submission"):
            self.backend.evaluate(self.workspace, self.output, 10, recover=True)
        self.assertFalse((self.root / "invoked").exists())

    def test_completed_receipt_recovery_ignores_allocation_deadline_without_evaluating(
        self,
    ):
        original = self.backend.evaluate(self.workspace, self.output, 10)
        (self.root / "invoked").unlink()
        (self.control / "STOP").touch()
        lease = json.loads((self.control / "ownership.json").read_text())
        self.write(
            self.control / "ownership.json", lease | {"expires_at": time.time() - 1}
        )
        self.write(self.records / "session.json", {"deadline": time.time() - 1})
        recovered = self.backend.evaluate(self.workspace, self.output, 10, recover=True)
        self.assertEqual(recovered["fingerprint"], original["fingerprint"])
        self.assertEqual(recovered["value"], original["value"])
        self.assertEqual(recovered["provenance"], "authoritative-record-reconciliation")
        self.assertFalse((self.root / "invoked").exists())
        self.assertEqual((self.root / "launch-count").read_text(), "1")
        (self.workspace / CANDIDATE_FILES[0]).write_text("# different source\n")
        with self.assertRaisesRegex(ProTokError, "frozen job source identity"):
            self.backend.completed_receipt(
                self.workspace, self.output, original["fingerprint"]
            )

    def test_completed_receipt_requires_settled_accounting_and_matching_frozen_config(
        self,
    ):
        original = self.backend.evaluate(self.workspace, self.output, 10)
        state_path = self.records / original["fingerprint"] / "state.json"
        state = json.loads(state_path.read_text())
        self.write(
            state_path,
            state | {"attempts": [{"status": "running", "owned_job_id": "123"}]},
        )
        with self.assertRaisesRegex(ProTokError, "settled"):
            self.backend.completed_receipt(
                self.workspace, self.output, original["fingerprint"]
            )
        self.write(state_path, state)
        self.write(
            self.output, self.config | {"workspace": str(self.workspace), "seeds": [43]}
        )
        with self.assertRaisesRegex(ProTokError, "Frozen job configuration"):
            self.backend.completed_receipt(
                self.workspace, self.output, original["fingerprint"]
            )
        self.assertEqual((self.root / "launch-count").read_text(), "1")

    def test_completed_summary_cannot_override_a_known_wrapper_failure(self):
        for mode in (
            "duplicate",
            "nonzero",
            "nonfinite",
            "wrong-metric",
            "stderr-only",
        ):
            with self.subTest(mode=mode):
                (self.root / "mode").write_text(mode)
                with self.assertRaises(ProTokError):
                    self.backend.evaluate(self.workspace, self.output, 10)
                prepared = self.backend.prepare_config(self.workspace, self.output)
                state = json.loads(
                    (self.records / prepared["fingerprint"] / "state.json").read_text()
                )
                self.assertEqual(state["status"], "complete")
                (self.root / "invoked").unlink()
                with self.assertRaisesRegex(
                    ProTokError, "Known failed evaluator wrapper"
                ):
                    self.backend.completed_receipt(
                        self.workspace, self.output, prepared["fingerprint"]
                    )
                self.assertFalse((self.root / "invoked").exists())
                for path in self.control.glob("evaluation-*/receipt.json"):
                    path.unlink()

    def test_cancelled_timed_out_and_nonzero_receipt_metadata_block_reconstruction(
        self,
    ):
        original = self.backend.evaluate(self.workspace, self.output, 10)
        path = Path(original["receipt"])
        for failure in (
            {"cancelled": True},
            {"timed_out": True},
            {"exit_code": -15},
            {"error": "observed failure"},
        ):
            with self.subTest(failure=failure):
                self.write(path, original | failure)
                with self.assertRaisesRegex(
                    ProTokError, "Known failed evaluator wrapper"
                ):
                    self.backend.completed_receipt(
                        self.workspace, self.output, original["fingerprint"]
                    )
        path.unlink()
        receipt = self.backend.completed_receipt(
            self.workspace, self.output, original["fingerprint"]
        )
        self.assertEqual(receipt["provenance"], "authoritative-record-reconciliation")
        self.assertNotIn("exit_code", receipt)
        self.assertEqual((self.root / "launch-count").read_text(), "1")

    def continuation(self):
        prepared = self.backend.prepare_config(self.workspace, self.output)
        for number in range(13):
            record = self.records / str(number)
            record.mkdir()
            self.write(
                record / "state.json",
                dict(
                    status="complete",
                    mode="scored",
                    fingerprint=str(number),
                    attempts=[
                        dict(
                            seed=42,
                            status="complete",
                            terminal_state="COMPLETED",
                            gpu_seconds=11660 if number == 0 else 0,
                        )
                    ],
                ),
            )
        off = self.project / ".autoresearch-off"
        self.write(off, {"reason": "historical count exhausted"})
        stat = off.stat()
        now = time.time()
        control = dict(
            schema=1,
            project=str(self.project),
            owner_token="owner",
            authorization="six hours uncapped",
            previous_study="old-study",
            started_at=now - 1,
            deadline=now + 9000,
            seconds=21600,
            max_new_candidates=None,
            config_identity=prepared["config_identity"],
            evaluator_sha256=self.config["locked_files"][EVALUATOR],
            original_session=json.loads((self.records / "session.json").read_text()),
            initial_scored_records=12,
            initial_gpu_seconds=11660,
            prior_off=json.loads(off.read_text()),
            prior_off_stat={
                name: getattr(stat, "st_" + name)
                for name in ("mtime_ns", "size", "ino")
            },
        )
        self.write(self.control / "continuation.json", control)
        return prepared

    def test_continuation_uses_trusted_cli_and_admits_thirteenth_record(self):
        prepared = self.continuation()
        self.backend._gate(prepared["fingerprint"], config_path=self.output)
        command = self.backend._command(self.output, "evaluate", recover=True)
        self.assertIn(
            str(
                Path(__file__).resolve().parents[3]
                / "integrations/protok/continuation.py"
            ),
            command,
        )
        self.assertIn("--recover", command)
        (self.control / "STOP").touch()
        with self.assertRaisesRegex(ProTokError, "paused"):
            self.backend._gate(prepared["fingerprint"], config_path=self.output)

    def test_continuation_receipt_recovery_survives_stop_and_expiry(self):
        prepared = self.continuation()
        record = self.records / prepared["fingerprint"]
        record.mkdir()
        self.write(
            record / "state.json",
            dict(
                status="incomplete",
                mode="scored",
                fingerprint=prepared["fingerprint"],
                controller_lease={"owner_token": "owner"},
                attempts=[dict(seed=42, status="running", owned_job_id="123")],
            ),
        )
        owner = json.loads((self.control / "ownership.json").read_text())
        self.write(
            self.control / "ownership.json", owner | {"expires_at": time.time() - 1}
        )
        (self.control / "STOP").touch()
        self.write(self.project / ".autoresearch-off", {"reason": "new stop"})
        self.backend._gate(
            prepared["fingerprint"], config_path=self.output, recover=True
        )
        with self.assertRaises(ProTokError):
            self.backend._gate(prepared["fingerprint"], config_path=self.output)

    def test_cancel_before_launch_and_during_owned_process(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaisesRegex(ProTokError, "cancelled before launch"):
            self.backend.evaluate(self.workspace, self.output, 10, cancel_event=stop)
        self.assertFalse((self.root / "invoked").exists())
        stop.clear()
        (self.root / "mode").write_text("sleep")

        def cancel_when_running():
            deadline = time.monotonic() + 5
            while not (self.root / "sleeping").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            stop.set()

        watcher = threading.Thread(target=cancel_when_running)
        watcher.start()
        try:
            with self.assertRaisesRegex(ProTokError, "cancelled"):
                self.backend.evaluate(
                    self.workspace, self.output, 10, cancel_event=stop
                )
        finally:
            watcher.join(6)
        self.assertTrue((self.root / "terminated").exists())
        receipt = json.loads(
            next(self.control.glob("evaluation-*/receipt.json")).read_text()
        )
        self.assertTrue(receipt["cancelled"])
        self.assertFalse(receipt["timed_out"])
        self.assertEqual(receipt["exit_code"], 23)

    def test_other_outstanding_blocks_even_explicit_recovery(self):
        (self.records / "other").mkdir()
        self.write(
            self.records / "other/state.json",
            dict(
                status="failed",
                mode="scored",
                fingerprint="other",
                attempts=[{"status": "running", "owned_job_id": "123"}],
            ),
        )
        self.assertEqual(len(self.backend.inspect()["outstanding"]), 1)
        with self.assertRaises(ProTokError):
            self.backend.evaluate(self.workspace, self.output, 10, recover=True)
        self.assertFalse((self.root / "invoked").exists())


if __name__ == "__main__":
    unittest.main()
