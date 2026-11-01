Source: https://github.com/Chen-HongRu/ProTok/blob/codex/fm-protok-parity/FM_PROTOK_PARITY.md
GitHub blob: c79a02a0d8f80faf386e27d7f3a580099779c4ce
Retrieved: 2026-11-01

# FM 达到 ProTok 重建性能

研究分支：`codex/fm-protok-parity`。目标是让 FM 从噪声积分得到的最终结构达到冻结 ProTok 原解码器的重建性能。先以 Cα 为对象；EGNN、X/H readout、对齐和参数化是研究手段，不是必须保留的路线。

## 比较契约

- 固定 teacher：`base_v2_token32code4k.ckpt`，32 个 tokens、4096 codebook；复用由该 teacher 生成的 raw hidden cache。当前阶段不通过更换 teacher 或增加条件信息来缩小差距。
- 固定现有 4491 条 validation，按 `(row_id, name, length)` 核对身份和有效残基。Teacher 旧表没有相同 row_id 时，通过已核对的唯一 name 与 length 映射。
- 主指标是最终采样 Cα 的 proper SO(3) Kabsch RMSD，单位 Å，逐蛋白计算后等权平均；不允许镜像或缩放，不以训练插值上的 clean prediction 替代生成终点。
- 同时报告 Cα lDDT、TM-score、RMSD 中位数/P90、逐蛋白配对差异、训练更新数及推理成本。训练 loss 下降本身不构成达到目标。
- 首轮沿用 Euler100、采样分母下限 0.01，以及同一 `row_id` 的固定初始噪声。若改变 sampler 或预算，单独标注，并保留原协议下的对照。
- 对照实验固定 hidden、数据顺序、时间/噪声策略、宽度、优化器和 scheduler；改变的因素明确列出。相同 seed 不保证不同结构的参数初始化逐位一致。

当前同集合 teacher Cα RMSD 为 **1.437979 Å**。阶段判据是 FM 的全量最终采样平均 RMSD 不高于这一参照；它不是任意任务上的固定阈值。换 split、残基支持或评估协议后，必须重新配对 teacher。

达到单次验证判据后，固定候选 checkpoint，用预先固定的新噪声复核完整集合，并检查其他结构指标和失败尾部；训练重复与独立留出集结果另行记录。现有 validation 已多次用于选择方案，不能称为独立测试。尚未完成复核的候选只标为“验证集达到”，不宣称稳定或普遍优于 ProTok。

## 当前研究配置：V1

本轮value消融已收敛到V1（三方向node value），作为本研究分支后续X/H实验的推荐配置。4k X读出的CA RMSD为V1 3.793Å、V2.1 3.803Å、V2 6.616Å；V1本轮实测耗时和显存最低。V2.1尾部与V2局部几何各有优势，选择V1不表示全面支配其他方案。三版均未解决H readout早期最优后退化。

[V1定义与入口](V1_BASELINE.md)给出当前实现契约；[阶段结论](reports/frame-values-convergence-20261028/README.md)集中记录同一步数结果、成本、配对分布与限制。V2.1 X/H各50k均已完成，不能据此推断V1/V2的50k表现。全部运行证据固定在`archive/frame-values-v1-v2-v21-20261028`；本轮没有合并main或启动新训练。

## 当前状态

2026-10-28，目标尚未达到。已有阶段汇总见 [实验结论](reports/fm-protok-parity-20261027/README.md)，此前H精细网格的历史中途记录见 [新旧H对比](reports/h-readout-optimum-20261027/COMPARISON-20261028.md)。

| 用途 | 当前参照 |
| --- | --- |
| 性能目标 | ProTok 原解码器：1.437979 Å |
| 最接近目标的已测 FM | 新版shared-attention H-D3 + Kabsch，1300 steps：1.473196 Å；2026-10-28中途结果，单训练seed/采样噪声，独立噪声复核未完成 |
| 当前研究推荐 | V1三方向node value，X/H各4k已验证；使用显式node_frame配置，公共main基线不变 |
| 当前阶段 | V1/V2/V2.1 value消融已完成并归档；H读出后期退化的机制仍待诊断，本次未启动新实验 |
| 研究边界 | PCA 主轴方案、直接 v-predict、多通道向量版及 NCAC 模板方向仍暂停 |

## 分支完成与回收

研究分支保留成功和失败的实验、对应配置、曲线及来源；历史运行加载其固定代码快照。候选达到上述指标并通过必要的执行验证后，将最小可复现实现、配置、针对性检查和结论摘要独立提交，再选择性带回 `main`。训练脚本、冻结范围和评估协议是改进的一部分，不能只摘取网络文件。

`main` 保留推荐基线、公共契约和紧凑结果索引；淘汰的模型变体、专用启动脚本和完整过程记录留在研究分支的固定归档 tag 中。保留指向 tag/commit 的历史入口。合并范围按 [工作流](WORKFLOW.md) 执行。

