"""Rebuild disposable views and atomically point to one complete generation."""

import os
import uuid

from .files import _sync_directory, publish_json
from .publication import Publisher
from .publication_views import build_views, render_research
from .store import ConflictError, OwnershipError
from .updates import Updater


class ProjectionWriter:
    def __init__(self, coordinator):
        self.coordinator = coordinator

    def rebuild(self):
        """Build from SQLite plus accepted control artifacts, never from old views.

        A concurrent publication leaves the previous pointer intact and raises a
        conflict. The caller may rebuild against the new snapshot. Incomplete or
        obsolete directories are disposable; no accepted record points to them.
        """
        owner = self.coordinator
        snapshot = Publisher(owner).snapshot()
        config = owner.snapshot()["config"]
        global_evaluation = config.get("global_evaluation")
        if not isinstance(global_evaluation, dict):
            raise ValueError("View reconstruction requires config.global_evaluation")
        reader = Updater(owner, validators={})
        publications = {
            row["id"]: reader._read_record(row["payload_ref"], row["identity"])[
                "normalized_output"
            ]
            for row in snapshot["publications"]
        }
        frontiers = build_views(snapshot, global_evaluation)
        research = render_research(snapshot, publications)
        root = owner.store.root
        views = root / "views"
        views.mkdir(exist_ok=True)
        if views.is_symlink():
            raise ValueError("Views directory must not be a symlink")
        folder = views / f"{snapshot['version']}-{uuid.uuid4().hex}"
        folder.mkdir()
        publish_json(folder / "frontiers.json", frontiers)
        with (folder / "research.md").open("xb") as handle:
            handle.write(research.encode("utf-8"))
            handle.flush()
            os.fchmod(handle.fileno(), 0o444)
            os.fsync(handle.fileno())
        pointer = dict(
            version=snapshot["version"],
            frontiers_ref=str((folder / "frontiers.json").relative_to(root)),
            research_ref=str((folder / "research.md").relative_to(root)),
        )
        publish_json(folder / "manifest.json", pointer)
        _sync_directory(folder)
        _sync_directory(views)
        _sync_directory(root)
        # All owner mutations use this mutex. Body generation and filesystem I/O
        # never occur inside a SQLite write transaction.
        with owner._mutex:
            owner._live()
            with owner.store._connection(readonly=True) as conn:
                conn.execute("BEGIN")
                control = conn.execute(
                    "SELECT owner_epoch,owner_open FROM control"
                ).fetchone()
                version = conn.execute(
                    "SELECT COALESCE(MAX(id),0) FROM publications"
                ).fetchone()[0]
            if control["owner_epoch"] != owner.owner_epoch or not control["owner_open"]:
                raise OwnershipError("Coordinator epoch is stale")
            if version != snapshot["version"]:
                raise ConflictError("Publication advanced while rebuilding views")
            for row in snapshot["publications"]:
                reader._check_identity(row["payload_ref"], row["identity"])
            temporary = views / f".current-{uuid.uuid4().hex}.tmp"
            try:
                publish_json(temporary, pointer)
                os.replace(temporary, views / "current.json")
                _sync_directory(views)
            finally:
                temporary.unlink(missing_ok=True)
        return pointer
