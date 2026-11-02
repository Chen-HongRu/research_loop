"""Explicit certification of the observed account-lookup submission rejection.

No scheduler receipt is invented. Raw attempts and reservations remain intact;
only a validated certificate can project their unused reservation out of totals.
"""

from datetime import datetime, timedelta, timezone
import fcntl
import json
from pathlib import Path
import re
import shlex

from integrations.protok.backend import ProTokError
from integrations.protok.no_allocation import _bytes, _json, _publish


def read(path):
    return json.loads(_bytes(path))


def _submission_logs(record):
    logs = read(record / "command-diagnostics/diagnostic-manifest.json")["logs"]
    return [row for row in logs if row.get("stage") == "submit"]


def remote_probe_command(remote):
    code = """import json,stat
from pathlib import Path
p=Path(REMOTE)
q=p/'job-id.txt.tmp'
allowed={'code','output','inputs.json','environment.json','job-id.txt','job-id.txt.tmp'}
print(json.dumps({'path':str(p),'job_id_exists':(p/'job-id.txt').exists() or (p/'job-id.txt').is_symlink(),'temporary_regular':stat.S_ISREG(q.lstat().st_mode),'temporary_size':q.stat().st_size,'output_names':sorted(x.name for x in (p/'output').iterdir()),'start_artifacts':sorted(x.name for x in p.iterdir() if x.name not in allowed)}))
""".replace("REMOTE", repr(remote))
    return "python3 -c " + shlex.quote(code)


def scheduler_commands(audit):
    local = timezone(timedelta(hours=8))
    start = (
        datetime.fromtimestamp(audit["start"], local)
        .replace(tzinfo=None)
        .isoformat(timespec="seconds")
    )
    end = (
        datetime.fromtimestamp(audit["end"], local)
        .replace(tzinfo=None)
        .isoformat(timespec="seconds")
    )
    account, control = audit["account"], audit["control_job_id"]
    return {
        "squeue": f'squeue -h -u {account} -o "%i|%u|%V|%T"',
        "sacct": f"sacct -n -X -u {account} -S {start} -E {end} --format=JobIDRaw,User,Submit,State --parsable2",
        "control": f"sacct -n -X -j {control} --format=JobIDRaw,User,Submit,State --parsable2",
    }


def certified_receipt(certificate, path):
    receipt = {
        **certificate["blocked"],
        "status": "failed",
        "failure_kind": "operator_certified_no_allocation",
        "gpu_seconds": 0,
        "cpu_seconds": 0,
        "error": "Operator certified rejected submission; no Slurm allocation or scientific result is asserted.",
        "rejection_certificate": str(path),
    }
    for name in ("owned_job_id", "terminal_state", "exit_code"):
        receipt.pop(name, None)
    return receipt


def certified_result(certificate, path):
    folder = path.parent.parent.parent / "research-sources" / path.parent.name
    receipt = certified_receipt(certificate, path)
    return {
        **receipt,
        "source_commit": certificate["source_commit"],
        "frozen_source": str(folder / "source"),
        "contract": certificate["source_record"]["contract"],
        "artifact": {
            "path": str(folder / "result.json"),
            "version": receipt["execution_identity"],
        },
    }


def _proof(certificate, control):
    job, state = certificate["job"], certificate["state"]
    attempts = state.get("attempts", [])
    if (
        certificate.get("schema") != 1
        or certificate.get("classification") != "operator-certified-research-rejection"
        or not isinstance(certificate.get("authorization"), str)
        or not certificate["authorization"].strip()
        or state.get("purpose") != "research"
        or len(attempts) != 1
    ):
        raise ProTokError("Explicit research rejection evidence is required")
    attempt = attempts[0]
    blocked = certificate["blocked"]
    if (
        job.get("status") != "blocked"
        or attempt.get("status") != "submitting"
        or attempt.get("mode") != "research"
        or attempt.get("seed") != 42
        or attempt.get("directory") != "allocation"
        or attempt.get("budget_seconds") != job.get("allocated_seconds")
        or attempt.get("remote") != job.get("remote")
        or not isinstance(attempt.get("submitted_at"), (int, float))
        or any(
            name in attempt
            for name in (
                "owned_job_id",
                "job_id",
                "gpu_seconds",
                "terminal_state",
                "exit_code",
            )
        )
    ):
        raise ProTokError("Research submission has unknown or allocated effects")
    if (
        blocked.get("status") != "blocked"
        or blocked.get("purpose") != "research"
        or blocked.get("quality_score_eligible") is not False
        or blocked.get("outputs") != []
        or any(
            blocked.get(name) is not None
            for name in (
                "owned_job_id",
                "terminal_state",
                "exit_code",
                "gpu_seconds",
                "cpu_seconds",
            )
        )
        or any(
            blocked.get(name) != job.get(name)
            for name in ("source_identity", "execution_identity", "allocated_seconds")
        )
    ):
        raise ProTokError(
            "Blocked receipt does not describe this unaccounted submission"
        )
    logs = certificate["submit_logs"]
    stdout = [row for row in logs if row.get("stream") == "stdout"]
    stderr = [row for row in logs if row.get("stream") == "stderr"]
    if (
        len(logs) != 2
        or len(stdout) != 1
        or len(stderr) != 1
        or any(row.get("returncode") != 1 for row in logs)
        or stdout[0].get("bytes") != 0
        or stdout[0].get("tail") != ""
        or stderr[0].get("bytes") != len(stderr[0].get("tail", "").encode())
        or "default account: timeout: context deadline exceeded"
        not in stderr[0]["tail"]
        or "sbatch: error: Batch job submission failed: Access/permission denied"
        not in stderr[0]["tail"]
    ):
        raise ProTokError("A complete positive sbatch rejection is required")
    finish = max(row["recorded_at"] for row in logs)
    known = {
        str(row["job_id"])
        for row in read(control / "shared-gpu-budget.json")["attempts"].values()
        if row.get("job_id") is not None
    }
    audits = certificate["audits"]
    if len(audits) != 2 or audits[1]["observed_at"] - audits[0]["observed_at"] < 30:
        raise ProTokError("Two separated successful scheduler audits are required")
    for audit in audits:
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]+", audit.get("account", ""))
            or not audit["start"]
            <= attempt["submitted_at"]
            <= finish
            <= audit["end"]
            <= audit["observed_at"]
            or audit["observed_at"] - finish < 300
            or audit.get("ssh_host") != job["base_config"]["ssh_host"]
            or audit.get("timezone")
            != {
                "command": "date +%z",
                "returncode": 0,
                "stdout": "+0800\n",
                "stderr": "",
            }
            or audit.get("account_query")
            != {
                "command": "id -un",
                "returncode": 0,
                "stdout": audit["account"] + "\n",
                "stderr": "",
            }
            or audit.get("control_job_id") not in known
            or audit.get("remote")
            != {
                "path": job["remote"],
                "job_id_exists": False,
                "temporary_regular": True,
                "temporary_size": 0,
                "output_names": [],
                "start_artifacts": [],
            }
        ):
            raise ProTokError("Scheduler audit does not cover this rejected submission")
        remote_query = audit["remote_query"]
        if (
            remote_query["returncode"] != 0
            or remote_query.get("stderr") != ""
            or remote_query["command"] != remote_probe_command(job["remote"])
            or json.loads(remote_query["stdout"]) != audit["remote"]
        ):
            raise ProTokError("Remote audit command does not inspect this submission")
        commands = scheduler_commands(audit)
        control_rows = []
        for name in ("squeue", "sacct", "control"):
            query = audit[name]
            if (
                query.get("returncode") != 0
                or query.get("command") != commands[name]
                or query.get("stderr") != ""
            ):
                raise ProTokError("Failed scheduler queries cannot certify rejection")
            for line in query["stdout"].splitlines():
                if not line.strip():
                    continue
                fields = line.strip().rstrip("|").split("|")
                if len(fields) != 4:
                    raise ProTokError("Unrecognized scheduler audit row")
                job_id, user, submitted, status = fields
                submitted = (
                    datetime.fromisoformat(submitted)
                    .replace(tzinfo=timezone(timedelta(hours=8)))
                    .timestamp()
                )
                if (
                    job_id not in known
                    or user != audit["account"]
                    or submitted >= attempt["submitted_at"]
                    or status
                    not in {"RUNNING", "COMPLETED", "TIMEOUT", "CANCELLED", "FAILED"}
                ):
                    raise ProTokError(
                        "Unknown scheduler effects prevent rejection certification"
                    )
                if name == "control":
                    control_rows.append(job_id)
        if control_rows != [audit["control_job_id"]]:
            raise ProTokError("A known-job positive accounting control is required")


def validate_budget_certificate(control, key, entry):
    """Read only bounded controller metadata; never inputs or checkpoints."""
    try:
        _validate_budget_certificate(control, key, entry)
    except (KeyError, TypeError, ValueError, IndexError) as error:
        raise ProTokError("Malformed research rejection evidence") from error


def _validate_budget_certificate(control, key, entry):
    control = Path(control).resolve()
    certificate_path = Path(entry["no_allocation_certificate"])
    identity = json.loads(key)
    if (
        len(identity) != 3
        or identity[0] != "research"
        or identity[2] != "allocation"
        or not re.fullmatch(r"[0-9]{4,}", Path(identity[1]).name)
        or Path(identity[1]) != control / "research-jobs" / Path(identity[1]).name
        or certificate_path != Path(identity[1]) / "rejected-submission.json"
    ):
        raise ProTokError("Rejection certificate belongs to another reservation")
    record = certificate_path.parent
    certificate = read(certificate_path)
    original_entry = {
        k: v for k, v in entry.items() if k != "no_allocation_certificate"
    }
    job = read(record / "job.json")
    if (
        certificate.get("budget_key") != key
        or certificate.get("reservation") != original_entry
        or original_entry.get("phase") != "submitting"
        or original_entry.get("job_id") is not None
        or any(
            name in original_entry
            for name in (
                "receipt",
                "owned_job_id",
                "gpu_seconds",
                "terminal_state",
                "exit_code",
            )
        )
        or certificate.get("state") != read(record / "state.json")
        or certificate.get("blocked") != read(record / "blocked.json")
        or certificate.get("submit_logs") != _submission_logs(record)
        or certificate.get("source_record")
        != read(control / "research-sources" / record.name / "record.json")
        or certificate["source_record"]["contract"]["execution"]
        != certificate["job"]["execution"]
        or job != certificate["job"]
        and job != {**certificate["job"], "status": "failed"}
    ):
        raise ProTokError(
            "Original rejected reservation or submission evidence changed"
        )
    _proof(certificate, control)
    if (record / "receipt.json").exists() and read(
        record / "receipt.json"
    ) != certified_receipt(certificate, certificate_path):
        raise ProTokError("Certified rejection receipt changed")
    result_path = control / "research-sources" / record.name / "result.json"
    if result_path.exists() and read(result_path) != certified_result(
        certificate, certificate_path
    ):
        raise ProTokError("Certified rejection result changed")


def certify(root, backend, iteration, *, audits, authorization):
    """Operator-only publication, serialized with submission and the runner."""
    control = Path(root) / "control"
    with (
        (control / "runner.lock").open("a") as runner,
        (control / "protok.lock").open("a") as allocation,
    ):
        for lock in (runner, allocation):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _certify(
            root, backend, iteration, audits=audits, authorization=authorization
        )


def _certify(root, backend, iteration, *, audits, authorization):
    from integrations.protok import run
    from integrations.protok.research_execution import (
        completed_script,
        frozen_contract,
        research_files,
    )
    from integrations.protok.shared_budget import SharedBudget

    root = Path(root)
    control = root / "control"
    study = run.Study(root / "study")
    state = study.status()
    if (
        f"{state['iteration']:04d}" != iteration
        or (state["phase"], state["status"]) != ("execute", "needs_attention")
        or (control / "STOP").exists()
        or (control / "HANDOFF").exists()
    ):
        raise ProTokError("Only the unchanged interrupted research Execute may certify")
    previous = study.root / state["active_attempt"]
    for path in (previous, previous / "agent"):
        study._check_attempt_stopped(path)
    folder = control / "research-sources" / iteration
    record = control / "research-jobs" / iteration
    saved, job = read(folder / "record.json"), read(record / "job.json")
    workspace = Path(
        read(study.root / "iterations" / iteration / "iteration.json")["workspace"]
    )
    contract = frozen_contract(root, iteration)
    if (
        saved["contract"] != contract
        or job["execution"] != contract["execution"]
        or job["owner"] != read(control / "ownership.json")
        or job["continuation"] != read(control / "continuation.json")
        or job["owner"]["owner_token"] != backend.owner_token
        or read(workspace / "evaluation-submission.json") != {"action": "research"}
        or (workspace / "evaluation-request.json").exists()
        or (workspace / "evaluation-request.json.tmp").exists()
    ):
        raise ProTokError("Rejected research identity or authorization changed")
    for source in (workspace, folder / "source"):
        run.check_fixed_candidate(root, source, iteration, folder / "source")
        run.check_scope(
            source, read(study.root / "study.json")["baseline"], allow_research=True
        )
        if (
            run.signatures({**run.candidate_bytes(source), **research_files(source)})
            != saved["source_signatures"]
        ):
            raise ProTokError("Rejected research source changed")
    key = json.dumps(["research", str(record), "allocation"])
    entry = read(control / "shared-gpu-budget.json")["attempts"][key]
    certificate_path = record / "rejected-submission.json"
    if not certificate_path.exists():
        certificate = {
            "schema": 1,
            "classification": "operator-certified-research-rejection",
            "authorization": authorization,
            "budget_key": key,
            "reservation": entry,
            "job": job,
            "state": read(record / "state.json"),
            "blocked": read(record / "blocked.json"),
            "submit_logs": _submission_logs(record),
            "audits": audits,
            "source_record": saved,
            "source_commit": run.git(folder / "source", "rev-parse", "HEAD"),
        }
        _proof(certificate, control)
        _publish(certificate_path, _json(certificate))
    SharedBudget(control).certify_no_allocation(key, certificate_path)
    certificate = read(certificate_path)
    receipt = certified_receipt(certificate, certificate_path)
    _publish(record / "receipt.json", _json(receipt))
    run.write(record / "job.json", {**certificate["job"], "status": "failed"})
    result = certified_result(certificate, certificate_path)
    _publish(folder / "result.json", _json(result))
    return completed_script(root, workspace, iteration)
