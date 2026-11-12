"""Small execution facts and explicit relationships, shared by runtime and queries."""

import json
import fcntl
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .state import now, write_json

KINDS = {"step", "call", "operation", "handoff", "decision"}


def record_thread(root, thread_id, **facts):
    """Attribute an actual native thread for offline cost and behavior queries."""
    path = Path(root) / "roles.json"
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        document = (
            json.loads(path.read_text())
            if path.exists()
            else {"version": 1, "threads": {}}
        )
        previous = document["threads"].get(thread_id, {})
        document["threads"][thread_id] = {**previous, **facts, "updated_at": now()}
        write_json(path, document)


class ExecutionTrace:
    def __init__(self, root, *, run_id, round, revision):
        self.root = Path(root) / "records"
        self.run_id, self.round, self.revision = run_id, round, revision
        self._paths = {}

    def begin(self, kind, label, **data):
        if kind not in KINDS:
            raise ValueError(f"Unknown execution record kind: {kind}")
        identity = f"{time.time_ns():020d}-{uuid4().hex[:12]}"
        directory = self.root / kind
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{identity}.json"
        record = {
            **data,
            "id": identity,
            "kind": kind,
            "label": label,
            "run_id": self.run_id,
            "round": self.round,
            "config_revision": self.revision,
            "status": "running",
            "started_at": now(),
        }
        write_json(path, record)
        self._paths[identity] = path
        return identity

    def end(self, identity, status="completed", **data):
        path = self._paths.get(identity)
        if path is None:
            path = next(
                (
                    self.root / kind / f"{identity}.json"
                    for kind in KINDS
                    if (self.root / kind / f"{identity}.json").is_file()
                ),
                None,
            )
        if path is None:
            raise KeyError(identity)
        record = json.loads(path.read_text())
        record.update(data, status=status, ended_at=now())
        write_json(path, record)
        return record

    def handoff(self, sender, receiver, inputs, *, status="received", **data):
        identity = self.begin(
            "handoff",
            "handoff",
            sender=sender,
            receiver=receiver,
            inputs=inputs,
            **data,
        )
        self.end(identity, status)
        return identity

    @contextmanager
    def step(self, label, **data):
        identity = self.begin("step", label, **data)
        try:
            yield identity
        except BaseException as error:
            self.end(
                identity,
                "failed",
                error={"type": type(error).__name__, "message": str(error)},
            )
            raise
        else:
            self.end(identity)


def read_record(root, kind, identity):
    if kind not in KINDS or Path(identity).name != identity:
        raise ValueError("Invalid execution record reference")
    return json.loads((Path(root) / "records" / kind / f"{identity}.json").read_text())


def list_records(root, kind, *, cursor=None, limit=50):
    """Paginate filenames before opening records; never read native histories here."""
    if kind not in KINDS or not 1 <= limit <= 200:
        raise ValueError("Invalid record kind or page limit (1..200)")
    directory = Path(root) / "records" / kind
    names = sorted(
        p.name for p in directory.glob("*.json") if cursor is None or p.stem > cursor
    )
    page = names[:limit]
    return {
        "items": [json.loads((directory / name).read_text()) for name in page],
        "next_cursor": Path(page[-1]).stem if len(names) > limit else None,
    }
