"""Exact Pi CLI transport, inheriting the runtime-owned callback process group.

No model is contacted on import/config validation. Raw prompts and streams stay
in a private per-call directory. The supervisor owns descendant-group retirement;
this transport synchronously reaps its direct CLI on success and local timeout.
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

from research_loop.harness import HarnessError, iter_pi_events
from research_loop.runtime.operation_contract import fields, json_value, text
from research_loop.runtime.model_requests import register_trace

from .tool_bridge import serve


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
    except (ValueError, TypeError):
        return False


def _submission(path, config, tools=()):
    """Validate every completed turn and exactly one terminating submission."""
    pending = {}
    seen = set()
    submitted = None
    settled = False
    allowed = {"submit_result", *tools}
    for event in iter_pi_events(path):
        if settled:
            raise HarnessError("Unexpected event after Pi submission")
        kind = event.get("type")
        if (
            submitted is not None
            and kind == "message_start"
            and (event.get("message") or {}).get("role") == "assistant"
        ):
            raise HarnessError("Pi started another assistant response after submission")
        if submitted is not None and kind in {"turn_start", "agent_start"}:
            raise HarnessError("Pi started another turn after submission")
        if kind == "message_end":
            message = event.get("message") or {}
            role = message.get("role")
            if role in {"system", "user"} and not pending and submitted is None:
                continue
            if role == "assistant":
                if pending or submitted is not None:
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
                    identity, name, args = (
                        call.get("id"),
                        call.get("name"),
                        call.get("arguments"),
                    )
                    if (
                        not isinstance(identity, str)
                        or not identity
                        or identity in seen
                        or name not in allowed
                        or not isinstance(args, dict)
                    ):
                        raise HarnessError(
                            "Invalid, unauthorized, or duplicate Pi tool call"
                        )
                    seen.add(identity)
                    pending[identity] = {
                        "name": name,
                        "arguments": args,
                        "phase": "start",
                    }
            elif role == "toolResult":
                item = pending.get(message.get("toolCallId"))
                if (
                    item is None
                    or item["phase"] != "receipt"
                    or message.get("toolName") != item["name"]
                    or message.get("isError") is not False
                    or not _same(message.get("details"), item["result"])
                ):
                    raise HarnessError("Unexpected Pi tool receipt")
                del pending[message["toolCallId"]]
            else:
                raise HarnessError("Unexpected Pi message")
        elif kind in {"tool_execution_start", "tool_execution_end"}:
            item = pending.get(event.get("toolCallId"))
            if item is None or event.get("toolName") != item["name"]:
                raise HarnessError("Unexpected Pi tool execution")
            if kind == "tool_execution_start":
                if item["phase"] != "start" or not _same(
                    event.get("args"), item["arguments"]
                ):
                    raise HarnessError("Invalid Pi tool execution start")
                item["phase"] = "end"
            else:
                result = event.get("result") or {}
                if item["phase"] != "end" or event.get("isError") is not False:
                    raise HarnessError("Pi tool execution failed or was interrupted")
                if item["name"] == "submit_result":
                    if (
                        submitted is not None
                        or result.get("terminate") is not True
                        or not _same(result.get("details"), item["arguments"])
                    ):
                        raise HarnessError("Invalid terminating submit_result receipt")
                    submitted = item["arguments"]
                elif result.get("terminate"):
                    raise HarnessError("Nonterminal tool terminated the invocation")
                item["result"] = result.get("details")
                item["phase"] = "receipt"
        elif kind == "agent_settled":
            if pending or submitted is None or event.get("aborted") is not False:
                raise HarnessError("Pi did not settle a completed submission")
            settled = True
    if not settled:
        raise HarnessError("Incomplete Pi submit_result stream")
    return json_value(submitted)


def run(
    prompt,
    config,
    *,
    response_schema,
    invocation=None,
    tools=None,
    tool_handler=None,
    request_budget=None,
    history=None,
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
    tools = json_value(tools or {})
    if not isinstance(tools, dict) or "submit_result" in tools:
        raise ValueError(
            "Native tool bindings must be a mapping excluding submit_result"
        )
    for name, declaration in tools.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("Invalid native tool name")
        fields(declaration, {"description", "parameters"}, "native tool declaration")
        text(declaration["description"], "tool description")
        if not isinstance(declaration["parameters"], dict):
            raise ValueError("Native tool parameters must be a JSON schema")
    if tools and not callable(tool_handler):
        raise ValueError("Native tools require a trusted Python handler")
    history = [] if history is None else history
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
    (directory / "prompt.txt").write_text(prompt, encoding="utf-8")
    (directory / "response-schema.json").write_text(
        json.dumps(response_schema, allow_nan=False), encoding="utf-8"
    )
    seconds = config["timeout_seconds"]
    if request_budget is not None and request_budget.get("deadline") is not None:
        seconds = min(seconds, request_budget["deadline"] - wall_time())
    deadline = time.monotonic() + seconds
    deadline_ms = (wall_time() + seconds) * 1000
    common = [
        config["executable"],
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
    ]

    def execute(arguments, stem, input_text=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Pi invocation timeout")
        with (
            (directory / (stem + ".stdout.jsonl")).open("w") as stdout,
            (directory / (stem + ".stderr.txt")).open("w") as stderr,
        ):
            # No new session/group: CLI descendants belong to the callback group.
            child = subprocess.Popen(
                arguments,
                stdin=subprocess.PIPE,
                stdout=stdout,
                stderr=stderr,
                text=True,
                cwd=directory,
            )
            try:
                child.communicate(input=input_text, timeout=remaining)
            except BaseException:
                child.kill()
                child.wait()
                raise
            if child.returncode:
                raise HarnessError("Pi CLI exited with code " + str(child.returncode))

    execute([*common, "--no-tools", "--offline", "--list-models"], "preflight")
    rows = (directory / "preflight.stdout.jsonl").read_text().splitlines()
    if not any(
        row.split()[:2] == [config["provider"], config["model"]] for row in rows
    ):
        raise HarnessError(
            "Pi catalog lacks the exact configured provider/model; no fallback"
        )
    with serve(directory, tools, tool_handler, request_budget, deadline, history) as (
        socket_path,
        state,
    ):
        (directory / "tool-binding.json").write_text(
            json.dumps(
                {
                    "socket": socket_path,
                    "tools": tools,
                    "deadline_ms": deadline_ms,
                    "submit_description": submit_description,
                },
                allow_nan=False,
            ),
            encoding="utf-8",
        )
        execute(
            [
                *common,
                "--no-builtin-tools",
                "--tools",
                ",".join([*tools, "submit_result"]),
                "-e",
                str(Path(__file__).resolve().with_name("submit_result.ts")),
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
            "response",
            prompt,
        )
        if state["failed"]:
            raise HarnessError("Native tool or provider request admission failed")
        return _submission(directory / "response.stdout.jsonl", config, tools)
