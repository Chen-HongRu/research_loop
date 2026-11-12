"""Configuration preparation is local, explicit-file-only and revision scoped."""

import json
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from research_loop.configuration import (
    prepare_configuration,
    read_configuration,
    resolve_configuration,
)


@pytest.fixture
def author(tmp_path):
    source = tmp_path / "author"
    source.mkdir()
    (source / "workflow.py").write_text("VALUE = 'original workflow'\n")
    (source / "group.py").write_text("VALUE = 'original group'\n")
    (source / "agent.md").write_text("Original instructions")
    (source / "schema.json").write_text('{"type": "object"}')
    declarations = source / "execution"
    declarations.mkdir()
    (declarations / "environment.json").write_text(
        json.dumps({"python": sys.executable})
    )
    (declarations / "resource.json").write_text(
        json.dumps(
            {
                "host": "example",
                "partition": "test",
                "remote_root": "/work",
                "gpu_seconds_total": 60,
                "job_seconds": 60,
                "ledger_path": "ledger.json",
            }
        )
    )
    value = {
        "version": 1,
        "workflow": {"program": "workflow.py", "settings": {"limit": 2}},
        "router": {"profile": "main"},
        "profiles": {"main": {"model": "gpt-6.1-sol", "effort": "medium"}},
        "agents": {
            "writer": {
                "instructions": "agent.md",
                "profile": "main",
                "tools": "none",
                "output_schema": "schema.json",
            }
        },
        "groups": {
            "work": {
                "version": "1",
                "description": "Bounded work",
                "program": "group.py",
                "agents": {"write": "writer"},
            }
        },
        "execution": {
            "environment": "execution/environment.json",
            "resource": "execution/resource.json",
        },
    }
    path = source / "run.yaml"
    path.write_text(yaml.safe_dump(value))
    return path, value


def write(author):
    path, value = author
    path.write_text(yaml.safe_dump(value))
    return path


def test_resolve_paths_and_bindings_without_allocating(author):
    path, _ = author
    result = resolve_configuration(path)
    assert result["workflow"] == {
        "program": str(path.parent / "workflow.py"),
        "settings": {"limit": 2},
    }
    assert result["groups"][0]["agents"] == {"write": "writer"}
    assert result["agents"]["writer"]["instructions"] == "Original instructions"
    assert result["agents"]["writer"]["output_schema"] == {"type": "object"}
    assert result["resource"]["ledger_path"] == str(
        path.parent / "execution/ledger.json"
    )
    assert result["environment"]["python"] == str(Path(sys.executable).absolute())
    assert not (path.parent / "execution/ledger.json").exists()


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c.update(provider="openai"),
        lambda c: c.update(version=True),
        lambda c: c["profiles"]["main"].update(provider="openai"),
        lambda c: c["profiles"]["main"].update(model="claude-anything"),
        lambda c: c["profiles"]["main"].update(effort="automatic"),
        lambda c: c["workflow"].update(settings={"value": float("nan")}),
        lambda c: c["groups"]["work"].update(version=1),
        lambda c: c["agents"]["writer"].update(tools="shell"),
    ],
)
def test_rejects_unknown_fields_and_wrong_types(author, change):
    change(author[1])
    with pytest.raises((ValidationError, ValueError)):
        resolve_configuration(write(author))


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c["router"].update(profile="missing"),
        lambda c: c["agents"]["writer"].update(profile="missing"),
        lambda c: c["groups"]["work"]["agents"].update(write="missing"),
    ],
)
def test_rejects_bad_references(author, change):
    change(author[1])
    with pytest.raises(ValueError, match="Unknown"):
        resolve_configuration(write(author))


def test_prepared_revisions_survive_source_edits_and_are_independent(author, tmp_path):
    source, _ = author
    first = tmp_path / "revisions/one"
    one = prepare_configuration(source, first, revision="one")
    (source.parent / "agent.md").write_text("New instructions")
    (source.parent / "group.py").write_text("VALUE = 'new group'\n")
    (source.parent / "workflow.py").write_text("VALUE = 'new workflow'\n")
    two = prepare_configuration(source, tmp_path / "revisions/two", revision="two")
    source.unlink()
    assert read_configuration(first) == one
    assert one["agents"]["writer"]["instructions"] == "Original instructions"
    assert two["agents"]["writer"]["instructions"] == "New instructions"
    assert Path(one["groups"][0]["program"]).read_text() == "VALUE = 'original group'\n"
    assert Path(two["groups"][0]["program"]).read_text() == "VALUE = 'new group'\n"
    namespace = {}
    exec(Path(one["workflow"]["program"]).read_text(), namespace)
    assert namespace["VALUE"] == "original workflow"
    with pytest.raises(FileExistsError):
        prepare_configuration(
            tmp_path / "revisions/two/files/declaration.yaml", first, revision="three"
        )


def test_incomplete_revision_rejected(author, tmp_path):
    destination = tmp_path / "revision"
    prepare_configuration(author[0], destination, revision="one")
    (destination / "complete.json").unlink()
    with pytest.raises(FileNotFoundError):
        read_configuration(destination)


def test_duplicate_yaml_fields_rejected(author):
    path, _ = author
    path.write_text(path.read_text() + "version: 1\n")
    with pytest.raises(ValueError, match="Duplicate YAML"):
        resolve_configuration(path)
