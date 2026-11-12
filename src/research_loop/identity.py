"""Native session discovery and explicit runtime identity attribution."""

from dataclasses import dataclass
from pathlib import Path


def session_paths(root, sessions_dir=None):
    """Discover only managed native histories, including per-Agent runtimes."""
    root = Path(root)
    directories = (
        [Path(sessions_dir)]
        if sessions_dir is not None
        else [
            *root.glob("groups/router/*/native/sessions"),
            *root.glob("group-runs/*/native-runtime/**/native/sessions"),
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
    is_router: bool


def resolve(metadata, mappings):
    """Seed membership from recorded threads; ancestry does not invent roles."""
    metadata = list(metadata)
    known = {row["thread_id"] for row in metadata}
    mapped = known & {tid for tid, value in mappings.items() if isinstance(value, dict)}
    selected = set(mapped)
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
        record = mappings.get(tid)
        record = record if isinstance(record, dict) else {}
        task = record.get("task")
        task = (
            task if isinstance(task, str) and task else row["task"].rsplit("/", 1)[-1]
        )
        role = record.get("role")
        explicit = isinstance(role, str) and bool(role)
        identities[tid] = Identity(
            thread_id=tid,
            parent=row["parent"],
            task=task,
            role=role if explicit else "unclassified",
            membership_source="thread-record" if tid in mapped else "native-descendant",
            role_source="thread-record" if explicit else "unknown",
            is_router=role == "router" and not record.get("group_run_id"),
        )
    return identities
