"""Trusted synthetic callback for the concurrent-runtime smoke, never a backend.

Local worker processes and durable files are real; job states, fees and numerical
observations are deterministic fixtures. No models, network or GPUs are used.
"""

import json
from pathlib import Path
import time
import uuid

from research_loop.runtime.files import publish_artifact, publish_json


APPLICATION_IDENTITY = {"id": "concurrent-fixture", "version": "1"}


def validate(role, output, context, config):
    """Validate only this deterministic fixture's shapes, not scientific claims."""
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
        raise ValueError("Unexpected synthetic role output")
    if role == "plan":
        if output != {"local_evaluation": None, "timeout_seconds": 60}:
            raise ValueError("Unexpected synthetic Plan")
    elif any(not isinstance(value, list) for value in output.values()):
        raise ValueError("Synthetic output fields must be lists")
    return json.loads(json.dumps(output))


def invoke(kind, payload, config):
    root = Path(config["root"])
    records = root / "fixture"
    records.mkdir(exist_ok=True)
    if kind == "agent":
        call = payload["call"]
        context = payload["package"]["context"]
        task = context.get("task")
        name = task["id"] if task else call["role"]
        role = call["role"]
        publish_json(
            records / f"call-{call['attempt_id']}.json", {"task": name, "role": role}
        )
        barrier = config.get("plan_barrier", [])
        if role == "plan" and name in barrier:
            (records / f"ready-{name}").touch()
            deadline = time.monotonic() + 5
            while not all((records / f"ready-{item}").exists() for item in barrier):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Synthetic Plan workers did not overlap")
                time.sleep(0.01)
        time.sleep(config.get("role_delays", {}).get(role, 0))
        if role == "plan":
            output = {"local_evaluation": None, "timeout_seconds": 60}
        elif role == "execute":
            if name in config.get("fail_execute", []):
                raise RuntimeError("Synthetic Execute failure")
            operations = []
            if name in config.get("backend_tasks", []):
                operations = [
                    {
                        "id": "job",
                        "backend": "synthetic",
                        "authorization_id": "fixture",
                        "resources": {"cpu": 1},
                        "cost_bounds": {"ticks": 1},
                        "timeout_seconds": 60,
                        "input_artifacts": [],
                        "parameters": {"task": name},
                    }
                ]
            output = {"artifacts": [], "operations": operations}
        elif role == "review":
            marker = records / f"review-failed-{name}"
            if name in config.get("fail_review_once", []) and not marker.exists():
                marker.touch()
                raise RuntimeError("Synthetic Review failure")
            reviews = [
                {
                    "measurement_id": value["id"],
                    "valid": True,
                    "verified": True,
                    "reason": "Deterministic synthetic receipt only",
                }
                for key, value in context.items()
                if key.startswith("measurement:")
                and value["iteration_id"] == call["iteration_id"]
            ]
            output = {"knowledge": [], "reviews": reviews, "corrections": []}
        elif role == "scout":
            output = {"knowledge": [], "reviews": [], "corrections": []}
        elif role == "advocate":
            output = {"suggestions": []}
        elif role == "judge":
            output = {"changes": []}
        else:
            raise ValueError("Unknown synthetic role")
        return {
            "output": output,
            "evidence_refs": payload["package"]["manifest"]["allowed_evidence"],
        }

    operation, action = payload["operation"], payload["action"]
    name = payload["request"]["parameters"]["task"]
    publish_json(
        records / f"action-{uuid.uuid4().hex}.json", {"task": name, "action": action}
    )
    job = records / f"job-{name}.json"
    event_key = action + ":" + uuid.uuid4().hex
    if action == "prepare":
        data = {
            "ready": True,
            "reason": "Free synthetic preparation",
            "effective_parameters": {},
        }
    elif action == "submit":
        if job.exists():
            raise AssertionError("Duplicate synthetic submit")
        publish_json(
            job,
            {
                "id": "fixture-job-" + name,
                "token": payload["permit"]["submission_token"],
            },
        )
        if name in config.get("lost_ack", []):
            raise RuntimeError("Synthetic acknowledgement lost after job creation")
        data = {
            "outcome": "accepted",
            "job_id": "fixture-job-" + name,
            "proof": None,
            "reason": "Fixture job exists",
        }
    elif action == "inspection":
        job_id = json.loads(job.read_text())["id"]
        data = {
            "job_id": job_id,
            "terminal": True,
            "outcome": "succeeded",
            "physical_released": True,
            "not_submitted_proof": None,
        }
    elif action == "collection":
        marker = records / f"collection-failed-{name}"
        if name in config.get("fail_collection_once", []) and not marker.exists():
            marker.touch()
            raise RuntimeError("Synthetic transient collection failure")
        artifact = {"path": f"fixture/result-{name}.json", "version": "fixture-v1"}
        publish_json(root / artifact["path"], {"score": 1.0})
        receipt = publish_artifact(
            root,
            {
                "version": 2,
                "iteration_id": operation["iteration_id"],
                "execute_attempt_id": operation["execute_attempt_id"],
                "candidate_id": name,
                "artifact": artifact,
                "evaluation_id": "synthetic-score",
                "comparison_scope": "fixed-fixture-v1",
                "evaluator": {"id": "synthetic-fixture", "version": "1"},
                "metrics": {"score": 1.0},
                "operation_id": operation["id"],
                "collection_event_key": event_key,
            },
        )
        data = {
            "artifacts": [artifact],
            "evaluations": [{"receipt_ref": receipt, "allow_feedback": True}],
        }
    elif action == "settlement":
        data = {"final_costs": {"ticks": 0}}
    elif action == "cancellation":
        data = {
            "job_id": operation["job_id"],
            "acknowledged": True,
            "reason": "Synthetic cancellation request recorded",
        }
    else:
        raise ValueError("Unknown synthetic backend action")
    return {"event_key": event_key, "data": data}
