"""Pinned official SDK transport. The native lead owns all research delegation."""

import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from codex_cli_bin import bundled_codex_path
from openai_codex.client import CodexClient, CodexConfig
from openai_codex.models import UnknownNotification
from pydantic import BaseModel, RootModel


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    call: Callable[[dict], Awaitable[object]]

    def descriptor(self):
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.parameters,
        }


def plain(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, UnknownNotification):
        return value.params
    return value


def _toml(values):
    return (
        "{"
        + ",".join(json.dumps(str(k)) + "=" + json.dumps(v) for k, v in values.items())
        + "}"
    )


def configuration(root, project, writable, model, effort):
    home = root / "native"
    home.mkdir(mode=0o700, exist_ok=True)
    home.chmod(0o700)
    auth = Path.home() / ".codex/auth.json"
    if not auth.is_file():
        raise RuntimeError("Sign in to the official Codex CLI with ChatGPT first")
    if not (home / "auth.json").exists():
        (home / "auth.json").symlink_to(auth)
    paths = {
        ":root": "deny",
        ":minimal": "read",
        ":slash_tmp": "deny",
        ":tmpdir": "deny",
    }
    for path in (
        project,
        root,
        Path(sys.prefix),
        Path(sys.base_prefix),
        Path(bundled_codex_path()).parent.parent,
    ):
        paths[str(path.resolve())] = "read"
    base = Path(sys.base_prefix).absolute()
    if base.is_symlink() and base.parent not in {Path(base.anchor), Path.home()}:
        paths[str(base.parent)] = "read"
    for path in writable:
        paths[str(path.resolve())] = "write"
        for name in (".git", ".codex", ".agents", ".aws"):
            paths[str(path.resolve() / name)] = "read"
    for path in (Path.home() / ".codex", Path.home() / ".ssh", home):
        paths[str(path.resolve())] = "deny"
    shell_env = {
        "PATH": str(Path(sys.executable).parent) + ":" + os.defpath,
        "HOME": str(root / "work"),
        "TMPDIR": str(root / "work/tmp"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    overrides = [
        'forced_login_method="chatgpt"',
        "mcp_servers={}",
        "features.multi_agent=true",
        "features.multi_agent_v2=true",
        "features.multi_agent_v2_dynamic_tools=false",
        "features.plugins=false",
        "features.apps=false",
        "features.hooks=false",
        "features.skip_host_skill_discovery=true",
        "allow_login_shell=false",
        'shell_environment_policy.inherit="none"',
        "shell_environment_policy.set=" + _toml(shell_env),
        "permissions.research.filesystem=" + _toml(paths),
        "permissions.research.network.enabled=false",
        'default_permissions="research"',
        "model=" + json.dumps(model),
        "model_reasoning_effort=" + json.dumps(effort),
        'web_search="live"',
    ]
    environment = {
        "HOME": str(Path.home()),
        "CODEX_HOME": str(home),
        "PATH": os.environ.get("PATH", os.defpath),
    }
    for key, value in os.environ.items():
        if key.lower().endswith("_proxy") or key in {
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "NODE_EXTRA_CA_CERTS",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "TZ",
        }:
            environment[key] = value
    command = [
        "/usr/bin/env",
        "-i",
        *(f"{k}={v}" for k, v in environment.items()),
        str(bundled_codex_path()),
    ]
    for item in overrides:
        command.extend(["--config", item])
    command.extend(["app-server", "--listen", "stdio://"])
    return CodexConfig(cwd=str(project), launch_args_override=tuple(command)), {
        "model": model,
        "effort": effort,
        "filesystem": paths,
        "network": False,
        "native_multi_agent": True,
        "child_dynamic_tools": False,
    }


class Native:
    def __init__(self, config, journal, tools=(), *, client_factory=CodexClient):
        self.journal = journal
        self.tools = {tool.name: tool for tool in tools}
        self.client = client_factory(config=config, approval_handler=self._request)
        self.thread_id = None
        self.turn_id = None
        self.children = set()
        self.stopping = False
        self.loop = None
        self.active = False
        self.settled = False
        self.closed = False

    async def open(self):
        self.loop = asyncio.get_running_loop()
        await asyncio.to_thread(self.client.start)
        await asyncio.to_thread(self.client.initialize)

    async def rpc(self, method, params):
        value = await asyncio.to_thread(
            self.client.request, method, params, response_model=RootModel[dict]
        )
        return plain(value)

    def _request(self, method, params):
        if method == "item/tool/call":
            return asyncio.run_coroutine_threadsafe(
                self._tool(params or {}), self.loop
            ).result()
        if method.endswith("requestApproval"):
            return {"decision": "decline"}
        if method == "item/tool/requestUserInput":
            return {"answers": {}}
        return {}

    async def _tool(self, params):
        name = params.get("tool")
        success = False
        try:
            if self.stopping or params.get("threadId") != self.thread_id:
                raise ValueError(
                    "External resource tools are registered for the lead thread only"
                )
            tool = self.tools[name]
            output = await tool.call(params.get("arguments", {}))
            success = True
        except Exception as error:
            output = {"error": type(error).__name__, "message": str(error)}
        self.journal.emit(
            "resource.tool",
            thread_id=params.get("threadId"),
            tool=name,
            arguments=params.get("arguments"),
            success=success,
            output=output,
        )
        return {
            "success": success,
            "contentItems": [
                {"type": "inputText", "text": json.dumps(output, ensure_ascii=False)}
            ],
        }

    async def verify(self, model, effort):
        account = await self.rpc("account/read", {"refreshToken": False})
        if (account.get("account") or {}).get("type") != "chatgpt":
            raise RuntimeError(
                "ChatGPT subscription authentication is required; no API-key fallback"
            )
        catalog = await self.rpc("model/list", {"includeHidden": True})
        matches = [m for m in catalog.get("data", []) if m.get("model") == model]
        if not matches or not any(
            item.get("reasoningEffort") == effort
            or item.get("reasoning_effort") == effort
            for item in matches[0].get(
                "supportedReasoningEfforts",
                matches[0].get("supported_reasoning_efforts", []),
            )
        ):
            raise RuntimeError(
                "The exact configured model/reasoning effort is unavailable"
            )

    async def run(
        self,
        instructions,
        payload,
        schema,
        *,
        model,
        effort,
        thread_id=None,
        on_started=None,
    ):
        if self.stopping:
            raise RuntimeError("Stop requested before model admission")
        definition = {
            "model": model,
            "allowProviderModelFallback": False,
            "developerInstructions": instructions,
            "permissions": "research",
            "approvalPolicy": "never",
            "config": {"model_reasoning_effort": effort},
            "dynamicTools": [tool.descriptor() for tool in self.tools.values()],
        }
        if thread_id:
            result = await asyncio.to_thread(
                self.client.thread_resume,
                thread_id,
                {
                    "model": model,
                    "permissions": "research",
                    "approvalPolicy": "never",
                    "excludeTurns": True,
                    "developerInstructions": instructions,
                },
            )
        else:
            result = await asyncio.to_thread(self.client.thread_start, definition)
        started = plain(result)
        if started.get("model") != model or started.get("reasoningEffort") != effort:
            raise RuntimeError("Codex changed the required model or reasoning effort")
        self.thread_id = started["thread"]["id"]
        if on_started:
            on_started(self.thread_id)
        self.journal.emit(
            "lead.input",
            thread_id=self.thread_id,
            instructions=instructions,
            input=payload,
            output_schema=schema,
            tools=definition["dynamicTools"],
        )
        if self.stopping:
            raise RuntimeError("Stop requested before model admission")
        started_turn = plain(
            await asyncio.to_thread(
                self.client.turn_start,
                self.thread_id,
                json.dumps(payload, ensure_ascii=False),
                {"outputSchema": schema, "effort": effort},
            )
        )
        self.turn_id = started_turn["turn"]["id"]
        self.active = True
        if self.stopping:
            await self.stop()
        final = ""
        while True:
            notification = await asyncio.to_thread(
                self.client.next_turn_notification, self.turn_id
            )
            method = notification.method
            data = plain(notification.payload)
            self.journal.emit("codex.event", method=method, data=data)
            item = data.get("item", {})
            if item.get("type") == "collabAgentToolCall":
                self.children.update(item.get("receiverThreadIds", []))
            if item.get("type") == "subAgentActivity":
                self.children.add(item["agentThreadId"])
            if (
                method == "item/completed"
                and item.get("type") == "agentMessage"
                and item.get("phase") in (None, "final_answer")
            ):
                final = item.get("text", "")
            if method == "turn/completed":
                self.active = False
                turn = data["turn"]
                if turn["status"] != "completed":
                    raise RuntimeError(
                        f"Native turn ended: {turn['status']}; {turn.get('error')}"
                    )
                return json.loads(final)

    async def settle(self):
        if self.settled or self.closed:
            return
        # This bounds cleanup of local native work, not research/model usage.
        async with asyncio.timeout(30):
            await self._settle()
        self.settled = True

    async def _settle(self):
        # CLI 0.167's JSONL store cannot paginate thread turns. Its native
        # rollout paths are the authoritative child history, not a copied trace.
        loaded = (
            await self.rpc("thread/loaded/list", {}) if self.thread_id else {"data": []}
        )
        threads = set(loaded.get("data", [])) | self.children
        if self.thread_id:
            threads.add(self.thread_id)
        unsettled = []
        for thread in sorted(threads):
            history = plain(
                await asyncio.to_thread(self.client.thread_read, thread, False)
            )
            metadata = history["thread"]
            self.journal.emit(
                "codex.history.reference",
                thread_id=thread,
                path=metadata.get("path"),
                metadata=metadata,
            )
            if metadata.get("status", {}).get("type") == "active":
                unsettled.append(thread)
            if thread in loaded.get("data", []):
                receipt = await self.rpc(
                    "thread/backgroundTerminals/clean", {"threadId": thread}
                )
                self.journal.emit(
                    "codex.terminals.cleaned", thread_id=thread, receipt=receipt
                )
        if unsettled:
            raise RuntimeError(
                f"Native delegates are still active; retain the draft and inspect original histories: {unsettled}"
            )

    async def stop(self):
        self.stopping = True
        if self.thread_id and self.turn_id and self.active and not self.closed:
            await asyncio.to_thread(
                self.client.turn_interrupt, self.thread_id, self.turn_id
            )

    async def close(self):
        if not self.closed:
            await asyncio.to_thread(self.client.close)
            self.closed = True
