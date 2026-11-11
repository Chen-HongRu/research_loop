"""Priority experiment admission and publication without a model or remote run."""

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from research_loop import human_input, loop
from test_loop import CONTINUE, prepare, scripted_native
from test_native import RESULT


def outcome(request, status="completed", **result):
    return {
        **RESULT,
        **result,
        "human_request": {
            "id": request["id"],
            "status": status,
            "evidence": ["knowledge/index.md", "work/measurement.json"],
            "reason": "Synthetic experiment was executed and analyzed.",
        },
    }


def test_missing_empty_direct_and_injected_requests(tmp_path):
    root = prepare(tmp_path)
    assert human_input.admit(root) is None
    path = root / "inputs/human.md"
    path.write_text(" \n")
    assert human_input.admit(root) is None
    human_input.submit(root, "Check the synthetic hypothesis.\n")
    admitted = human_input.admit(root)
    assert admitted["text"] == "Check the synthetic hypothesis.\n"
    assert human_input.admit(root) == admitted
    with pytest.raises(ValueError, match="pending"):
        human_input.submit(root, "Do not overwrite")
    with pytest.raises(ValueError, match="nonempty"):
        human_input.submit(root, "  ")
    path.write_text("A directly authored replacement")
    assert human_input.admit(root)["id"] != admitted["id"]


def test_symlink_and_special_file_rejected(tmp_path):
    root = prepare(tmp_path)
    directory = root / "inputs"
    directory.mkdir()
    path = directory / "human.md"
    path.symlink_to(root / "goal.md")
    with pytest.raises(ValueError, match="symlink"):
        human_input.admit(root)
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular file"):
        human_input.admit(root)


def test_real_cpu_request_path_and_published_negative_result(tmp_path, monkeypatch):
    root = prepare(tmp_path)
    human_input.submit(root, "Measure 1 - 2 and analyze the negative result.")
    request = human_input.admit(root)

    def on_turn(number, packet):
        assert packet["human_request"] == request
        assert packet["human_request_file"] == str(root / "inputs/human.md")
        measurement = Path(packet["work_root"]) / "measurement.json"
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import json,sys;open(sys.argv[1],'w').write(json.dumps({'delta':1-2}))",
                str(measurement),
            ],
            check=True,
        )
        (Path(packet["knowledge_draft"]) / "index.md").write_text(
            "Synthetic delta = -1. Negative result answers the request."
        )

    factory, clients = scripted_native(monkeypatch, [outcome(request)], on_turn)
    state = asyncio.run(loop.run(root, native_factory=factory))
    assert state["status"] == "completed"
    assert (root / "inputs/human.md").read_text() == ""
    snapshot = root / "iterations/000001" / f"human-request-{request['id']}.json"
    assert json.loads(snapshot.read_text()) == request
    assert loop.Knowledge(root).published_state()["human_request"] == request
    assert "Negative result" in (root / "knowledge/current/index.md").read_text()
    assert json.loads((root / "work/measurement.json").read_text()) == {"delta": -1}
    schema = json.loads((root / "call.json").read_text())["output_schema"]
    assert "human_request" in schema["required"]
    asyncio.run(loop.run(root, native_factory=factory))
    assert len(clients) == 1


@pytest.mark.parametrize("status", ["pending", "blocked"])
def test_unfinished_request_retained_after_publication(tmp_path, monkeypatch, status):
    root = prepare(tmp_path)
    human_input.submit(root, "An unfinished synthetic request")
    request = human_input.admit(root)
    factory, _ = scripted_native(monkeypatch, [outcome(request, status)])
    asyncio.run(loop.run(root, native_factory=factory))
    assert human_input.admit(root) == request


@pytest.mark.parametrize("failure", ["transport", "publication", "stop", "resources"])
def test_failed_or_stopped_work_does_not_consume(tmp_path, monkeypatch, failure):
    root = prepare(tmp_path)
    human_input.submit(root, "Keep this request until publication")
    request = human_input.admit(root)
    initial = (root / "knowledge/current").resolve()

    def on_turn(number, packet):
        if failure == "stop":
            (root / "STOP").touch()

    if failure == "publication":

        def broken_publish(*args, **kwargs):
            raise RuntimeError("synthetic publication failure")

        monkeypatch.setattr(loop.Knowledge, "publish", broken_publish)
    if failure == "resources":

        class UnknownResource:
            def status(self):
                return {"reservations": [{"status": "UNKNOWN"}]}

            def tools(self):
                return []

            async def close(self):
                return self.status()

        monkeypatch.setattr(loop, "resource", lambda *args: UnknownResource())
    factory, _ = scripted_native(
        monkeypatch,
        [outcome(request)],
        on_turn,
        fail_at=1 if failure == "transport" else None,
    )
    if failure in {"transport", "publication"}:
        with pytest.raises(RuntimeError, match="synthetic"):
            asyncio.run(loop.run(root, native_factory=factory))
    else:
        asyncio.run(loop.run(root, native_factory=factory))
    assert human_input.admit(root) == request
    assert (root / "knowledge/current").resolve() == initial


def test_new_edit_during_execution_is_next_iteration_input(tmp_path, monkeypatch):
    root = prepare(tmp_path)
    human_input.submit(root, "First experiment")
    first = human_input.admit(root)
    second = None

    def on_turn(number, packet):
        nonlocal second
        if number == 1:
            with human_input.input_lock(root) as path:
                path.write_text("Second experiment")
            second = human_input.admit(root)
        else:
            assert packet["human_request"] == second

    # The second result's exact id becomes known during the first turn.
    results = [outcome(first, **CONTINUE), RESULT.copy()]

    def inject_second_result(number, packet):
        on_turn(number, packet)
        if number == 1:
            results[1] = outcome(second)

    factory, clients = scripted_native(monkeypatch, results, inject_second_result)
    state = asyncio.run(loop.run(root, native_factory=factory))
    assert [client.packet["human_request"]["text"] for client in clients] == [
        "First experiment",
        "Second experiment",
    ]
    assert state["iteration"] == 2
    assert human_input.admit(root) is None


def test_crash_after_pointer_commit_recovers_consumption_without_replay(
    tmp_path, monkeypatch
):
    root = prepare(tmp_path)
    human_input.submit(root, "Publish once")
    request = human_input.admit(root)
    publish = loop.Knowledge.publish

    def interrupted_publish(*args, **kwargs):
        publish(*args, **kwargs)
        raise RuntimeError("synthetic crash after activation")

    monkeypatch.setattr(loop.Knowledge, "publish", interrupted_publish)
    factory, clients = scripted_native(monkeypatch, [outcome(request)])
    with pytest.raises(RuntimeError, match="after activation"):
        asyncio.run(loop.run(root, native_factory=factory))
    assert human_input.admit(root) == request
    state = asyncio.run(loop.run(root, native_factory=factory))
    assert state["status"] == "completed"
    assert human_input.admit(root) is None
    assert len(clients) == 1


def test_consumption_waits_for_editor_and_preserves_its_revision(tmp_path):
    root = prepare(tmp_path)
    human_input.submit(root, "Original")
    first = human_input.admit(root)
    publication = {"human_request": first, "result": outcome(first)}
    entered = threading.Event()
    finished = threading.Event()

    def consume():
        entered.set()
        human_input.consume(root, publication)
        finished.set()

    with human_input.input_lock(root) as path:
        worker = threading.Thread(target=consume)
        worker.start()
        assert entered.wait(2)
        assert not finished.wait(0.05)
        path.write_text("User revision")
    worker.join(timeout=2)
    assert finished.is_set()
    assert human_input.admit(root)["text"] == "User revision"
    # Repeated recovery must also preserve the replacement.
    human_input.consume(root, publication)
    assert human_input.admit(root)["text"] == "User revision"


@pytest.mark.parametrize("mode", ["file", "stdin"])
def test_real_cli_injection_is_offline(tmp_path, mode):
    root = prepare(tmp_path)
    source = tmp_path / "experiment.md"
    source.write_text("Synthetic user-authorized request\n")
    arguments = ["--file", str(source)] if mode == "file" else ["--stdin"]
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "research_loop.loop",
            "experiment",
            str(root),
            *arguments,
        ],
        input=source.read_text() if mode == "stdin" else None,
        text=True,
        capture_output=True,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["request_id"] == human_input.admit(root)["id"]
    assert human_input.admit(root)["text"] == source.read_text()
    assert not (root / "native").exists()
    assert not (root / "events.jsonl").exists()


def test_editor_command_uses_lock_and_updates_input(tmp_path, monkeypatch):
    root = prepare(tmp_path)
    editor = tmp_path / "editor.py"
    editor.write_text(
        "from pathlib import Path\nimport sys\n"
        "Path(sys.argv[1]).write_text('Edited by user')\n"
    )
    monkeypatch.setenv("EDITOR", f"{sys.executable} {editor}")
    monkeypatch.delenv("VISUAL", raising=False)
    human_input.edit(root)
    assert human_input.admit(root)["text"] == "Edited by user"


def test_editor_without_saving_preserves_request_identity(tmp_path, monkeypatch):
    root = prepare(tmp_path)
    human_input.submit(root, "Do not repeat after opening the editor")
    request = human_input.admit(root)
    monkeypatch.setenv("VISUAL", "true")
    human_input.edit(root)
    assert human_input.admit(root) == request
    human_input.consume(root, {"human_request": request, "result": outcome(request)})
    assert human_input.admit(root) is None


@pytest.mark.parametrize("mode", ["native", "host"])
def test_request_permissions_are_read_only_in_both_modes(tmp_path, monkeypatch, mode):
    from research_loop.codex import configuration
    from test_native import provider

    root = prepare(tmp_path)
    human_input.submit(root, "Read-only request")
    _, effective = configuration(
        root,
        tmp_path,
        [root / "work", root / "knowledge/draft"],
        "synthetic-model",
        "medium",
        provider=provider(tmp_path, monkeypatch),
        delegation=mode,
    )
    assert effective["filesystem"][":root"] == "read"
    assert str(root / "inputs") not in effective["filesystem"]
    assert str(root / "iterations") not in effective["filesystem"]
    assert effective["filesystem"][str(root / "work")] == "write"


@pytest.mark.skipif(
    os.environ.get("RESEARCH_NATIVE_TEST") != "1",
    reason="Opt-in native sandbox command without a model call",
)
@pytest.mark.parametrize("mode", ["native", "host"])
def test_native_sandbox_denies_request_write_without_model(tmp_path, monkeypatch, mode):
    from openai_codex.client import CodexClient
    from openai_codex.generated.v2_all import CommandExecResponse
    from research_loop.codex import configuration

    root = prepare(tmp_path)
    human_input.submit(root, "Protected synthetic request")
    request = human_input.admit(root)
    monkeypatch.setenv("SYNTHETIC_PROVIDER_KEY", "SYNTHETIC_NOT_A_CREDENTIAL")
    settings = {
        "name": "synthetic",
        "base_url": "https://example.invalid/v1",
        "api_key_env": "SYNTHETIC_PROVIDER_KEY",
        "model_catalog": None,
        "context_window": 65536,
    }
    config, _ = configuration(
        root,
        tmp_path / "project",
        [root / "work", root / "knowledge/draft"],
        "gpt-6.1-sol",
        "medium",
        provider=settings,
        delegation=mode,
    )
    script = (
        "from pathlib import Path\nimport sys\n"
        "request=Path(sys.argv[1])\nassert request.read_text()\n"
        "try:\n request.write_text('forbidden')\n"
        "except PermissionError:\n print('WRITE_DENIED')\n"
        "else:\n raise RuntimeError('Request write was allowed')\n"
        "Path(sys.argv[2]).write_text('allowed')\nprint('WORK_WRITE_ALLOWED')\n"
    )
    with CodexClient(config) as client:
        client.initialize()
        response = client.request(
            "command/exec",
            {
                "command": [
                    sys.executable,
                    "-c",
                    script,
                    str(root / "inputs/human.md"),
                    str(root / "work/probe.txt"),
                ],
                "cwd": str(root / "work"),
                "timeoutMs": 10000,
            },
            response_model=CommandExecResponse,
        )
    assert response.exit_code == 0, response.stderr
    assert "WRITE_DENIED" in response.stdout
    assert "WORK_WRITE_ALLOWED" in response.stdout
    assert human_input.admit(root) == request


def test_invalid_acknowledgement_does_not_publish_or_consume(tmp_path, monkeypatch):
    root = prepare(tmp_path)
    human_input.submit(root, "A request")
    request = human_input.admit(root)
    result = outcome(request)
    result["human_request"]["id"] = "wrong-request"
    initial = (root / "knowledge/current").resolve()
    factory, _ = scripted_native(monkeypatch, [result])
    with pytest.raises(ValueError, match="matching human request"):
        asyncio.run(loop.run(root, native_factory=factory))
    assert human_input.admit(root) == request
    assert (root / "knowledge/current").resolve() == initial


def test_completed_request_requires_evidence_and_blocked_requires_stop(tmp_path):
    root = prepare(tmp_path)
    human_input.submit(root, "A request")
    request = human_input.admit(root)
    result = outcome(request)
    result["human_request"]["evidence"] = []
    with pytest.raises(ValueError, match="evidence references"):
        human_input.validate_result(request, result)
    with pytest.raises(ValueError, match="must stop"):
        human_input.validate_result(request, outcome(request, "blocked", **CONTINUE))
