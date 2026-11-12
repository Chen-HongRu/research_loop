"""The shared application interface for CLI, Codex and future UI clients."""

import json
import os
import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from . import environment, queries
from .configuration import (
    prepare_configuration,
    read_configuration,
    resolve_configuration,
)
from .group_router import ACTIONS, DECISION_SCHEMA
from .interventions import submit_intervention, get_intervention
from .protocol import load_workflow
from .state import PublicationStore, now, write_json


def configuration(root, revision=None):
    root = Path(root).resolve(strict=True)
    revision = revision or json.loads((root / "active.json").read_text())["revision"]
    if Path(revision).name != revision:
        raise ValueError("Invalid configuration revision")
    config = read_configuration(root / "configurations" / revision)
    identity = json.loads((root / "run.json").read_text())
    return {
        **config,
        "project": identity["project"],
        "input_refs": identity["input_refs"],
    }


def _snapshot_implementation(root, resolved):
    destination = root / "implementation/src"
    destination.mkdir(parents=True)
    packages = {Path(__file__).parent}
    for name, source in resolved["metadata"]["files"].items():
        if not (name == "workflow" or name.startswith("group-")):
            continue
        package = Path(source).parent
        if not (package / "__init__.py").exists():
            continue
        while (package.parent / "__init__.py").exists():
            package = package.parent
        packages.add(package)
    for package in packages:
        if (destination / package.name).exists():
            raise ValueError("Managed packages must have unique import names")
        _copy_package(package, destination / package.name)
    metadata = {
        "python": sys.executable,
        "prefix": sys.prefix,
        "package_version": version("research-loop"),
        "codex_version": version("openai-codex"),
        "source": str(destination),
        "packages": sorted(p.name for p in packages),
    }
    write_json(root / "implementation/identity.json", metadata)
    return metadata


def _copy_package(source, destination):
    """Freeze Python packages without traversing data, caches or environments."""
    destination.mkdir()
    for path in source.iterdir():
        if path.is_symlink():
            continue
        if path.is_file() and path.suffix == ".py":
            shutil.copyfile(path, destination / path.name)
        elif path.is_dir() and (path / "__init__.py").is_file():
            _copy_package(path, destination / path.name)


def _prepared_process(root):
    identity = json.loads((root / "run.json").read_text())
    implementation = identity["implementation"]
    if not Path(implementation["python"]).is_file():
        raise ValueError("Prepared interpreter is unavailable")
    env = dict(os.environ)
    env["PYTHONPATH"] = implementation["source"]
    return implementation, env


def prepare(source, root, *, project, goal, input_refs=()):
    root = Path(root).resolve()
    project, goal = Path(project).resolve(strict=True), Path(goal).resolve(strict=True)
    if not project.is_dir() or not goal.is_file() or goal.stat().st_size > 1024 * 1024:
        raise ValueError("Provide an existing project and a goal file of at most 1 MiB")
    refs = [str(Path(ref).resolve(strict=True)) for ref in input_refs]
    if any(not Path(ref).is_file() for ref in refs):
        raise ValueError("Initial evidence references must identify files")
    resolved = resolve_configuration(source)
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    (root / "goal.md").write_text(goal.read_text())
    (root / "work/tmp").mkdir(parents=True)
    implementation = _snapshot_implementation(root, resolved)
    revision = "r000001"
    prepare_configuration(source, root / "configurations" / revision, revision=revision)
    write_json(
        root / "run.json",
        {
            "id": uuid4().hex,
            "format": 1,
            "project": str(project),
            "input_refs": refs,
            "created_at": now(),
            "implementation": implementation,
        },
    )
    write_json(root / "active.json", {"revision": revision})
    PublicationStore(root).prepare()
    write_json(
        root / "status.json",
        {
            "status": "prepared",
            "iteration": 1,
            "config_revision": revision,
            "created_at": now(),
        },
    )
    return inspect(root)


def inspect(root, *, revision=None):
    root = Path(root).resolve(strict=True)
    implementation, env = _prepared_process(root)
    if not Path(__file__).resolve().is_relative_to(Path(implementation["source"])):
        result = subprocess.run(
            [
                implementation["python"],
                "-c",
                "import json,sys; from research_loop.application import inspect; "
                "print(json.dumps(inspect(sys.argv[1], revision=sys.argv[2] or None)))",
                str(root),
                revision or "",
            ],
            env=env,
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)
    config = configuration(root, revision)
    workflow = load_workflow(config)
    sample = {
        "runs": [],
        "groups": config["groups"],
        "references": {},
        "allowed_actions": list(ACTIONS),
    }
    allowed = workflow.route(sample)["allowed_actions"]
    return {
        "run": queries.get_run(root),
        "config": config,
        "router": {
            "policy": workflow.router_policy,
            "schema_template": {
                **DECISION_SCHEMA,
                "properties": {
                    **DECISION_SCHEMA["properties"],
                    "action": {"type": "string", "enum": allowed},
                },
            },
            "profile": config["profiles"][config["router"]["profile"]],
            "tools": [],
        },
        "environment_observation": environment.observe(
            config.get("environment"), resource_config=config.get("resource")
        ),
        "agent_permissions": {
            key: {
                "declared": value["tools"],
                "effective_rule": "declaration intersect run authorization; workspace reads are not isolated",
                "writes": "per-Group workspace and native scratch"
                if value["tools"] == "workspace"
                else [],
            }
            for key, value in config["agents"].items()
        },
    }


def start(root, *, resume=False, foreground=False):
    root = Path(root).resolve(strict=True)
    implementation, env = _prepared_process(root)
    state = queries.get_run(root)
    if (
        state["status"] in {"failed", "stopped", "completed", "needs-reconciliation"}
        and not resume
    ):
        raise ValueError(
            "This execution ended; use explicit resume after reconciling its results"
        )
    command = [implementation["python"], "-m", "research_loop.controller", str(root)]
    if resume:
        command.append("--resume")
    with (root / "controller.log").open("ab") as output:
        if foreground:
            subprocess.run(
                command,
                env=env,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                check=True,
            )
            return status(root)
        process = subprocess.Popen(
            command,
            env=env,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return {
        "root": str(root),
        "pid": process.pid,
        "status": "starting",
        "log": str(root / "controller.log"),
        "completion": str(root / "completion.json"),
    }


def intervene(
    root, *, kind, payload=None, target="run", expected_revision=None, identity=None
):
    config = configuration(root)
    target_revision = expected_revision or config["revision"]
    if expected_revision is None and target != "run":
        try:
            target_revision = queries.get_run(root, target)["config_revision"]
        except FileNotFoundError:
            # Persist the request so the controller's rejection is queryable.
            pass
    return submit_intervention(
        root,
        {
            "id": identity or uuid4().hex,
            "kind": kind,
            "payload": payload or {},
            "target": target,
            "expected_revision": target_revision,
        },
    )


def stop(root, *, target="run", identity=None):
    return intervene(root, kind="stop", target=target, identity=identity)


def resume(root):
    return start(root, resume=True)


def revise(root, source, *, affected, expected_revision=None, identity=None):
    root = Path(root).resolve(strict=True)
    current = configuration(root)
    revision = "r-" + uuid4().hex
    prepare_configuration(source, root / "configurations" / revision, revision=revision)
    inspect(root, revision=revision)
    return intervene(
        root,
        kind="revise",
        payload={"revision": revision, "affected": list(affected)},
        expected_revision=expected_revision or current["revision"],
        identity=identity,
    )


status = queries.get_run
__all__ = [
    "prepare",
    "inspect",
    "start",
    "status",
    "intervene",
    "stop",
    "resume",
    "revise",
    "get_intervention",
]
