"""Controller-issued role contexts. Only trusted builders issue a new grant."""

import json
import stat
from pathlib import Path

from .context import ContextPackage
from .files import _sync_directory, publish_artifact, publish_json
from .role_access import RoleAccess
from .store import ConflictError


def _path(root, attempt_id):
    # The attempt comes from the fenced SQLite reservation, not the input package.
    return Path(root) / "context-grants" / (attempt_id + ".json")


def _read(root, attempt_id):
    path = _path(root, attempt_id)
    if path.parent.is_symlink():
        raise ConflictError("Invalid context grant directory")
    try:
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode) or mode & 0o222:
            raise ConflictError("Invalid context grant file")
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return None


def _require_grant(root, record):
    grant = _read(root, record["attempt"]["id"])
    if grant is None:
        raise ConflictError(
            "Configured role requires a controller-issued context grant"
        )
    binding = record["binding"]
    if binding and (
        binding["context_ref"] != grant["ref"]
        or binding["identity"] != grant["identity"]
    ):
        raise ConflictError("Binding differs from controller-issued role scope")
    return grant


def _issue_context(reader, record, build):
    """Freeze the first trusted selection, including exact evidence refs, for this attempt."""
    access = RoleAccess.resolve(record["control"]["config"], record["attempt"]["role"])
    if not access.configured:
        return build()
    root = reader.coordinator.store.root
    grant = _read(root, record["attempt"]["id"])
    if grant is None:
        retry = record.get("retry")
        if retry and retry["source_context_ref"]:
            previous = _read(root, retry["source_attempt_id"])
            if not previous or previous != {
                "ref": retry["source_context_ref"],
                "identity": retry["source_context_identity"],
            }:
                raise ConflictError("Review retry requires an issued original context")
        package = build()
        ref = publish_artifact(root, package.to_dict())
        from .updates import _identity

        grant = {"ref": ref, "identity": _identity(root, ref)}
        path = _path(root, record["attempt"]["id"])
        if not path.parent.exists():
            path.parent.mkdir(mode=0o700, exist_ok=True)
            _sync_directory(Path(root))
        try:
            publish_json(path, grant)
            return package
        except FileExistsError:
            grant = _require_grant(root, record)
    return ContextPackage.from_dict(
        reader._read_record(grant["ref"], grant["identity"])
    )
