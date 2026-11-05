"""Private local Pi bridge: trusted Python tools and provider admission.

Only frozen tool declarations cross into Pi. The handler and Study capabilities
remain in the callback; every request and outcome is retained on failure.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import socketserver
import tempfile
import threading
import time

from research_loop.runtime.model_requests import (
    check_active,
    record_consulted,
    record_retrieval,
    reserve_request,
)
from research_loop.runtime.operation_contract import json_value


@contextmanager
def serve(directory, tools, handler, budget, deadline, history):
    socket_directory = Path(tempfile.mkdtemp(prefix="rl-pi-", dir="/tmp"))
    os.chmod(socket_directory, 0o700)
    socket_path = socket_directory / "bridge.sock"
    state = {"requests": 0, "failed": False, "submitted": False}
    lock = threading.Lock()

    def record(value):
        value = json_value({"recorded_at": time.time(), **value})
        with (directory / "tool-history.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        history.append(value)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            with lock:
                request = None
                try:
                    raw = self.rfile.readline(8 * 1024 * 1024 + 1)
                    if len(raw) > 8 * 1024 * 1024 or not raw.endswith(b"\n"):
                        raise ValueError("Invalid bridge frame")
                    request = json.loads(raw)
                    record({"event": "request", "request": request})
                    if state["failed"] or state["submitted"]:
                        raise ValueError("Invocation is already failed or submitted")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Original Pi invocation deadline reached")
                    action = request["action"]
                    if budget is not None and action != "provider_request":
                        check_active(budget)
                    if action == "provider_request":
                        ordinal = state["requests"] + 1
                        if budget is not None:
                            reserve_request(budget, ordinal)
                        state["requests"] = ordinal
                        result = {"ordinal": ordinal}
                    elif action == "tool":
                        name, args = request["name"], request["arguments"]
                        if (
                            name not in tools
                            or name == "submit_result"
                            or handler is None
                        ):
                            raise ValueError(
                                "Tool is outside this invocation's authorization"
                            )
                        result = json_value(handler(name, args))
                        if state["failed"]:
                            raise ValueError(
                                "Invocation was interrupted during tool execution"
                            )
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                "Original Pi invocation deadline reached"
                            )
                        if budget is not None and name in {
                            "read",
                            "read_index",
                            "search",
                        }:
                            references = []
                            if isinstance(result, dict):
                                if result.get("fixed_ref") is not None:
                                    references.append(result["fixed_ref"])
                                references.extend(result.get("dependencies", []))
                            elif name == "search" and isinstance(result, list):
                                references.extend(
                                    hit["reference"]
                                    for hit in result
                                    if isinstance(hit, dict) and "reference" in hit
                                )
                            record_consulted(budget, references)
                        if name == "read" and budget is not None:
                            reference = record_retrieval(budget, name, args, result)
                            result = (
                                {**result, "evidence_ref": reference}
                                if isinstance(result, dict)
                                else {"content": result, "evidence_ref": reference}
                            )
                    elif action == "submit":
                        state["submitted"] = True
                        result = {"submitted": True}
                    else:
                        raise ValueError("Unknown bridge action")
                    record({"event": "success", "request": request, "result": result})
                    response = {"ok": True, "result": result}
                except Exception as error:
                    state["failed"] = True
                    record(
                        {
                            "event": "failure",
                            "request": request,
                            "error": type(error).__name__,
                            "message": str(error),
                        }
                    )
                    response = {
                        "ok": False,
                        "error": "Native tool or provider admission rejected; invocation aborted.",
                    }
                self.wfile.write(
                    (json.dumps(response, allow_nan=False) + "\n").encode()
                )

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True
        block_on_close = False

    server = Server(str(socket_path), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield str(socket_path), state
    except BaseException:
        state["failed"] = True
        raise
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        shutil.rmtree(socket_directory)
