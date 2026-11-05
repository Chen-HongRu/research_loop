"""Trusted synthetic application launcher for process-control regression tests.

Only the test executable is admitted here. Production filesystem.command and
its installed-Pi checks remain unchanged.
"""

from pathlib import Path
from unittest.mock import patch

from integrations.native_runtime import application, filesystem

APPLICATION_IDENTITY = application.APPLICATION_IDENTITY


def command(executable, workspace, control, state, socket, roots, writable):
    path = Path(executable)
    if not path.is_file() or path.name != "pi-stub":
        raise ValueError("Synthetic launcher requires the test pi-stub")
    return [str(path)]


def invoke(kind, payload, config):
    with patch.object(filesystem, "command", side_effect=command):
        return application.invoke(kind, payload, config)
