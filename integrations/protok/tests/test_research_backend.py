import copy
import hashlib
import io
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from integrations.protok import research_backend as research
from integrations.protok.backend import CANDIDATE_FILES, PROTOCOL, ProTokError


class Fixed:
    def __init__(self):
        self.submissions = 0
        self.behavior = "complete"
        self.cancel_event = None

    def load_config(self, path):
        return self.config

    def ssh(self, config, command, **kwargs):
        if command.startswith("mkdir "):
            subprocess.run(command, shell=True, check=True, capture_output=True)
            return ""
        if command.startswith("squeue "):
            return ""
        if command.startswith("sacct "):
            return "123|136|\n"
        parts = shlex.split(command)
        if len(parts) == 3 and parts[1] == "-c":
            return subprocess.check_output(
                [sys.executable, "-c", parts[2]], text=True, stderr=subprocess.PIPE
            )
        raise AssertionError(command)

    def run(self, args, **kwargs):
        if self.behavior == "stage-failure":
            raise OSError("rsync unavailable before submission")
        source, destination = args[-2:]
        destination = destination.split(":", 1)[1]
        shutil.copytree(source.rstrip("/"), destination.rstrip("/"), dirs_exist_ok=True)
        return ""

    def save_state(self, record, state):
        research._write(record / "state.json", state)

    def submit(self, config, record, state, attempt):
        self.submissions += 1
        attempt["status"] = "submitting"
        self.save_state(record, state)
        if self.behavior == "unknown":
            raise OSError("lost sbatch reply and no owned receipt")
        attempt.update(status="running", owned_job_id="123")
        self.save_state(record, state)
        remote = Path(attempt["remote"])
        (remote / "output/result.json").write_text('{"observation": 1}\n')
        (remote / "run.log").write_bytes(b"x" * 100000)
        if self.cancel_event:
            self.cancel_event.set()

    def wait_for_job(self, config, session, record, state, attempt):
        self.ssh(config, "squeue -h -j 123 -o %T")
        if self.behavior == "unknown":
            raise RuntimeError("unresolved owned receipt")
        cancelled = attempt.get("cancel_requested")
        attempt.update(
            status="failed" if cancelled else "finished",
            gpu_seconds=17,
            terminal_state="CANCELLED" if cancelled else "COMPLETED",
            exit_code="0:15" if cancelled else "0:0",
        )
        self.save_state(record, state)
        if cancelled:
            raise RuntimeError("allocation cancelled and charged")

    def cancel_interrupted(self, config, record, state):
        attempt = state["attempts"][0]
        if attempt.get("owned_job_id"):
            attempt["cancel_requested"] = True
        self.save_state(record, state)


class ResearchBackendTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.root = self.directory / "run"
        self.control = self.root / "control"
        self.control.mkdir(parents=True)
        self.source = self.directory / "source"
        self.source.mkdir()
        for name in (
            *CANDIDATE_FILES,
            "scripts/protected.py",
            "configs/fixed.json",
            "research/probe.py",
        ):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                '{"teacher_checkpoint": "/trusted/teacher.ckpt"}'
                if name.endswith("json")
                else "# source\n"
            )
        # Git metadata and unrelated historical artifacts must never be read/staged.
        (self.source / ".git").mkdir()
        (self.source / ".git/unreadable").symlink_to("/does-not-exist")
        (self.source / "historical.pt").write_bytes(b"not selected")
        self.config = {
            "locked_files": {
                name: hashlib.sha256((self.source / name).read_bytes()).hexdigest()
                for name in ("scripts/protected.py", "configs/fixed.json")
            },
            "remote_python": "/data/user/hongruchen/projects/ProTok/.venv/bin/python",
            "remote_root": str(self.directory / "remote"),
            "ssh_host": "test-hpc",
            "cpus": 8,
            "mem": "96G",
            "reserve_gpu_seconds": 3600,
            "cache_root": "/trusted/hidden",
            "target_root": "/trusted/targets",
            "template": str(self.source),
            "fixed_config": "configs/fixed.json",
            "records_root": str(self.directory / "formal-records"),
        }
        cache = self.directory / "hidden-cache"
        cache.mkdir()
        research._write(
            cache / "manifest.json",
            {
                "format": "protok-frozen-hidden-v1",
                "identity": "cache-v1",
                "complete": True,
                "splits": {"train": {"rows": 10}, "val": {"rows": 2}},
            },
        )
        self.config["cache_root"] = str(cache)
        self.deadline = time.time() + 7200
        self.lease = {"initial_gpu_seconds": 1000, "additional_gpu_seconds": 86400}
        research._write(self.control / "continuation.json", self.lease)
        research._write(self.control / "ownership.json", {"owner_token": "owner"})
        research._write(
            self.control / "research-policy.json",
            {
                "version": 1,
                "authorization": "user authorized arbitrary diagnostic scripts",
                "max_time_limit_seconds": 1800,
            },
        )
        self.execution = {
            "mode": "script",
            "entrypoint": "research/probe.py",
            "gpu": True,
            "time_limit_seconds": 61,
            "inputs": [
                {"name": "hidden", "source": "hidden_cache", "role": "development"}
            ],
            "outputs": ["result.json"],
        }
        self.backend = Mock()
        self.backend.project = self.source
        self.backend.owner_token = "owner"
        self.backend._config.return_value = self.config
        self.backend.inspect.return_value = {
            "used_gpu_seconds": 1000,
            "outstanding": [],
            "debug_outstanding": [],
        }
        self.fixed = Fixed()
        self.fixed.config = self.config
        self.patch = patch.object(
            research,
            "validate_continuation",
            return_value=(
                self.fixed,
                self.config,
                {"effective_deadline": self.deadline},
            ),
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)
        loader = patch.object(
            research, "_load_backend", return_value=(self.fixed, self.config)
        )
        loader.start()
        self.addCleanup(loader.stop)

    def run_job(self, **changes):
        return research.run_script(
            self.backend,
            root=self.root,
            iteration="0002",
            source=self.source,
            execution=self.execution,
            deadline=self.deadline,
            **changes,
        )

    def environment(self):
        return {
            "version": 1,
            "id": "common-20261102-v1",
            "base_python": research.BASE_PYTHON,
            "python_version": "3.12.13",
            "site_packages": research.RESEARCH_ENVIRONMENTS
            + "/common-20261102-v1/site-packages",
            "packages": [
                {
                    "distribution": "numpy",
                    "import": "numpy",
                    "version": "2.2.6",
                    "source": "base",
                },
                {
                    "distribution": "scipy",
                    "import": "scipy",
                    "version": "1.15.3",
                    "source": "supplement",
                },
            ],
        }

    def test_complete_server_log_is_registered_beyond_existing_tail(self):
        from research_loop.diagnostics import read_log_page

        receipt = self.run_job()
        capture = receipt["diagnostics"]
        self.assertTrue(Path(capture["manifest"]).is_file())
        self.assertEqual(
            capture["directory"],
            str((self.control / "research-jobs/0002/command-diagnostics").resolve()),
        )
        for log in capture["logs"]:
            page = read_log_page(capture["directory"], log["log_id"])
            self.assertIsInstance(page["text"], str)
        complete = next(log for log in capture["logs"] if log["bytes"] == 100000)
        first = read_log_page(capture["directory"], complete["log_id"], limit=65536)
        second = read_log_page(
            capture["directory"],
            complete["log_id"],
            offset=first["next_offset"],
            limit=65536,
        )
        self.assertEqual(first["text"] + second["text"], "x" * 100000)
        self.assertTrue(second["eof"])
        self.assertEqual(self.fixed.submissions, 1)

    def test_failure_public_receipt_omits_subprocess_argv_and_owner(self):
        failure = subprocess.CalledProcessError(
            8, ["private-prompt", "owner"], stderr="token=secret"
        )
        with patch.object(self.fixed, "run", side_effect=failure):
            receipt = self.run_job()
        self.assertNotIn("private-prompt", receipt["error"])
        self.assertNotIn("owner", receipt["error"])
        raw = Path(receipt["diagnostics"]["directory"]) / "raw"
        self.assertTrue(
            any("private-prompt" in path.read_text() for path in raw.glob("*.error"))
        )
        self.assertEqual(self.fixed.submissions, 0)

    def test_environment_is_snapshotted_mounted_and_bound_to_identity(self):
        environment = self.environment()
        research._write(self.control / "research-environment.json", environment)
        receipt = self.run_job()
        record = self.control / "research-jobs/0002"
        self.assertEqual(receipt["environment"], environment)
        for path in (
            record / "job.json",
            record / "staging/environment.json",
            record / "environment.json",
        ):
            value = research._read(path)
            self.assertEqual(value.get("environment", value), environment)
        self.assertIn(str((record / "environment.json").resolve()), receipt["evidence"])
        import json

        expected = hashlib.sha256(
            json.dumps(
                {
                    "source_identity": receipt["source_identity"],
                    "execution": self.execution,
                    "inputs": receipt["inputs"],
                    "iteration": "0002",
                    "event_writer_identity": research._read(
                        self.control / "research-jobs/0002/job.json"
                    )["event_writer_identity"],
                    "environment": environment,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        self.assertEqual(receipt["execution_identity"], expected)
        launcher = research._launcher(
            self.config, "/remote/attempt", self.execution, [], environment
        )
        self.assertIn(repr((environment["site_packages"], "/research-libs")), launcher)
        self.assertIn(
            repr(("/remote/attempt/environment.json", "/environment.json")), launcher
        )
        self.assertIn('args += ["--ro-bind",src,dst]', launcher)
        self.assertIn("/workspace:/workspace/scripts:/research-libs", launcher)
        self.assertIn('"MPLBACKEND":"Agg"', launcher)
        self.assertNotIn(
            repr((research.RESEARCH_ENVIRONMENTS, research.RESEARCH_ENVIRONMENTS)),
            launcher,
        )
        compile(launcher, "launcher", "exec")

    def test_recovery_uses_retained_environment_even_when_live_manifest_changes(self):
        environment = self.environment()
        research._write(self.control / "research-environment.json", environment)
        self.fixed.behavior = "unknown"
        initial = self.run_job()
        # Invalid live configuration must not affect an already admitted allocation.
        research._write(self.control / "research-environment.json", {"invalid": True})
        recovered = self.run_job(recover=True)
        self.assertEqual(recovered["environment"], environment)
        self.assertEqual(recovered["execution_identity"], initial["execution_identity"])
        self.assertEqual(self.fixed.submissions, 1)

    def test_legacy_recovery_ignores_new_environment_and_preserves_old_receipts(self):
        self.fixed.behavior = "unknown"
        initial = self.run_job()
        research._write(self.control / "research-environment.json", self.environment())
        recovered = self.run_job(recover=True)
        self.assertNotIn("environment", initial)
        self.assertNotIn("environment", recovered)
        self.assertEqual(recovered["execution_identity"], initial["execution_identity"])
        record = self.control / "research-jobs/0002"
        self.assertFalse((record / "staging/environment.json").exists())
        launcher = (record / "staging/code/autoresearch-se3.sbatch").read_text()
        self.assertNotIn("/research-libs", launcher)

    def test_invalid_environment_is_rejected_before_publication(self):
        for key, value in (
            ("site_packages", research.RESEARCH_ENVIRONMENTS + "/../private"),
            ("base_python", "/other/python"),
            ("python_version", "3.13.0"),
            ("packages", [{"distribution": "scipy"}]),
        ):
            with self.subTest(key=key):
                environment = self.environment()
                environment[key] = value
                research._write(self.control / "research-environment.json", environment)
                with self.assertRaises(ProTokError):
                    self.run_job()
                self.assertFalse((self.control / "research-jobs/0002").exists())

    def admit_script_round(self):
        from integrations.protok.round_admission import admit, commit

        now = time.time()
        self.lease.update(started_at=now - 10, deadline=now + 1, owner_token="owner")
        research._write(self.control / "continuation.json", self.lease)
        research._write(
            self.control / "ownership.json",
            {
                "owner_token": "owner",
                "expires_at": now + 1,
                "handoff_acknowledged": True,
            },
        )
        token = admit(self.root, "0002", owner_token="owner")
        committed_at = commit(self.root, "0002", token, owner_token="owner")
        folder = self.root / "study/iterations/0002"
        folder.mkdir(parents=True)
        research._write(folder / "iteration.json", {"round_admission": token})
        research._write(
            folder / "plan.json",
            {
                "decision": "investigate",
                "contract": {"timeout_seconds": 1800},
                "research_contract": {"execution": self.execution},
            },
        )
        research._write(
            self.root / "study/state.json",
            {
                "iteration": 2,
                "phase": "execute",
                "status": "ready",
                "round_admission": token,
                "round_admitted_at": committed_at,
                "plan_ref": "iterations/0002/plan.json",
            },
        )
        self.deadline = now + 1800
        research.validate_continuation.return_value = (
            self.fixed,
            self.config,
            {"effective_deadline": now + 1},
        )
        return now

    def test_admitted_script_uses_own_window_without_formal_override(self):
        from integrations.protok.round_admission import frozen_execution_deadline

        now = self.admit_script_round()
        with patch(
            "integrations.protok.research_backend.time.time", return_value=now + 2
        ):
            receipt = self.run_job()
        self.assertEqual(receipt["status"], "completed")
        self.assertNotIn(
            "execution_deadline", research.validate_continuation.call_args.kwargs
        )
        self.assertEqual(
            frozen_execution_deadline(
                self.root, owner_token="owner", attempt="research/0002"
            ),
            self.deadline,
        )

    def test_admitted_script_window_cannot_extend_plan_or_ignore_stop(self):
        self.admit_script_round()
        self.deadline += 1
        with self.assertRaisesRegex(ProTokError, "Plan timeout"):
            self.run_job()
        self.assertFalse((self.control / "research-jobs/0002").exists())
        self.deadline -= 1
        (self.control / "STOP").touch()
        with self.assertRaisesRegex(ProTokError, "original lease"):
            self.run_job()
        self.assertFalse((self.control / "research-jobs/0002").exists())

    def test_script_receipt_is_bounded_nonformal_and_repeatable(self):
        receipt = self.run_job()
        self.assertEqual(receipt["status"], "completed")
        self.assertFalse(receipt["quality_score_eligible"])
        self.assertEqual(receipt["purpose"], "research")
        self.assertEqual(receipt["allocated_seconds"], 120)
        self.assertEqual(receipt["gpu_seconds"], 17)
        self.assertEqual(receipt["cpu_seconds"], 136)
        self.assertNotIn("metric", receipt)
        self.assertEqual(
            Path(receipt["outputs"][0]).read_text(), '{"observation": 1}\n'
        )
        self.assertEqual(
            (self.control / "research-jobs/0002/stdout-tail.log").stat().st_size,
            research.LOG_BYTES,
        )
        self.assertEqual(self.run_job(), receipt)
        self.assertEqual(self.fixed.submissions, 1)
        staging = self.control / "research-jobs/0002/staging/code"
        self.assertFalse((staging / ".git").exists())
        self.assertFalse((staging / "historical.pt").exists())
        self.assertTrue((staging / "research/probe.py").exists())

    def test_unknown_submission_stays_outstanding_and_is_not_resubmitted(self):
        self.fixed.behavior = "unknown"
        receipt = self.run_job()
        self.assertEqual(receipt["status"], "blocked")
        record = self.control / "research-jobs/0002"
        self.assertFalse((record / "receipt.json").exists())
        with self.assertRaises(ProTokError):
            self.run_job()
        recovered = self.run_job(recover=True)
        self.assertEqual(recovered["status"], "blocked")
        self.config["ssh_host"] = "different-hpc"
        with self.assertRaises(ProTokError):
            self.run_job(recover=True)
        self.assertEqual(self.fixed.submissions, 1)

    def test_pre_submission_failure_is_settled_zero_cost(self):
        self.fixed.behavior = "stage-failure"
        receipt = self.run_job()
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["gpu_seconds"], 0)
        self.assertEqual(receipt["cpu_seconds"], 0)
        self.assertEqual(self.fixed.submissions, 0)

    def test_cancellation_preserves_owned_accounting(self):
        event = threading.Event()
        self.fixed.cancel_event = event
        receipt = self.run_job(cancel_event=event)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["terminal_state"], "CANCELLED")
        self.assertEqual(receipt["gpu_seconds"], 17)
        self.assertEqual(receipt["owned_job_id"], "123")

    def test_budget_and_cpu_only_rejected_before_effects(self):
        self.execution["gpu"] = False
        with self.assertRaises(ProTokError):
            self.run_job()
        self.execution["gpu"] = True
        self.backend.inspect.return_value["used_gpu_seconds"] = 85000
        with self.assertRaises(ProTokError):
            self.run_job()
        self.assertFalse((self.control / "research-jobs/0002").exists())

    def test_changed_identity_and_locked_files_fail_closed(self):
        self.run_job()
        (self.source / "research/probe.py").write_text("# changed\n")
        with self.assertRaises(ProTokError):
            self.run_job()
        (self.source / "scripts/protected.py").write_text("# forbidden\n")
        with self.assertRaises(ProTokError):
            self.run_job()
        self.assertEqual(self.fixed.submissions, 1)

    def test_sandbox_has_explicit_mounts_and_no_host_root_or_home(self):
        inputs = research._inputs(self.root, self.config, self.execution, "0002")
        launcher = research._launcher(
            self.config, "/remote/attempt", self.execution, inputs
        )
        self.assertIn('"--unshare-net"', launcher)
        self.assertIn('"--unshare-pid"', launcher)
        self.assertIn('"--as-pid-1"', launcher)
        self.assertNotIn('"--clearenv"', launcher)
        self.assertIn('os.execve(args[0],args,{"PATH":"/usr/bin:/bin"})', launcher)
        self.assertNotIn("('/etc', '/etc')", launcher)
        self.assertIn("/etc/ld.so.cache", launcher)
        self.assertNotIn("('/', '/')", launcher)
        self.assertNotIn("('/home', '/home')", launcher)
        self.assertIn(repr((self.config["cache_root"], "/inputs/hidden")), launcher)
        base = "/data/user/hongruchen/.local/share/uv/python/cpython-3.12.13-linux-x86_64-gnu"
        alias = (
            "/data/user/hongruchen/.local/share/uv/python/cpython-3.12-linux-x86_64-gnu"
        )
        self.assertIn(repr((base, alias)), launcher)
        self.assertNotIn("('/data', '/data')", launcher)
        self.assertIn("/dev/nvidia", launcher)
        compile(launcher, "launcher", "exec")

    def test_launcher_mounts_ldconfig_read_only_and_preserves_isolation(self):
        inputs = research._inputs(self.root, self.config, self.execution, "0002")
        launcher = research._launcher(
            self.config, "/remote/attempt", self.execution, inputs
        )
        output = io.StringIO()
        with (
            patch.dict(
                "os.environ",
                {
                    "CUDA_VISIBLE_DEVICES": "0",
                    "SLURM_JOB_GPUS": "4",
                    "SECRET_TOKEN": "must-not-be-logged",
                },
            ),
            patch(
                "subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess([], 0, "0, GPU-test\n", ""),
                    subprocess.CompletedProcess(
                        [],
                        0,
                        "<nvidia_smi_log><gpu><uuid>GPU-test</uuid><minor_number>4</minor_number></gpu></nvidia_smi_log>",
                        "",
                    ),
                ],
            ) as query,
            patch("ctypes.CDLL", side_effect=OSError("driver probe unavailable")),
            patch("os.path.exists", return_value=True),
            patch("os.execve") as execute,
            patch("sys.stdout", output),
        ):
            exec(compile(launcher, "research-launcher", "exec"), {})
        self.assertEqual(
            query.call_args_list[0].args[0],
            [
                "nvidia-smi",
                "--query-gpu=index,uuid",
                "--format=csv,noheader",
                "-i",
                "0",
            ],
        )
        self.assertEqual(
            query.call_args_list[1].args[0], ["nvidia-smi", "-q", "-x", "-i", "0"]
        )
        args = execute.call_args.args[1]
        read_only_mounts = [
            tuple(args[index + 1 : index + 3])
            for index, arg in enumerate(args)
            if arg == "--ro-bind"
        ]
        self.assertIn(("/sbin", "/sbin"), read_only_mounts)
        self.assertIn(("/remote/attempt/code", "/workspace"), read_only_mounts)
        for item in inputs:
            self.assertIn((item["path"], item["mount"]), read_only_mounts)
        writable_mounts = [
            tuple(args[index + 1 : index + 3])
            for index, arg in enumerate(args)
            if arg == "--bind"
        ]
        self.assertEqual(writable_mounts, [("/remote/attempt/output", "/output")])
        self.assertIn("--unshare-net", args)
        self.assertIn("--unshare-pid", args)
        self.assertEqual(args[args.index("--cap-drop") + 1], "ALL")
        self.assertIn("/dev/nvidia4", args)
        self.assertNotIn("/dev/nvidia0", args)
        text = output.getvalue()
        self.assertIn('"minor_number": "4"', text)
        self.assertIn('"SLURM_JOB_GPUS": "4"', text)
        self.assertIn("host_libcuda_error", text)
        self.assertNotIn("SECRET_TOKEN", text)
        self.assertNotIn("must-not-be-logged", text)

    def test_gpu_query_failure_keeps_driver_evidence_and_bounded_error_tails(self):
        launcher = research._launcher(
            self.config, "/remote/attempt", self.execution, []
        )
        output = io.StringIO()
        with (
            patch.dict("os.environ", {"CUDA_VISIBLE_DEVICES": "0"}),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess(
                    [], 2, "x" * 10000, "unsupported field"
                ),
            ),
            patch("ctypes.CDLL", side_effect=OSError("driver location unavailable")),
            patch("os.execve") as execute,
            patch("sys.stdout", output),
        ):
            with self.assertRaisesRegex(RuntimeError, "metadata query failed"):
                exec(compile(launcher, "research-launcher", "exec"), {})
        text = output.getvalue()
        self.assertLess(
            text.index("host_libcuda_error"), text.index("RESEARCH_GPU_QUERY_ERROR")
        )
        self.assertIn("unsupported field", text)
        self.assertNotIn("x" * 4097, text)
        execute.assert_not_called()

    def test_collector_refuses_symlinks_and_caps_total_output(self):
        remote = self.directory / "collect"
        (remote / "output").mkdir(parents=True)
        record = self.directory / "collection-receipt"
        record.mkdir()
        (remote / "run.log").write_text("log")
        (remote / "outside").write_text("not an output")
        (remote / "output/link").symlink_to(remote / "outside")
        (remote / "output/large").write_bytes(b"x" * (research.MAX_OUTPUT_BYTES + 1))
        paths, errors = research._collect(
            self.fixed, self.config, str(remote), ["link", "large"], record
        )
        self.assertEqual(paths, [])
        self.assertEqual(len(errors), 2)

    def test_roles_and_unknown_research_jobs_rejected_before_effects(self):
        for role in ("final", "holdout", "model", "reference"):
            with self.subTest(role=role):
                self.execution["inputs"][0]["role"] = role
                with self.assertRaises(ProTokError):
                    self.run_job()
        self.execution["inputs"][0]["role"] = "development"
        self.backend.inspect.return_value["research_outstanding"] = ["another-script"]
        with self.assertRaises(ProTokError):
            self.run_job()
        self.assertFalse((self.control / "research-jobs/0002").exists())

    def test_execution_identity_binds_script_request_and_input_metadata(self):
        (self.source / "research/constants.csv").write_text("a,b\n1,2\n")
        receipt = self.run_job()
        import json

        expected = hashlib.sha256(
            json.dumps(
                {
                    "source_identity": receipt["source_identity"],
                    "execution": self.execution,
                    "inputs": receipt["inputs"],
                    "iteration": "0002",
                    "event_writer_identity": research._read(
                        self.control / "research-jobs/0002/job.json"
                    )["event_writer_identity"],
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        self.assertEqual(receipt["execution_identity"], expected)
        self.assertEqual(receipt["inputs"][0]["metadata"]["identity"], "cache-v1")
        stage = self.control / "research-jobs/0002/staging"
        self.assertTrue((stage / "code/research/constants.csv").is_file())
        manifest = research._read(stage / "inputs.json")
        self.assertIn("mount field", manifest["instructions"])
        batch = (stage / "code/autoresearch-se3.sbatch").read_text()
        self.assertIn("ulimit -f 16384", batch)

    def test_full_cache_with_extra_holdout_split_is_not_exposed(self):
        path = Path(self.config["cache_root"]) / "manifest.json"
        manifest = research._read(path)
        manifest["splits"]["test"] = {"rows": 1}
        research._write(path, manifest)
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_job()
        self.assertEqual(self.fixed.submissions, 0)
        self.assertFalse((self.control / "research-jobs/0002").exists())

    def test_resolved_checkpoint_requires_matching_completed_receipts(self):
        folder = self.root / "study/iterations/0001"
        folder.mkdir(parents=True)
        research._write(folder / "iteration.json", {"status": "reviewed"})
        research._write(folder / "publication.json", {})
        job = self.control / "jobs/0001"
        job.mkdir(parents=True)
        research._write(
            job / "job.json", {"status": "completed", "fingerprint": "identity"}
        )
        research._write(
            job / "result.json", {"status": "completed", "fingerprint": "identity"}
        )
        formal = Path(self.config["records_root"]) / "identity"
        formal.mkdir(parents=True)
        state = {
            "status": "complete",
            "fingerprint": "identity",
            "summary": {"protocol": PROTOCOL, "quality_score_eligible": True},
            "attempts": [
                {
                    "seed": 42,
                    "terminal_state": "COMPLETED",
                    "exit_code": "0:0",
                    "gpu_seconds": 300,
                    "owned_job_id": "99",
                    "remote": self.config["remote_root"] + "/formal/identity",
                }
            ],
        }
        research._write(formal / "state.json", state)
        execution = copy.deepcopy(self.execution)
        execution["inputs"] = [
            {"name": "model", "source": "checkpoint:0001:2000", "role": "development"}
        ]
        inputs = research._inputs(self.root, self.config, execution, "0002")
        self.assertTrue(inputs[0]["path"].endswith("/train/checkpoint-step-2000.pt"))
        state["attempts"][0]["terminal_state"] = "FAILED"
        research._write(formal / "state.json", state)
        with self.assertRaises(ProTokError):
            research._inputs(self.root, self.config, execution, "0002")


if __name__ == "__main__":
    unittest.main()
