"""Small, explicit adapters for the user's already-authenticated agent CLIs."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


class HarnessError(RuntimeError):
    pass


@dataclass(frozen=True)
class HarnessConfig:
    backend: str = "codex"
    model: str = "gpt-6.1-sol"
    reasoning: str = "medium"
    executable: str | None = None
    timeout_seconds: int = 600

    def __post_init__(self) -> None:
        if self.backend not in {"codex", "pi"}:
            raise ValueError(f"Unsupported harness backend: {self.backend!r}")
        if self.model not in {"gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"}:
            raise ValueError(
                f"Unsupported model: {self.model!r}; no fallback is allowed"
            )
        if self.reasoning not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError(f"Unsupported reasoning effort: {self.reasoning!r}")
        if type(self.timeout_seconds) is not int or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive integer")
        if self.executable is not None and (
            not isinstance(self.executable, str) or not self.executable.strip()
        ):
            raise ValueError("executable must be a nonempty string or None")


class AgentHarness(Protocol):
    def run(
        self,
        *,
        prompt: str,
        schema: dict,
        workspace: Path,
        run_dir: Path,
        writable: bool = False,
        timeout_seconds: int | None = None,
    ) -> dict: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def publish_completed_response(run_dir: Path, result: dict) -> None:
    """Atomically commit a successfully parsed and accepted response."""
    if not isinstance(result, dict):
        raise ValueError("A completed harness response must be an object")
    run_dir = Path(run_dir)
    process_path = run_dir / "process.json"
    process = json.loads(process_path.read_text()) if process_path.exists() else {}
    _write_json(run_dir / "response.json", result)
    _write_json(
        run_dir / "completed.json",
        {
            "version": 1,
            "run_id": process.get("run_id"),
            "completed_at": _now(),
            "response": result,
        },
    )


def completed_response(run_dir: Path) -> dict | None:
    """Read explicit completion evidence, never a CLI's partial response file.

    A receipt survives interruption after parsing succeeds but before final process
    bookkeeping. Pre-receipt runs remain readable only with status=succeeded.
    """
    run_dir = Path(run_dir)
    try:
        process_path = run_dir / "process.json"
        process = json.loads(process_path.read_text()) if process_path.exists() else {}
        if not isinstance(process, dict):
            return None
        receipt_path = run_dir / "completed.json"
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            if (
                not isinstance(receipt, dict)
                or receipt.get("version") != 1
                or receipt.get("run_id") != process.get("run_id")
                or not isinstance(receipt.get("response"), dict)
            ):
                return None
            return receipt["response"]
        if process.get("run_id") or process.get("status") != "succeeded":
            return None
        response = json.loads((run_dir / "response.json").read_text())
        return response if isinstance(response, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def _terminate_group(process: subprocess.Popen) -> None:
    """Stop local children; this does not assert that a remote request was cancelled."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # A child may ignore SIGTERM even after the group leader has exited.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _execute(
    argv: list[str],
    *,
    prompt: str | None,
    workspace: Path,
    run_dir: Path,
    config: HarnessConfig,
    record: dict,
    prefix: str = "",
) -> None:
    record.update(
        argv=argv,
        phase="preflight" if prefix else "request",
        pid=None,
        returncode=None,
    )
    with (
        (run_dir / f"{prefix}stdout.jsonl").open("w") as stdout,
        (run_dir / f"{prefix}stderr.txt").open("w") as stderr,
    ):
        process = subprocess.Popen(
            argv,
            cwd=workspace,
            stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )
        record.update(pid=process.pid, returncode=None)
        try:
            _write_json(run_dir / "process.json", record)
            process.communicate(input=prompt, timeout=config.timeout_seconds)
            # The invocation owns this session, including any children surviving
            # their leader. No agent CLI background process may outlive the run.
            _terminate_group(process)
        except subprocess.TimeoutExpired as exc:
            _terminate_group(process)
            record.update(returncode=process.returncode, status="timed_out")
            raise HarnessError(
                f"{config.backend} local process timed out; remote request state is unknown"
            ) from exc
        except BaseException:
            _terminate_group(process)
            record.update(returncode=process.returncode, status="interrupted")
            raise
        record["returncode"] = process.returncode
    if process.returncode != 0:
        raise HarnessError(
            f"{config.backend} {record['phase']} exited {process.returncode}; "
            f"see {run_dir / f'{prefix}stderr.txt'}"
        )


def _object(text: str) -> dict:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise HarnessError("Agent response is not valid JSON") from exc
    if not isinstance(value, dict):
        raise HarnessError("Agent response must be a JSON object")
    return value


class _Harness:
    backend: str

    def __init__(self, config: HarnessConfig):
        if config.backend != self.backend:
            raise ValueError(f"{type(self).__name__} requires backend={self.backend!r}")
        self.config = config

    def run(
        self,
        *,
        prompt: str,
        schema: dict,
        workspace: Path,
        run_dir: Path,
        writable: bool = False,
        timeout_seconds: int | None = None,
    ) -> dict:
        original_config = self.config
        if timeout_seconds is not None:
            self.config = replace(self.config, timeout_seconds=timeout_seconds)
        workspace = Path(workspace).resolve()
        run_dir = Path(run_dir).resolve()
        record = {
            "run_id": uuid.uuid4().hex,
            "backend": self.backend,
            # Legacy fields retain the requested settings, not verified service identity.
            "model": self.config.model,
            "reasoning": self.config.reasoning,
            "requested_model": self.config.model,
            "requested_reasoning": self.config.reasoning,
            "model_verification": "unverified",
            "reasoning_verification": "unverified",
            "response_model_verification": "unverified",
            "timeout_seconds": self.config.timeout_seconds,
            "started": _now(),
            "ended": None,
            "status": "running",
            "pid": None,
            "returncode": None,
        }
        (run_dir / "prompt.md").write_text(prompt)
        _write_json(run_dir / "schema.json", schema)
        (run_dir / "stdout.jsonl").touch()
        (run_dir / "stderr.txt").touch()
        _write_json(run_dir / "process.json", record)
        try:
            result = self._run(prompt, schema, workspace, run_dir, writable, record)
            publish_completed_response(run_dir, result)
            record["status"] = "succeeded"
            return result
        except BaseException as exc:
            if record["status"] == "running":
                record["status"] = (
                    "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
                )
            record["error"] = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, OSError):
                raise HarnessError(f"Cannot run {self.backend}: {exc}") from exc
            raise
        finally:
            record["ended"] = _now()
            _write_json(run_dir / "process.json", record)
            self.config = original_config


class CodexHarness(_Harness):
    backend = "codex"

    def __init__(self, config: HarnessConfig, *, web_search: bool = False):
        super().__init__(config)
        self.web_search = web_search

    def _run(self, prompt, schema, workspace, run_dir, writable, record) -> dict:
        config = self.config
        argv = [
            config.executable or "codex",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--ephemeral",
            "--model",
            config.model,
            "-c",
            f'model_reasoning_effort="{config.reasoning}"',
            "-c",
            "sandbox_workspace_write.network_access=false",
            "-c",
            'approval_policy="never"',
            "--sandbox",
            "workspace-write" if writable else "read-only",
            "-C",
            str(workspace),
            "--output-schema",
            str(run_dir / "schema.json"),
            "--output-last-message",
            str(run_dir / "response.json"),
            "-",
        ]
        if self.web_search:
            if writable:
                raise HarnessError("Research search harness must be read-only")
            argv[-1:-1] = ["-c", 'web_search="live"']
        _execute(
            argv,
            prompt=prompt,
            workspace=workspace,
            run_dir=run_dir,
            config=config,
            record=record,
        )
        try:
            return _object((run_dir / "response.json").read_text())
        except FileNotFoundError as exc:
            raise HarnessError("Codex exited without a final response") from exc


class PiHarness(_Harness):
    backend = "pi"

    def _run(self, prompt, schema, workspace, run_dir, writable, record) -> dict:
        config = self.config
        common = [
            config.executable or "pi",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-mcp",
            "--no-approve",
        ]
        _execute(
            [*common, "--offline", "--list-models"],
            prompt=None,
            workspace=workspace,
            run_dir=run_dir,
            config=config,
            record=record,
            prefix="preflight.",
        )
        rows = (run_dir / "preflight.stdout.jsonl").read_text().splitlines()
        if not any(row.split()[:2] == ["openai-codex", config.model] for row in rows):
            raise HarnessError(
                f"Pi local model catalog lacks exact openai-codex/{config.model}; "
                "no request was started and no fallback is allowed"
            )
        tools = "read,bash,edit,write,grep,find,ls" if writable else "read,grep,find,ls"
        full_prompt = (
            prompt
            + "\n\nReturn only one JSON object satisfying this JSON Schema:\n"
            + json.dumps(schema, ensure_ascii=False)
        )
        (run_dir / "prompt.md").write_text(full_prompt)
        _execute(
            [
                *common,
                "--print",
                "--mode",
                "json",
                "--provider",
                "openai-codex",
                "--model",
                config.model,
                "--thinking",
                config.reasoning,
                "--no-session",
                "--tools",
                tools,
                "--",
                full_prompt,
            ],
            prompt=None,
            workspace=workspace,
            run_dir=run_dir,
            config=config,
            record=record,
        )
        try:
            events = [
                json.loads(line)
                for line in (run_dir / "stdout.jsonl").read_text().splitlines()
                if line.strip()
            ]
        except json.JSONDecodeError as exc:
            raise HarnessError("Pi emitted invalid JSONL") from exc
        if any(not isinstance(event, dict) for event in events):
            raise HarnessError("Pi JSONL events must be objects")
        settled = [event for event in events if event.get("type") == "agent_settled"]
        if not settled or settled[-1].get("aborted") is not False:
            raise HarnessError("Pi did not report agent_settled with aborted=false")
        messages = [
            event["message"]
            for event in events
            if event.get("type") == "message_end"
            and isinstance(event.get("message"), dict)
            and event["message"].get("role") == "assistant"
        ]
        if not messages:
            raise HarnessError("Pi emitted no final assistant message")
        message = messages[-1]
        if message.get("stopReason") != "stop" or message.get("errorMessage"):
            raise HarnessError(
                f"Pi assistant failed: {message.get('errorMessage') or message.get('stopReason')}"
            )
        if (
            message.get("provider") != "openai-codex"
            or message.get("model") != config.model
        ):
            raise HarnessError(
                "Pi response provider/model differs from the exact request"
            )
        record["model_verification"] = {
            "status": "matched",
            "source": "Pi assistant message.provider and message.model",
            "provider": message["provider"],
            "model": message["model"],
        }
        for field in ("thinkingLevel", "providerThinkingLevel"):
            if field in message and message[field] != config.reasoning:
                raise HarnessError(
                    f"Pi {field} differs from requested reasoning effort"
                )
        record["reasoning_verification"] = {
            field: message.get(field, "unverified")
            for field in ("thinkingLevel", "providerThinkingLevel")
        }
        if "responseModel" in message:
            # Preserve the report without treating it as verified service identity.
            record["response_model"] = message["responseModel"]
        content = message.get("content")
        if not isinstance(content, list):
            raise HarnessError("Pi assistant content must be a list")
        text = "".join(
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
        return _object(text)


def make_harness(config: HarnessConfig) -> AgentHarness:
    return CodexHarness(config) if config.backend == "codex" else PiHarness(config)
