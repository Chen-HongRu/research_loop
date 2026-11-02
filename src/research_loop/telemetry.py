"""Small append-only observations, independent from model and scoring libraries.

Run ``python -m research_loop.telemetry --study ROOT --stream ID`` to page raw
observations. Frozen manifests provide an exact ``--until`` byte boundary.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from collections import deque
from itertools import islice
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

MAX_EVENT_BYTES = 8192
EVENT_FIELDS = {
    "version",
    "id",
    "time",
    "name",
    "value",
    "tags",
    "step",
    "unit",
    "context",
}


def _json(value):
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _observed(value):
    if isinstance(value, float) and not math.isfinite(value):
        return {
            "nonfinite": "NaN"
            if math.isnan(value)
            else "Infinity"
            if value > 0
            else "-Infinity"
        }
    if isinstance(value, (list, tuple)):
        return [_observed(item) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Event JSON object keys must be strings")
        return {key: _observed(item) for key, item in value.items()}
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise ValueError("Event values must be small JSON values")


def _event(value):
    if (
        not isinstance(value, dict)
        or set(value) != EVENT_FIELDS
        or value["version"] != 1
    ):
        raise ValueError("Invalid version 1 event fields")
    if (
        not isinstance(value["id"], str)
        or not value["id"]
        or not isinstance(value["name"], str)
        or not value["name"].strip()
    ):
        raise ValueError("Event id and name must be nonempty strings")
    if type(value["time"]) not in (int, float) or not math.isfinite(value["time"]):
        raise ValueError("Event time must be a finite Unix timestamp")
    if not isinstance(value["tags"], list) or any(
        not isinstance(tag, str) or not tag.strip() for tag in value["tags"]
    ):
        raise ValueError("Event tags must be strings")
    if value["step"] is not None and type(value["step"]) is not int:
        raise ValueError("Event step must be an integer or null")
    if value["unit"] is not None and not isinstance(value["unit"], str):
        raise ValueError("Event unit must be a string or null")
    if not isinstance(value["context"], dict):
        raise ValueError("Event context must be an object")
    _json(value)
    return value


def emit(
    name, value, *, tags=("internal",), step=None, unit=None, context=None, path=None
):
    """Append to the local OS in one unbuffered write, surviving process exit/kill.

    Power-loss/node-crash durability is not guaranteed. Visibility is explicitly
    opted into with ``dashboard``; no per-event fsync perturbs the training path.
    """
    target = path if path is not None else os.environ.get("RESEARCH_LOOP_EVENT_FILE")
    if not target:
        raise ValueError("Set RESEARCH_LOOP_EVENT_FILE or pass an explicit event path")
    if isinstance(tags, str):
        raise ValueError("Event tags must be a sequence of strings")
    event = _event(
        {
            "version": 1,
            "id": str(uuid.uuid4()),
            "time": time.time(),
            "name": name,
            "value": _observed(value),
            "tags": list(tags),
            "step": step,
            "unit": unit,
            "context": _observed({} if context is None else context),
        }
    )
    data = _json(event) + b"\n"
    if len(data) > MAX_EVENT_BYTES:
        raise ValueError("Individual event exceeds 8192 bytes")
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        size = os.fstat(fd).st_size
        if size:
            os.lseek(fd, -1, os.SEEK_END)
            if os.read(fd, 1) != b"\n":
                data = b"\n" + data
        if os.write(fd, data) != len(data):
            raise OSError(
                "Incomplete event append; retained bytes must not be rewritten"
            )
    finally:
        os.close(fd)
    return event


def _stream(study_root, stream_id):
    if (
        not isinstance(stream_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", stream_id)
        or stream_id in {".", ".."}
    ):
        raise ValueError("Invalid telemetry stream ID")
    return Path(study_root).resolve() / "telemetry" / stream_id


@contextmanager
def _locked(study_root):
    folder = Path(study_root).resolve() / "telemetry"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield folder


def _read(path):
    if path.stat().st_size > 65536:
        raise ValueError("Telemetry metadata exceeds 64 KiB")
    return json.loads(path.read_text())


def _write(path, value):
    data = _json(value) + b"\n"
    if len(data) > 65536:
        raise ValueError("Telemetry metadata exceeds 64 KiB")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _source(source):
    if not isinstance(source, dict) or any(
        not isinstance(source.get(key), str) or not source[key].strip()
        for key in ("kind", "experiment_id", "source_identity", "execution_identity")
    ):
        raise ValueError(
            "Stream source requires immutable kind/experiment/source/execution identity"
        )

    def inspect(value):
        if isinstance(value, dict):
            if "owner_token" in value:
                raise ValueError(
                    "Owner tokens cannot appear in telemetry source metadata"
                )
            for item in value.values():
                inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)

    inspect(source)
    normalized = _observed(source)
    if len(_json(normalized)) > 32768:
        raise ValueError("Telemetry source metadata exceeds 32 KiB")
    return normalized


def _info(folder):
    if not (folder / "meta.json").is_file():
        raise ValueError("Unknown telemetry stream")
    result = _read(folder / "meta.json")
    return {
        **result,
        "log_path": str(folder / "events.jsonl"),
        "byte_end": (folder / "events.jsonl").stat().st_size,
    }


def register(study_root, stream_id, source):
    source = _source(source)
    folder = _stream(study_root, stream_id)
    with _locked(study_root):
        if (folder / "meta.json").exists():
            result = _info(folder)
            if result["source"] != source:
                raise ValueError("Telemetry stream source identity mismatch")
            return result
        folder.mkdir(parents=True, exist_ok=True)
        log = folder / "events.jsonl"
        if log.exists() and log.stat().st_size:
            raise ValueError("Unidentified telemetry evidence already exists")
        log.touch(exist_ok=True)
        _write(
            folder / "meta.json",
            {
                "version": 1,
                "stream_id": stream_id,
                "source": source,
                "status": "registered",
                "updated_at": time.time(),
            },
        )
        return _info(folder)


def info(study_root, stream_id):
    return _info(_stream(study_root, stream_id))


def append_chunk(study_root, stream_id, source, data: bytes, *, offset: int):
    """Retain raw source bytes at their exact cursor; stale retries never duplicate."""
    if not isinstance(data, bytes) or type(offset) is not int or offset < 0:
        raise ValueError(
            "Telemetry append requires bytes and a nonnegative byte offset"
        )
    folder = _stream(study_root, stream_id)
    source = _source(source)
    with _locked(study_root):
        current = _info(folder)
        if current["source"] != source:
            raise ValueError("Telemetry stream source identity mismatch")
        fd = os.open(folder / "events.jsonl", os.O_WRONLY | os.O_APPEND)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            actual = os.fstat(fd).st_size
            if offset != actual:
                raise ValueError(
                    f"Telemetry offset mismatch; expected {actual}, received {offset}"
                )
            if data:
                if os.write(fd, data) != len(data):
                    raise OSError(
                        "Incomplete telemetry append; recover using actual file size"
                    )
                os.fsync(fd)
        finally:
            os.close(fd)
        # Cursor authority is file size, even if publication fails after append.
        _write(
            folder / "meta.json",
            {
                key: value
                for key, value in current.items()
                if key not in {"log_path", "byte_end"}
            }
            | {"updated_at": time.time()},
        )
        return _info(folder)


def update_status(study_root, stream_id, **status):
    if set(status) - {
        "status",
        "error",
        "remote_size",
        "remote_offset",
        "remote_identity",
        "updated_at",
    }:
        raise ValueError("Unsupported telemetry status fields")
    if "status" in status and not isinstance(status["status"], str):
        raise ValueError("Telemetry status must be a string")
    if (
        "error" in status
        and status["error"] is not None
        and not isinstance(status["error"], str)
    ):
        raise ValueError("Telemetry error must be a string or null")
    for field in ("remote_size", "remote_offset"):
        if field in status and (type(status[field]) is not int or status[field] < 0):
            raise ValueError(f"{field} must be a nonnegative integer")
    if "remote_identity" in status and (
        not isinstance(status["remote_identity"], list)
        or len(status["remote_identity"]) != 2
        or any(
            type(value) is not int or value < 0 for value in status["remote_identity"]
        )
    ):
        raise ValueError("Remote identity must be a [device, inode] integer pair")
    folder = _stream(study_root, stream_id)
    with _locked(study_root):
        current = _info(folder)
        _write(
            folder / "meta.json",
            {
                key: value
                for key, value in current.items()
                if key not in {"log_path", "byte_end"}
            }
            | _observed(status)
            | {"updated_at": time.time()},
        )
        return _info(folder)


def read_events(
    study_root, stream_id, *, after=0, until=None, tag=None, max_bytes=131072, limit=200
):
    """Read a bounded byte page; incomplete trailing lines remain before the cursor."""
    current = info(study_root, stream_id)
    actual = current["byte_end"]
    bound = actual if until is None else until
    if (
        any(type(value) is not int for value in (after, bound, max_bytes, limit))
        or not 0 <= after <= bound <= actual
        or max_bytes < MAX_EVENT_BYTES
        or limit < 1
    ):
        raise ValueError("Invalid telemetry byte bounds/page limits")
    requested_after = after
    with Path(current["log_path"]).open("rb") as handle:
        if after:
            handle.seek(after - 1)
            continuation = handle.read(1) != b"\n"
        else:
            continuation = False
        handle.seek(after)
        data = handle.read(min(max_bytes, bound - after))
    skipped = []
    if continuation and data:
        newline = data.find(b"\n")
        consumed = len(data) if newline < 0 else newline + 1
        skipped.append(
            {
                "offset": after,
                "end_offset": after + consumed,
                "error": "Discarded continuation of oversized or misaligned event record",
            }
        )
        after, data = after + consumed, data[consumed:]
    page = _page(current, data, after, bound, tag=tag, limit=limit)
    complete_end = after + data.rfind(b"\n") + 1
    fragment_end = after + len(data)
    # A valid event includes its newline in the 8192-byte limit. Once this
    # fragment reaches that size it cannot become a valid record. Advance only
    # when all earlier complete records in this page were already consumed.
    if (
        page["next_offset"] == complete_end
        and fragment_end - complete_end >= MAX_EVENT_BYTES
    ):
        page["errors"].append(
            {
                "offset": complete_end,
                "end_offset": fragment_end,
                "error": "Oversized incomplete event fragment; raw bytes retained",
            }
        )
        page["next_offset"] = fragment_end
    page["errors"] = skipped + page["errors"]
    page["after"] = requested_after
    return page


def _records(current, data, after):
    cursor = after
    for content in data.split(b"\n")[:-1]:
        line = content + b"\n"
        end = cursor + len(line)
        try:
            if len(line) > MAX_EVENT_BYTES:
                raise ValueError("Event exceeds 8192 bytes")
            event = {
                **_event(json.loads(line)),
                "offset": cursor,
                "end_offset": end,
                "source": current["source"],
            }
            error = None
        except (ValueError, TypeError, UnicodeError, OverflowError):
            event = None
            error = {
                "offset": cursor,
                "end_offset": end,
                "error": "Malformed complete event record",
            }
        yield event, error, end
        cursor = end


def _page(current, data, after, bound, *, tag=None, limit=200):
    events, errors, cursor = [], [], after
    for event, error, cursor in islice(_records(current, data, after), limit):
        if error is not None:
            errors.append(error)
        elif tag is None or tag in event["tags"]:
            events.append(event)
    return {
        "version": 1,
        "stream_id": current["stream_id"],
        "source": current["source"],
        "events": events,
        "errors": errors,
        "after": after,
        "next_offset": cursor,
        "byte_end": bound,
    }


def _tail_page(current, data, after, bound, *, tag=None, limit=200):
    # Selection happens after parsing this one bounded byte window: internal
    # observations do not consume visible-event slots, and newest points win.
    events, errors = deque(maxlen=limit), deque(maxlen=min(limit, 200))
    matching, malformed, cursor = 0, 0, after
    for event, error, cursor in _records(current, data, after):
        if error is not None:
            malformed += 1
            errors.append(error)
        elif tag is None or tag in event["tags"]:
            matching += 1
            events.append(event)
    return {
        "version": 1,
        "stream_id": current["stream_id"],
        "source": current["source"],
        "events": list(events),
        "errors": list(errors),
        "after": after,
        "next_offset": cursor,
        "byte_end": bound,
        "omitted_events": matching - len(events),
        "omitted_errors": malformed - len(errors),
    }


def snapshot(study_root, *, experiment_id=None, tag=None, limit=100):
    if type(limit) is not int or limit < 1:
        raise ValueError("Snapshot stream limit must be positive")
    rows = []
    folder = Path(study_root).resolve() / "telemetry"
    for path in sorted(folder.glob("*/meta.json")):
        current = _info(path.parent)
        if experiment_id is None or current["source"]["experiment_id"] == experiment_id:
            rows.append(current)
    rows.sort(
        key=lambda item: (item.get("updated_at", 0), item["stream_id"]),
        reverse=True,
    )
    streams = []
    for current in rows[:limit]:
        preview = _tail_unlocked(current, tag=tag)
        streams.append(
            {
                **current,
                "events": preview["events"],
                "errors": preview["errors"],
                "omitted_events": preview["omitted_events"],
                "omitted_errors": preview["omitted_errors"],
                "preview_after": preview["after"],
                "next_offset": preview["next_offset"],
            }
        )
    return {"version": 1, "streams": streams}


def freeze(study_root, experiment_id, destination):
    """Publish an immutable prefix manifest, retaining all raw tags and byte bounds."""
    destination = Path(destination)
    if destination.is_dir():
        destination = destination / "telemetry.json"
    with _locked(study_root) as folder:
        if destination.exists():
            value = json.loads(destination.read_text())
            if (
                value.get("version") != 1
                or value.get("experiment_id") != experiment_id
                or value.get("study_root") != str(Path(study_root).resolve())
            ):
                raise ValueError(
                    "Frozen telemetry destination belongs to another context"
                )
            return value
        streams = []
        for path in sorted(folder.glob("*/meta.json")):
            current = _info(path.parent)
            if current["source"]["experiment_id"] == experiment_id:
                streams.append(current)
        # Do not call lock-taking read_events while this store lock is held.
        manifest = {
            "version": 1,
            "study_root": str(Path(study_root).resolve()),
            "experiment_id": experiment_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "streams": streams,
        }
        for current in streams:
            current["preview"] = _tail_unlocked(current)
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = _json(manifest) + b"\n"
        temporary = destination.with_name(
            destination.name + "." + uuid.uuid4().hex + ".tmp"
        )
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            if os.write(fd, data) != len(data):
                raise OSError("Incomplete frozen telemetry manifest")
            os.fsync(fd)
            os.link(temporary, destination)
        finally:
            os.close(fd)
            temporary.unlink(missing_ok=True)
        return manifest


def _tail_unlocked(current, *, tag=None, limit=200):
    """Read at most 128 KiB, skipping only a fragment at the tail's left edge."""
    end = current["byte_end"]
    start = max(0, end - 131072)
    with Path(current["log_path"]).open("rb") as handle:
        if start:
            handle.seek(start - 1)
            boundary = handle.read(1) == b"\n"
        else:
            boundary = True
        handle.seek(start)
        raw = handle.read(end - start)
    if not boundary:
        cut = raw.find(b"\n")
        if cut < 0:
            start, raw = end, b""
        else:
            start, raw = start + cut + 1, raw[cut + 1 :]
    return _tail_page(current, raw, start, end, tag=tag, limit=limit)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--stream", required=True)
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--until", type=int)
    parser.add_argument("--tag")
    parser.add_argument("--max-bytes", type=int, default=131072)
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            read_events(
                args.study,
                args.stream,
                after=args.after,
                until=args.until,
                tag=args.tag,
                max_bytes=args.max_bytes,
                limit=args.limit,
            ),
            ensure_ascii=False,
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
