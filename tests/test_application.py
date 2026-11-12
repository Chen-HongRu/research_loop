"""Real detached application lifecycle with frozen, model-free local programs."""

import asyncio
import json
import os
import signal
import sys
import time

import pytest
import yaml

from research_loop import application, publication, queries
from research_loop.controller import Controller
from research_loop.state import PublicationStore, write_json


WORKFLOW = """
import json
from pathlib import Path

class Workflow:
    router_policy = "Synthetic deterministic local workflow; no model call required."
    max_active_groups = 1

    def context(self, root, config, iteration, previous_result, request):
        return {"opaque": "local smoke", "request": request}

    def route(self, packet):
        return packet

    def advance(self, view):
        rows = [row for row in view["runs"] if not row.get("interrupted")]
        if not rows:
            return {"action": "start", "group_id": "job", "task_ref": str(Path(view["root"]) / "goal.md"), "input_refs": []}
        for row in rows:
            if row["status"] == "returned":
                return {"action": "close", "run_id": row["run_id"]}
            if row["status"] == "running":
                return {"action": "wait"}
            if row["status"] == "needs-reconciliation":
                raise RuntimeError("Original operation requires reconciliation")
            if row["status"] == "failed":
                raise RuntimeError("Synthetic Group failed")
        return {"action": "finish_round", "result_ref": rows[-1]["output"]["artifacts"][0]}

    def complete(self, view, result_ref):
        result = json.loads(Path(result_ref).read_text())
        return {"action": "stop", "result": result, "artifacts": [{"source": result_ref, "destination": "answer.json"}]}

    def validate_input(self, kind, payload):
        if kind != "task":
            raise ValueError("Only task input is supported")
        return dict(payload)


def create(settings):
    return Workflow()
"""

GROUP = """
import asyncio
import json
import shlex
import sys
from synthetic_task.helper import VALUE

async def run(ctx, payload):
    mode = ctx.definition.settings["mode"]
    (ctx.work / "started.json").write_text(json.dumps({"run_id": ctx.run_id, "value": VALUE}))
    if mode == "unknown":
        ctx.state.setdefault("executions", {})["original"] = {
            "tool": "local", "origin_group_run_id": ctx.run_id, "operation_id": "original",
            "arguments": {"command": "unresolved original operation; never redispatch"},
            "status": "unknown", "receipt": {"unknown": True},
        }
        ctx.save_state()
        raise RuntimeError("Synthetic original operation is UNKNOWN")
    if mode == "cooperative" and len(list(ctx.root.parent.glob("*/run.json"))) == 1:
        while not (ctx.work / "release").exists():
            await asyncio.sleep(0.01)
    if mode == "command":
        script = "import json; from pathlib import Path; Path('answer.json').write_text(json.dumps({'answer': " + str(VALUE) + "}))"
        receipt = await ctx.services.execute_prepared(shlex.quote(sys.executable) + " -c " + shlex.quote(script), operation_id="arithmetic", backend="local")
        if receipt["exit_code"] != 0:
            raise RuntimeError("Synthetic arithmetic command failed")
    else:
        (ctx.work / "answer.json").write_text(json.dumps({"answer": VALUE}))
    return {"summary": "Local arithmetic complete", "completion": "completed", "unfinished": [], "help": "", "artifacts": ["answer.json"]}
"""


def author(tmp_path, mode="command", *, workflow_text=WORKFLOW, package_data=False):
    directory = tmp_path / "author"
    package = directory / "synthetic_task"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "workflow.py").write_text(workflow_text)
    (package / "group.py").write_text(GROUP)
    (package / "helper.py").write_text("VALUE = 42\n")
    if package_data:
        data = package / "data"
        data.mkdir()
        (data / "payload.bin").write_bytes(b"synthetic data is not controller code")
        (data / "not_a_package.py").write_text("VALUE = 'unmanaged data module'\n")
    (directory / "goal.md").write_text("Compute a synthetic local arithmetic result.")
    (directory / "environment.json").write_text(json.dumps({"python": sys.executable}))
    declaration = {
        "version": 1,
        "workflow": {"program": "synthetic_task/workflow.py"},
        "router": {"profile": "main"},
        "profiles": {"main": {"model": "gpt-6.1-sol", "effort": "medium"}},
        "agents": {},
        "groups": {
            "job": {
                "version": "1",
                "description": "Synthetic local arithmetic",
                "program": "synthetic_task/group.py",
                "settings": {"mode": mode, "execution_backend": "local"},
            }
        },
        "execution": {"environment": "environment.json"},
    }
    source = directory / "run.yaml"
    source.write_text(yaml.safe_dump(declaration))
    root = tmp_path / "run"
    inspected = application.prepare(
        source, root, project=directory, goal=directory / "goal.md"
    )
    return root, package, inspected


def wait_for(root, predicate, *, timeout=15):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = application.status(root)
        if predicate(last):
            return last
        time.sleep(0.02)
    log = root / "controller.log"
    pytest.fail(
        f"Controller did not reach expected state; status={last}; log={log.read_text() if log.exists() else '<missing>'}"
    )


def wait_exit(pid, *, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            reaped, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return None
        if reaped == pid:
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.02)
    pytest.fail(f"Owned controller process {pid} did not exit")


def cleanup(pid):
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return
    if not reaped:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        wait_exit(pid)


def test_detached_start_executes_frozen_package_and_publishes_real_cpu_result(tmp_path):
    root, package, inspected = author(tmp_path)
    assert inspected["run"]["status"] == "prepared"
    assert inspected["router"]["tools"] == []
    assert (
        inspected["router"]["policy"]
        == "Synthetic deterministic local workflow; no model call required."
    )
    snapshot = root / "implementation/src/synthetic_task/helper.py"
    assert snapshot.read_text() == "VALUE = 42\n"
    (package / "helper.py").write_text("VALUE = 9001\n")
    (package / "group.py").write_text(
        "raise RuntimeError('Changed author Group must not run')\n"
    )
    (package / "workflow.py").write_text(
        "raise RuntimeError('Changed author workflow must not run')\n"
    )
    assert (
        application.inspect(root)["router"]["policy"] == inspected["router"]["policy"]
    )
    incoming = application.intervene(
        root,
        kind="task",
        payload={"text": "Synthetic explicit task"},
        identity="task-one",
    )
    assert incoming["status"] == "received"
    rejected_control = application.intervene(
        root, kind="pause", expected_revision="stale-revision", identity="stale-control"
    )
    assert rejected_control["status"] == "received"
    started = application.start(root)
    try:
        state = wait_for(
            root,
            lambda value: (
                value["status"] in {"completed", "failed", "needs-reconciliation"}
            ),
        )
        assert state["status"] == "completed", (root / "controller.log").read_text()
        assert state["result"] == {"answer": 42}
        assert json.loads((root / "outputs/current/answer.json").read_text()) == {
            "answer": 42
        }
        runs = queries.list_runs(root)["items"]
        assert len(runs) == 1 and runs[0]["status"] == "closed"
        operations = queries.list_operations(root, runs[0]["run_id"])["items"]
        assert len(operations) == 1
        assert operations[0]["operation_id"] == "arithmetic"
        assert operations[0]["origin_group_run_id"] == runs[0]["run_id"]
        assert operations[0]["status"] == "completed"
        assert operations[0]["receipt"]["exit_code"] == 0
        assert queries.list_calls(root, runs[0]["run_id"])["items"] == []
        admitted_input = application.get_intervention(root, "task-one")
        assert admitted_input["status"] == "applied"
        # Admission acknowledges delivery. This workflow proposes no work outcome.
        assert "work_outcome" not in admitted_input
        rejected_control = application.get_intervention(root, "stale-control")
        assert rejected_control["status"] == "rejected"
        assert application.get_intervention(root, "stale-control") == rejected_control
        input_receipt = json.loads(
            (
                root
                / "group-runs"
                / runs[0]["run_id"]
                / "rounds/000001/input-receipt.json"
            ).read_text()
        )
        assert input_receipt["interventions"] == ["task-one"]
        assert not (root / "groups/router").exists()
        assert (
            json.loads((root / "completion.json").read_text())["status"] == "completed"
        )
        assert wait_exit(started["pid"]) == 0
    finally:
        cleanup(started["pid"])


def test_pause_stop_and_explicit_resume_create_a_new_group_execution(tmp_path):
    root, _, _ = author(tmp_path, "cooperative")
    paused = application.intervene(
        root, kind="pause", identity="pause-before-admission"
    )
    assert paused["status"] == "received"
    assert "work_outcome" not in paused
    started = application.start(root)
    resumed = None
    try:
        wait_for(root, lambda value: value["status"] == "paused")
        assert queries.list_runs(root)["items"] == []
        assert (
            application.get_intervention(root, "pause-before-admission")["status"]
            == "applied"
        )
        application.intervene(root, kind="continue", identity="continue-admission")
        wait_for(
            root,
            lambda value: bool(list((root / "group-runs").glob("*/work/started.json"))),
        )
        first = queries.list_runs(root)["items"][0]
        assert first["status"] == "running"
        application.stop(root, identity="stop-owned-work")
        stopped = wait_for(
            root,
            lambda value: (
                value["status"] in {"stopped", "needs-reconciliation", "failed"}
            ),
        )
        assert stopped["status"] == "stopped", (root / "controller.log").read_text()
        assert wait_exit(started["pid"]) == 0
        original = queries.get_run(root, first["run_id"])
        assert original["interrupted"]
        assert original["status"] != "running"
        assert (
            application.get_intervention(root, "stop-owned-work")["status"] == "applied"
        )
        assert "publication_version" not in stopped
        resumed = application.resume(root)
        state = wait_for(
            root,
            lambda value: (
                value["status"] in {"completed", "failed", "needs-reconciliation"}
            ),
        )
        assert state["status"] == "completed", (root / "controller.log").read_text()
        assert wait_exit(resumed["pid"]) == 0
        runs = queries.list_runs(root)["items"]
        assert len(runs) == 2
        replacement = next(row for row in runs if row["run_id"] != first["run_id"])
        assert replacement["predecessor"] == first["run_id"]
        assert replacement["status"] == "closed"
        assert queries.get_run(root, first["run_id"]) == original
        assert state["result"] == {"answer": 42}
    finally:
        cleanup(started["pid"])
        if resumed is not None:
            cleanup(resumed["pid"])


def test_original_unknown_operation_blocks_resume_and_is_never_replayed(tmp_path):
    root, _, _ = author(tmp_path, "unknown")
    started = application.start(root)
    resumed = None
    try:
        state = wait_for(
            root, lambda value: value["status"] in {"needs-reconciliation", "failed"}
        )
        assert state["status"] == "needs-reconciliation", (
            root / "controller.log"
        ).read_text()
        assert wait_exit(started["pid"]) != 0
        runs = queries.list_runs(root)["items"]
        assert len(runs) == 1
        original = runs[0]
        assert original["status"] == "needs-reconciliation"
        original_state = (
            root / "group-runs" / original["run_id"] / "state.json"
        ).read_bytes()
        resumed = application.resume(root)
        assert wait_exit(resumed["pid"]) != 0
        assert application.status(root)["status"] == "needs-reconciliation"
        assert queries.list_runs(root)["items"] == runs
        assert (
            root / "group-runs" / original["run_id"] / "state.json"
        ).read_bytes() == original_state
        assert len(list((root / "group-runs").glob("*/work/started.json"))) == 1
        assert not (root / "outputs/current/answer.json").exists()
    finally:
        cleanup(started["pid"])
        if resumed is not None:
            cleanup(resumed["pid"])


def test_prepare_and_inspect_share_canonical_paths_through_parent_symlink(tmp_path):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(canonical, target_is_directory=True)
    root, _, inspected = author(alias)
    assert inspected["run"]["status"] == "prepared"
    assert application.inspect(root.resolve())["config"] == inspected["config"]
    assert application.configuration(root)["workflow"]["program"].startswith(
        str(root.resolve())
    )


def test_pause_during_work_waits_for_output_before_reporting_paused(tmp_path):
    root, _, _ = author(tmp_path, "cooperative")
    started = application.start(root)
    try:
        wait_for(
            root,
            lambda value: bool(list((root / "group-runs").glob("*/work/started.json"))),
        )
        application.intervene(root, kind="pause", identity="pause-active-work")
        wait_for(root, lambda value: value["status"] == "pausing")
        assert (
            application.get_intervention(root, "pause-active-work")["status"]
            == "pending"
        )
        first = queries.list_runs(root)["items"][0]
        assert first["status"] == "running"
        (root / "group-runs" / first["run_id"] / "work/release").touch()
        wait_for(root, lambda value: value["status"] == "paused")
        assert (
            application.get_intervention(root, "pause-active-work")["status"]
            == "applied"
        )
        assert queries.get_run(root, first["run_id"])["status"] == "returned"
        assert not (root / "outputs/current/answer.json").exists()
        application.intervene(root, kind="continue", identity="continue-after-boundary")
        state = wait_for(
            root,
            lambda value: (
                value["status"] in {"completed", "failed", "needs-reconciliation"}
            ),
        )
        assert state["status"] == "completed", (root / "controller.log").read_text()
        assert state["result"] == {"answer": 42}
        assert len(queries.list_runs(root)["items"]) == 1
        assert wait_exit(started["pid"]) == 0
    finally:
        cleanup(started["pid"])


def test_inspect_and_foreground_use_frozen_imports_without_copying_package_data(
    tmp_path,
):
    workflow = WORKFLOW.replace(
        "from pathlib import Path",
        "from pathlib import Path\nfrom synthetic_task.helper import VALUE",
        1,
    ).replace(
        'router_policy = "Synthetic deterministic local workflow; no model call required."',
        'router_policy = f"Synthetic frozen policy {VALUE}"',
    )
    root, package, prepared = author(
        tmp_path, workflow_text=workflow, package_data=True
    )
    assert prepared["router"]["policy"] == "Synthetic frozen policy 42"
    snapshot = root / "implementation/src/synthetic_task"
    assert (snapshot / "helper.py").read_text() == "VALUE = 42\n"
    assert not (snapshot / "data").exists()
    (package / "helper.py").write_text("VALUE = 9001\n")
    revision = application.revise(
        root, package.parent / "run.yaml", affected=["all"], identity="frozen-imports"
    )["request"]["payload"]["revision"]
    assert (
        application.inspect(root, revision=revision)["router"]["policy"]
        == "Synthetic frozen policy 42"
    )
    assert application.status(root)["config_revision"] == "r000001"
    (package / "workflow.py").write_text(
        "raise RuntimeError('Author workflow changed after prepare')\n"
    )
    (package / "group.py").write_text(
        "raise RuntimeError('Author Group changed after prepare')\n"
    )
    assert application.inspect(root)["router"]["policy"] == "Synthetic frozen policy 42"
    state = application.start(root, foreground=True)
    assert state["status"] == "completed", (root / "controller.log").read_text()
    assert application.get_intervention(root, "frozen-imports")["status"] == "applied"
    assert state["result"] == {"answer": 42}
    assert json.loads((root / "outputs/current/answer.json").read_text()) == {
        "answer": 42
    }
    assert application.inspect(root)["router"]["policy"] == "Synthetic frozen policy 42"


@pytest.mark.parametrize(
    "condition", ["unknown", "stop", "completed", "completed-stop", "startup-error"]
)
def test_controller_finalizes_early_exit_and_startup_error(
    tmp_path, monkeypatch, condition
):
    async def scenario():
        root, _, _ = author(tmp_path, "simple")
        if condition.startswith("completed"):
            baseline = PublicationStore(root).prepare()
            await publication.commit(
                root,
                {"iteration": 3, "status": "running"},
                baseline,
                {"action": "stop", "result": {"answer": 42}, "artifacts": []},
            )
        if condition in {"stop", "completed-stop"}:
            (root / "STOP").touch()
        if condition == "unknown":
            state = application.status(root)
            state.update(
                status="needs-reconciliation",
                cleanup_errors=["Original allocation is still UNKNOWN"],
            )
            write_json(root / "status.json", state)
        write_json(root / "completion.json", {"status": "stale-completion"})
        instance = Controller(root)
        if condition == "startup-error":

            def fail_startup():
                raise ValueError("Synthetic control channel startup failure")

            monkeypatch.setattr(instance, "_open_channel", fail_startup)
        if condition == "unknown":
            with pytest.raises(RuntimeError, match="Unknown operations"):
                await instance.run(resume=True)
            expected = "needs-reconciliation"
            assert instance.state["cleanup_errors"] == [
                "Original allocation is still UNKNOWN"
            ]
        elif condition == "startup-error":
            with pytest.raises(ValueError, match="startup failure"):
                await instance.run()
            expected = "failed"
        else:
            result = await instance.run()
            expected = "completed" if condition.startswith("completed") else "stopped"
            assert result["status"] == expected
            if condition.startswith("completed"):
                assert result["iteration"] == 3
                assert application.status(root)["iteration"] == 3
        assert instance.journal.stream.closed
        completion = json.loads((root / "completion.json").read_text())
        assert completion["status"] == expected
        assert completion["ended_at"] == instance.state["ended_at"]
        assert application.status(root)["status"] == expected
        if condition in {"unknown", "startup-error"}:
            assert completion["error"] == instance.state["error"]
        assert queries.list_runs(root)["items"] == []

    asyncio.run(scenario())


def test_successful_foreground_resume_clears_old_error_without_clearing_cleanup_records(
    tmp_path,
):
    root, _, _ = author(tmp_path)
    state = application.status(root)
    state.update(
        status="failed",
        error={"type": "RuntimeError", "message": "Older failed attempt"},
        cleanup_errors=[],
    )
    write_json(root / "status.json", state)
    completed = application.start(root, resume=True, foreground=True)
    assert completed["status"] == "completed"
    assert completed["result"] == {"answer": 42}
    assert "error" not in completed
    assert completed["cleanup_errors"] == []
    assert json.loads((root / "completion.json").read_text())["error"] is None
    assert "error" not in PublicationStore(root).published_state()
