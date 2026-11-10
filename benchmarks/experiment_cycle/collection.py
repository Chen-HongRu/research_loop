"""Prepare and explicitly launch ten Cactus development experiment cycles."""

import argparse
import asyncio
import json
from pathlib import Path
import shlex
from uuid import uuid4

from research_loop.state import write_json
from . import cactus_case
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
            f"remote transport returned no receipt (exit {process.returncode}); reconcile original operation before retry"
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
            "remote": settings["remote"],
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


async def collect(dataset, output, strategy=None):
    dataset = Path(dataset).resolve(strict=True)
    specification = json.loads((dataset / "collection.json").read_text())
    if (
        specification["model"] != MODEL
        or specification["target_cycles"] != 10
        or len(specification["cases"]) != 10
    ):
        raise ValueError("expected ten fixed Terra experiment cases")
    case_paths = [
        (dataset / path).resolve(strict=True) for path in specification["cases"]
    ]
    if any(not path.is_relative_to(dataset) for path in case_paths):
        raise ValueError("collection case path escapes dataset")
    cases = [cactus_case.load_case(path) for path in case_paths]
    if len({case["remote"]["gpu_uuid"] for case in cases}) != 1:
        raise ValueError("collection must use one fixed GPU")
    readiness = await remote(cases[0]["remote"], "preflight")
    if not readiness.get("ready"):
        return {
            "status": "blocked",
            "reason": readiness["reason"],
            "model_calls": 0,
            "cycles_completed": 0,
        }
    output = Path(output).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    reports = []
    summary = {
        "status": "running",
        "model": MODEL,
        "target_cycles": 10,
        "cycles_completed": 0,
        "attempts": [],
    }
    for index, case in enumerate(cases, 1):
        readiness = await remote(case["remote"], "preflight")
        if not readiness.get("ready"):
            summary.update(status="blocked", reason=readiness["reason"])
            break
        report = await run(
            case_paths[index - 1],
            output / f"cycle-{index:02d}",
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
        summary["attempts"].append(
            {
                "case_id": case["case_id"],
                "report": str(output / f"cycle-{index:02d}/report.json"),
                "accepted": complete,
            }
        )
        summary["cycles_completed"] += int(complete)
        write_json(output / "collection-status.json", summary)
        if not complete:
            summary.update(
                status="needs-attention",
                reason="case failed a stage, correctness or coverage; no automatic retry",
            )
            break
    if summary["cycles_completed"] == 10:
        summary["status"] = "completed"
    summary["recorded_tokens"] = sum(
        r["usage"]["total"]["total_tokens"] for r in reports
    )
    summary["cycle_wall_seconds"] = sum(r["wall_seconds"] for r in reports)
    write_json(output / "collection-status.json", summary)
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
            spec = json.loads((args.dataset / "collection.json").read_text())
            result = asyncio.run(
                remote(
                    cactus_case.load_case(args.dataset / spec["cases"][0])["remote"],
                    "preflight",
                )
            )
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
