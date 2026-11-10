"""Prepare and explicitly launch ten Cactus development experiment cycles."""

import argparse
import asyncio
import json
from pathlib import Path
import shlex
from time import monotonic
from uuid import uuid4

from research_loop.state import write_json
from . import cactus_case
from .accounting import summarize_usage
from .runner import run

MODEL = "gpt-6.1-terra"
HYPOTHESES = (
    "Replace every ReLU in tiny_cnn with GELU, changing no other layer.",
    "Replace every ReLU in tiny_cnn with SiLU, changing no other layer.",
    "Add BatchNorm2d after each convolution before its existing activation.",
    "Add GroupNorm with 8 groups after each convolution before its activation.",
    "Replace global AdaptiveAvgPool2d(1) with AdaptiveMaxPool2d(1).",
    "Add Dropout(0.2) after Flatten and before the existing Linear layer.",
    "Widen the second and third convolution outputs from 64 to 96 and match their input and Linear dimensions.",
    "Narrow the second and third convolution outputs from 64 to 32 and match their input and Linear dimensions.",
    "Change only the first convolution kernel from 3 to 5 and padding from 1 to 2.",
    "Add one 64-to-64 3x3 convolution with padding 1 and ReLU before global pooling.",
)


async def remote(remote_config, action, identity=None, request=None):
    command = [
        remote_config["python"],
        remote_config["project_root"] + "/benchmarks/experiment_cycle/remote_worker.py",
        "--project",
        remote_config["project_root"],
        "--gpu-uuid",
        remote_config["gpu_uuid"],
        action,
    ]
    if identity:
        command.append(identity)
    if action == "run":
        command.append("--allow-gpu")
    process = await asyncio.create_subprocess_exec(
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        remote_config["ssh_host"],
        shlex.join(command),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await process.communicate(
            json.dumps(request).encode() if request is not None else None
        )
    except BaseException:
        process.terminate()
        await process.wait()
        raise
    if not stdout.strip():
        raise RuntimeError(
            f"remote transport returned no receipt (exit {process.returncode}); "
            f"{stderr.decode(errors='replace')[-1000:]}; "
            "reconcile original operation before retry"
        )
    value = json.loads(stdout)
    if not isinstance(value, dict):
        raise ValueError("remote response is not an object")
    if (
        process.returncode
        and not (action == "preflight" and "ready" in value)
        and not (action == "run" and "exit_code" in value)
    ):
        raise RuntimeError(value.get("error", stderr.decode(errors="replace")[-1000:]))
    return value


class RemoteExecutor:
    async def __call__(self, case, workspace, root, command, model, effort):
        identity = "cycle-" + uuid4().hex
        write_json(
            root / "remote-operation.json",
            {"operation_id": identity, "status": "dispatching"},
        )
        source = (workspace / "train.py").read_text()
        async with asyncio.timeout(case["limits"]["execute_seconds"]):
            receipt = await remote(
                case["remote"], "run", identity, {"candidate_source": source}
            )
        write_json(
            root / "remote-operation.json",
            {"operation_id": identity, "status": "terminal", "receipt": receipt},
        )
        if receipt.get("exit_code") == 0:
            baseline = receipt["baseline"]["score"]
            candidate = receipt["candidate"]["score"]
            write_json(
                workspace / "metrics.json",
                {
                    "baseline_auc": baseline,
                    "candidate_auc": candidate,
                    "delta_auc": candidate - baseline,
                },
            )
        return receipt


def prepare(deployment, output):
    import shutil

    deployment = Path(deployment).resolve(strict=True)
    settings = json.loads(deployment.read_text())
    source = (deployment.parent / settings["starting_source"]).resolve(strict=True)
    metadata = (deployment.parent / settings["data_metadata"]).resolve(strict=True)
    remote_settings = settings["remote"]
    gpu_uuids = remote_settings.get("gpu_uuids", [remote_settings.get("gpu_uuid")])
    if (
        not isinstance(gpu_uuids, list)
        or not gpu_uuids
        or any(not isinstance(gpu, str) or not gpu for gpu in gpu_uuids)
        or len(set(gpu_uuids)) != len(gpu_uuids)
    ):
        raise ValueError("gpu_uuids must be a nonempty list of distinct GPU UUIDs")
    output = Path(output).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    manifests = []
    for index, hypothesis in enumerate(HYPOTHESES, 1):
        directory = output / f"cycle-{index:02d}"
        workspace = directory / "workspace"
        workspace.mkdir(parents=True)
        shutil.copyfile(source, workspace / "train.py")
        shutil.copyfile(metadata, workspace / "data-metadata.json")
        task = f"""# Cactus development architecture experiment {index}
Question: after exactly one epoch from scratch, does this single intervention
improve development ROC-AUC relative to the unchanged tiny CNN?
Intervention: {hypothesis}
Modify ONLY the tiny_cnn function, returning nn.Sequential with numeric layer
arguments. All other Python AST, data roles, BCEWithLogitsLoss, Adam, seed 0,
batch size 128, learning rate .001, 4 CPU threads and RGB [B,3,32,32] /255
remain fixed. No pretrained state, extra data, augmentations, label access or
checkpoint reuse. Device is one fixed GPU through the trusted host. Plan the
matched control and interpretation before implementation. Prepare may syntax
compile only and must return [\"{{python}}\",\"train.py\"]; actual execution
belongs to the host. See data-metadata.json for the existing prepared split.
The host trains BOTH control and candidate and grades dev predictions using
the pinned upstream Cactus ROC-AUC grader. Test scores never enter this loop.
Evaluate returns supported iff candidate_auc-baseline_auc > 0, otherwise
contradicted. An unfavorable result is valid. Return metrics baseline_auc,
candidate_auc,delta_auc; evidence must contain exact filename metrics.json
and may cite .cycle/execute.json or train.py, with no explanatory prose in paths.
Limit claims to this one-epoch development experiment; this is not an official
full MLE-bench result, an independent task, or a generalization claim.
"""
        (directory / "task.md").write_text(task)
        manifest = {
            "version": 1,
            "case_type": "cactus-dev",
            "case_id": f"cactus-architecture-{index:02d}",
            "source": {
                "kind": "mle-bench-lite-development",
                "reference": "aerial-cactus-identification; pinned official grader 507f92e; prepared mirror 01a3db85; additional project dev split seed 0",
            },
            "task": "task.md",
            "workspace": "workspace",
            "command": ["{python}", "train.py"],
            "result_file": "metrics.json",
            "device": "cuda:0",
            "limits": {"stage_seconds": 300, "execute_seconds": 1500},
            "remote": {
                **{k: v for k, v in remote_settings.items() if k != "gpu_uuids"},
                "gpu_uuid": gpu_uuids[(index - 1) % len(gpu_uuids)],
            },
        }
        write_json(directory / "case.json", manifest)
        cactus_case.load_case(directory / "case.json")
        manifests.append(f"cycle-{index:02d}/case.json")
    write_json(
        output / "collection.json",
        {
            "version": 1,
            "model": MODEL,
            "target_cycles": 10,
            "cases": manifests,
            "status": "prepared-not-started",
        },
    )
    return {
        "cases": len(manifests),
        "manifest": str(output / "collection.json"),
        "status": "prepared-not-started",
    }


def load_collection(dataset):
    dataset = Path(dataset).resolve(strict=True)
    specification = json.loads((dataset / "collection.json").read_text())
    if (
        specification["model"] != MODEL
        or specification["target_cycles"] != 10
        or len(specification["cases"]) != 10
    ):
        raise ValueError("expected ten fixed Terra experiment cases")
    paths = [(dataset / path).resolve(strict=True) for path in specification["cases"]]
    if any(not path.is_relative_to(dataset) for path in paths):
        raise ValueError("collection case path escapes dataset")
    return paths, [cactus_case.load_case(path) for path in paths]


async def preflight(cases):
    configs = {}
    for case in cases:
        config = case["remote"]
        configs.setdefault((config["ssh_host"], config["gpu_uuid"]), config)
    checks = await asyncio.gather(
        *(remote(config, "preflight") for config in configs.values())
    )
    return list(zip(configs, checks))


async def collect(dataset, output, strategy=None):
    case_paths, cases = load_collection(dataset)
    for _, readiness in await preflight(cases):
        if not readiness.get("ready"):
            return {
                "status": "blocked",
                "reason": readiness["reason"],
                "model_calls": 0,
                "cycles_completed": 0,
            }
    output = Path(output).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    started = monotonic()
    reports = []
    summary = {
        "status": "running",
        "model": MODEL,
        "target_cycles": 10,
        "cycles_completed": 0,
        "attempts": [],
    }
    groups = {}
    for index, case in enumerate(cases):
        config = case["remote"]
        groups.setdefault((config["ssh_host"], config["gpu_uuid"]), []).append(index)
    stopped = False

    def save():
        summary["recorded_tokens"] = sum(
            r["usage"]["total"]["total_tokens"] for r in reports
        )
        summary["cycle_wall_seconds"] = sum(r["wall_seconds"] for r in reports)
        summary["batch_wall_seconds"] = monotonic() - started
        write_json(output / "collection-status.json", summary)

    async def worker(indices):
        nonlocal stopped
        for index in indices:
            if stopped:
                return
            case = cases[index]
            attempt = {
                "case_id": case["case_id"],
                "gpu_uuid": case["remote"]["gpu_uuid"],
                "ssh_host": case["remote"]["ssh_host"],
                "report": str(output / f"cycle-{index + 1:02d}/report.json"),
                "accepted": False,
            }
            try:
                ready = await remote(case["remote"], "preflight")
                if stopped:
                    return
                if not ready.get("ready"):
                    stopped = True
                    summary.update(status="blocked", reason=ready["reason"])
                    attempt["error"] = ready["reason"]
                else:
                    report = await run(
                        case_paths[index],
                        output / f"cycle-{index + 1:02d}",
                        model=MODEL,
                        strategy=strategy,
                        executor=RemoteExecutor(),
                        case_loader=cactus_case.load_case,
                        case_checker=cactus_case.check,
                    )
                    reports.append(report)
                    complete = (
                        report["status"] == "completed"
                        and report["correctness"]["status"] == "pass"
                        and report["usage"]["coverage"]["complete"]
                    )
                    attempt["accepted"] = complete
                    summary["cycles_completed"] += int(complete)
                    if not complete:
                        stopped = True
                        summary.update(
                            status="needs-attention",
                            reason="case failed a stage, correctness or coverage; no automatic retry",
                        )
            except Exception as error:
                stopped = True
                summary.update(
                    status="needs-attention",
                    reason="cycle raised an exception; no automatic retry",
                )
                attempt["error"] = f"{type(error).__name__}: {error}"
                report_path = Path(attempt["report"])
                if report_path.exists():
                    try:
                        reports.append(json.loads(report_path.read_text()))
                    except (OSError, ValueError):
                        pass
                else:
                    try:
                        tasks = json.loads(
                            (report_path.parent / "tasks.json").read_text()
                        )
                        stages = json.loads(
                            (report_path.parent / "stages.json").read_text()
                        )
                        reports.append(
                            {
                                "usage": summarize_usage(tasks["tasks"]),
                                "wall_seconds": sum(
                                    stage.get("wall_seconds") or 0 for stage in stages
                                ),
                            }
                        )
                        attempt["partial_accounting"] = True
                    except (OSError, ValueError, KeyError):
                        attempt["partial_accounting_unavailable"] = True
            summary["attempts"].append(attempt)
            save()
            if stopped:
                return

    save()
    await asyncio.gather(*(worker(indices) for indices in groups.values()))
    if summary["cycles_completed"] == 10:
        summary["status"] = "completed"
    save()
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    setup = sub.add_parser(
        "prepare", help="Materialize ten private cases; no models or GPU"
    )
    setup.add_argument("--deployment", type=Path, required=True)
    setup.add_argument("--output", type=Path, required=True)
    probe = sub.add_parser("preflight", help="Read GPU availability only")
    probe.add_argument("dataset", type=Path)
    launch = sub.add_parser("run", help="Run only when the fixed GPU is idle")
    launch.add_argument("dataset", type=Path)
    launch.add_argument("--output", type=Path, required=True)
    launch.add_argument("--strategy", type=Path)
    launch.add_argument("--allow-model", action="store_true")
    launch.add_argument("--allow-gpu", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            result = prepare(args.deployment, args.output)
        elif args.action == "preflight":
            _, cases = load_collection(args.dataset)
            checks = asyncio.run(preflight(cases))
            result = {
                "ready": all(check.get("ready") for _, check in checks),
                "gpus": [
                    {"ssh_host": host, "gpu_uuid": gpu, **check}
                    for (host, gpu), check in checks
                ],
            }
        else:
            if not (args.allow_model and args.allow_gpu):
                parser.error("run requires --allow-model and --allow-gpu")
            result = asyncio.run(collect(args.dataset, args.output, args.strategy))
        print(json.dumps(result, indent=2))
        if (
            result.get("status") in ("blocked", "needs-attention")
            or result.get("ready") is False
        ):
            raise SystemExit(75)
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
