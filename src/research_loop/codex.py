"""Pinned official SDK transport. The native lead owns all research delegation."""

import asyncio
import json
import os
import sys
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from codex_cli_bin import bundled_codex_path
from openai_codex.client import CodexClient, CodexConfig
from openai_codex.models import UnknownNotification
from pydantic import BaseModel, RootModel

from .delegation import DELEGATE_TOOL, WAIT_TOOL, HostDelegation


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


BASE_INSTRUCTIONS = (
    "You are Codex, a coding agent. You and the user share one workspace. "
    "Use the available tools to inspect files, run commands and make edits, "
    "and follow the developer instructions and safety constraints supplied in "
    "the conversation.\n"
)

# A non-OpenAI Responses provider cannot decrypt inter-agent payloads, so the
# generated catalog selects the V1 delegation family that sends tasks as plain
# user messages. Hosted web search is unavailable through such a gateway.
PROVIDER_MULTI_AGENT_VERSION = "v1"


def model_catalog(
    model, effort, context_window, multi_agent_version=PROVIDER_MULTI_AGENT_VERSION
):
    levels = ["low", "medium", "high"]
    if effort not in levels:
        levels.append(effort)
    return {
        "models": [
            {
                "slug": model,
                "display_name": model,
                "description": "OpenAI-compatible provider model",
                "prefer_websockets": False,
                "support_verbosity": True,
                "default_verbosity": "low",
                "apply_patch_tool_type": "freeform",
                "web_search_tool_type": "text",
                "input_modalities": ["text"],
                "supports_image_detail_original": False,
                "truncation_policy": {"mode": "tokens", "limit": 10000},
                "supports_parallel_tool_calls": True,
                "tool_mode": None,
                "multi_agent_version": multi_agent_version,
                "use_responses_lite": False,
                "include_skills_usage_instructions": False,
                "auto_review_model_override": None,
                "context_window": context_window,
                "max_context_window": context_window,
                "effective_context_window_percent": 95,
                "auto_compact_token_limit": None,
                "comp_hash": "3000",
                "reasoning_summary_format": "experimental",
                "default_reasoning_summary": "none",
                "default_reasoning_level": effort,
                "supported_reasoning_levels": [
                    {"effort": level, "description": level} for level in levels
                ],
                "shell_type": "shell_command",
                "visibility": "list",
                "minimal_client_version": "0.144.0",
                "supported_in_api": True,
                "availability_nux": None,
                "upgrade": None,
                "priority": 1,
                "experimental_supported_tools": [],
                "supports_search_tool": False,
                "default_service_tier": None,
                "supports_reasoning_summaries": True,
                "base_instructions": BASE_INSTRUCTIONS,
            }
        ]
    }


def configuration(
    root, project, writable, model, effort, provider=None, delegation="native"
):
    home = root / "native"
    home.mkdir(mode=0o700, exist_ok=True)
    home.chmod(0o700)
    if provider is None:
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
    common = [
        "mcp_servers={}",
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
    effective = {
        "model": model,
        "effort": effort,
        "filesystem": paths,
        "network": False,
        "native_multi_agent": True,
        "child_dynamic_tools": False,
    }
    if provider is None:
        overrides = ['forced_login_method="chatgpt"']
        if delegation == "host":
            overrides.extend(
                ["features.multi_agent=false", "features.multi_agent_v2=false"]
            )
        else:
            overrides.extend(
                ["features.multi_agent=true", "features.multi_agent_v2=true"]
            )
        overrides.extend([*common, 'web_search="live"'])
    else:
        key_env = provider["api_key_env"]
        key = os.environ.get(key_env)
        if not key:
            raise RuntimeError(
                f"The provider API key environment variable {key_env} is unset"
            )
        name = provider["name"]
        catalog_version = None if delegation == "host" else PROVIDER_MULTI_AGENT_VERSION
        catalog = provider.get("model_catalog")
        if catalog is None:
            catalog = home / "models.json"
            catalog.write_text(
                json.dumps(
                    model_catalog(
                        model,
                        effort,
                        provider["context_window"],
                        multi_agent_version=catalog_version,
                    ),
                    indent=2,
                )
            )
            catalog.chmod(0o600)
        else:
            entry = next(
                (
                    item
                    for item in json.loads(Path(catalog).read_text()).get("models", [])
                    if item.get("slug") == model
                ),
                None,
            )
            if entry is None:
                raise RuntimeError(
                    "The provider model catalog does not declare the configured model"
                )
            if entry.get("multi_agent_version") != catalog_version:
                if delegation == "host":
                    raise RuntimeError(
                        "Host delegation requires a model catalog without "
                        "multi_agent_version so native subagent tools stay disabled"
                    )
                raise RuntimeError(
                    "Native provider delegation requires multi_agent_version v1 so "
                    "delegated tasks reach non-OpenAI models as plain user messages"
                )
            if entry.get("supports_search_tool") is not False:
                raise RuntimeError(
                    "Provider model catalogs require supports_search_tool=false so "
                    "V1 delegation tools are exposed directly"
                )
        environment[key_env] = key
        overrides = [
            "features.multi_agent=false"
            if delegation == "host"
            else "features.multi_agent=true",
            "features.multi_agent_v2=false",
            *common,
            'web_search="disabled"',
            "model_provider=" + json.dumps(name),
            f"model_providers.{name}="
            + _toml(
                {
                    "name": name,
                    "base_url": provider["base_url"],
                    "env_key": key_env,
                    "wire_api": "responses",
                }
            ),
            "model_catalog_json=" + json.dumps(str(catalog)),
        ]
        effective.update(
            model_provider=name,
            multi_agent_version=catalog_version,
            model_catalog=str(catalog),
            web_search=False,
        )
    effective["delegation"] = delegation
    command = [
        "/usr/bin/env",
        "-i",
        *(f"{k}={v}" for k, v in environment.items()),
        str(bundled_codex_path()),
    ]
    for item in overrides:
        command.extend(["--config", item])
    command.extend(["app-server", "--listen", "stdio://"])
    return CodexConfig(cwd=str(project), launch_args_override=tuple(command)), effective


class Native:
    def __init__(self, config, journal, tools=(), *, client_factory=CodexClient):
        self.journal = journal
        self.tools = {tool.name: tool for tool in tools}
        self.client = client_factory(config=config, approval_handler=self._request)
        self.delegator = None
        self.thread_id = None
        self.turn_id = None
        self.children = set()
        self.children_lock = threading.Lock()
        self.stopping = False
        self.loop = None
        self.active = False
        self.settled = False
        self.closed = False

    def _add_child(self, thread_id):
        with self.children_lock:
            self.children.add(thread_id)

    def _add_children(self, thread_ids):
        with self.children_lock:
            self.children.update(thread_ids)

    def _child_snapshot(self):
        with self.children_lock:
            return set(self.children)

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

    @staticmethod
    def _journal_arguments(name, arguments):
        if name == "delegate_task" and isinstance(arguments, dict):
            message = arguments.get("message")
            return {
                "role": arguments.get("role"),
                "task_name": arguments.get("task_name"),
                "message_chars": len(message) if isinstance(message, str) else None,
            }
        if name == "wait_tasks" and isinstance(arguments, dict):
            return {"task_ids": arguments.get("task_ids")}
        return arguments

    @staticmethod
    def _journal_output(name, output):
        if name == "wait_tasks" and isinstance(output, dict):
            rows = output.get("tasks")
            if isinstance(rows, list):
                return {
                    "tasks": [
                        {
                            "task_id": row.get("task_id"),
                            "status": row.get("status"),
                        }
                        for row in rows
                        if isinstance(row, dict)
                    ]
                }
        return output

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
            arguments=self._journal_arguments(name, params.get("arguments")),
            success=success,
            output=self._journal_output(name, output),
        )
        return {
            "success": success,
            "contentItems": [
                {"type": "inputText", "text": json.dumps(output, ensure_ascii=False)}
            ],
        }

    async def verify(self, model, effort, provider=None):
        account = await self.rpc("account/read", {"refreshToken": False})
        if provider is None and (account.get("account") or {}).get("type") != "chatgpt":
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
            if provider is not None:
                raise RuntimeError(
                    "The configured provider model/reasoning effort is missing "
                    "from the model catalog"
                )
            raise RuntimeError(
                "The exact configured model/reasoning effort is unavailable"
            )

    def install_delegation(self, model, effort, roles_path=None):
        if self.delegator is not None:
            return
        self.delegator = HostDelegation(
            self.client,
            self.journal,
            model=model,
            effort=effort,
            roles_path=roles_path,
            on_thread=self._add_child,
        )

        async def delegate(arguments):
            return self.delegator.start(
                arguments.get("role"),
                arguments.get("task_name"),
                arguments.get("message"),
            )

        async def wait(arguments):
            return self.delegator.wait(arguments.get("task_ids") or [])

        for spec, call in ((DELEGATE_TOOL, delegate), (WAIT_TOOL, wait)):
            if spec["name"] in self.tools:
                raise ValueError(f"Delegation tool name collides with {spec['name']}")
            self.tools[spec["name"]] = Tool(
                spec["name"], spec["description"], spec["parameters"], call
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
        delegation="native",
        roles_path=None,
    ):
        if self.stopping:
            raise RuntimeError("Stop requested before model admission")
        if delegation == "host":
            self.install_delegation(model, effort, roles_path)
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
                self._add_children(item.get("receiverThreadIds", []))
            if item.get("type") == "subAgentActivity":
                self._add_child(item["agentThreadId"])
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
        if self.delegator is not None:
            await asyncio.to_thread(self.delegator.settle)
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
        threads = set(loaded.get("data", [])) | self._child_snapshot()
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
        if self.delegator is not None:
            await asyncio.to_thread(self.delegator.stop)
        if self.thread_id and self.turn_id and self.active and not self.closed:
            await asyncio.to_thread(
                self.client.turn_interrupt, self.thread_id, self.turn_id
            )

    async def close(self):
        if not self.closed:
            if self.delegator is not None:
                await asyncio.to_thread(self.delegator.close)
            await asyncio.to_thread(self.client.close)
            self.closed = True
