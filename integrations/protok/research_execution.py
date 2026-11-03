"""Unscored research contracts, frozen source and shared-budget evidence."""

from __future__ import annotations

import json
from pathlib import Path
import stat
import time

from integrations.protok.backend import ProTokError


def read(path):
    return json.loads(Path(path).read_text())


def policy(root):
    path = Path(root) / "control/research-policy.json"
    if not path.exists():
        return None
    value = read(path)
    if (
        value.get("version") != 1
        or not isinstance(value.get("authorization"), str)
        or not value["authorization"].strip()
        or type(value.get("max_time_limit_seconds")) is not int
        or not 60 <= value["max_time_limit_seconds"] <= 1800
    ):
        raise ProTokError("Invalid research-script authorization")
    return value


def research_usage(control):
    """Small controller receipts only; never read computational inputs/outputs."""
    used, outstanding, overruns = 0, [], []
    for path in sorted((Path(control) / "research-jobs").glob("*/job.json")):
        job = read(path)
        receipt = path.parent / "receipt.json"
        if not receipt.exists():
            outstanding.append(str(path.parent))
            continue
        settled = read(receipt)
        seconds = settled.get("gpu_seconds")
        if (
            settled.get("purpose") != "research"
            or settled.get("quality_score_eligible") is not False
            or settled.get("status") not in {"completed", "failed"}
            or type(seconds) is not int
            or seconds < 0
            or type(job.get("allocated_seconds")) is not int
            or job["allocated_seconds"] < 0
            or settled.get("allocated_seconds", job["allocated_seconds"])
            != job["allocated_seconds"]
            or settled.get("execution_identity") != job.get("execution_identity")
            or settled.get("source_identity") != job["source_identity"]
        ):
            raise ProTokError("Research accounting is unresolved or inconsistent")
        used += seconds
        if seconds > job["allocated_seconds"]:
            overruns.append(
                {
                    "iteration": path.parent.name,
                    "allocated_seconds": job["allocated_seconds"],
                    "gpu_seconds": seconds,
                    "overrun_seconds": seconds - job["allocated_seconds"],
                }
            )
    return {"gpu_seconds": used, "outstanding": outstanding, "overruns": overruns}


def research_files(workspace):
    """Read only bounded code/configuration authored for this investigation."""
    workspace = Path(workspace)
    directory = workspace / "research"
    if not directory.exists() and not directory.is_symlink():
        return {}
    result, total = {}, 0
    pending = [directory]
    while pending:
        path = pending.pop()
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            if len(pending) + len(result) > 256:
                raise ProTokError("Research source has too many entries")
            pending.extend(path.iterdir())
            continue
        if (
            not stat.S_ISREG(mode)
            or path.suffix
            not in {".py", ".json", ".md", ".txt", ".toml", ".yaml", ".yml", ".csv"}
            or path.stat().st_size > 1024 * 1024
            or len(result) >= 128
        ):
            raise ProTokError(
                "Research files must be small regular source/configuration files"
            )
        total += path.stat().st_size
        if total > 4 * 1024 * 1024:
            raise ProTokError(
                "Research source exceeds 4 MiB; keep outputs outside source"
            )
        result[path.relative_to(workspace).as_posix()] = path.read_bytes()
    return result


def frozen_contract(root, iteration, *, required=True):
    root = Path(root)
    state = read(root / "study/state.json")
    if f"{state['iteration']:04d}" != iteration or not state.get("plan_ref"):
        raise ProTokError("Research execution requires this iteration's frozen Plan")
    reference = Path(state["plan_ref"])
    if reference.is_absolute() or ".." in reference.parts:
        raise ProTokError("Plan reference leaves the Study")
    plan = read(root / "study" / reference)
    execution = plan.get("execution")
    if execution is None and not required:
        return None
    if plan.get("decision") != "investigate" or not isinstance(execution, dict):
        raise ProTokError("No admitted research contract")
    return plan


def execute_script(harness, workspace, iteration, stop, deadline):
    from integrations.protok import run
    from integrations.protok.research_backend import run_script

    root, workspace = harness.root, Path(workspace)
    authorization = policy(root)
    if authorization is None:
        raise ProTokError("This Study has not authorized research scripts")
    contract = frozen_contract(root, iteration)
    execution = contract["execution"]
    if execution["mode"] != "script":
        raise ProTokError("Research submission differs from the frozen execution mode")
    if execution["time_limit_seconds"] > authorization["max_time_limit_seconds"]:
        raise ProTokError("Script exceeds the admitted per-allocation limit")
    if stop.is_set() or time.time() >= deadline:
        raise ProTokError("Execution ended before research admission")
    run.admission(root, harness.backend)
    baseline = read(root / "study/study.json")["baseline"]
    run.check_scope(workspace, baseline, allow_research=True)
    contents = {**run.candidate_bytes(workspace), **research_files(workspace)}
    if execution["entrypoint"] not in contents:
        raise ProTokError("Declared research entrypoint is missing")
    identity = run.signatures(contents)
    folder = root / "control/research-sources" / iteration
    record_path = folder / "record.json"
    if record_path.exists():
        saved = read(record_path)
        if saved["contract"] != contract or saved["source_signatures"] != identity:
            raise ProTokError("Cannot replace a submitted research investigation")
        return completed_script(root, workspace, iteration)
    folder.mkdir(parents=True, exist_ok=False)
    run.write(record_path, {"contract": contract, "source_signatures": identity})
    commit, _ = run.freeze_source(
        harness.backend.project,
        workspace,
        folder / "source",
        baseline,
        allow_research=True,
        include_research=True,
    )
    run.check_fixed_candidate(root, workspace, iteration, folder / "source")
    receipt = run_script(
        harness.backend,
        root=root,
        iteration=iteration,
        source=folder / "source",
        execution=execution,
        deadline=deadline,
        cancel_event=stop,
    )
    if receipt.get("status") not in {"completed", "failed"}:
        raise ProTokError(
            "Research execution has unresolved effects; reconcile the same job"
        )
    result = {
        **receipt,
        "source_commit": commit,
        "frozen_source": str(folder / "source"),
        "contract": contract,
        "artifact": {
            "path": str(folder / "result.json"),
            "version": receipt["execution_identity"],
        },
    }
    run.write(folder / "result.json", result)
    return completed_script(root, workspace, iteration)


def completed_script(root, workspace, iteration):
    from integrations.protok import run

    root, workspace = Path(root), Path(workspace)
    folder = root / "control/research-sources" / iteration
    run.check_fixed_candidate(root, workspace, iteration, folder / "source")
    saved = read(folder / "record.json")
    result = read(folder / "result.json")
    receipt = read(root / "control/research-jobs" / iteration / "receipt.json")
    if (
        result.get("status") not in {"completed", "failed"}
        or result.get("purpose") != "research"
        or result.get("quality_score_eligible") is not False
        or any(result.get(key) != value for key, value in receipt.items())
        or result.get("contract") != saved["contract"]
        or frozen_contract(root, iteration) != saved["contract"]
        or research_usage(root / "control")["outstanding"]
        or result["artifact"]
        != {
            "path": str(folder / "result.json"),
            "version": receipt["execution_identity"],
        }
        or run.git(folder / "source", "rev-parse", "HEAD") != result["source_commit"]
    ):
        raise ProTokError("Research result differs from its frozen, settled execution")
    for path in (workspace, folder / "source"):
        run.check_scope(
            path, read(root / "study/study.json")["baseline"], allow_research=True
        )
        if (
            run.signatures({**run.candidate_bytes(path), **research_files(path)})
            != saved["source_signatures"]
        ):
            raise ProTokError("Research code changed after submission")
    if (workspace / "evaluation-submission.json").exists() and read(
        workspace / "evaluation-submission.json"
    ) != {"action": "research"}:
        raise ProTokError("Research submission receipt changed")
    return result


def instructions(root):
    if policy(root) is None:
        return ""
    return """
User-authorized research execution amendment (applies to future frozen Plans;
replaces earlier prohibitions on additional research scripts, local measurements
and checkpoint diagnostics). You may create arbitrary Python experiments under
research/, plus small code/configuration/documentation files there. These may
perform inference, gradient/Jacobian probes, simulations, counterexamples, short
controlled updates, new local benchmarks or other computations; training is not
required. Keep the three candidate files as the only editable model implementation.
Protected data/scorer and the official ranking protocol remain fixed.
Plan execution chooses fixed_evaluation or script. Submit using
the same controller API, --action evaluate or --action research respectively.
The controller executes a frozen script and its imported research/candidate code;
do not change those files after submission. One script may perform multiple paired
computations within one shared allocation. Do not SSH, install dependencies or
launch compute by another path. The Aliyun controller has no Torch.
The verified library inventory is supplied by the operator execution-environment
context. Supplemental research libraries, when configured, are described by the
retained /environment.json inside the sandbox and mounted read-only at /research-libs.
Do not assume unlisted libraries are available or auto-install them; report an unmet
prerequisite when a required library is unavailable.
The available compute profile is the existing H100 debug allocation, gpu=true,
8 CPU / 96 GiB, 60..1800 seconds in whole minutes. Even CPU calculations in this
profile consume and report GPU allocation seconds from the original shared budget.
Request the smallest useful limit and leave polling/queue time within Plan timeout.
Script inputs must be declared by name/source: sources hidden_cache, targets,
teacher, checkpoint:<published iteration>:<step>, or independently reviewed
long-checkpoint:<run ID>:<2000|4000|6000|8000|10000>. Long checkpoints retain their
separate full-validation protocol; inspect their published Review and exact source.
Use development or exploratory
data only; final-only evidence cannot feed adaptive search. No full data/cache copy
or validation scan. Load only samples/checkpoints needed for actual computation.
The remote manifest /inputs.json describes resolved read-only paths under /inputs/;
code is at /workspace, working/output directory is /output. Write only declared
relative output files there (small JSON/CSV/Markdown/figures; 16 MiB total).
Publish completed subtests incrementally: after each completed configuration or
measurement, atomically replace a declared small JSON file with completed
observations and pending/missing items. Retain conditions, warmup, measured steps,
timing boundaries and units; synchronize asynchronous GPU work for GPU timing.
Write an initial empty/pending record before expensive setup, and update at useful
boundaries. Do not wait until the entire experiment succeeds to write results.
A time limit does not invalidate earlier complete observations; explain exactly
which comparisons remain incomplete. Never invent observations still in memory
or treat an interrupted measurement or missing configuration as zero.
Live structured observations use the controller-supplied standalone helper:
    from research_loop_events import emit
    emit("train/loss", float(loss), step=step, unit="loss",
         tags=["dashboard", "training"], context={"split": "development"})
    emit("diagnostic/gradient", statistics, step=step, tags=["diagnostic"])
Names, values, units, steps, tags and small condition objects are chosen by Plan/Execute.
Without an explicit dashboard tag, an event stays outside the default live panel.
Every tag remains available to Review; tags are presentation labels, not access rights
or evidence verification. Data roles remain development/exploratory only. Do not log
final-only evaluation data, dataset contents, hidden caches or checkpoint tensors.
Each event is a small JSON record (8192-byte maximum); nonfinite floating observations
are represented explicitly as nonnumeric JSON, never as valid numeric measurements.
The helper appends to the reserved controller stream at RESEARCH_LOOP_EVENT_FILE;
do not truncate/replace/edit it, and do not declare it as a scientific output artifact.
Publish at useful completed-observation boundaries; recording partial progress is
allowed but must be labeled in its context. The controller incrementally synchronizes
the complete records; this neither extends compute nor starts additional allocations.
Keep declared final/partial artifacts as before: live telemetry is additional evidence,
not a substitute for complete fixed validation or a source of automatic scores.
Import protected project modules from /workspace. See the actual trusted result
for locally collected output/evidence paths. No checkpoint is promised to exist:
missing input is a failed prerequisite, never permission to substitute another.
For script results the controller constructs measurements=[] and exactly the returned
artifact; it sets outcome=completed only when the declared computation succeeded,
otherwise incomplete with its settled failure evidence. Your final response contains
only the execution report requested by the adapter. Independent Review may learn from a negative or
inconclusive result without a formal score. Do not turn script outputs into official
RMSD measurements. New local benchmarks have their own frozen definition/data role;
they are evidence in the artifact and never replace the official Frontier.
Execution success does not certify a scientific conclusion. Review must match each
claim to actual output, code and predeclared criteria, separating intervention checks,
mechanism observations and performance generalization. Report missing discriminating
evidence as insufficient. Post-hoc explanations remain hypotheses for a later test.
"""
