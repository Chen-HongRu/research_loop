"""Host-owned one-case fresh-context startup and reviewed GPU smoke benchmark."""

import argparse
import contextlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid


BASELINE = """# Fixed synthetic GPU execution contract v3
Only perform the authorized seed-43 three-update smoke, not a training study.
The committed source/ contains the exact deployed model and original loss modules;
gpu_smoke.py is the versioned synthetic tensor generator and smoke entry point.
Read backend.json and these source files to inspect the active contract.
The controller GENERATES the synthetic tensors from this source: B=2, L=16,
K=4; coordinate-state FM with frozen hidden conditioning, H readout, the unchanged equal sum of five ESM3 losses;
no FM loss, no extra hidden noise. No local Torch/data, installations or SSH
are needed. There are no missing input mounts, datasets, or checkpoints.
The trusted backend runs on one CUDA GPU; do not execute the smoke locally.
Operator remote environment/source setup completed before this Study launch.
ExecutionBackend.prepare validates the configured contract without allocation;
start retains identity before one GPU job; inspect never starts another job.
During Execute use the canonical submission interface:
python3 submission.py submit --workspace . --action research
python3 submission.py status --workspace .
Wait for status completed or failed; accepted is never permission to resubmit.
The trusted backend fixes seed43 and three updates; the request accepts no code
or model commands. Report the actual result and limitations. Measurements are an
unranked synthetic diagnostic, not evidence of bbRMSD research improvement.
Do not invent metrics, change losses, or launch another GPU job.
"""
FIXTURE = """# Prepared Study launch service
Read the supplied research-loop SKILL and relevant docs to start the user's study.
Only the local reference source and generator are available initially (backend v3;
B=2,L=16,K=4, seed43, three updates, one GPU job). No local Torch/data,
installations or SSH are required. The controller GENERATES synthetic tensors
from the committed gpu_smoke.py; no external data mounts are missing.
No remote case environment/backend is ready yet. First request actual operator setup:
python3 client.py prepare
Wait for prepared status. This stages a fresh remote case environment/source through
the fixed host-owned interface; it does not launch a GPU job. Then write your goal
to a local text file and launch:
python3 client.py launch --goal GOAL_FILE [--task TASK_JSON] [--selection-policy direct|debate]
The optional task file must use the actual research-loop TaskQueue request schema.
The host launches a real Study with real Terra Plan, Execute and Review roles.
The client waits for Review and verified final scalars in the existing dashboard
telemetry API. Request each operation once; inspect failures, do not retry effects.
"""


def write_json(path, value):
    if path.is_symlink():
        raise ValueError("Refusing symlink output")
    temporary = path.with_name(path.name + ".host-tmp")
    with temporary.open("x") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
    temporary.replace(path)


def read_request(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 50000:
            raise ValueError("Launch request must be a regular file <= 50000 bytes")
        raw = stream.read(50001)
        if len(raw) > 50000:
            raise ValueError("Launch request exceeds 50000 bytes")
    value = json.loads(raw)
    if type(value) is not dict or set(value) != {
        "goal_text",
        "task",
        "selection_policy",
    }:
        raise ValueError("Invalid launch request fields")
    if (
        not isinstance(value["goal_text"], str)
        or not 0 < len(value["goal_text"]) <= 20000
        or not value["goal_text"].strip()
    ):
        raise ValueError("Goal must be nonempty and <= 20000 characters")
    if value["selection_policy"] not in (None, "direct", "debate"):
        raise ValueError("Invalid selection policy")
    task = value["task"]
    allowed = {
        "version",
        "id",
        "kind",
        "title",
        "instruction",
        "route_id",
        "base_experiment",
        "priority",
        "allow_changes",
        "budget",
    }
    if task is not None and (type(task) is not dict or set(task) - allowed):
        raise ValueError("Invalid task request fields")
    if task is not None and task.get("kind", "investigation") != "investigation":
        raise ValueError("Only the authorized smoke investigation is supported")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "runtime-root", "codex-wrapper", "prepared-source"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("host", "remote-root", "remote-python", "source-revision"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--gpu", default="0")
    parser.add_argument("--timeout-seconds", type=int, default=420)
    args = parser.parse_args()
    if (
        not args.gpu.isdecimal()
        or args.host.startswith("-")
        or args.timeout_seconds <= 0
    ):
        parser.error("Invalid GPU, SSH host or timeout")
    runtime = args.runtime_root.resolve()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    sys.path.insert(0, str(runtime / "src"))
    from benchmarks.startup.runner import run_case
    from benchmarks.startup.backend import FixedStartupBackend
    from benchmarks.startup.study_bridge import StartupBridge
    from benchmarks.startup.verify_dashboard import verify
    from research_loop import Study
    from research_loop.cli import main as cli_main
    from research_loop.harness import HarnessConfig
    from research_loop.tasks import TaskQueue

    root = args.root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    case, baseline = root / "case", root / "baseline"
    case.mkdir()
    baseline.mkdir()
    shutil.copyfile(Path(__file__).with_name("client.py"), case / "client.py")
    (baseline / "README.md").write_text(BASELINE)
    prepared = args.prepared_source.resolve()
    if not (prepared / "source").is_dir() or not (prepared / "gpu_smoke.py").is_file():
        raise ValueError("prepared-source requires source/ and deployed gpu_smoke.py")
    for source in sorted((prepared / "source").rglob("*.py")):
        if source.is_symlink():
            raise ValueError("Prepared source modules must not be symlinks")
        destination = baseline / "source" / source.relative_to(prepared / "source")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    if not (baseline / "source").is_dir():
        raise ValueError("Prepared source contains no Python modules")
    shutil.copyfile(prepared / "gpu_smoke.py", baseline / "gpu_smoke.py")
    for name, module in (
        ("submission.py", "submission.py"),
        ("research_loop_events.py", "telemetry.py"),
    ):
        shutil.copyfile(runtime / "src/research_loop" / module, baseline / name)
    write_json(
        baseline / "backend.json",
        {
            "version": 3,
            "synthetic": True,
            "generator": "gpu_smoke.py",
            "model_and_original_losses": "source/",
            "batch": 2,
            "length": 16,
            "k": 4,
            "seed": 43,
            "updates": 3,
            "input_identity": "versioned gpu_smoke.py synthetic generator; no external tensors",
            "trusted_command": "python gpu_smoke.py --source-root source --output <unique>/result.json --seed 43",
            "execution": "host-owned CUDA backend; no local execution dependencies required",
            "operator_setup": "required client.py prepare; fresh remote case environment/source",
            "execution_interface": "canonical submission.py submit/status; ExecutionBackend prepare/start/inspect",
            "evaluation": "unranked synthetic diagnostic only; no research-quality claim",
        },
    )
    for command in (
        ["init", "-q"],
        [
            "add",
            "README.md",
            "backend.json",
            "gpu_smoke.py",
            "source",
            "submission.py",
            "research_loop_events.py",
        ],
        [
            "-c",
            "user.name=Benchmark",
            "-c",
            "user.email=benchmark@example.invalid",
            "commit",
            "-qm",
            "Prepared backend",
        ],
    ):
        subprocess.run(
            ["git", "-C", str(baseline), *command], check=True, capture_output=True
        )
    identity = {
        field: subprocess.check_output(
            ["git", "-C", str(baseline), "rev-parse", revision], text=True
        ).strip()
        for field, revision in (
            ("baseline_commit", "HEAD"),
            ("source_tree", "HEAD:source"),
        )
    }
    write_json(case / "backend-identity.json", identity)
    (case / "README.md").write_text(
        FIXTURE
        + f"\nRead-only prepared baseline evidence: {baseline}\n"
        + f"Committed source identity: {identity['baseline_commit']}\n"
        + "Inspect backend.json, gpu_smoke.py and source/ there if needed; do not edit them.\n"
    )
    prompt = root / "prompt.txt"
    prompt.write_text(
        f"使用 {runtime / 'SKILL.md'} 和相关文档启动一个全新的研究 Study。"
        "目标是固定 hidden 条件的坐标 FM、H readout、原有五项 ESM3 loss，关注 bbRMSD。"
        "当前唯一授权的第一步是 synthetic B=2 L=16 K=4、seed43、三次更新 GPU smoke；"
        "保持 losses，不添加 hidden noise，只允许一个 GPU job。必须实际完成 Terra Plan Execute Review。"
        "从实际后端环境准备开始，使用既有接口启动研究，跑完GPU轮次并让最终标量在现有dashboard telemetry可见。"
        "工作区 README 描述本地参考源码及prepare和launch接口。请执行并等待完成。"
    )
    started = time.monotonic()
    stop = threading.Event()
    trusted = {
        "source_revision": args.source_revision,
        "remote_python": args.remote_python,
        "backend_identity": identity,
        "gpu_submitted": False,
    }
    run_id = "startup-" + uuid.uuid4().hex

    def prepare_remote():
        remaining = started + args.timeout_seconds - time.monotonic()
        if stop.is_set() or remaining < 80:
            raise TimeoutError("Insufficient outer budget for fresh operator setup")
        trusted["preparation_start_seconds"] = time.monotonic() - started
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "ConnectTimeout=10",
            args.host,
            shlex.join(
                ["bash", args.remote_root.rstrip("/") + "/prepare_case.sh", run_id]
            ),
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=min(70, remaining - 10)
        )
        (root / "prepare-stdout.txt").write_text(result.stdout)
        (root / "prepare-stderr.txt").write_text(result.stderr)
        result.check_returncode()
        manifest = json.loads(result.stdout)
        trusted["preparation_complete_seconds"] = time.monotonic() - started
        trusted["preparation_manifest"] = manifest
        return manifest

    def run_gpu():
        if stop.is_set() or started + args.timeout_seconds - time.monotonic() < 50:
            raise TimeoutError("Insufficient outer budget for the fixed GPU smoke")
        trusted["gpu_start_seconds"] = time.monotonic() - started
        trusted["gpu_submitted"] = True
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "ConnectTimeout=10",
            args.host,
            shlex.join(
                ["bash", args.remote_root.rstrip("/") + "/run_gpu.sh", args.gpu, run_id]
            ),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=40)
        (root / "gpu-stdout.txt").write_text(result.stdout)
        (root / "gpu-stderr.txt").write_text(result.stderr)
        trusted["gpu_exit_code"] = result.returncode
        result.check_returncode()
        receipt = json.loads(result.stdout)
        trusted["gpu_complete_seconds"] = time.monotonic() - started
        trusted["gpu_receipt"] = receipt
        return receipt

    backend = FixedStartupBackend(
        run_id=run_id,
        state_dir=root / "backend",
        source_identity=identity,
        prepare_remote=prepare_remote,
        run_gpu=run_gpu,
    )

    def launch():
        request = case / "launch-request.json"
        setup = case / "setup-request.json"
        try:
            while not setup.exists() and not setup.is_symlink():
                if request.exists():
                    raise RuntimeError("Remote setup required before Study launch")
                if stop.wait(0.2):
                    return
            trusted["preparation_request_seconds"] = time.monotonic() - started
            descriptor = os.open(setup, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                if (
                    not stat.S_ISREG(os.fstat(stream.fileno()).st_mode)
                    or stream.read(1025).strip() != b"{}"
                ):
                    raise ValueError("Operator setup accepts only an empty JSON object")
            write_json(case / "setup-result.json", backend.setup())
            while not request.exists() and not request.is_symlink():
                if stop.wait(0.2):
                    return
            value = read_request(request)
            child_seconds = min(
                360, int(started + args.timeout_seconds - time.monotonic() - 10)
            )
            if stop.is_set() or child_seconds < 1:
                raise TimeoutError("Insufficient outer budget to launch Study")
            call_seconds = min(120, child_seconds)
            trusted["launch_seconds"] = time.monotonic() - started
            goal = root / "goal.md"
            goal.write_text(value["goal_text"])
            command = [
                "init",
                str(root / "study"),
                "--goal",
                str(goal),
                "--workspace",
                str(baseline),
                "--allow-write",
                "--model",
                "gpt-6.1-terra",
                "--executable",
                str(args.codex_wrapper.resolve()),
                "--max-iterations",
                "1",
                "--max-calls",
                "16",
                "--max-seconds",
                str(child_seconds),
                "--timeout",
                str(call_seconds),
            ]
            if value["selection_policy"] is not None:
                command += ["--selection-policy", value["selection_policy"]]
            with (
                (root / "init-stdout.txt").open("w") as output,
                contextlib.redirect_stdout(output),
            ):
                if cli_main(command) != 0:
                    raise RuntimeError("Study initialization failed")
            if value["task"] is not None:
                TaskQueue(root / "study").submit(value["task"])
            bridge = StartupBridge(
                config=HarnessConfig(
                    model="gpt-6.1-terra",
                    executable=str(args.codex_wrapper.resolve()),
                    timeout_seconds=call_seconds,
                ),
                backend=backend,
                study_root=root / "study",
            )
            study = Study(root / "study", harness=bridge)
            state = study.run(iterations=1)
            trusted["inner_state"] = state
            trusted["bridge_errors"] = bridge.bridge_errors
            trusted["trusted_gpu_receipt"] = bridge.receipt
            publication = root / "study/iterations/0001/publication.json"
            reviewed = (
                publication.is_file()
                and json.loads(publication.read_text())
                .get("metadata", {})
                .get("status")
                == "reviewed"
            )
            if not reviewed or bridge.receipt is None or bridge.bridge_errors:
                raise RuntimeError("Study did not complete the GPU smoke and Review")
            trusted["reviewed_complete_seconds"] = time.monotonic() - started
            display = verify(root, bridge.receipt)
            if display.get("ok") is not True:
                raise RuntimeError("Final scalar dashboard verification failed")
            trusted["display_verification"] = display
            trusted["display_ready_seconds"] = time.monotonic() - started
            write_json(
                case / "launch-result.json",
                {
                    "status": "completed",
                    "execution_identity": run_id,
                    "display": display,
                    "study_state": state,
                },
            )
        except Exception as error:
            trusted["error"] = f"{type(error).__name__}: {error}"
            state_path = root / "study/state.json"
            state = json.loads(state_path.read_text()) if state_path.is_file() else None
            if not (case / "setup-result.json").exists():
                write_json(
                    case / "setup-result.json",
                    {"status": "error", "error": trusted["error"], "run_id": run_id},
                )
            write_json(
                case / "launch-result.json",
                {
                    "status": "error",
                    "error": trusted["error"],
                    "study_state": state,
                    "stop_reason": state.get("stop_reason") if state else None,
                    "gpu_submitted": trusted["gpu_submitted"],
                    "execution_identity": run_id,
                    "retained_execution": backend.inspect(),
                },
            )
        finally:
            state_path = root / "study/state.json"
            if state_path.is_file():
                trusted["inner_state"] = json.loads(state_path.read_text())
            trusted["inner_phase_logs"] = [
                str(path.relative_to(root))
                for path in sorted((root / "study").rglob("process.json"))
            ]

    watcher = threading.Thread(target=launch, daemon=True)
    watcher.start()
    try:
        outer = run_case(case, prompt, root / "outer", args.timeout_seconds)
    finally:
        stop.set()
        watcher.join(
            timeout=max(0, started + args.timeout_seconds - time.monotonic()) + 2
        )
    trusted["host_worker_unresolved"] = watcher.is_alive()
    trusted["outer"] = outer
    trusted["total_seconds"] = time.monotonic() - started
    write_json(root / "benchmark-result.json", trusted)
    passed = (
        "display_ready_seconds" in trusted
        and trusted.get("display_verification", {}).get("ok") is True
        and not watcher.is_alive()
        and outer["exit_code"] == 0
        and not outer["timed_out"]
        and outer["launch_error"] is None
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
