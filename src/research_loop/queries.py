"""Read-only projections of persisted run and explicit execution facts."""

import json
import re
from pathlib import Path

from .records import list_records, read_record

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def _identity(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError("Invalid query identity")
    return value


def _root(root):
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Query root must be a run directory")
    return root


def _inside(root, path):
    if not path.resolve().is_relative_to(root):
        raise ValueError("Query path escapes the run directory")
    return path


def _run(root, run_id):
    root = _root(root)
    path = _inside(root, root / "group-runs" / _identity(run_id))
    if not path.is_dir():
        raise FileNotFoundError(path)
    return path


def _page(cursor, limit):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Query page limit must be an integer in 1..200")
    if cursor is not None:
        _identity(cursor)


def _read(root, path):
    value = json.loads(_inside(root, path).read_text())
    if not isinstance(value, dict):
        raise ValueError("Expected a query record object")
    return value


def list_runs(root, *, cursor=None, limit=50):
    """Read only the requested page of Group run records, ordered by run ID."""
    root = _root(root)
    _page(cursor, limit)
    directory = _inside(root, root / "group-runs")
    paths = sorted(
        (
            path
            for path in directory.glob("*/run.json")
            if cursor is None or path.parent.name > cursor
        ),
        key=lambda path: path.parent.name,
    )
    page = paths[:limit]
    items = []
    for path in page:
        _identity(path.parent.name)
        record = _read(root, path)
        if record.get("run_id") != path.parent.name:
            raise ValueError("Group run identity does not match its directory")
        items.append(record)
    return {
        "items": items,
        "next_cursor": page[-1].parent.name if len(paths) > limit else None,
    }


def get_run(root, run_id=None):
    """Return an existing Group run, or the controller's recorded status."""
    if run_id is not None:
        directory = _run(root, run_id)
        record = _read(directory, directory / "run.json")
        if record.get("run_id") != run_id:
            raise ValueError("Group run identity does not match its directory")
        return record
    root = _root(root)
    status = _read(root, root / "status.json")
    config = _read(root, root / "active.json")
    return {**status, "config_revision": config["revision"]}


def _list(root, run_id, kind, cursor, limit):
    _page(cursor, limit)
    directory = _run(root, run_id)
    records = _inside(directory, directory / "records" / kind)
    # Validate selected paths before list_records opens them; no record bodies read.
    selected = sorted(
        path for path in records.glob("*.json") if cursor is None or path.stem > cursor
    )[:limit]
    for path in selected:
        _inside(directory, path)
    return list_records(directory, kind, cursor=cursor, limit=limit)


def _get(root, run_id, kind, record_id):
    directory = _run(root, run_id)
    identity = _identity(record_id)
    _inside(directory, directory / "records" / kind / f"{identity}.json")
    return read_record(directory, kind, identity)


def list_steps(root, run_id, *, cursor=None, limit=50):
    return _list(root, run_id, "step", cursor, limit)


def get_step(root, run_id, record_id):
    return _get(root, run_id, "step", record_id)


def list_calls(root, run_id, *, cursor=None, limit=50):
    return _list(root, run_id, "call", cursor, limit)


def get_call(root, run_id, record_id):
    return _get(root, run_id, "call", record_id)


def list_handoffs(root, run_id, *, cursor=None, limit=50):
    return _list(root, run_id, "handoff", cursor, limit)


def get_handoff(root, run_id, record_id):
    return _get(root, run_id, "handoff", record_id)


def list_operations(root, run_id, *, cursor=None, limit=50):
    return _list(root, run_id, "operation", cursor, limit)


def get_operation(root, run_id, record_id):
    return _get(root, run_id, "operation", record_id)


def list_interventions(root, *, cursor=None, limit=50):
    from .interventions import list_interventions as listing

    _page(cursor, limit)
    return listing(_root(root), cursor=cursor, limit=limit)


def get_intervention(root, intervention_id):
    from .interventions import get_intervention as get

    return get(_root(root), _identity(intervention_id))
