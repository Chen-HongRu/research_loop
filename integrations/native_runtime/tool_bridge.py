"""Private admission and submission; native file tools stay native."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import socketserver
import tempfile
import threading
import time

from research_loop.runtime.model_requests import check_active, reserve_request
from research_loop.runtime.operation_contract import json_value


@contextmanager
def serve(budget, deadline, *, startup_profile=None):
    directory = Path(tempfile.mkdtemp(prefix="rl-pi-bridge-", dir="/tmp"))
    os.chmod(directory, 0o700)
    socket_path = directory / "bridge.sock"
    state = {"requests": 0, "failed": False, "submitted": False}
    lock = threading.Lock()
    profile_root = startup_profile.parent if startup_profile is not None else None

    def seal_profile():
        if profile_root is None:
            return
        # No model has received a response yet. Pi already parsed its native
        # configuration; erase even sanitized endpoint metadata before native
        # tools become callable. Truncation also clears any Linux bind-mounted
        # inode before unlinking its host pathname.
        if startup_profile.exists():
            startup_profile.chmod(0o600)
            startup_profile.write_text("{}")
            startup_profile.unlink()
        for name in ("auth.json.lock", "trust.json.lock", "settings.json.lock"):
            if (profile_root / name).exists():
                raise RuntimeError("Pi startup authentication/trust lock is still held")
        for path in profile_root.iterdir():
            if path.is_file() and path.stat().st_mode & 0o777 != 0o400:
                path.chmod(0o400)
        profile_root.chmod(0o500)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            with lock:
                try:
                    raw = self.rfile.readline(8 * 1024 * 1024 + 1)
                    if len(raw) > 8 * 1024 * 1024 or not raw.endswith(b"\n"):
                        raise ValueError("Invalid bridge frame")
                    request = json.loads(raw)
                    if state["failed"] or state["submitted"]:
                        raise ValueError("Invocation is already finished")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Original Pi invocation deadline reached")
                    action = request["action"]
                    if action == "provider_request":
                        ordinal = state["requests"] + 1
                        if ordinal == 1 and startup_profile is not None:
                            seal_profile()
                        if budget is not None:
                            reserve_request(budget, ordinal)
                        state["requests"] = ordinal
                        result = {"ordinal": ordinal}
                    elif action == "submit":
                        if budget is not None:
                            check_active(budget)
                        state["submission"] = json_value(request["arguments"])
                        state["submitted"] = True
                        result = {"submitted": True}
                    elif action == "reject":
                        raise ValueError(
                            str(request.get("reason", "Pi runtime rejected admission"))[
                                :256
                            ]
                        )
                    else:
                        raise ValueError("Unknown bridge action")
                    response = {"ok": True, "result": result}
                except Exception as error:
                    state["failed"] = True
                    state["error"] = type(error).__name__ + ": " + str(error)[:512]
                    response = {
                        "ok": False,
                        "error": "Pi invocation admission rejected",
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
        shutil.rmtree(directory)
        if profile_root is not None:
            # Host-only cleanup, after the CLI has returned or been terminated.
            profile_root.chmod(0o700)
            for path in profile_root.iterdir():
                if path.is_file():
                    path.chmod(0o600)
