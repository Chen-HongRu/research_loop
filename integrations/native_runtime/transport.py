"""Pi native file tools inside host-enforced filesystem capabilities.

Only submission and provider admission use the bridge. Prompts, native tool
arguments and results are not retained in transport logs.
"""

import json
import math
import os
import re
from pathlib import Path
import subprocess
import tempfile
import time
from time import time as wall_time

from research_loop.harness import HarnessError
from research_loop.runtime.operation_contract import fields, json_value, text
from research_loop.runtime.model_requests import register_trace

from .tool_bridge import serve
from . import filesystem as host_filesystem


SYSTEM_PROMPT = (
    "Follow the supplied role and frozen ContextPackage. Finish by calling submit_result "
    "exactly once with the complete output and evidence_refs. Use only the supplied tools. "
    "You may retrieve authorized knowledge over multiple turns before submitting. "
    "Call submit_result alone; its arguments are your final answer."
)


SUBMIT_DESCRIPTION = "Submit complete current-role output with exact evidence_refs to end this invocation."


def validate_config(config):
    value = json_value(config)
    if not isinstance(value, dict):
        raise ValueError("Transport configuration must be an object")
    optional = {"private_calls_dir"} & value.keys()
    fields(
        value,
        {"backend", "executable", "provider", "model", "reasoning", "timeout_seconds"}
        | optional,
        "Pi transport",
    )
    if value["backend"] != "pi":
        raise ValueError("Only the Pi CLI native transport is implemented")
    for name in ("executable", "provider", "model", "reasoning"):
        text(value[name], name)
    if value["reasoning"] not in {"off", "minimal", "low", "medium", "high", "xhigh"}:
        raise ValueError("Unsupported explicit Pi thinking level")
    seconds = value["timeout_seconds"]
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Transport timeout must be finite and positive")
    if "private_calls_dir" in value and (
        not isinstance(value["private_calls_dir"], str)
        or not Path(value["private_calls_dir"]).is_absolute()
    ):
        raise ValueError("Private call directory must be an absolute path")
    return value


def _same(left, right):
    try:
        return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
            right, sort_keys=True, allow_nan=False
        )
    except (TypeError, ValueError):
        return False


def _submission(events, config, tools=()):
    """Validate terminal identity and receipts without retaining native tool bodies."""
    terminal = None
    phase = None
    pending = {}
    seen = set()
    settled = False
    allowed = {"submit_result", *tools}
    for event in events:
        if not isinstance(event, dict) or settled:
            raise HarnessError("Unexpected event after Pi submission")
        kind = event.get("type")
        message = event.get("message") or {}
        if terminal is not None and (
            kind in {"turn_start", "agent_start"}
            or kind == "message_start"
            and message.get("role") == "assistant"
        ):
            raise HarnessError("Pi started another response after submission")
        if kind == "message_end":
            role = message.get("role")
            if role in {"system", "user"} and terminal is None and not pending:
                continue
            if role == "assistant":
                if terminal is not None or pending:
                    raise HarnessError("Unexpected Pi assistant response")
                if any(
                    message.get(key) != config[field]
                    for key, field in (
                        ("provider", "provider"),
                        ("model", "model"),
                        ("thinkingLevel", "reasoning"),
                    )
                ):
                    raise HarnessError(
                        "Pi response provider/model/thinking differs from the exact request"
                    )
                blocks = message.get("content")
                if not isinstance(blocks, list) or any(
                    not isinstance(block, dict)
                    or block.get("type") not in {"thinking", "text", "toolCall"}
                    for block in blocks
                ):
                    raise HarnessError("Invalid Pi assistant content")
                calls = [block for block in blocks if block["type"] == "toolCall"]
                if (
                    not calls
                    or message.get("stopReason") not in {"toolUse", "stop"}
                    or message.get("errorMessage")
                ):
                    raise HarnessError("Pi assistant did not complete a tool turn")
                if (
                    any(call.get("name") == "submit_result" for call in calls)
                    and len(calls) != 1
                ):
                    raise HarnessError(
                        "submit_result must be the only call in its turn"
                    )
                for call in calls:
                    identity, name = call.get("id"), call.get("name")
                    if (
                        not isinstance(identity, str)
                        or not identity
                        or identity in seen
                        or name not in allowed
                        or not isinstance(call.get("arguments"), dict)
                    ):
                        raise HarnessError(
                            "Invalid, unauthorized, or duplicate Pi tool call"
                        )
                    seen.add(identity)
                    if name == "submit_result":
                        terminal, phase = call, "start"
                    else:
                        # Only execution identity is needed for native tools.
                        pending[identity] = {"name": name, "phase": "start"}
            elif role == "toolResult":
                identity = message.get("toolCallId")
                if terminal is not None:
                    if (
                        identity != terminal["id"]
                        or message.get("toolName") != "submit_result"
                        or phase != "receipt"
                        or message.get("isError") is not False
                        or not _same(message.get("details"), terminal["arguments"])
                    ):
                        raise HarnessError("Unexpected Pi terminal tool receipt")
                    phase = "done"
                else:
                    item = pending.get(identity)
                    if (
                        item is None
                        or item["name"] != message.get("toolName")
                        or item["phase"] != "receipt"
                    ):
                        raise HarnessError("Unexpected Pi native tool receipt")
                    del pending[identity]
            else:
                raise HarnessError("Unexpected Pi message")
        elif kind in {"tool_execution_start", "tool_execution_end"}:
            identity, name = event.get("toolCallId"), event.get("toolName")
            if terminal is not None:
                if identity != terminal["id"] or name != "submit_result":
                    raise HarnessError("Unexpected execution after Pi submission")
                if kind == "tool_execution_start":
                    if phase != "start" or not _same(
                        event.get("args"), terminal["arguments"]
                    ):
                        raise HarnessError("Invalid Pi terminal execution start")
                    phase = "end"
                else:
                    result = event.get("result") or {}
                    if (
                        phase != "end"
                        or event.get("isError") is not False
                        or result.get("terminate") is not True
                        or not _same(result.get("details"), terminal["arguments"])
                    ):
                        raise HarnessError("Invalid terminating submit_result receipt")
                    phase = "receipt"
            else:
                item = pending.get(identity)
                if item is None or item["name"] != name:
                    raise HarnessError("Unexpected Pi native tool execution")
                if kind == "tool_execution_start":
                    if item["phase"] != "start":
                        raise HarnessError("Repeated Pi native tool start")
                    item["phase"] = "end"
                else:
                    if item["phase"] != "end" or (event.get("result") or {}).get(
                        "terminate"
                    ):
                        raise HarnessError("Invalid native tool termination")
                    item["phase"] = "receipt"
        elif kind == "agent_settled":
            if (
                pending
                or terminal is None
                or phase != "done"
                or event.get("aborted") is not False
            ):
                raise HarnessError("Pi did not settle a completed submission")
            settled = True
    if not settled:
        raise HarnessError("Incomplete Pi submit_result stream")
    return json_value(terminal["arguments"])


def run(
    prompt,
    config,
    *,
    response_schema,
    invocation=None,
    tools=None,
    filesystem=None,
    request_budget=None,
    system_prompt=None,
    submit_description=None,
):
    config = validate_config(config)
    system_prompt = SYSTEM_PROMPT if system_prompt is None else system_prompt
    submit_description = (
        SUBMIT_DESCRIPTION if submit_description is None else submit_description
    )
    text(system_prompt, "system prompt")
    text(submit_description, "submit description")
    if tools is None:
        tools = []
    if (
        not isinstance(tools, (list, tuple))
        or any(
            not isinstance(name, str) or name not in host_filesystem.NATIVE_TOOLS
            for name in tools
        )
        or len(tools) != len(set(tools))
    ):
        raise ValueError("Pi tools must be a unique list of native file tool names")
    roots, writable, mounts = host_filesystem.capabilities(filesystem, tools)
    metadata = None
    prefix = "native-call-"
    if invocation is not None:
        metadata = json_value(invocation)
        fields(metadata, {"call", "context_ref"}, "private invocation metadata")
        if not isinstance(metadata["call"], dict):
            raise ValueError("Invocation call identity must be an object")
        identity = metadata["call"].get("attempt_id")
        text(identity, "call attempt identity")
        text(metadata["context_ref"], "context reference")
        prefix = "call-" + re.sub(r"[^A-Za-z0-9_-]", "_", identity)[:128] + "-"
    root = config.get("private_calls_dir")
    if root is None and request_budget is not None:
        root = str(Path(request_budget["study_root"]) / "native-calls")
    if root:
        Path(root).mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = Path(tempfile.mkdtemp(prefix=prefix, dir=root))
    os.chmod(directory, 0o700)
    if request_budget is not None:
        register_trace(request_budget, directory)
    if metadata is not None:
        (directory / "invocation.json").write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
        )
    seconds = config["timeout_seconds"]
    if request_budget is not None and request_budget.get("deadline") is not None:
        seconds = min(seconds, request_budget["deadline"] - wall_time())
    deadline = time.monotonic() + seconds
    deadline_ms = (wall_time() + seconds) * 1000
    with tempfile.TemporaryDirectory(prefix="rl-pi-session-", dir="/tmp") as temporary:
        session = Path(temporary).resolve()
        workspace = session / "workspace"
        workspace.mkdir()
        control = session / "control"
        control.mkdir()
        state_dir = session / "agent"
        environment = host_filesystem.prepare_agent(state_dir, config["provider"])
        knowledge = workspace / "knowledge"
        knowledge.mkdir()
        for alias, target in mounts.items():
            (knowledge / alias).symlink_to(target, target_is_directory=True)
        if writable is not None:
            (workspace / "draft").symlink_to(writable, target_is_directory=True)
        extension = control / "submit_result.ts"
        extension.write_bytes(Path(__file__).with_name("submit_result.ts").read_bytes())
        binary_dir = control / "bin"
        binary_dir.mkdir()
        for name, tool in (("rg", "grep"), ("fd", "find")):
            binary = host_filesystem.native_binary(name)
            if binary is not None:
                (binary_dir / name).symlink_to(binary)
            elif tool in tools:
                raise HarnessError(
                    f"Native Pi {tool} requires a local {name} executable"
                )
        environment["PATH"] = str(binary_dir) + os.pathsep + environment.get("PATH", "")
        with serve(
            request_budget, deadline, startup_profile=state_dir / "models.json"
        ) as (socket_path, state):
            environment["RL_PI_BINDING"] = json.dumps(
                {
                    "socket": socket_path,
                    "schema": response_schema,
                    "deadline_ms": deadline_ms,
                    "submit_description": submit_description,
                    "provider": config["provider"],
                    "model": config["model"],
                    "reasoning": config["reasoning"],
                },
                allow_nan=False,
            )
            launcher = host_filesystem.command(
                config["executable"],
                workspace,
                control,
                state_dir,
                socket_path,
                roots,
                writable,
            )
            common = [
                *launcher,
                "--no-extensions",
                "--no-skills",
                "--no-prompt-templates",
                "--no-mcp",
                "--no-approve",
                "--no-context-files",
                "--system-prompt",
                system_prompt,
                "--append-system-prompt",
                "",
                "-e",
                str(extension),
            ]

            def execute(arguments, input_text=None):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Pi invocation timeout")
                child = subprocess.Popen(
                    arguments,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=workspace,
                    env=environment,
                )
                try:
                    stdout, stderr = child.communicate(
                        input=input_text, timeout=remaining
                    )
                except BaseException:
                    # Keep the original callback group under its supervisor.
                    # An inherited descendant pipe must not delay returning the
                    # failure so that supervisor can retire the entire group.
                    child.kill()
                    try:
                        child.wait(timeout=1)
                    finally:
                        for stream in (child.stdin, child.stdout, child.stderr):
                            if stream is not None:
                                stream.close()
                    raise
                if child.returncode:
                    raise HarnessError(
                        "Isolated Pi CLI exited with code "
                        + str(child.returncode)
                        + "; "
                        + stderr[-1024:]
                    )
                return stdout

            try:
                catalog = execute([*common, "--no-tools", "--offline", "--list-models"])
                if not any(
                    row.split()[:2] == [config["provider"], config["model"]]
                    for row in catalog.splitlines()
                ):
                    raise HarnessError(
                        "Pi catalog lacks the exact configured provider/model; no fallback"
                    )
                stdout = execute(
                    [
                        *common,
                        "--tools",
                        ",".join([*tools, "submit_result"]),
                        "--print",
                        "--mode",
                        "json",
                        "--provider",
                        config["provider"],
                        "--model",
                        config["model"],
                        "--thinking",
                        config["reasoning"],
                        "--no-session",
                    ],
                    prompt,
                )
                if state["failed"]:
                    raise HarnessError(
                        "Pi request admission failed: " + state.get("error", "unknown")
                    )
                result = _submission(
                    (json.loads(line) for line in stdout.split("\n") if line.strip()),
                    config,
                    tools,
                )
                if state.get("submission") != result:
                    raise HarnessError(
                        "Pi stream differs from the accepted terminal submission"
                    )
                return result
            except BaseException:
                state["failed"] = True
                raise
            finally:
                (directory / "outcome.json").write_text(
                    json.dumps(
                        {
                            "requests": state["requests"],
                            "submitted": state["submitted"],
                            "failed": state["failed"],
                        }
                    )
                )
