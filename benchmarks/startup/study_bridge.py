"""Trusted submission/ExecutionBackend bridge around real Codex Study stages."""

import json
from pathlib import Path
import threading
import time

from benchmarks.cactus.bridge import _publish, publish_execution, report_arguments
from benchmarks.startup.backend import FixedStartupBackend
from research_loop import submission, telemetry
from research_loop.harness import CodexHarness, HarnessConfig, HarnessError


class StartupBridge:
    def __init__(
        self, *, config: HarnessConfig, backend: FixedStartupBackend, study_root: Path
    ):
        self.delegate = CodexHarness(config)
        self.config = config
        self.backend = backend
        self.study_root = Path(study_root)
        self.started = False
        self.receipt: dict | None = None
        self.bridge_errors: list[str] = []

    def _retain_telemetry(self, receipt: dict) -> None:
        events = receipt.get("telemetry_events")
        if not isinstance(events, list) or not events:
            raise ValueError(
                "Completed GPU smoke must provide canonical telemetry events"
            )
        data = b"".join(
            json.dumps(
                event, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode()
            + b"\n"
            for event in events
        )
        if len(data) > 131072:
            raise ValueError("Smoke telemetry exceeds 128 KiB")
        source = {
            "kind": "startup-smoke",
            "experiment_id": "0001",
            "source_identity": self.backend.source_identity["baseline_commit"],
            "execution_identity": self.backend.run_id,
            "run_id": self.backend.run_id,
        }
        stream = self.backend.run_id
        current = telemetry.register(self.study_root, stream, source)
        if current["byte_end"] != 0:
            raise RuntimeError("Telemetry already retained; inspect rather than replay")
        telemetry.append_chunk(self.study_root, stream, source, data, offset=0)
        telemetry.update_status(
            self.study_root,
            stream,
            status="completed",
            remote_size=len(data),
            remote_offset=len(data),
        )

    def _service(self, workspace: Path) -> None:
        self.started = True
        try:
            prepared = None

            def preflight(request):
                nonlocal prepared
                if request["action"] != "research":
                    raise ValueError(
                        "Only the authorized research smoke may be submitted"
                    )
                prepared = self.backend.prepare()

            state = submission.status(workspace)
            if state["status"] == "pending":
                claimed = submission.claim_request(workspace, preflight=preflight)
                if claimed is None:
                    raise RuntimeError(
                        "Submission was not claimed; no execution started"
                    )
                receipt = self.backend.start(prepared)
            else:
                receipt = self.backend.inspect()
            if receipt.get("status") != "completed":
                raise RuntimeError("Retained execution is unresolved; never resubmit")
            self.receipt = receipt
            self._retain_telemetry(receipt)
            _publish(workspace / submission.RESULT, receipt)
        except Exception as error:
            self.bridge_errors.append(f"{type(error).__name__}: {error}")
            try:
                _publish(
                    workspace / submission.RESULT,
                    {
                        "status": "error",
                        "error": self.bridge_errors[-1],
                        "execution_identity": self.backend.run_id,
                        "retained_execution": self.backend.inspect(),
                    },
                )
            except (OSError, ValueError):
                self.bridge_errors.append("Could not publish bridge error")

    def _watch(self, workspace: Path, stop: threading.Event, deadline: float) -> None:
        request = workspace / submission.REQUEST
        while not self.started and time.monotonic() < deadline:
            if request.exists() or request.is_symlink():
                self._service(workspace)
                return
            if stop.wait(0.2):
                if request.exists() or request.is_symlink():
                    self._service(workspace)
                return

    def run(self, **kwargs) -> dict:
        if not kwargs.get("writable", False):
            return self.delegate.run(**kwargs)
        workspace = Path(kwargs["workspace"]).resolve()
        already_started = self.started
        deadline = time.monotonic() + (
            kwargs.get("timeout_seconds") or self.config.timeout_seconds
        )
        stop = threading.Event()
        watcher = threading.Thread(
            target=self._watch,
            args=(workspace, stop, deadline),
            name="startup-trusted-gpu-bridge",
            daemon=True,
        )
        watcher.start()
        try:
            report = self.delegate.run(**report_arguments(kwargs))
        finally:
            stop.set()
            watcher.join(timeout=max(0, deadline - time.monotonic()) + 2)
            if watcher.is_alive():
                self.bridge_errors.append("GPU callback exceeded the bridge deadline")
                raise HarnessError("GPU effects unresolved; reconciliation required")
        completed = (
            not already_started and self.receipt is not None and not self.bridge_errors
        )
        if completed:
            _publish(workspace / submission.RESULT, self.receipt)
        artifacts = (
            [
                {
                    "path": str(workspace / submission.RESULT),
                    "version": self.backend.run_id,
                }
            ]
            if completed
            else []
        )
        return publish_execution(kwargs["run_dir"], report, artifacts, [], completed)
