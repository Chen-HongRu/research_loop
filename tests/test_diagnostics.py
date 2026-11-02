import base64
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
    redact_text,
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

    def test_structured_authorization_headers_are_redacted(self):
        for value in (
            '{"Authorization":"Basic synthetic-credential"}',
            "{'Proxy-Authorization': 'Bearer synthetic-credential'}",
            "Authorization: Basic synthetic-credential\nnext line",
            'authorization = "Basic synthetic-credential"',
        ):
            with self.subTest(value=value):
                public = redact_text(value)
                self.assertNotIn("synthetic-credential", public)
                self.assertIn("[REDACTED]", public)

    def test_nested_jsonl_credentials_keep_surrounding_log_evidence(self):
        secret = "synthetic-nested-credential"
        for header in ("Authorization", "Proxy-Authorization", "api_token"):
            nested = json.dumps({header: "Bearer " + secret, "detail": "kept detail"})
            for depth in range(4):
                nested = json.dumps({"type": "tool_result", "output": nested})
                with self.subTest(header=header, depth=depth):
                    public = redact_text(nested)
                    self.assertNotIn(secret, public)
                    self.assertIn("kept detail", public)
                    self.assertIn("[REDACTED]", public)
                    path = self.root / "stdout.jsonl"
                    path.write_text("plain before\n" + nested + "\nplain after\n")
                    self.diagnostics.record_files([path])
                    log = self.diagnostics.view()["logs"][-1]
                    public = self.full(log["log_id"], limit=3)
                    self.assertNotIn(secret, public)
                    for evidence in ("plain before", "kept detail", "plain after"):
                        self.assertIn(evidence, public)

    def test_redacted_json_keeps_quotes_and_escaped_credential_tails(self):
        for header in ("Authorization", "Proxy-Authorization", "api_token"):
            for credential in (
                'Bearer synthetic-prefix"synthetic-tail',
                'Bearer synthetic-prefix\\"synthetic-tail',
                "Bearer synthetic-prefix\\",
                "Bearer synthetic-prefix'synthetic-tail",
            ):
                for depth in range(5):
                    with self.subTest(
                        header=header, credential=credential, depth=depth
                    ):
                        original = json.dumps({header: credential, "detail": "kept"})
                        for _ in range(depth):
                            original = json.dumps(
                                {"output": original, "context": "kept"}
                            )
                        public = redact_text(original)
                        self.assertNotIn("synthetic-prefix", public)
                        self.assertNotIn("synthetic-tail", public)
                        decoded = json.loads(public)
                        for _ in range(depth):
                            self.assertEqual(decoded["context"], "kept")
                            decoded = json.loads(decoded["output"])
                        self.assertEqual(decoded[header], "[REDACTED]")
                        self.assertEqual(decoded["detail"], "kept")

    def test_truncated_quoted_credentials_redact_to_end_of_line(self):
        for header in ("Authorization", "Proxy-Authorization", "api_token"):
            for ending in ("", "\nnext evidence\n", "\r\nnext evidence\r\n"):
                for depth in range(3):
                    with self.subTest(header=header, ending=ending, depth=depth):
                        nested = json.dumps(
                            {header: "Bearer synthetic-truncated-credential"}
                        )
                        for _ in range(depth):
                            nested = json.dumps({"output": nested})
                        truncated = nested[
                            : nested.index("synthetic-truncated-credential")
                        ]
                        truncated += "synthetic-truncated-credential" + ending
                        public = redact_text(truncated)
                        self.assertNotIn("synthetic-truncated-credential", public)
                        self.assertIn("[REDACTED]", public)
                        if ending:
                            self.assertIn("next evidence", public)

    def test_utf8_pages_round_budget_up_and_keep_byte_offsets(self):
        path = self.root / "unicode.log"
        text = "a中é🙂終\n"
        path.write_text(text)
        self.diagnostics.record_files([path])
        log_id = self.diagnostics.view()["logs"][0]["log_id"]
        for limit in (1, 2, 3, 4, 5, 7):
            with self.subTest(limit=limit):
                offset, parts = 0, []
                while True:
                    page = read_log_page(
                        self.diagnostics.directory, log_id, offset=offset, limit=limit
                    )
                    parts.append(page["text"])
                    self.assertEqual(
                        page["next_offset"] - offset,
                        len(page["text"].encode("utf-8")),
                    )
                    self.assertGreater(page["next_offset"], offset)
                    self.assertLessEqual(page["next_offset"] - offset, limit + 3)
                    offset = page["next_offset"]
                    if page["eof"]:
                        break
                self.assertEqual("".join(parts), text)
                self.assertEqual(offset, len(text.encode("utf-8")))

    def test_ssh_collector_projects_encoded_payload_without_losing_raw(self):
        backend = self.backend()
        payload = {
            "files": [
                {
                    "name": "owner-value.txt",
                    "data": base64.b64encode(b"owner-value artifact").decode(),
                }
            ],
            "errors": ["missing owner-value.txt"],
            "stdout_tail": base64.b64encode(
                b"progress line\nAuthorization: Basic synthetic-credential\nowner-value"
            ).decode(),
            "future_payload": base64.b64encode(b"owner-value").decode(),
        }
        result = backend.ssh(
            {"python": sys.executable},
            "import base64; print(" + repr(json.dumps(payload)) + ")",
        )
        self.assertEqual(json.loads(result), payload)
        logs = self.diagnostics.view()["logs"]
        public = self.full(logs[0]["log_id"], limit=1)
        self.assertIn("progress line", public)
        self.assertIn("missing", public)
        self.assertNotIn("owner-value", public)
        self.assertNotIn("synthetic-credential", public)
        self.assertNotIn(payload["files"][0]["data"], public)
        self.assertNotIn(payload["stdout_tail"], public)
        self.assertNotIn(payload["future_payload"], public)
        raw = self.diagnostics.directory / "raw" / (logs[0]["log_id"] + ".log")
        self.assertEqual(raw.read_text(), result)
        self.assertNotIn("owner-value", json.dumps(self.diagnostics.view()))

    def test_unknown_encoded_ssh_stdout_is_operator_only_but_stderr_survives(self):
        backend = self.backend()
        encoded = base64.b64encode(b"owner-value").decode()
        result = backend.ssh(
            {"python": sys.executable},
            "import base64, sys; print(base64.b64encode(b'owner-value').decode()); "
            "print('collector failed owner-value', file=sys.stderr)",
        )
        self.assertEqual(result.strip(), encoded)
        logs = self.diagnostics.view()["logs"]
        self.assertNotIn(encoded, self.full(logs[0]["log_id"]))
        self.assertIn("collector failed [REDACTED]", self.full(logs[1]["log_id"]))

    def test_encoded_telemetry_and_malformed_transport_are_not_public(self):
        backend = self.backend()
        encoded = base64.b64encode(b"owner-value").decode()
        for output in (
            json.dumps({"identity": [1, 2], "size": 11, "data": encoded}),
            json.dumps({"files": None, "errors": [], "stdout_tail": encoded}),
            '{"data": "' + encoded,
        ):
            with self.subTest(output=output):
                backend.ssh(
                    {"python": sys.executable},
                    "import base64; print("
                    + repr(output)
                    + "); # base64.b64encode transport",
                )
                log = self.diagnostics.view()["logs"][-2]
                self.assertNotIn(encoded, self.full(log["log_id"]))
                raw = self.diagnostics.directory / "raw" / (log["log_id"] + ".log")
                self.assertEqual(raw.read_text(), output + "\n")

    def test_recaptured_transport_exception_keeps_stdout_operator_only(self):
        backend = self.backend()
        encoded = base64.b64encode(b"owner-value").decode()
        error = subprocess.CalledProcessError(1, ["ssh"], encoded, "failed")
        with patch.object(backend.subprocess.original, "run", side_effect=error):
            with self.assertRaises(subprocess.CalledProcessError):
                backend.ssh({"python": sys.executable}, "base64.b64encode transport")
        self.diagnostics.record_error(error)
        manifest = json.loads(
            (self.diagnostics.directory / "diagnostic-manifest.json").read_text()
        )
        for log in manifest["logs"]:
            self.assertNotIn(encoded, self.full(log["log_id"]))

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
