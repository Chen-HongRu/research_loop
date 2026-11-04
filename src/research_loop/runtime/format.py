"""Runtime format boundary shared with the legacy entry points."""

from pathlib import Path


class RuntimeFormatError(ValueError):
    pass


def require_legacy(root: Path) -> None:
    """Fail closed even if initialization stopped before publishing its marker."""
    root = Path(root)
    if any((root / name).exists() for name in ("runtime.json", "runtime.sqlite3")):
        raise RuntimeFormatError(
            "This Study uses the SQLite runtime; the legacy controller and task "
            "queue cannot read or modify it. Use the matching runtime version."
        )
