"""Run one real Codex research iteration and one post-loop private evaluation."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from research_loop import Study, initialize
from research_loop.harness import HarnessConfig

if __package__:
    from .bridge import CactusBridge
else:
    from bridge import CactusBridge


INTERFACE = """# Cactus: one real GPU training investigation

The trusted controller provides fixed tiny-cnn training on a remote GPU. You have
no remote shell, SSH, network access, data labels, or controller evaluator access.
Only one actual training job is allowed for this entire smoke. Do not attempt
network calls, remote access, package installation or bypassing this interface.
Do not modify training source or controller files. Choose a bounded configuration.

During Execute, atomically write `train-request.json` in your current authorized
workspace (write a temporary JSON file, then rename). Exactly these fields:

```json
{"job_id":"tiny-001","epochs":1,"lr":0.001,"batch_size":128,"seed":0}
```

job_id: 1–64 ASCII letters/digits/underscore/hyphen, beginning with a letter/digit;
epochs: integer 1–5; lr: finite 0 < lr <= 0.1; batch_size: integer 32–512;
seed: integer 0–4294967295. No extra fields, paths, commands or code are accepted.
The bridge always runs tiny-cnn, device cuda:0, four CPU threads, fitting split only.
Monitor `results/<job_id>/result.json` until completed or error; use short polling
within the Plan timeout. Requests do not produce instantaneous results. Errors
may also appear in `bridge-error.json`. Do not change/resubmit a consumed job.

The bridge copies checkpoint.pt, dev-predictions.csv, test-predictions.csv,
train.py, run.json and timing.json into results/<job_id> and publishes dev-score.json
and result.json. No labels are copied. Read these local files for actual evidence.
result.json gives the development AUC, exact artifact path/version, source_sha256,
request, and evidence references. Keep test predictions unchanged and unscored.
The controller constructs the checkpoint artifact and development measurement from its receipt.
No fabricated measurements or claims of success are permitted on bridge errors.
"""

GOAL = """完成一次真实 Cactus tiny-cnn GPU 训练调查，报告固定开发集 AUC、
运行条件、准确 checkpoint 版本和可定位证据。允许选择一次有限训练配置，
但整个 smoke 最多执行一个真实训练任务，不做多次搜索或 27 次实验。
模型与数据划分固定；不得读取或索取开发/测试标签，不得访问远端或联网。
最终 private test 由外部控制器在循环结束且候选冻结后评价，不反馈给搜索。
开发集结果不能证明 private 提升，也不声称相对既有基线改善。

读取工作区 README.md 中的文件请求接口。选择和 Plan 必须聚焦于该可执行接口，
提出一次训练行动，base_experiment 为 null，历史 references 为空。
Plan 可以不设置 local_evaluation，input_artifacts 只引用实际存在的基线 README.md
及其 Git HEAD 版本；不要把远端路径当成本地输入。Plan 的 timeout_seconds 在剩余
预算内，应足以等待真实训练（建议至多 900 秒）。不要求机制假设。

Execute 通过 train-request.json 提交一次配置，等待本地结果，并核对 run.json、
timing.json、dev-score.json 与 checkpoint。最终仅返回所给 schema 的 markdown。
控制器根据可信收据构造 checkpoint 路径/版本、候选 ID、开发评估范围、AUC 和证据。
报告说明 split=dev、source_sha256、配置、初始化和首 batch 延迟、吞吐和显存；
未测项目明确标注。不得用测试预测作开发数据，不得自行评分测试预测。
错误或缺失结果须如实说明；不能编造测量或成功。

Review 核查真实开发指标、产物和来源，更新两层研究文档及候选核查记录。
目标只要求真实训练与完整开发结果和限制报告，不要求任意 AUC 阈值或机制解释。
一轮后停止。private 分数不可用于这轮目标、路线选择或后续调整。
"""


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def create_baseline(path: Path) -> None:
    """Always create a new repository; never import historical or private refs."""
    path.mkdir(parents=True, exist_ok=False)
    (path / "README.md").write_text(INTERFACE, encoding="utf-8")
    _git(path, "init")
    _git(path, "add", "README.md")
    _git(
        path,
        "-c",
        "user.name=Cactus Smoke",
        "-c",
        "user.email=cactus-smoke@localhost",
        "commit",
        "-m",
        "Document restricted training request interface",
    )


def _write(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )


def frozen_job(study: Path, state: dict, bridge: CactusBridge) -> tuple[str, dict]:
    """Bind the post-loop grader to exactly the unique recorded Execute artifact."""
    if state.get("phase") != "done" or not state.get("review_ref"):
        raise ValueError(
            "The iteration did not complete Review; private evaluation withheld"
        )
    reference = state.get("execution_ref")
    if not reference:
        raise ValueError("No frozen Execute result; private evaluation withheld")
    execute = json.loads((study / reference).read_text())
    artifacts = execute["artifacts"]
    if execute["outcome"] != "completed" or len(artifacts) != 1:
        raise ValueError(
            "Execute must freeze exactly one completed checkpoint candidate"
        )
    matches = [
        (job, result)
        for job, result in bridge.jobs.items()
        if result.get("status") == "completed" and result["artifact"] == artifacts[0]
    ]
    if len(matches) != 1:
        raise ValueError("Frozen candidate does not match the unique actual bridge job")
    job, result = matches[0]
    global_measurements = [
        measurement
        for measurement in execute["measurements"]
        if measurement["evaluation_id"] == "global-development-v1"
        and measurement["comparison_scope"] == "development-v1"
        and measurement["artifact"] == result["artifact"]
    ]
    if len(global_measurements) != 1 or global_measurements[0]["metrics"] != [
        {"name": "auc", "value": result["score"]}
    ]:
        raise ValueError(
            "Frozen global measurement does not match actual dev evaluation"
        )
    return job, global_measurements[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--remote-base", required=True)
    parser.add_argument(
        "--host", required=True, help="Operator-configured SSH host alias"
    )
    parser.add_argument(
        "--gpu", required=True, help="Numeric GPU index already confirmed idle"
    )
    parser.add_argument(
        "--codex", required=True, help="Exact authenticated Codex CLI path"
    )
    parser.add_argument(
        "--initial-repo", type=Path, help="New, nonexistent baseline repository path"
    )
    args = parser.parse_args(argv)
    started = time.monotonic()
    study_root = args.study.resolve()
    if study_root.exists():
        parser.error("--study must be a new nonexistent directory")
    repo = (
        args.initial_repo or study_root.with_name(study_root.name + "-baseline")
    ).resolve()
    if (
        repo == study_root
        or repo.is_relative_to(study_root)
        or study_root.is_relative_to(repo)
    ):
        parser.error("Initial repository and study must be disjoint directories")
    identifier = "smoke-" + uuid.uuid4().hex[:20]
    config = HarnessConfig(executable=args.codex, timeout_seconds=900)
    bridge = CactusBridge(
        config=config,
        host=args.host,
        remote_base=args.remote_base,
        gpu=args.gpu,
        study_id=identifier,
        seconds=1800,
    )
    create_baseline(repo)
    evaluation = {
        "id": "global-development-v1",
        "comparison_scope": "development-v1",
        "protocol": "Fixed prepared dev split (2,835 rows); pinned official ID-aligned ROC-AUC evaluator; one final checkpoint from fixed tiny-cnn, four CPU threads and the assigned GPU; no best-checkpoint selection or repeated-run aggregation. Only config fields allowed by the trusted bridge vary.",
        "data_role": "development",
        "allow_feedback": True,
        "objectives": [{"name": "auc", "direction": "maximize", "unit": "fraction"}],
        "constraints": [],
    }
    initialize(
        study_root,
        goal=GOAL,
        harness=config,
        workspace=repo,
        allow_write=True,
        global_evaluation=evaluation,
        max_iterations=1,
        max_calls=12,
        max_seconds=1800,
        selection={"seed": 0, "debate_rounds": 1, "max_resamples": 0},
    )
    # Preserve the exact small source set used by this run, not datasets/weights.
    implementation = study_root / "implementation"
    implementation.mkdir()
    project = Path(__file__).resolve().parents[2]
    for source in (project / "src/research_loop").glob("*.py"):
        shutil.copy2(source, implementation / source.name)
    for name in ("bridge.py", "run_smoke.py", "train.py", "prepare.py", "evaluate.py"):
        shutil.copy2(Path(__file__).parent / name, implementation / name)
    summary = {
        "study_id": identifier,
        "study": str(study_root),
        "host": args.host,
        "remote_base": args.remote_base,
        "gpu": args.gpu,
        "initial_repository": str(repo),
        "real_model_calls": True,
        "private_feedback_to_search": False,
        "status": "running",
    }
    _write(study_root / "smoke-summary.json", summary)
    try:
        state = Study(study_root, harness=bridge).run(iterations=1)
        summary["state"] = state
        job, measurement = frozen_job(study_root, state, bridge)
        # No model call follows this point: the Execute candidate is already frozen.
        private = bridge.evaluate(job, "test")
        private.update(
            job_id=job,
            candidate_id=measurement["candidate_id"],
            artifact=measurement["artifact"],
        )
        _write(study_root / "private-score.json", private)
        summary.update(
            status="completed",
            frozen_job=job,
            global_measurement=measurement,
            private_score=private,
        )
    except Exception as exc:
        summary.update(status="incomplete", error=f"{type(exc).__name__}: {exc}")
    finally:
        if summary["status"] == "running":
            summary["status"] = "incomplete"
        summary.update(
            wall_seconds=time.monotonic() - started,
            actual_training_requests=int(bridge.started),
            bridge_jobs=bridge.jobs,
            bridge_errors=bridge.bridge_errors,
        )
        _write(study_root / "smoke-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
