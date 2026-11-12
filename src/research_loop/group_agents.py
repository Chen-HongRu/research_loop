"""Declarative Agent sessions and thin Group service composition."""

import asyncio
import json
import re
import uuid
from dataclasses import replace
from pathlib import Path

from .codex import Native, configuration
from .execution import Commands
from .records import ExecutionTrace, record_thread
from .state import now

CONTEXT_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}")


def _snapshot(value):
    return json.loads(json.dumps(value))


class _RunJournal:
    def __init__(self, journal, run_id, context=None):
        self.journal, self.run_id, self.context = journal, run_id, context

    def emit(self, event, **data):
        self.journal.emit(
            event, **{**data, "group_run_id": self.run_id, "context": self.context}
        )


class Agents:
    """Own sessions and call receipts; Group programs choose collaboration."""

    def __init__(self, ctx, config, journal, *, native_factory=Native, on_started=None):
        self.ctx, self.config, self.journal = ctx, _snapshot(config), journal
        self.native_factory, self.on_started = native_factory, on_started
        self.active, self.locks = set(), {}
        self.pending_calls = 0
        self.closed = self.stopping = False
        self.control_root = Path(getattr(ctx, "study_root", ctx.root))
        self.runtime_root = ctx.work.parent / "native-runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.revision = ctx.config_revision
        if self.revision != config["revision"]:
            raise ValueError("Group Run configuration revision does not match")
        owner = ctx.state.setdefault("native_owner", ctx.run_id)
        if owner != ctx.run_id:
            raise ValueError("Native context state belongs to another GroupRun")
        definition = {
            "revision": self.revision,
            "agents": config["agents"],
            "profiles": config["profiles"],
            "bindings": ctx.definition.agents,
        }
        pinned = ctx.state.setdefault(
            "native_definition_snapshot", _snapshot(definition)
        )
        if pinned != definition:
            raise ValueError("Agent definitions cannot change within a Group Run")
        self.definitions = _snapshot(pinned)
        ctx.state.setdefault("native_contexts", {})
        sessions = ctx.state.setdefault("native_sessions", {})
        for context, record in ctx.state["native_contexts"].items():
            stored = sessions.get(record["session_id"])
            if stored != record:
                raise ValueError("Context and session receipts disagree")
            ctx.state["native_contexts"][context] = stored
        ctx.state.setdefault("native_calls", [])
        self.trace = getattr(ctx, "trace", None)
        if self.trace is None:
            self.trace = ExecutionTrace(
                ctx.root, run_id=ctx.run_id, round=ctx.round, revision=self.revision
            )
        ctx.save_state()

    def _admit(self):
        if self.closed or self.stopping or (self.control_root / "STOP").exists():
            raise RuntimeError("Stop requested before GroupRun work admission")

    def close_admission(self):
        self.stopping = True

    def request_stop(self):
        self.close_admission()
        # Create every interruption task before awaiting a slow transport.
        return asyncio.gather(
            *(native.stop() for native in tuple(self.active)), return_exceptions=True
        )

    async def stop(self):
        results = await self.request_stop()
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def call(
        self,
        binding,
        context,
        instructions,
        payload,
        schema=None,
        *,
        session="new",
        step_id=None,
        cwd=None,
        allow_tools=True,
        max_output_chars=65536,
    ):
        self._admit()
        if session not in {"new", "reuse"}:
            raise ValueError("Session must be explicit new or reuse")
        if not isinstance(context, str) or not CONTEXT_NAME.fullmatch(context):
            raise ValueError("Use a simple local context name")
        if binding not in self.definitions["bindings"]:
            raise ValueError(f"Unknown Agent binding: {binding}")
        self.pending_calls += 1
        try:
            async with self.locks.setdefault(context, asyncio.Lock()):
                self._admit()
                return await self._call(
                    binding,
                    context,
                    instructions,
                    _snapshot(payload),
                    schema,
                    session=session,
                    step_id=step_id,
                    cwd=cwd,
                    allow_tools=allow_tools,
                    max_output_chars=max_output_chars,
                )
        finally:
            self.pending_calls -= 1

    async def _call(
        self,
        binding,
        context,
        instructions,
        payload,
        schema,
        *,
        session,
        step_id,
        cwd,
        allow_tools,
        max_output_chars,
    ):
        agent_id = self.definitions["bindings"][binding]
        agent = self.definitions["agents"][agent_id]
        profile = self.definitions["profiles"][agent["profile"]]
        effective_tools = allow_tools and agent["tools"] == "workspace"
        runtime_root = (
            self.runtime_root
            / context
            / agent_id
            / ("workspace" if effective_tools else "none")
        )
        runtime_root.mkdir(parents=True, exist_ok=True)
        native_config, effective = configuration(
            runtime_root,
            Path(self.config["project"]),
            [self.ctx.work],
            profile["model"],
            profile["effort"],
            execution_environment=self.config.get("execution_environment"),
            allow_tools=effective_tools,
        )
        if cwd is not None:
            directory = Path(cwd).resolve(strict=True)
            if not directory.is_dir() or not directory.is_relative_to(
                self.ctx.work.resolve()
            ):
                raise ValueError("Agent cwd must be inside its own work directory")
            native_config = replace(native_config, cwd=str(directory))
            effective["cwd"] = str(directory)
        schema = _snapshot(schema if schema is not None else agent.get("output_schema"))
        identity = {
            "group_run_id": self.ctx.run_id,
            "config_revision": self.revision,
            "agent_id": agent_id,
            "agent": agent,
            "profile": profile,
            "effective": effective,
        }
        contexts = self.ctx.state["native_contexts"]
        prior = contexts.get(context)
        if prior and prior.get("status") in {"running", "unknown"}:
            raise RuntimeError("Prior context call requires reconciliation")
        if session == "reuse":
            if not prior or prior["identity"] != identity or not prior.get("thread_id"):
                raise ValueError(
                    "Session reuse requires a compatible existing identity"
                )
            record = prior
        else:
            session_id = f"session-{uuid.uuid4().hex}"
            record = {
                "session_id": session_id,
                "context": context,
                "identity": _snapshot(identity),
                "thread_id": None,
            }
            contexts[context] = record
            self.ctx.state["native_sessions"][session_id] = record
        assignment = {
            "instructions": instructions,
            "assignment": payload,
            "work": str(self.ctx.work),
            "group_run_id": self.ctx.run_id,
            "config_revision": self.revision,
            "execution_environment": self.config.get("execution_environment"),
        }
        call_id = self.trace.begin(
            "call",
            binding,
            binding=binding,
            parent_step_id=step_id,
            agent_id=agent_id,
            context=context,
            session_id=record["session_id"],
            thread_id=record["thread_id"],
            input=assignment,
            instructions=agent["instructions"],
            schema=schema,
            profile=profile,
            effective=effective,
        )
        call = {
            "call_id": call_id,
            "binding": binding,
            "parent_step_id": step_id,
            "agent_id": agent_id,
            "context": context,
            "session_id": record["session_id"],
            "thread_id": record["thread_id"],
            "status": "running",
            "at": now(),
            "config_revision": self.revision,
            "input": _snapshot(assignment),
            "instructions": agent["instructions"],
            "schema": schema,
            "profile": _snapshot(profile),
            "effective": _snapshot(effective),
        }
        self.ctx.state["native_calls"].append(call)
        record["status"] = "running"
        self.ctx.save_state()
        native = None
        failure = None

        def started(thread_id):
            record["thread_id"] = call["thread_id"] = thread_id
            record_thread(
                self.ctx.study_root,
                thread_id,
                role=agent_id,
                task=f"{self.ctx.run_id}/{binding}/{context}",
                group_run_id=self.ctx.run_id,
                config_revision=self.revision,
                session_id=record["session_id"],
                status="running",
            )
            self.ctx.save_state()
            if self.on_started:
                self.on_started(thread_id)

        try:
            native = self.native_factory(
                native_config, _RunJournal(self.journal, self.ctx.run_id, context)
            )
            self.active.add(native)
            await native.open()
            await native.verify(profile["model"], profile["effort"])
            self._admit()
            result = await native.run(
                agent["instructions"],
                assignment,
                schema,
                model=profile["model"],
                effort=profile["effort"],
                thread_id=record["thread_id"],
                on_started=started,
                allow_tools=effective_tools,
                max_output_chars=max_output_chars,
            )
            if not isinstance(result, dict):
                raise ValueError("Agent structured output must be an object")
            call.update(status="completed", result=_snapshot(result))
            record["status"] = "completed"
            return result
        except BaseException as error:
            failure = error
            call.update(status="unknown", error=f"{type(error).__name__}: {error}")
            record["status"] = "unknown"
            raise
        finally:
            cleanup_error = None
            try:
                if native is not None:
                    if call["status"] != "completed":
                        await native.stop()
                    await native.settle()
            except BaseException as error:
                cleanup_error = error
            finally:
                try:
                    if native is not None:
                        await native.close()
                except BaseException as error:
                    cleanup_error = cleanup_error or error
                finally:
                    if cleanup_error is not None:
                        record["status"] = call["status"] = "unknown"
                        call["cleanup_error"] = (
                            f"{type(cleanup_error).__name__}: {cleanup_error}"
                        )
                    self.active.discard(native)
                    if cleanup_error is None and failure is not None:
                        record["status"] = call["status"] = (
                            "cancelled"
                            if isinstance(failure, asyncio.CancelledError)
                            else "failed"
                        )
                    if record["thread_id"] is not None:
                        record_thread(
                            self.ctx.study_root,
                            record["thread_id"],
                            status=record["status"],
                        )
                    self.ctx.save_state()
                    self.trace.end(
                        call_id,
                        call["status"],
                        **{
                            key: value
                            for key, value in call.items()
                            if key not in {"call_id", "status"}
                        },
                    )
            if cleanup_error is not None and failure is None:
                raise cleanup_error

    async def settle(self):
        await asyncio.sleep(0)
        if self.pending_calls or self.active:
            raise RuntimeError("GroupRun work is still active")
        if any(
            row.get("status") in {"running", "unknown"}
            for row in self.ctx.state["native_sessions"].values()
        ):
            raise RuntimeError("GroupRun context requires reconciliation")

    async def close(self):
        self.closed = True
        await self.stop()
        await self.settle()


class NativeGroupServices:
    """Group-facing composition of independent Agent and command services."""

    def __init__(
        self,
        ctx,
        config,
        journal,
        resource_tools=(),
        *,
        native_factory=Native,
        resource_lock=None,
        on_started=None,
    ):
        self.ctx, self.config = ctx, _snapshot(config)
        self.agents = Agents(
            ctx, config, journal, native_factory=native_factory, on_started=on_started
        )
        self.commands = Commands(
            ctx,
            config,
            _RunJournal(journal, ctx.run_id),
            resource_tools,
            resource_lock=resource_lock,
        )

    async def call(self, *args, **kwargs):
        return await self.agents.call(*args, **kwargs)

    @property
    def execution_work(self):
        return self.commands.execution_work

    async def execute_prepared(self, *args, **kwargs):
        return await self.commands.execute_prepared(*args, **kwargs)

    async def execute(self, *args, **kwargs):
        return await self.commands.execute(*args, **kwargs)

    def adopt_execution(self, operation_id, record):
        return self.commands.adopt_execution(operation_id, record)

    def close_admission(self):
        self.agents.close_admission()
        self.commands.close_admission()

    def request_stop(self):
        self.close_admission()
        self.commands.request_stop()
        return self.agents.request_stop()

    async def stop(self):
        results = await self.request_stop()
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def settle(self):
        await self.agents.settle()
        await self.commands.settle()

    async def close(self):
        self.close_admission()
        await self.stop()
        self.agents.closed = self.commands.closed = True
        await self.settle()
