"""Explicit frozen operator control inputs, never dataset or checkpoint readers.

Each source is an immutable JSON object. Denied/other-role sources are metadata
only; their bodies and file metadata are never read. Eligible objects may be
omitted when optional but cannot be rewritten or summarized. Callers supply the
existing control-record reader; all body reads happen outside SQLite writers.
"""

import json
import re

from .operation_contract import fields, json_value
from .role_access import ROLES, RoleAccess
from .store import ConflictError

_FIELDS = {"name", "ref", "identity", "revision", "roles", "allow_feedback", "required"}
_METADATA = {"id", "key", "ref", "revision", "reason", "allow_feedback"}


def descriptors(config, role):
    """Validate detached config and derive existing ContextSnapshot metadata."""
    entries = json_value(config.get("context_sources", []))
    if not isinstance(entries, list):
        raise ValueError("context_sources must be an explicit list")
    access = RoleAccess.resolve(config, role)
    result, names = [], set()
    for entry in entries:
        fields(entry, _FIELDS, "operator context source")
        name = entry["name"]
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name)
            or name in names
        ):
            raise ValueError("Context source names must be unique safe names")
        names.add(name)
        if not isinstance(entry["ref"], str) or not re.fullmatch(
            r"artifacts/[0-9a-f]{32}\.json", entry["ref"]
        ):
            raise ValueError(
                "Context source requires an immutable control artifact ref"
            )
        fields(
            entry["identity"],
            {"device", "inode", "size", "mtime_ns"},
            "context source identity",
        )
        if any(
            type(value) is not int or value < 0 for value in entry["identity"].values()
        ):
            raise ValueError("Invalid context source stat identity")
        roles = entry["roles"]
        if (
            not isinstance(roles, list)
            or not roles
            or any(not isinstance(item, str) or item not in ROLES for item in roles)
            or len(set(roles)) != len(roles)
        ):
            raise ValueError("Context source roles must be a unique native-role subset")
        if (
            type(entry["required"]) is not bool
            or type(entry["allow_feedback"]) is not bool
        ):
            raise ValueError("Context source feedback/required flags must be boolean")
        eligible = access.source_allowed(entry)
        reason = (
            "Frozen operator control source"
            if eligible
            else "Operator source denied for feedback"
            if not entry["allow_feedback"]
            else "Operator source not assigned to this role"
        )
        key = "provided:" + name
        result.append(
            (
                entry,
                dict(
                    id=key,
                    key=key,
                    ref=entry["ref"],
                    revision=entry["revision"],
                    reason=reason,
                    allow_feedback=eligible,
                ),
            )
        )
    return result


def extend(config, role, read_record, inputs, sources, protected, allowed):
    """Append selected objects and denied placeholders to one local snapshot."""
    for entry, source in descriptors(config, role):
        if role in config.get("role_access", {}) and not source["allow_feedback"]:
            continue
        key = source["key"]
        if key in inputs:
            raise ValueError("Operator source collides with an existing context key")
        value = None
        if source["allow_feedback"]:
            value = read_record(entry["ref"], entry["identity"])
            if not isinstance(value, dict):
                raise ValueError("Operator context artifact body must be a JSON object")
            value = json_value(value)
            if entry["required"]:
                protected.append(key)
            if entry["ref"] not in allowed:
                allowed.append(entry["ref"])
        inputs[key] = value
        sources.append(source)


def validate_package(config, role, package, read_record):
    """Independently bind provided content and capability metadata to config."""
    entries = descriptors(config, role)
    context, manifest = package.context, package.manifest
    provided = {
        source["key"]: source
        for source in manifest["sources"]
        if source["key"].startswith("provided:")
    }
    refs = {entry["ref"] for entry, _ in entries}
    if role in config.get("role_access", {}):
        entries = [
            (entry, source) for entry, source in entries if source["allow_feedback"]
        ]
    expected = {source["key"] for _, source in entries}
    if (
        set(provided) != expected
        or {key for key in context if key.startswith("provided:")} - expected
    ):
        raise ConflictError("Unknown or missing configured operator context source")
    allowed = {entry["ref"] for entry, source in entries if source["allow_feedback"]}
    if role in config.get("role_access", {}):
        allowed &= {
            s["ref"] for s in manifest["sources"] if s["disposition"] != "omitted"
        }
    if set(manifest["allowed_evidence"]) & refs != allowed:
        raise ConflictError("Operator source evidence permissions differ from config")
    for entry, source in entries:
        key, actual = source["key"], provided[source["key"]]
        if json.dumps(
            {field: actual[field] for field in _METADATA}, sort_keys=True
        ) != json.dumps(source, sort_keys=True):
            raise ConflictError("Operator source metadata differs from frozen config")
        eligible = source["allow_feedback"]
        if not eligible:
            if (
                key in context
                or actual["disposition"] != "omitted"
                or key in manifest["protected_keys"]
            ):
                raise ConflictError("Denied operator source cannot provide context")
            continue
        if entry["required"] and (
            key not in manifest["protected_keys"] or key not in context
        ):
            raise ConflictError("Required operator source must remain protected")
        if key not in context:
            if actual["disposition"] != "omitted":
                raise ConflictError("Missing operator source is not explained")
            continue
        if actual["disposition"] != "provided":
            raise ConflictError("Operator source cannot be truncated or rewritten")
        original = read_record(entry["ref"], entry["identity"])
        if not isinstance(original, dict) or json.dumps(
            json_value(original), sort_keys=True
        ) != json.dumps(context[key], sort_keys=True):
            raise ConflictError(
                "Operator source differs from its frozen control artifact"
            )


def check_metadata(config, role, check_identity):
    """Cheap commit-time identity rechecks only, never source body reads."""
    for entry, source in descriptors(config, role):
        if source["allow_feedback"]:
            check_identity(entry["ref"], entry["identity"])
