# ProTok SE3 研究接管

本集成复用 `Study → Route → Plan → Execute → Review`，使用固定 ProTok 后端评价候选。当前实例为 `studies/protok-se3-takeover-20261101/`，执行入口与模块冻结于 `control/runtime-r5/`，清单为 `control/runtime-r5-manifest.json`；运行中的实例不随仓库源码更新。

本地只读研究看板：<http://127.0.0.1:8765>。每 5 秒读取本地小型状态和评价元数据，显示当前阶段/作业、可点击探索图、代码继承与证据引用、已审阅 RMSD/最优曲线、各节点 Plan/Execute/Review/选择依据和全局研究。待审分数单独标记，无质量测量不画成零。GPU 状态来自后端本地回执，不提供远端逐训练步进度；已用 GPU 时间在作业核账后更新。看板不运行 Agent、训练、远程命令或修改研究状态。

重启看板（仓库根目录，前台服务）：

```sh
uv run python integrations/protok/dashboard.py \
  --root studies/protok-se3-takeover-20261101 --port 8765
```

授权窗口为 **2026-11-01 09:36:24–15:36:24（Asia/Shanghai）**，共六小时，不设候选或角色调用次数上限。固定截止不顺延，每次新分配须容纳完整 30 分钟。原全局记录、session 和 GPU 累计不重置：接管起点已使用 **11,660 GPU 秒**，仍受原 24 GPU 小时预算及 1 GPU 小时预留约束。

**当前已按用户要求暂停。** `control/STOP` 保留，研究 heartbeat 已删除，无在途作业。0010/job788028 已确认 CANCELLED，使用904 GPU秒；暂停核账累计19084 GPU秒、19条计分记录，详见 `control/user-pause.json`。恢复须由用户明确指示，原预算和记录不重置。只读看板继续可用。

**0009 在 0008 背景下将 loss 时间权重 d 改为 d²，已通过独立 Review，保留为当前开发协议下最佳，RMSD 2.2394492307654823 Å。** 相对接管起点 2.6046197081836633 Å 本次观测下降14.02%，参数仍为633200；尚无跨seed稳定性或独立测试证据。旧节点、失败记录与全部计数保留。

当前本地预检采用最小普通输入与必要 padding 检查。人工修改权重、精确共线等额外压力用例只形成警告，不因此让 Execute 不完整、候选失去资格、路线暂停或 Study 停止；不要求先修复它们，也不为无关候选重复运行。固定后端已有的初始化/训练终态检查、真实训练梯度和指标有效性仍须通过，失败时保留回执并停止依赖动作。评分器、固定评价和资源协议未修改。

- **0001** 审计导入历史最佳 **2.6046197081836633 Å**，fingerprint 为 `b5db45ac63db923c9bbbe7fdfce6cc59a04913bb4e3defb800fe8ff34d5f84de`。这是历史回执导入，无新训练，不是独立复现。
- **0002** 的本地 CPU/FP32 预检在非零权重、共线输入的参数梯度有限性断言处失败。日志未定位失败版本或参数，不能归因于此次改动。旧合同下未提交正式评价，无新 GPU 分配、无新 RMSD；历史最佳保留。
- **0003** 仅将末层 gain 由 4 改为 2。人工非零权重、共线输入的 CPU/FP32 检查中，原始与候选均有 17 个参数张量出现 NaN 梯度，旋转协变比较最大绝对误差分别为 0.1729230881 和 0.1553504467。独立 Review 确认这些输出并按旧合同暂停；底层原因尚未定位，不归因于 gain，也不据此推翻历史最佳。没有提交正式评价。该失败与停止决定保留，新用户合同仅适用于后续 Plan。
- **0004** 将逐残基鲁棒惩罚后平均，改为先计算每蛋白平均平方残差再施加鲁棒惩罚；参数和部署网络保持。作业 **787694 / COMPLETED / 0:0**，固定指标 **2.6159627869944497 Å**，使用 **1073 GPU 秒**。独立 Review 核查 valid/verified 并继续研究；本次观测未改善历史最优 2.6046197081836633 Å。累计计分记录 13、GPU 12733 秒；单 seed 开发观测不证明稳定优劣。
- **0005** 恢复三处几何归一化分母的梯度，固定指标 **2.934139392237956 Å**；作业 787738 正常完成，1212 GPU 秒。独立 Review 核查为有效负结果。
- **0006** 仅在 δ2 鲁棒 loss 的每蛋白项乘原有时间分母 `max(1−t,0.05)`，以完整 logical_size 归约，不按权重和重新归一化。固定指标 **2.4506948121883445 Å**；作业 787784 正常完成，1040 GPU 秒，独立 Review 核查 valid/verified 并发布为最佳。模型、633200 参数、固定配置和推理保持。累计计分记录 15、GPU 14985 秒，历史最优及各节点均保留。
- **0008** 在0006上仅将第三消息方向改为归一化的更新前有效中心向量，按源j广播并聚合；固定指标 **2.3107223408535233 Å**，相对0006观测下降5.71%。作业787873正常完成，1136 GPU秒；独立Review核查并发布为最佳。固定训练协议、633200参数和loss保持，未验证受控推理速度优势。累计计分记录17、GPU17170秒。
- **0009** 在0008上仅将loss时间权重d改为d²，固定指标 **2.2394492307654823 Å**，相对0008观测下降3.08%。作业787986正常完成，1010 GPU秒；独立Review核查并发布为最佳。模型、参数、部署和固定评价保持。累计计分记录18、GPU18180秒。

固定协议为 seed42、从头训练 2000 optimizer updates、449 条开发蛋白、Euler100、proper-SO(3) Cα RMSD 越低越好；资源为 H100 80GB、debug/00:30:00、8 CPU、96 GiB、2 workers。teacher、数据、noise、optimizer、scheduler、scorer 与采样器固定。子集已用于自适应开发，单 seed 排名不提供跨 seed 稳定性或独立测试证据。

候选只允许改变三个文件：

- `protok/model/autoresearch_se3.py`
- `protok/model/autoresearch_se3_loss.py`
- `configs/autoresearch_se3_candidate.json`

Agent 使用独立 worktree；可信桥冻结源码与 Git 版本，只将原配置的 `workspace` 指向该源码快照。旧 `autoresearch.sh` 使用原绝对路径，不作为接管入口。可信 continuation 验证交接、授权和源码身份，保留原 `.eval.lock`、计分身份及全部历史记录。原 `.autoresearch-off` 保持原样；只有其封存的历史内容与元数据获新授权认可，标记变化或新 `control/STOP` 禁止新增提交。停止进入已有作业的取消、核账路径，取消确认仍受远程轮询延迟约束。

从仓库根目录使用冻结入口查看状态：

```sh
ROOT=studies/protok-se3-takeover-20261101
PYTHONPATH="$PWD/$ROOT/control/runtime-r5/src" \
  UV_CACHE_DIR=/private/tmp/research-loop-uv-cache \
  uv run --no-project --python 3.12 python -B \
  "$ROOT/control/runtime-r5/integrations/protok/run.py" --root "$ROOT" --status
```

同一冻结入口的 `--run-one` 每次最多推进一个 iteration；有活动 runner 时只观察该执行，不启动第二个。不要对已有实例重复 `--init`，也不要重放未知效果的 Execute。创建 `control/STOP` 可禁止新增工作；已有 owned attempt 只能显式恢复取消与回执核账，不重新提交。`control/runner.json` 与非阻塞锁记录 runner 状态，实际研究状态以 `study/state.json` 及已发布 Review 为准；`control/status.json` 可能保留较早失败快照。运行到停止状态后不自动重置为 ready，须先明确核清停止原因和已有执行效果。

接管文档与证据位置（均相对当前实例）：

- `control/handoff.json`、`ownership.json`、`continuation.json`：交接、授权与固定窗口。
- `study/goal.md`、`research.md`、`routes/`、`frontiers.json`：研究合同、当前理解、路线与已核查候选。
- `study/iterations/0001/workspace/evaluation-result.json`、`control/active-best.json`：历史最佳导入及来源。
- `study/iterations/0002/execute/attempt-003/stdout.jsonl`、`study/iterations/0002/review/attempt-001/response.json`：实际预检输出及独立 Review。
- `study/iterations/0003/execute/attempt-001/stdout.jsonl`、`study/iterations/0003/review/attempt-001/response.json`：原始与候选分别记录的共线诊断失败及停止决定。
- `control/reconciliations/local-incomplete-0002-attempt-003.json`：本地失败、未评价的协调记录。
- `control/reconciliations/review-0002-resume-next-plan.json`：监督端恢复后续新 Plan 的完整前后状态与 goal 版本；0002 原发布和 R0004 暂停状态保持不变。
- `control/reconciliations/user-corner-policy-0003.json`：用户将人工 corner case 降为非阻断诊断的授权、新旧合同、路线和状态；保留旧节点，仅恢复新 Plan。
- `control/first-real-e2e-verification.json`、`study/iterations/0004/publication.json`：首个新增真实评测与独立 Review 完成的记录，结果未改善。
- `control/jobs/`、`journal.jsonl`：冻结评价任务与发布记录，链接原账本；成本由原全局后端记录目录累计。
- 原项目 `experiments/research-loop-handoff.json`：接管链接；原模型、scorer、session 与 checkpoint 未因接管覆盖。

r4 验证日志 `control/verification-r4-tests.log` 记录 **115 项测试通过（12.432 秒）**。r5 仅改变本地预检指引；Ruff format/check 与 **13 项 runner 测试**通过（`/private/tmp/protok-r5-runner-tests.log`）。已确认 0004 冻结了新合同。模拟传输下的取消、恢复和控制语义测试不构成新的蛋白训练结果。
