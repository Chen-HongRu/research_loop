"""Audited operator settlement of one submission that never obtained an allocation.

The scheduler observations support an operator certification, not a Slurm terminal
state. Raw evaluator state stays unchanged; only accounting receives a projection.
"""

from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from integrations.protok.backend import ProTokError

CERTIFICATE = "no-allocation.json"
EVIDENCE = (
    "state-before.json",
    "submission-observations.txt",
    "scheduler-observations.txt",
    "final-scheduler-audit.txt",
)


def _bytes(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
        raise ProTokError("No-allocation evidence must be a small regular file")
    return path.read_bytes()


def _read(path):
    return json.loads(_bytes(path))


def _json(value):
    return (json.dumps(value, indent=2, allow_nan=False) + "\n").encode()


def _publish(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        if _bytes(path) != data:
            raise ProTokError("Immutable no-allocation evidence changed")
        return
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=".no-allocation-", delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o444)
            try:
                os.link(temporary, path)
            except FileExistsError:
                if _bytes(path) != data:
                    raise ProTokError(
                        "Immutable no-allocation evidence changed"
                    ) from None
        finally:
            temporary.unlink(missing_ok=True)


def _state(state):
    attempts = state.get("attempts", [])
    if (
        state.get("status") != "incomplete"
        or state.get("mode") != "scored"
        or state.get("baseline") is not False
        or state.get("imported_baseline")
        or len(attempts) != 1
        or not re.fullmatch(r"[a-f0-9]{64}", state.get("fingerprint", ""))
    ):
        raise ProTokError(
            "No-allocation settlement requires the original incomplete submission"
        )
    attempt = attempts[0]
    if (
        attempt.get("status") != "submitting"
        or attempt.get("seed") != 42
        or attempt.get("mode") != "evaluate"
        or attempt.get("budget_seconds") != 1800
        or attempt.get("include_smoke") is not True
        or any(
            key in attempt
            for key in (
                "owned_job_id",
                "job_id",
                "terminal_state",
                "gpu_seconds",
                "exit_code",
            )
        )
        or not re.fullmatch(r"[A-Za-z0-9_-]+", attempt.get("directory", ""))
        or not isinstance(attempt.get("submitted_at"), (int, float))
        or not isinstance(attempt.get("remote"), str)
        or not attempt["remote"].endswith(
            "/" + state["fingerprint"] + "/" + attempt["directory"]
        )
        or "job-id.txt" not in state.get("error", "")
    ):
        raise ProTokError(
            "Submission has an allocation identity or incompatible original effects"
        )
    return attempt


def _observations(evidence, state):
    """Validate the actual account/window audit and retained evaluator failure."""
    attempt = _state(state)
    if json.loads(evidence["state-before.json"]) != state:
        raise ProTokError("No-allocation observation differs from the original state")
    submission = evidence["submission-observations.txt"].decode()
    decoder = json.JSONDecoder()
    first = submission.index("{")
    observed, end = decoder.raw_decode(submission[first:])
    remainder = submission[first + end :].lstrip()
    process, _ = decoder.raw_decode(remainder)
    if (
        observed != state
        or process.get("fingerprint") != state["fingerprint"]
        or process.get("record_name") != state["fingerprint"]
        or process.get("verified") is not False
        or process.get("exit_code") != 1
        or process.get("timed_out") is not False
        or process.get("cancelled") is not False
    ):
        raise ProTokError(
            "No-allocation process evidence does not match the failed submission"
        )
    earlier = evidence["scheduler-observations.txt"].decode()
    if (
        "BEGIN SQUEUE\nSQUEUE_EXIT=0\nBEGIN SACCT\nSACCT_EXIT=0\n" not in earlier
        or "CONTROL_EXIT=0" not in earlier
    ):
        raise ProTokError(
            "Earlier scheduler observations are not successful empty queries"
        )
    audit = evidence["final-scheduler-audit.txt"].decode()
    match = re.match(
        r"(?P<observed>\S+)\nACCOUNT=(?P<account>[A-Za-z0-9_-]+)\n"
        r"SQUEUE_BEGIN\nSQUEUE_EXIT=0\nSACCT_BEGIN submission interval "
        r"(?P<start>\S+) to (?P<end>\S+)\nSACCT_EXIT=0\nCONTROL_BEGIN\n"
        r"(?P<control>[1-9][0-9]*)\|[^|\n]+\|COMPLETED\|[0-9]+\|[^|\n]+\nCONTROL_EXIT=0\n",
        audit,
    )
    if not match:
        raise ProTokError(
            "No-allocation requires successful empty account queries and a known-job control"
        )
    fields = match.groupdict()
    observed = datetime.fromisoformat(fields["observed"])
    if observed.tzinfo is None:
        raise ProTokError("No-allocation audit timestamp requires a timezone")
    start = datetime.fromisoformat(fields["start"]).replace(tzinfo=observed.tzinfo)
    end = datetime.fromisoformat(fields["end"]).replace(tzinfo=observed.tzinfo)
    if not (
        start.timestamp()
        <= attempt["submitted_at"]
        <= process["finished_at"]
        <= end.timestamp()
        <= observed.timestamp()
    ):
        raise ProTokError(
            "Scheduler query interval does not cover the original submission"
        )
    listing = audit[match.end() :]
    if (
        not re.search(
            r"^-\S+\s+\d+\s+"
            + re.escape(fields["account"])
            + r"\s+\S+\s+0\s+\S+\s+\d+\s+\S+\s+job-id\.txt\.tmp$",
            listing,
            re.M,
        )
        or re.search(r"^[-dl]\S+.*\sjob-id\.txt$", listing, re.M)
        or f"ls -la {attempt['remote']}" not in listing
        or f"sacct -u {fields['account']} -S {fields['start']} -E {fields['end']} -X"
        not in listing
        or f"squeue -h -u {fields['account']} " not in listing
        or f"sacct -j {fields['control']} " not in listing
    ):
        raise ProTokError(
            "Remote submission evidence or audited query identity differs"
        )
    return {
        key: fields[key] for key in ("account", "start", "end", "observed", "control")
    }


def projection(record, state):
    """Return a zero-allocation accounting view only for an unchanged certificate.

    This view deliberately contains no Slurm job ID or invented terminal state.
    The raw attempts remain in state.json and in the immutable certificate.
    """
    record = Path(record)
    path = record / CERTIFICATE
    if not path.exists() and not path.is_symlink():
        return state
    certificate = _read(path)
    if (
        certificate.get("schema") != 1
        or certificate.get("classification") != "operator-certified-no-allocation"
        or not isinstance(certificate.get("authorization"), str)
        or not certificate["authorization"].strip()
        or certificate.get("original_state") != state
        or certificate.get("record_name") != record.name
        or certificate.get("run_config") != _read(record / "run-config.json")
    ):
        raise ProTokError("No-allocation certificate identity changed")
    evidence = {
        name: _bytes(record / "no-allocation-evidence" / name) for name in EVIDENCE
    }
    if certificate.get("evidence_sha256") != {
        name: hashlib.sha256(data).hexdigest() for name, data in evidence.items()
    }:
        raise ProTokError("Immutable no-allocation observations changed")
    if _observations(evidence, state) != certificate.get("scheduler_audit"):
        raise ProTokError("No-allocation scheduler certification changed")
    return {
        **copy.deepcopy(state),
        "status": "failed",
        "attempts": [],
        "operator_no_allocation": str(path),
        "quality_score_eligible": False,
    }


def receipt(
    backend,
    workspace,
    config_path,
    fingerprint,
    *,
    record_name=None,
    evidence_dir=None,
    authorization=None,
):
    """Create or revalidate an audited receipt, without evaluating or submitting."""
    config = backend._config()
    owner = _read(backend.control / "ownership.json")
    workspace, config_path = Path(workspace).resolve(), Path(config_path).resolve()
    frozen = _read(config_path)
    if (
        owner.get("owner_token") != backend.owner_token
        or owner.get("project") != str(backend.project)
        or owner.get("handoff_acknowledged") is not True
        or frozen.get("workspace") != str(workspace)
        or not backend._receipt_config_matches(
            frozen, config | {"workspace": str(workspace)}
        )
        or record_name not in (None, fingerprint)
    ):
        raise ProTokError(
            "No-allocation receipt has no matching frozen owner/source configuration"
        )
    record = Path(config["records_root"]) / fingerprint
    state = _read(record / "state.json")
    _state(state)
    if (
        state["fingerprint"] != fingerprint
        or state.get("controller_lease", {}).get("owner_token") != backend.owner_token
    ):
        raise ProTokError(
            "No-allocation original submission belongs to another identity"
        )
    if not backend._receipt_config_matches(_read(record / "run-config.json"), frozen):
        raise ProTokError("No-allocation evaluator configuration changed")
    prepared = subprocess.run(
        backend._command(config_path, "prepare"),
        capture_output=True,
        text=True,
        cwd=backend.control,
        timeout=120,
    )
    if (
        prepared.returncode
        or json.loads(prepared.stdout).get("fingerprint") != fingerprint
    ):
        raise ProTokError("No-allocation frozen source identity changed")
    certificate_path = record / CERTIFICATE
    if not certificate_path.exists():
        if (
            not isinstance(authorization, str)
            or not authorization.strip()
            or evidence_dir is None
        ):
            raise ProTokError(
                "Explicit operator certification and retained evidence are required"
            )
        evidence = {name: _bytes(Path(evidence_dir) / name) for name in EVIDENCE}
        audit = _observations(evidence, state)
        certificate = {
            "schema": 1,
            "classification": "operator-certified-no-allocation",
            "authorization": authorization,
            "record_name": fingerprint,
            "original_state": state,
            "run_config": _read(record / "run-config.json"),
            "frozen_config": frozen,
            "scheduler_audit": audit,
            "evidence_sha256": {
                name: hashlib.sha256(data).hexdigest()
                for name, data in evidence.items()
            },
        }
        for name, data in evidence.items():
            _publish(record / "no-allocation-evidence" / name, data)
        _publish(certificate_path, _json(certificate))
    projection(record, state)
    if _read(certificate_path).get("frozen_config") != frozen:
        raise ProTokError("No-allocation frozen receipt configuration changed")
    return {
        "purpose": "formal",
        "status": "failed",
        "failure_kind": "operator_certified_no_allocation",
        "fingerprint": fingerprint,
        "record_name": fingerprint,
        "gpu_seconds": 0,
        "quality_score_eligible": False,
        "cause": "Operator certified no allocation after successful empty account queries and a known-job control; no Slurm terminal status is asserted.",
        "evidence": [
            str(certificate_path),
            *[str(record / "no-allocation-evidence" / name) for name in EVIDENCE],
        ],
    }
