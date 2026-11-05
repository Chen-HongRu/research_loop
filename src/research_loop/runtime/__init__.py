"""Transactional runtime foundation; not yet the production Study runner."""

from pathlib import Path

from .files import publish_artifact, publish_json
from .store import StateStore

__all__ = ["StateStore", "initialize", "publish_artifact"]


def initialize(
    root: Path,
    *,
    config: dict,
    **limits,
) -> StateStore:
    """Create a new SQLite Study without invoking an Agent or backend."""
    root = Path(root).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("SQLite initialization requires a new or empty directory")
    store = StateStore.create(root, config=config, **limits)
    publish_json(
        root / "runtime.json",
        {"format": "sqlite-v1", "study_id": store.snapshot()["study_id"]},
    )
    return store
