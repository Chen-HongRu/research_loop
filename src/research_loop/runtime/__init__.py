"""Transactional runtime foundation; not yet the production Study runner."""

from pathlib import Path

from .files import publish_artifact, publish_json
from .store import StateStore

__all__ = ["StateStore", "initialize", "publish_artifact"]


def initialize(
    root: Path,
    *,
    config: dict,
    max_iterations: int | None = 10,
    max_calls: int | None = 100,
    max_seconds: float = 3600,
    admission_deadline: float | None = None,
) -> StateStore:
    """Create a new SQLite Study without invoking an Agent or backend."""
    root = Path(root).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("SQLite initialization requires a new or empty directory")
    store = StateStore.create(
        root,
        config=config,
        max_iterations=max_iterations,
        max_calls=max_calls,
        max_seconds=max_seconds,
        admission_deadline=admission_deadline,
    )
    publish_json(
        root / "runtime.json",
        {"format": "sqlite-v1", "study_id": store.snapshot()["study_id"]},
    )
    return store
