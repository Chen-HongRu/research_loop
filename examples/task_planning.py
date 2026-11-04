"""Disposable subprocess planning smoke; no models, network, GPU or fork helpers.

The fixed fixture validators establish only this example's synthetic contract.
Run with: uv run python examples/task_planning.py
"""

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading

from research_loop.runtime.planning import PlanningRunner
from research_loop.runtime.store import StateStore
from research_loop.runtime.task_pool import TaskPool


_ADVOCATE = {
    "proposals": [{"id": "autonomous", "instruction": "Synthetic autonomous task"}]
}
_JUDGE = {
    "changes": [
        {
            "action": "create",
            "id": "autonomous",
            "specification": {"instruction": "Synthetic autonomous task"},
            "priority": -1000,
            "dependencies": [],
        }
    ]
}
_CHILD = """
import json
from pathlib import Path
import sys
import time

request = json.load(sys.stdin)
if request['role'] == 'advocate':
    directory = Path(sys.argv[1])
    (directory / ('advocate-' + request['attempt_id'])).write_text('ready')
    deadline = time.monotonic() + 5
    while len(list(directory.glob('advocate-*'))) != 2:
        if time.monotonic() >= deadline:
            raise TimeoutError('The two synthetic Advocate subprocesses did not overlap')
        time.sleep(0.01)
print(json.dumps({'output': request['fixture_output'], 'evidence_refs': []}))
"""


def _validator(expected):
    def validate(output, context):
        if output != expected:
            raise ValueError("Output differs from the explicit synthetic fixture")
        return json.loads(json.dumps(output))

    return validate


def run_smoke():
    """Exercise the real runner and return detached observations after cleanup."""
    with TemporaryDirectory(prefix="task-planning-") as temporary:
        root = Path(temporary)
        calls_dir = root / "calls"
        calls_dir.mkdir()
        store = StateStore.create(
            root / "study",
            config={"goal": "Synthetic subprocess planning", "permissions": {}},
        )
        invocation_count = 0
        lock = threading.Lock()

        def invoke(call, package):
            nonlocal invocation_count
            with lock:
                invocation_count += 1
            request = dict(
                role=call.role,
                attempt_id=call.attempt_id,
                context=package.to_dict(),
                fixture_output=_ADVOCATE if call.role == "advocate" else _JUDGE,
            )
            completed = subprocess.run(
                [sys.executable, "-c", _CHILD, str(calls_dir)],
                input=json.dumps(request),
                text=True,
                capture_output=True,
                check=True,
                timeout=10,
            )
            return json.loads(completed.stdout)

        with store.coordinator() as owner:
            pool = TaskPool(owner)
            pool.create_human(
                "human",
                {"instruction": "Preserve this human requirement"},
                request_id="synthetic-human",
                priority=1000,
            )
            original_human = store.snapshot()["tasks"][0]
            runner = PlanningRunner(
                owner,
                invoke,
                validators={
                    "advocate": _validator(_ADVOCATE),
                    "judge": _validator(_JUDGE),
                },
            )
            result = runner.run_once()
            if result["status"] != "applied":
                raise AssertionError("Synthetic planning round failed")
            if runner.run_once() is not None or invocation_count != 3:
                raise AssertionError("Idle planning dispatched extra calls")
            overlap = len(list(calls_dir.glob("advocate-*"))) == 2
            claim_order = [owner.claim_next()["task_id"], owner.claim_next()["task_id"]]
            human = next(
                task for task in store.snapshot()["tasks"] if task["id"] == "human"
            )
            if claim_order != ["human", "autonomous"] or human != original_human:
                raise AssertionError("Human ownership or requirements changed")
            if not overlap or store.snapshot()["counters"]["calls"] != 3:
                raise AssertionError(
                    "Subprocess overlap or unified call accounting failed"
                )
            return dict(
                calls=invocation_count,
                overlap=overlap,
                claim_order=claim_order,
                human_unchanged=human == original_human,
            )


if __name__ == "__main__":
    print(json.dumps(run_smoke(), sort_keys=True))
