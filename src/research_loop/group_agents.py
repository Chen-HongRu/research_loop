"""Leaf Codex contexts and settled command receipts owned by one GroupRun."""

import asyncio
import fcntl
import json
import os
import re
import signal
import shlex
from dataclasses import replace
from pathlib import Path, PurePosixPath

from .codex import Native, configuration
from .instructions import _ROLE_SECTIONS
from .state import now, write_json

CONTEXT_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}")
GROUP_GUARD = """You are a leaf specialist inside one deterministic research GroupRun.
The current role, assigned problem scope and source artifact paths are in the
user payload. Work within that scope and the supplied authorization. Scientific
design assignments may explicitly produce or revise plans. When implementing an
approved plan, preserve its evaluation/data roles, loss/mask/time semantics and
completed execution identities; do not silently change its scientific meaning.
Ask for assistance when a required decision or missing evidence lies outside the
assigned scope. Do not spawn/delegate agents, invoke remote compute,
use alternate SSH/allocation paths, or edit controller/control files. Prepare
commands for the host service. Write only assigned files under the supplied work
path and the supplied execution_work namespace; preserve other Runs and canonical
inputs/checkpoints. Remote inputs must already exist; the host does not synchronize
local implementation files to the resource backend.
Use scientific artifacts rather than other agents' chats. A context can retain
its own earlier implementation phases. Analyst and Critic use separate contexts.
Separate observations, inference, negative evidence and limitations. Return the
requested schema with exact artifact paths; a prepared command is not execution.
"""


class _RunJournal:
    def __init__(self, journal, run_id, context=None):
        self.journal, self.run_id, self.context = journal, run_id, context

    def emit(self, event, **data):
        self.journal.emit(
            event, **{**data, "group_run_id": self.run_id, "context": self.context}
        )


class NativeGroupServices:
    """No research scheduling: the Python group program chooses every phase."""

    def __init__(
        self,
        ctx,
        study_config,
        journal,
        resource_tools=(),
        *,
        native_factory=Native,
        resource_lock=None,
    ):
        self.ctx, self.config = ctx, study_config
        self.journal = _RunJournal(journal, ctx.run_id)
        self.raw_journal = journal
        self.tools = {tool.name: tool for tool in resource_tools}
        self.native_factory = native_factory
        self.active = set()
        self.pending_calls = 0
        self.pending_commands = 0
        self.resource_lock = resource_lock or asyncio.Lock()
        self.processes = set()
        self.closed = False
        self.stopping = False
        self.locks = {}
        self.study_root = Path(
            getattr(ctx, "study_root", study_config.get("study_root", ctx.root))
        )
        self.runtime_root = ctx.work.parent / "native-runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        (self.runtime_root / "work/tmp").mkdir(parents=True, exist_ok=True)
        owner = ctx.state.setdefault("native_owner", ctx.run_id)
        if owner != ctx.run_id:
            raise ValueError("Native context state belongs to another GroupRun")
        snapshot = ctx.state.setdefault(
            "native_instruction_snapshot",
            {
                "guard": GROUP_GUARD,
                "roles": dict(_ROLE_SECTIONS),
            },
        )
        self.guard, self.role_sections = snapshot["guard"], snapshot["roles"]
        ctx.state.setdefault("native_contexts", {})
        ctx.state.setdefault("native_calls", [])
        ctx.state.setdefault("executions", {})
        ctx.save_state()
        input_path = ctx.root / "rounds" / f"{ctx.round:06d}" / "input.json"
        admitted = json.loads(input_path.read_text()) if input_path.exists() else {}
        self.readable = [
            Path(ref)
            for ref in [
                *([admitted["task_ref"]] if admitted.get("task_ref") else []),
                *admitted.get("input_refs", []),
                *admitted.get("feedback_refs", []),
            ]
        ]
        # Pin scientific evidence scope and one published knowledge version.
        # Ordinary workspace-write does not hide other host-readable files; the
        # guard governs conversation isolation and use of unrelated evidence.
        call_path = self.study_root / "call.json"
        if "native_study_context" not in ctx.state:
            call = json.loads(call_path.read_text()) if call_path.is_file() else {}
            study_input = dict(call.get("input", {}))
            published = self.study_root / "knowledge/current"
            if published.is_dir():
                snapshot = published.resolve()
                study_input["published_knowledge_root"] = str(snapshot)
                study_input["published_knowledge_index_file"] = str(
                    snapshot / "index.md"
                )
            ctx.state["native_study_context"] = {
                "input": study_input,
                "output_schema": call.get("output_schema", {}),
            }
            ctx.save_state()
        self.study_context = ctx.state["native_study_context"]
        for field in (
            "goal_file",
            "published_knowledge_root",
        ):
            ref = self.study_context["input"].get(field)
            if ref and Path(ref).exists():
                self.readable.append(Path(ref))

    def _admit(self):
        if self.closed or self.stopping or (self.study_root / "STOP").exists():
            raise RuntimeError("Stop requested before GroupRun work admission")

    def _roles(self, thread_id, context, role, status):
        path = self.study_root / "roles.json"
        with path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            document = json.loads(path.read_text()) if path.exists() else {}
            threads = document.setdefault("threads", {})
            threads[thread_id] = {
                "role": role,
                "task": f"{self.ctx.run_id}/{context}",
                "task_id": f"{self.ctx.run_id}/{context}",
                "status": status,
                "group_run_id": self.ctx.run_id,
                "context": context,
            }
            document.setdefault("version", 1)
            write_json(path, document)

    async def call(
        self,
        role,
        context,
        instructions,
        payload,
        schema,
        *,
        common=None,
        include_role_template=True,
        cwd=None,
    ):
        self.pending_calls += 1
        try:
            return await self._call(
                role,
                context,
                instructions,
                payload,
                schema,
                common=common,
                include_role_template=include_role_template,
                cwd=cwd,
            )
        finally:
            self.pending_calls -= 1

    async def _call(
        self,
        role,
        context,
        instructions,
        payload,
        schema,
        *,
        common,
        include_role_template,
        cwd,
    ):
        self._admit()
        role = role.lower()
        if role not in self.role_sections or not CONTEXT_NAME.fullmatch(context):
            raise ValueError("Use a known research role and a simple context name")
        lock = self.locks.setdefault(context, asyncio.Lock())
        async with lock:
            self._admit()
            contexts = self.ctx.state["native_contexts"]
            record = contexts.setdefault(context, {"role": role, "thread_id": None})
            if role in {"analyst", "critic"} or record["role"] in {"analyst", "critic"}:
                if record["role"] != role:
                    raise ValueError("Analyst and Critic require independent contexts")
            if record.get("status") in {"running", "unknown"}:
                raise RuntimeError("Prior context call requires reconciliation")
            # Never grant specialist sessions the host's resource tools.
            native_config, effective = configuration(
                self.runtime_root,
                Path(self.config["project"]),
                [self.ctx.work],
                self.config["model"],
                self.config["effort"],
                provider=self.config.get("provider"),
                delegation="none",
                readable_roots=self.readable,
                execution_environment=self.config.get("execution_environment"),
            )
            if cwd is not None:
                directory = Path(cwd).resolve(strict=True)
                if not directory.is_relative_to(self.ctx.work.resolve()):
                    raise ValueError(
                        "Specialist cwd must be inside its own work directory"
                    )
                native_config = replace(native_config, cwd=str(directory))
                effective["cwd"] = str(directory)
            native = self.native_factory(
                native_config, _RunJournal(self.raw_journal, self.ctx.run_id, context)
            )
            self.active.add(native)
            call = {"context": context, "role": role, "status": "running", "at": now()}
            self.ctx.state["native_calls"].append(call)
            record["status"] = "running"
            self.ctx.save_state()

            def started(thread_id):
                record["thread_id"] = thread_id
                self.ctx.save_state()
                self._roles(thread_id, context, role, "running")

            try:
                await native.open()
                await native.verify(
                    self.config["model"],
                    self.config["effort"],
                    provider=self.config.get("provider"),
                )
                self._admit()
                assignment = {
                    "role": role,
                    "instructions": instructions,
                    "assignment": payload,
                    "study_context": self.study_context,
                    "work": str(self.ctx.work),
                    "execution_work": self.execution_work,
                    "group_run_id": self.ctx.run_id,
                    "execution_environment": self.config.get("execution_environment"),
                }
                if include_role_template:
                    assignment["role_template"] = self.role_sections[role]
                result = await native.run(
                    self.guard if common is None else common + "\n" + self.guard,
                    assignment,
                    schema,
                    model=self.config["model"],
                    effort=self.config["effort"],
                    thread_id=record["thread_id"],
                    on_started=started,
                    delegation="none",
                )
                call.update(status="completed", result=result, effective=effective)
                record["status"] = "completed"
                return result
            except BaseException as error:
                call.update(status="unknown", error=f"{type(error).__name__}: {error}")
                record["status"] = "unknown"
                raise
            finally:
                try:
                    if call["status"] != "completed":
                        await native.stop()
                    await native.settle()
                except BaseException:
                    record["status"] = call["status"] = "unknown"
                    raise
                finally:
                    try:
                        await native.close()
                    except BaseException:
                        record["status"] = call["status"] = "unknown"
                        raise
                    finally:
                        self.active.discard(native)
                        self.ctx.save_state()
                        if record["thread_id"]:
                            self._roles(
                                record["thread_id"], context, role, record["status"]
                            )

    def _save_execution(self):
        self.ctx.save_state()
        write_json(
            self.ctx.work.parent / "executions.json", self.ctx.state["executions"]
        )

    @property
    def execution_work(self):
        if self.ctx.definition.settings.get("execution_backend") == "local":
            return str(self.ctx.work)
        root = (self.config.get("resource") or {}).get("remote_root")
        return (
            str(PurePosixPath(root) / ".group-runs" / self.ctx.run_id) if root else None
        )

    def _resource_command(self, command):
        root = (self.config.get("resource") or {}).get("remote_root")
        if (
            not isinstance(root, str)
            or not root.startswith("/")
            or any(c in root for c in "\n\r\x00")
        ):
            raise ValueError(
                "Resource execution requires an authorized absolute remote_root"
            )
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise ValueError(
                "Prepared resource command must be a nonempty shell string"
            )
        work = shlex.quote(str(PurePosixPath(root) / ".group-runs" / self.ctx.run_id))
        return f"mkdir -p {work} && cd {work} && (\n{command}\n)"

    async def execute(self, tool_name, arguments, *, operation_id):
        """Call the injected authorized tool; persist identity before dispatch."""
        self._admit()
        if tool_name == "gpu_exec":
            arguments = {
                **arguments,
                "command": self._resource_command(arguments.get("command")),
            }
        operations = self.ctx.state["executions"]
        existing = operations.get(operation_id)
        if existing:
            if existing["tool"] != tool_name or existing["arguments"] != arguments:
                raise ValueError("Operation identity cannot be rebound")
            if existing["status"] == "completed":
                return existing["receipt"]
            raise RuntimeError("Operation requires reconciliation; never redispatch")
        if tool_name not in self.tools:
            raise ValueError(f"No authorized resource tool: {tool_name}")
        row = operations[operation_id] = {
            "tool": tool_name,
            "arguments": arguments,
            "status": "dispatching",
            "at": now(),
        }
        self._save_execution()
        try:
            receipt = await self.tools[tool_name].call(arguments)
            if not isinstance(receipt, dict):
                raise ValueError("Resource tool must return an object receipt")
            row.update(receipt=receipt, status="completed")
            if self._uncertain(receipt):
                row["status"] = "unknown"
            elif receipt.get("running"):
                row["status"] = "pending"
            elif tool_name in {"gpu_exec", "gpu_poll"} and not isinstance(
                receipt.get("exit_code"), int
            ):
                row["status"] = "unknown"
            if tool_name == "gpu_poll" and receipt.get("session_id"):
                for prior in operations.values():
                    if (
                        prior.get("receipt", {}).get("session_id")
                        == receipt["session_id"]
                        and prior["status"] == "pending"
                    ):
                        prior.update(receipt=receipt, status=row["status"])
            self.journal.emit(
                "resource.tool",
                tool=tool_name,
                arguments=arguments,
                success=True,
                output=receipt,
            )
            return receipt
        except BaseException as error:
            row.update(status="unknown", error=f"{type(error).__name__}: {error}")
            self.journal.emit(
                "resource.tool",
                tool=tool_name,
                arguments=arguments,
                success=False,
                output={"error": str(error)},
            )
            raise
        finally:
            self._save_execution()

    @staticmethod
    def _uncertain(receipt):
        # Slurm marks its ledger UNKNOWN on SSH 255, even when error is null.
        return bool(
            receipt.get("error")
            or receipt.get("unknown")
            or receipt.get("exit_code") == 255
        )

    async def execute_prepared(
        self, command, *, operation_id="experiment", backend="resource"
    ):
        """Wait for one prepared command. Recovery never dispatches it twice."""
        self.pending_commands += 1
        try:
            self._admit()
            if backend == "local":
                return await self._local(command, operation_id)
            async with self.resource_lock:
                self._admit()
                return await self._prepared(
                    command, operation_id=operation_id, backend=backend
                )
        finally:
            self.pending_commands -= 1

    async def _prepared(self, command, *, operation_id, backend):
        if backend == "local":
            return await self._local(command, operation_id)
        if backend != "resource":
            raise ValueError("Execution backend must be explicit local or resource")
        rows = self.ctx.state["executions"]
        row = rows.get(operation_id)
        if row and row["status"] == "pending":
            if row["arguments"] != {"command": self._resource_command(command)}:
                raise ValueError("Operation identity cannot be rebound")
            receipt = row["receipt"]
        else:
            receipt = await self.execute(
                "gpu_exec", {"command": command}, operation_id=operation_id
            )
        row = rows[operation_id]
        while receipt.get("running") and not self._uncertain(receipt):
            if "gpu_poll" not in self.tools or not receipt.get("session_id"):
                raise RuntimeError("Running command lacks an authorized poll identity")
            # Polling the original identity is read/reconciliation, never dispatch.
            try:
                receipt = await self.tools["gpu_poll"].call(
                    {"session_id": receipt["session_id"]}
                )
            except BaseException:
                row["status"] = "unknown"
                self._save_execution()
                raise
            self.journal.emit(
                "resource.tool",
                tool="gpu_poll",
                success=True,
                arguments={"session_id": receipt.get("session_id")},
                output=receipt,
            )
            row.update(
                receipt=receipt,
                status="pending" if receipt.get("running") else "completed",
            )
            self._save_execution()
            if receipt.get("running"):
                await asyncio.sleep(0.1)
        if self._uncertain(receipt) or not isinstance(receipt.get("exit_code"), int):
            row["status"] = "unknown"
            self._save_execution()
            raise RuntimeError("Command effect requires reconciliation")
        return receipt

    async def _local(self, command, operation_id):
        self._admit()
        rows = self.ctx.state["executions"]
        if operation_id in rows:
            row = rows[operation_id]
            if row["tool"] != "local" or row["arguments"] != {"command": command}:
                raise ValueError("Operation identity cannot be rebound")
            if row["status"] == "completed":
                return row["receipt"]
            raise RuntimeError(
                "Local command requires reconciliation; never redispatch"
            )
        if not isinstance(command, str) or not command.strip():
            raise ValueError("Prepared local command must be a nonempty string")
        row = rows[operation_id] = {
            "tool": "local",
            "arguments": {"command": command},
            "status": "dispatching",
            "at": now(),
        }
        self._save_execution()
        process = None
        try:
            log_path = self.ctx.work / f"command-{len(rows):03d}.log"
            with log_path.open("wb") as log:
                process = await asyncio.create_subprocess_shell(
                    command,
                    cwd=self.ctx.work,
                    stdout=log,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
                self.processes.add(process)
                row.update(pid=process.pid, status="running", log_path=str(log_path))
                self._save_execution()
                waiter = asyncio.create_task(process.wait())
                while process.returncode is None:
                    if self.stopping or (self.study_root / "STOP").exists():
                        os.killpg(process.pid, signal.SIGTERM)
                    try:
                        await asyncio.wait_for(asyncio.shield(waiter), 0.1)
                    except TimeoutError:
                        if self.stopping or (self.study_root / "STOP").exists():
                            os.killpg(process.pid, signal.SIGKILL)
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                    raise RuntimeError(
                        "Local command left background work; reconcile before return"
                    )
            receipt = {
                "exit_code": process.returncode,
                "running": False,
                "log_path": str(log_path),
            }
            row.update(receipt=receipt, status="completed")
            return receipt
        except BaseException:
            row["status"] = "unknown"
            raise
        finally:
            if process:
                if process.returncode is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
                self.processes.discard(process)
            self._save_execution()

    async def stop(self):
        self.stopping = True
        for native in tuple(self.active):
            await native.stop()
        for process in tuple(self.processes):
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)

    async def settle(self):
        await asyncio.sleep(0)
        if self.pending_calls or self.pending_commands or self.active or self.processes:
            raise RuntimeError("GroupRun work is still active")
        if any(
            row["status"] != "completed"
            for row in self.ctx.state["executions"].values()
        ):
            raise RuntimeError("GroupRun execution requires reconciliation")
        if any(
            row.get("status") in {"running", "unknown"}
            for row in self.ctx.state["native_contexts"].values()
        ):
            raise RuntimeError("GroupRun context requires reconciliation")

    async def close(self):
        try:
            await self.settle()
        finally:
            self.closed = True
            for native in tuple(self.active):
                await native.stop()
                try:
                    await native.settle()
                finally:
                    await native.close()
