"""Issue 77 acceptance with actual Git and managed local subprocess jobs.

Run: uv run python examples/runtime_acceptance.py

Agent replies and evaluation values are deterministic fixtures. Process overlap,
child exit, submit-ACK loss, coordinator recovery, collection retry, and Review
retry are exercised through the real runtime. No models, GPUs, or network calls.
"""

import json
from contextlib import contextmanager
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time

if __package__:
    from examples import local_acceptance_backend as backend
    from examples.runtime_publication import EVALUATION
else:
    import local_acceptance_backend as backend
    from runtime_publication import EVALUATION

from research_loop.runtime.application import load_application, prepare_application
from research_loop.runtime.controls import ControlInbox, receipt, submit_command
from research_loop.runtime.coordinator import ConcurrentCoordinator
from research_loop.runtime.store import StateStore
from research_loop.runtime.task_pool import TaskPool


def _git(workspace, *arguments):
    return subprocess.check_output(
        ["git", "-C", str(workspace), *arguments], text=True
    ).strip()


def _workspace(base):
    workspace = base / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    (workspace / "candidate.py").write_text(
        "import time\n\ndef run(task):\n"
        "    time.sleep(0.05)\n"
        '    if task == "A":\n'
        '        raise RuntimeError("deterministic local candidate failure")\n'
        "    return float(sum(range(100)) == 4950)\n"
    )
    _git(workspace, "add", "candidate.py")
    _git(
        workspace,
        "-c",
        "user.name=Acceptance Fixture",
        "-c",
        "user.email=acceptance@example.invalid",
        "commit",
        "-q",
        "-m",
        "Synthetic acceptance candidate",
    )
    return workspace, _git(workspace, "rev-parse", "HEAD")


def _configuration(root, workspace, commit):
    adapter = backend.APPLICATION_IDENTITY
    application = prepare_application(
        backend.__file__,
        dict(root=str(root), workspace=str(workspace), commit=commit),
        adapter,
    )
    return dict(
        goal="Local process acceptance; deterministic values have no research meaning",
        permissions={},
        global_evaluation=EVALUATION,
        runtime=dict(
            worker_slots=2,
            worker_adapter=adapter,
            application=application,
            call_timeout_seconds=8,
            action_timeout_seconds=8,
            inspection_interval_seconds=0.1,
            action_retries=1,
            planning=False,
        ),
        execution_backends={
            "local-acceptance": dict(
                version="1", environment=dict(id="temporary-git", version=commit)
            )
        },
        resource_pools={"cpu": 2},
        operation_authorizations={
            "acceptance": dict(
                backends=["local-acceptance"],
                max_operations=2,
                resource_limits={"cpu": 2},
                cost_limits={"ticks": 2},
                deadline=None,
            )
        },
    )


def _iterations(owner):
    return {row["task_id"]: row for row in owner.snapshot()["iterations"]}


def _actions(root, action=None, task=None):
    rows = [
        json.loads(path.read_text())
        for path in (root / "backend" / "actions").glob("*.json")
    ]
    return [
        row
        for row in rows
        if (action is None or row["action"] == action)
        and (task is None or row["task"] == task)
    ]


def _drive(runner, condition, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = runner.tick()
        if condition():
            return
        time.sleep(0.02)
    raise TimeoutError("Acceptance runtime stalled: " + json.dumps(result))


@contextmanager
def _execution_owner(store):
    with store.coordinator() as owner:
        application = load_application(owner)
        runner = ConcurrentCoordinator(
            owner, application.invoker, application.validators
        )
        try:
            yield owner, runner
        finally:
            pending = [run for run in runner.ledger.list() if not run["worker_retired"]]
            for run in pending:
                application.invoker.cancel(
                    run["token"], "Local acceptance owner cleanup"
                )
            deadline = time.monotonic() + 5
            while pending and time.monotonic() < deadline:
                pending = [
                    run
                    for run in pending
                    if not application.invoker.inspect(run["token"])["proof_ref"]
                ]
                if pending:
                    time.sleep(0.01)
            if pending:
                raise TimeoutError("Acceptance callback senders lack retirement proof")


def run_smoke(base=None):
    if base is None:
        with TemporaryDirectory(prefix="runtime-acceptance-") as temporary:
            return run_smoke(Path(temporary))
    base = Path(base).resolve()
    base.mkdir(parents=True, exist_ok=True)
    workspace, commit = _workspace(base)
    root = base / "study"
    store = StateStore.create(
        root,
        config=_configuration(root, workspace, commit),
        max_iterations=5,
        max_calls=20,
        max_seconds=60,
    )
    with backend.managed_backend(root, workspace, commit) as service:
        with _execution_owner(store) as (owner, runner):
            for priority, (name, dependencies) in enumerate(
                (("A", []), ("B", []), ("C", ["A"]), ("D", ["B"])), 1
            ):
                submit_command(
                    root,
                    "task_create",
                    dict(
                        id=name,
                        specification={
                            "instruction": "Exercise a deterministic local candidate"
                        },
                        priority=priority,
                        dependencies=dependencies,
                    ),
                    "task-" + name,
                )
            ControlInbox(owner).drain()

            def lost_ack_with_two_jobs():
                ops = runner.operations.list()
                states = {
                    runner.operations.request(op["id"])["parameters"]["task"]: op[
                        "state"
                    ]
                    for op in ops
                }
                entered = list((root / "backend" / "jobs").glob("*/entered.json"))
                return (
                    states == {"A": "submitted", "B": "unknown"} and len(entered) == 2
                )

            _drive(runner, lost_ack_with_two_jobs)
            original = {
                runner.operations.request(op["id"])["parameters"]["task"]: op
                for op in runner.operations.list()
            }
            assert len(_actions(root, "submit")) == 2
            assert original["B"]["job_id"] is None
            assert original["B"]["submission_token"]
            submits_at_recovery = len(_actions(root, "submit"))
            first_epoch = owner.owner_epoch
            inspections_before = len(_actions(root, "inspection", "B"))

        # The backend service retains and waits for the same actual children;
        # replacing the coordinator does not detach/restart callback senders.
        with _execution_owner(store) as (owner, runner):
            assert owner.owner_epoch > first_epoch
            _drive(
                runner,
                lambda: (
                    len(_actions(root, "inspection", "B")) > inspections_before
                    and runner.operations.get(original["B"]["id"])["job_id"] is not None
                ),
            )
            assert len(_actions(root, "submit")) == 2
            assert (
                runner.operations.get(original["B"]["id"])["submission_token"]
                == original["B"]["submission_token"]
            )
            (root / "backend" / "release").touch()
            _drive(
                runner,
                lambda: all(
                    _iterations(owner).get(name, {}).get("status") == "failed"
                    for name in ("A", "B")
                ),
            )
            failed = _iterations(owner)["B"]
            assert failed["phase"] == "review"
            assert len(_actions(root, "collection", "B")) == 2
            b_collections = _actions(root, "collection", "B")
            assert len({row["submission_token"] for row in b_collections}) == 1
            assert len({row["job_id"] for row in b_collections}) == 1
            attempts = owner.snapshot()["attempts"]
            source = next(
                row
                for row in attempts
                if row["iteration_id"] == failed["id"]
                and row["role"] == "review"
                and row["status"] == "failed"
            )
            retired = next(
                row
                for row in runner.ledger.list("call")
                if row["attempt_id"] == source["id"]
            )
            assert (
                retired["worker_retired"]
                and retired["settled"]
                and retired["completion_ref"]
            )
            execute_before = len([row for row in attempts if row["role"] == "execute"])
            submit_command(
                root,
                "retry_review",
                dict(
                    iteration_id=failed["id"],
                    expected_revision=failed["revision"],
                    reason="Explicit local acceptance Review retry",
                    proof_ref=retired["completion_ref"],
                ),
                "retry-B-review",
            )
            ControlInbox(owner).drain(admit_calls=True)
            assert receipt(root, "retry-B-review")["status"] == "applied"
            _drive(
                runner,
                lambda: (
                    _iterations(owner).get("D", {}).get("status") == "published"
                    and all(
                        op["physical_released"] and op["costs_final"]
                        for op in runner.operations.list()
                    )
                ),
            )
            rows = _iterations(owner)
            assert {name: row["status"] for name, row in rows.items()} == {
                "A": "failed",
                "B": "published",
                "D": "published",
            }
            pool = {row["id"]: row for row in TaskPool(owner).snapshot()["tasks"]}
            assert pool["C"]["status"] == "pending" and pool["C"][
                "blocked_reasons"
            ] == ["dependency:A:failed"]
            b_attempts = [
                row
                for row in owner.snapshot()["attempts"]
                if row["iteration_id"] == failed["id"]
            ]
            b_execute_count = sum(row["role"] == "execute" for row in b_attempts)
            b_review_count = sum(row["role"] == "review" for row in b_attempts)
            assert b_execute_count == 1 and b_review_count == 2
            assert len(_actions(root, "submit")) == 2
            assert execute_before == 2

            # Add ready work and latch STOP before the scheduler sees it.
            submit_command(
                root,
                "task_create",
                dict(id="after-stop", specification={}, priority=0, dependencies=[]),
                "after-stop",
            )
            submit_command(
                root,
                "stop",
                dict(reason="Acceptance completed", cancel_running=False),
                "stop",
            )
            ControlInbox(owner).drain()
            before_stop = owner.snapshot()["counters"].copy()
            before_submits = len(_actions(root, "submit"))
            for _ in range(3):
                runner.tick()
            after_stop = owner.snapshot()["counters"]
            assert (after_stop["calls"], after_stop["iterations"]) == (
                before_stop["calls"],
                before_stop["iterations"],
            )
            assert len(_actions(root, "submit")) == before_submits
            assert "after-stop" not in _iterations(owner)
            jobs = [
                json.loads(path.read_text())
                for path in (root / "backend" / "jobs").glob("*/started.json")
            ]
            finishes = [
                json.loads(path.read_text())
                for path in (root / "backend" / "jobs").glob("*/finished.json")
            ]
            assert len(jobs) == len(finishes) == 2
            assert all(job["service"] == service for job in [*jobs, *finishes])
            assert len({job["pid"] for job in jobs}) == 2
            maximum = max(len(job["active_pids"]) for job in jobs)
            overlap = min(row["finished"] for row in finishes) - max(
                row["started"] for row in jobs
            )
            assert maximum == 2 and overlap > 0
            assert _git(workspace, "status", "--porcelain") == ""
            summary = dict(
                synthetic_agents=True,
                actual_git_workspace=True,
                actual_backend_jobs=len(jobs),
                max_actual_active_jobs=maximum,
                overlap_seconds=round(overlap, 3),
                coordinator_restarts=owner.owner_epoch - first_epoch,
                submits=len(_actions(root, "submit")),
                new_submits_after_recovery=len(_actions(root, "submit"))
                - submits_at_recovery,
                B_collection_attempts=len(b_collections),
                B_review_attempts=b_review_count,
                B_execute_attempts=b_execute_count,
                tasks={
                    "A": "failed",
                    "B": "published",
                    "C": "blocked",
                    "D": "published",
                },
                calls=after_stop["calls"],
                publications=owner.snapshot()["knowledge_version"],
                stop_extra_calls=after_stop["calls"] - before_stop["calls"],
                stop_extra_iterations=after_stop["iterations"]
                - before_stop["iterations"],
                stop_extra_submits=len(_actions(root, "submit")) - before_submits,
            )
    assert (
        json.loads((root / "backend" / "reaped.json").read_text())["remaining_children"]
        == 0
    )
    summary["backend_children_reaped"] = True
    return summary


if __name__ == "__main__":
    print(json.dumps(run_smoke(), indent=2))
