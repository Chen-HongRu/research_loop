"""Independent, reviewed long-training studies selected from published evidence."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
from pathlib import Path
import time
import uuid

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
        if name == "max_runs" and value[name] is None:
            continue
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
    if phase != "review":
        return schema
    result = schema
    text = {"type": "string"}
    if context.get("long_training_run"):
        result = copy.deepcopy(result)
        fields = {
            "step": {"type": "integer"},
            "valid": {"type": "boolean"},
            "verified": {"type": "boolean"},
            "reason": text,
        }
        result["properties"]["observation_reviews"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": fields,
                "required": list(fields),
                "additionalProperties": False,
            },
        }
        result["required"].append("observation_reviews")
    if not context.get("long_training"):
        return result
    result = copy.deepcopy(result)
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
    if not any(
        context.get(key)
        for key in ("long_training", "long_training_results", "long_training_run")
    ):
        return ""
    result = """
Long-training evidence has a separate fixed protocol. Incorporate independently
reviewed long_training_results into research knowledge with exact sources,
checkpoint steps, data scope, failures and limitations. Compare all metrics from
the SAME checkpoint. Distinguish the final endpoint from a best-over-curve value.
Full-development validation is search feedback, not independent final-test evidence.
Long training and repeated summaries of it are not independent replications.
Preserve partial full-validation measurements, exact checkpoint steps, missing endpoint
and uncertainty in Review markdown and global research knowledge. A timeout is not
proof that the candidate is ineffective. Do not compare different steps as the same
10k endpoint, invent missing measurements, or promote partial observations to the
endpoint Frontier. Reusable checkpoints require independent per-point acceptance.
Do not rank long-training measurements in the short-training Frontier or infer
causality/statistical significance from a single seed or numeric improvement.
"""
    if phase == "review" and context.get("long_training_run"):
        result += """
Fill observation_reviews with exactly one step/valid/verified/reason entry for every
complete curve point in long_training_observations, including rejected observations.
Do not add missing or unfinished validation steps. Give a concrete nonempty reason
for each acceptance or rejection based on the artifact and checkpoint evidence.
An incomplete Execute has no endpoint measurement: candidate_reviews remains empty.
Record accepted and rejected partial observations, missing_validation_steps and
endpoint_complete explicitly in markdown, global_research and knowledge_changes.
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


def observation_context(context):
    """Read one declared bounded result artifact; backend owns metric/data semantics."""
    request, execute = context["long_training_run"], context["execute"]
    artifacts = [
        item
        for item in execute["artifacts"]
        if Path(item["path"]).name == "result.json"
    ]
    if (
        not artifacts
        and execute["outcome"] == "incomplete"
        and not execute["measurements"]
    ):
        return {
            "curve": [],
            "endpoint_complete": False,
            "missing_validation_steps": list(
                range(
                    request["recipe"]["validation_interval"],
                    request["recipe"]["total_steps"] + 1,
                    request["recipe"]["validation_interval"],
                )
            ),
        }
    if len(artifacts) != 1:
        raise ValueError("Long observations require one declared result.json artifact")
    workspace = Path(context["experiment"]["workspace"]).resolve()
    path = Path(artifacts[0]["path"])
    path = path if path.is_absolute() else workspace / path
    if (
        any(
            part.is_symlink()
            for part in [path, *path.parents]
            if part == workspace or workspace in part.parents
        )
        or not path.resolve().is_relative_to(workspace)
        or not path.is_file()
        or path.stat().st_size > 4 * 1024 * 1024
    ):
        raise ValueError(
            "Long observation result must be a bounded regular workspace artifact"
        )
    result = _read(path)
    if (
        result.get("run_id") != request["id"]
        or result.get("recipe") != request["recipe"]
        or result.get("source_candidate") != request["candidate"]
        or result.get("evaluation") != request["evaluation"]
        or result.get("source_identity") != artifacts[0]["version"]
    ):
        raise ValueError("Long observation artifact differs from the frozen request")
    recipe = request["recipe"]
    expected = list(
        range(
            recipe["validation_interval"],
            recipe["total_steps"] + 1,
            recipe["validation_interval"],
        )
    )
    points = result.get("curve")
    if not isinstance(points, list) or len(points) > len(expected):
        raise ValueError("Invalid bounded long observation curve")
    steps = [point.get("step") for point in points if isinstance(point, dict)]
    if (
        len(steps) != len(points)
        or any(type(step) is not int or step not in expected for step in steps)
        or len(set(steps)) != len(steps)
        or steps != sorted(steps)
    ):
        raise ValueError(
            "Long observation steps must be unique scheduled complete points"
        )
    if any(
        isinstance(value, float) and not math.isfinite(value)
        for point in points
        for value in point.values()
    ):
        raise ValueError("Long observation curve contains nonfinite values")
    missing = [step for step in expected if step not in steps]
    if (
        result.get("missing_validation_steps") != missing
        or type(result.get("endpoint_complete")) is not bool
    ):
        raise ValueError("Long observation endpoint/missing-step metadata differs")
    if result["endpoint_complete"]:
        if missing or execute["outcome"] != "completed":
            raise ValueError("Complete endpoint requires all scheduled validations")
    elif execute["outcome"] != "incomplete" or execute["measurements"]:
        raise ValueError("Partial observations cannot publish endpoint measurements")
    return result


def validate_observation_reviews(context, review):
    """Require independent acceptance/rejection of every available complete point."""
    if not context.get("long_training_run"):
        return
    rows = review["observation_reviews"]
    steps = [row["step"] for row in rows]
    expected = [
        point["step"] for point in context["long_training_observations"]["curve"]
    ]
    if len(steps) != len(set(steps)) or set(steps) != set(expected):
        raise ValueError("Observation reviews must match complete curve steps exactly")
    if any(not row["reason"].strip() for row in rows):
        raise ValueError("Every observation review requires a concrete reason")


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


def admission_path(folder):
    """Keep controller authorization outside Agent-visible Study artifacts."""
    folder = Path(folder).resolve()
    return folder.parents[2] / "control/long-training-rounds" / (folder.name + ".json")


def admitted_round(folder):
    """Return only an authorization committed into a real child iteration."""
    folder = Path(folder)
    path = admission_path(folder)
    if not path.exists():
        return None
    record = _read(path)
    token = record["token"]
    state_path = folder / "study/state.json"
    metadata_path = folder / "study/iterations/0001/iteration.json"
    if not state_path.exists() or not metadata_path.exists():
        return None
    state, metadata = _read(state_path), _read(metadata_path)
    if (
        state.get("round_admitted_at") != record.get("committed_at")
        or record.get("committed_at", float("inf")) >= record.get("cutoff", 0)
        or record.get("committed_at", 0) < record.get("entered_at", float("inf"))
        or state.get("iteration") != 1
        or state.get("round_admission") != token
        or metadata.get("round_admission") != token
        or record.get("child") != str((folder / "study").resolve())
        or record.get("entered_at", float("inf")) >= record.get("cutoff", 0)
        or record.get("request") != _read(folder / "request.json")
    ):
        return None
    return record


class _LongHarness:
    """Reuse ordinary Study publication/recovery, with a fixed selected candidate."""

    def __init__(
        self,
        request,
        delegate,
        executor,
        deadline,
        folder,
        admission=None,
        call_timeout=3600,
    ):
        self.request, self.delegate, self.executor, self.deadline = (
            request,
            delegate,
            executor,
            deadline,
        )

        self.folder, self.admission = Path(folder), admission
        self.call_timeout = call_timeout

    def admit_iteration(self, iteration):
        if iteration != "0001":
            raise ValueError("Only one frozen long-training round is authorized")
        existing = admitted_round(self.folder)
        state_path = self.folder / "study/state.json"
        if (
            not existing
            and state_path.exists()
            and _read(state_path).get("round_admission") is not None
        ):
            raise ValueError(
                "Committed long round admission is invalid; cannot replace its identity"
            )
        identity = self.admission(self.request, existing) if self.admission else None
        if existing:
            if existing["authorization"] != identity:
                raise ValueError("Long round authorization identity changed")
            return existing["token"]
        if time.time() >= self.deadline:
            raise ValueError("Long-training round admission cutoff reached")
        record = {
            "token": {
                "version": 1,
                "id": self.request["id"],
                "nonce": uuid.uuid4().hex,
            },
            "child": str((self.folder / "study").resolve()),
            "request": self.request,
            "cutoff": self.deadline,
            "entered_at": time.time(),
            "authorization": identity,
        }
        if record["entered_at"] >= self.deadline:
            raise ValueError("Long-training round admission cutoff reached")
        _write(admission_path(self.folder), record)
        return record["token"]

    def commit_iteration(self, iteration, token):
        existing = admitted_round(self.folder)
        if existing:
            self.admit_iteration(iteration)
            return existing["committed_at"]
        record = _read(admission_path(self.folder))
        if record["token"] != token:
            raise ValueError("Long round token changed before commit")
        if self.admission:
            identity = self.admission(self.request, None)
            if identity != record["authorization"]:
                raise ValueError("Long round authorization identity changed")
        now = time.time()
        if now >= record["cutoff"]:
            raise ValueError("Long-training round admission cutoff reached")
        _write(admission_path(self.folder), record | {"committed_at": now})
        return now

    def call_allowance(self, remaining):
        # Cumulative child wall time is accounting, not the GPU allocation limit.
        return max(remaining, self.call_timeout)

    def run(self, **kwargs):
        if self.admission:
            token = admitted_round(self.folder)
            if not token:
                raise ValueError("Long child round admission is not committed")
            if self.admission(self.request, token) != token["authorization"]:
                raise ValueError("Long round authorization identity changed")
        token = admitted_round(self.folder)
        grant = (token or {}).get("authorization") or {}
        gpu_limit = grant.get("policy", {}).get("gpu_seconds_per_run")
        controller_contract = (
            "\nThis fixed long-training controller defines contract.timeout_seconds as the Slurm GPU allocation time, exactly "
            + str(gpu_limit)
            + " seconds. Queue waiting has no additional wall-clock cap and consumes no GPU allocation time. State this explicitly in the contract command and acceptance criteria. Plan and Review agents retain their own configured call timeouts. STOP or identity changes can cancel the job.\n"
            if gpu_limit
            else ""
        )
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
            if (
                gpu_limit
                and context["plan"]["contract"]["timeout_seconds"] != gpu_limit
            ):
                raise ValueError(
                    "Frozen long Plan timeout differs from Slurm allocation contract"
                )
            try:
                result = self.executor(
                    self.request,
                    Path(kwargs["workspace"]),
                    deadline=time.time() + kwargs["timeout_seconds"],
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
                    + controller_contract
                    + "\nNo candidate changes, alternative recipe, checkpoint selection or new measurements are authorized. Review the full curve and all metrics at matching checkpoint steps; distinguish endpoint from best-over-curve, and preserve negative/incomplete results. The short-study goal is not proved by this independent protocol.\n",
                }
            )
            if phase == "plan" and result["decision"] == "investigate":
                if gpu_limit and result["contract"]["timeout_seconds"] != gpu_limit:
                    raise ValueError(
                        "Long Plan must freeze the Slurm GPU allocation timeout"
                    )
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

    def __init__(self, study_root, delegate, executor, *, admission=None):
        self.root = Path(study_root)
        self.delegate, self.executor = delegate, executor
        self.admission = admission

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

    def run_one(self, *, deadline, allow_new=True):
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
            prepared = []
            for row in started:
                state = _read(lane / row["id"] / "study/state.json")
                if (
                    row["status"] != "withdrawn"
                    and state.get("iteration") == 0
                    and state.get("phase") == "done"
                    and state.get("status") == "ready"
                    and state.get("call_count") == 0
                    and state.get("calls") == {}
                    and state.get("active_attempt") is None
                    and state.get("round_admission") is None
                ):
                    prepared.append(row)
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
                [
                    r
                    for r in rows
                    if (r not in started or r in prepared)
                    and r["status"] != "withdrawn"
                ][:1]
                if allow_new
                and (
                    policy["max_runs"] is None
                    or len(started) - len(prepared) < policy["max_runs"]
                )
                else []
            )
            if not eligible:
                return None
            if time.time() >= deadline and not admitted_round(lane / eligible[0]["id"]):
                return None
            row = eligible[0]
            request, folder = row["request"], lane / row["id"]
            child = folder / "study"
            if row in prepared or not (child / "study.json").exists():
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
            if not (child / "study.json").exists():
                parent = _read(self.root / "study.json")
                harness_config = HarnessConfig(**parent["harness"])
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
            child_config = _read(child / "study.json")
            child_state = _read(child / "state.json")
            if child_config.get("long_training_run") is not None:
                if child_config["long_training_run"] != request:
                    raise ValueError("Frozen long-training child request changed")
            elif (
                child_state.get("iteration") == 0
                and child_state.get("phase") == "done"
                and child_state.get("status") == "ready"
                and child_state.get("call_count") == 0
                and child_state.get("calls") == {}
                and child_state.get("active_attempt") is None
                and child_state.get("round_admission") is None
            ):
                _write(
                    child / "study.json", child_config | {"long_training_run": request}
                )
            harness = _LongHarness(
                request,
                self.delegate,
                self.executor,
                deadline,
                folder,
                self.admission,
                HarnessConfig(**_read(child / "study.json")["harness"]).timeout_seconds,
            )
            study = Study(child, harness)
            # A failed/unknown Execute requires explicit reconciliation, as in short studies.
            state = study.run()
            iteration = child / "iterations/0001"
            if state["phase"] != "done" or not _published(child, iteration):
                return state
            review = _read(child / state["review_ref"])
            execute = _read(child / state["execution_ref"])
            output = child / "workspace/result.json"
            if _read(child / "study.json").get("long_training_run") is not None:
                review_context = _read(
                    (child / state["review_ref"]).parent / "context.json"
                )
                observed = review_context["long_training_observations"]
            else:
                observed = _read(output) if output.exists() else None
            result = {
                "id": request["id"],
                "source_iteration": request["source_iteration"],
                "candidate_id": request["candidate"]["id"],
                "recipe": request["recipe"],
                "evaluation": request["evaluation"],
                "review": review,
                "execute": execute,
                "result": observed,
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
