# v2 实现与 Cactus 真实验证

> 历史验证摘要：以下数值来自开发时保留的记录，本次公开整理未重跑训练。原始运行产物未随源码分发，读者无法仅凭此摘要独立复核这些数值。

日期：2026-11-01。新框架已完成一轮真实模型调用、GPU 训练、证据评审与冻结后的独立评分。没有运行三任务 27-run 对照，没有验证相对普通 Agent 或线性循环的研究收益。

## 已实现的协议

| 能力 | 实现与验证范围 |
| --- | --- |
| 路线选择 | 洗牌采样、新路线机会、两个独立 Advocate、有限质疑与回应、Judge 及有界重选；真实调用走通 |
| 调查合同 | Plan 固定起点、权限、评价和预算；假设可选，局部指标可新增并保留独立定义 |
| 实验图与版本 | 以前轮 ID 建立起点与引用，使用独立 Git worktree 保存小型源码和配置；三轮恢复与未列出源码依赖由测试验证 |
| 双层 Frontier | 全局与路线局部定义隔离，单目标最优集合、多目标非支配集合；真实运行发布一个全局候选，局部及多目标场景由确定性测试验证 |
| 研究理解 | 更新两层研究文档，保存快照；空闲路线人工编辑形成独立修订，历史结果纠正保留原记录 |
| 恢复 | 成功执行后的后处理错误复用收据，未知执行需核对后显式重试；发布中断按同一清单补齐，冲突保留人工内容 |
| 权限与预算 | 仅授权的 Execute 可写，调用和时间边界受控，最终评分不回到研究上下文 |

核心模块为 [core.py](../src/research_loop/core.py)、[protocol.py](../src/research_loop/protocol.py)、[evaluation.py](../src/research_loop/evaluation.py) 和 [harness.py](../src/research_loop/harness.py)。新 Study 格式为 version 2，旧 v1 目录保持原样，不自动迁移。

## 本地检查

```sh
uvx ruff format src tests benchmarks/cactus
uvx ruff check src tests benchmarks/cactus
uv run python -m unittest discover -s tests -v
```

Ruff 检查通过，44 项测试通过，最终一次完整测试约 4.15 秒。本机执行时通过 `UV_CACHE_DIR` 与 `UV_TOOL_DIR` 使用 `/tmp/` 中的任务缓存。

测试包含真实临时 Git 仓库、阶段协议、CLI 适配、受限训练请求解析，以及成功执行后 checkpoint 失败、普通发布写入错误、进程丢失和人工编辑冲突的故障注入。它们检验控制与数学关系，不将固定回复当作研究成绩。仅供最终验收的测量在 Execute 接纳边界拒绝，评分须由循环外的可信组件完成。

## 真实执行设置

研究控制器和已登录的 Codex CLI 在本机运行，训练在 任务主机 的选定 RTX 4090 上执行。未复制登录凭据。模型请求为 `gpt-6.1-sol / medium`；CLI 返回的 usage 与请求记录不等于独立核验服务端型号或计费。

成功运行目录为 `studies/cactus-real-20261101-02/`，使用 1 个 iteration、最多 12 次 CLI 调用和 1800 秒预算。实际完成 10 次 CLI 角色调用：两次提案、两次质疑、两次回应、Judge、Plan、Execute、Review。它不是 10 次模型推理量的计费统计。

Execute 只能通过本地文件请求桥选择一次有限配置；桥只执行固定远程命令，返回开发指标和指定产物。模型没有远程 shell，命令工具网络关闭，开发和测试标签只保存在远端控制器目录。private test 在 Review 发布及候选冻结后评分，之后没有再调用模型。

| 项目 | 固定值 |
| --- | --- |
| 拟合 / 开发 / private 行数 | 11,340 / 2,835 / 3,325 |
| 模型 | 三个卷积层的 tiny-cnn，池化后单个二分类 logit |
| 输入 | RGB `[batch, 3, 32, 32]`，除以 255 |
| 训练 | 1 epoch，Adam，学习率 0.001，BCEWithLogitsLoss |
| batch / seed / CPU 线程 | 128 / 0 / 4 |
| GPU | 远端 GPU 1，经 `CUDA_VISIBLE_DEVICES=1` 使用 `cuda:0` |
| 选择规则 | 最终 checkpoint，无最佳 checkpoint 搜索、无预训练与增强 |

数据来自固定 revision 的公开 prepared mirror，官方制备与评分源代码也固定到 commit。一次性元数据检查确认了分区和标签的重建一致性；没有读取全部图像做校验或重新哈希数据。镜像图像字节未与 Kaggle 原始下载独立核验。来源、准备方式和准确版本见 [Cactus 任务说明](../benchmarks/cactus/README.md)。

## 结果

| 指标 | 实测结果 |
| --- | ---: |
| 开发 ROC-AUC | 0.9655275996206457 |
| private test ROC-AUC | 0.9705100349627824 |
| 成功闭环墙钟 | 386.29 秒 |
| 训练循环 | 3.139 秒 |
| 训练脚本总耗时，不含解释器与 import | 5.576 秒 |
| 初始化，不含解释器与 import | 1.131 秒 |
| 初始化后的首 batch | 0.417 秒 |
| 开始至首 batch，不含解释器与 import | 1.548 秒 |
| 排除首 batch 的训练吞吐 | 4,119.24 样本/秒 |
| PyTorch 峰值训练分配显存 | 78,624,256 bytes，约 75 MiB |

训练启动没有额外的数据集校验扫描。首 batch 前已知读取量为 442,829 bytes 的 CSV 与元信息，另有一个 batch 的图像读取；压缩图像字节数、解释器与依赖导入 I/O 未单独计量。90 个外部 1Hz GPU 样本的窗口峰值为 3%，窗口包含等待时间，不能作为训练平均利用率。

CLI 阶段时间为：选择 157.91 秒、Plan 62.95 秒、Execute 81.78 秒、Review 81.60 秒。可信桥的训练、开发评分和产物传输合计约 18.03 秒；本次小模型 smoke 的主要墙钟消耗在模型调用和编排。CLI 报告 input tokens 690,990，其中 cached input 456,576，output tokens 14,673；这是客户端 usage，不是独立计费审计。

Review 将候选标为 `valid=true / verified=true`，控制器发布唯一 Global Frontier 成员 `0001:cactus-0001-tiny-001-final`。Study 的 `goal_status=met` 只表示这次目标要求的真实训练、开发结果与报告已完成，不表示方法优于基线或完成机制验证。

证据入口：

- 成功运行摘要（私有运行产物，未随源码分发）、调用与 GPU 观测（私有运行产物，未随源码分发）。
- 实验节点与 Git 版本（私有运行产物，未随源码分发）、路线选择记录（私有运行产物，未随源码分发）。
- 执行报告（私有运行产物，未随源码分发）、评审报告（私有运行产物，未随源码分发）。
- 当前研究理解（私有运行产物，未随源码分发）、Frontier（私有运行产物，未随源码分发）。
- 运行与配置（私有运行产物，未随源码分发）、性能记录（私有运行产物，未随源码分发）、独立 private 评分（私有运行产物，未随源码分发）。

精确训练产物仍在远端任务的 `runs/<run-id>/`；本机保留 checkpoint、预测、实际源码、配置和报告。`implementation/` 保存本轮使用的小型实现源码快照。运行目录由 `.gitignore` 排除，不随源码分发。

## 已保留的失败与限制

首轮 `studies/cactus-real-20261101/` 在实际训练开始前失败：`reset_peak_memory_stats` 先于 CUDA 初始化，触发 `Invalid device argument`。已复现并修正为先初始化 CUDA；模型、损失、数据划分和评价协议未改变。原轮返回 incomplete、未产生候选，也没有进行 private 评分；失败摘要（私有运行产物，未随源码分发）保持原样。成功轮在新 Study 中执行唯一一次实际训练。

本次固定 tiny-cnn，只验证一个配置的真实通路，没有架构搜索、跨路线真实实验对比或多目标真实优化。Pi 仍未做真实调用；多路线恢复、Local Frontier 和 Pareto 行为的证据来自确定性合同测试。服务器与继承的 PyTorch 环境并非独占、完整锁定的镜像；实际包版本已记录。关于研究收益的结论须等待[同预算对照](benchmark-plan.md)，不能由这次单次 AUC 推出。
