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

from research_loop.harness import HarnessError, _pi_final_message
from research_loop.runtime.operation_contract import fields, json_value, text


SYSTEM_PROMPT = (
    "You are a tool-free research assistant. Follow the supplied native role and "
    "frozen ContextPackage. Return only the requested JSON object."
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


def run(prompt, config, *, invocation=None):
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
    deadline = time.monotonic() + config["timeout_seconds"]
    common = [
        config["executable"],
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-mcp",
        "--no-approve",
        "--no-tools",
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

    execute([*common, "--offline", "--list-models"], "preflight")
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
    message = _pi_final_message(directory / "response.stdout.jsonl")
    if message.get("stopReason") != "stop" or message.get("errorMessage"):
        raise HarnessError("Pi assistant did not stop successfully")
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
    content = message.get("content")
    if not isinstance(content, list):
        raise HarnessError("Pi response content must be a list")
    text_blocks = []
    for block in content:
        if not isinstance(block, dict):
            raise HarnessError("Invalid Pi response content")
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text_blocks.append(block["text"])
        elif (
            block.get("type") == "thinking"
            and isinstance(block.get("thinking"), str)
            and ("redacted" not in block or type(block["redacted"]) is bool)
        ):
            # Installed Pi ThinkingContent represents redaction with redacted=True,
            # not a separate active content type. Never expose thinking/signatures.
            continue
        else:
            raise HarnessError(
                "Tool-free Pi response contains unsupported active content"
            )
    value = json.loads("".join(text_blocks))
    return json_value(value)
