"""Codex subscription execution for the loop's existing role/tool descriptions.

The pinned SDK has no public dynamic-tool registration helper. The two private
seams below install its request handler and start a thread with the runtime's
experimental ``dynamicTools`` and ``environments`` fields. No RPC transport or
Agents Runner is implemented here.
"""

import asyncio
import hashlib
import inspect
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agents import FunctionTool, RunContextWrapper, agent_span, function_span
from agents.tool_context import ToolContext
from codex_cli_bin import bundled_codex_path
from openai_codex import AsyncCodex, CodexConfig
from openai_codex.api import AsyncThread, AsyncTurnHandle
from openai_codex.errors import InvalidRequestError
from openai_codex.generated.v2_all import ReasoningEffort
from pydantic import BaseModel, TypeAdapter

MODEL = "gpt-6.1-sol"
EFFORT = ReasoningEffort.medium
REFERENCE = "codex_thread_reference"


class CodexRoleError(RuntimeError):
    """A role failed, with its native thread available for diagnosed recovery."""


@dataclass
class CodexRoleResult:
    final_output: Any
    thread_id: str
    summary: str
    summary_missing: bool = False

    def to_input_list(self):
        return [{"type": REFERENCE, "thread_id": self.thread_id}]


@dataclass
class _Role:
    agent: Any
    tools: dict
    submit: str
    schema: dict
    context: RunContextWrapper
    validate: Any
    run_config: Any
    thread: Any = None
    handle: Any = None
    output: Any = None
    accepted: bool = False
    receipt: dict | None = None
    arguments: Any = None
    fatal: BaseException | None = None
    abort: BaseException | None = None
    stopping: bool = False
    tasks: set = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    span: Any = None
    native_error: Any = None
    failed_calls: set = field(default_factory=set)
    client: Any = None
    terminal: bool = False
    interrupt_requested: bool = False


async def _resolve(value):
    return await value if inspect.isawaitable(value) else value


def _json(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if hasattr(value, "params"):
        return value.params
    return value


class CodexRoleRunner:
    """An independent official client and native thread per active role.

    ``allow_native_response_budget`` acknowledges an SDK limitation: max_turns
    cannot currently be enforced as a count of model responses. It is recorded,
    never replaced with a count of user turns or tool calls. It defaults off.
    """

    def __init__(
        self,
        *,
        event_path: Path,
        cwd: Path | None = None,
        state_dir: Path | None = None,
        allow_native_response_budget: bool = False,
    ):
        self.event_path = Path(event_path)
        self.cwd = Path(cwd) if cwd is not None else None
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.allow_native_response_budget = allow_native_response_budget
        self._client = None
        self._roles = {}
        self._stream = None
        self._temporary = None
        self._blocked_error = None
        self._config = None
        self._clients = []
        self._entered = False

    async def __aenter__(self):
        self._loop = asyncio.get_running_loop()
        if self.state_dir is None:
            self._temporary = tempfile.TemporaryDirectory(prefix="research-codex-")
            root = Path(self._temporary.name)
        else:
            root = self.state_dir
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = root.stat()
            if root.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise CodexRoleError(
                    "state_dir must be owned by this user with mode 0700"
                )
        home = root / "home"
        self._bindings = root / "roles"
        self._bindings.mkdir(mode=0o700, exist_ok=True)
        home.mkdir(mode=0o700, exist_ok=True)
        if (
            home.is_symlink()
            or home.stat().st_uid != os.getuid()
            or home.stat().st_mode & 0o077
        ):
            raise CodexRoleError("Codex home must be private and owned by this user")
        # The official runtime reads its own subscription credentials. Python
        # never reads or exports their contents; no user config/plugins are linked.
        auth = Path.home() / ".codex" / "auth.json"
        if not auth.is_file():
            if self._temporary is not None:
                self._temporary.cleanup()
            raise CodexRoleError("Official Codex subscription login is required")
        if not (home / "auth.json").exists():
            (home / "auth.json").symlink_to(auth)
        cwd = self.cwd or root / "workspace"
        cwd.mkdir(parents=True, exist_ok=True)
        self._cwd = cwd.resolve()
        self._stream = self.event_path.open("x", encoding="utf-8")
        self.event_path.chmod(0o600)
        overrides = (
            'forced_login_method="chatgpt"',
            "mcp_servers={}",
            "features.shell_tool=false",
            "features.apps=false",
            "features.plugins=false",
            "features.multi_agent=false",
            "features.multi_agent_v2=false",
            "features.goals=false",
            "features.view_image=false",
            "features.skip_host_skill_discovery=true",
            "features.hooks=false",
            "features.sleep_tool=false",
            'web_search="disabled"',
            'permissions.study.filesystem={":root"="none"}',
            "permissions.study.network.enabled=false",
            'default_permissions="study"',
        )
        # CodexConfig.env merges with the parent. Use env -i so provider keys,
        # endpoint overrides and inherited application secrets cannot cross.
        child_env = {
            "CODEX_HOME": str(home),
            "PATH": os.defpath,
            "HOME": str(Path.home()),
        }
        for key, value in os.environ.items():
            if key.lower().endswith("_proxy") or key in {
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "NODE_EXTRA_CA_CERTS",
                "REQUESTS_CA_BUNDLE",
                "CURL_CA_BUNDLE",
                "TMPDIR",
                "LANG",
                "LC_ALL",
                "TZ",
            }:
                child_env[key] = value
        # Native model capabilities control built-ins that feature flags alone
        # leave registered. Retain the bundled model's identity and reasoning
        # settings, changing only its tool capability fields.
        probe = await asyncio.create_subprocess_exec(
            str(bundled_codex_path()),
            "debug",
            "models",
            "--bundled",
            env=child_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await probe.communicate()
        if probe.returncode:
            raise CodexRoleError("Unable to read the pinned runtime model catalog")
        matches = [
            entry
            for entry in json.loads(stdout)["models"]
            if entry.get("slug") == MODEL
        ]
        if len(matches) != 1:
            raise CodexRoleError(
                "Pinned runtime does not contain the exact required model"
            )
        entry = matches[0]
        entry.update(
            multi_agent_version=None,
            multi_agent_reasoning_effort=None,
            apply_patch_tool_type=None,
            experimental_supported_tools=[],
            tool_mode=None,
        )
        catalog = root / "model-catalog.json"
        catalog.write_text(json.dumps({"models": [entry]}))
        catalog.chmod(0o600)
        overrides += ("model_catalog_json=" + json.dumps(str(catalog)),)
        command = [
            "/usr/bin/env",
            "-i",
            *(f"{key}={value}" for key, value in child_env.items()),
            str(bundled_codex_path()),
        ]
        for override in overrides:
            command.extend(["--config", override])
        command.extend(["app-server", "--listen", "stdio://"])
        self._config = CodexConfig(
            cwd=str(self._cwd), launch_args_override=tuple(command)
        )
        self._client = AsyncCodex(self._config)
        self._client._client._sync._approval_handler = self._request
        try:
            await self._client.__aenter__()
            account = _json(await self._client.account(refresh_token=False))
            if (account.get("account") or {}).get("type") != "chatgpt":
                raise CodexRoleError(
                    "Official Codex ChatGPT subscription auth is required"
                )
            models = _json(await self._client.models(include_hidden=True))
            matches = [
                model for model in models.get("data", []) if model.get("model") == MODEL
            ]
            if not matches or not any(
                entry.get("reasoning_effort", entry.get("reasoningEffort")) == "medium"
                for entry in matches[0].get(
                    "supported_reasoning_efforts",
                    matches[0].get("supportedReasoningEfforts", []),
                )
            ):
                raise CodexRoleError("Required gpt-6.1-sol / medium is unavailable")
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        await self._client.close()
        self._client = None
        self._entered = True
        return self

    async def __aexit__(self, *_):
        try:
            for role in self._roles.values():
                if role.handle is not None:
                    await self._stop(role)
            if self._client is not None:
                await self._client.close()
            for client in self._clients:
                await client.close()
        finally:
            self._entered = False
            if self._stream is not None:
                self._stream.close()
            if self._temporary is not None:
                self._temporary.cleanup()

    async def _new_client(self):
        if self._config is None:
            return self._client
        client = AsyncCodex(self._config)
        client._client._sync._approval_handler = self._request
        try:
            await client.__aenter__()
        except BaseException:
            await client.close()
            raise
        self._clients.append(client)
        return client

    async def _turn(self, role, prompt):
        started, subscription = await role.client._client._start_turn(
            role.thread.id,
            [{"type": "text", "text": prompt}],
            {"model": MODEL, "effort": "medium", "environments": []},
            for_handle=True,
        )
        return AsyncTurnHandle(
            role.client, role.thread.id, started.turn.id, _subscription=subscription
        )

    def _event(self, event, **data):
        row = {"event": event, "at": datetime.now(UTC).isoformat(), **data}
        self._stream.write(json.dumps(row, default=_json) + "\n")
        self._stream.flush()

    def _request(self, method, params):
        # This is the SDK's sole reader thread. Never wait for an interrupt RPC
        # here: only the asynchronous tool handler may be awaited by this bridge.
        if method != "item/tool/call":
            if method == "item/tool/requestUserInput":
                return {"answers": {}}
            if method.endswith("requestApproval"):
                return {"decision": "decline"}
            return {}
        future = asyncio.run_coroutine_threadsafe(self._tool(params or {}), self._loop)
        return future.result()

    async def _tool(self, params):
        role = self._roles.get(params.get("threadId"))
        if role is None:
            return self._reply(False, "Unknown role thread")
        task = asyncio.current_task()
        role.tasks.add(task)
        name = params.get("tool")
        args = params.get("arguments")
        try:
            async with role.lock:
                if role.stopping:
                    return self._reply(False, "Role is stopping; no tool was executed")
                if name == role.submit:
                    with function_span(
                        name=name, input=json.dumps(args), parent=role.span
                    ) as span:
                        reply = await self._submit(role, args)
                        span.span_data.output = reply["contentItems"][0]["text"]
                        return reply
                if role.accepted:
                    return self._reply(
                        False, "Submission is sealed; no tool was executed"
                    )
                tool = role.tools.get(name)
                if tool is None:
                    return self._reply(
                        False, "Tool is outside the role's allowed tools"
                    )
                enabled = tool.is_enabled
                if callable(enabled):
                    enabled = await _resolve(enabled(role.context, role.agent))
                if not enabled:
                    return self._reply(
                        False, "Tool is currently disabled; no tool was executed"
                    )
                raw = json.dumps(args)
                with function_span(name=name, input=raw, parent=role.span) as span:
                    context = ToolContext(
                        context=role.context.context,
                        usage=role.context.usage,
                        tool_name=name,
                        tool_call_id=params["callId"],
                        tool_arguments=raw,
                        agent=role.agent,
                        run_config=role.run_config,
                    )
                    try:
                        output = await tool.on_invoke_tool(context, raw)
                    except BaseException as exc:  # noqa: BLE001 - retain cancellation/fatal tool state
                        span.set_error({"message": type(exc).__name__, "data": {}})
                        self._fail(role, exc)
                        return self._reply(False, f"Tool execution failed: {exc}")
                    span.span_data.output = str(output)
                return self._reply(True, output)
        except BaseException as exc:  # noqa: BLE001 - bridge must reply before interrupting
            self._fail(role, exc)
            return self._reply(
                False, f"Tool execution failed: {type(exc).__name__}: {exc}"
            )
        finally:
            role.tasks.discard(task)
            if role.fatal is not None or role.abort is not None:
                role.failed_calls.add(params.get("callId"))

    @staticmethod
    def _fail(role, exc):
        from .workflow import is_fatal_error

        if is_fatal_error(exc):
            role.fatal = exc
        else:
            role.abort = exc
        role.stopping = True

    @staticmethod
    def _reply(success, value):
        return {
            "success": success,
            "contentItems": [{"type": "inputText", "text": str(value)}],
        }

    async def _submit(self, role, args):
        if role.accepted:
            if args == role.arguments:
                return self._reply(True, json.dumps(role.receipt))
            return self._reply(False, "Submission is sealed and cannot be overwritten")
        try:
            output = (
                args["output"]
                if role.agent.output_type in (None, str)
                else TypeAdapter(role.agent.output_type).validate_python(args)
            )
            if role.agent.output_type in (None, str) and not isinstance(output, str):
                raise ValueError("output must be a string")
            for guardrail in role.agent.output_guardrails:
                checked = await guardrail.run(role.context, role.agent, output)
                if checked.output.tripwire_triggered:
                    raise ValueError(
                        f"Guardrail {guardrail.get_name()}: {checked.output.output_info}"
                    )
            if role.validate is not None:
                try:
                    await _resolve(role.validate(output))
                except BaseException as exc:
                    if isinstance(exc, ValueError) and not getattr(
                        role.validate, "codex_side_effecting", False
                    ):
                        raise
                    role.abort, role.stopping = exc, True
                    return self._reply(
                        False,
                        f"Submission callback failed: {type(exc).__name__}: {exc}",
                    )
        except (ValueError, TypeError, KeyError) as exc:
            # Validation is pure contract checking, never scientific acceptance.
            from .workflow import is_fatal_error

            if is_fatal_error(exc):
                role.fatal, role.stopping = exc, True
            return self._reply(
                False, f"Submission rejected: {type(exc).__name__}: {exc}"
            )
        role.output, role.arguments, role.accepted = output, args, True
        role.receipt = {"accepted": True, "role": role.agent.name}
        if self._config is not None:
            sidecar = self._bindings / f"{role.thread.id}.json"
            saved = json.loads(sidecar.read_text())
            saved["sealed"] = True
            draft = sidecar.with_suffix(".tmp")
            draft.write_text(json.dumps(saved))
            draft.chmod(0o600)
            draft.replace(sidecar)
        self._event(
            "submission.accepted", thread_id=role.thread.id, role=role.agent.name
        )
        return self._reply(True, json.dumps(role.receipt))

    async def _stop(self, role):
        role.stopping = True
        pending = list(role.tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if role.handle is not None and not role.terminal:
            await self._interrupt(role)

    @staticmethod
    async def _interrupt(role):
        if role.interrupt_requested or role.terminal:
            return
        role.interrupt_requested = True
        try:
            await role.handle.interrupt()
        except InvalidRequestError as exc:
            if exc.code != -32600 or "no active turn to interrupt" not in exc.message:
                raise

    async def run(
        self,
        agent,
        input,
        *,
        max_turns=12,
        run_config=None,
        error_handlers=None,
        validate=None,
        context=None,
    ):
        if max_turns is not None and not self.allow_native_response_budget:
            raise CodexRoleError(
                "Codex SDK cannot enforce max_turns as model responses; explicit "
                "allow_native_response_budget acknowledgement is required"
            )
        if not self._entered and self._client is None:
            raise CodexRoleError("Enter CodexRoleRunner before running a role")
        if self._blocked_error is not None:
            raise self._blocked_error
        history = input if isinstance(input, list) else []
        refs = [item for item in history if item.get("type") == REFERENCE]
        resume_id = None
        role = None
        if refs:
            if len(refs) != 1 or history[0] != refs[0]:
                raise ValueError(
                    "A continuation requires one leading native thread reference"
                )
            resume_id = refs[0]["thread_id"]
            role = self._roles.get(resume_id)
            if not re.fullmatch(r"[A-Za-z0-9_-]+", resume_id):
                raise ValueError("Invalid native thread identifier")
            if role is None and self.state_dir is None:
                raise CodexRoleError(
                    "Cross-process continuation requires explicit state_dir"
                )
        if role is not None:
            if role.agent is not agent or role.fatal is not None:
                raise CodexRoleError(
                    "Native role thread is unavailable or unsafe to resume"
                )
            if role.accepted:
                raise CodexRoleError("A sealed submission cannot be reopened")
            prompt = json.dumps(history[1:])
            role.validate = validate
            role.stopping, role.abort = False, None
            if role.client is None:
                role.client = await self._new_client()
                await role.client._client.thread_resume(
                    resume_id,
                    {"permissions": "study", "model": MODEL, "excludeTurns": True},
                )
                role.thread = AsyncThread(role.client, resume_id)
        else:
            tools = {}
            wrapper = RunContextWrapper(context=context)
            for tool in agent.tools:
                if not isinstance(tool, FunctionTool):
                    raise TypeError(
                        "Codex roles accept only explicit FunctionTool handlers"
                    )
                if (
                    tool.needs_approval is not False
                    or tool.tool_input_guardrails
                    or tool.tool_output_guardrails
                ):
                    raise ValueError(
                        "Approval/tool guardrail policies require explicit host support"
                    )
                enabled = tool.is_enabled
                if callable(enabled):
                    enabled = await _resolve(enabled(wrapper, agent))
                if enabled:
                    if tool.name in tools:
                        raise ValueError("Duplicate role tool name")
                    tools[tool.name] = tool
            submit = "submit_" + re.sub(r"[^a-z0-9_]", "_", agent.name.lower())
            if submit in tools:
                raise ValueError("Role tool collides with submission tool")
            schema = (
                {
                    "type": "object",
                    "properties": {"output": {"type": "string"}},
                    "required": ["output"],
                    "additionalProperties": False,
                }
                if agent.output_type in (None, str)
                else TypeAdapter(agent.output_type).json_schema()
            )
            role = _Role(agent, tools, submit, schema, wrapper, validate, run_config)
            instructions = agent.instructions
            if callable(instructions):
                instructions = await _resolve(instructions(wrapper, agent))
            instructions = (instructions or "") + (
                f"\nSubmit the stage result using {submit}. Fix rejected submissions "
                "in this thread. After acceptance, give a brief natural-language "
                "summary. Submission does not replace independent scientific Review."
            )
            params = {
                "model": MODEL,
                "allowModelFallback": False,
                "cwd": str(self._cwd),
                "baseInstructions": instructions,
                "approvalPolicy": "never",
                "permissions": "study",
                "environments": [],
                "config": {"model_reasoning_effort": "medium"},
                "dynamicTools": [
                    {
                        "type": "function",
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": tool.params_json_schema,
                    }
                    for tool in tools.values()
                ]
                + [
                    {
                        "type": "function",
                        "name": submit,
                        "description": "Validate and seal this stage result; then summarize in natural language.",
                        "inputSchema": schema,
                    }
                ],
            }
            binding = hashlib.sha256(
                json.dumps(
                    {
                        "role": agent.name,
                        "instructions": instructions,
                        "tools": params["dynamicTools"],
                        "model": MODEL,
                        "effort": "medium",
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            if resume_id is not None:
                saved = json.loads((self._bindings / f"{resume_id}.json").read_text())
                if saved != {"binding": binding, "sealed": False}:
                    raise CodexRoleError(
                        "Native thread role/tool binding differs or submission is sealed"
                    )
            role.client = await self._new_client()
            if resume_id is not None:
                started = await role.client._client.thread_resume(
                    resume_id,
                    {"permissions": "study", "model": MODEL, "excludeTurns": True},
                )
            else:
                started = await role.client._client.thread_start(params)
            if started.model != MODEL or started.reasoning_effort != EFFORT:
                raise CodexRoleError(
                    "Codex runtime changed the required model or reasoning effort"
                )
            role.thread = AsyncThread(role.client, started.thread.id)
            if self._config is not None:
                sidecar = self._bindings / f"{role.thread.id}.json"
                if resume_id is None:
                    with sidecar.open("x") as stream:
                        stream.write(json.dumps({"binding": binding, "sealed": False}))
                    sidecar.chmod(0o600)
            self._roles[role.thread.id] = role
            prompt = (
                json.dumps(history[1:])
                if resume_id
                else input
                if isinstance(input, str)
                else json.dumps(input)
            )
        self._event(
            "role.start",
            role=agent.name,
            thread_id=role.thread.id,
            model=MODEL,
            effort="medium",
            requested_max_model_responses=max_turns,
            model_response_count=None,
        )
        with agent_span(name=agent.name, tools=[*role.tools, role.submit]) as span:
            role.span = span
            role.terminal = False
            role.interrupt_requested = False
            consume = None
            try:
                starting = asyncio.create_task(self._turn(role, prompt))
                try:
                    role.handle = await asyncio.shield(starting)
                except asyncio.CancelledError:
                    # SDK cancellation while turn/start is pending only discards
                    # its subscription. Retain the handle so we can stop/drain it.
                    await self._stop(role)
                    role.handle = await asyncio.shield(starting)
                    consume = asyncio.create_task(self._consume(role))
                    raise
                consume = asyncio.create_task(self._consume(role))
                summary = await asyncio.shield(consume)
                if role.fatal is not None:
                    raise role.fatal
                if role.abort is not None:
                    raise role.abort
                if not role.accepted:
                    raise CodexRoleError("Role ended without an accepted submission")
                return CodexRoleResult(
                    role.output,
                    role.thread.id,
                    summary,
                    summary_missing=not bool(summary.strip()),
                )
            except BaseException as exc:
                if not role.terminal:
                    await self._stop(role)
                if consume is not None:
                    await asyncio.shield(
                        asyncio.gather(consume, return_exceptions=True)
                    )
                span.set_error({"message": type(exc).__name__, "data": {}})
                if role.accepted and role.fatal is None and isinstance(exc, Exception):
                    self._event(
                        "role.summary_missing",
                        thread_id=role.thread.id,
                        error_type=type(exc).__name__,
                    )
                    return CodexRoleResult(
                        role.output, role.thread.id, "", summary_missing=True
                    )
                exc.run_data = SimpleNamespace(
                    history=[{"type": REFERENCE, "thread_id": role.thread.id}],
                    output=role.output,
                    accepted=role.accepted,
                    receipt=role.receipt,
                    native_error=role.native_error,
                )
                raise
            finally:
                role.handle = None
                if self._config is not None and role.client is not None:
                    await role.client.close()
                    self._clients.remove(role.client)
                    role.client = None

    async def _consume(self, role):
        summary = ""
        completed = None
        async for event in role.handle.stream():
            payload = _json(event.payload)
            self._event(
                "codex.native",
                method=event.method,
                data=payload,
                role=role.agent.name,
                thread_id=role.thread.id,
                turn_id=role.handle.id,
            )
            if event.method == "item/completed":
                item = payload.get("item", {})
                if (
                    item.get("type") == "dynamicToolCall"
                    and item.get("id") in role.failed_calls
                ):
                    # Native completion confirms receipt of the tool response.
                    # Interrupt only after this protocol boundary.
                    role.failed_calls.discard(item["id"])
                    await self._interrupt(role)
                if item.get("type") == "agentMessage" and item.get("phase") in (
                    None,
                    "final_answer",
                ):
                    summary = item.get("text", "")
            elif event.method == "turn/completed":
                completed = payload["turn"]
                role.terminal = True
                role.native_error = completed.get("error")
                error_text = json.dumps(role.native_error)
                if any(
                    code in error_text
                    for code in ('"usageLimitExceeded"', '"unauthorized"')
                ):
                    self._blocked_error = CodexRoleError(
                        f"Codex subscription requires external recovery: {error_text}"
                    )
        if completed is None:
            raise CodexRoleError("Codex stream ended without terminal confirmation")
        if (
            completed["status"] != "completed"
            and role.fatal is None
            and role.abort is None
        ):
            raise CodexRoleError(f"Codex turn ended: {completed}")
        return summary
