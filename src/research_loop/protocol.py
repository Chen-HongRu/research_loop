"""Load an explicitly configured trusted workflow, without a plugin registry."""

import importlib.util
from pathlib import Path
from uuid import uuid4


def load_workflow(config):
    path = Path(config["workflow"]["program"])
    spec = importlib.util.spec_from_file_location(f"workflow_{uuid4().hex}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    workflow = module.create(config["workflow"].get("settings", {}))
    for name in ("context", "route", "advance", "complete"):
        if not callable(getattr(workflow, name, None)):
            raise ValueError(f"Workflow must implement {name}")
    if not isinstance(workflow.router_policy, str):
        raise ValueError("Workflow router_policy must be text")
    maximum = workflow.max_active_groups
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 1:
        raise ValueError("Workflow max_active_groups must be a positive integer")
    return workflow
