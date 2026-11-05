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

from research_loop.harness import HarnessError, iter_pi_events
from research_loop.runtime.operation_contract import fields, json_value, text


SYSTEM_PROMPT = (
    "Follow the supplied role and frozen ContextPackage. Finish by calling submit_result "
    "exactly once with the complete output and evidence_refs. Its arguments are your final answer."
)


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


def _assistant_submission(message, config):
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
        not isinstance(b, dict) or b.get("type") not in {"thinking", "text", "toolCall"}
        for b in blocks
    ):
        raise HarnessError("Invalid Pi assistant content")
    calls = [b for b in blocks if b["type"] == "toolCall"]
    if len(calls) != 1 or calls[0].get("name") != "submit_result":
        raise HarnessError("Pi must call submit_result exactly once")
    call_id, arguments = calls[0].get("id"), calls[0].get("arguments")
    if not isinstance(call_id, str) or not call_id or not isinstance(arguments, dict):
        raise HarnessError("Invalid submit_result identity or arguments")
    return call_id, arguments


def _submission(path, config):
    """Accept a single completed, terminating Pi tool call from the requested model."""
    phase = "assistant"
    call_id = None
    arguments = None

    def matches(event):
        return (
            event.get("toolCallId") == call_id
            and event.get("toolName") == "submit_result"
        )

    def same_details(details):
        return json.dumps(details, sort_keys=True, allow_nan=False) == json.dumps(
            arguments, sort_keys=True, allow_nan=False
        )

    for event in iter_pi_events(path):
        if phase == "settled":
            raise HarnessError("Unexpected event after Pi submission")
        kind = event.get("type")
        if kind in {"turn_start", "agent_start"} and phase != "assistant":
            raise HarnessError("Pi started another turn after submission")
        if (
            kind == "message_start"
            and (event.get("message") or {}).get("role") == "assistant"
            and phase != "assistant"
        ):
            raise HarnessError("Pi started another assistant response after submission")
        if kind == "message_end":
            message = event.get("message") or {}
            role = message.get("role")
            if role in {"system", "user"} and phase == "assistant":
                continue
            if role == "assistant":
                if (
                    phase != "assistant"
                    or message.get("stopReason") not in {"toolUse", "stop"}
                    or message.get("errorMessage")
                ):
                    raise HarnessError("Pi assistant did not complete one submission")
                call_id, arguments = _assistant_submission(message, config)
                phase = "start"
            elif (
                role == "toolResult"
                and phase == "receipt"
                and matches(message)
                and message.get("isError") is False
                and same_details(message.get("details"))
            ):
                phase = "complete"
            else:
                raise HarnessError("Unexpected Pi message or tool receipt")
        elif kind == "tool_execution_start":
            if phase != "start" or not matches(event):
                raise HarnessError("Unexpected Pi tool execution")
            phase = "end"
        elif kind == "tool_execution_end":
            result = event.get("result") or {}
            if (
                phase != "end"
                or not matches(event)
                or event.get("isError") is not False
                or result.get("terminate") is not True
                or not same_details(result.get("details"))
            ):
                raise HarnessError("Invalid terminating submit_result receipt")
            phase = "receipt"
        elif kind == "agent_settled":
            if phase != "complete" or event.get("aborted") is not False:
                raise HarnessError("Pi did not settle a completed submission")
            phase = "settled"
    if phase != "settled":
        raise HarnessError("Incomplete Pi submit_result stream")
    return json_value(arguments)


def run(prompt, config, *, response_schema, invocation=None):
    config = validate_config(config)
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
    if root:
        Path(root).mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = Path(tempfile.mkdtemp(prefix=prefix, dir=root))
    os.chmod(directory, 0o700)
    if metadata is not None:
        (directory / "invocation.json").write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
        )
    (directory / "prompt.txt").write_text(prompt, encoding="utf-8")
    (directory / "response-schema.json").write_text(
        json.dumps(response_schema, allow_nan=False), encoding="utf-8"
    )
    deadline = time.monotonic() + config["timeout_seconds"]
    common = [
        config["executable"],
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-mcp",
        "--no-approve",
        "--no-context-files",
        "--system-prompt",
        SYSTEM_PROMPT,
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
    execute(
        [
            *common,
            "--no-builtin-tools",
            "--tools",
            "submit_result",
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
    return _submission(directory / "response.stdout.jsonl", config)
