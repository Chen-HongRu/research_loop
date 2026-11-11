"""Native session discovery and role attribution from recorded Study metadata."""

from dataclasses import dataclass
from pathlib import Path


def session_paths(root, sessions_dir=None):
    """Discover only native session trees, never other Study JSONL artifacts."""
    root = Path(root)
    directories = (
        [Path(sessions_dir)]
        if sessions_dir is not None
        else [
            root / "native/sessions",
            *root.glob("groups/router/*/native/sessions"),
            *root.glob("group-runs/*/native-runtime/native/sessions"),
        ]
    )
    return sorted(
        {path for directory in directories for path in directory.rglob("*.jsonl")}
    )


@dataclass(frozen=True)
class Identity:
    thread_id: str
    parent: str | None
    task: str
    role: str
    membership_source: str
    role_source: str


def resolve(lead, metadata, host_roles):
    """Resolve recorded threads belonging to a Study and their role evidence.

    Metadata rows contain thread_id, parent and task (a name or path). Callers
    retain their input validation and state lifecycle. Only mapped threads with
    available metadata seed membership; mappings without history do not invent
    measurable activity.
    Duplicate rows may establish ancestry, but the first row supplies identity.
    """
    metadata = list(metadata)
    known = {row["thread_id"] for row in metadata}
    mapped = known & host_roles.keys()
    selected = ({lead} if lead else set()) | mapped
    while True:
        children = {row["thread_id"] for row in metadata if row["parent"] in selected}
        if children <= selected:
            break
        selected |= children

    identities = {}
    for row in metadata:
        tid = row["thread_id"]
        if tid not in selected or tid in identities:
            continue
        host = host_roles.get(tid)
        host = host if isinstance(host, dict) else {}
        task = host.get("task") or row["task"].rsplit("/", 1)[-1]
        if tid == lead and host.get("role") != "router":
            role, role_source = "lead", "lead"
        elif host.get("role"):
            role, role_source = host["role"], "host-role-record"
        else:
            role = next(
                (
                    name
                    for name in (
                        "scout",
                        "proposer",
                        "experimenter",
                        "analyst",
                        "critic",
                    )
                    if task.lower().startswith(name)
                ),
                "unclassified",
            )
            role_source = (
                "inferred-from-task-name" if role != "unclassified" else "unknown"
            )
        identities[tid] = Identity(
            thread_id=tid,
            parent=row["parent"],
            task=task,
            role=role,
            membership_source="lead"
            if tid == lead
            else "host-role-record"
            if tid in mapped
            else "native-descendant",
            role_source=role_source,
        )
    return identities
