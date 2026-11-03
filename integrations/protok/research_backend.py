"""Isolated, receipted Slurm script execution; never a formal quality evaluation."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import copy
import fcntl
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import time
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from research_loop.research_contract import (
    ExecutionEnvelope,
    ResearchExecution,
    ResearchInput,
)
from research_loop.schema import OutputSchema, choices

from integrations.protok.backend import CANDIDATE_FILES, PROTOCOL, ProTokError
from integrations.protok.continuation import _load_backend, validate_continuation

MAX_OUTPUT_BYTES = 16 * 1024 * 1024
LOG_BYTES = 65536
RESEARCH_ENVIRONMENTS = "/data/user/hongruchen/experiments/ProTok/research-environments"
BASE_PYTHON = "/data/user/hongruchen/projects/ProTok/.venv/bin/python"


def _read(path):
    return json.loads(Path(path).read_text())


def _write(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def _environment(value):
    """Validate bounded operator configuration, without inspecting library contents."""
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "version",
            "id",
            "base_python",
            "python_version",
            "site_packages",
            "packages",
        }
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["id"], str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value["id"])
        or value["base_python"] != BASE_PYTHON
        or value["python_version"] != "3.12.13"
        or value["site_packages"]
        != f"{RESEARCH_ENVIRONMENTS}/{value['id']}/site-packages"
        or not isinstance(value["packages"], list)
        or len(value["packages"]) > 256
    ):
        raise ProTokError("Invalid trusted research environment")
    seen = set()
    for package in value["packages"]:
        if (
            not isinstance(package, dict)
            or set(package) != {"distribution", "import", "version", "source"}
            or any(
                not isinstance(package[key], str)
                or not package[key]
                or len(package[key]) > 256
                for key in ("distribution", "import", "version")
            )
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", package["distribution"])
            or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", package["import"])
            or package["source"] not in ("base", "supplement")
            or package["distribution"].lower().replace("_", "-") in seen
        ):
            raise ProTokError("Invalid trusted research package inventory")
        seen.add(package["distribution"].lower().replace("_", "-"))
    return value


def _relative(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+", value):
        raise ProTokError("Script paths must be bounded relative paths")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {".", ".."} for p in value.split("/")):
        raise ProTokError("Script paths must not traverse directories")
    return value


class ScriptInput(ResearchInput):
    name: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")]


ScriptInputs = Annotated[list[ScriptInput], Field(max_length=16)]
ScriptOutputs = Annotated[list[str], Field(max_length=64)]


class ScriptExecution(ExecutionEnvelope):
    mode: Literal["script"]
    gpu: choices(True)
    time_limit_seconds: Annotated[int, Field(ge=1, le=1800)]
    inputs: ScriptInputs
    outputs: ScriptOutputs


class ProTokPlanExecution(ResearchExecution):
    gpu: choices(True)
    inputs: ScriptInputs
    outputs: ScriptOutputs


SCRIPT_EXECUTION = OutputSchema(ScriptExecution)


def _execution(value, policy):
    try:
        SCRIPT_EXECUTION.validate(value)
    except ValidationError as error:
        raise ProTokError(str(error)) from error
    limit = value["time_limit_seconds"]
    if limit > policy["max_time_limit_seconds"]:
        raise ProTokError("Research time exceeds the authorized debug-partition limit")
    allocated = math.ceil(limit / 60) * 60
    if allocated > policy["max_time_limit_seconds"]:
        raise ProTokError("Rounded Slurm allocation exceeds the research policy")
    entry = _relative(value["entrypoint"])
    if not entry.startswith("research/") or not entry.endswith(".py"):
        raise ProTokError("Research entrypoint must be research/*.py")
    for name in value["outputs"]:
        _relative(name)
    if len(set(value["outputs"])) != len(value["outputs"]):
        raise ProTokError("Duplicate research outputs")
    names = [item["name"] for item in value["inputs"]]
    if len(set(names)) != len(names):
        raise ProTokError("Duplicate research input name")
    return allocated


def _source_identity(source, config):
    """Select and hash only locked/candidate/research source, never Git or artifacts."""
    signatures, total = {}, 0
    names = set(config["locked_files"]) | set(CANDIDATE_FILES)
    research = source / "research"
    if research.is_symlink():
        raise ProTokError("Research source cannot be a symlink")
    if research.exists():
        for path in research.rglob("*"):
            if path.is_symlink():
                raise ProTokError("Research source cannot contain symlinks")
            if not path.is_dir():
                names.add(str(path.relative_to(source)))
    for name in sorted(names):
        _relative(name)
        path = source / name
        if any(
            parent.is_symlink()
            for parent in [path, *path.parents]
            if parent == source or source in parent.parents
        ):
            raise ProTokError("Frozen research source cannot contain symlinks")
        if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            raise ProTokError(
                "Frozen research source contains a non-source/large artifact"
            )
        if path.suffix not in {
            ".py",
            ".json",
            ".md",
            ".txt",
            ".csv",
            ".sh",
            ".toml",
            ".yaml",
            ".yml",
        }:
            raise ProTokError(
                "Frozen research source must contain only small source files"
            )
        total += path.stat().st_size
        if total > 32 * 1024 * 1024 or len(signatures) >= 1024:
            raise ProTokError(
                "Frozen research source exceeds its bounded source envelope"
            )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if name in config["locked_files"] and digest != config["locked_files"][name]:
            raise ProTokError(
                "Frozen locked source differs from canonical source: " + name
            )
        signatures[name] = digest
    return hashlib.sha256(
        json.dumps(signatures, sort_keys=True).encode()
    ).hexdigest(), sorted(names)


def _inputs(root, config, execution, iteration):
    result = []
    for item in execution["inputs"]:
        source = item["source"]
        provenance = {"source": source}
        if source in {"hidden_cache", "targets"}:
            path = config["cache_root" if source == "hidden_cache" else "target_root"]
        elif isinstance(source, str) and source.startswith("long-checkpoint:"):
            match = re.fullmatch(r"long-checkpoint:(L[a-f0-9]{16}):([0-9]+)", source)
            if not match:
                raise ProTokError("Invalid reviewed long checkpoint reference")
            from integrations.protok.long_training import resolve_checkpoint

            run_id, step = match.groups()
            request = _read(root / "study/long-runs" / run_id / "request.json")
            if int(request["source_iteration"]) >= int(iteration):
                raise ProTokError(
                    "Long checkpoint source must precede this investigation"
                )
            resolved = resolve_checkpoint(root, run_id, int(step))
            path = resolved["path"]
            provenance.update(resolved)
        elif source == "teacher":
            path = _read(Path(config["template"]) / config["fixed_config"])[
                "teacher_checkpoint"
            ]
        else:
            match = re.fullmatch(r"checkpoint:([0-9]{4,}):([0-9]+)", source)
            if not match or int(match[1]) >= int(iteration):
                raise ProTokError(
                    "Checkpoint inputs require an earlier published Study iteration"
                )
            source_id, step = match.groups()
            folder = root / "study/iterations" / source_id
            metadata = _read(folder / "iteration.json")
            if (
                metadata["status"] != "reviewed"
                or not (folder / "publication.json").is_file()
            ):
                raise ProTokError("Checkpoint source has no published Review")
            from integrations.protok.run import job_path

            job = job_path(root, source_id)
            record, receipt = _read(job / "job.json"), _read(job / "result.json")
            identity = record["fingerprint"]
            state = _read(
                Path(config["records_root"])
                / record.get("record_name", identity)
                / "state.json"
            )
            summary = state.get("summary", {})
            attempts = state.get("attempts", [])
            if (
                record["status"] != "completed"
                or receipt.get("status") != "completed"
                or receipt.get("fingerprint") != identity
                or state.get("status") != "complete"
                or state.get("fingerprint") != identity
                or summary.get("protocol") != PROTOCOL
                or summary.get("quality_score_eligible") is not True
                or len(attempts) != 1
            ):
                raise ProTokError(
                    "Checkpoint has no matching completed fixed-backend receipt"
                )
            attempt = attempts[0]
            if (
                attempt.get("seed") != 42
                or attempt.get("terminal_state") != "COMPLETED"
                or attempt.get("exit_code") != "0:0"
                or type(attempt.get("gpu_seconds")) not in (int, float)
                or not math.isfinite(attempt["gpu_seconds"])
                or attempt["gpu_seconds"] < 0
                or not re.fullmatch(
                    r"[1-9][0-9]*", str(attempt.get("owned_job_id", ""))
                )
                or int(step) not in {0, 1000, 2000}
            ):
                raise ProTokError(
                    "Checkpoint allocation or requested save step is unverified"
                )
            remote = attempt["remote"]
            if not Path(remote).is_relative_to(Path(config["remote_root"])):
                raise ProTokError(
                    "Checkpoint remote path is outside the canonical experiment root"
                )
            path = remote + f"/train/checkpoint-step-{int(step)}.pt"
            provenance.update(
                iteration=source_id,
                fingerprint=identity,
                owned_job_id=attempt["owned_job_id"],
                receipt=str(job / "result.json"),
                step=int(step),
            )
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or ".." in PurePosixPath(path).parts
        ):
            raise ProTokError("Research input must resolve to a trusted absolute path")
        result.append(
            {
                **item,
                "path": path,
                "mount": "/inputs/" + item["name"],
                "provenance": provenance,
            }
        )
    return result


def _input_metadata(fixed, config, inputs, *, timeout=120):
    """One bounded root-manifest/stat read per input, no tensor/checkpoint reads."""
    probe = f"""import json,os
from pathlib import Path
items={inputs!r}
for item in items:
 path=Path(item["path"]); info=path.stat()
 item["metadata"]={{"size":info.st_size,"mtime_ns":info.st_mtime_ns,"ino":info.st_ino,"resolved_path":str(path.resolve())}}
 if item["source"] in {{"hidden_cache","targets"}}:
  if not path.is_dir(): raise ValueError("Configured cache is not a directory")
  with (path/"manifest.json").open() as stream: text=stream.read(262145)
  if len(text)>262144: raise ValueError("Cache root manifest exceeds 256 KiB")
  manifest=json.loads(text);splits=manifest.get("splits",{{}})
  if set(splits)-{{"train","val"}}: raise ValueError("Cache includes undeclared test/holdout splits")
  if not manifest.get("identity") or manifest.get("complete") is not True: raise ValueError("Configured cache is incomplete or unidentified")
  item["metadata"].update(identity=manifest["identity"],format=manifest.get("format"),splits={{name:meta.get("rows") for name,meta in splits.items()}},exposure="whole configured development cache; no final or holdout use")
 elif not path.is_file(): raise ValueError("Model/checkpoint input is not a regular file")
print(json.dumps(items))
"""
    result = json.loads(
        fixed.ssh(
            config, shlex.join([config["remote_python"], "-c", probe]), timeout=timeout
        )
    )
    if len(result) != len(inputs) or any(
        {k: v for k, v in item.items() if k != "metadata"} != original
        for item, original in zip(result, inputs)
    ):
        raise ProTokError("Input metadata probe changed its admitted inputs")
    return result


def _launcher(config, remote, execution, inputs, environment=None):
    """Trusted launcher with an empty root and explicit read-only mounts."""
    # The shell writes this generated Python launcher; no agent shell is evaluated.
    prefix = "/data/user/hongruchen/projects/ProTok/.venv"
    base = (
        "/data/user/hongruchen/.local/share/uv/python/cpython-3.12.13-linux-x86_64-gnu"
    )
    if config["remote_python"] != prefix + "/bin/python":
        raise ProTokError(
            "Research sandbox requires the verified existing Python environment"
        )
    mounts = [(p, p) for p in ("/usr", "/lib", "/lib64", "/bin", "/sys", prefix, base)]
    # The venv executable targets uv's stable 3.12 alias, which is itself a
    # symlink to the versioned base. Mounting only the resolved base leaves that
    # executable symlink dangling inside the empty sandbox root.
    mounts += [
        (
            base,
            "/data/user/hongruchen/.local/share/uv/python/cpython-3.12-linux-x86_64-gnu",
        )
    ]
    mounts += [
        (remote + "/code", "/workspace"),
        (remote + "/inputs.json", "/inputs.json"),
    ]
    mounts += [(item["path"], item["mount"]) for item in inputs]
    pythonpath = "/workspace:/workspace/scripts"
    if environment is not None:
        environment = _environment(environment)
        mounts += [
            (environment["site_packages"], "/research-libs"),
            (remote + "/environment.json", "/environment.json"),
        ]
        pythonpath += ":/research-libs"
    script = f"""import ctypes,json,os,re,subprocess,sys,xml.etree.ElementTree as ET
visible=os.environ.get("CUDA_VISIBLE_DEVICES", "")
resources={{"CUDA_VISIBLE_DEVICES":visible,"SLURM_JOB_GPUS":os.environ.get("SLURM_JOB_GPUS"),"host_libcuda_paths":[]}}
try:
 driver=ctypes.CDLL("libcuda.so.1")
 with open("/proc/self/maps") as maps:
  resources["host_libcuda_paths"]=sorted({{line.split()[-1] for line in maps if line.split() and os.path.basename(line.split()[-1]).startswith("libcuda.so")}})[:8]
except OSError as error: resources["host_libcuda_error"]=str(error)[:1000]
print("RESEARCH_GPU_RESOURCES "+json.dumps(resources,sort_keys=True),flush=True)
if not visible or "," in visible: raise RuntimeError("Exactly one Slurm-assigned GPU is required")
def query(command):
 def tail(value):
  return (value.decode(errors="replace") if isinstance(value,bytes) else value or "")[-4096:]
 try: result=subprocess.run(command,capture_output=True,text=True,timeout=30)
 except subprocess.TimeoutExpired as error:
  print("RESEARCH_GPU_QUERY_ERROR "+json.dumps({{"command":command,"error":"timeout","stdout_tail":tail(error.stdout),"stderr_tail":tail(error.stderr)}}),flush=True)
  raise RuntimeError("NVIDIA metadata query timed out") from error
 if result.returncode:
  print("RESEARCH_GPU_QUERY_ERROR "+json.dumps({{"command":command,"returncode":result.returncode,"stdout_tail":tail(result.stdout),"stderr_tail":tail(result.stderr)}}),flush=True)
  raise RuntimeError("NVIDIA metadata query failed")
 return result.stdout
row=query(["nvidia-smi","--query-gpu=index,uuid","--format=csv,noheader","-i",visible]).strip().splitlines()
if len(row)!=1: raise RuntimeError("GPU identity is ambiguous")
index,uuid=[x.strip() for x in row[0].split(",")]
if not re.fullmatch(r"[0-9]+",index) or not uuid.startswith("GPU-"): raise RuntimeError("Unsupported GPU identity")
gpus=ET.fromstring(query(["nvidia-smi","-q","-x","-i",visible])).findall("gpu")
if len(gpus)!=1 or (gpus[0].findtext("uuid") or "").strip()!=uuid: raise RuntimeError("XML GPU identity differs from the assigned GPU")
minor=(gpus[0].findtext("minor_number") or "").strip()
if not re.fullmatch(r"[0-9]+",minor): raise RuntimeError("GPU XML has no usable device minor number")
resources.update(index=index,uuid=uuid,minor_number=minor,device="/dev/nvidia"+minor)
print("RESEARCH_GPU_RESOURCES "+json.dumps(resources,sort_keys=True),flush=True)
args=["/bin/bwrap","--unshare-user","--unshare-pid","--as-pid-1","--unshare-net","--unshare-ipc","--unshare-uts","--die-with-parent","--new-session","--cap-drop","ALL","--proc","/proc","--dev","/dev","--tmpfs","/tmp","--dir","/tmp/home","--dir","/inputs"]
for src,dst in {mounts!r}: args += ["--ro-bind",src,dst]
if os.path.isfile("/etc/ld.so.cache"): args += ["--ro-bind","/etc/ld.so.cache","/etc/ld.so.cache"]
args += ["--bind",{remote + "/output"!r},"/output"]
for dev in ["/dev/nvidiactl","/dev/nvidia"+minor,"/dev/nvidia-uvm","/dev/nvidia-uvm-tools"]:
 if os.path.exists(dev): args += ["--dev-bind",dev,dev]
for key,value in {{"HOME":"/tmp/home","PATH":"/usr/bin:/bin","TMPDIR":"/tmp","PYTHONPATH":{pythonpath!r},"PYTHONDONTWRITEBYTECODE":"1","PYTHONUNBUFFERED":"1","OMP_NUM_THREADS":"1","CUDA_VISIBLE_DEVICES":uuid,"TORCHINDUCTOR_CACHE_DIR":"/tmp/torch","TRITON_CACHE_DIR":"/tmp/triton","MPLBACKEND":"Agg","MPLCONFIGDIR":"/tmp/matplotlib","NUMBA_CACHE_DIR":"/tmp/numba","JOBLIB_TEMP_FOLDER":"/tmp/joblib","RESEARCH_LOOP_EVENT_FILE":"/output/events.jsonl"}}.items(): args += ["--setenv",key,value]
args += ["--chdir","/output",{config["remote_python"]!r},"-B",{str(PurePosixPath("/workspace") / execution["entrypoint"])!r}]
os.execve(args[0],args,{{"PATH":"/usr/bin:/bin"}})
"""
    return script


def _batch(config, remote, execution, inputs, allocated, environment=None):
    launcher = _launcher(config, remote, execution, inputs, environment)
    directives = {
        "partition": "debug",
        "nodes": 1,
        "ntasks": 1,
        "gres": "gpu:1",
        "cpus-per-task": config["cpus"],
        "mem": config["mem"],
        "time": f"00:{allocated // 60:02d}:00",
        "output": remote + "/slurm.log",
        "error": remote + "/slurm.log",
        "chdir": remote,
    }
    if any(
        not re.fullmatch(r"[A-Za-z0-9_./:+-]+", str(v)) for v in directives.values()
    ):
        raise ProTokError("Unsafe research Slurm directive")
    return "\n".join(
        [
            "#!/bin/bash",
            *[f"#SBATCH --{k}={v}" for k, v in directives.items()],
            "set -euo pipefail",
            "ulimit -f 16384",
            "exec >" + shlex.quote(remote + "/run.log") + " 2>&1",
            shlex.quote(config["remote_python"]) + " - <<'RESEARCH_LAUNCHER'",
            launcher,
            "RESEARCH_LAUNCHER",
            "",
        ]
    )


def _collect(fixed, config, remote, outputs, record):
    collector = f"""import base64,json,os,stat
from pathlib import Path
root=Path({remote + "/output"!r}); result={{"files":[],"errors":[]}}; total=0
for name in {outputs!r}:
 try:
  path=root/name
  if any(p.is_symlink() for p in [path,*path.parents] if p==root or root in p.parents): raise ValueError("symlink output")
  info=path.stat()
  if not stat.S_ISREG(info.st_mode): raise ValueError("nonregular output")
  total+=info.st_size
  if total>{MAX_OUTPUT_BYTES}: raise ValueError("declared output total exceeds 16 MiB")
  fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
  with os.fdopen(fd,"rb") as f: data=f.read(info.st_size+1)
  if len(data)!=info.st_size: raise ValueError("output size changed")
  result["files"].append({{"name":name,"data":base64.b64encode(data).decode()}})
 except Exception as e: result["errors"].append(name+": "+str(e))
try:
 with open({remote + "/run.log"!r},"rb") as f:
  f.seek(0,2); size=f.tell(); f.seek(max(0,size-{LOG_BYTES})); result["stdout_tail"]=base64.b64encode(f.read({LOG_BYTES})).decode()
except OSError as e: result["stdout_tail"]="";result["errors"].append("stdout: "+str(e))
print(json.dumps(result))
"""
    result = json.loads(
        fixed.ssh(
            config, shlex.join([config["remote_python"], "-c", collector]), timeout=120
        )
    )
    destination = record / "artifacts"
    destination.mkdir(exist_ok=True)
    paths, total = [], 0
    for item in result["files"]:
        name = _relative(item["name"])
        if name not in outputs:
            raise ProTokError("Remote collector returned an undeclared artifact")
        data = base64.b64decode(item["data"], validate=True)
        total += len(data)
        if total > MAX_OUTPUT_BYTES:
            raise ProTokError("Remote research output exceeds collection cap")
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        paths.append(str(path))
    tail = base64.b64decode(result["stdout_tail"], validate=True)
    if len(tail) > LOG_BYTES:
        raise ProTokError("Remote stdout exceeds tail cap")
    (record / "stdout-tail.log").write_bytes(tail)
    return paths, result["errors"]


@dataclass(frozen=True)
class PreparedScript:
    root: Path
    iteration: str
    owner: dict
    continuation: dict
    source_identity: str
    execution: dict
    deadline: float
    policy: dict
    inputs: list[dict]
    environment: dict | None
    config: dict


def prepare_script(
    backend, *, root, iteration, source, execution, deadline, cancel_event=None
):
    """Validate source and bounded input metadata without retaining effects.

    This is also the dry-run API. It neither stages files nor claims/submits a
    job. Trusted configuration, input provenance and accounting errors block.
    """
    root, source = Path(root).resolve(), Path(source).resolve()
    if not re.fullmatch(r"[0-9]{4,}", iteration) or not isinstance(execution, dict):
        raise ProTokError("Invalid research iteration/execution")
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ProTokError("Finite original research deadline required")
    control = root / "control"
    policy = _read(control / "research-policy.json")
    if (
        policy.get("version") != 1
        or not isinstance(policy.get("authorization"), str)
        or not policy["authorization"].strip()
        or type(policy.get("max_time_limit_seconds")) is not int
    ):
        raise ProTokError("Explicit version 1 research policy required")
    _execution(execution, policy)
    from .research_execution import authored_source

    authored_source(source, execution["entrypoint"])
    fixed, config, _ = validate_continuation(
        backend.project,
        control,
        backend.owner_token,
        backend.project / "autoresearch-run.json",
    )
    identity, _ = _source_identity(source, config)
    if cancel_event is not None and cancel_event.is_set():
        raise ProTokError("Research preparation cancelled")
    if time.time() >= deadline:
        raise ProTokError("Research preparation exceeded the original deadline")
    inputs = _input_metadata(
        fixed,
        config,
        _inputs(root, config, execution, iteration),
        timeout=min(120, max(0.001, deadline - time.time())),
    )
    environment_path = control / "research-environment.json"
    environment = None
    if environment_path.exists():
        if environment_path.stat().st_size > 256 * 1024:
            raise ProTokError("Trusted research environment exceeds 256 KiB")
        environment = _environment(_read(environment_path))
    remote = (
        config["remote_root"]
        + "/research-loop-scripts/"
        + iteration
        + "-"
        + identity[:16]
    )
    _batch(
        config, remote, execution, inputs, _execution(execution, policy), environment
    )
    if time.time() >= deadline or (cancel_event is not None and cancel_event.is_set()):
        raise ProTokError("Research preparation exceeded its original execution window")
    return PreparedScript(
        root,
        iteration,
        _read(control / "ownership.json"),
        _read(control / "continuation.json"),
        identity,
        copy.deepcopy(execution),
        deadline,
        policy,
        inputs,
        environment,
        copy.deepcopy(config),
    )


def run_script(
    backend,
    *,
    root,
    iteration,
    source,
    execution,
    deadline,
    cancel_event=None,
    recover=False,
    prepared=None,
):
    """Run one script allocation or reconcile its same owned Slurm job, never resubmit."""
    root, source = Path(root).resolve(), Path(source).resolve()
    if not re.fullmatch(r"[0-9]{4,}", iteration) or not isinstance(execution, dict):
        raise ProTokError("Invalid research iteration/execution")
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ProTokError("Finite original research deadline required")
    control = root / "control"
    from .round_admission import (
        active_round,
        execution_deadline,
        freeze_execution_deadline,
    )

    policy = _read(control / "research-policy.json")
    if (
        policy.get("version") != 1
        or not isinstance(policy.get("authorization"), str)
        or not policy["authorization"].strip()
        or type(policy.get("max_time_limit_seconds")) is not int
    ):
        raise ProTokError("Explicit version 1 research policy required")
    allocated = _execution(execution, policy)
    record = control / "research-jobs" / iteration
    if not recover and prepared is None and not (record / "job.json").exists():
        prepared = prepare_script(
            backend,
            root=root,
            iteration=iteration,
            source=source,
            execution=execution,
            deadline=deadline,
            cancel_event=cancel_event,
        )
    identity, source_names = _source_identity(source, backend._config())
    if prepared is not None and (
        prepared.root != root
        or prepared.iteration != iteration
        or prepared.source_identity != identity
        or prepared.execution != execution
        or prepared.deadline != deadline
        or prepared.policy != policy
    ):
        raise ProTokError("Research source/contract changed after preparation")
    if not (source / execution["entrypoint"]).is_file():
        raise ProTokError("Research entrypoint missing from frozen source")
    record = control / "research-jobs" / iteration
    with (control / "protok.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        existing = (
            _read(record / "job.json") if (record / "job.json").exists() else None
        )
        lease = _read(control / "continuation.json")
        owner = _read(control / "ownership.json")
        if prepared is not None and (
            prepared.owner != owner or prepared.continuation != lease
        ):
            raise ProTokError("Research authorization changed after preparation")
        if existing:
            if (
                existing["source_identity"] != identity
                or existing["execution"] != execution
                or existing["deadline"] != deadline
                or existing["policy"] != policy
                or existing["owner"] != owner
                or existing["continuation"] != lease
            ):
                raise ProTokError(
                    "Retained research job identity/authorization changed"
                )
            if (record / "receipt.json").exists():
                from .telemetry import drain_existing

                try:
                    fixed, _ = _load_backend(backend.project, lease)
                    drain_existing(
                        root / "study",
                        "research-" + iteration,
                        {
                            "kind": "research",
                            "experiment_id": iteration,
                            "source_identity": identity,
                            "execution_identity": existing["execution_identity"],
                        },
                        existing["base_config"],
                        existing["remote"] + "/output/events.jsonl",
                        ssh=fixed.ssh,
                    )
                except Exception:
                    pass
                return _read(record / "receipt.json")
            if not recover:
                raise ProTokError(
                    "Research submission has retained effects; recover the same job"
                )
            fixed, config = _load_backend(backend.project, lease)
            config = fixed.load_config(backend.project / "autoresearch-run.json")
            if config != existing["base_config"]:
                raise ProTokError(
                    "Research recovery configuration differs from its owned allocation"
                )
            state = _read(record / "state.json")
            attempt = state["attempts"][0]
            if attempt["status"] == "prepared":
                raise ProTokError(
                    "Recovery cannot submit a retained prepared research job"
                )
            inputs = existing["inputs"]
            environment = existing.get("environment")
            if environment is not None:
                _environment(environment)
        else:
            if recover:
                raise ProTokError("No retained research job to recover")
            fixed, config, admitted = validate_continuation(
                backend.project,
                control,
                backend.owner_token,
                backend.project / "autoresearch-run.json",
            )
            current_round = active_round(root, owner_token=backend.owner_token)
            if current_round:
                from .research_execution import frozen_contract

                if (
                    current_round["iteration"] != iteration
                    or frozen_contract(root, iteration)["execution"] != execution
                ):
                    raise ProTokError(
                        "Research execution differs from its admitted frozen Plan"
                    )
                plan = _read(
                    root / "study" / _read(root / "study/state.json")["plan_ref"]
                )
                if deadline > time.time() + min(3600, plan["timeout_seconds"]):
                    raise ProTokError(
                        "Research deadline exceeds its frozen Plan timeout"
                    )
                window = freeze_execution_deadline(
                    root,
                    deadline,
                    owner_token=backend.owner_token,
                    attempt=f"research/{iteration}",
                )
                if window != deadline:
                    raise ProTokError(
                        "Research deadline differs from its frozen attempt window"
                    )
            elif deadline > admitted["effective_deadline"]:
                raise ProTokError("Research cannot extend the original lease")
            status = backend.inspect()
            spent = status["used_gpu_seconds"] - lease["initial_gpu_seconds"]
            if (
                status["outstanding"]
                or status.get("research_outstanding")
                or status.get("debug_outstanding")
                or spent < 0
                or spent + allocated + config["reserve_gpu_seconds"]
                > lease["additional_gpu_seconds"]
                or deadline - time.time() < allocated
                or (cancel_event is not None and cancel_event.is_set())
            ):
                raise ProTokError(
                    "Research allocation does not fit the original settled budget/deadline"
                )
            if prepared.config != config:
                raise ProTokError("Trusted configuration changed after preparation")
            inputs, environment = prepared.inputs, prepared.environment
            environment_path = control / "research-environment.json"
            current_environment = (
                _environment(_read(environment_path))
                if environment_path.exists()
                else None
            )
            if current_environment != environment:
                raise ProTokError("Trusted environment changed after preparation")
            from .telemetry import writer_source

            event_writer, event_writer_identity = writer_source()
            execution_identity = hashlib.sha256(
                json.dumps(
                    {
                        "source_identity": identity,
                        "execution": execution,
                        "inputs": inputs,
                        "iteration": iteration,
                        "event_writer_identity": event_writer_identity,
                        **(
                            {"environment": environment}
                            if environment is not None
                            else {}
                        ),
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            remote = (
                config["remote_root"]
                + "/research-loop-scripts/"
                + iteration
                + "-"
                + identity[:16]
            )
            _batch(
                config, remote, execution, inputs, allocated, environment
            )  # Validate before publishing effects.
            record.mkdir(parents=True)
            existing = {
                "purpose": "research",
                "quality_score_eligible": False,
                "source_identity": identity,
                "execution_identity": execution_identity,
                "event_writer_identity": event_writer_identity,
                "iteration": iteration,
                "execution": execution,
                "gpu": True,
                "allocated_seconds": allocated,
                "status": "prepared",
                "remote": remote,
                "deadline": deadline,
                "inputs": inputs,
                "policy": policy,
                "base_config": config,
                "owner": owner,
                "continuation": lease,
                **({"environment": environment} if environment is not None else {}),
            }
            attempt = {
                "seed": 42,
                "directory": "allocation",
                "mode": "research",
                "budget_seconds": allocated,
                "status": "prepared",
                "remote": remote,
            }
            state = {"purpose": "research", "status": "prepared", "attempts": [attempt]}
            _write(record / "job.json", existing)
            _write(record / "state.json", state)
            if environment is not None:
                _write(record / "environment.json", environment)
        wall_deadline = execution_deadline(
            root,
            deadline,
            3600,
            owner_token=backend.owner_token,
            attempt=f"research/{iteration}",
        )
        from research_loop.diagnostics import CommandDiagnostics, redact_text

        diagnostics = CommandDiagnostics(
            record / "command-diagnostics", secrets=(backend.owner_token,)
        )
        diagnostics.install(fixed)
        original_ssh = fixed.ssh
        watching = True

        def live():
            if (
                (control / "STOP").exists()
                or time.time() >= wall_deadline
                or (cancel_event is not None and cancel_event.is_set())
                or _read(control / "ownership.json") != owner
                or _read(control / "continuation.json") != lease
            ):
                raise KeyboardInterrupt(
                    "Research stopped or original lease changed/expired"
                )

        def ssh(config, command, **kwargs):
            if watching and re.fullmatch(r"squeue -h -j [1-9][0-9]* -o %T", command):
                live()
            return original_ssh(config, command, **kwargs)

        fixed.ssh = ssh
        from .telemetry import job_relay

        relay = job_relay(
            root / "study",
            "research-" + iteration,
            {
                "kind": "research",
                "experiment_id": iteration,
                "source_identity": identity,
                "execution_identity": existing["execution_identity"],
            },
            original_ssh,
            config,
            existing["remote"] + "/output/events.jsonl",
        )
        error, outputs = None, []
        try:
            if not recover:
                live()
                staging = record / "staging"
                staging.mkdir()
                (staging / "code").mkdir()
                for name in source_names:
                    destination = staging / "code" / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source / name, destination)
                from .telemetry import stage_writer

                stage_writer(staging / "code", event_writer)
                (staging / "code/autoresearch-se3.sbatch").write_text(
                    _batch(
                        config,
                        existing["remote"],
                        execution,
                        inputs,
                        allocated,
                        environment,
                    )
                )
                if environment is not None:
                    _write(staging / "environment.json", environment)
                _write(
                    staging / "inputs.json",
                    {
                        "inputs": inputs,
                        "source_identity": identity,
                        "purpose": "research",
                        "execution_identity": existing["execution_identity"],
                        "instructions": "Read each input through its mount field under /inputs; path is host provenance and is not a sandbox path. Configured full caches are exposed read-only only for declared development/exploratory use, never final evaluation.",
                    },
                )
                fixed.ssh(
                    config,
                    "mkdir -p "
                    + shlex.quote(str(Path(existing["remote"]).parent))
                    + " && mkdir "
                    + shlex.quote(existing["remote"])
                    + " && mkdir "
                    + shlex.quote(existing["remote"] + "/output"),
                )
                fixed.run(
                    [
                        "rsync",
                        "-a",
                        "-e",
                        "ssh -o BatchMode=yes -o ClearAllForwardings=yes",
                        str(staging) + "/",
                        config["ssh_host"] + ":" + existing["remote"] + "/",
                    ],
                    timeout=600,
                )
                live()
                if deadline - time.time() < allocated:
                    raise ProTokError(
                        "Staging exhausted the original allocation window"
                    )
                fixed.submit(config, record, state, attempt)
            if attempt["status"] in {"submitting", "running"}:
                watching = not recover or bool(
                    active_round(root, owner_token=backend.owner_token)
                )
                fixed.wait_for_job(
                    config,
                    {
                        "deadline": min(wall_deadline, time.time())
                        if recover
                        and not active_round(root, owner_token=backend.owner_token)
                        else wall_deadline
                    },
                    record,
                    state,
                    attempt,
                )
            if attempt["status"] not in {"finished", "complete"}:
                raise ProTokError("Research allocation did not complete successfully")
        except BaseException as failure:
            error = diagnostics.record_error(failure)
            watching = False
            if attempt["status"] in {"submitting", "running"}:
                fixed.cancel_interrupted(config, record, state)
                try:
                    fixed.wait_for_job(
                        config, {"deadline": time.time()}, record, state, attempt
                    )
                except BaseException as reconciliation:
                    error += "; reconciliation: " + diagnostics.record_error(
                        reconciliation, stage="reconciliation"
                    )
        finally:
            fixed.ssh = original_ssh
            if relay is not None:
                relay.close()
        settled = all(
            key in attempt
            for key in ("gpu_seconds", "terminal_state", "exit_code", "owned_job_id")
        )
        unsubmitted = attempt["status"] == "prepared"
        cpu_seconds = None
        if settled:
            try:
                job_id = str(attempt["owned_job_id"])
                if not re.fullmatch(r"[1-9][0-9]*", job_id):
                    raise ProTokError("Invalid owned research job ID")
                rows = (
                    fixed.ssh(
                        config,
                        f"sacct -n -X -j {job_id} --format=JobIDRaw,CPUTimeRAW --parsable2",
                    )
                    .strip()
                    .splitlines()
                )
                rows = [
                    line.split("|") for line in rows if line.split("|", 1)[0] == job_id
                ]
                if (
                    len(rows) != 1
                    or len(rows[0]) < 2
                    or not re.fullmatch(r"[0-9]+", rows[0][1])
                ):
                    raise ProTokError("Missing unambiguous allocated CPU accounting")
                cpu_seconds = int(rows[0][1])
            except Exception as cpu_accounting:
                error = (
                    (error + "; " if error else "")
                    + "CPU accounting: "
                    + diagnostics.record_error(cpu_accounting, stage="accounting")
                )
            try:
                outputs, problems = _collect(
                    fixed, config, existing["remote"], execution["outputs"], record
                )
                if problems:
                    error = (error + "; " if error else "") + "; ".join(problems)
            except Exception as collection:
                error = (
                    (error + "; " if error else "")
                    + "output collection: "
                    + diagnostics.record_error(collection, stage="collection")
                )
        if settled:
            from research_loop.diagnostics import fetch_remote_log

            fetch_remote_log(fixed, config, existing["remote"])
        seconds = attempt.get("gpu_seconds", 0 if unsubmitted else None)
        if seconds is not None and (
            type(seconds) not in (int, float)
            or not math.isfinite(seconds)
            or seconds < 0
            or int(seconds) != seconds
        ):
            settled, unsubmitted, seconds = False, False, None
            error = (
                error + "; " if error else ""
            ) + "Invalid authoritative allocation accounting"
        receipt = {
            "purpose": "research",
            "quality_score_eligible": False,
            "status": "completed"
            if settled and error is None
            else "failed"
            if settled or unsubmitted
            else "blocked",
            "source_identity": identity,
            "execution_identity": existing["execution_identity"],
            "iteration": iteration,
            "gpu": True,
            "allocated_seconds": allocated,
            "gpu_seconds": int(seconds) if seconds is not None else None,
            "cpu_seconds": cpu_seconds if settled else 0 if unsubmitted else None,
            "cpu_accounting": "Slurm CPUTimeRAW allocated CPU-seconds; not process CPU utilization",
            "owned_job_id": attempt.get("owned_job_id"),
            "terminal_state": attempt.get("terminal_state"),
            "exit_code": attempt.get("exit_code"),
            "inputs": inputs,
            **({"environment": environment} if environment is not None else {}),
            "outputs": outputs,
            "evidence": [
                str(record / "job.json"),
                str(record / "state.json"),
                *(
                    [str(record / "environment.json")]
                    if environment is not None
                    else []
                ),
                *(
                    [str(record / "stdout-tail.log")]
                    if (record / "stdout-tail.log").exists()
                    else []
                ),
            ],
            "error": redact_text(error, secrets=(backend.owner_token,))
            if error
            else None,
            "diagnostics": diagnostics.view(),
        }
        existing["status"] = receipt["status"]
        _write(record / "job.json", existing)
        _write(
            record
            / ("blocked.json" if receipt["status"] == "blocked" else "receipt.json"),
            receipt,
        )
        return receipt
