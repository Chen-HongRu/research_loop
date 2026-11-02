# AeroArm 接入

Research Loop 提供通用控制器。本仓库的 AeroArm 示例仅检查接入条件；项目的任务、数据和可信评价器需由 AeroArm 提供。

## 第一步：检查入口

```sh
uv sync --locked
uv run research-loop init studies/aeroarm-check \
  --goal examples/aeroarm-goal.md \
  --workspace /absolute/path/to/aeroarm \
  --max-iterations 1 --max-calls 12 --max-seconds 900
```

`init` 不调用模型。先阅读生成的 `goal.md` 和 `study.json`，将目标中的只读范围与项目目录核对后，再用 `uv run research-loop run studies/aeroarm-check` 完成有界检查。该命令会使用已登录 CLI 的模型额度。

此阶段仅记录实际存在的项目入口、配置、依赖和评价条件；缺少的信息应标记为未决，不能填成假定的指标或成绩。离线流程展示使用独立目录 `studies/demo`，不引用真实项目。

## 第二步：冻结一个实际调查

在新的目标文件中固定以下内容，再初始化独立 Study：

| 内容 | 要明确的合同 |
| --- | --- |
| 问题 | 一次调查要回答的问题、已有证据和停止条件 |
| 可修改范围 | 基线 commit、可编辑代码与受保护文件 |
| 模型与数据 | 模型/损失、张量形状、mask、时间对齐、数据划分及角色 |
| 评价 | 固定评价器、聚合、候选选择规则、约束和可比范围 |
| 资源 | 轮数、调用、墙钟预算；外部计算需另有实际授权 |
| 产物 | 精确版本的源码、配置、测量及可信证据引用 |

数值评价 JSON 的接口见 [evaluation.json](../examples/evaluation.json)。该文件用 AUC 示范 schema；为 AeroArm 定义自己的指标名称、方向、单位和协议，不能原样套用其含义。定性调查可不提供数值评价。

开发评价可以反馈给搜索；最终验收必须在候选冻结后由循环外的可信入口完成。将 `data_role`、`allow_feedback` 与真实数据使用规则对应，不让最终验收结果进入后续研究上下文。

## 第三步：连接执行与评价

本地代码调查可以使用 `--workspace` 和 `--allow-write`。基线为干净的 Git 仓库，每轮冻结输入并建立独立工作区；工作区不能等于或包含 Study 目录。目标文件应授权实际需要的操作，而非笼统授权所有操作。

外部训练/评价适配器负责以下边界：

1. 接收一次有身份的请求，冻结确切候选与协议。
2. 在已授权资源内执行，持久化作业身份、终态、实际费用和输出。
3. 返回绑定精确产物的测量与证据；重复请求返回同一作业状态。
4. 未知外部效果先停留并核对，不能用重试重新提交。

参考 [Cactus 请求桥](../benchmarks/cactus/bridge.py) 的有限请求接口和 [ProTok 适配器](../integrations/protok/README.md) 的固定评价边界。它们的任务指标、数据及科学合同不属于 AeroArm 协议。通用接口为 [AgentHarness](../src/research_loop/harness.py)、[阶段 schema](../src/research_loop/protocol.py) 和 [提交协议](../src/research_loop/submission.py)。

## 最小验收

先以一个真实候选、小预算和独立输出目录跑一轮，检查：输入与结果绑定相同候选；Review 能核对实际证据；研究文档和 `publication.json` 已发布；失败或未测量项保留原样。一次成功接线不足以证明研究收益，正式比较需另行固定基线、预算与重复方案。
