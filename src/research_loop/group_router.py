"""Tool-free Router decisions over explicit workflow capabilities and references."""

import json
from pathlib import Path
from uuid import uuid4

from .codex import Native, configuration
from .records import ExecutionTrace, record_thread
from .state import write_json

ACTIONS = ("start", "resume", "hold", "close", "wait", "finish_round", "escalate")
_FIELDS = {
    "action": {"type": "string", "enum": list(ACTIONS)},
    "group_id": {"type": "string"},
    "run_id": {"type": "string"},
    "task_ref": {"type": "string"},
    "input_refs": {"type": "array", "items": {"type": "string"}},
    "prompt": {"type": "string"},
    "result_ref": {"type": "string"},
    "reason": {"type": "string", "minLength": 1},
}
DECISION_SCHEMA = {
    "type": "object",
    "properties": _FIELDS,
    "required": list(_FIELDS),
    "additionalProperties": False,
}


def decision_schema(packet):
    refs = list(packet["references"])
    properties = {
        **_FIELDS,
        "action": {"type": "string", "enum": packet["allowed_actions"]},
        "task_ref": {"type": "string", "enum": ["", *refs]},
        "result_ref": {"type": "string", "enum": ["", *refs]},
        "input_refs": {"type": "array", "items": {"type": "string", "enum": refs}},
        "group_id": {
            "type": "string",
            "enum": ["", *[g["id"] for g in packet["groups"]]],
        },
        "run_id": {"type": "string", "enum": ["", *[r["id"] for r in packet["runs"]]]},
    }
    return {**DECISION_SCHEMA, "properties": properties}


def validate_decision(decision, packet):
    if not isinstance(decision, dict) or set(decision) != set(_FIELDS):
        raise ValueError("Router must return exactly the decision fields")
    for key in _FIELDS:
        if key == "input_refs":
            if not isinstance(decision[key], list) or not all(
                isinstance(v, str) for v in decision[key]
            ):
                raise ValueError("Router input_refs must contain reference IDs")
        elif not isinstance(decision[key], str):
            raise ValueError(f"Router {key} must be a string")
    action = decision["action"]
    if action not in packet["allowed_actions"] or not decision["reason"].strip():
        raise ValueError("Router action is unavailable at this boundary")
    for ref in [decision["task_ref"], decision["result_ref"], *decision["input_refs"]]:
        if ref and ref not in packet["references"]:
            raise ValueError("Router referred to an unsupplied artifact")
    if action == "start" and (
        decision["group_id"] not in {g["id"] for g in packet["groups"]}
        or not decision["task_ref"]
    ):
        raise ValueError("start requires an available Group and task reference")
    if action in {"resume", "hold", "close"}:
        runs = {r["id"]: r for r in packet["runs"]}
        expected = {"held"} if action == "resume" else {"returned"}
        if action == "close":
            expected.add("failed")
        if runs.get(decision["run_id"], {}).get("status") not in expected:
            raise ValueError("Router selected an unavailable Group boundary")
    if action == "wait" and not any(r["status"] == "running" for r in packet["runs"]):
        raise ValueError(
            "No running Group can produce an event; input or escalation is needed"
        )
    if action == "finish_round" and not decision["result_ref"]:
        raise ValueError("finish_round requires an exported result reference")
    return decision


def packet(
    root,
    config,
    runs,
    admitted,
    context,
    *,
    request=None,
    recoveries=(),
    context_ref=None,
):
    root = Path(root)
    context_ref = str(context_ref or root / "call.json")
    references = {str(root / "goal.md"), context_ref}
    initial = list(config.get("input_refs", []))
    initial.extend(str(row["reference"]) for row in recoveries)
    references.update(initial)
    current = root / "outputs/current/index.md"
    if current.exists():
        references.add(str(current.resolve()))
    rows = []
    for run in runs:
        if run["status"] == "closed" and run["run_id"] not in admitted:
            continue
        boundary = {} if run["status"] == "running" else run.get("output") or {}
        references.update(boundary.get("artifacts", []))
        prior = []
        for number in range(1, run["round"]):
            path = (
                root
                / "group-runs"
                / run["run_id"]
                / "rounds"
                / f"{number:06d}"
                / "output.json"
            )
            if path.exists():
                prior.extend(json.loads(path.read_text())["artifacts"])
        references.update(prior)
        references.add(run["task_ref"])
        rows.append(
            {
                "id": run["run_id"],
                "group_id": run["group_id"],
                "status": run["status"],
                "config_revision": run["config_revision"],
                "task_ref": run["task_ref"],
                "output": boundary,
                "prior_artifacts": prior,
                "interrupted": run.get("interrupted", False),
            }
        )
    ids = {path: f"f{index}" for index, path in enumerate(sorted(references))}
    for row in rows:
        row["task_ref"] = ids[row["task_ref"]]
        row["prior_artifacts"] = [ids[ref] for ref in row["prior_artifacts"]]
        row["output"] = {
            **row["output"],
            "artifacts": [ids[ref] for ref in row["output"].get("artifacts", [])],
        }
    text = (
        json.dumps(request, ensure_ascii=False)
        if request
        else (root / "goal.md").read_text()
    )
    return {
        "config_revision": config["revision"],
        "request": text[:4000],
        "request_truncated": len(text) > 4000,
        "goal_ref": ids[str(root / "goal.md")],
        "request_ref": ids[context_ref],
        "groups": [
            {"id": g["id"], "version": g["version"], "description": g["description"]}
            for g in config["groups"]
        ],
        "runs": rows,
        "input_refs": [ids[ref] for ref in initial],
        "references": {value: key for key, value in ids.items()},
        "allowed_actions": list(ACTIONS),
        "recoveries": [
            {
                "predecessor": row["predecessor"],
                "reference": ids[str(row["reference"])],
                "group_id": row["group_id"],
            }
            for row in recoveries
        ],
    }


class Router:
    def __init__(self, root, journal, *, native_factory=Native):
        self.root, self.journal, self.native_factory = (
            Path(root),
            journal,
            native_factory,
        )
        self.native = None

    async def decide(self, config, policy, value, *, iteration):
        directory = self.root / "groups/router" / f"{iteration:06d}-{uuid4().hex}"
        directory.mkdir(parents=True)
        profile = config["profiles"][config["router"]["profile"]]
        native_config, permissions = configuration(
            directory,
            self.root,
            [],
            profile["model"],
            profile["effort"],
            allow_tools=False,
        )
        native = self.native_factory(native_config, self.journal, [])
        self.native = native
        thread = None

        def started(thread_id):
            nonlocal thread
            thread = thread_id
            record_thread(
                self.root,
                thread_id,
                role="router",
                task=f"router/{directory.name}",
                config_revision=config["revision"],
                status="running",
            )

        schema = decision_schema(value)
        trace = ExecutionTrace(
            directory, run_id="router", round=iteration, revision=config["revision"]
        )
        identity = trace.begin(
            "decision",
            "router",
            input=value,
            policy=policy,
            schema=schema,
            profile=profile,
            permissions=permissions,
        )
        write_json(
            directory / "call.json",
            {
                "input": value,
                "policy": policy,
                "schema": schema,
                "profile": profile,
                "config_revision": config["revision"],
                "permissions": permissions,
            },
        )
        try:
            await native.open()
            await native.verify(profile["model"], profile["effort"])
            decision = await native.run(
                policy,
                value,
                schema,
                model=profile["model"],
                effort=profile["effort"],
                allow_tools=False,
                max_output_chars=12000,
                on_started=started,
            )
            validate_decision(decision, value)
            write_json(directory / "decision.json", decision)
            trace.end(identity, output=decision)
            if thread:
                record_thread(self.root, thread, status="completed")
            return decision
        except BaseException as error:
            if thread:
                record_thread(self.root, thread, status="failed")
            trace.end(
                identity,
                "failed",
                error={"type": type(error).__name__, "message": str(error)},
            )
            raise
        finally:
            await native.stop()
            await native.settle()
            await native.close()
            self.native = None

    async def stop(self):
        if self.native is not None:
            await self.native.stop()
