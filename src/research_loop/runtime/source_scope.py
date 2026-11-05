"""Frozen disclosure grants for derivatives of immutable operator inputs.

These references identify control artifacts; they never permit opening an
artifact path. Peer mounts may share derivatives only of universally granted
sources. All grant resolution reads configuration metadata, never source bodies.
"""

from copy import deepcopy
import json
import re

from .role_access import ROLES, RoleAccess

_ARTIFACT = re.compile(r"artifacts/([0-9a-f]{32})\.json\Z")


def source_reference(library_id, source):
    match = _ARTIFACT.fullmatch(source.get("ref", ""))
    if match is None:
        raise ValueError("Operator source requires a fixed control artifact")
    return dict(
        library_id=library_id,
        kind="source",
        id=match[1],
        revision=json.dumps(source["revision"], sort_keys=True, allow_nan=False),
    )


def _granted(config, role, library_id):
    access = RoleAccess.resolve(config, role)
    return {
        json.dumps(source_reference(library_id, entry), sort_keys=True)
        for entry in config.get("context_sources", [])
        if access.source_allowed(entry)
    }


def directory_source_scope(library, config, role, bindings):
    """Resolve grants from directory metadata, bounded by any supplied authority."""
    grants = set()
    for item in bindings:
        library_id = item["library_id"]
        if item["root"] == library.root:
            grants.update(_granted(config, role, library_id))
            continue
        with library._connection(item["root"], readonly=True) as conn:
            library._check_library_identity(conn, library_id)
            if (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='control'"
                ).fetchone()
                is None
            ):
                continue
            row = conn.execute(
                "SELECT config_json FROM control WHERE singleton=1"
            ).fetchone()
        if row is None:
            continue
        origin = json.loads(row["config_json"])
        universal = [_granted(origin, name, library_id) for name in ROLES]
        grants.update(set.intersection(*universal))
    if library._source_scope_supplied:
        grants.intersection_update(library._source_scope)
    return [json.loads(value) for value in sorted(grants)]


def freeze_source_scope(library, config, role, manifest):
    """Resolve a private maintenance/history scope, never an Agent read snapshot."""
    return directory_source_scope(
        library, config, role, list(library._manifest(manifest).values())
    )


def input_dependencies(assembly, library_id):
    """Retain all actually supplied operator inputs even when outputs omit citations."""
    return [
        source_reference(library_id, source)
        for source in assembly["input_sources"]
        if source["key"].startswith("provided:")
        and source["key"] in assembly["inputs"]
        and source["disposition"] != "omitted"
    ]


def derived_dependencies(assembly, library_id):
    result = deepcopy(assembly.get("inherited_dependencies", []))
    for reference in assembly.get("knowledge_dependencies", []):
        if reference not in result:
            result.append(deepcopy(reference))
    for reference in input_dependencies(assembly, library_id):
        if reference not in result:
            result.append(reference)
    return result
