"""Retained candidate repair, unscored native smoke, then a fresh formal allocation."""

from __future__ import annotations

import json
import math
import os
import shutil
import threading
import time
from pathlib import Path

from research_loop.core import BudgetExhausted
from research_loop.debug_budget import DebugBudget
from research_loop.harness import (
    HarnessError,
    completed_response,
    publish_completed_response,
)
from research_loop.submission import RECEIPT, STAGING

from .backend import CANDIDATE_FILES, METRIC, PROTOCOL, allocation_record
from .agent_output import REPORT_SCHEMA, report_response


REPAIR = """CONTROLLER DEBUG REPAIR ONLY. This invocation repairs an already failed
implementation under the SAME frozen scientific Plan. Earlier Execute instructions
to request evaluation do not apply to this repair invocation. Edit only the three
candidate files. Fix the supplied actual failure; preserve the scientific intervention,
data, teacher, scorer, native gradient/finite gates and fixed protocol. No network,
SSH, training, GPU launch, evaluation request, interface file, gate bypass, dependency
installation or change outside candidate scope. The controller may run smoke and
formal evaluation only when separately authorized and within the existing budget.
Return only markdown with a concise account of the implementation repair. The
controller supplies incomplete status and empty artifacts/measurements.
"""


class _RepairDelegate:
    def __init__(self, delegate, root, deadline):
        self.delegate = delegate
        self.root, self.deadline = Path(root), deadline
        self.diagnostic_secrets = getattr(delegate, "diagnostic_secrets", ())

    def run(self, **kwargs):
        remaining = int(self.deadline - time.time())
        if remaining <= 0 or (self.root / "control/STOP").exists():
            raise HarnessError(
                "Repair Agent cannot launch after STOP or original deadline"
            )
        kwargs["timeout_seconds"] = min(
            kwargs.get("timeout_seconds", remaining), remaining
        )
        kwargs["prompt"] = REPAIR + "\n" + kwargs["prompt"] + "\n" + REPAIR
        # Repair status and absence of measurements are controller-owned facts.
        from research_loop.schema import omit_fields

        kwargs["schema"] = omit_fields(REPORT_SCHEMA, "outcome")
        kwargs["prompt"] += (
            "\nFinal output overrides the ordinary Execute envelope: return only "
            "markdown describing the repair. The controller supplies incomplete "
            "status and empty artifacts/measurements; do not request evaluation.\n"
        )
        outer = Path(kwargs["run_dir"])
        agent = outer / "agent"
        agent.mkdir()
        shutil.copyfile(outer / "context.json", agent / "context.json")
        kwargs["run_dir"] = agent
        report = self.delegate.run(**kwargs)
        from research_loop.protocol import validate_response

        validate_response("execute", report, schema=kwargs["schema"])
        result = report_response(
            {"outcome": "incomplete", "markdown": report["markdown"]}
        )
        publish_completed_response(outer, result)
        return result


def _no_interface(workspace, expected_submission=None):
    for name in ("evaluation-request.json", "evaluation-result.json", STAGING):
        path = workspace / name
        if path.exists() or path.is_symlink():
            raise HarnessError(
                "Debug repair cannot retain evaluation interface effects"
            )
    receipt = workspace / RECEIPT
    if (
        receipt.is_symlink()
        or (
            receipt.exists()
            and (
                expected_submission is None
                or json.loads(receipt.read_text()) != expected_submission
            )
        )
        or (expected_submission is not None and not receipt.exists())
    ):
        raise HarnessError("Debug repair changed the certified original submission")


def _stopped(attempt, read):
    for path in (attempt / "process.json", attempt / "agent/process.json"):
        if not path.exists():
            continue
        process = read(path)
        pid = process.get("pid")
        if process.get("status") != "running" or type(pid) is not int or pid <= 0:
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass
        raise HarnessError("Partial repair still has an active or unknown Agent CLI")


def recover_execution(harness, failure, *, workspace, run_dir, context, invoke):
    # Local import avoids coupling the generic controller to this adapter.
    from .run import (
        candidate_bytes,
        check_fixed_candidate,
        certify_candidate_failure,
        check_scope,
        freeze_source,
        git,
        immutable_record,
        read,
        research_policy,
        signatures,
        timed_out_job,
        write,
    )

    root, workspace, run_dir = (
        Path(harness.root).resolve(),
        Path(workspace).resolve(),
        Path(run_dir).resolve(),
    )
    iteration = workspace.parent.name
    if (
        failure.get("iteration") != iteration
        or failure.get("kind") != "candidate_rejected"
    ):
        raise HarnessError(
            "Debug requires an explicitly certified investigation failure"
        )
    fixed = check_fixed_candidate(root, workspace, iteration)
    if fixed is not None:
        trusted = certify_candidate_failure(root, harness.backend, workspace, iteration)
        if failure != trusted or failure["source_signatures"] != fixed:
            raise HarnessError("Fixed-candidate failure differs from its original job")
    from .run import debug_policy

    policy = (
        debug_policy(root) if (root / "control/debug-policy.json").exists() else None
    )
    debug = root / "control/debug" / iteration
    debug.mkdir(parents=True, exist_ok=True)
    if policy is None and (debug / "debug-budget.json").exists():
        raise HarnessError("GPU recovery ledger requires its original authorization")
    budget = (
        DebugBudget(debug, scope_id=iteration, limit_gpu_seconds=1800)
        if policy is not None
        else None
    )

    def budget_status():
        return (
            budget.status()
            if budget is not None
            else {
                "limit_gpu_seconds": 0,
                "used_gpu_seconds": 0,
                "remaining_gpu_seconds": 0,
                "active_attempt": None,
                "breached": False,
                "attempts": {},
            }
        )

    baseline = read(root / "study/study.json")["baseline"]
    allow_research = research_policy(root) is not None
    check_scope(workspace, baseline, allow_research=allow_research)
    _no_interface(workspace, failure.get("submission"))
    immutable_record(debug / "failure.json", failure)
    flow_path = debug / "flow.json"
    if flow_path.exists():
        state = read(flow_path)
        if state["policy"] != policy or state["deadline"] != harness.deadline:
            raise HarnessError(
                "Debug policy/deadline cannot change or reset on recovery"
            )
    else:
        current = signatures(candidate_bytes(workspace))
        if current != failure["source_signatures"]:
            raise HarnessError(
                "Initial debug source differs from certified failed source"
            )
        state = {
            "schema": 1,
            "iteration": iteration,
            "policy": policy,
            "deadline": harness.deadline,
            "phase": "repair",
            "revision": 1,
            "repair_try": 1,
            "formal": 0,
            "failed_signatures": current,
            "failures": [failure],
        }
        write(flow_path, state)

    if fixed is not None:
        if (
            state["failed_signatures"] != fixed
            or state.get("source_signatures", fixed) != fixed
            or state["failures"] != [failure]
        ):
            raise HarnessError("Fixed-candidate recovery retained a modified candidate")
        for attempt in run_dir.glob("repairs/*/attempt-*"):
            _stopped(attempt, read)
            if completed_response(attempt) is None:
                raise HarnessError(
                    "Interrupted repair requires explicit reconciliation"
                )

    from .round_admission import execution_deadline

    if fixed is not None:
        wall_deadline = 0  # No repair or new allocation is authorized for this task.
    elif policy is None:
        original_deadline = failure.get("execution_deadline")
        if type(original_deadline) not in (int, float) or not math.isfinite(
            original_deadline
        ):
            original_deadline = 0
        wall_deadline = min(state["deadline"], original_deadline)
    else:
        wall_deadline = execution_deadline(
            root,
            state["deadline"],
            3600,
            owner_token=harness.backend.owner_token,
            attempt=f"debug-flow/{iteration}",
        )

    def save():
        write(flow_path, state)

    def finish(reason, result=None):
        check_scope(workspace, baseline, allow_research=allow_research)
        _no_interface(workspace, failure.get("submission"))
        accounting = budget_status()
        if accounting["active_attempt"] is not None or accounting["breached"]:
            raise HarnessError("Cannot finish with unresolved debug accounting")
        effects = harness.backend.inspect()
        if (
            effects["outstanding"]
            or effects.get("debug_outstanding")
            or effects.get("research_outstanding")
        ):
            raise HarnessError("Cannot finish with unresolved formal effects")
        if result is None:
            result = {
                "outcome": "incomplete",
                "markdown": "Controller recovery ended without a formal measurement: "
                + reason
                + ". Original failure and debug receipts are retained at "
                + str(debug)
                + ".",
                "artifacts": [],
                "measurements": [],
            }
        decision = {
            "schema": 1,
            "status": "completed" if result["outcome"] == "completed" else "failed",
            "reason": reason,
            "response": result,
            "source_signatures": signatures(candidate_bytes(workspace)),
            "failures": state["failures"],
            "budget": budget_status(),
        }
        immutable_record(debug / "decision.json", decision)
        state["phase"] = "done"
        save()
        return result

    def freeze(job):
        job.mkdir(parents=True, exist_ok=True)
        source, temporary = job / "source", job / "source.preparing"
        if not source.exists():
            if temporary.exists():
                shutil.rmtree(temporary)
            freeze_source(
                harness.backend.project,
                workspace,
                temporary,
                baseline,
                allow_research=allow_research,
            )
            temporary.rename(source)
        expected = signatures(candidate_bytes(workspace))
        if signatures(candidate_bytes(source)) != expected:
            raise HarnessError("Frozen recovery source differs from retained candidate")
        prepared = harness.backend.prepare_config(source, job / "runtime-config.json")
        return git(source, "rev-parse", "HEAD"), expected, prepared

    while True:
        check_scope(workspace, baseline, allow_research=allow_research)
        _no_interface(workspace, failure.get("submission"))
        pending = (state["phase"] == "smoke" and state.get("smoke_submitted")) or (
            state["phase"] == "formal" and state.get("formal_submitted")
        )
        if (
            (root / "control/STOP").exists()
            and not pending
            and state["phase"] != "done"
            and not (debug / "decision.json").exists()
        ):
            raise HarnessError("Debug recovery is paused by control/STOP")
        if (
            state["phase"] not in {"repair", "done"}
            and signatures(candidate_bytes(workspace)) != state["source_signatures"]
        ):
            raise HarnessError("Candidate changed outside the retained repair call")
        if state["phase"] == "done" or (debug / "decision.json").exists():
            decision = read(debug / "decision.json")
            accounting = budget_status()
            if (
                accounting["active_attempt"] is not None
                or accounting["breached"]
                or harness.backend.inspect()["outstanding"]
            ):
                raise HarnessError(
                    "Recovery decision has unresolved or breached accounting"
                )
            if signatures(candidate_bytes(workspace)) != decision["source_signatures"]:
                raise HarnessError("Candidate changed after recovery decision")
            state["phase"] = "done"
            save()
            return decision["response"]
        if fixed is not None:
            return finish(
                "fixed candidate failed: "
                + failure["cause"]
                + "; actual GPU allocation charge "
                + str(failure["gpu_seconds"])
                + " seconds; allow_changes=false forbids repair or a new evaluation. "
                "Independent Review must assess this incomplete experiment. "
                "Any repair requires a separate task that allows changes"
            )
        if state["phase"] == "repair":
            if budget_status()["active_attempt"] is not None:
                raise HarnessError("Unresolved debug accounting blocks a new repair")
            key = f"repair-{state['revision']:03d}-try-{state['repair_try']:03d}"
            directory = run_dir / "repairs" / key
            attempts = sorted(directory.glob("attempt-*"))
            if attempts and completed_response(attempts[-1]) is None:
                _stopped(attempts[-1], read)
                _no_interface(workspace, failure.get("submission"))
                raise HarnessError(
                    "Interrupted repair requires explicit reconciliation"
                )
            if wall_deadline <= time.time() or (
                budget is not None and budget.remaining() < 60
            ):
                return finish(
                    "debug budget exhausted or original wall deadline reached"
                )
            remaining = vars(invoke).get(
                "budget_status", lambda: context.get("budget", {})
            )()
            if not attempts and (
                (
                    remaining.get("calls_left") is not None
                    and remaining["calls_left"] <= 1
                )
                or remaining.get("seconds_left", 1) < 1
            ):
                return finish("original call/time budget cannot admit a repair")
            try:
                invoke(
                    key,
                    {
                        "debug": {
                            "instructions": REPAIR,
                            "failure": state["failures"][-1],
                            "original_failure": failure,
                        }
                    },
                    directory,
                    _RepairDelegate(harness.delegate, root, wall_deadline),
                )
            except BudgetExhausted:
                return finish("original call/time repair budget exhausted")
            except HarnessError:
                check_scope(workspace, baseline, allow_research=allow_research)
                _no_interface(workspace, failure.get("submission"))
                for attempt in directory.glob("attempt-*"):
                    _stopped(attempt, read)
                raise
            check_scope(workspace, baseline, allow_research=allow_research)
            _no_interface(workspace, failure.get("submission"))
            changed = signatures(candidate_bytes(workspace))
            if changed == state["failed_signatures"]:
                return finish("repair did not change the failed candidate source")
            if policy is None:
                return finish("single CPU repair completed; no independent GPU grant")
            state.update(phase="smoke_prepare", source_signatures=changed)
            save()
        if state["phase"] == "smoke_prepare":
            revision = debug / "revisions" / f"{state['revision']:03d}"
            commit, current, prepared = freeze(revision)
            state.update(
                phase="smoke",
                source_commit=commit,
                fingerprint=prepared["fingerprint"],
                source_signatures=current,
                smoke_submitted=False,
            )
            save()
        if state["phase"] == "smoke":
            revision = debug / "revisions" / f"{state['revision']:03d}"
            if (
                not state["smoke_submitted"]
                and min(budget.remaining(), wall_deadline - time.time()) < 60
            ):
                return finish(
                    "no complete scheduler minute remains in debug/wall budget"
                )
            recover = state["smoke_submitted"]
            state["smoke_submitted"] = True
            save()
            receipt = harness.backend.debug_smoke(
                revision / "source",
                revision / "runtime-config.json",
                debug,
                f"smoke-{state['revision']:03d}",
                state["deadline"],
                recover=recover,
            )
            if receipt.get("status") == "blocked" and not receipt.get("fingerprint"):
                raise HarnessError(
                    "Debug smoke returned an unbound blocked receipt: "
                    + str(receipt.get("cause") or receipt.get("error"))
                )
            if (
                receipt.get("purpose") != "debug"
                or receipt.get("quality_score_eligible") is not False
                or receipt.get("fingerprint") != state["fingerprint"]
            ):
                raise HarnessError(
                    "Debug smoke receipt has an invalid purpose or source identity"
                )
            if (
                receipt.get("status") == "blocked"
                or budget_status()["active_attempt"] is not None
            ):
                immutable_record(revision / "blocked.json", receipt)
                raise HarnessError("Debug smoke has unknown/unaccounted effects")
            immutable_record(revision / "smoke.json", receipt)
            if receipt.get("status") == "failed":
                if receipt.get("failure_kind") != "candidate" or not receipt.get(
                    "cause"
                ):
                    raise HarnessError(
                        "Debug smoke failure has an unknown or infrastructure cause"
                    )
                if budget.remaining() < 60:
                    state["failures"].append(receipt)
                    save()
                    return finish("cumulative debug GPU grant exhausted")
                state["failures"].append(receipt)
                state.update(
                    phase="repair",
                    failed_signatures=state["source_signatures"],
                    revision=state["revision"] + 1,
                    repair_try=1,
                )
                save()
                continue
            if (
                receipt.get("status") != "completed"
                or receipt.get("verified") is not True
            ):
                raise HarnessError("Debug smoke lacks a verified native-gate success")
            state.update(phase="formal_prepare", formal=state["formal"] + 1)
            save()
        if state["phase"] == "formal_prepare":
            job = debug / f"formal-{state['formal']:03d}"
            fresh = f"debug-{iteration}-formal-{state['formal']:03d}"
            preparing = {
                "iteration": iteration,
                "request": {"action": "evaluate"},
                "status": "preparing",
                "source_signatures": state["source_signatures"],
                "fingerprint": state["fingerprint"],
                "fresh_attempt": fresh,
                "record_name": allocation_record(state["fingerprint"], fresh),
            }
            if (job / "job.json").exists():
                previous = read(job / "job.json")
                if any(
                    previous.get(name) != value
                    for name, value in preparing.items()
                    if name != "status"
                ) or previous.get("status") not in {"preparing", "prepared"}:
                    raise HarnessError(
                        "Fresh formal preparation cannot replace another job"
                    )
            else:
                write(job / "job.json", preparing)
            commit, current, prepared = freeze(job)
            if prepared["fingerprint"] != state["fingerprint"]:
                raise HarnessError(
                    "Fresh formal source differs from the smoke-tested candidate"
                )
            record = {
                "iteration": iteration,
                "request": {"action": "evaluate"},
                "status": "prepared",
                "source_signatures": current,
                "source_commit": commit,
                "fingerprint": prepared["fingerprint"],
                "fresh_attempt": fresh,
                "record_name": allocation_record(prepared["fingerprint"], fresh),
            }
            write(job / "job.json", record)
            state.update(phase="formal", formal_submitted=False)
            save()
        if state["phase"] == "formal":
            job = debug / f"formal-{state['formal']:03d}"
            record = read(job / "job.json")
            recover = state["formal_submitted"] or record["status"] in {
                "submitted",
                "completed",
                "failed",
            }
            if not recover:
                capacity = harness.backend.formal_capacity(
                    job / "runtime-config.json", wall_deadline
                )
                if not capacity["admitted"]:
                    return finish(
                        "full formal allocation unavailable: " + capacity["reason"]
                    )
            write(
                root / "control/jobs" / iteration / "recovery.json",
                {"job_directory": str(job)},
            )
            state["formal_submitted"] = True
            record["status"] = "submitted"
            write(job / "job.json", record)
            save()
            try:
                receipt = harness.backend.evaluate(
                    job / "source",
                    job / "runtime-config.json",
                    timeout_seconds=max(0.01, wall_deadline - time.time()),
                    recover=recover,
                    fresh_attempt=record["fresh_attempt"],
                    cancel_event=threading.Event(),
                )
            except Exception as error:
                try:
                    rejected = harness.backend.settled_failure(
                        job / "source",
                        job / "runtime-config.json",
                        record["fingerprint"],
                        record_name=record["record_name"],
                    )
                except Exception as unknown:
                    try:
                        timeout = timed_out_job(
                            root, harness.backend, workspace, iteration
                        )
                    except Exception:
                        raise HarnessError(
                            "Fresh formal allocation has unknown/unsettled effects"
                        ) from unknown
                    state["failures"].append(timeout)
                    return finish(
                        "settled formal TIMEOUT; no measurement; actual GPU charge "
                        + str(timeout["gpu_seconds"])
                        + " seconds"
                    )
                if (
                    rejected.get("kind") != "candidate_rejected"
                    or rejected.get("fingerprint") != record["fingerprint"]
                    or rejected.get("purpose") != "formal"
                    or rejected.get("quality_score_eligible") is not False
                ):
                    raise HarnessError(
                        "Fresh formal failure is not a certified candidate rejection"
                    )
                rejected = {
                    **rejected,
                    "iteration": iteration,
                    "job_directory": str(job),
                    "source_signatures": record["source_signatures"],
                    "source_commit": record["source_commit"],
                }
                immutable_record(job / "failure.json", rejected)
                record.update(status="failed", error=str(error))
                write(job / "job.json", record)
                state["failures"].append(rejected)
                state.update(
                    phase="repair",
                    failed_signatures=record["source_signatures"],
                    revision=state["revision"] + 1,
                    repair_try=1,
                )
                save()
                continue
            if (
                receipt.get("verified") is not True
                or receipt.get("metric") != METRIC
                or receipt.get("fingerprint") != record["fingerprint"]
                or receipt.get("record_name") != record["record_name"]
                or type(receipt.get("value")) not in (int, float)
                or not math.isfinite(receipt["value"])
            ):
                raise HarnessError(
                    "Fresh formal evaluator did not return a verified fixed-protocol score"
                )
            if signatures(candidate_bytes(workspace)) != record["source_signatures"]:
                raise HarnessError("Candidate changed during fresh formal evaluation")
            trusted = {
                **receipt,
                "status": "completed",
                "source_commit": record["source_commit"],
                "artifact": {
                    "path": str(workspace / CANDIDATE_FILES[2]),
                    "version": record["fingerprint"],
                },
                "config_snapshot": str(job / "runtime-config.json"),
                "frozen_source": str(job / "source"),
                "provenance": "fresh-formal-after-unscored-debug",
            }
            if (job / "result.json").exists():
                saved = read(job / "result.json")
                for name in (
                    "status",
                    "source_commit",
                    "artifact",
                    "config_snapshot",
                    "frozen_source",
                    "fingerprint",
                    "metric",
                    "value",
                    "verified",
                ):
                    if saved.get(name) != trusted[name]:
                        raise HarnessError(
                            "Retained formal result differs from authoritative receipt"
                        )
                trusted = saved
            else:
                immutable_record(job / "result.json", trusted)
            record.update(status="completed", completed_at=time.time())
            write(job / "job.json", record)
            result = {
                "outcome": "completed",
                "markdown": "Controller recovery repaired a certified implementation failure, passed unscored native smoke gates, and obtained a NEW full fixed-protocol formal measurement. Debug outputs are excluded from the score. Retained evidence: "
                + str(debug),
                "artifacts": [trusted["artifact"]],
                "measurements": [
                    {
                        "candidate_id": trusted["fingerprint"],
                        "artifact": trusted["artifact"],
                        "evaluation_id": PROTOCOL,
                        "comparison_scope": PROTOCOL,
                        "metrics": [{"name": METRIC, "value": trusted["value"]}],
                        "evidence": trusted["evidence"],
                    }
                ],
            }
            return finish("new full formal evaluation completed", result)
