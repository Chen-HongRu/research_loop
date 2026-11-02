"""Independent, bounded ProTok long-run allocation and evidence bridge."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time

from integrations.protok.backend import CANDIDATE_FILES, ProTokError
from integrations.protok.continuation import _load_backend
from integrations.protok.research_backend import _source_identity, _launcher
from research_loop.protocol import EVALUATION, _validate, validate_response

RECIPE = {
    "total_steps": 10000,
    "validation_interval": 2000,
    "validation_split": "full",
    "initialization": "fresh",
    "seed": 42,
}
STEPS = [2000, 4000, 6000, 8000, 10000]
METRICS = {
    "full_ca_rmsd_10000_seed42": "ca_rmsd",
    "full_tm_score_10000_seed42": "tm_score",
    "full_lddt_10000_seed42": "lddt",
}
MAX_EVIDENCE_BYTES = 4 * 1024 * 1024


def _read(path):
    return json.loads(Path(path).read_text())


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _run_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"L[a-f0-9]{16}", value):
        raise ProTokError("Invalid long-run ID")
    return value


def _policy(control):
    value = _read(control / "long-training-policy.json")
    if (
        value.get("schema") != 1
        or not isinstance(value.get("authorization"), str)
        or not value["authorization"].strip()
        or type(value.get("gpu_seconds_per_run")) is not int
        or type(value.get("total_gpu_seconds")) is not int
        or not 60 <= value["gpu_seconds_per_run"] <= value["total_gpu_seconds"]
        or value["gpu_seconds_per_run"] % 60
        or not _finite(value.get("deadline"))
    ):
        raise ProTokError("Explicit bounded long-training policy required")
    return value


def _directory(root, run_id):
    return Path(root) / "study" / "long-runs" / _run_id(run_id)


def usage(control):
    """Read only small allocation ledgers; unresolved jobs remain charged/outstanding."""
    control = Path(control)
    used, outstanding = 0, []
    for path in sorted((control / "long-training-jobs").glob("L*/job.json")):
        job = _read(path)
        receipt_path = path.parent / "receipt.json"
        if not receipt_path.exists():
            outstanding.append(str(path.parent))
            continue
        receipt = _read(receipt_path)
        seconds = receipt.get("gpu_seconds")
        if (
            type(seconds) is not int
            or not 0 <= seconds <= job["allocated_seconds"]
            or receipt.get("source_identity") != job["source_identity"]
            or receipt.get("run_id") != job["run_id"]
            or receipt.get("status") not in {"completed", "failed"}
        ):
            raise ProTokError("Long-training accounting receipt is invalid")
        used += seconds
    return {"gpu_seconds": used, "outstanding": outstanding}


def _validate_request(request):
    _run_id(request.get("id"))
    if request.get("recipe") != RECIPE:
        raise ProTokError("Only fresh seed42 10k/full-validation recipe is authorized")
    _validate(EVALUATION, request["evaluation"], "long-training.evaluation")
    names = [item["name"] for item in request["evaluation"]["objectives"]]
    if (
        set(names) != set(METRICS)
        or len(names) != len(METRICS)
        or request["evaluation"]["constraints"]
    ):
        raise ProTokError(
            "Long evaluation requires exactly CA RMSD, TM-score and lDDT objectives"
        )
    directions = {
        item["name"]: item["direction"] for item in request["evaluation"]["objectives"]
    }
    if any(
        directions[name] != ("minimize" if key == "ca_rmsd" else "maximize")
        for name, key in METRICS.items()
    ):
        raise ProTokError("Long objective direction differs from canonical metrics")
    if not re.fullmatch(r"[a-f0-9]{40}", request.get("source_workspace_ref", "")):
        raise ProTokError("Frozen source Git revision required")


def _published_source(root, request):
    study = Path(root) / "study"
    if not re.fullmatch(r"[0-9]{4,}", request.get("source_iteration", "")):
        raise ProTokError("Published source iteration required")
    if (
        Path(request["source_project"]).resolve()
        != Path(_read(study / "study.json")["workspace"]).resolve()
    ):
        raise ProTokError("Long source project differs from its parent Study")
    iteration = study / "iterations" / request["source_iteration"]
    publication = _read(iteration / "publication.json")
    metadata = _read(iteration / "iteration.json")
    state = _read(study / "state.json")
    if (
        metadata.get("status") != "reviewed"
        or publication.get("metadata") != metadata
        or int(request["source_iteration"]) > state["iteration"]
        or (
            int(request["source_iteration"]) == state["iteration"]
            and state["phase"] != "done"
        )
        or metadata.get("workspace") != request["source_workspace"]
        or metadata.get("workspace_ref") != request["source_workspace_ref"]
    ):
        raise ProTokError("Long source is not the exact published parent workspace")
    candidates = _read(iteration / "candidates.json")["candidates"]
    matching = [
        row for row in candidates if row.get("id") == request["candidate"]["id"]
    ]
    if matching != [request["candidate"]]:
        raise ProTokError("Long candidate differs from published parent evidence")


def _scored_source(fixed, config, request, source):
    fingerprint = fixed.fingerprint(
        config, {name: (source / name).read_bytes() for name in CANDIDATE_FILES}
    )
    if fingerprint != request["candidate"]["artifact"]["version"]:
        raise ProTokError(
            "Nominated candidate bytes differ from its scored fingerprint"
        )
    records = Path(config["records_root"]).resolve()
    paths = {records / fingerprint / "state.json"}
    for name in request["candidate"].get("evidence", []):
        path = Path(name).resolve()
        if (
            path.name == "state.json"
            and path.parent.parent == records
            and re.fullmatch(
                re.escape(fingerprint) + r"(?:--[A-Za-z0-9_-]{1,64})?", path.parent.name
            )
        ):
            paths.add(path)
    for path in paths:
        if not path.exists():
            continue
        state, summary = _read(path), _read(path.parent / "summary.json")
        if (
            state.get("status") == "complete"
            and state.get("fingerprint") == fingerprint
            and summary.get("fingerprint") == fingerprint
            and summary.get("quality_score_eligible") is True
            and state.get("attempts")
            and all(
                attempt.get("terminal_state") == "COMPLETED"
                and attempt.get("exit_code") == "0:0"
                and _finite(attempt.get("gpu_seconds"))
                for attempt in state["attempts"]
            )
        ):
            return fingerprint
    raise ProTokError(
        "Nominated candidate lacks a settled eligible trusted short receipt"
    )


def _sandbox_inputs(source, config):
    fixed = _read(source / config["fixed_config"])
    paths = [
        config["cache_root"],
        fixed["teacher_checkpoint"],
        fixed["source_manifest"],
    ]
    if config.get("target_root"):
        paths.append(config["target_root"])
    if any(
        not isinstance(path, str)
        or not Path(path).is_absolute()
        or path.startswith(("/output", "/workspace"))
        for path in paths
    ):
        raise ProTokError("Canonical absolute read-only inputs required")
    return [{"path": path, "mount": path} for path in dict.fromkeys(paths)]


def _snapshot(backend, request, record, config, fixed):
    source = Path(request["source_workspace"]).resolve()
    revision = request["source_workspace_ref"]
    observed = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != revision:
        raise ProTokError("Nomination source Git revision changed")
    destination = record / "source"
    destination.mkdir()
    names = set(config["locked_files"]) | set(CANDIDATE_FILES)
    for name in sorted(names):
        original = source / name
        if (
            original.is_symlink()
            or not original.is_file()
            or original.stat().st_size > 4 * 1024 * 1024
        ):
            raise ProTokError("Unsafe nominated source")
        data = original.read_bytes()
        if name in CANDIDATE_FILES:
            committed = subprocess.check_output(
                ["git", "-C", str(source), "show", f"{revision}:{name}"]
            )
            if data != committed:
                raise ProTokError("Candidate bytes differ from nominated Git revision")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    identity, _ = _source_identity(destination, config)
    _scored_source(fixed, config, request, destination)
    shutil.copyfile(
        Path(__file__).with_name("long_train.py"), destination / "long_train.py"
    )
    # Trusted driver identity is part of the allocation, independently of search locks.
    identity = hashlib.sha256(
        (
            identity
            + hashlib.sha256((destination / "long_train.py").read_bytes()).hexdigest()
        ).encode()
    ).hexdigest()
    return destination, identity


def _batch(config, remote, seconds, inputs=None):
    launcher = _launcher(config, remote, {"entrypoint": "long_train.py"}, inputs or [])
    # Reuse the established empty-root/GPU launcher. Only this run's persistent
    # output and the frozen input-inventory file have different host locations.
    launcher = launcher.replace(repr(remote + "/output"), repr(remote + "/train"))
    launcher = launcher.replace(
        repr(remote + "/inputs.json"), repr(remote + "/code/inputs.json")
    )
    directives = {
        "partition": "acd_u",
        "nodes": 1,
        "ntasks": 1,
        "gres": "gpu:1",
        "cpus-per-task": config["cpus"],
        "mem": config["mem"],
        "time": f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:00",
        "output": remote + "/slurm.log",
        "error": remote + "/slurm.log",
        "chdir": remote + "/code",
    }
    if any(
        not re.fullmatch(r"[A-Za-z0-9_./:+-]+", str(value))
        for value in directives.values()
    ):
        raise ProTokError("Unsafe long-training Slurm directive")
    return "\n".join(
        [
            "#!/bin/bash",
            *[f"#SBATCH --{key}={value}" for key, value in directives.items()],
            "set -euo pipefail",
            "exec >" + shlex.quote(remote + "/run.log") + " 2>&1",
            shlex.quote(config["remote_python"]) + " - <<'LONG_TRAINING_LAUNCHER'",
            launcher,
            "LONG_TRAINING_LAUNCHER",
            "",
        ]
    )


def _curve(completion, request, identity, remote):
    if (
        completion.get("status") != "complete"
        or completion.get("run_id") != request["id"]
        or completion.get("protocol") != request["evaluation"]["id"]
        or completion.get("source_identity") != identity
        or completion.get("recipe") != RECIPE
        or completion.get("optimizer_step") != 10000
        or completion.get("scheduler_horizon") != 50000
    ):
        raise ProTokError("Long completion identity/recipe differs")
    points = completion.get("points", [])
    if len(points) != len(STEPS):
        raise ProTokError("Five complete full validations required")
    curve = []
    for step, point in zip(STEPS, points, strict=True):
        if (
            point.get("optimizer_step") != step
            or point.get("proteins") != 4491
            or point.get("scope") != "full"
        ):
            raise ProTokError("Long-validation step/split/coverage differs")
        values = {
            "ca_rmsd": point.get("ca_rmsd"),
            "tm_score": point.get("tm_score"),
            "lddt": point.get("lddt_ca"),
        }
        if not all(_finite(value) for value in values.values()):
            raise ProTokError("Nonfinite or missing full-validation metrics")
        if "loss" in point:
            if not _finite(point["loss"]):
                raise ProTokError("Nonfinite validation loss")
            values["loss"] = point["loss"]
        curve.append(
            {
                "step": step,
                "proteins": 4491,
                **values,
                "checkpoint": f"long-checkpoint:{request['id']}:{step}",
                "checkpoint_path": remote + f"/train/checkpoint-step-{step}.pt",
                "evidence": [
                    remote + f"/train/full-validation/step-{step}/perprotein.csv",
                    remote + f"/train/full-validation/step-{step}/eval.csv",
                ],
            }
        )
    expected = [
        {"optimizer_step": step, "path": remote + f"/train/checkpoint-step-{step}.pt"}
        for step in STEPS
    ]
    if completion.get("checkpoints") != expected:
        raise ProTokError("Long checkpoint references differ")
    return curve


def _collect(fixed, config, remote):
    script = f"from pathlib import Path; import base64; p=Path({remote + '/train/long-completion.json'!r}); n=p.stat().st_size; assert n<={MAX_EVIDENCE_BYTES}; print(base64.b64encode(p.read_bytes()).decode())"
    encoded = fixed.ssh(config, shlex.join([config["remote_python"], "-c", script]))
    data = base64.b64decode(encoded.strip(), validate=True)
    if len(data) > MAX_EVIDENCE_BYTES:
        raise ProTokError("Long completion evidence exceeds bound")
    return json.loads(data)


def _retained_result(job, result):
    request = job["request"]
    if (
        result.get("run_id") != request["id"]
        or result.get("recipe") != RECIPE
        or result.get("source_identity") != job["source_identity"]
        or result.get("source_candidate") != request["candidate"]
        or result.get("evaluation") != request["evaluation"]
        or result.get("remote") != job["remote"]
        or type(result.get("gpu_seconds")) is not int
        or not 0 <= result["gpu_seconds"] <= job["allocated_seconds"]
        or result.get("status") not in {"completed", "failed"}
    ):
        raise ProTokError("Retained long result differs from its allocation")
    if result["status"] == "completed":
        points = result.get("curve", [])
        if len(points) != len(STEPS):
            raise ProTokError("Retained long result lacks five full validations")
        for step, point in zip(STEPS, points, strict=True):
            if (
                point.get("step") != step
                or point.get("proteins") != 4491
                or point.get("checkpoint") != f"long-checkpoint:{request['id']}:{step}"
                or point.get("checkpoint_path")
                != job["remote"] + f"/train/checkpoint-step-{step}.pt"
                or not all(_finite(point.get(key)) for key in METRICS.values())
            ):
                raise ProTokError(
                    "Retained long validation differs from its allocation"
                )
    return result


def _envelope(request, result_path, result):
    artifact = {"path": str(result_path), "version": result["source_identity"]}
    measurements = []
    if result["status"] == "completed":
        final = result["curve"][-1]
        measurements = [
            {
                "candidate_id": request["candidate"]["id"],
                "artifact": artifact,
                "evaluation_id": request["evaluation"]["id"],
                "comparison_scope": request["evaluation"]["comparison_scope"],
                "metrics": [
                    {"name": item["name"], "value": final[METRICS[item["name"]]]}
                    for item in request["evaluation"]["objectives"]
                ],
                "evidence": [str(result_path), *final["evidence"]],
            }
        ]
    return validate_response(
        "execute",
        {
            "outcome": "completed" if measurements else "incomplete",
            "markdown": "Fresh seed42 long training: "
            + result["status"]
            + ("; " + result["error"] if result.get("error") else ""),
            "artifacts": [artifact],
            "measurements": measurements,
        },
    )


def execute(
    backend,
    root: Path,
    request: dict,
    workspace: Path,
    *,
    deadline: float,
    recover: bool = False,
):
    """Run/reconcile one independently receipted allocation; never replace uncertain effects."""
    _validate_request(request)
    root, workspace = Path(root).resolve(), Path(workspace).resolve()
    control = root / "control"
    control.mkdir(parents=True, exist_ok=True)
    record = control / "long-training-jobs" / request["id"]
    with (control / "long-training.lock").open("a") as lock:
        # This lane's lock deliberately does not block search/research jobs.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = backend._config()
        _published_source(root, request)
        fixed, _ = _load_backend(
            backend.project,
            {
                "evaluator_sha256": config["locked_files"][
                    "scripts/autoresearch_se3_eval.py"
                ]
            },
        )
        policy = _policy(control)
        owner = _read(control / "ownership.json")
        if (
            owner.get("owner_token") != backend.owner_token
            or owner.get("handoff_acknowledged") is not True
            or not _finite(owner.get("expires_at"))
            or not _finite(deadline)
        ):
            raise ProTokError("Active parent ownership required")
        effective = min(deadline, policy["deadline"], owner["expires_at"])
        off = backend.project / ".autoresearch-off"
        off_value = _read(off) if off.exists() else None
        stat = off.stat() if off.exists() else None
        off_stat = (
            {name: getattr(stat, "st_" + name) for name in ("mtime_ns", "size", "ino")}
            if stat
            else None
        )
        continuation_path = control / "continuation.json"
        continuation = _read(continuation_path) if continuation_path.exists() else None
        if off.exists() and (
            not continuation
            or continuation.get("prior_off") != off_value
            or continuation.get("prior_off_stat") != off_stat
        ):
            raise ProTokError("Unacknowledged project stop marker")
        existing = (
            _read(record / "job.json") if (record / "job.json").exists() else None
        )
        if existing:
            if (
                existing["request"] != request
                or existing["policy"] != policy
                or existing["owner"] != owner
                or existing["deadline"] != effective
                or existing["config"] != config
            ):
                raise ProTokError(
                    "Retained long allocation identity/authorization changed"
                )
            if (record / "receipt.json").exists():
                result = _retained_result(existing, _read(record / "receipt.json"))
                _write(workspace / "result.json", result)
                return _envelope(request, workspace / "result.json", result)
            if not recover:
                raise ProTokError(
                    "Long allocation has retained effects; recover its owned job"
                )
            state = _read(record / "state.json")
            if state["attempts"][0]["status"] == "prepared":
                raise ProTokError(
                    "Retained prepared long allocation cannot be resubmitted"
                )
        else:
            if recover:
                raise ProTokError("No long allocation to recover")
            spent = usage(control)
            allocated = policy["gpu_seconds_per_run"]
            if (
                spent["outstanding"]
                or spent["gpu_seconds"] + allocated > policy["total_gpu_seconds"]
                or effective - time.time() < allocated
                or (control / "STOP").exists()
            ):
                raise ProTokError(
                    "Long allocation does not fit settled authorization/deadline"
                )
            record.mkdir(parents=True)
            source, identity = _snapshot(backend, request, record, config, fixed)
            remote = config["remote_root"] + "/long-training/" + request["id"]
            launcher_config = {
                **{
                    key: config[key]
                    for key in (
                        "fixed_config",
                        "candidate_config",
                        "cache_root",
                        "target_root",
                        "workers",
                    )
                },
                "source": "/workspace",
                "output": "/output",
                "output_reference": remote + "/train",
                "run_id": request["id"],
                "source_identity": identity,
                "protocol": request["evaluation"]["id"],
                "recipe": RECIPE,
            }
            inputs = _sandbox_inputs(source, config)
            _write(
                source / "inputs.json", {"inputs": inputs, "purpose": "long-training"}
            )
            _write(source / "long-config.json", launcher_config)
            (source / "autoresearch-se3.sbatch").write_text(
                _batch(config, remote, allocated, inputs)
            )
            existing = {
                "run_id": request["id"],
                "request": request,
                "source_identity": identity,
                "policy": policy,
                "owner": owner,
                "deadline": effective,
                "config": config,
                "off": off_value,
                "off_stat": off_stat,
                "continuation": continuation,
                "allocated_seconds": allocated,
                "remote": remote,
            }
            state = {
                "attempts": [
                    {
                        "seed": 42,
                        "status": "prepared",
                        "remote": remote,
                        "budget_seconds": allocated,
                    }
                ]
            }
            _write(record / "job.json", existing)
            _write(record / "state.json", state)
        attempt = state["attempts"][0]
        original_ssh = fixed.ssh
        watching = not recover

        def live():
            current_stat = off.stat() if off.exists() else None
            current = (
                {
                    name: getattr(current_stat, "st_" + name)
                    for name in ("mtime_ns", "size", "ino")
                }
                if current_stat
                else None
            )
            if (
                (control / "STOP").exists()
                or time.time() >= effective
                or _read(control / "ownership.json") != owner
                or _policy(control) != policy
                or current != existing["off_stat"]
                or (_read(off) if off.exists() else None) != existing["off"]
                or (_read(continuation_path) if continuation_path.exists() else None)
                != existing["continuation"]
            ):
                raise KeyboardInterrupt(
                    "Long training stopped or authorization changed"
                )

        def ssh(current, command, **kwargs):
            if watching and re.fullmatch(r"squeue -h -j [1-9][0-9]* -o %T", command):
                live()
            return original_ssh(current, command, **kwargs)

        fixed.ssh = ssh
        error, curve = None, []
        try:
            if not recover:
                live()
                remote = existing["remote"]
                fixed.ssh(
                    config,
                    "mkdir -p "
                    + shlex.quote(str(Path(remote).parent))
                    + " && mkdir "
                    + shlex.quote(remote)
                    + " && mkdir "
                    + shlex.quote(remote + "/code")
                    + " && mkdir "
                    + shlex.quote(remote + "/train"),
                )
                fixed.run(
                    [
                        "rsync",
                        "-a",
                        "-e",
                        "ssh -o BatchMode=yes -o ClearAllForwardings=yes",
                        str(record / "source") + "/",
                        config["ssh_host"] + ":" + remote + "/code/",
                    ],
                    timeout=600,
                )
                live()
                if effective - time.time() < existing["allocated_seconds"]:
                    raise ProTokError("Staging exhausted long allocation window")
                fixed.submit(config, record, state, attempt)
            fixed.wait_for_job(
                config,
                {"deadline": min(effective, time.time()) if recover else effective},
                record,
                state,
                attempt,
            )
            if (
                attempt.get("terminal_state") != "COMPLETED"
                or attempt.get("exit_code") != "0:0"
            ):
                raise ProTokError("Long allocation did not finish successfully")
            curve = _curve(
                _collect(fixed, config, existing["remote"]),
                request,
                existing["source_identity"],
                existing["remote"],
            )
        except BaseException as failure:
            error = f"{type(failure).__name__}: {failure}"
            watching = False
            if attempt["status"] in {"submitting", "running"}:
                fixed.cancel_interrupted(config, record, state)
                try:
                    fixed.wait_for_job(
                        config, {"deadline": time.time()}, record, state, attempt
                    )
                except BaseException as failure:
                    error += f"; reconciliation: {type(failure).__name__}: {failure}"
        finally:
            fixed.ssh = original_ssh
        settled = all(
            key in attempt
            for key in ("gpu_seconds", "terminal_state", "exit_code", "owned_job_id")
        )
        unsubmitted = attempt["status"] == "prepared"
        seconds = attempt.get("gpu_seconds", 0 if unsubmitted else None)
        if settled and (
            type(seconds) is not int
            or not 0 <= seconds <= existing["allocated_seconds"]
        ):
            settled = False
            error = (error or "") + "; invalid authoritative accounting"
        result = {
            "status": "completed"
            if settled and error is None
            else "failed"
            if settled or unsubmitted
            else "blocked",
            "run_id": request["id"],
            "recipe": RECIPE,
            "source_identity": existing["source_identity"],
            "source_workspace_ref": request["source_workspace_ref"],
            "source_candidate": request["candidate"],
            "evaluation": request["evaluation"],
            "gpu_seconds": seconds if settled or unsubmitted else None,
            "allocated_seconds": existing["allocated_seconds"],
            "owned_job_id": attempt.get("owned_job_id"),
            "terminal_state": attempt.get("terminal_state"),
            "exit_code": attempt.get("exit_code"),
            "remote": existing["remote"],
            "curve": curve if error is None else [],
            "error": error,
        }
        _write(
            record
            / ("receipt.json" if result["status"] != "blocked" else "blocked.json"),
            result,
        )
        _write(workspace / "result.json", result)
        if result["status"] == "blocked":
            raise ProTokError(
                "Long allocation effects/accounting unresolved; recover the same owned job: "
                + (error or "unknown effects")
            )
        return _envelope(request, workspace / "result.json", result)


def resolve_checkpoint(root, run_id, step):
    """Resolve only independently reviewed completed long-run evidence."""
    if type(step) is not int or step not in STEPS:
        raise ProTokError("Unsupported long checkpoint step")
    run = _directory(root, run_id)
    allocation = Path(root) / "control" / "long-training-jobs" / run_id
    receipt = _retained_result(
        _read(allocation / "job.json"), _read(allocation / "receipt.json")
    )
    wrapper = _read(run / "review.json")
    child = run / "study"
    publication_path = child / "iterations/0001/publication.json"
    if wrapper.get("publication_ref") != str(publication_path):
        raise ProTokError("Long Review publication does not belong to its child Study")
    publication = _read(publication_path)
    metadata = _read(publication_path.parent / "iteration.json")
    state = _read(child / "state.json")
    if (
        state.get("phase") != "done"
        or state.get("iteration") != 1
        or metadata.get("status") != "reviewed"
        or publication.get("metadata") != metadata
    ):
        raise ProTokError("Long child Review has not been published")
    review = _read(child / state["review_ref"])
    execute = _read(child / state["execution_ref"])
    candidate_id = receipt["source_candidate"]["id"]
    if (
        wrapper.get("id") != run_id
        or wrapper.get("candidate_id") != candidate_id
        or wrapper.get("review") != review
        or wrapper.get("execute") != execute
        or receipt.get("status") != "completed"
        or receipt.get("run_id") != run_id
        or not any(
            row.get("candidate_id") == candidate_id
            and row.get("valid") is True
            and row.get("verified") is True
            for row in review.get("candidate_reviews", [])
        )
    ):
        raise ProTokError(
            "Completed long checkpoint needs independent accepting Review"
        )
    measurements = [
        row
        for row in execute.get("measurements", [])
        if row.get("candidate_id") == candidate_id
    ]
    if len(measurements) != 1:
        raise ProTokError("Accepted long candidate measurement is missing or ambiguous")
    measurement = measurements[0]
    final = receipt["curve"][-1]
    expected = {name: final[key] for name, key in METRICS.items()}
    observed = {row["name"]: row["value"] for row in measurement["metrics"]}
    if (
        measurement["artifact"]["version"] != receipt["source_identity"]
        or measurement.get("evaluation_id") != receipt["evaluation"]["id"]
        or measurement.get("comparison_scope")
        != receipt["evaluation"]["comparison_scope"]
        or observed != expected
        or len(measurement["metrics"]) != len(expected)
    ):
        raise ProTokError("Reviewed long output differs from allocation evidence")
    point = next((row for row in receipt["curve"] if row["step"] == step), None)
    if point is None:
        raise ProTokError("Checkpoint not present in accepted full-validation curve")
    return {
        "source": f"long-checkpoint:{run_id}:{step}",
        "path": point["checkpoint_path"],
        "role": "model",
        "source_identity": receipt["source_identity"],
        "optimizer_step": step,
    }
