"""Durable Group runs and settled, file-based output boundaries.

Programs are trusted operator code. Permissions describe the write contract;
this module is not an operating-system sandbox for arbitrary Python programs.
Programs must await their own Python tasks and use services for external work.
"""

import asyncio
import copy
import inspect
import json
import re
import stat
import sys
import types
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .state import now, write_json

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MAX_SOURCE = 1024 * 1024
_MAX_ARTIFACT = 4 * 1024 * 1024
_MAX_TOTAL_ARTIFACTS = 16 * 1024 * 1024
_OUTPUT_FIELDS = {"summary", "completion", "unfinished", "help", "artifacts"}


def _name(value, label):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError(f"Invalid {label}")
    return value


def _json_copy(value):
    return json.loads(json.dumps(value, allow_nan=False))


@dataclass(frozen=True)
class GroupDefinition:
    id: str
    version: str
    description: str
    program: Path
    settings: dict = field(default_factory=dict)

    def __post_init__(self):
        _name(self.id, "Group id")
        _name(self.version, "Group version")
        if not isinstance(self.description, str):
            raise ValueError("Group description must be text")
        if not isinstance(self.settings, dict):
            raise ValueError("Group settings must be an object")
        object.__setattr__(self, "program", Path(self.program).resolve())
        object.__setattr__(self, "settings", _json_copy(self.settings))

    @classmethod
    def from_json(cls, value, base_dir=None):
        if isinstance(value, (str, Path)):
            path = Path(value).resolve()
            base_dir = path.parent
            value = json.loads(path.read_text())
        if not isinstance(value, dict) or set(value) - {
            "id",
            "version",
            "description",
            "program",
            "settings",
        }:
            raise ValueError("Invalid Group definition")
        program = Path(value["program"])
        if not program.is_absolute() and base_dir is not None:
            program = Path(base_dir) / program
        return cls(
            value["id"],
            value["version"],
            value["description"],
            program,
            value.get("settings", {}),
        )

    def metadata(self):
        return {
            "id": self.id,
            "version": self.version,
            "description": self.description,
            "program": "program.py",
            "settings": _json_copy(self.settings),
        }


class GroupContext:
    def __init__(self, runtime, record, definition):
        self.study_root = runtime.study_root
        self.run_id = record["run_id"]
        self.root = runtime.root / self.run_id
        self.work = self.root / "work"
        self.round = record["round"]
        self.definition = definition
        self.previous_output = copy.deepcopy(record.get("output"))
        path = self.root / "state.json"
        self.state = json.loads(path.read_text()) if path.exists() else {}
        self.services = None

    def save_state(self):
        if not isinstance(self.state, dict):
            raise ValueError("Group state must be an object")
        write_json(self.root / "state.json", _json_copy(self.state))


class GroupRuntime:
    """One event-loop owner for isolated Group runs under a study directory."""

    def __init__(self, study_root, definitions, service_factory=None):
        self.study_root = Path(study_root).resolve()
        self.root = self.study_root / "group-runs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.service_factory = service_factory
        self._tasks = {}
        self._contexts = {}
        self._stop_errors = {}
        self._records = {}
        self._events = asyncio.Queue()
        self._stopped = (self.study_root / "STOP").exists()
        self.definitions = {}
        values = (
            definitions.values() if isinstance(definitions, Mapping) else definitions
        )
        for definition in values:
            if not isinstance(definition, GroupDefinition):
                definition = GroupDefinition.from_json(definition)
            if definition.id in self.definitions:
                raise ValueError("Duplicate Group id")
            self.definitions[definition.id] = self._pin_definition(definition)
        for path in sorted(self.root.glob("*/run.json")):
            record = json.loads(path.read_text())
            if record["run_id"] != path.parent.name:
                raise ValueError("Group run identity does not match directory")
            self._records[record["run_id"]] = record
            if record["status"] == "running":
                record.update(
                    status="needs-reconciliation",
                    updated_at=now(),
                    error="Restarted with execution outcome unknown; no automatic retry",
                )
                self._persist(record)

    def _pin_definition(self, definition):
        program = definition.program
        if not program.is_file() or program.stat().st_size > _MAX_SOURCE:
            raise ValueError("Group program must be a small Python source file")
        source = program.read_bytes()
        metadata = definition.metadata()
        destination = (
            self.study_root / "group-definitions" / definition.id / definition.version
        )
        if destination.exists():
            if (
                json.loads((destination / "definition.json").read_text()) != metadata
                or (destination / "program.py").read_bytes() != source
            ):
                raise ValueError("Group code/config changed without a new version")
        else:
            destination.mkdir(parents=True)
            (destination / "program.py").write_bytes(source)
            write_json(destination / "definition.json", metadata)
            (destination / "program.py").chmod(0o444)
            (destination / "definition.json").chmod(0o444)
        return GroupDefinition.from_json(destination / "definition.json")

    def _persist(self, record):
        write_json(self.root / record["run_id"] / "run.json", record)

    def _admit(self):
        if self._stopped or (self.study_root / "STOP").exists():
            raise RuntimeError("Group runtime stopped; no new admission")

    def _refs(self, refs):
        result = []
        for ref in refs:
            path = Path(ref).resolve(strict=True)
            if not path.is_file():
                raise ValueError("Group input references must be files")
            if path.is_relative_to(self.root):
                relative = path.relative_to(self.root)
                if len(relative.parts) < 4 or relative.parts[1] != "exports":
                    raise ValueError("Cross-Run inputs must be exported artifacts")
                run_id, _, round_id = relative.parts[:3]
                metadata = self.root / run_id / "rounds" / round_id / "output.json"
                if (
                    not metadata.is_file()
                    or str(path) not in json.loads(metadata.read_text())["artifacts"]
                ):
                    raise ValueError(
                        "Cross-Run inputs must be published boundary artifacts"
                    )
            result.append(str(path))
        return result

    async def start(self, group_id, task_ref, input_refs, prompt=""):
        self._admit()
        definition = self.definitions[group_id]
        payload = {
            "task_ref": self._refs([task_ref])[0],
            "input_refs": self._refs(input_refs),
            "prompt": prompt,
            "feedback_refs": [],
        }
        payload = _json_copy(payload)
        run_id = uuid4().hex
        root = self.root / run_id
        root.mkdir()
        for name in ("work", "rounds", "exports"):
            (root / name).mkdir()
        (root / "program.py").write_bytes(definition.program.read_bytes())
        write_json(root / "definition.json", definition.metadata())
        (root / "program.py").chmod(0o444)
        (root / "definition.json").chmod(0o444)
        record = {
            "run_id": run_id,
            "group_id": definition.id,
            "version": definition.version,
            "status": "running",
            "round": 1,
            "created_at": now(),
            "updated_at": now(),
            "output": None,
            **copy.deepcopy(payload),
        }
        self._records[run_id] = record
        self._schedule(record, payload)
        return run_id

    async def resume(self, run_id, feedback_refs, prompt=""):
        self._admit()
        record = self._records[run_id]
        if record["status"] != "held":
            raise RuntimeError("Only a held Group run may resume")
        previous = json.loads(
            (
                self.root / run_id / "rounds" / f"{record['round']:06d}" / "input.json"
            ).read_text()
        )
        payload = _json_copy(
            {
                **previous,
                "feedback_refs": self._refs(feedback_refs),
                "prompt": prompt,
            }
        )
        record.update(
            status="running",
            round=record["round"] + 1,
            updated_at=now(),
            feedback_refs=payload["feedback_refs"],
            prompt=payload["prompt"],
        )
        self._schedule(record, payload)
        return run_id

    def _schedule(self, record, payload):
        directory = self.root / record["run_id"] / "rounds" / f"{record['round']:06d}"
        directory.mkdir()
        write_json(directory / "input.json", payload)
        self._persist(record)
        self._tasks[record["run_id"]] = asyncio.create_task(
            self._execute(record, payload)
        )

    async def _execute(self, record, payload):
        ctx = None
        result = None
        error = None
        uncertain = False
        try:
            root = self.root / record["run_id"]
            definition = GroupDefinition.from_json(root / "definition.json")
            ctx = GroupContext(self, record, definition)
            self._contexts[record["run_id"]] = ctx
            if self.service_factory is not None:
                services = self.service_factory(ctx)
                if inspect.isawaitable(services):
                    services = await services
                ctx.services = services
            module = types.ModuleType(f"group_{record['run_id']}_{record['round']}")
            module.__file__ = str(definition.program)
            sys.modules[module.__name__] = module
            try:
                exec(
                    compile(
                        definition.program.read_bytes(), str(definition.program), "exec"
                    ),
                    module.__dict__,
                )
                result = await module.run(ctx, payload)
            finally:
                sys.modules.pop(module.__name__, None)
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if ctx is not None and ctx.services is not None:
                for action in ("settle", "close"):
                    try:
                        value = getattr(ctx.services, action)()
                        if inspect.isawaitable(value):
                            value = await value
                        if value is False:
                            raise RuntimeError(f"Services did not {action}")
                    except BaseException as exc:
                        uncertain = True
                        error = f"{action}: {type(exc).__name__}: {exc}"
        if record["run_id"] in self._stop_errors:
            uncertain = True
            error = self._stop_errors[record["run_id"]]
        try:
            if ctx is not None:
                ctx.save_state()
            if error is None:
                output = self._publish(ctx, result)
                record.update(status="returned", output=output)
            else:
                record.update(
                    status="needs-reconciliation" if uncertain else "failed",
                    error=error,
                )
        except BaseException as exc:
            record.update(
                status="needs-reconciliation" if uncertain else "failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        if record["status"] in {"failed", "needs-reconciliation"}:
            record["output"] = None
        if record["status"] == "failed":
            try:
                record["output"] = self._failure_output(record)
            except Exception as exc:
                record["error"] += (
                    f"; diagnostic publication failed: {type(exc).__name__}: {exc}"
                )
        record["updated_at"] = now()
        self._persist(record)
        self._events.put_nowait(self.observe(record["run_id"]))
        self._contexts.pop(record["run_id"], None)

    def _failure_output(self, record):
        root = self.root / record["run_id"]
        # Runtime diagnostics must remain writable even if the program damaged work.
        work = root / "rounds" / f"{record['round']:06d}" / "runtime-diagnostic"
        work.mkdir()
        message = record["error"][:8192]
        write_json(work / "runtime-error.json", {"message": message})
        ctx = types.SimpleNamespace(root=root, work=work, round=record["round"])
        return self._publish(
            ctx,
            {
                "summary": "Group execution failed",
                "completion": "",
                "unfinished": [message],
                "help": "Diagnose the runtime failure within scope",
                "artifacts": ["runtime-error.json"],
            },
        )

    def _publish(self, ctx, output):
        if not isinstance(output, dict) or set(output) != _OUTPUT_FIELDS:
            raise ValueError(
                "Group output must contain exactly the five boundary fields"
            )
        for key in ("summary", "completion", "help"):
            if not isinstance(output[key], str) or len(output[key]) > 32768:
                raise ValueError(f"Invalid output {key}")
        for key in ("unfinished", "artifacts"):
            if (
                not isinstance(output[key], list)
                or len(output[key]) > 128
                or any(
                    not isinstance(value, str) or len(value) > 8192
                    for value in output[key]
                )
            ):
                raise ValueError(f"Invalid output {key}")
        if ctx.work.is_symlink() or not ctx.work.is_dir():
            raise ValueError("Run work must be a real directory")
        sources = []
        total = 0
        for name in output["artifacts"]:
            path = Path(name)
            if not path.is_absolute():
                path = ctx.work / path
            if not path.is_relative_to(ctx.work) or ".." in path.parts:
                raise ValueError("Artifacts must be regular files inside Run work")
            cursor = ctx.work
            for part in path.relative_to(ctx.work).parts:
                cursor /= part
                if cursor.is_symlink():
                    raise ValueError("Artifacts may not contain symlinks")
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_ARTIFACT:
                raise ValueError("Artifacts must be small regular report/source files")
            total += info.st_size
            if total > _MAX_TOTAL_ARTIFACTS:
                raise ValueError("Boundary artifacts exceed the delivery size limit")
            sources.append(path)
        if len(sources) != len(set(sources)):
            raise ValueError("Duplicate boundary artifact")
        destination = ctx.root / "exports" / f"{ctx.round:06d}"
        destination.mkdir()
        delivered = []
        source_map = {}
        for index, source in enumerate(sources):
            target = destination / source.relative_to(ctx.work)
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as stream, target.open("xb") as out:
                data = stream.read(_MAX_ARTIFACT + 1)
                if len(data) > _MAX_ARTIFACT:
                    raise ValueError("Artifact grew beyond the delivery size limit")
                out.write(data)
            target.chmod(0o444)
            delivered.append(str(target))
            source_map[source.relative_to(ctx.work).as_posix()] = str(target)
        output = {**output, "artifacts": delivered}
        write_json(
            ctx.root / "rounds" / f"{ctx.round:06d}" / "artifacts.json", source_map
        )
        write_json(ctx.root / "rounds" / f"{ctx.round:06d}" / "output.json", output)
        destination.chmod(0o555)
        return output

    def observe(self, run_id=None):
        if run_id is not None:
            return copy.deepcopy(self._records[run_id])
        return {
            "stopped": self._stopped,
            "runs": [copy.deepcopy(value) for value in self._records.values()],
        }

    def _boundary(self, run_id, status):
        record = self._records[run_id]
        safe_failure_close = status == "closed" and record["status"] == "failed"
        if record["status"] != "returned" and not safe_failure_close:
            raise RuntimeError(
                "Router actions require a returned Group boundary or settled failure close"
            )
        record.update(status=status, updated_at=now())
        self._persist(record)
        return self.observe(run_id)

    def hold(self, run_id):
        return self._boundary(run_id, "held")

    def close(self, run_id):
        return self._boundary(run_id, "closed")

    async def wait(self):
        """Wait for a new terminal execution event, or for runtime STOP."""
        if self._stopped and self._events.empty():
            return {"stopped": True}
        return await self._events.get()

    async def settle(self):
        """Drain executions without automatically retrying uncertain external work."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks.values()))

    async def stop(self):
        """Stop admission and owned work, then retain the cleanup outcome."""
        self._stopped = True
        # Let newly admitted tasks enter their try/finally before cancellation.
        await asyncio.sleep(0)
        for run_id, ctx in tuple(self._contexts.items()):
            if self._tasks[run_id].done() or ctx.services is None:
                continue
            action = getattr(ctx.services, "stop", None)
            if action is not None:
                try:
                    result = action()
                    if inspect.isawaitable(result):
                        await result
                except BaseException as exc:
                    self._stop_errors[run_id] = f"stop: {type(exc).__name__}: {exc}"
        for task in tuple(self._tasks.values()):
            if not task.done():
                task.cancel()
        await self.settle()
        for run_id, error in self._stop_errors.items():
            record = self._records[run_id]
            record.update(status="needs-reconciliation", error=error, updated_at=now())
            self._persist(record)
        self._events.put_nowait({"stopped": True})

    async def close_runtime(self):
        await self.stop()
