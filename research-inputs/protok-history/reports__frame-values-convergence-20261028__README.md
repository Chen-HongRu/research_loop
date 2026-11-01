Source: https://github.com/Chen-HongRu/ProTok/blob/codex/fm-protok-parity/reports/frame-values-convergence-20261028/README.md
GitHub blob: 6351e00f09f00ab3a9cb288020bbafc8006df613
Retrieved: 2026-11-01

# 三方向 Value 消融阶段结论：收敛到 V1

2026-10-28。当前研究分支 `codex/fm-protok-parity` 推荐使用 V1（三方向 node value）。本轮结论是选择后续研究起点；FM 达到 ProTok 性能的目标尚未完成，H readout 的后期退化仍未解决。固定归档标签为 `archive/frame-values-v1-v2-v21-20261028`。

## 实验问题与控制项

本轮固定 shared invariant attention、D3/width128/heads4/head_dim16，比较坐标更新的 scalar value：

| 版本 | Value | 几何方向 |
|---|---|---|
| V1 | node MLP128(H_j) | R、C、归一化 R×C |
| V2 | pair MLP16(H_i,H_j,d_ij) | 同V1 |
| V2.1 | node MLP128 + pair MLP16 residual，pair输出层零初始化 | 同V1 |

同一冻结 teacher/raw hidden cache、449771 train/4491 validation、seed42、data/noise20261022、microbatch32/K4/acc8（每更新256蛋白）、FP32/TF32关闭、Proteina时间分布、loss floor0.05、采样floor0.01、Euler100、坐标尺度10。AdamW1e-4、betas(0.9,0.99)、weight_decay0.05、warmup1000、cosine horizon50000相同。H预测Y并proper-Kabsch对齐当前Xt；X直接读出坐标，两类读出分别比较。

V1/V2/V2.1各有X/H的4k实验，0/500/…/4000全4491验证并保存checkpoint；V2.1另有fresh X/H 50k，每2k全量验证。V2.1短/长训练的0/2k/4k结构指标逐值相同。配对时核对(row_id,name,length)一致。这里的同预算指相同optimizer更新和训练样本量，不是相同计算时间。

## 同步数结构结果

全部为同4491条validation、逐蛋白等权CA RMSD（Å，越低越好）。

| 读出 | 版本 | 1500步 | 2000步 | 4000步 |
|---|---|---:|---:|---:|
| X | V1 | 7.579102 | 6.035586 | 3.793105 |
| X | V2 | 7.487652 | 6.903264 | 6.616270 |
| X | V2.1 | 7.431390 | 5.603929 | 3.803133 |
| H | V1 | 1.496785 | 1.541143 | 1.947591 |
| H | V2 | 1.494929 | 1.530972 | 1.981800 |
| H | V2.1 | 1.492565 | 1.588023 | 1.991452 |

4k X读出的误差关系为 **V1≈V2.1<V2**。V2.1在2k比V1低7.15%，但到4k均值高0.26%；增加pair残差没有形成稳定的平均精度收益。

均值不代表全面支配：4k V2.1相对V1仅31.08%蛋白改善，差值中位数+0.2561Å；它的P90更好（7.1280 vs9.0052Å），中位数更差（3.0051 vs2.6175Å）。V2的局部几何更好：4k lDDT V2/V1/V2.1为0.6447/0.6230/0.5999，相邻CA距离RMSE为0.9340/1.0593/1.6157Å。选择V1依据平均重建精度、简洁性及成本，而非所有指标领先。

## 同为4k运行的实测成本

| 版本 | X 秒/更新 | X 峰值GiB | H 秒/更新 | H 峰值GiB |
|---|---:|---:|---:|---:|
| V1 | 0.28524 | 8.1901 | 0.25351 | 6.6642 |
| V2 | 0.39371 | 12.3591 | 0.32709 | 9.3551 |
| V2.1 | 0.42140 | 12.1648 | 0.34597 | 9.4218 |

每更新耗时由完成凭据的steady吞吐换算，排除前10更新和全部验证；显存为训练peak allocated。V1相对V2.1，X省32%时间/33%显存，H省27%时间/29%显存。不同节点、单次运行，不是硬件无关性能保证；推理速度未单独基准测试。V1只按节点计算value，pair版本额外执行逐边MLP。

## 尚未解决：H readout 后期退化

三版H在500步验证网格上的最佳已测点均为1500附近，之后结构指标退化。固定256条TRAIN probe也从1500→4000退化：V1 1.4740→1.9820Å，V2 1.4727→2.0254Å，V2.1 1.4706→2.0265Å；同期FM loss仍可下降。

这表明loss改善与ODE采样指标恶化并存，退化并非只发生在validation。尚不能据此定位为Kabsch、solver、优化目标或其他唯一原因，也不能排除所有过拟合机制。选择V1并不表示该问题已经解决。

## 50k结果与可作出的结论

V2.1 X最佳已测32k=2.193842Å，50k=2.215555Å，后期约2.2的平台。H 50k=1.892299Å，仍差于短运行1500步的1.492565Å。两组均完成50000 optimizer更新、26次全4491验证，W&B服务端finished/50000/completion_verified=true。

V1/V2只有4k，不能把V2.1的50k结果当作同预算架构优势，也不能声称V1长程一定更好。固定teacher参照1.437979Å；本轮未达到研究目标。结果均为单训练seed与固定采样seed，validation被多次用于选型，尚非独立测试结论。

## 后续研究约定

V1作为当前推荐配置：`configs/ca_frame_v1_d3_4k.json`及`configs/ca_frame_v1_h_d3_4k.json`，显式使用`value_mode=node_frame`。保留现有初始化、网络计算、loss、数据顺序和评估协议，不因归档重写默认构造逻辑或旧实验身份。V2/V2.1作为历史消融留在研究分支及固定标签，后续实验优先使用V1。

下一阶段问题是H读出早期checkpoint到后期checkpoint的预测误差与采样行为为何背离。此处只记录研究问题，不启动新训练、改sampler或合并main。PCA主轴与NCAC暂停草稿继续保留在本地。

## 可追踪来源

- [V1/V2四组4k结果](../frame-h-v2-v1-20261028/RESULTS.md)：配置、曲线、完成凭据、TRAIN probe。
- [V2.1方案与4k执行](../frame-v2_1-20261028/README.md)。
- [V2.1与V1/V2配对、50k终点](../frame-v2_1-50k-20261028/RESULTS.md)：`comparison-records/`包含八组配置、曲线、完成凭据与protocol；`paired-comparison.json`包含配对分布。
- [记录索引](../records-index.json)列出纳入Git的文件；checkpoint/cache/原始日志仍保存在服务器，W&B仅上传配置及指标。

