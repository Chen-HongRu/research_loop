"""Bounded native Slurm script adapter, configured entirely by the operator.

The trusted launcher owns sandboxing and the environment. This module never
interprets script-authored values as evaluator measurements. Each callback does
at most one scheduler action; submission uncertainty never permits replay.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import time
import uuid

from research_loop.runtime.operation_contract import fields, json_value

SUFFIXES = {".py", ".json", ".md", ".txt", ".sh", ".toml", ".yaml", ".yml", ".sbatch"}
SUBMISSION_MARGIN_SECONDS = 60
TERMINAL = {
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "TIMEOUT",
    "OUT_OF_MEMORY",
    "NODE_FAIL",
    "PREEMPTED",
    "DEADLINE",
    "BOOT_FAIL",
}
CONFIG_FIELDS = {
    "journal_root",
    "study_root",
    "baseline_root",
    "baseline_ref",
    "source_paths",
    "protected_paths",
    "edit_roots",
    "runtime_files",
    "shared_exposure_gate",
    "resource_pool",
    "cost_unit",
    "ssh_target",
    "slurm_user",
    "slurm_timezone",
    "remote_root",
    "partition",
    "account",
    "cpus",
    "memory",
    "remote_python",
    "launcher_source",
    "input_mappings",
    "metadata_checks",
    "report_suffixes",
    "max_report_bytes",
    "max_collection_bytes",
    "command_timeout_seconds",
    "cleanup_timeout_seconds",
    "staging_headroom_seconds",
}


def _relative(value):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_./-]+", value)
        or value.startswith("/")
        or any(x in {"", ".", ".."} for x in value.split("/"))
    ):
        raise ValueError("Expected a bounded relative path")
    return value


def _absolute(value):
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or ".." in Path(value).parts
        or "\n" in value
    ):
        raise ValueError("Expected an explicit absolute operator path")
    return value


def _identity(path):
    s = Path(path).stat()
    return {
        "device": s.st_dev,
        "inode": s.st_ino,
        "size": s.st_size,
        "mtime_ns": s.st_mtime_ns,
    }


def _regular(path):
    path = Path(path)
    if any(p.is_symlink() for p in [path, *path.parents]) or not path.is_file():
        raise ValueError("Expected a regular nonsymlink file")
    return path


def _read(path, limit=262144):
    path = _regular(path)
    if path.stat().st_size > limit:
        raise ValueError("Control artifact exceeds bounded size")
    return json.loads(path.read_text())


def _write(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x") as stream:
        json.dump(value, stream, sort_keys=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def validate_config(config):
    c = json_value(config)
    fields(c, CONFIG_FIELDS, "script backend configuration")
    for key in (
        "journal_root",
        "study_root",
        "baseline_root",
        "remote_root",
        "remote_python",
    ):
        _absolute(c[key])
    ZoneInfo(c["slurm_timezone"])
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", c["remote_root"]):
        raise ValueError("Remote root must be safe in Slurm directives")
    for key in (
        "baseline_ref",
        "resource_pool",
        "cost_unit",
        "ssh_target",
        "slurm_user",
        "partition",
        "account",
        "memory",
    ):
        if (
            not isinstance(c[key], str)
            or not re.fullmatch(r"[A-Za-z0-9_./:@+-]+", c[key])
            or c[key].startswith("-")
        ):
            raise ValueError("Unsafe operator identity/directive")
    for key in (
        "cpus",
        "max_report_bytes",
        "max_collection_bytes",
        "command_timeout_seconds",
        "cleanup_timeout_seconds",
        "staging_headroom_seconds",
    ):
        if type(c[key]) is not int or c[key] < 1:
            raise ValueError("Expected positive bounded configuration")
    if (
        c["max_report_bytes"] > 1024 * 1024
        or c["max_collection_bytes"] > 16 * 1024 * 1024
        or c["command_timeout_seconds"] > 120
        or c["cleanup_timeout_seconds"] > 120
    ):
        raise ValueError("Configured callback/report bound is too large")
    if c["staging_headroom_seconds"] <= SUBMISSION_MARGIN_SECONDS:
        raise ValueError(
            "Lifecycle headroom must include staging beyond submission safety margin"
        )
    for key in ("source_paths", "protected_paths", "edit_roots"):
        if (
            not isinstance(c[key], list)
            or len(c[key]) != len(set(c[key]))
            or len(c[key]) > 4096
        ):
            raise ValueError("Source scopes must be finite unique lists")
        for item in c[key]:
            _relative(item)
    if not set(c["protected_paths"]) <= set(c["source_paths"]):
        raise ValueError("Protected source must belong to the explicit baseline scope")
    if not isinstance(c["runtime_files"], dict) or not isinstance(
        c["input_mappings"], dict
    ):
        raise ValueError("Explicit runtime/input maps required")
    for dest, source in c["runtime_files"].items():
        _relative(dest)
        _absolute(source)
    for source, value in c["input_mappings"].items():
        _relative(source)
        fields(value, {"path", "mount", "kind"}, "input mapping")
        if value["kind"] not in {"file", "directory"}:
            raise ValueError("Invalid canonical input kind")
        _absolute(value["path"])
        _absolute(value["mount"])
    if not isinstance(c["metadata_checks"], list) or len(c["metadata_checks"]) > 32:
        raise ValueError("Metadata checks must be bounded")
    for check in c["metadata_checks"]:
        fields(check, {"path", "kind", "identity"}, "metadata check")
        _absolute(check["path"])
        if check["kind"] not in {"file", "directory"}:
            raise ValueError("Invalid metadata check kind")
        if check["identity"] is not None:
            _check_identity(check["identity"])
    fields(c["shared_exposure_gate"], {"path", "identity"}, "shared exposure gate")
    _absolute(c["shared_exposure_gate"]["path"])
    _check_identity(c["shared_exposure_gate"]["identity"])
    if (
        not isinstance(c["launcher_source"], str)
        or len(c["launcher_source"].encode()) > 262144
    ):
        raise ValueError("Trusted launcher must be bounded Python source")
    compile(c["launcher_source"], "operator-launcher", "exec")
    if not isinstance(c["report_suffixes"], list) or not set(c["report_suffixes"]) <= {
        ".json",
        ".csv",
        ".txt",
        ".log",
        ".md",
    }:
        raise ValueError(
            "Collection only admits explicitly configured small report suffixes"
        )
    return c


def _check_identity(value):
    fields(value, {"device", "inode", "size", "mtime_ns"}, "artifact identity")
    if any(type(v) is not int or v < 0 for v in value.values()):
        raise ValueError("Invalid artifact stat identity")


def _gate(c):
    gate = c["shared_exposure_gate"]
    if (
        _identity(_regular(gate["path"])) != gate["identity"]
        or _read(gate["path"]).get("shared_exposure_reconciled") is not True
    ):
        raise ValueError(
            "Original shared GPU exposure has not been positively reconciled"
        )


def validate_request(request, config):
    """Pure parameter/resource checks; the application also matches accepted Plan."""
    c = validate_config(config)
    p = request["parameters"]
    fields(p, {"baseline_ref", "source_files", "execution"}, "script parameters")
    if p["baseline_ref"] != c["baseline_ref"]:
        raise ValueError("Source baseline differs from frozen operator reference")
    if not isinstance(p["source_files"], list) or len(p["source_files"]) > 4096:
        raise ValueError("Source files must be finite typed edits")
    seen = set()
    for item in p["source_files"]:
        fields(item, {"path", "content"}, "source edit")
        name = _relative(item["path"])
        if (
            name in seen
            or name in c["protected_paths"]
            or not isinstance(item["content"], str)
            or len(item["content"].encode()) > 4 * 1024 * 1024
        ):
            raise ValueError("Duplicate, protected or oversized source edit")
        if name not in c["source_paths"] and not any(
            name.startswith(root + "/") for root in c["edit_roots"]
        ):
            raise ValueError("Source edit falls outside operator scope")
        if Path(name).suffix not in SUFFIXES and name != ".gitignore":
            raise ValueError("Source edit is not source text")
        seen.add(name)
    e = p["execution"]
    fields(
        e,
        {"mode", "entrypoint", "gpu", "time_limit_seconds", "inputs", "outputs"},
        "script execution",
    )
    entry = _relative(e["entrypoint"])
    seconds = e["time_limit_seconds"]
    if (
        e["mode"] != "script"
        or e["gpu"] is not True
        or not entry.startswith("research/")
        or not entry.endswith(".py")
        or type(seconds) is not int
        or not 1 <= seconds <= 1800
    ):
        raise ValueError("Expected research/*.py on one GPU for at most 1800 seconds")
    allocated = math.ceil(seconds / 60) * 60
    if request["resources"] != {c["resource_pool"]: 1} or request["cost_bounds"] != {
        c["cost_unit"]: allocated
    }:
        raise ValueError("Resource/cost bound must match rounded one-GPU allocation")
    if request["timeout_seconds"] < allocated + c["staging_headroom_seconds"]:
        raise ValueError("Lifecycle timeout omits staging and queue headroom")
    if not isinstance(e["inputs"], list) or len(e["inputs"]) > len(c["input_mappings"]):
        raise ValueError("Inputs must be explicit canonical mappings")
    names, sources = set(), set()
    for item in e["inputs"]:
        fields(item, {"name", "source"}, "script input")
        if (
            not isinstance(item["name"], str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", item["name"])
            or item["source"] not in c["input_mappings"]
            or item["name"] in names
            or item["source"] in sources
        ):
            raise ValueError("Invalid/repeated canonical input")
        names.add(item["name"])
        sources.add(item["source"])
    if not isinstance(e["outputs"], list) or len(e["outputs"]) > 64:
        raise ValueError("Outputs must be a bounded list")
    for name in e["outputs"]:
        _relative(name)
    if len(set(e["outputs"])) != len(e["outputs"]):
        raise ValueError("Duplicate declared output")
    return allocated


class SSHTransport:
    def __init__(self, target):
        self.target = target

    def run(self, command, *, stdin=None, timeout=120):
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ClearAllForwardings=yes",
                "-o",
                "ConnectTimeout=15",
                self.target,
                command,
            ],
            input=stdin,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("Bounded SSH action failed")
        return result.stdout


def _binding(op):
    return {
        key: op.get(key)
        for key in (
            "id",
            "logical_id",
            "plan_attempt_id",
            "execute_attempt_id",
            "backend",
            "backend_version",
            "environment",
            "deadline",
        )
    }


@contextmanager
def _journal(op, c):
    key = hashlib.sha256(op["id"].encode()).hexdigest()
    root = Path(c["journal_root"])
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError("Journal root cannot be a symlink")
    with (root / (key + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        directory = root / key
        if directory.is_symlink():
            raise ValueError("Operation directory cannot be a symlink")
        directory.mkdir(exist_ok=True)
        yield directory


def _timeout(op, c, cleanup=False):
    bound = (
        c["cleanup_timeout_seconds"]
        if cleanup
        else min(c["command_timeout_seconds"], int(op["deadline"] - time.time()))
    )
    if bound < 1:
        raise ValueError("Operation deadline expired")
    return bound


def _probe(transport, op, c, checks):
    source = f"""import json,os,stat
checks={checks!r}
for item in checks:
 p=item['path']; s=os.stat(p)
 if not (stat.S_ISREG(s.st_mode) if item['kind']=='file' else stat.S_ISDIR(s.st_mode)): raise ValueError('Input type mismatch')
 actual=dict(device=s.st_dev,inode=s.st_ino,size=s.st_size,mtime_ns=s.st_mtime_ns)
 if item['identity'] is not None and actual!=item['identity']: raise ValueError('Input metadata changed')
print(json.dumps(checks))
"""
    raw = transport.run(
        shlex.join([c["remote_python"], "-c", source]), timeout=_timeout(op, c)
    )
    if json.loads(raw) != checks:
        raise ValueError("Metadata response changed declared input checks")


def _sources(request, c):
    values, total = {}, 0
    for name in c["source_paths"]:
        path = _regular(Path(c["baseline_root"]) / name)
        if path.stat().st_size > 4 * 1024 * 1024 or (
            path.suffix not in SUFFIXES and name != ".gitignore"
        ):
            raise ValueError("Baseline source exceeds source envelope")
        values[name] = path.read_bytes()
    for edit in request["parameters"]["source_files"]:
        values[edit["path"]] = edit["content"].encode()
    for name, path in c["runtime_files"].items():
        path = _regular(path)
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("Runtime source is too large")
        value = path.read_bytes()
        if name in values and values[name] != value:
            raise ValueError("Runtime source collides with candidate source")
        values[name] = value
    for name, value in values.items():
        total += len(value)
        if name.endswith(".py"):
            compile(value, name, "exec")
    if (
        total > 64 * 1024 * 1024
        or len(values) > 4096
        or request["parameters"]["execution"]["entrypoint"] not in values
    ):
        raise ValueError("Source envelope or entrypoint invalid")
    return values


def _source_manifest(directory, record):
    values = {}
    for name, digest in record["source"].items():
        value = _regular(directory / "source" / name).read_bytes()
        if hashlib.sha256(value).hexdigest() != digest:
            raise ValueError("Frozen prepared source changed")
        values[name] = value
    return values


def _event(op, action, data):
    return {
        "event_key": "script:"
        + hashlib.sha256(op["id"].encode()).hexdigest()
        + ":"
        + action
        + ":"
        + _digest(data),
        "data": data,
    }


def _load(directory, op, request, c):
    record = _read(directory / "journal.json", 2 * 1024 * 1024)
    if (
        record["binding"] != _binding(op)
        or record["request_identity"] != _digest(request)
        or record["config_identity"] != _digest(c)
    ):
        raise ValueError("Native operation/request/backend/environment binding changed")
    token = op.get("submission_token")
    owner = op.get("submit_owner_epoch", op.get("owner_epoch"))
    if record.get("token") is not None and (
        record["token"] != token or record["owner_epoch"] != owner
    ):
        raise ValueError("Submission token or original owner changed")
    if op.get("job_id") and record.get("job_id") and op["job_id"] != record["job_id"]:
        raise ValueError("Native and journal job identities contradict")
    return record


def _query(transport, op, c, record):
    start = datetime.fromtimestamp(
        record["created_at"] - 60, ZoneInfo(c["slurm_timezone"])
    ).strftime("%Y-%m-%dT%H:%M:%S")
    args = [
        "sacct",
        "-X",
        "--noheader",
        "--parsable2",
        "--user=" + c["slurm_user"],
        "--starttime=" + start,
        "--name=" + record["job_name"],
        "--format=JobIDRaw,JobName%128,Comment%128,User%128,Account%128,State,ExitCode,ElapsedRaw,AllocTRES,End",
    ]
    if record.get("job_id"):
        args += ["--jobs=" + record["job_id"]]
    raw = transport.run(shlex.join(args), timeout=_timeout(op, c, True))
    rows = []
    for line in raw.splitlines():
        row = line.strip().split("|")
        if len(row) == 11 and not row[-1]:
            row.pop()
        if len(row) != 10:
            raise ValueError("Malformed accounting observation")
        if not row[0].isdigit():
            continue
        expected = [
            record["job_name"],
            record["comment"],
            c["slurm_user"],
            c["account"],
        ]
        if any(
            actual and actual != wanted
            for actual, wanted in zip(row[1:5], expected, strict=True)
        ) or (record.get("job_id") and row[0] != record["job_id"]):
            raise ValueError("Scheduler identity contradicts original operation")
        rows.append(row)
    if len(rows) > 1:
        raise ValueError("Multiple scheduler jobs contradict one-submit identity")
    # Slurm accounting may publish the allocation before all identity metadata.
    # A partial row cannot authorize cancellation, release or final accounting.
    return rows[0] if rows and all(rows[0][1:5]) else None


def _outcome(row):
    state = row[5].split()[0].split("+")[0]
    if state not in TERMINAL:
        return None
    return (
        "succeeded"
        if state == "COMPLETED" and row[6] == "0:0"
        else "cancelled"
        if state == "CANCELLED"
        else "timed_out"
        if state in {"TIMEOUT", "DEADLINE"}
        else "failed"
    )


def _submit(transport, directory, record, op, request, c):
    if record.get("token") is not None:
        # Intent, including failed staging or a lost sbatch acknowledgement, is never replayed.
        return {
            "outcome": "accepted" if record.get("job_id") else "unknown",
            "job_id": record.get("job_id"),
            "proof": None,
            "reason": "Original durable submission identity retained; no replay",
        }
    _gate(c)
    token = op.get("submission_token")
    owner = op.get("submit_owner_epoch", op.get("owner_epoch"))
    if not isinstance(token, str) or not token or type(owner) is not int or owner < 1:
        raise ValueError("Submission requires native durable one-use permit")
    # Admission reserves lifecycle headroom once. Preparation/staging consume it;
    # only the allocation plus a fixed scheduling safety margin must still fit.
    if time.time() + record["seconds"] + SUBMISSION_MARGIN_SECONDS >= op["deadline"]:
        raise ValueError(
            "Frozen operation deadline cannot fit allocation and safety margin"
        )
    values = _source_manifest(directory, record)
    key = hashlib.sha256((op["id"] + ":" + token).encode()).hexdigest()
    record.update(
        token=token,
        owner_epoch=owner,
        job_name="native-" + key[:48],
        comment="native-" + key,
        remote=c["remote_root"].rstrip("/") + "/" + key,
        status="effect_intent",
    )
    # Fsync native identity before mkdir/upload or sbatch can have any effect.
    _write(directory / "journal.json", record)
    remote = record["remote"]
    e = request["parameters"]["execution"]
    inputs = [{**item, **c["input_mappings"][item["source"]]} for item in e["inputs"]]
    run = {
        "remote": remote,
        "execution": e,
        "inputs": inputs,
        "seconds": record["seconds"],
        "deadline": op["deadline"],
    }
    launcher = "RUN=" + repr(run) + "\n" + c["launcher_source"]
    batch = "\n".join(
        [
            "#!/bin/bash",
            "#SBATCH --no-requeue",
            *[
                "#SBATCH --" + key + "=" + str(value)
                for key, value in {
                    "partition": c["partition"],
                    "nodes": 1,
                    "ntasks": 1,
                    "gres": "gpu:1",
                    "cpus-per-task": c["cpus"],
                    "mem": c["memory"],
                    "time": "00:%02d:00" % (record["seconds"] // 60),
                    "output": remote + "/slurm.log",
                    "error": remote + "/slurm.log",
                    "chdir": remote,
                    "deadline": datetime.fromtimestamp(
                        op["deadline"], ZoneInfo(c["slurm_timezone"])
                    ).strftime("%Y-%m-%dT%H:%M:%S"),
                }.items()
            ],
            "set -euo pipefail",
            "test $(date +%s) -lt " + str(int(op["deadline"] - record["seconds"])),
            shlex.join([c["remote_python"], remote + "/launcher.py"]),
            "",
        ]
    )
    bundle = io.BytesIO()
    with tarfile.open(fileobj=bundle, mode="w:gz") as archive:
        for name, value in {
            **{"code/" + name: value for name, value in values.items()},
            "launcher.py": launcher.encode(),
            "inputs.json": json.dumps(inputs).encode(),
            "execution.json": json.dumps(e).encode(),
            "job.sbatch": batch.encode(),
        }.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(value), 0o444
            archive.addfile(info, io.BytesIO(value))
    (directory / "source.tar.gz").write_bytes(bundle.getvalue())
    stage = f"import sys,base64,io,tarfile;from pathlib import Path;p=Path({remote!r});p.mkdir(parents=True,exist_ok=False);(p/'output').mkdir();tarfile.open(fileobj=io.BytesIO(base64.b64decode(sys.stdin.read())),mode='r:gz').extractall(p,filter='data')"
    try:
        transport.run(
            shlex.join([c["remote_python"], "-c", stage]),
            stdin=base64.b64encode(bundle.getvalue()).decode(),
            timeout=_timeout(op, c),
        )
        _gate(c)
        if (
            time.time() + record["seconds"] + SUBMISSION_MARGIN_SECONDS
            >= op["deadline"]
        ):
            raise ValueError("Deadline expired before sbatch")
        record["status"] = "submission_intent"
        _write(directory / "journal.json", record)
        ack = (
            transport.run(
                shlex.join(
                    [
                        "sbatch",
                        "--parsable",
                        "--account=" + c["account"],
                        "--job-name=" + record["job_name"],
                        "--comment=" + record["comment"],
                        remote + "/job.sbatch",
                    ]
                ),
                timeout=_timeout(op, c),
            )
            .strip()
            .split(";")[0]
        )
        if not re.fullmatch(r"[1-9][0-9]*", ack):
            raise ValueError("Unrecognized sbatch acknowledgement")
        record.update(job_id=ack, status="submitted")
        _write(directory / "journal.json", record)
    except Exception:
        return {
            "outcome": "unknown",
            "job_id": record.get("job_id"),
            "proof": None,
            "reason": "Submission effect unresolved; inspect original identity, never retry",
        }
    return {
        "outcome": "accepted",
        "job_id": ack,
        "proof": None,
        "reason": "Single sbatch acknowledgement journaled",
    }


def _collect(transport, directory, record, op, request, c):
    if (
        op.get("state") != "terminal"
        or not op.get("physical_released")
        or op.get("inconsistency") is not None
        or not record.get("job_id")
        or op.get("job_id") != record["job_id"]
    ):
        raise ValueError("Collection requires the same reconciled terminal job")
    retained = directory / "collection.json"
    if retained.exists():
        previous = _read(retained, 2 * 1024 * 1024)
        for evidence in previous["research_evidence"]:
            if (
                _identity(_regular(Path(c["study_root"]) / evidence["ref"]))
                != evidence["identity"]
            ):
                raise ValueError("Retained research evidence metadata changed")
        return previous
    outputs = request["parameters"]["execution"]["outputs"]
    reports = [name for name in outputs if Path(name).suffix in c["report_suffixes"]]
    probe = f"""import base64,json,os,stat
from pathlib import Path
root=Path({record["remote"] + "/output"!r}); result=[];total=0
for name in {outputs!r}:
 p=root/name
 if any(x.is_symlink() for x in [p,*p.parents] if x==root or root in x.parents): raise ValueError('Symlink output')
 if not p.exists(): result.append(dict(name=name,missing=True));continue
 s=p.stat()
 if not stat.S_ISREG(s.st_mode): raise ValueError('Nonregular output')
 item=dict(name=name,size=s.st_size,mtime_ns=s.st_mtime_ns)
 if name in {reports!r}:
  if s.st_size>{c["max_report_bytes"]}: raise ValueError('Report too large')
  total+=s.st_size
  if total>{c["max_collection_bytes"]}: raise ValueError('Collection too large')
  with p.open('rb') as stream: value=stream.read({c["max_report_bytes"]}+1)
  if len(value)!=s.st_size or p.stat().st_mtime_ns!=s.st_mtime_ns: raise ValueError('Report changed during collection')
  item['content']=base64.b64encode(value).decode()
 result.append(item)
print(json.dumps(result))
"""
    rows = json.loads(
        transport.run(
            shlex.join([c["remote_python"], "-c", probe]), timeout=_timeout(op, c, True)
        )
    )
    if not isinstance(rows, list) or [item.get("name") for item in rows] != outputs:
        raise ValueError("Collector changed exact declared outputs")
    artifacts, evidence = [], []
    study = Path(c["study_root"])
    (study / "artifacts").mkdir(parents=True, exist_ok=True)
    total = 0
    for index, item in enumerate(rows):
        name = item["name"]
        if item.get("missing") is True:
            fields(item, {"name", "missing"}, "missing report")
            continue
        fields(
            item,
            {"name", "size", "mtime_ns"} | ({"content"} if name in reports else set()),
            "collected output",
        )
        if any(type(item[k]) is not int or item[k] < 0 for k in ("size", "mtime_ns")):
            raise ValueError("Invalid remote output metadata")
        body = {
            "kind": "unverified_research_report",
            "verified_evaluation": False,
            "operation": record["binding"],
            "job_id": record["job_id"],
            "source": record["source"],
            "name": name,
            "remote_path": record["remote"] + "/output/" + name,
            "size": item["size"],
            "mtime_ns": item["mtime_ns"],
        }
        if name in reports:
            value = base64.b64decode(item["content"], validate=True)
            total += len(value)
            if (
                len(value) != item["size"]
                or len(value) > c["max_report_bytes"]
                or total > c["max_collection_bytes"]
            ):
                raise ValueError("Report bytes exceed declared bounds")
            body["text"] = value.decode("utf-8")
        else:
            body["content_collected"] = False
        ref = "artifacts/" + uuid.uuid4().hex + ".json"
        _write(study / ref, body)
        artifact = {"path": ref, "version": "native-report-" + _digest(body)}
        artifacts.append(artifact)
        evidence.append(
            {
                "name": "output-%02d" % index,
                "artifact": artifact,
                "ref": ref,
                "identity": _identity(study / ref),
                "allow_feedback": True,
            }
        )
    data = {
        "artifacts": artifacts,
        "evaluations": [],
        "research_evidence": evidence,
        "unavailable_reason": "Script outputs are unverified research evidence; no evaluator measurement was created",
    }
    _write(retained, data)
    return data


def invoke(action, operation, request, config, *, transport=None):
    """Return one native action receipt; transport injection is for synthetic tests."""
    action = {
        "submit": "submission",
        "inspect": "inspection",
        "cancel": "cancellation",
        "collect": "collection",
        "settle": "settlement",
    }.get(action, action)
    if action not in {
        "prepare",
        "submission",
        "inspection",
        "cancellation",
        "collection",
        "settlement",
    }:
        raise ValueError("Unknown script backend action")
    op, request, c = json_value(operation), json_value(request), validate_config(config)
    seconds = validate_request(request, c)
    transport = transport or SSHTransport(c["ssh_target"])
    with _journal(op, c) as directory:
        if action == "prepare":
            _gate(c)
            if (directory / "journal.json").exists():
                record = _load(directory, op, request, c)
                _source_manifest(directory, record)
            else:
                values = _sources(request, c)
                checks = list(c["metadata_checks"]) + [
                    {
                        "path": c["input_mappings"][item["source"]]["path"],
                        "kind": c["input_mappings"][item["source"]]["kind"],
                        "identity": None,
                    }
                    for item in request["parameters"]["execution"]["inputs"]
                ]
                _probe(transport, op, c, checks)
                for name, value in values.items():
                    path = directory / "source" / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(value)
                record = {
                    "binding": _binding(op),
                    "request_identity": _digest(request),
                    "config_identity": _digest(c),
                    "created_at": time.time(),
                    "seconds": seconds,
                    "source": {
                        name: hashlib.sha256(value).hexdigest()
                        for name, value in values.items()
                    },
                    "token": None,
                    "status": "prepared",
                }
                _write(directory / "journal.json", record)
            data = {
                "ready": True,
                "reason": "Independent source frozen; metadata-only inputs checked; shared exposure reconciled",
                "effective_parameters": request["parameters"],
            }
        else:
            record = _load(directory, op, request, c)
            if action == "submission":
                data = _submit(transport, directory, record, op, request, c)
            elif action == "collection":
                data = _collect(transport, directory, record, op, request, c)
            else:
                if not record.get("token") or not record.get("job_name"):
                    raise ValueError("No durable original submission identity")
                row = _query(transport, op, c, record)
                if row is not None:
                    record["job_id"] = row[0]
                    _write(directory / "journal.json", record)
                if action == "inspection":
                    outcome = _outcome(row) if row else None
                    data = {
                        "job_id": record.get("job_id"),
                        "terminal": outcome is not None,
                        "outcome": outcome,
                        "physical_released": outcome is not None,
                        "not_submitted_proof": None,
                    }
                elif action == "cancellation":
                    if row is None:
                        data = {
                            "job_id": record.get("job_id"),
                            "acknowledged": False,
                            "reason": "Original scheduler identity unresolved",
                        }
                    elif _outcome(row) is not None:
                        data = {
                            "job_id": row[0],
                            "acknowledged": False,
                            "reason": "Original job already terminal; no cancellation sent",
                        }
                    else:
                        transport.run(
                            shlex.join(
                                [
                                    "scancel",
                                    "--name=" + record["job_name"],
                                    "--user=" + c["slurm_user"],
                                    "--account=" + c["account"],
                                    row[0],
                                ]
                            ),
                            timeout=_timeout(op, c, True),
                        )
                        data = {
                            "job_id": row[0],
                            "acknowledged": True,
                            "reason": "Same positively identified job cancellation acknowledged; release requires inspection",
                        }
                else:
                    outcome = _outcome(row) if row else None
                    if (
                        row is not None
                        and outcome is not None
                        and op.get("state") == "terminal"
                        and (
                            op.get("job_id") != row[0]
                            or op.get("terminal_outcome") != outcome
                        )
                    ):
                        raise ValueError(
                            "Final fees contradict the inspected terminal identity or outcome"
                        )
                    gpu = (
                        re.search(r"(?:^|,)gres/gpu=([0-9]+)(?:,|$)", row[8])
                        if row
                        else None
                    )
                    if gpu is not None and gpu.group(1) != "1":
                        raise ValueError(
                            "Final allocated GPU count contradicts allocation"
                        )
                    if row is not None and row[7] and not row[7].isdigit():
                        raise ValueError("Invalid final allocated elapsed seconds")
                    if (
                        row is None
                        or outcome is None
                        or op.get("state") != "terminal"
                        or row[9] in {"", "Unknown", "None"}
                        or not row[7]
                        or gpu is None
                    ):
                        data = {"final_costs": {}}
                    else:
                        data = {"final_costs": {c["cost_unit"]: int(row[7])}}
        return _event(op, action, data)
