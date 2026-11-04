"""Publish small controller artifacts before accepting their database references."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from pathlib import Path


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_json(path: Path, value: dict) -> None:
    """Durably publish a new file; never replace a previously published file."""
    data = (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode()
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), 0o444)
            os.fsync(handle.fileno())
        # A link publishes only the complete inode and fails if the name exists.
        os.link(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def publish_artifact(root: Path, value: dict) -> str:
    root = Path(root).resolve()
    folder = root / "artifacts"
    folder.mkdir(exist_ok=True)
    if folder.is_symlink():
        raise ValueError("Artifact directory must not be a symlink")
    _sync_directory(root)
    relative = f"artifacts/{uuid.uuid4().hex}.json"
    publish_json(root / relative, value)
    return relative


def validate_artifact_ref(root: Path, reference: str) -> Path:
    """Check a single file's metadata without rereading or hashing its contents."""
    if not isinstance(reference, str) or not re.fullmatch(
        r"artifacts/[0-9a-f]{32}\.json", reference
    ):
        raise ValueError("Expected an immutable artifact reference")
    root = Path(root).resolve()
    path = root / reference
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("Artifact references must not traverse symlinks")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o222:
        raise ValueError("Artifact must be a published read-only regular file")
    return path
