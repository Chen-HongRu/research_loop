"""Real local preparation and interface routing; no model or remote execution."""

import json

import pytest
import yaml

from research_loop import cli
from research_loop.records import ExecutionTrace
from research_loop.state import write_json


@pytest.fixture
def author(tmp_path):
    (tmp_path / "project").mkdir()
    (tmp_path / "goal.md").write_text("Synthetic local test")
    (tmp_path / "workflow.py").write_text("""
class Workflow:
    router_policy = "Synthetic offline routing"
    max_active_groups = 1
    def context(self, *args): return {}
    def route(self, *args): return {"allowed_actions": ["start", "complete"]}
    def advance(self, *args): return {}
    def complete(self, *args): return {}
def create(settings): return Workflow()
""")
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "workflow": {"program": "workflow.py"},
                "router": {"profile": "router"},
                "profiles": {"router": {"model": "gpt-6.1-sol", "effort": "medium"}},
                "agents": {},
                "groups": {},
            }
        )
    )
    return tmp_path, config


def prepare(author, capsys):
    directory, config = author
    root = directory / "run"
    assert (
        cli.main(
            [
                "prepare",
                str(root),
                "--config",
                str(config),
                "--project",
                str(directory / "project"),
                "--goal",
                str(directory / "goal.md"),
            ]
        )
        == 0
    )
    return root, json.loads(capsys.readouterr().out)


def test_prepare_inspect_and_status_use_same_frozen_configuration(author, capsys):
    root, first = prepare(author, capsys)
    assert first["config"]["revision"] == "r000001"
    assert first["router"]["policy"] == "Synthetic offline routing"
    (author[0] / "workflow.py").write_text(
        "raise RuntimeError('modified source must not run')"
    )
    author[1].unlink()
    assert cli.main(["inspect", str(root)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["config"] == first["config"]
    assert inspected["router"] == first["router"]
    assert cli.main(["status", str(root)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "prepared"
    assert status["config_revision"] == "r000001"
    assert not (root / "native").exists()
    assert not (root / "controller.log").exists()


def test_targeted_text_intervention_is_durable_and_queryable(author, capsys):
    root, _ = prepare(author, capsys)
    text = author[0] / "feedback.txt"
    text.write_text("Evaluate this synthetic branch")
    assert (
        cli.main(
            [
                "intervene",
                str(root),
                "--kind",
                "feedback",
                "--file",
                str(text),
                "--target",
                "group-one",
                "--id",
                "feedback-one",
                "--expected-revision",
                "r000001",
            ]
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    assert (
        cli.main(
            ["query", str(root), "--kind", "interventions", "--id", "feedback-one"]
        )
        == 0
    )
    queried = json.loads(capsys.readouterr().out)
    assert queried == receipt
    assert queried["request"]["target"] == "group-one"
    assert queried["request"]["payload"] == {"text": "Evaluate this synthetic branch"}
    assert queried["request"]["expected_revision"] == "r000001"


def test_query_reads_real_step_by_group_scope_and_id(author, capsys):
    root, _ = prepare(author, capsys)
    group = root / "group-runs/group-one"
    group.mkdir(parents=True)
    write_json(group / "run.json", {"run_id": "group-one", "status": "running"})
    trace = ExecutionTrace(group, run_id="group-one", round=1, revision="r000001")
    identity = trace.begin("step", "branch", parent_step_id="explicit-parent")
    assert (
        cli.main(
            [
                "query",
                str(root),
                "--kind",
                "steps",
                "--run-id",
                "group-one",
                "--id",
                identity,
            ]
        )
        == 0
    )
    record = json.loads(capsys.readouterr().out)
    assert record["id"] == identity
    assert record["parent_step_id"] == "explicit-parent"
    assert cli.main(["query", str(root), "--kind", "steps"]) == 1
    error = json.loads(capsys.readouterr().err)
    assert "--run-id" in error["message"]


def test_start_and_explicit_resume_use_application_supervision(
    author, capsys, monkeypatch
):
    root, _ = prepare(author, capsys)
    calls = []

    def start(path):
        calls.append(("start", path))
        return {"status": "starting", "pid": 123}

    def resume(path):
        calls.append(("resume", path))
        return {"status": "starting", "pid": 124}

    monkeypatch.setattr(cli.application, "start", start)
    monkeypatch.setattr(cli.application, "resume", resume)
    assert cli.main(["start", str(root)]) == 0
    capsys.readouterr()
    assert cli.main(["resume", str(root)]) == 0
    capsys.readouterr()
    assert calls == [("start", root), ("resume", root)]


@pytest.mark.parametrize("command", ["init", "run", "experiment", "edit-experiment"])
def test_removed_commands_are_not_aliases(command):
    with pytest.raises(SystemExit) as failure:
        cli.main([command, "/unused"])
    assert failure.value.code == 2


def test_invalid_payload_and_missing_run_return_json_error(author, capsys):
    root, _ = prepare(author, capsys)
    payload = author[0] / "payload.json"
    payload.write_text("[]")
    assert (
        cli.main(
            ["intervene", str(root), "--kind", "feedback", "--payload", str(payload)]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().err)["error"] == "ValueError"
    assert cli.main(["status", str(author[0] / "missing")]) == 1
    assert json.loads(capsys.readouterr().err)["error"] == "FileNotFoundError"


def test_controls_and_revision_requests_keep_explicit_targets(author, capsys):
    root, _ = prepare(author, capsys)
    for command in ("pause", "continue", "stop"):
        assert (
            cli.main(
                [command, str(root), "--target", "group-one", "--id", command + "-one"]
            )
            == 0
        )
        request = json.loads(capsys.readouterr().out)["request"]
        assert request["kind"] == command
        assert request["target"] == "group-one"
    assert (
        cli.main(
            [
                "revise",
                str(root),
                "--config",
                str(author[1]),
                "--affected",
                "group-one",
                "--affected",
                "group-two",
                "--expected-revision",
                "r000001",
                "--id",
                "revise-one",
            ]
        )
        == 0
    )
    request = json.loads(capsys.readouterr().out)["request"]
    assert request["payload"]["affected"] == ["group-one", "group-two"]
    assert request["expected_revision"] == "r000001"
    assert request["payload"]["revision"] != "r000001"
    assert json.loads((root / "active.json").read_text()) == {"revision": "r000001"}
