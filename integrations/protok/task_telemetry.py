"""Read-only locations for telemetry belonging to bound operator long tasks."""

import json
from pathlib import Path
import re

TASK_ID = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"


def manual_locations(study, task_id=None):
    """Yield canonical task/iteration identities without reading workflow outputs."""
    study = Path(study)
    if task_id is not None and not re.fullmatch(TASK_ID, task_id):
        return
    folders = (
        [study / "tasks" / task_id]
        if task_id is not None
        else sorted((study / "tasks").glob("*"))
    )
    for folder in folders:
        if (
            not re.fullmatch(TASK_ID, folder.name)
            or folder.is_symlink()
            or not folder.resolve().is_relative_to(study.resolve())
        ):
            continue
        try:
            values = []
            for name in ("request.json", "binding.json"):
                path = folder / name
                if path.is_symlink() or path.stat().st_size > 1024 * 1024:
                    raise ValueError("Invalid task metadata")
                values.append(json.loads(path.read_text()))
            request, binding = values
            iteration = binding["iteration"]
            child = folder / "study"
            if (
                request["id"] != folder.name
                or request["kind"] != "long_training"
                or not re.fullmatch(r"[0-9]{4,}", iteration)
                or Path(binding["study"]).resolve() != child.resolve()
                or child.is_symlink()
            ):
                continue
            location = child / "iterations" / iteration
            if location.is_symlink() or not location.resolve().is_relative_to(
                child.resolve()
            ):
                continue
            yield folder.name, iteration, location
        except (OSError, ValueError, TypeError, KeyError):
            continue
