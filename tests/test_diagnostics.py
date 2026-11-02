import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from research_loop.diagnostics import (
    CommandDiagnostics,
    diagnostic_view,
    fetch_remote_log,
    read_log_page,
)


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.diagnostics = CommandDiagnostics(
            self.root / "diagnostics", secrets=("owner-value",)
        )

    def backend(self):
        backend = ModuleType("fixed")
        exec(
            """import subprocess

def run(command, **kwargs):
    return subprocess.run(command, text=True, capture_output=True, check=True, **kwargs).stdout

def ssh(config, command, **kwargs):
    return run([config['python'], '-c', command], **kwargs)
""",
            backend.__dict__,
        )
        self.diagnostics.install(backend)
        return backend

    def full(self, log_id, limit=257):
        parts, offset = [], 0
        while True:
            page = read_log_page(
                self.diagnostics.directory, log_id, offset=offset, limit=limit
            )
            parts.append(page["text"])
            if page["eof"]:
                return "".join(parts)
            offset = page["next_offset"]

    def test_success_stderr_and_return_value_are_preserved(self):
        backend = self.backend()
        original_run = subprocess.run
        self.assertEqual(
            backend.ssh(
                {"python": sys.executable},
                "import sys; print('result'); print('warning owner-value', file=sys.stderr)",
            ),
            "result\n",
        )
        self.assertIs(subprocess.run, original_run)
        logs = self.diagnostics.view()["logs"]
        self.assertEqual(len(logs), 2)
        self.assertEqual(self.full(logs[1]["log_id"]), "warning [REDACTED]\n")
        self.assertIn(
            "owner-value",
            (
                self.diagnostics.directory / "raw" / (logs[1]["log_id"] + ".log")
            ).read_text(),
        )

    def test_original_sbatch_failure_survives_later_recovery_error(self):
        backend = self.backend()
        errors = [
            subprocess.CalledProcessError(
                7, ["secret argv"], "first stdout", "sbatch original detail owner-value"
            ),
            subprocess.CalledProcessError(
                1, ["other argv"], "", "cat: job-id.txt missing"
            ),
        ]
        with patch.object(backend.subprocess.original, "run", side_effect=errors):
            for command, expected in zip(
                ("sbatch --parsable trial", "cat trial/job-id.txt"), errors
            ):
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    backend.ssh({"python": sys.executable}, command)
                self.assertIs(caught.exception, expected)
        view = self.diagnostics.view()
        self.assertEqual(view["stage"], "submit")
        self.assertEqual(view["returncode"], 7)
        logs = json.loads(
            (self.diagnostics.directory / "diagnostic-manifest.json").read_text()
        )["logs"]
        self.assertIn("sbatch original detail", self.full(logs[1]["log_id"]))
        self.assertIn("job-id.txt missing", self.full(logs[3]["log_id"]))
        self.assertNotIn("secret argv", json.dumps(view))

    def test_full_log_pagination_redacts_across_chunk_and_page_boundaries(self):
        path = self.root / "stderr.log"
        data = (
            "begin\n"
            + "x" * 65526
            + "owner-value"
            + "\napi_token=another-secret\n"
            + "z\n" * 50000
        )
        path.write_text(data)
        self.diagnostics.record_files({"stderr": path}, stage="agent", owner="pipeline")
        log = self.diagnostics.view()["logs"][0]
        whole = self.full(log["log_id"], limit=8191)
        self.assertIn("begin", whole)
        self.assertNotIn("owner-value", whole)
        self.assertNotIn("another-secret", whole)
        self.assertEqual(whole.count("z\n"), 50000)
        self.assertIn("[REDACTED]", whole)

    def test_unknown_ids_traversal_symlinks_and_bounds_are_rejected(self):
        path = self.root / "stdout.log"
        path.write_text("safe")
        self.diagnostics.record_files([path])
        log_id = self.diagnostics.view()["logs"][0]["log_id"]
        for bad in ("../stdout.log", "raw/secret", "f" * 32):
            with self.assertRaises(ValueError):
                read_log_page(self.diagnostics.directory, bad)
        with self.assertRaises(ValueError):
            read_log_page(self.diagnostics.directory, log_id, offset=-1)
        public = self.diagnostics.directory / "public" / (log_id + ".log")
        public.unlink()
        public.symlink_to(path)
        with self.assertRaises(ValueError):
            read_log_page(self.diagnostics.directory, log_id)

    def test_capture_failure_does_not_replace_original_exception(self):
        backend = self.backend()
        error = subprocess.CalledProcessError(3, ["argv"], stderr="original")
        with (
            patch.object(self.diagnostics, "_stream", side_effect=OSError("disk full")),
            patch.object(backend.subprocess.original, "run", side_effect=error),
        ):
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                backend.ssh({"python": sys.executable}, "sbatch trial")
        self.assertIs(caught.exception, error)
        self.assertEqual(
            self.diagnostics.view()["capture_errors"][0]["stage"], "diagnostic_capture"
        )

    def test_previews_are_bounded_and_install_does_not_double_capture(self):
        backend = self.backend()
        other = CommandDiagnostics(self.diagnostics.directory)
        other.install(backend)
        for _ in range(6):
            backend.ssh({"python": sys.executable}, "print('poll')")
        self.assertEqual(other.view()["log_count"], 12)
        self.assertLessEqual(len(other.view()["logs"]), 6)

    def test_safe_cause_chain_and_cli_reader(self):
        try:
            try:
                raise ValueError("password=secret owner-value")
            except ValueError as cause:
                raise RuntimeError("outer") from cause
        except RuntimeError as error:
            view = diagnostic_view(error, secrets=("owner-value",))
        self.assertEqual(
            [row["type"] for row in view["cause_chain"]], ["RuntimeError", "ValueError"]
        )
        self.assertNotIn("secret", json.dumps(view))
        path = self.root / "stdout.log"
        path.write_text("read via CLI")
        self.diagnostics.record_files([path])
        log_id = self.diagnostics.view()["logs"][0]["log_id"]
        result = subprocess.run(
            [
                sys.executable,
                str(Path(sys.modules[CommandDiagnostics.__module__].__file__)),
                "read",
                "--directory",
                str(self.diagnostics.directory),
                "--log-id",
                log_id,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(result.stdout)["text"], "read via CLI")

    def test_missing_remote_log_is_not_a_primary_command_failure(self):
        backend = self.backend()
        view = fetch_remote_log(
            backend,
            {"remote_python": sys.executable, "python": sys.executable},
            str(self.root / "missing"),
        )
        self.assertIsNone(view["returncode"])
        self.assertEqual(view["failure_count"], 0)
        self.assertEqual(view["capture_errors"][0]["stage"], "log_collection")

    def test_raw_exception_is_operator_only_and_public_message_omits_argv(self):
        error = subprocess.CalledProcessError(
            4, ["private-prompt", "owner-value"], stderr="password=secret"
        )
        message = self.diagnostics.record_error(error)
        self.assertNotIn("private-prompt", message)
        self.assertNotIn("owner-value", json.dumps(self.diagnostics.view()))
        raw = list((self.diagnostics.directory / "raw").glob("*.error"))
        self.assertIn("private-prompt", raw[0].read_text())

    def test_traceback_frames_and_wrapped_command_message_are_bounded(self):
        try:
            raise RuntimeError(
                str(subprocess.CalledProcessError(9, ["private-prompt"]))
            )
        except RuntimeError as error:
            view = diagnostic_view(error)
        cause = view["cause_chain"][0]
        self.assertNotIn("private-prompt", cause["message"])
        self.assertEqual(
            cause["frames"][-1]["function"],
            "test_traceback_frames_and_wrapped_command_message_are_bounded",
        )
        long = diagnostic_view(RuntimeError("x" * 9000))["cause_chain"][0]
        self.assertEqual(len(long["message"]), 8192)
        self.assertTrue(long["truncated"])

    def test_remote_fetch_failure_is_secondary_and_never_resubmits(self):
        backend = self.backend()
        backend.ssh = lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("missing log")
        )
        view = fetch_remote_log(
            backend, {"remote_python": sys.executable}, "/owned/run"
        )
        self.assertEqual(view["capture_errors"][0]["stage"], "log_collection")
        with self.assertRaises(ValueError):
            fetch_remote_log(backend, {}, "/owned/run", relative="../checkpoint")


if __name__ == "__main__":
    unittest.main()
