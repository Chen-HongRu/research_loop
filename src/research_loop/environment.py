"""Frozen interpreter declarations and one local stdlib-only observation."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

OBSERVE = """
import importlib.metadata, json, sys
packages = {}
for distribution in importlib.metadata.distributions():
    name = distribution.metadata.get("Name")
    if name:
        packages[name] = distribution.version
print(json.dumps({"executable": sys.executable, "version": sys.version.split()[0],
                  "prefix": sys.prefix, "base_prefix": sys.base_prefix,
                  "packages": packages}))
"""


def shell_environment(snapshot, work):
    """Select one Python runtime while retaining available host uv/rg tools."""
    local = (
        snapshot["local"]
        if snapshot
        else {
            "executable": sys.executable,
            "prefix": sys.prefix,
            "base_prefix": sys.base_prefix,
        }
    )
    paths = [str(Path(local["executable"]).parent)]
    for tool in ("uv", "rg"):
        executable = shutil.which(tool)
        if executable:
            paths.append(str(Path(executable).absolute().parent))
    paths.extend(os.defpath.split(os.pathsep))
    result = {
        "PATH": os.pathsep.join(dict.fromkeys(paths)),
        "HOME": str(work),
        "TMPDIR": str(Path(work) / "tmp"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    if local["prefix"] != local["base_prefix"]:
        result["VIRTUAL_ENV"] = local["prefix"]
    return result


def read_config(path):
    """Copy a declaration; preserve venv interpreter symlinks as invoked paths."""
    path = Path(path).absolute()
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or set(value) - {"python", "resource"}:
        raise ValueError("Environment requires only python and optional resource")
    python = value.get("python")
    if (
        not isinstance(python, str)
        or not python.strip()
        or any(c in python for c in "\x00\n\r")
    ):
        raise ValueError("Environment python must be an interpreter path")
    python = os.path.abspath(path.parent / python)
    if not Path(python).is_file() or not os.access(python, os.X_OK):
        raise ValueError("Environment python must be an existing executable file")
    result = {"python": python}
    if "resource" in value:
        resource = value["resource"]
        if not isinstance(resource, dict) or set(resource) - {"python", "packages"}:
            raise ValueError(
                "Resource environment requires only python and optional packages"
            )
        remote_python = resource.get("python")
        if (
            not isinstance(remote_python, str)
            or not PurePosixPath(remote_python).is_absolute()
            or any(c in remote_python for c in "\x00\n\r")
        ):
            raise ValueError("Resource python must be an absolute POSIX path")
        packages = resource.get("packages", {})
        if not isinstance(packages, dict) or any(
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name)
            or not isinstance(version, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*", version)
            for name, version in packages.items()
        ):
            raise ValueError(
                "Resource packages must map distribution names to version strings"
            )
        result["resource"] = {"python": remote_python, "packages": packages}
    return result


def observe(settings=None, *, resource_config=None, resource_status=None, tools=()):
    """Observe installed distribution metadata without importing those packages."""
    python = settings["python"] if settings else sys.executable
    observed = subprocess.run(
        [python, "-I", "-c", OBSERVE],
        capture_output=True,
        text=True,
        timeout=15,
        env={"PATH": os.defpath, "HOME": str(Path.home()), "PYTHONNOUSERSITE": "1"},
    )
    if observed.returncode:
        raise RuntimeError(
            f"Execution Python observation failed: {observed.stderr[:1000]}"
        )
    local = json.loads(observed.stdout)
    if not all(
        isinstance(local.get(key), str) and local[key]
        for key in ("executable", "version", "prefix", "base_prefix")
    ) or not isinstance(local.get("packages"), dict):
        raise ValueError("Execution Python returned invalid metadata")
    local["status"] = "observed"
    declared = (settings or {}).get("resource", {})
    return {
        "local": local,
        "resource": {
            "status": "declared-unverified" if declared else "undeclared-unverified",
            "python": declared.get("python"),
            "packages": declared.get("packages", {}),
            "configured": bool(resource_config),
            "partition": (resource_config or {}).get("partition"),
            "budget": (resource_status or {}).get("budget"),
            "tools": [tool["name"] for tool in tools],
        },
    }


def render(snapshot):
    if snapshot is None:
        return ""
    return (
        """Execution environment snapshot (local OBSERVED; resource DECLARED / UNVERIFIED)
Use local.executable for local Python work. Package versions are installed
metadata, not proof of import, runtime or CUDA availability. The resource
interpreter/packages are declarations; no remote environment was probed.
A configured partition, including debug, may run real tests or experiments
within the existing goal, resource authorization and remaining budget. Missing
local CUDA does not block that configured route. Only the coordinator calls
actually registered resource tools; delegates return bounded execution requests.
This snapshot grants no GPU access, installation permission or alternate SSH
route. Current resource receipts remain authoritative for spending/availability.
"""
        + json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
        + "\n"
    )


def prepend(text, snapshot):
    context = render(snapshot)
    return context + "\n" + text if context and context not in text else text
