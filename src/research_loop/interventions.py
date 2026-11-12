"""Atomic external requests; only the controller consumes or applies them."""

import json
import os
import re
import socket
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .state import now, write_json

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class Intervention(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    target: str = "run"
    kind: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    payload: dict = Field(default_factory=dict)
    expected_revision: str


def _identity(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Invalid intervention ID")
    return value


def _path(root, directory, identity):
    return Path(root) / "interventions" / directory / f"{_identity(identity)}.json"


def submit_intervention(root, request):
    root = Path(root).resolve(strict=True)
    body = Intervention.model_validate(request).model_dump()
    _identity(body["target"])
    encoded = json.dumps(body, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) > 1024 * 1024:
        raise ValueError(
            "An intervention must be at most 1 MiB; reference larger inputs"
        )
    path = _path(root, "requests", body["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "request": body,
        "received_at": now(),
        "submitted_by": {
            "uid": os.getuid(),
            "pid": os.getpid(),
            "transport": "local-process",
        },
    }
    temporary = path.parent / f".{uuid4().hex}.json"
    write_json(temporary, document)
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            if json.loads(path.read_text())["request"] != body:
                raise ValueError(
                    "Intervention ID already identifies a different request"
                ) from None
    finally:
        temporary.unlink(missing_ok=True)
    # The request is durable even when the controller is not running.
    endpoint = root / "controller.json"
    if endpoint.exists():
        address = json.loads(endpoint.read_text()).get("socket")
        if address:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
                try:
                    channel.sendto(b"intervention", address)
                except (FileNotFoundError, ConnectionRefusedError):
                    pass
    return get_intervention(root, body["id"])


def get_intervention(root, identity):
    document = json.loads(_path(root, "requests", identity).read_text())
    path = _path(root, "receipts", identity)
    receipt = json.loads(path.read_text()) if path.exists() else {"status": "received"}
    return {**document, **receipt, "id": identity}


def list_interventions(root, *, cursor=None, limit=50):
    if not 1 <= limit <= 200:
        raise ValueError("Page limit must be 1..200")
    if cursor is not None:
        _identity(cursor)
    directory = Path(root) / "interventions" / "requests"
    names = sorted(
        p.stem for p in directory.glob("*.json") if cursor is None or p.stem > cursor
    )
    page = names[:limit]
    return {
        "items": [get_intervention(root, name) for name in page],
        "next_cursor": page[-1] if len(names) > limit else None,
    }


def record_intervention(root, identity, status, **data):
    if status not in {"received", "pending", "applied", "rejected"}:
        raise ValueError("Invalid intervention state")
    previous = get_intervention(root, identity)
    if previous["status"] in {"applied", "rejected"} and previous["status"] != status:
        raise ValueError("A terminal intervention cannot be applied again")
    path = _path(root, "receipts", identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    prior = json.loads(path.read_text()) if path.exists() else {}
    write_json(path, {**prior, **data, "status": status, "updated_at": now()})
    return get_intervention(root, identity)


def pending_interventions(root):
    cursor = None
    while True:
        page = list_interventions(root, cursor=cursor, limit=200)
        for row in page["items"]:
            if row["status"] in {"received", "pending"}:
                yield row
        cursor = page["next_cursor"]
        if cursor is None:
            break
