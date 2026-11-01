Source: https://github.com/Chen-HongRu/ProTok/blob/8ba779e23b177e18f246aa1b04320c5345e48d65/reports/ca-h-x-readout-20261027/README.md
GitHub blob: 6f459d5be543d05e072214423cde3053b7af3b3e
Retrieved: 2026-11-01

# H / X 读出实验交接（2026-10-27）

本页是本次交接的结果入口；旧报告中的“运行中”“待复核”等状态以本页及 records 中的实际快照为准。代码入口见 [CA_H_X_READOUT.md](../../CA_H_X_READOUT.md)。云端可离线读取这些 CSV/JSON；HPC 路径仅用于定位来源，不保证 ChatGPT 能访问。

## 实验身份与完成范围

| 实验 | 作业 | 已确认范围 | W&B run |
|---|---|---|---|
| Fresh H-D3 |767819|完成50k，Adam/scheduler50k，26个全量验证点|cahd3k4p50k767819|
| Fresh H-D12 |767820|用户停止；最后训练日志41950，最新checkpoint及全量验证40000；没有50k完成凭据|cahd12k4p50k767820|
| H-D3加密复现 |770139|完成6k，15个全量验证点；2k/4k/6k参数对原run漂移见JSON|cahd3dense6k770139|
| X-D3+Kabsch |770223|采集时仍运行，计划4k；本次归档最新全量验证2750，不是4k最终结果|caxkabschd3dense4k770223|

W&B项目：[LeWorldModel/ProTok](https://forge.coreweave.com/wandb/LeWorldModel/ProTok)。所有val点是4491条，横轴optimizer_step；train_probe/*单独标明256条TRAIN诊断。

## 已观察结果

| step | H-D3验证RMSD Å | X-D3+Kabsch验证RMSD Å |
|---|---:|---:|
|500|1.874180|25.501864|
|1000|1.493718|19.548051|
|1250|1.489560|17.755142|
|2000|1.535521|15.580288|
|2250|1.589781|15.176979|
|2750|1.820485|14.697125|

H加密曲线最佳已测点1250；6000验证RMSD2.053201、TRAIN probe2.093165。1250对应TRAIN probe1.460440。训练集诊断与验证采样性能共同变差，不能简单归因为只损害验证集的经典过拟合。训练probe只有256条，且约1757updates之前不保证每条都已见过。

X在2k验证/训练probe RMSD分别15.580288/15.854422，验证loss .990679、bond RMSE13.563403Å。H同一步分别1.535521/1.512029、loss .238613、bond RMSE .362420Å。X在训练集上也拟合差，但仍在改善，不能声明已到达容量上限。

原H-D3的50k RMSD1.867867，不应把1250最佳点当50k终点。独立复核汇总在records/recheck/，对原2k及后期checkpoint使用完整4491原噪声与128条新噪声；128条仅诊断，不是正式验证。

## 对原因的证据边界

X分支是受限几何残差：单位相对向量和单位叉积，tanh标量系数，全邻居平均。H直接解码形状再Kabsch对齐。稀疏有效邻居被平均稀释、方向抵消、attention分数复用为位移系数、gain .001近恒等初始化，都是值得诊断的机制，尚非已确认的主要原因。

Kabsch单次只旋转，保留内部距离；它可能改变学习梯度和整个Euler轨迹。当前对照说明“加Kabsch不足以追上H”，不能证明比纯X无改善。没有匹配的本次纯X-D3训练曲线。

X-D3有3次坐标更新/2次H更新；H-D3有2次坐标更新/3次H更新及MLP。相同随机种子不意味着共享模块初始张量相同。因此这是完整架构比较，不是同一trunk仅换一个末端算子的完全单因素实验。

归一化分母detach未切断整个几何梯度，但改变了归一化的Jacobian；H中间坐标层也使用此规则。现有测试通过不代表所有训练状态的SVD梯度均稳定。没有测得tanh饱和或梯度消失证据。

只读8条验证样本诊断作业770353在归档检查时仍PENDING；预期结果位于X实验根的readout-probe.json。不能把待运行诊断作为已测证据。保存配方见scripts/archive/ca-h-x-readout-20261027/。

## 数据与时间契约

同冻结teacher的raw hidden缓存，train449771/val4491；CA居中后除10。D3宽128、heads4、head_dim16、rawhidden1024；B32 K4累积8，FP32/TF32off，编译训练/eager验证。种子模型42、数据/噪声20261022；噪声每microbatch按step*8+micro变化，不是每个蛋白固定训练噪声。AdamW1e-4、warmup1000、50k余弦日程，6k/4k只改变停止时刻。

训练t为.02Uniform+.98Beta(1.9,1)；loss=MSE/max(1-t,.05)^2。验证loss的t为sigmoid(N(-.8,.8))，所以优化train/loss与val/loss不能直接当作同分布泛化差距。TRAIN probe使用与VAL相同的估计方法。采样从独立高斯噪声开始，Euler100，每步重算读出与Kabsch，速度分母floor .01。

## 代码与数据边界

本次不提交checkpoint、蛋白坐标、hidden缓存、完整日志、W&B二进制/上传进程状态，也不提交暂停NCAC草稿。原始结果保留服务器。运行作业代码不随本次Git整理变化。仓库另有W320未提交工作，不属于此次H/X交接。

