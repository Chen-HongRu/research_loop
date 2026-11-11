"""Fixed local interpreters and shared declared resource context; no model/GPU."""

import asyncio
import json
import subprocess
import sys
import tomllib
import venv
from pathlib import Path

import pytest

from research_loop import environment, loop
from research_loop.codex import Native, configuration
from research_loop.instructions import instructions
from research_loop.state import Journal
from test_native import Client, RESULT


@pytest.fixture
def selected_python(tmp_path):
    root = tmp_path / "selected"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(root)
    python = root / "bin/python"
    sites = list((root / "lib").glob("python*/site-packages"))
    assert len(sites) == 1
    package = sites[0] / "synthetic_marker"
    package.mkdir()
    (package / "__init__.py").write_text(
        "raise AssertionError('Package must not be imported by observation')\n"
    )
    metadata = sites[0] / "synthetic_marker-7.1.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: synthetic-marker\nVersion: 7.1\n"
    )
    return python


def settings(tmp_path, python, **extra):
    path = tmp_path / "environment.json"
    path.write_text(json.dumps({"python": str(python), **extra}))
    return path


def test_relative_venv_symlink_is_preserved_and_distribution_not_imported(
    tmp_path, selected_python
):
    path = settings(
        tmp_path,
        "selected/bin/python",
        resource={
            "python": "/opt/resource/bin/python",
            "packages": {"synthetic-resource": "2.0+gpu"},
        },
    )
    config = environment.read_config(path)
    assert config["python"] == str(selected_python)
    assert selected_python.is_symlink()
    assert config["python"] != str(selected_python.resolve())
    snapshot = environment.observe(
        config,
        resource_config={"partition": "debug"},
        resource_status={"budget": {"remaining_seconds": 600}},
        tools=[{"name": "gpu_exec"}],
    )
    local = snapshot["local"]
    assert local["executable"] == str(selected_python)
    assert local["prefix"] == str(selected_python.parent.parent)
    assert local["base_prefix"] != local["prefix"]
    assert local["packages"]["synthetic-marker"] == "7.1"
    assert snapshot["resource"] == {
        "status": "declared-unverified",
        "python": "/opt/resource/bin/python",
        "packages": {"synthetic-resource": "2.0+gpu"},
        "configured": True,
        "partition": "debug",
        "budget": {"remaining_seconds": 600},
        "tools": ["gpu_exec"],
    }


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"python": 1},
        {"python": ""},
        {"python": "/absent/interpreter"},
        {"python": sys.executable, "env": {"SECRET": "not-permitted"}},
        {"python": sys.executable, "resource": None},
        {"python": sys.executable, "resource": {"python": "relative/python"}},
        {
            "python": sys.executable,
            "resource": {"python": "/usr/bin/python", "command": "install"},
        },
        {
            "python": sys.executable,
            "resource": {"python": "/usr/bin/python", "packages": []},
        },
        {
            "python": sys.executable,
            "resource": {"python": "/usr/bin/python", "packages": {"name": 2}},
        },
    ],
)
def test_invalid_environment_configuration_is_rejected(tmp_path, value):
    path = tmp_path / "environment.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        environment.read_config(path)


def test_init_copies_control_config_and_inspect_observes_once(
    tmp_path, selected_python, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    goal = tmp_path / "goal.md"
    goal.write_text("Synthetic fixed interpreter inspection; no remote/model calls.")
    source = settings(tmp_path, selected_python)
    root = tmp_path / "study"
    loop.initialize(root, project, goal, environment_file=source, coordinator="lead")
    source.write_text(json.dumps({"python": "/changed/after/initialization"}))
    observed = []
    observe = environment.observe

    def capture(*args, **kwargs):
        observed.append(args[0])
        return observe(*args, **kwargs)

    monkeypatch.setattr(environment, "observe", capture)
    call = loop.inspect(root)
    assert observed == [{"python": str(selected_python)}]
    snapshot = call["input"]["execution_environment"]
    assert snapshot["local"]["packages"]["synthetic-marker"] == "7.1"
    assert snapshot["resource"]["configured"] is False
    assert snapshot["resource"]["status"] == "undeclared-unverified"
    assert snapshot["resource"]["tools"] == []
    assert snapshot["local"]["prefix"] in call["permissions"]["read"]
    assert environment.render(snapshot) in call["instructions"]
    assert not (root / "events.jsonl").exists()


def test_legacy_snapshot_uses_controller_python_without_resource_dispatch():
    snapshot = environment.observe()
    assert snapshot["local"]["executable"] == sys.executable
    assert snapshot["local"]["prefix"] == sys.prefix
    assert snapshot["resource"]["configured"] is False


def test_configuration_executes_selected_python_and_protects_credentials(
    tmp_path, selected_python, monkeypatch
):
    owner = tmp_path / "owner"
    (owner / ".codex").mkdir(parents=True)
    (owner / ".codex/auth.json").write_text("synthetic-auth-fixture")
    monkeypatch.setattr(Path, "home", lambda: owner)
    snapshot = environment.observe(
        environment.read_config(settings(tmp_path, selected_python))
    )
    root = tmp_path / "study"
    root.mkdir()
    config, effective = configuration(
        root,
        tmp_path / "project",
        [root / "work"],
        "gpt-6.1-sol",
        "medium",
        execution_environment=snapshot,
    )
    args = config.launch_args_override
    value = next(
        x.split("=", 1)[1]
        for x in args
        if x.startswith("shell_environment_policy.set=")
    )
    shell = tomllib.loads("value = " + value)["value"]
    assert shell["PATH"].split(":")[0] == str(selected_python.parent)
    assert shell["VIRTUAL_ENV"] == str(selected_python.parent.parent)
    assert shell["PYTHONNOUSERSITE"] == "1"
    assert "BOUNDLESS_API_KEY" not in shell and "OPENAI_API_KEY" not in shell
    result = subprocess.run(
        ["python", "-c", "import sys;print(sys.prefix)"],
        env=shell,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == snapshot["local"]["prefix"]
    assert effective["filesystem"][snapshot["local"]["prefix"]] == "read"
    assert effective["filesystem"][str(owner / ".codex")] == "deny"
    assert effective["filesystem"][str(owner / ".ssh")] == "deny"
    global_instructions = next(
        x.split("=", 1)[1] for x in args if x.startswith("developer_instructions=")
    )
    assert json.loads(global_instructions) == environment.render(snapshot)


def test_nonvenv_configuration_does_not_set_virtual_env(tmp_path, monkeypatch):
    owner = tmp_path / "owner"
    (owner / ".codex").mkdir(parents=True)
    (owner / ".codex/auth.json").write_text("synthetic-auth-fixture")
    monkeypatch.setattr(Path, "home", lambda: owner)
    root = tmp_path / "study"
    root.mkdir()
    snapshot = {
        "local": {
            "executable": sys.executable,
            "prefix": sys.base_prefix,
            "base_prefix": sys.base_prefix,
        },
        "resource": {"status": "undeclared-unverified"},
    }
    config, _ = configuration(
        root,
        tmp_path,
        [root / "work"],
        "gpt-6.1-sol",
        "medium",
        execution_environment=snapshot,
    )
    value = next(
        x.split("=", 1)[1]
        for x in config.launch_args_override
        if x.startswith("shell_environment_policy.set=")
    )
    assert "VIRTUAL_ENV" not in tomllib.loads("value = " + value)["value"]


@pytest.mark.parametrize("mode", ["native", "host"])
def test_native_run_infers_same_environment_and_host_child_receives_it(tmp_path, mode):
    snapshot = environment.observe()

    async def check():
        journal = Journal(tmp_path / "events")
        native = Native(None, journal, client_factory=Client)
        await native.open()
        try:
            result = await native.run(
                instructions(mode, snapshot),
                {"execution_environment": snapshot},
                {},
                model="gpt-6.1-sol",
                effort="medium",
                delegation=mode,
            )
            assert result == RESULT
            definition = native.client.definition
            assert environment.render(snapshot) in definition["developerInstructions"]
            if mode == "host":
                assert native.delegator.environment is snapshot
                # Start a real host worker with a separate synthetic client stream.
                from test_delegation import Client as RoleClient

                role_client = RoleClient()
                native.delegator.client = role_client
                native.delegator.start(
                    "analyst", "environment-check", "Read the supplied environment."
                )
                native.delegator.settle()
                assert (
                    environment.render(snapshot)
                    in role_client.definitions[0]["developerInstructions"]
                )
            else:
                common = definition["developerInstructions"].split(
                    "## Common contract", 1
                )[1]
                assert environment.render(snapshot) in common
        finally:
            await native.close()
            journal.close()

    asyncio.run(check())
