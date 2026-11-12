"""Strict author declarations and immutable, explicit-file configuration revisions.

Programs' shared imports belong to the controller snapshot, not this file copier.
Data and resource ledgers remain external references and are never copied or read.
"""

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from . import environment

Name = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
Text = Annotated[str, Field(min_length=1)]
_MAX_DECLARATION = 1024 * 1024


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Profile(_Input):
    # An explicit OpenAI identifier is required. This is not an availability probe.
    model: Annotated[
        str,
        Field(
            pattern=r"^(gpt-[A-Za-z0-9][A-Za-z0-9._-]*|o[1-9][0-9]*(?:-[A-Za-z0-9._-]+)?)$"
        ),
    ]
    effort: Literal["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]


class Program(_Input):
    program: Text
    settings: dict[str, JsonValue] = Field(default_factory=dict)


class Router(_Input):
    profile: Name


class Agent(_Input):
    instructions: Text
    profile: Name
    tools: Literal["workspace", "none"]
    output_schema: Text | None = None


class Group(Program):
    version: Name
    description: str
    agents: dict[Name, Name] = Field(default_factory=dict)


class Execution(_Input):
    environment: Text | None = None
    resource: Text | None = None


class Declaration(_Input):
    version: Annotated[int, Field(ge=1, le=1)]
    workflow: Program
    router: Router
    profiles: dict[Name, Profile]
    agents: dict[Name, Agent]
    groups: dict[Name, Group]
    execution: Execution = Field(default_factory=Execution)


class Resource(_Input):
    host: Text
    partition: Text
    account: Text | None = None
    remote_root: Text
    gpu_seconds_total: Annotated[int, Field(ge=1)]
    job_seconds: Annotated[int, Field(ge=60, multiple_of=60)]
    cpus: Annotated[int, Field(ge=1)] = 4
    grace_seconds: Annotated[int, Field(ge=0)] = 30
    memory: Annotated[str, Field(pattern=r"^[1-9][0-9]*[KMGTP]?$")] = "48G"
    ledger_path: Text


class _Loader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in result:
            raise ValueError(f"Duplicate YAML field: {key}")
        result[key] = loader.construct_object(value_node)
    return result


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _small(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file() or path.stat().st_size > _MAX_DECLARATION:
        raise ValueError("Configuration declarations must be files of at most 1 MiB")
    return path


def _json_file(path):
    value = json.loads(_small(path).read_text())
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    # Reject non-finite values which JSON's default parser accepts.
    json.dumps(value, allow_nan=False)
    return value


def _resolve(source):
    source = _small(source)
    declaration = Declaration.model_validate(
        yaml.load(source.read_text(), Loader=_Loader)
    )
    files = {"declaration": str(source)}

    def reference(value, label):
        path = _small(source.parent / value)
        files[label] = str(path)
        return path

    def profile(name):
        if name not in declaration.profiles:
            raise ValueError(f"Unknown profile: {name}")

    profile(declaration.router.profile)
    agents = {}
    for name, agent in declaration.agents.items():
        profile(agent.profile)
        agents[name] = {
            "instructions": reference(
                agent.instructions, f"agent-{name}-instructions"
            ).read_text(),
            "profile": agent.profile,
            "tools": agent.tools,
            "output_schema": _json_file(
                reference(agent.output_schema, f"agent-{name}-schema")
            )
            if agent.output_schema
            else None,
        }
    groups = []
    for name, group in declaration.groups.items():
        for agent in group.agents.values():
            if agent not in agents:
                raise ValueError(f"Unknown agent: {agent}")
        groups.append(
            {
                "id": name,
                **group.model_dump(),
                "program": str(reference(group.program, f"group-{name}")),
            }
        )
    execution = declaration.execution
    environment_config = (
        environment.read_config(reference(execution.environment, "environment"))
        if execution.environment
        else None
    )
    resource = None
    if execution.resource:
        path = reference(execution.resource, "resource")
        resource = Resource.model_validate(_json_file(path)).model_dump()
        for field in ("host", "partition", "account"):
            if resource[field] is not None and not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", resource[field]
            ):
                raise ValueError(f"Invalid resource {field}")
        if not resource["remote_root"].startswith("/") or any(
            c in resource["remote_root"] for c in "\n\r\x00"
        ):
            raise ValueError("Resource remote_root must be an absolute POSIX path")
        resource["ledger_path"] = str((path.parent / resource["ledger_path"]).resolve())
    result = {
        "version": 1,
        "revision": None,
        "workflow": {
            **declaration.workflow.model_dump(),
            "program": str(reference(declaration.workflow.program, "workflow")),
        },
        "router": declaration.router.model_dump(),
        "profiles": {
            name: value.model_dump() for name, value in declaration.profiles.items()
        },
        "agents": agents,
        "groups": groups,
        "environment": environment_config,
        "resource": resource,
        "metadata": {"source": str(source), "files": files},
    }
    json.dumps(result, allow_nan=False)
    return result


def resolve_configuration(source: Path) -> dict:
    """Resolve one declaration without writes, execution, or environment probes."""
    return _resolve(source)


def prepare_configuration(source: Path, destination: Path, *, revision: str) -> dict:
    """Atomically publish a new revision directory; never replace an existing one."""
    if not isinstance(revision, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", revision
    ):
        raise ValueError("Invalid configuration revision")
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError(destination)
    result = resolve_configuration(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent)
    )
    try:
        copied = {}
        for label, path in result["metadata"]["files"].items():
            relative = Path("files") / (label + Path(path).suffix)
            target = staging / relative
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(path, target)
            copied[label] = str(relative)
        result["revision"] = revision
        result["workflow"]["program"] = str(destination / copied["workflow"])
        for group in result["groups"]:
            group["program"] = str(destination / copied[f"group-{group['id']}"])
        result["metadata"]["copies"] = copied
        encoded = json.dumps(result, indent=2, allow_nan=False) + "\n"
        if len(encoded.encode()) > _MAX_DECLARATION:
            raise ValueError("Normalized configuration exceeds 1 MiB")
        (staging / "config.json").write_text(encoded)
        (staging / "complete.json").write_text(
            json.dumps({"version": 1, "revision": revision}) + "\n"
        )
        # A populated revision cannot be replaced by rename on supported platforms.
        if destination.exists():
            raise FileExistsError(destination)
        os.rename(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return result


def read_configuration(destination: Path) -> dict:
    """Read a published revision using only its bounded metadata and file stats."""
    destination = Path(destination).resolve()
    result = _json_file(destination / "config.json")
    complete = _json_file(destination / "complete.json")
    if complete != {"version": 1, "revision": result.get("revision")} or not result.get(
        "revision"
    ):
        raise ValueError("Incomplete configuration revision")
    copies = result.get("metadata", {}).get("copies")
    if not isinstance(copies, dict) or not {"declaration", "workflow"} <= copies.keys():
        raise ValueError("Incomplete configuration file manifest")
    for relative in copies.values():
        path = destination / relative
        if not path.resolve().is_relative_to(destination.resolve()):
            raise ValueError("Configuration file escapes its revision")
        _small(path)
    if result["workflow"]["program"] != str(destination / copies["workflow"]):
        raise ValueError("Workflow program is outside its revision")
    for group in result["groups"]:
        if group["program"] != str(destination / copies[f"group-{group['id']}"]):
            raise ValueError("Group program is outside its revision")
    return result
