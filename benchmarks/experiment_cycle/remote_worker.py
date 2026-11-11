"""Validation primitives for the trusted remote benchmark worker.

Training requires an explicit operator config, an idle fixed GPU and --allow-gpu.
"""

from __future__ import annotations

import ast
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

if __package__:
    from .interventions import verify_intervention
else:
    from interventions import verify_intervention

# Only numerical layer construction is admitted; no user-supplied callables.
_LAYERS = frozenset(
    {
        "Conv2d",
        "Linear",
        "ReLU",
        "LeakyReLU",
        "GELU",
        "SiLU",
        "Sigmoid",
        "Tanh",
        "BatchNorm2d",
        "GroupNorm",
        "LayerNorm",
        "MaxPool2d",
        "AvgPool2d",
        "AdaptiveAvgPool2d",
        "AdaptiveMaxPool2d",
        "Dropout",
        "Dropout2d",
        "Flatten",
        "Identity",
    }
)
_MAX_SOURCE_BYTES = 256 * 1024


def _number_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return type(node.value) in (int, float, bool) and (
            not isinstance(node.value, float) or abs(node.value) != float("inf")
        )
    if isinstance(node, (ast.Tuple, ast.List)):
        return all(_number_literal(item) for item in node.elts)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _number_literal(node.operand)
    return False


def validate_candidate(baseline_source: str, candidate_source: str) -> None:
    """Require identical module AST except a restricted tiny_cnn return body."""
    if (
        not isinstance(candidate_source, str)
        or len(candidate_source.encode()) > _MAX_SOURCE_BYTES
    ):
        raise ValueError("candidate source is missing or too large")
    try:
        baseline = ast.parse(baseline_source)
        candidate = ast.parse(candidate_source)
    except (SyntaxError, ValueError) as exc:
        raise ValueError("invalid Python source") from exc

    def model(tree: ast.Module) -> ast.FunctionDef:
        matches = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "tiny_cnn"
        ]
        if len(matches) != 1:
            raise ValueError("exactly one top-level tiny_cnn is required")
        return matches[0]

    old, new = model(baseline), model(candidate)
    if len(new.body) != 1 or not isinstance(new.body[0], ast.Return):
        raise ValueError("tiny_cnn must contain only a return")
    sequence = new.body[0].value
    if (
        not isinstance(sequence, ast.Call)
        or not _nn_call(sequence, "Sequential")
        or sequence.keywords
    ):
        raise ValueError("tiny_cnn must return nn.Sequential")
    for layer in sequence.args:
        if not isinstance(layer, ast.Call) or not _nn_call(layer):
            raise ValueError("only whitelisted nn layer constructors are allowed")
        if any(not _number_literal(arg) for arg in layer.args) or any(
            kw.arg is None or not _number_literal(kw.value) for kw in layer.keywords
        ):
            raise ValueError("layer arguments must be numerical literals")
    old.body = [ast.Pass()]
    new.body = [ast.Pass()]
    if ast.dump(baseline, include_attributes=False) != ast.dump(
        candidate, include_attributes=False
    ):
        raise ValueError("changes outside the tiny_cnn body are forbidden")


def _nn_call(node: ast.Call, name: str | None = None) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "nn"
        and (func.attr == name if name else func.attr in _LAYERS)
    )


# Everything below is an operator-owned execution path, never loaded from strategy.


def write_record(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2))
    temporary.replace(path)


def operator_config(project, gpu_uuid=None):
    config = json.loads(
        (project / "evidence/benchmark-collection/config.json").read_text()
    )
    if gpu_uuid is not None:
        profiles = config.get("gpu_profiles", {})
        if gpu_uuid in profiles:
            profile = profiles[gpu_uuid]
            if not isinstance(profile, dict) or set(profile) != {
                "gpu_index",
                "gpu_lock",
            }:
                raise ValueError("invalid operator GPU profile")
            config.update(profile, gpu_uuid=gpu_uuid)
        elif gpu_uuid != config.get("gpu_uuid"):
            raise ValueError("GPU is not in the operator allowlist")
    for key in (
        "gpu_uuid",
        "gpu_index",
        "data_root",
        "controller_root",
        "python",
        "baseline_script",
        "grade_script",
    ):
        if key not in config:
            raise ValueError(f"missing operator setting {key}")
    for key in (
        "data_root",
        "controller_root",
        "python",
        "baseline_script",
        "grade_script",
    ):
        if not Path(config[key]).is_absolute() or not Path(config[key]).exists():
            raise ValueError(f"operator path unavailable: {key}")
    if type(config["gpu_index"]) is not int or config["gpu_index"] < 0:
        raise ValueError("invalid GPU index")
    if (
        config.get("gpu_lock") is not None
        and not Path(config["gpu_lock"]).is_absolute()
    ):
        raise ValueError("GPU lock must be an absolute path")
    return config


def preflight(config):
    query = subprocess.run(
        [
            "nvidia-smi",
            "-i",
            config["gpu_uuid"],
            "--query-gpu=index,uuid,memory.used,memory.free",
            "--format=csv,noheader,nounits",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    fields = [field.strip() for field in query.stdout.strip().split(",")]
    if (
        len(fields) != 4
        or int(fields[0]) != config["gpu_index"]
        or fields[1] != config["gpu_uuid"]
    ):
        raise ValueError("GPU identity changed")
    used, free = int(fields[2]), int(fields[3])
    processes = subprocess.run(
        [
            "nvidia-smi",
            "-i",
            config["gpu_uuid"],
            "--query-compute-apps=pid",
            "--format=csv,noheader,nounits",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    idle = used < 100 and not processes.stdout.strip()
    return {
        "ready": idle and free >= 8192,
        "gpu_uuid": fields[1],
        "memory_used_mib": used,
        "memory_free_mib": free,
        "reason": "ready"
        if idle and free >= 8192
        else "selected GPU is occupied; no job started",
    }


def execution_command(config, code_dir, writable_dir, source, output):
    """Direct operator-authorized execution; no filesystem/network sandbox."""
    argv = [
        config["python"],
        "-I",
        str(code_dir / "launch.py"),
        str(source),
        "--data",
        config["data_root"],
        "--output",
        str(output),
        "--epochs",
        "1",
        "--seed",
        "0",
        "--device",
        "cuda:0",
        "--model",
        "tiny-cnn",
        "--batch-size",
        "128",
        "--lr",
        "0.001",
        "--threads",
        "4",
    ]
    environment = {
        "PATH": os.defpath,
        "HOME": str(writable_dir),
        "TMPDIR": str(writable_dir),
        "CUDA_VISIBLE_DEVICES": config["gpu_uuid"],
        "OMP_NUM_THREADS": "4",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return argv, environment


def run_operation(project, identity, request, gpu_uuid=None):
    config = (
        operator_config(project, gpu_uuid)
        if gpu_uuid is not None
        else operator_config(project)
    )
    ready = preflight(config)
    if not ready["ready"]:
        raise RuntimeError(ready["reason"])
    source = request.get("candidate_source")
    baseline = Path(config["baseline_script"]).read_text()
    validate_candidate(baseline, source)
    intervention_id = request.get("intervention_id")
    verify_intervention(baseline, source, intervention_id)
    root = project / "evidence/benchmark-baseline" / identity
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    config_copy = dict(config)
    write_record(root / "operator.json", config_copy)
    write_record(root / "request.json", request)
    write_record(root / "state.json", {"status": "admitted", "operation_id": identity})
    lock_path = Path(
        config.get(
            "gpu_lock", str(project / f"evidence/gpu-{config['gpu_index']}.lock")
        )
    )
    active = None
    receipt = {
        "exit_code": 1,
        "gpu_uuid": config["gpu_uuid"],
        "operation_id": identity,
        "execution_mode": "direct",
        "intervention_id": intervention_id,
        "intervention_verified": True,
    }
    try:
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            ready = preflight(config)
            if not ready["ready"]:
                raise RuntimeError(ready["reason"])
            for name, text in (("baseline", baseline), ("candidate", source)):
                code = root / f"{name}-code"
                code.mkdir()
                script = code / "train.py"
                script.write_text(text)
                (code / "launch.py").write_text(
                    "import sys,runpy,torch\ntorch.cuda.set_per_process_memory_fraction(0.25,0)\nsys.argv=sys.argv[1:]\nrunpy.run_path(sys.argv[0],run_name='__main__')\n"
                )
                writable = root / f"{name}-output-root"
                writable.mkdir()
                output = writable / "result"
                argv, environment = execution_command(
                    config, code, writable, script, output
                )
                begun = time.monotonic()
                with (root / f"{name}.log").open("wb") as log:
                    active = subprocess.Popen(
                        argv,
                        env=environment,
                        cwd=code,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    write_record(
                        root / "state.json",
                        {
                            "status": "running",
                            "operation_id": identity,
                            "phase": name,
                            "pid": active.pid,
                            "gpu_uuid": config["gpu_uuid"],
                        },
                    )
                    try:
                        code_value = active.wait(timeout=600)
                    except BaseException:
                        if active.poll() is None:
                            os.killpg(active.pid, signal.SIGKILL)
                            active.wait(timeout=10)
                        raise
                    # Retain the process handle if cleanup itself fails: the
                    # outer handler must retry cleanup and report uncertainty.
                    active = None
                item = {
                    "exit_code": code_value,
                    "operation_id": f"{identity}-{name}",
                    "wall_seconds": time.monotonic() - begun,
                }
                receipt[name] = item
                if code_value:
                    raise RuntimeError(
                        f"{name} training exited {code_value}; inspect its durable log"
                    )
                grading = subprocess.run(
                    [
                        config["python"],
                        config["grade_script"],
                        "--controller",
                        config["controller_root"],
                        "--submission",
                        str(output / "dev-predictions.csv"),
                        "--split",
                        "dev",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                score = json.loads(grading.stdout)
                if score.get("metric") != "roc_auc" or score.get("split") != "dev":
                    raise ValueError("invalid fixed grader receipt")
                item.update(score)
                write_record(root / f"{name}-receipt.json", item)
            receipt["exit_code"] = 0
    except BaseException as error:
        receipt["error"] = f"{type(error).__name__}: {error}"
        if active is not None and active.poll() is None:
            try:
                os.killpg(active.pid, signal.SIGKILL)
                active.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired) as cleanup:
                receipt["cleanup_uncertain"] = str(cleanup)
    finally:
        write_record(root / "receipt.json", receipt)
        write_record(
            root / "state.json",
            {
                "status": "uncertain"
                if receipt.get("cleanup_uncertain")
                else "completed"
                if receipt["exit_code"] == 0
                else "failed",
                "operation_id": identity,
            },
        )
    return receipt


def operation_status(root):
    path = root / "receipt.json"
    return json.loads((path if path.exists() else root / "state.json").read_text())


def reconcile_operation(project, identity):
    """Atomically fence an unadmitted identity, or return its original state."""
    root = project / "evidence/benchmark-baseline" / identity
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        return operation_status(root)
    # run_operation must win this same mkdir before any training starts. A late
    # dispatch therefore cannot launch after we have certified no admission.
    receipt = {
        "status": "not_dispatched",
        "operation_id": identity,
        "exit_code": 1,
        "dispatch_fenced": True,
        "execution_mode": "direct",
    }
    write_record(root / "receipt.json", receipt)
    write_record(root / "state.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(
        description="Operator-owned fixed single-card Cactus execution"
    )
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("action", choices=("preflight", "run", "status", "reconcile"))
    parser.add_argument("operation_id", nargs="?")
    parser.add_argument("--allow-gpu", action="store_true")
    parser.add_argument("--gpu-uuid", help="Select an operator-allowlisted GPU")
    args = parser.parse_args()

    def interrupted(signum, frame):
        raise InterruptedError(f"worker received signal {signum}")

    if args.action == "run":
        for name in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(name, interrupted)
    try:
        if args.action == "preflight":
            result = preflight(operator_config(args.project, args.gpu_uuid))
        else:
            if not args.operation_id or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,80}", args.operation_id
            ):
                raise ValueError("invalid operation identity")
            if args.action == "status":
                root = args.project / "evidence/benchmark-baseline" / args.operation_id
                result = operation_status(root)
            elif args.action == "reconcile":
                result = reconcile_operation(args.project, args.operation_id)
            else:
                if not args.allow_gpu:
                    raise ValueError("run requires --allow-gpu")
                result = run_operation(
                    args.project, args.operation_id, json.load(sys.stdin), args.gpu_uuid
                )
        print(json.dumps(result))
        if (args.action == "preflight" and not result["ready"]) or (
            args.action == "run" and result["exit_code"]
        ):
            raise SystemExit(1)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
