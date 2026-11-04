"""Runtime format boundary shared with the legacy entry points."""

import json
import os
from pathlib import Path


class RuntimeFormatError(ValueError):
    pass


def require_legacy(root: Path) -> None:
    """Fail closed even if initialization stopped before publishing its marker."""
    root = Path(root)
    if any(
        os.path.lexists(root / name) for name in ("runtime.json", "runtime.sqlite3")
    ):
        raise RuntimeFormatError(
            "This Study uses the SQLite runtime; the legacy controller and task "
            "queue cannot read or modify it. Use the matching runtime version."
        )


def runtime_marker(root: Path):
    """Read only the small authority marker; an incomplete handoff is closed."""
    path = Path(root) / "runtime.json"
    if not os.path.lexists(path):
        return None
    if path.is_symlink():
        raise RuntimeFormatError("Runtime marker must not be a symlink")
    try:
        marker = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeFormatError("Runtime handoff marker is unreadable") from exc
    if (
        not isinstance(marker, dict)
        or marker.get("version") != 1
        or marker.get("format") != "research-loop-sqlite"
        or marker.get("state") != "committed"
        or marker.get("database") != "runtime.sqlite3"
    ):
        raise RuntimeFormatError(
            "Runtime handoff is incomplete; resume the same explicit import"
        )
    return marker
