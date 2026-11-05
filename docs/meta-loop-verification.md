> Historical record: commands and results describe the former runtime and do not validate the current system. See [current entry](../README.md) and [retirement](runtime-retirement.md).

# Meta Loop 实现与验证

> 历史验证摘要：以下数值来自开发时保留的记录，本次公开整理未重跑训练。原始运行产物未随源码分发，读者无法仅凭此摘要独立复核这些数值。

日期：2026-11-01。目标：在固定任务质量与资源条件下，减少路线选择和 Review 开销。本次只验证一个候选、一组真实开发对照及受限监督任务，不执行正式确认套件。

## 实现

- `strategy.py`：`debate/full` 为监督策略；候选 `direct/concise` 保留两个独立提案、Judge、Plan、Execute、独立 Review 和完整研究记忆发布。候选只修改两个策略配置，不能修改任意源码。
- `core.py`：普通 Study 接纳策略配置，持久化选择与 Review 的真实墙钟区间。Review 计时覆盖核查、Frontier 更新和发布；失败区间保留，无法恢复的崩溃时间标记缺失。
- `meta.py`：冻结源码、依赖引用和策略为只读 Git 版本；保存父监督引用、协议、预算预留、可信作业回执、评价和决定；实现提名、正式门槛、发布绑定、原子监督指针、恢复与回滚记录。
- `meta_inner.py`：每个被测版本由独立进程加载，核对模块路径和 Git commit；结果写在源码目录外。可信桥强制两次训练使用完全相同的配置。固定 runner 源码按协议散列归档，后续子进程从归档启动。
- `run_meta_smoke.py`：使用冻结 S 运行真实 Meta Study；Execute 请求候选冻结、S/C 开发评价和监督 canary；Review 更新两层研究文档与开发 Frontier。外层记录必须匹配实际策略产物及可信比值。

代码入口：[Meta 驱动](https://github.com/Chen-HongRu/research_loop/blob/0d81cbc44025e0e8742a9818fc18326484023790/src/research_loop/meta.py)、[策略](https://github.com/Chen-HongRu/research_loop/blob/0d81cbc44025e0e8742a9818fc18326484023790/src/research_loop/strategy.py)、[真实 runner](https://github.com/Chen-HongRu/research_loop/blob/0d81cbc44025e0e8742a9818fc18326484023790/benchmarks/cactus/run_meta_smoke.py)。

## 固定开发协议

| 条件 | 值 |
| --- | --- |
| 任务 | Aerial Cactus，固定 train/dev/private 划分，沿用已准备的真实数据 |
| 模型、优化器、损失 | tiny-cnn、Adam、BCEWithLogitsLoss |
| 配置 | epochs=1、lr=0.001、batch_size=128、seed=0、CPU threads=4 |
| 计算 | 显式指定远端 GPU；本机已登录 Codex 组织研究，不复制凭据 |
| Agent | 请求 `gpt-6.1-sol / medium`；请求记录不等于服务端身份独立核验 |
| 配对 | 一个 S/C 配对，顺序在候选产生前固定；相同初始任务状态，无跨 run 研究记忆继承 |
| 单个内层上限 | 12 次 CLI 调用、1800 秒、5,000,000 reported tokens，两个版本相同 |
| canary | S/C 各完成一次受限策略提案和 Review；没有训练请求桥，不展开第三层 |
| 总上限 | 1 个候选、5 个研究作业、60 次 CLI 调用、10,800 秒、30,000,000 reported tokens |
| 评分 | 开发反馈用于本轮 Review；private 在所有模型调用及外层发布完成后评分 |

总秒数同时约束批次真实墙钟和作业耗时累计。外层等待内层时，两层耗时都计入累计预算。Token 在每个完整 CLI 调用边界检查，单次调用造成的超支如实记录并停止后续工作；它不是服务端硬性输出配额。

`O` 为选择与 Review 的控制器墙钟之和；`T` 为父进程启动被测子进程至 Review 发布、实际产物核查完成；`B=input_tokens+output_tokens`，缓存命中已含在输入中，不重复加 reasoning tokens。缺少 usage、完成回执或阶段时间不产生有效成绩。

## 执行

需要已准备的远端 Cactus 环境及一个支持目标模型的准确 Codex CLI 路径。Study 目录必须不存在。

```sh
uv run python benchmarks/cactus/run_meta_smoke.py \
  --study studies/meta-cactus-unique \
  --codex /absolute/path/to/codex \
  --host TASK_SSH_ALIAS \
  --remote-base /path/to/task \
  --gpu 1

uv run research-loop meta status studies/meta-cactus-unique
```

公开研究位于 `<study>/research/`；冻结版本、内层工作区、原始评分与预算收据位于同级 `.<study-name>.meta-controller/`。同一作业 ID 不可再次提交；未知效果先核对，不自动重复训练。

只有已收到成功 CLI 收据、但测量候选映射被控制器拒绝的 Review，允许在同一调用预算内恢复一次。原失败回复、耗时与 token 保留；Execute 不重复。无数值 measurement 的任务返回 `candidate_reviews=[]`，实际产物核查写入研究文档。

普通 Study 也可通过 `init --selection-policy direct --review-policy concise` 使用候选策略。默认保持 `debate/full`。

## 检查

Ruff 格式与检查通过，72 项 unittest 通过，完整一次约 6.78 秒。测试覆盖原有控制与 Frontier 语义、两种策略的调用顺序和发布、冻结来源、配对身份、预算与未知效果、质量/时间/token 门槛、开发协议拒绝晋升、晋升及回滚的故障注入恢复。新增的实际控制器测试验证结构化 Review 错误只恢复一次，原 Execute 只调用一次，全部耗时与调用仍被计入预算。

正式晋升成功路径使用受控测试回执验证；测试数值不作为研究效果证据。真实开发结果另列。

## 真实结果

成功轮为 `studies/meta-cactus-real-20261101-03/`，协议 `meta-cactus-development-v2`。外层使用冻结 S 完成一个真实 Meta iteration，发布路线与全局研究文档及开发 Frontier。五个作业全部完成，42 次角色 CLI 调用均成功，合计 reported input+output tokens 为 3,625,833；没有触发 Review 恢复。外层 Study 墙钟约 1,347.49 秒，包含等待内层评价。角色调用数不是模型推理次数或计费统计。

| 指标 | 监督版 S | 候选版 C |
| --- | ---: | ---: |
| 角色 CLI 调用 | 10 | 6 |
| 选择秒 | 166.331 | 72.108 |
| Review 秒，含发布 | 72.362 | 80.282 |
| 选择 + Review 秒 | 238.693 | 152.390 |
| 完整闭环秒 | 382.745 | 263.917 |
| input + output tokens | 729,984 | 564,061 |
| 其中 cached input | 496,768 | 445,952 |
| 开发 AUC | 0.9655299438 | 0.9655279345 |
| 最终 private AUC | 0.9705073508 | 0.9705061307 |

本轮 `rO=0.638436`、`rT=0.689537`、`rB=0.772703`，分别对应开销降低 36.2%、完整耗时降低 31.0%、reported tokens 降低 22.7%。主要收益来自选择阶段；Review 单项耗时增加约 10.9%，没有证明 `concise` 提示改善了 Review 开销。与上一轮的差异也表明，单个配对不能确定稳定的时延收益。两次配置和训练源码一致，微小 AUC 差异不作为框架提升证据。

S/C 均通过受限监督 canary，分别使用 10/6 次角色调用，完整发布研究文档，训练请求数均为 0。外层 Review 将唯一开发候选标记为 `valid=true / verified=true`，Global Frontier 发布 `0001:direct-concise-v1`，`mean_rO=0.6384357013986618`。这不赋予晋升资格。

固定驱动实际判定为 `inconclusive`，失败类别 `development_only`，监督指针保持 S。成功晋升、回滚和中断恢复的证据来自测试；本轮没有实际生产晋升。该轮即使满足单个配对的收益数值，也没有六组确认与读取隔离证据，不能绕过固定合同。

版本及来源核对：

- A0/S：`24c8fac79338e890ea9a6f63303b6610c7225daf`。
- C：`bbc83aee16e618c815154d158b5bfbd56839c823`，父版本为 S。
- 协议：`b866f2429d272362a3ab7cf41d738b4dc6719ed897d649038aefe03b0df4ea9c`。
- 两个子进程的 `core/harness/protocol/strategy` 模块均从对应冻结目录实际加载，Git HEAD 匹配，源码工作树保持干净、目录和策略文件只读。
- 最终 private 评分文件生成时间晚于全部 42 个角色调用结束时间；评分后没有新的研究角色调用。private 没有进入本轮 Agent 上下文或研究记忆。

训练计量如下。时间不含解释器和 import；它们已包含在父进程测量的完整闭环 T 中。

| 训练观测 | S | C |
| --- | ---: | ---: |
| 初始化秒 | 1.128 | 1.167 |
| 初始化后的首 batch 秒 | 0.349 | 0.345 |
| 开始至首 batch 秒 | 1.478 | 1.512 |
| 实际训练秒 | 3.006 | 2.989 |
| 排除首 batch 的 samples/s | 4,219.91 | 4,240.49 |
| 峰值分配显存 bytes | 78,624,256 | 78,624,256 |

训练启动额外验证为 0 秒、0 bytes；首 batch 前已知元数据与 CSV 读取为 442,829 bytes，另有一个 batch 的图像读取。压缩图像字节、解释器/import I/O 与 GPU 利用率未测，不用窗口空闲值推算训练利用率。数据没有被重新全量扫描或哈希。

证据入口：成功摘要（私有运行产物，未随源码分发）、可信观测汇总（私有运行产物，未随源码分发）、候选冻结（私有运行产物，未随源码分发）、外层评审（私有运行产物，未随源码分发）、全局研究记忆（私有运行产物，未随源码分发）、开发 Frontier（私有运行产物，未随源码分发）、晋升判定（私有运行产物，未随源码分发）。原始调用、固定 runner 和最终评分保留在同级受控目录。运行目录不随源码分发。

## 保留的失败

`meta-cactus-real-20261101` 使用 PATH 中的 CLI 0.156.0，在首个角色调用返回 `model_not_found`，没有提交 GPU 训练，usage 不完整，完整预算预留保持占用。改用上次 Cactus 接线使用的桌面内置 CLI 0.166.0-alpha.5.1，模型请求仍为 `gpt-6.1-sol / medium`。失败摘要（私有运行产物，未随源码分发）

`meta-cactus-real-20261101-02` 完成了两个真实 GPU 闭环，随后 S canary 因空 measurement 对应了非空 `candidate_reviews` 而被严格控制器拒绝发布。外层如实返回 incomplete，未提交有效全局 measurement，没有 private 评分或晋升。配对实测（私有运行产物，未随源码分发）、外层摘要（私有运行产物，未随源码分发）

| 该轮观测 | S | C |
| --- | ---: | ---: |
| CLI 调用 | 10 | 6 |
| 选择 + Review 秒 | 220.075 | 180.836 |
| 完整秒 | 374.944 | 295.212 |
| input + output tokens | 717,095 | 571,028 |
| 开发 AUC | 0.9655266 | 0.9655453 |

该轮 `rO=0.821705`，没有达到 20% 的主要收益门槛。修订只明确无测量时的既有 Review 合同，并增加受限的只读恢复；候选策略、训练、评分和晋升阈值不变。新协议 `meta-cactus-development-v2` 重新测量 S/C，不复用旧成绩；两个协议的结果分别报告，不取最好值或混合平均。

包含修复过程共进行了四次 tiny-cnn GPU 训练：失败协议中的一组 S/C，以及修正协议中的一组 S/C；没有执行 27 组或多候选大规模搜索。

## 边界

只实现一个不可变评估批次。下一代批次的新确认资源续接和常驻监督进程尚未实现；正式确认任务 Denoising Dirty Documents、Spooky Author Identification 尚未接入真实适配器。

本机同一用户的只读目录不能保证跨 run 读取隔离，该隔离未验证。开发协议明确不具备正式晋升资格；正式接口要求读取隔离已由外部运行环境实际建立，并满足完整确认套件。Cactus 已公开的 private 划分也不被当作新的盲测证据。

本次减少选择调用和压缩 Review 同时发生，不能分别归因。单个配对没有估计时延噪声，服务器与模型服务均非独占。即使本轮比值满足收益阈值，也不能推断跨任务收益或自动晋升。
