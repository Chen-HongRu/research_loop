"""A local JSONL sink for native SDK trace events; no collector or query engine."""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from agents import FunctionTool, custom_span, set_tracing_disabled
from agents.run_config import CallModelData, ModelInputData
from agents.tracing import TracingProcessor, set_trace_provider
from agents.tracing.provider import DefaultTraceProvider


class JsonlTraceProcessor(TracingProcessor):
    def __init__(self, path: Path):
        # Exclusive creation avoids mixing or overwriting separate runs.
        self._stream = path.open("x", encoding="utf-8")
        path.chmod(0o600)
        self._lock = threading.Lock()

    def _write(self, event, value):
        row = {
            "event": event,
            "at": datetime.now(timezone.utc).isoformat(),
            "data": value.export(),
        }
        with self._lock:
            self._stream.write(json.dumps(row) + "\n")
            self._stream.flush()

    def on_trace_start(self, trace):
        self._write("trace.start", trace)

    def on_trace_end(self, trace):
        self._write("trace.end", trace)

    def on_span_start(self, span):
        self._write("span.start", span)

    def on_span_end(self, span):
        self._write("span.end", span)

    def force_flush(self):
        with self._lock:
            if not self._stream.closed:
                self._stream.flush()

    def shutdown(self):
        with self._lock:
            if not self._stream.closed:
                self._stream.close()


def configure_local_tracing(path: Path) -> JsonlTraceProcessor:
    """Configure a standalone worker before its first run (process-global SDK API)."""
    processor = JsonlTraceProcessor(path)
    # Build the native provider with only our sink. Calling set_trace_processors
    # first would lazily construct the SDK's cloud exporter and HTTP client.
    provider = DefaultTraceProvider()
    provider.set_processors([processor])
    set_trace_provider(provider)
    set_tracing_disabled(False)
    return processor


def trace_model_input(data: CallModelData) -> ModelInputData:
    """Record the SDK input boundary, including instructions and function schemas."""
    with custom_span(
        "model_input",
        data={
            "agent": data.agent.name,
            "instructions": data.model_data.instructions,
            "input": data.model_data.input,
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.params_json_schema,
                }
                for tool in data.agent.tools
                if isinstance(tool, FunctionTool)
            ],
            "settings": data.agent.model_settings.to_json_dict(),
        },
    ):
        return data.model_data
