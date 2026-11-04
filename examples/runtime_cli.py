"""Synthetic CLI smoke with real local callbacks; no models, network or GPU.

Run: uv run python examples/runtime_cli.py
"""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory

if __package__:
    from examples import coordinator_fixture
    from examples.concurrent_runtime import configuration
else:
    import coordinator_fixture
    from concurrent_runtime import configuration

from research_loop.cli import main


def run_smoke(base=None):
    if base is None:
        with TemporaryDirectory(prefix="runtime-cli-") as temporary:
            return run_smoke(Path(temporary))
    base = Path(base).resolve()
    base.mkdir(parents=True, exist_ok=True)
    root = base / "study"
    config = base / "config.json"
    application_config = base / "application.json"
    specification = base / "task.json"
    config.write_text(json.dumps(configuration()))
    application_config.write_text(
        json.dumps(
            {
                "root": str(root),
                "plan_barrier": ["a", "b"],
                "fail_execute": ["a"],
                "backend_tasks": ["b"],
                "lost_ack": ["b"],
                "fail_collection_once": ["b"],
            }
        )
    )
    specification.write_text(json.dumps({"instruction": "Synthetic CLI example"}))

    def command(*args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["runtime", *map(str, args)])
        if code:
            raise RuntimeError(f"Synthetic CLI command failed: {args[0]}")
        return json.loads(output.getvalue())

    command(
        "init",
        root,
        "--config",
        config,
        "--application",
        coordinator_fixture.__file__,
        "--application-config",
        application_config,
        "--max-iterations",
        "3",
        "--max-calls",
        "12",
        "--max-seconds",
        "60",
    )
    for name, dependency in (("a", None), ("b", None), ("c", "b")):
        command(
            "task",
            "create",
            root,
            name,
            "--specification",
            specification,
            "--request-id",
            "task-" + name,
            *(["--depends-on", dependency] if dependency else []),
        )
    run = command("run", root)
    view = command("status", root)
    states = {row["id"]: row["status"] for row in view["tasks"]}
    assert states == {"a": "failed", "b": "published", "c": "published"}
    operation = view["operations"][0]
    assert operation["physical_released"] and operation["costs_final"]
    assert view["publication_version"] == 2
    return {
        "synthetic": True,
        "driver_state": run["state"],
        "tasks": states,
        "calls": view["counters"]["calls"],
        "publications": view["publication_version"],
        "physical_released": operation["physical_released"],
        "fees_settled": operation["costs_final"],
    }


if __name__ == "__main__":
    print(json.dumps(run_smoke(), indent=2))
