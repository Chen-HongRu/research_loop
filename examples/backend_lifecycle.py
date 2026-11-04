"""Disposable SQLite/backend smoke with one real, bounded local subprocess.

No model, network, GPU or scientific benchmark is used. The adapter below is a
fixed fixture, not a general process launcher. Run from the repository root:
``uv run python examples/backend_lifecycle.py``.
"""

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

from research_loop.runtime.backends import BackendDriver
from research_loop.runtime.context import ContextSnapshot, build_context
from research_loop.runtime.files import publish_artifact, publish_json
from research_loop.runtime.measurements import MeasurementRegistry
from research_loop.runtime.operations import Operations
from research_loop.runtime.store import InvalidTransition, StateStore
from research_loop.runtime.updates import CallIdentity, Updater


EVALUATION = {
    "id": "local-fixture",
    "comparison_scope": "fixed-arithmetic-v1",
    "protocol": "Sum the integers 1 through 3, then divide by ten",
    "data_role": "development",
    "allow_feedback": True,
    "objectives": [{"name": "score", "direction": "maximize", "unit": "points"}],
    "constraints": [],
}

CONFIG = {
    "goal": "Exercise backend lifecycle using synthetic local arithmetic",
    "permissions": {},
    "global_evaluation": EVALUATION,
    "execution_backends": {
        "local-fixture": {
            "version": "1",
            "environment": {"id": "fixture-python", "version": "1"},
        }
    },
    "resource_pools": {"local-slot": 1},
    "operation_authorizations": {
        "fixture-grant": {
            "backends": ["local-fixture"],
            "max_operations": 1,
            "resource_limits": {"local-slot": 1},
            "cost_limits": {"fixture-unit": 1},
            "deadline": None,
        }
    },
}

REQUEST = {
    "id": "arithmetic",
    "backend": "local-fixture",
    "authorization_id": "fixture-grant",
    "resources": {"local-slot": 1},
    "cost_bounds": {"fixture-unit": 1},
    "timeout_seconds": 15,
    "input_artifacts": [],
    "parameters": {"fixture": "sum-one-through-three"},
}

_CHILD = """
import json
import os
from pathlib import Path
import sys
import time

directory, token = Path(sys.argv[1]), sys.argv[2]
deadline = time.monotonic() + 5
while not (directory / 'release').exists():
    if time.monotonic() >= deadline:
        raise TimeoutError('Fixture was not released within five seconds')
    time.sleep(0.01)
result = {'job_id': token, 'score': sum(range(1, 4)) / 10}
temporary = directory / 'result.tmp'
with temporary.open('w') as stream:
    json.dump(result, stream)
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temporary, directory / 'result.json')
"""


def accept_fixture(owner, iteration_id, role, output):
    """Accept explicit synthetic replies using the real protected context path."""
    state = owner.snapshot()
    iteration = next(i for i in state["iterations"] if i["id"] == iteration_id)
    attempt = owner.begin_attempt(
        role,
        request_id=f"fixture-{role}",
        iteration_id=iteration_id,
        expected_revision=iteration["revision"],
    )
    call = CallIdentity.from_attempt(state["study_id"], attempt)
    updater = Updater(owner, validators={role: lambda value, context: value})
    record = owner._feedback_snapshot(call.to_dict())
    inputs = dict(state["config"], task=record["task"], budget=state["limits"])
    if role in {"execute", "review"}:
        inputs["plan"] = updater._plan_output(record)
    snapshot = ContextSnapshot.capture(
        study_id=call.study_id,
        revisions={
            "study": 1,
            "task": record["task"]["revision"],
            "iteration": call.expected_revision,
            "knowledge": record["knowledge_version"],
        },
        inputs=inputs,
        sources=[
            {
                "id": key,
                "key": key,
                "ref": f"control:{key}",
                "revision": 1,
                "reason": "Explicit synthetic fixture context",
                "allow_feedback": True,
            }
            for key in inputs
        ],
        protected_keys=list(inputs),
        allowed_evidence=[],
    )
    package = build_context(snapshot, role, record["task"], "fixture-v1", {})
    updater.bind_context(call, package)
    proposal = updater.validate_output(call, output, [])
    updater.apply_update(proposal, proposal.revisions)
    return call


class LocalFixtureBackend:
    """Finite local fixture with a durable identity and an intentionally lost ACK."""

    version = "1"
    environment = {"id": "fixture-python", "version": "1"}

    def __init__(self, study_root, directory):
        self.study_root = Path(study_root)
        self.directory = Path(directory)
        self.process = None
        self.submissions = 0

    @staticmethod
    def event(operation, kind, data):
        return {"event_key": f"fixture:{operation['id']}:{kind}", "data": data}

    def prepare(self, request, operation):
        if request != REQUEST:
            raise ValueError("This adapter runs only the declared arithmetic fixture")
        self.directory.mkdir()
        publish_json(
            self.directory / "prepared.json", {"operation_id": operation["id"]}
        )
        return self.event(
            operation,
            "prepare",
            {
                "ready": True,
                "reason": "Fixture checked",
                "effective_parameters": request["parameters"],
            },
        )

    def submit(self, request, permit):
        if time.time() >= permit["deadline"]:
            raise TimeoutError("Fixture deadline elapsed before launch")
        self.submissions += 1
        publish_json(self.directory / "intent.json", permit)
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CHILD,
                str(self.directory),
                permit["submission_token"],
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
        # The real child is running, but the controller receives no acceptance.
        raise ConnectionError("Intentional synthetic lost submission response")

    def inspect(self, request, operation):
        intent = json.loads((self.directory / "intent.json").read_text())
        if intent["submission_token"] != operation["submission_token"]:
            raise ValueError("Original fixture identity changed")
        finished = (
            self.process is not None
            and self.process.poll() == 0
            and (self.directory / "result.json").exists()
        )
        return self.event(
            operation,
            "finished" if finished else "running",
            {
                "job_id": intent["submission_token"],
                "terminal": finished,
                "outcome": "succeeded" if finished else None,
                "physical_released": finished,
                "not_submitted_proof": None,
            },
        )

    def collect(self, request, operation):
        result = json.loads((self.directory / "result.json").read_text())
        if result["job_id"] != operation["job_id"]:
            raise ValueError("Result belongs to another job")
        artifact = {
            "path": str(self.directory / "result.json"),
            "version": operation["id"],
        }
        event_key = f"fixture:{operation['id']}:collection"
        receipt = {
            "version": 2,
            "iteration_id": operation["iteration_id"],
            "execute_attempt_id": operation["execute_attempt_id"],
            "operation_id": operation["id"],
            "collection_event_key": event_key,
            "candidate_id": "arithmetic-result",
            "artifact": artifact,
            "evaluation_id": EVALUATION["id"],
            "comparison_scope": EVALUATION["comparison_scope"],
            "evaluator": {"id": "local-arithmetic-fixture", "version": "1"},
            "metrics": {"score": result["score"]},
        }
        ref = publish_artifact(self.study_root, receipt)
        return {
            "event_key": event_key,
            "data": {
                "artifacts": [artifact],
                "evaluations": [{"receipt_ref": ref, "allow_feedback": True}],
            },
        }

    def settle(self, request, operation):
        return self.event(operation, "settlement", {"final_costs": {"fixture-unit": 0}})

    def cancel(self, request, operation):
        raise NotImplementedError("Cancellation is not exercised by this fixed fixture")


def run_smoke():
    with TemporaryDirectory(prefix="backend-lifecycle-") as temporary:
        root = Path(temporary)
        store = StateStore.create(
            root / "study", config=CONFIG, max_calls=3, max_seconds=60
        )
        backend = LocalFixtureBackend(store.root, root / "backend")
        try:
            with store.coordinator() as owner:
                owner.create_task("arithmetic", {})
                iteration = owner.claim_next()
                accept_fixture(owner, iteration["id"], "plan", {"timeout_seconds": 15})
                execute = accept_fixture(
                    owner, iteration["id"], "execute", {"operations": [REQUEST]}
                )
                operations = Operations(owner)
                operation_id = f"{execute.attempt_id}:arithmetic"
                driver = BackendDriver(owner, {"local-fixture": backend})
                driver.prepare(operation_id)
                driver.submit(operation_id)
                unknown = operations.get(operation_id)
                if unknown["state"] != "unknown" or not unknown["held_resources"]:
                    raise AssertionError(
                        "Lost ACK must retain identity and resource holds"
                    )
                first_epoch = owner.owner_epoch
            # The child is still blocked on its gate while SQLite ownership closes.
            with store.coordinator() as owner:
                operations = Operations(owner)
                driver = BackendDriver(owner, {"local-fixture": backend})
                driver.inspect(operation_id)
                try:
                    driver.submit(operation_id)
                except InvalidTransition:
                    pass
                else:
                    raise AssertionError("Unknown submission was admitted twice")
                (backend.directory / "release").touch()
                _, stderr = backend.process.communicate(timeout=10)
                if backend.process.returncode:
                    raise RuntimeError(f"Local fixture failed: {stderr.decode()}")
                driver.inspect(operation_id)
                driver.collect(operation_id)
                collected = operations.get(operation_id)
                collection = json.loads(
                    (store.root / collected["collection_ref"]).read_text()
                )
                ref = collection["data"]["evaluations"][0]["receipt_ref"]
                measurement = MeasurementRegistry(owner).register(iteration["id"], ref)
                operations.finalize_feedback(execute.attempt_id)
                # Review may begin while actual fees remain explicitly unknown.
                accept_fixture(
                    owner,
                    iteration["id"],
                    "review",
                    {"observation": "Fixed local fixture completed"},
                )
                before_settlement = operations.get(operation_id)
                driver.settle(operation_id)
                final = operations.get(operation_id)
                if measurement["metrics"] != {"score": 0.6}:
                    raise AssertionError(
                        "Evaluator result differs from child computation"
                    )
                if not before_settlement["held_costs"] or final["held_costs"]:
                    raise AssertionError(
                        "Unknown fees did not retain their separate hold"
                    )
                return {
                    "submissions": backend.submissions,
                    "owner_reopened": owner.owner_epoch > first_epoch,
                    "score": measurement["metrics"]["score"],
                    "review_before_fee_settlement": True,
                    "resource_hold": final["held_resources"],
                    "cost_hold": final["held_costs"],
                }
        finally:
            if backend.process is not None and backend.process.poll() is None:
                backend.process.terminate()
                backend.process.communicate(timeout=5)


if __name__ == "__main__":
    print(json.dumps(run_smoke(), sort_keys=True))
