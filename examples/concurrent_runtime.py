"""Bounded local-process smoke; synthetic jobs, no models, network or GPU.

Run: uv run python examples/concurrent_runtime.py
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time

if __package__:
    from examples import coordinator_fixture
    from examples.runtime_publication import EVALUATION
else:
    import coordinator_fixture
    from runtime_publication import EVALUATION
from research_loop.runtime.coordinator import ConcurrentCoordinator
from research_loop.runtime.invocations import LocalProcessInvoker
from research_loop.runtime.store import StateStore


ADAPTER = {"id": "concurrent-fixture", "version": "1"}


def configuration(**settings):
    return {
        "goal": "Synthetic concurrent runtime",
        "permissions": {},
        "global_evaluation": EVALUATION,
        "runtime": {
            "worker_slots": 2,
            "worker_adapter": ADAPTER,
            "call_timeout_seconds": 8,
            "action_timeout_seconds": 8,
            "inspection_interval_seconds": 0.01,
            "action_retries": 1,
            "planning": False,
            **settings,
        },
        "execution_backends": {
            "synthetic": {
                "version": "1",
                "environment": {"id": "local", "version": "1"},
            }
        },
        "resource_pools": {"cpu": 2},
        "operation_authorizations": {
            "fixture": {
                "backends": ["synthetic"],
                "max_operations": 10,
                "resource_limits": {"cpu": 2},
                "cost_limits": {"ticks": 10},
                "deadline": None,
            }
        },
    }


def run_smoke(root=None):
    if root is None:
        with TemporaryDirectory(prefix="concurrent-runtime-") as temporary:
            return run_smoke(Path(temporary))
    root = Path(root)
    store = StateStore.create(
        root, config=configuration(), max_calls=12, max_iterations=3
    )
    invoker = LocalProcessInvoker(
        root,
        coordinator_fixture.__file__,
        ADAPTER,
        {
            "root": str(root),
            "plan_barrier": ["a", "b"],
            "fail_execute": ["a"],
            "backend_tasks": ["b"],
            "lost_ack": ["b"],
            "fail_collection_once": ["b"],
        },
    )
    validators = {
        role: lambda output, _: output for role in ("plan", "execute", "review")
    }
    with store.coordinator() as owner:
        owner.create_task("a", {}, priority=1)
        owner.create_task("b", {}, priority=2)
        owner.create_task("c", {}, dependencies=["b"])
        runner = ConcurrentCoordinator(owner, invoker, validators)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = runner.tick()
            rows = {row["task_id"]: row for row in owner.snapshot()["iterations"]}
            if rows.get("c", {}).get("status") == "published" and all(
                op["costs_final"] for op in runner.operations.list()
            ):
                break
            time.sleep(0.02)  # Bounded deterministic demo driver, no model polling.
        else:
            raise TimeoutError(f"Synthetic runtime did not finish: {result}")
        assert rows["a"]["status"] == "failed"
        actions = [
            json.loads(path.read_text())
            for path in (root / "fixture").glob("action-*.json")
        ]
        assert sum(row["action"] == "submit" for row in actions) == 1
        assert sum(row["action"] == "collection" for row in actions) == 2
        return {
            "synthetic": True,
            "iterations": {key: value["status"] for key, value in rows.items()},
            "calls": owner.snapshot()["counters"]["calls"],
            "submissions": 1,
            "collection_attempts": 2,
            "published_version": owner.snapshot()["knowledge_version"],
            "worker_slots_in_use": runner.ledger.active_count(),
        }


if __name__ == "__main__":
    print(json.dumps(run_smoke(), indent=2))
