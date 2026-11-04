"""Trusted acceptance application and parent-owned local subprocess backend.

Callbacks never detach children. The acceptance driver starts a backend service
before the coordinator; that service durably identifies, owns, and waits for its
actual child jobs. Missing records never establish job absence. This tiny local
fixture is not a production process supervisor or a scientific evaluator.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import time
import uuid

from research_loop.runtime.files import publish_artifact, publish_json


APPLICATION_IDENTITY = {"id": "local-acceptance", "version": "1"}


def _wait(path, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text())
        time.sleep(0.01)
    raise TimeoutError(f"Expected positive backend record: {path.name}")


@contextmanager
def managed_backend(root, workspace, commit):
    """Own one bounded service and reap it, including on failed assertions."""
    root = Path(root)
    folder = root / "backend"
    for name in ("requests", "jobs", "actions", "calls"):
        (folder / name).mkdir(parents=True, exist_ok=True)
    with (folder / "service.log").open("wb") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "serve",
                str(root),
                str(workspace),
                commit,
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            identity = _wait(folder / "service.json")
            if identity["pid"] != process.pid:
                raise AssertionError("Backend service ownership differs")
            yield identity
        finally:
            (folder / "stop").touch()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # This is the explicitly owned backend service group, never a
                # detached callback sender or a PID discovered by inference.
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
            if process.returncode:
                raise RuntimeError(
                    "Local backend service failed: "
                    + (folder / "service.log").read_text()
                )


def _serve(root, workspace, commit):
    folder = root / "backend"
    identity = dict(pid=os.getpid(), process_group=os.getpgrp(), nonce=uuid.uuid4().hex)
    publish_json(folder / "service.json", identity)
    live, seen = {}, set()
    deadline = time.monotonic() + 35
    try:
        while not (folder / "stop").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Bounded acceptance backend service expired")
            for token, process in list(live.items()):
                code = process.poll()
                if code is not None:
                    process.wait()
                    publish_json(
                        folder / "jobs" / token / "finished.json",
                        dict(
                            exit_code=code, finished=time.monotonic(), service=identity
                        ),
                    )
                    del live[token]
            for path in sorted((folder / "requests").glob("*.json")):
                if path.stem in seen or len(live) >= 2:
                    continue
                request = json.loads(path.read_text())
                if (
                    request["commit"] != commit
                    or request["submission_token"] != path.stem
                ):
                    raise ValueError(
                        "Backend request differs from frozen workspace/token"
                    )
                token = path.stem
                job = folder / "jobs" / token
                job.mkdir()
                with (job / "output.log").open("wb") as log:
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            str(Path(__file__).resolve()),
                            "job",
                            str(root),
                            token,
                            request["task"],
                            str(workspace),
                            commit,
                        ],
                        cwd=workspace,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
                live[token] = process
                seen.add(token)
                publish_json(
                    job / "started.json",
                    dict(
                        **request,
                        job_id="acceptance-" + token,
                        pid=process.pid,
                        service=identity,
                        started=time.monotonic(),
                        active_pids=[
                            child.pid for child in live.values() if child.poll() is None
                        ],
                    ),
                )
            time.sleep(0.01)
    finally:
        for process in live.values():
            if process.poll() is None:
                process.terminate()
        for process in live.values():
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        publish_json(
            folder / "reaped.json", dict(service=identity, remaining_children=0)
        )


def _job(root, token, name, workspace, commit):
    folder = root / "backend"
    job = folder / "jobs" / token
    observed = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=workspace, text=True
    ).strip()
    if observed != commit:
        raise ValueError("Actual job workspace commit changed")
    publish_json(
        job / "entered.json", dict(pid=os.getpid(), commit=observed, task=name)
    )
    deadline = time.monotonic() + 25
    while not (folder / "release").exists():
        if (folder / "stop").exists() or time.monotonic() >= deadline:
            raise TimeoutError("Actual local job was never released")
        time.sleep(0.01)
    candidate = runpy.run_path(str(workspace / "candidate.py"))
    try:
        score = candidate["run"](name)
        result = dict(task=name, score=score, commit=observed, succeeded=True)
    except RuntimeError as error:
        result = dict(
            task=name, score=None, commit=observed, succeeded=False, error=str(error)
        )
    publish_json(job / "result.json", result)
    return 0 if result["succeeded"] else 1


def validate(role, output, context, config):
    expected = {
        "plan": {"local_evaluation", "timeout_seconds"},
        "execute": {"artifacts", "operations"},
        "review": {"knowledge", "reviews", "corrections"},
        "scout": {"knowledge", "reviews", "corrections"},
        "advocate": {"suggestions"},
        "judge": {"changes"},
    }
    if (
        role not in expected
        or not isinstance(output, dict)
        or set(output) != expected[role]
    ):
        raise ValueError("Unexpected deterministic acceptance output")
    if role == "plan":
        if output != {"local_evaluation": None, "timeout_seconds": 30}:
            raise ValueError("Unexpected acceptance plan")
    elif any(not isinstance(value, list) for value in output.values()):
        raise ValueError("Acceptance output fields must be lists")
    return json.loads(json.dumps(output))


def _agent(payload, config):
    folder = Path(config["root"]) / "backend"
    call, package = payload["call"], payload["package"]
    context = package["context"]
    name, role = context["task"]["id"], call["role"]
    publish_json(
        folder / "calls" / f"{call['attempt_id']}.json", dict(task=name, role=role)
    )
    if role == "plan":
        output = dict(local_evaluation=None, timeout_seconds=30)
    elif role == "execute":
        operations = []
        if name in {"A", "B"}:
            operations = [
                dict(
                    id="train",
                    backend="local-acceptance",
                    authorization_id="acceptance",
                    resources={"cpu": 1},
                    cost_bounds={"ticks": 1},
                    timeout_seconds=30,
                    input_artifacts=[],
                    parameters=dict(task=name, commit=config["commit"]),
                )
            ]
        output = dict(artifacts=[], operations=operations)
    elif role == "review":
        if name == "A":
            if context["operations"][0]["terminal_outcome"] != "failed":
                raise AssertionError("A must fail in the actual backend job")
            raise RuntimeError("Acceptance Review rejects the failed local job")
        failed = folder / "review-failed-B.json"
        if name == "B" and not failed.exists():
            publish_json(failed, dict(attempt_id=call["attempt_id"]))
            raise RuntimeError("Acceptance Review fails once before explicit retry")
        output = dict(
            knowledge=[],
            corrections=[],
            reviews=[
                dict(
                    measurement_id=value["id"],
                    valid=True,
                    verified=True,
                    reason="Deterministic local acceptance value only",
                )
                for key, value in context.items()
                if key.startswith("measurement:")
                and value["iteration_id"] == call["iteration_id"]
            ],
        )
    else:
        raise ValueError("Acceptance does not invoke autonomous planning or Scout")
    return dict(output=output, evidence_refs=package["manifest"]["allowed_evidence"])


def invoke(kind, payload, config):
    if kind == "agent":
        return _agent(payload, config)
    root = Path(config["root"])
    folder = root / "backend"
    action, operation = payload["action"], payload["operation"]
    name = payload["request"]["parameters"]["task"]
    event_key = "acceptance:" + uuid.uuid4().hex
    token = (
        payload["permit"]["submission_token"]
        if action == "submit"
        else operation["submission_token"]
    )
    publish_json(
        folder / "actions" / f"{uuid.uuid4().hex}.json",
        dict(
            action=action,
            task=name,
            operation_id=operation["id"],
            submission_token=token,
            job_id=operation["job_id"],
        ),
    )
    if action == "prepare":
        data = dict(
            ready=True,
            reason="Local job has not been allocated",
            effective_parameters=payload["request"]["parameters"],
        )
    elif action == "submit":
        publish_json(
            folder / "requests" / f"{token}.json",
            dict(
                operation_id=operation["id"],
                submission_token=token,
                task=name,
                commit=config["commit"],
            ),
        )
        started = _wait(folder / "jobs" / token / "started.json")
        _wait(folder / "jobs" / token / "entered.json")
        if name == "B":
            publish_json(
                folder / "lost-ack-B.json",
                dict(job_id=started["job_id"], submission_token=token),
            )
            raise RuntimeError(
                "Submit acknowledgement lost after actual backend child started"
            )
        data = dict(
            outcome="accepted",
            job_id=started["job_id"],
            proof=None,
            reason="Managed local subprocess started",
        )
    else:
        if token is None:
            raise ValueError("No original submission token: job absence is not proof")
        job = folder / "jobs" / token
        started = _wait(job / "started.json")
        if started["operation_id"] != operation["id"]:
            raise ValueError("Original job belongs to another operation")
        finished = (
            json.loads((job / "finished.json").read_text())
            if (job / "finished.json").exists()
            else None
        )
        if action == "inspection":
            data = dict(
                job_id=started["job_id"],
                terminal=finished is not None,
                outcome=("succeeded" if finished["exit_code"] == 0 else "failed")
                if finished
                else None,
                physical_released=finished is not None,
                not_submitted_proof=None,
            )
        elif action == "collection":
            if finished is None:
                raise ValueError("Collection needs positive child exit receipt")
            failed = folder / "collection-failed-B.json"
            if name == "B" and not failed.exists():
                publish_json(failed, dict(job_id=started["job_id"]))
                raise RuntimeError("Transient original-job collection failure")
            result = json.loads((job / "result.json").read_text())
            artifact = dict(
                path=f"backend/jobs/{token}/result.json", version=started["job_id"]
            )
            evaluations = []
            if result["succeeded"]:
                reference = publish_artifact(
                    root,
                    dict(
                        version=2,
                        iteration_id=operation["iteration_id"],
                        execute_attempt_id=operation["execute_attempt_id"],
                        candidate_id=name,
                        artifact=artifact,
                        evaluation_id="synthetic-score",
                        comparison_scope="fixed-fixture-v1",
                        evaluator=dict(id="local-acceptance", version="1"),
                        metrics=dict(score=result["score"]),
                        operation_id=operation["id"],
                        collection_event_key=event_key,
                    ),
                )
                evaluations = [dict(receipt_ref=reference, allow_feedback=True)]
            data = dict(artifacts=[artifact], evaluations=evaluations)
        elif action == "settlement":
            # This local fixture charges zero, independently of physical exit.
            data = dict(final_costs={"ticks": 0})
        elif action == "cancellation":
            # ACK records intent only. Physical release still requires service
            # wait()/finished.json and subsequent inspection.
            data = dict(
                job_id=started["job_id"],
                acknowledged=True,
                reason="Managed local cancellation requested",
            )
        else:
            raise ValueError("Unknown local acceptance action")
    return dict(event_key=event_key, data=data)


if __name__ == "__main__":
    mode, location, *arguments = sys.argv[1:]
    if mode == "serve":
        _serve(Path(location), Path(arguments[0]), arguments[1])
    elif mode == "job":
        raise SystemExit(
            _job(
                Path(location),
                arguments[0],
                arguments[1],
                Path(arguments[2]),
                arguments[3],
            )
        )
    else:
        raise ValueError("Unknown local acceptance process mode")
