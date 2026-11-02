"""Independent, reviewed long-training studies selected from published evidence."""

from __future__ import annotations

import copy
from dataclasses import replace
import fcntl
import hashlib
import json
from pathlib import Path
import time

from .evaluation import build_frontier, validate_evaluation
from .harness import HarnessConfig, HarnessError, publish_completed_response


def _read(path):
    return json.loads(Path(path).read_text())


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    temporary.replace(path)


def validate_policy(value):
    if value is None:
        return None
    fields = {"version", "recipe", "evaluation", "max_runs", "max_seconds_per_run"}
    if not isinstance(value, dict) or set(value) != fields or value["version"] != 1:
        raise ValueError(
            "Long training requires version/recipe/evaluation/max_runs/max_seconds_per_run"
        )
    for name in ("max_runs", "max_seconds_per_run"):
        if type(value[name]) is not int or value[name] < 1:
            raise ValueError(f"Long training {name} must be a positive integer")
    recipe = value["recipe"]
    if not isinstance(recipe, dict) or set(recipe) != {
        "total_steps",
        "validation_interval",
        "validation_split",
        "initialization",
        "seed",
    }:
        raise ValueError("Invalid frozen long-training recipe")
    if any(
        type(recipe[k]) is not int or recipe[k] < 1
        for k in ("total_steps", "validation_interval")
    ):
        raise ValueError("Long-training steps must be positive integers")
    if (
        recipe["total_steps"] % recipe["validation_interval"]
        or recipe["validation_split"] != "full"
        or recipe["initialization"] != "fresh"
        or type(recipe["seed"]) is not int
    ):
        raise ValueError(
            "Long training requires fresh initialization and full validation including the final step"
        )
    evaluation = validate_evaluation(value["evaluation"])
    if evaluation["data_role"] != "development" or not evaluation["allow_feedback"]:
        raise ValueError(
            "Feedback from long training requires a development evaluation"
        )
    return copy.deepcopy(value)


def stage_schema(phase, context, schema):
    if phase != "review" or not context.get("long_training"):
        return schema
    result = copy.deepcopy(schema)
    text = {"type": "string"}
    nomination = {
        "candidate_id": text,
        "comparison_candidate_id": text,
        "basis": {
            "type": "string",
            "enum": ["significant_frontier_gain", "architecture_gain"],
        },
        "reason": text,
        "evidence": {"type": "array", "items": text},
    }
    fields = {
        "reason": text,
        "nominations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": nomination,
                "required": list(nomination),
                "additionalProperties": False,
            },
        },
    }
    result["properties"]["long_training_decision"] = {
        "type": "object",
        "properties": fields,
        "required": list(fields),
        "additionalProperties": False,
    }
    result["required"].append("long_training_decision")
    return result


def guidance(phase, context):
    if not context.get("long_training") and not context.get("long_training_results"):
        return ""
    result = """
Long-training evidence has a separate fixed protocol. Incorporate independently
reviewed long_training_results into research knowledge with exact sources,
checkpoint steps, data scope, failures and limitations. Compare all metrics from
the SAME checkpoint. Distinguish the final endpoint from a best-over-curve value.
Full-development validation is search feedback, not independent final-test evidence.
Long training and repeated summaries of it are not independent replications.
Do not rank long-training measurements in the short-training Frontier or infer
causality/statistical significance from a single seed or numeric improvement.
"""
    if phase == "review" and context.get("long_training"):
        result += """
Fill long_training_decision with a reason even when nominations is empty.
Nominate only a current valid, verified global-evaluation candidate that strictly
improves on a named earlier eligible comparable candidate. significant_frontier_gain
requires entering the global Frontier and improving on a previous Frontier member;
judge practical magnitude using the actual protocol and uncertainty, without an
invented statistical significance threshold. architecture_gain may be small, but
must cite a substantial representation/readout/conditioning/message/factorization
change and its exact baseline comparison. Scalar tuning is not an architecture change.
For each nomination cite candidate_id, qualified comparison_candidate_id, concrete
reason and evidence paths. Explain cost and what the frozen long run will resolve.
Do not nominate unchanged candidates or use missing measurements as improvements.
Nominations authorize only the separately configured long-training lane and budget.
"""
    return result


def _published(root, folder):
    if (
        not (folder / "publication.json").exists()
        or not (folder / "iteration.json").exists()
    ):
        return False
    metadata = _read(folder / "iteration.json")
    state = _read(Path(root) / "state.json")
    return metadata.get("status") == "reviewed" and (
        int(folder.name) < state["iteration"]
        or (int(folder.name) == state["iteration"] and state["phase"] == "done")
    )


def nominations(root, policy, decision, candidates, before, after, metadata):
    """Check measured eligibility/direction; scientific value remains Review's judgment."""
    if not decision["reason"].strip():
        raise ValueError("Long-training decision needs an explanation")
    if not decision["nominations"]:
        return []
    definition = _read(Path(root) / "study.json")["global_evaluation"]
    if (
        not definition
        or definition["id"] == policy["evaluation"]["id"]
        or definition["comparison_scope"] == policy["evaluation"]["comparison_scope"]
    ):
        raise ValueError(
            "Long training requires a distinct evaluation identity and scope"
        )
    known = {}
    for folder in sorted((Path(root) / "iterations").glob("*")):
        if _published(root, folder) and (folder / "candidates.json").exists():
            record = _read(folder / "candidates.json")
            for correction in record.get("corrections", []):
                known[correction["candidate_id"]].update(
                    valid=correction["valid"], verified=correction["verified"]
                )
            known.update({c["id"]: dict(c) for c in record["candidates"]})
    current = {c["id"].split(":", 1)[1]: c for c in candidates}
    previous_ids = set(
        before.get("global", {}).get(definition["id"], {}).get("candidate_ids", [])
    )
    next_ids = set(after["global"][definition["id"]]["candidate_ids"])
    next_exclusions = {
        e["id"]: e["reason"] for e in after["global"][definition["id"]]["excluded"]
    }
    requests, seen = [], set()
    for nomination in decision["nominations"]:
        cid = nomination["candidate_id"]
        candidate = current.get(cid)
        comparison = known.get(nomination["comparison_candidate_id"])
        if cid in seen or not candidate or not comparison:
            raise ValueError(
                "Long-training nomination needs unique current and earlier candidate IDs"
            )
        seen.add(cid)
        if (
            not nomination["reason"].strip()
            or not nomination["evidence"]
            or any(not s.strip() for s in nomination["evidence"])
        ):
            raise ValueError("Long-training nomination needs a reason and evidence")
        if not metadata.get("workspace_ref"):
            raise ValueError("Long training needs a recoverable exact source version")
        for item in (candidate, comparison):
            if (
                not build_frontier(definition, [item])["candidate_ids"]
                or next_exclusions.get(item["id"], "dominated") != "dominated"
            ):
                raise ValueError(
                    "Long-training candidate/comparison is ineligible or corrected"
                )
        objectives = definition["objectives"]
        improved = False
        for objective in objectives:
            a, b = (
                candidate["metrics"][objective["name"]],
                comparison["metrics"][objective["name"]],
            )
            gain = a - b if objective["direction"] == "maximize" else b - a
            if gain < 0:
                raise ValueError(
                    "Long-training nomination must improve without worsening comparable objectives"
                )
            improved |= gain > 0
        if not improved:
            raise ValueError("Unchanged results do not qualify for long training")
        if nomination["basis"] == "significant_frontier_gain" and (
            candidate["id"] not in next_ids or comparison["id"] not in previous_ids
        ):
            raise ValueError(
                "Frontier nomination needs a previous Frontier comparison and current membership"
            )
        identity = {
            "artifact_version": candidate["artifact"]["version"],
            "recipe": policy["recipe"],
            "evaluation": policy["evaluation"],
        }
        run_id = (
            "L"
            + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[
                :16
            ]
        )
        requests.append(
            {
                "id": run_id,
                "source_iteration": metadata["id"],
                "candidate": candidate,
                "comparison": comparison,
                "nomination": nomination,
                "source_workspace": metadata["workspace"],
                "source_workspace_ref": metadata["workspace_ref"],
                "source_project": _read(Path(root) / "study.json")["workspace"],
                "recipe": policy["recipe"],
                "evaluation": policy["evaluation"],
            }
        )
    return requests


def snapshot(study_root):
    rows = []
    for path in sorted((Path(study_root) / "long-runs").glob("*/request.json")):
        request = _read(path)
        folder = path.parent
        state = (
            _read(folder / "study/state.json")
            if (folder / "study/state.json").exists()
            else {}
        )
        row = {
            "id": request["id"],
            "request": request,
            "source_iteration": request["source_iteration"],
            "status": state.get("status", "queued"),
            "phase": state.get("phase", "queued"),
            "error": state.get("error"),
        }
        if (folder / "withdrawn.json").exists():
            row.update(
                status="withdrawn",
                phase="done",
                error=_read(folder / "withdrawn.json")["reason"],
            )
        if (folder / "result.json").exists():
            row.update(
                status="reviewed", phase="done", result=_read(folder / "result.json")
            )
        rows.append(row)
    return rows


def completed_results(study_root):
    return [
        row["result"] for row in snapshot(study_root) if row["status"] == "reviewed"
    ]


class _LongHarness:
    """Reuse ordinary Study publication/recovery, with a fixed selected candidate."""

    def __init__(self, request, delegate, executor, deadline):
        self.request, self.delegate, self.executor, self.deadline = (
            request,
            delegate,
            executor,
            deadline,
        )

    def run(self, **kwargs):
        context = _read(kwargs["run_dir"] / "context.json")
        # Study context has no source DAG references: the parent is explicitly external.
        phase = kwargs["run_dir"].parent.name
        if phase.endswith("-proposal"):
            result = {
                "markdown": self.request["nomination"]["reason"],
                "route_id": "new",
                "route_title": "Frozen long-training confirmation",
                "action": "Train the frozen nominated candidate under the exact long-training recipe",
                "base_experiment": None,
                "references": [],
            }
        elif phase == "judge":
            result = {
                "decision": "select",
                "choice": "a",
                "action": context["proposals"]["a"]["action"],
                "markdown": "Candidate and protocol were fixed by the published parent nomination.",
                "goal_status": "undetermined",
                "stop_reason": None,
            }
        elif phase == "execute":
            try:
                result = self.executor(
                    self.request,
                    Path(kwargs["workspace"]),
                    deadline=min(
                        self.deadline, time.time() + kwargs["timeout_seconds"]
                    ),
                )
            except Exception as error:
                raise HarnessError(str(error)) from error
        else:
            result = self.delegate.run(
                **{
                    **kwargs,
                    "prompt": kwargs["prompt"]
                    + "\nFrozen long-training request (external parent evidence, not an editable recipe):\n"
                    + json.dumps(self.request, ensure_ascii=False)
                    + "\nNo candidate changes, alternative recipe, checkpoint selection or new measurements are authorized. Review the full curve and all metrics at matching checkpoint steps; distinguish endpoint from best-over-curve, and preserve negative/incomplete results. The short-study goal is not proved by this independent protocol.\n",
                }
            )
            if phase == "plan" and result["decision"] == "investigate":
                if (
                    result["local_evaluation"] is not None
                    or result["base_experiment"] is not None
                    or result["references"]
                ):
                    raise ValueError(
                        "Long-training Plan changed the frozen evaluation or source"
                    )
        publish_completed_response(kwargs["run_dir"], result)
        return result


class LongTrainingManager:
    """One process per lane; child studies never write parent research documents."""

    def __init__(self, study_root, delegate, executor):
        self.root = Path(study_root)
        self.delegate, self.executor = delegate, executor

    def sync_requests(self):
        for folder in sorted((self.root / "iterations").glob("*")):
            path = folder / "long-training.json"
            if path.exists() and _published(self.root, folder):
                for request in _read(path)["requests"]:
                    destination = (
                        self.root / "long-runs" / request["id"] / "request.json"
                    )
                    if not destination.exists():
                        _write(destination, request)

    def run_one(self, *, deadline):
        from .core import Study, initialize

        policy = validate_policy(_read(self.root / "study.json").get("long_training"))
        if policy is None:
            return None
        lane = self.root / "long-runs"
        lane.mkdir(parents=True, exist_ok=True)
        with (lane / "lane.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.sync_requests()
            rows = snapshot(self.root)
            started = [
                r for r in rows if (lane / r["id"] / "study/study.json").exists()
            ]
            active = [
                r
                for r in started
                if r["status"] != "reviewed"
                and not (
                    r["phase"] == "done"
                    and not _published(
                        lane / r["id"] / "study",
                        lane / r["id"] / "study/iterations/0001",
                    )
                )
            ]
            eligible = active or (
                [r for r in rows if r not in started and r["status"] != "withdrawn"][:1]
                if len(started) < policy["max_runs"]
                else []
            )
            if not eligible or time.time() >= deadline:
                return None
            row = eligible[0]
            request, folder = row["request"], lane / row["id"]
            child = folder / "study"
            deadline_file = folder / "deadline.json"
            if not deadline_file.exists():
                _write(
                    deadline_file,
                    {
                        "deadline": min(
                            deadline, time.time() + policy["max_seconds_per_run"]
                        )
                    },
                )
            run_deadline = min(deadline, _read(deadline_file)["deadline"])
            if not (child / "study.json").exists():
                views = _read(self.root / "frontiers.json")["global"]
                view = views.get(request["candidate"]["evaluation_id"], {})
                exclusions = {
                    item["id"]: item["reason"] for item in view.get("excluded", [])
                }
                cid = request["candidate"]["id"]
                if (
                    cid not in view.get("candidate_ids", [])
                    and exclusions.get(cid) != "dominated"
                ):
                    _write(
                        folder / "withdrawn.json",
                        {
                            "reason": "Nominated source eligibility was corrected before allocation",
                            "candidate_id": cid,
                        },
                    )
                    return None
                parent = _read(self.root / "study.json")
                harness_config = replace(
                    HarnessConfig(**parent["harness"]),
                    timeout_seconds=policy["max_seconds_per_run"],
                )
                initialize(
                    child,
                    goal="Independently assess the fixed long-training candidate and full-validation trajectory. Preserve source, recipe, all metric/checkpoint pairs, limitations and reusable checkpoint references.\n"
                    + json.dumps(request, ensure_ascii=False),
                    harness=harness_config,
                    global_evaluation=request["evaluation"],
                    max_iterations=1,
                    max_calls=12,
                    max_seconds=policy["max_seconds_per_run"],
                    strategy={"selection": "direct", "review": "full"},
                )
            harness = _LongHarness(request, self.delegate, self.executor, run_deadline)
            study = Study(child, harness)
            # A failed/unknown Execute requires explicit reconciliation, as in short studies.
            state = study.run()
            iteration = child / "iterations/0001"
            if state["phase"] != "done" or not _published(child, iteration):
                return state
            review = _read(child / state["review_ref"])
            execute = _read(child / state["execution_ref"])
            output = child / "workspace/result.json"
            result = {
                "id": request["id"],
                "source_iteration": request["source_iteration"],
                "candidate_id": request["candidate"]["id"],
                "recipe": request["recipe"],
                "evaluation": request["evaluation"],
                "review": review,
                "execute": execute,
                "result": _read(output) if output.exists() else None,
                "publication_ref": str(iteration / "publication.json"),
            }
            _write(
                folder / "review.json",
                {
                    "id": request["id"],
                    "candidate_id": request["candidate"]["id"],
                    "publication_ref": result["publication_ref"],
                    "review": review,
                    "execute": execute,
                },
            )
            _write(folder / "result.json", result)
            return result
