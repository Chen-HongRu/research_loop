"""Generic bounded output staging, atomic publication and receipt recovery."""

import asyncio
import json
import os
import shutil
import stat
from pathlib import Path, PurePosixPath

from .interventions import get_intervention, record_intervention
from .state import PublicationStore, iteration_directory, now, record_status, write_json

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_SELECTED_BYTES = 16 * 1024 * 1024


def _source_bytes(source):
    path = Path(source)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Publication sources must be regular files, not links")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Publication sources must be regular files")
        if metadata.st_size > MAX_FILE_BYTES:
            raise ValueError("A publication file must be at most 4 MiB")
        content = stream.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("A publication file must be at most 4 MiB")
    return content


def _proposal(proposal):
    if not isinstance(proposal, dict) or set(proposal) - {
        "action",
        "result",
        "artifacts",
        "input_outcome",
    }:
        raise ValueError("Invalid publication proposal fields")
    if proposal.get("action") not in {"continue", "stop"} or not isinstance(
        proposal.get("result"), dict
    ):
        raise ValueError("Publication requires an action and JSON object result")
    artifacts = proposal.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("Publication artifacts must be a list")
    # Only explicitly selected small outputs are read, once, to stage immutable
    # bytes. No traversal or hashing of source datasets/caches is performed.
    entries, destinations, total = [], set(), 0
    canonical = {
        "action": proposal["action"],
        "result": proposal["result"],
        "artifacts": [],
    }
    for entry in artifacts:
        if not isinstance(entry, dict) or set(entry) not in (
            {"source", "destination"},
            {"content", "destination"},
        ):
            raise ValueError(
                "Each artifact requires a destination and source or text content"
            )
        destination = entry["destination"]
        if (
            not isinstance(destination, str)
            or not destination
            or "\x00" in destination
            or PurePosixPath(destination).is_absolute()
            or any(part in {"", ".", ".."} for part in destination.split("/"))
        ):
            raise ValueError(
                "Publication destinations must be relative paths within the output tree"
            )
        path = PurePosixPath(destination)
        if destination in destinations or any(
            path in other.parents or other in path.parents
            for other in map(PurePosixPath, destinations)
        ):
            raise ValueError(
                "Publication destinations must be unique without file/directory conflicts"
            )
        destinations.add(destination)
        if "content" in entry:
            if not isinstance(entry["content"], str):
                raise ValueError("Publication content must be text")
            if len(entry["content"]) > MAX_FILE_BYTES:
                raise ValueError("A publication file must be at most 4 MiB")
            content = entry["content"].encode("utf-8")
            normalized = dict(entry)
        else:
            source = entry["source"]
            if not isinstance(source, (str, os.PathLike)) or not str(source):
                raise ValueError("Publication source must be a file path")
            content = _source_bytes(source)
            normalized = {
                "source": str(Path(source).absolute()),
                "destination": destination,
            }
        if len(content) > MAX_FILE_BYTES:
            raise ValueError("A publication file must be at most 4 MiB")
        total += len(content)
        if total > MAX_SELECTED_BYTES:
            raise ValueError("Selected publication artifacts must total at most 16 MiB")
        entries.append((destination, content))
        canonical["artifacts"].append(normalized)
    if "input_outcome" in proposal:
        outcome = proposal["input_outcome"]
        if (
            not isinstance(outcome, dict)
            or set(outcome) != {"id", "status"}
            or not all(
                isinstance(outcome[key], str) and outcome[key]
                for key in ("id", "status")
            )
        ):
            raise ValueError("Input outcome requires an ID and status")
        canonical["input_outcome"] = dict(outcome)
    return json.loads(json.dumps(canonical, allow_nan=False)), entries


def _stage(store, baseline, entries):
    if str(store.current.resolve()) != baseline:
        raise RuntimeError("Published outputs changed since this proposal began")
    if store.draft.is_symlink() or not store.draft.is_dir():
        raise ValueError("Publication draft must be a real directory")
    # A failed or directly edited draft is not an additional proposal. Rebuild
    # from the active small output tree, then add only declared artifacts.
    shutil.rmtree(store.draft)
    shutil.copytree(store.current.resolve(), store.draft)
    for destination, content in entries:
        path = store.draft / destination
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not path.is_file():
            raise ValueError("Publication artifact would replace an output directory")
        path.write_bytes(content)


def _project_outcome(root, publication):
    outcome = publication.get("input_outcome")
    if outcome is None:
        return
    current = get_intervention(root, outcome["id"])
    desired = {
        "work_outcome": outcome["status"],
        "publication": publication["publication_version"],
    }
    if current["status"] == "applied" and all(
        current.get(key) == value for key, value in desired.items()
    ):
        return
    record_intervention(root, outcome["id"], "applied", **desired)


def _save_publication(root, state):
    write_json(root / "result.json", state["result"])
    write_json(
        iteration_directory(root, state["iteration"]) / "result.json", state["result"]
    )
    record_status(root, state)


async def commit(root, state, baseline, proposal):
    """Publish declared settled outputs; the active pointer is the commit fact."""
    root = Path(root)
    proposal, entries = await asyncio.to_thread(_proposal, proposal)
    outcome = proposal.get("input_outcome")
    if (
        outcome is not None
        and get_intervention(root, outcome["id"])["status"] == "rejected"
    ):
        raise ValueError("Cannot publish work for a rejected intervention")
    publication = {
        **state,
        "action": proposal["action"],
        "result": proposal["result"],
        "proposal": proposal,
        "status": "iteration-completed"
        if proposal["action"] == "continue"
        else "completed",
        "ended_at": now(),
    }
    publication.pop("next_iteration", None)
    publication.pop("input_outcome", None)
    publication.pop("publication_version", None)
    if outcome is not None:
        publication["input_outcome"] = outcome
    if proposal["action"] == "continue":
        publication["next_iteration"] = state["iteration"] + 1
    store = PublicationStore(root)
    await asyncio.to_thread(_stage, store, baseline, entries)
    version = await asyncio.to_thread(store.publish, baseline, publication=publication)
    state.clear()
    state.update(publication, publication_version=version)
    # These small projections stay on the controller's writer task. Filesystem
    # staging/activation can yield, but status and intervention writes serialize.
    _save_publication(root, state)
    _project_outcome(root, state)
    return state


async def recover(root, state):
    """Repair projections from the activated receipt without replaying work."""
    root = Path(root)
    published = PublicationStore(root).published_state()
    if published is None:
        return state
    _project_outcome(root, published)
    if published["iteration"] >= state.get("iteration", 0) and state != published:
        state = published
        _save_publication(root, state)
    return state
