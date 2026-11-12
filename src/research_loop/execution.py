"""Settled local and authorized resource commands for one Group Run."""

import asyncio
import json
import os
import shlex
import signal
from pathlib import Path, PurePosixPath

from .environment import shell_environment
from .state import now, write_json


class Commands:
    def __init__(self, ctx, config, journal, tools=(), *, resource_lock=None):
        self.ctx, self.config, self.journal = (
            ctx,
            json.loads(json.dumps(config)),
            journal,
        )
        self.tools = {tool.name: tool for tool in tools}
        self.resource_lock = resource_lock or asyncio.Lock()
        self.pending_commands = 0
        self.processes = set()
        self.closed = self.stopping = False
        self.control_root = Path(getattr(ctx, "study_root", ctx.root))
        self.trace = getattr(ctx, "trace", None)
        if self.trace is None and hasattr(ctx, "config_revision"):
            from .records import ExecutionTrace

            self.trace = ExecutionTrace(
                ctx.root,
                run_id=ctx.run_id,
                round=ctx.round,
                revision=ctx.config_revision,
            )
        ctx.state.setdefault("executions", {})
        ctx.save_state()

    def _admit(self):
        if self.closed or self.stopping or (self.control_root / "STOP").exists():
            raise RuntimeError("Stop requested before GroupRun work admission")

    def close_admission(self):
        self.stopping = True

    def request_stop(self):
        self.close_admission()
        # Signal every local process before any await. Backend cancellation is
        # owned by the injected resource tool/controller, not by an SSH guess.
        for process in tuple(self.processes):
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    async def stop(self):
        self.request_stop()

    def adopt_execution(self, operation_id, record):
        """Accept a settled original operation as evidence, without dispatch."""
        self._admit()
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("Operation identity must be a nonempty string")
        row = json.loads(json.dumps(record))
        receipt = row.get("receipt")
        if (
            row.get("status") != "completed"
            or not isinstance(row.get("origin_group_run_id"), str)
            or not row["origin_group_run_id"]
            or row.get("tool") not in {"local", "gpu_exec"}
            or not isinstance(row.get("arguments"), dict)
            or not isinstance(row["arguments"].get("command"), str)
            or row.get("operation_id") != operation_id
            or not isinstance(receipt, dict)
            or (row.get("tool") != "local" and self._uncertain(receipt))
            or receipt.get("running")
            or type(receipt.get("exit_code")) is not int
        ):
            raise ValueError(
                "Only an identified settled command receipt can be adopted"
            )
        operations = self.ctx.state["executions"]
        existing = operations.get(operation_id)
        if existing is not None and existing != row:
            raise ValueError("Operation identity cannot be rebound")
        operations[operation_id] = row
        self._save_execution()
        if self.trace is not None and existing is None:
            identity = self.trace.begin(
                "operation",
                operation_id,
                origin_group_run_id=row["origin_group_run_id"],
                operation_id=operation_id,
                adopted=True,
            )
            self.trace.end(identity, "completed", receipt=receipt)
        return json.loads(json.dumps(receipt))

    def _begin_operation(self, operation_id, row):
        if self.trace is not None:
            row["trace_id"] = self.trace.begin("operation", operation_id, **row)

    def _end_operation(self, row):
        if self.trace is not None and row.get("trace_id"):
            self.trace.end(
                row["trace_id"],
                row["status"],
                **{
                    key: value
                    for key, value in row.items()
                    if key not in {"status", "trace_id"}
                },
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
        arguments = json.loads(json.dumps(arguments))
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
                return json.loads(json.dumps(existing["receipt"]))
            raise RuntimeError("Operation requires reconciliation; never redispatch")
        if tool_name not in self.tools:
            raise ValueError(f"No authorized resource tool: {tool_name}")
        row = operations[operation_id] = {
            "tool": tool_name,
            "origin_group_run_id": self.ctx.run_id,
            "operation_id": operation_id,
            "arguments": arguments,
            "status": "dispatching",
            "at": now(),
        }
        self._begin_operation(operation_id, row)
        self._save_execution()
        try:
            receipt = await self.tools[tool_name].call(arguments)
            if not isinstance(receipt, dict):
                raise ValueError("Resource tool must return an object receipt")
            row.update(receipt=json.loads(json.dumps(receipt)), status="completed")
            if self._uncertain(receipt):
                row["status"] = "unknown"
            elif receipt.get("running"):
                row["status"] = "pending"
            elif (
                tool_name in {"gpu_exec", "gpu_poll"}
                and type(receipt.get("exit_code")) is not int
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
            self._end_operation(row)

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
                original_session = receipt["session_id"]
                receipt = await self.tools["gpu_poll"].call(
                    {"session_id": original_session}
                )
                if (
                    not isinstance(receipt, dict)
                    or receipt.get("session_id") != original_session
                ):
                    raise ValueError(
                        "Poll must retain the original command session identity"
                    )
            except BaseException:
                row["status"] = "unknown"
                self._save_execution()
                self._end_operation(row)
                raise
            self.journal.emit(
                "resource.tool",
                tool="gpu_poll",
                success=True,
                arguments={"session_id": receipt.get("session_id")},
                output=receipt,
            )
            row.update(
                receipt=json.loads(json.dumps(receipt)),
                status="pending" if receipt.get("running") else "completed",
            )
            self._save_execution()
            if receipt.get("running"):
                await asyncio.sleep(0.1)
        if self._uncertain(receipt) or type(receipt.get("exit_code")) is not int:
            row["status"] = "unknown"
            self._save_execution()
            self._end_operation(row)
            raise RuntimeError("Command effect requires reconciliation")
        self._end_operation(row)
        return json.loads(json.dumps(receipt))

    async def _local(self, command, operation_id):
        self._admit()
        rows = self.ctx.state["executions"]
        if operation_id in rows:
            row = rows[operation_id]
            if row["tool"] != "local" or row["arguments"] != {"command": command}:
                raise ValueError("Operation identity cannot be rebound")
            if row["status"] == "completed":
                return json.loads(json.dumps(row["receipt"]))
            raise RuntimeError(
                "Local command requires reconciliation; never redispatch"
            )
        if not isinstance(command, str) or not command.strip():
            raise ValueError("Prepared local command must be a nonempty string")
        row = rows[operation_id] = {
            "tool": "local",
            "origin_group_run_id": self.ctx.run_id,
            "operation_id": operation_id,
            "arguments": {"command": command},
            "status": "dispatching",
            "at": now(),
        }
        self._begin_operation(operation_id, row)
        self._save_execution()
        process = None
        try:
            overrides = shell_environment(
                self.config.get("execution_environment"), self.ctx.work
            )
            Path(overrides["TMPDIR"]).mkdir(parents=True, exist_ok=True)
            environment = dict(os.environ)
            environment.pop("PYTHONHOME", None)
            environment.pop("PYTHONPATH", None)
            environment.pop("VIRTUAL_ENV", None)
            environment.update(overrides)
            log_path = self.ctx.work / f"command-{len(rows):03d}.log"
            with log_path.open("wb") as log:
                process = await asyncio.create_subprocess_shell(
                    command,
                    cwd=self.ctx.work,
                    env=environment,
                    stdout=log,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
                self.processes.add(process)
                row.update(pid=process.pid, status="running", log_path=str(log_path))
                self._save_execution()
                waiter = asyncio.create_task(process.wait())
                while process.returncode is None:
                    if self.stopping or (self.control_root / "STOP").exists():
                        os.killpg(process.pid, signal.SIGTERM)
                    try:
                        await asyncio.wait_for(asyncio.shield(waiter), 0.1)
                    except TimeoutError:
                        if self.stopping or (self.control_root / "STOP").exists():
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
            row.update(receipt=json.loads(json.dumps(receipt)), status="completed")
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
            self._end_operation(row)

    async def settle(self):
        await asyncio.sleep(0)
        if self.pending_commands or self.processes:
            raise RuntimeError("GroupRun work is still active")
        if any(
            row["status"] != "completed"
            for row in self.ctx.state["executions"].values()
        ):
            raise RuntimeError("GroupRun execution requires reconciliation")

    async def close(self):
        self.closed = True
        self.request_stop()
        await self.settle()
