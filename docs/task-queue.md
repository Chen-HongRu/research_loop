# 人工实验与任务调度

Codex 将讨论后的问题、约束和预算保存为任务，通过 CLI 提交。提交只发布请求，不调用模型或启动计算。后台 supervisor 在轮次边界读取请求；dashboard 提供只读队列、进度、证据和费用视图。

## 任务

一项任务对应一次有边界的调查。任务属于一个 Study，使用稳定的 `id`。相同 ID、相同请求重提返回首次提交的记录和基线；内容不同则拒绝。有意复验使用新 ID。

| 字段 | 定义 |
| --- | --- |
| `version` | `1`，可省略 |
| `id` | 1–64 个字母、数字、下划线或连字符，首字符为字母或数字 |
| `kind` | `investigation`（默认）或 `long_training` |
| `title` | 展示名称，默认使用 ID |
| `instruction` | 问题、指定行动、对照、固定条件、判断标准及预期产物 |
| `route_id` | 主调查的已有路线 ID 或 `new`（默认） |
| `base_experiment` | 已完整发布评审且有源码版本的轮号，例如 `0012`；默认使用 Study 原始基线，无基线的只读 Study 使用提交时的 Git HEAD |
| `priority` | 整数，越小越优先，默认 `100`；同优先级按提交时间排序 |
| `allow_changes` | 是否允许修改候选代码，默认 `true`；`false` 仍允许提交评价及生成结果文件 |
| `budget` | 人工长训必须声明的独立计算授权 |

提交时冻结准确的源码 Git 版本和来源。排队期间新的 Frontier 不改变任务基线。主调查执行指定问题，直接进入 Plan → Execute → Review；Plan 不能将任务改成自主选题。自动探索在没有待执行人工主调查时继续使用原选择策略。

只读 Study 的人工调查也在独立的冻结源码 worktree 中执行，Agent 保持只读权限。输入使用已提交的 Git 版本，未提交修改不进入任务源码；默认只读工作区没有 Git 提交、或指定的已发布调查没有可恢复版本时，提交明确失败。旧请求缺少源码版本时停止在 Execute 之前。提交仅解析小型 Git 引用元数据，执行准备只创建本轮源码工作区，不扫描或哈希数据集与 checkpoint。

```json
{
  "version": 1,
  "id": "readout-ablation-001",
  "kind": "investigation",
  "title": "固定训练条件的 readout 消融",
  "instruction": "仅修改 readout，其他训练条件不变；按当前短训协议评价，记录负结果及证据限制。",
  "base_experiment": "0012",
  "priority": 10
}
```

主调查使用父 Study 原有的权限、调用和短训资源授权。排队不增加额度、不延长截止时间；当前实验先完成发布，再进入下一项任务。

ProTok 的 `allow_changes: false` 将提交时 `source.revision` 中的三个候选文件固定为本任务的实验对象。控制器在评估提交、冻结结果确认、完成回执复用及 Review/发布边界比较实际候选与该版本；工作区与作业快照同时变成另一候选也会阻断。发现不一致时保留现场，不恢复源码后接受其他候选的结果。

固定候选的原作业失败后，即使另有 debug GPU 授权，也不启动修复 Agent、debug smoke 或新的正式评估。原作业已可信结算且无活动执行者或未知效果时，保留错误、诊断与实际费用，以 incomplete、无有效测量进入独立 Review。重启与历史回执遵守相同约束；已有恢复作业指针保持阻断，不能用后续分配替换原失败。效果或费用未知时只核对原作业，不重提、不按零费用处理。需要修改候选时，另建 `allow_changes: true` 的任务，从原版本开始并保留独立身份与结果。

任务约束不改变 Study 的操作系统写权限。评估请求、日志和结果仍可写；当前保护在控制器边界发现违规修改，不能保证执行期间候选始终不可写。若需操作系统强制保护，应将候选源码以只读方式提供给 Agent，同时提供独立可写输出目录。新增身份核对仅由主控制器在上述边界读取三个候选文件及对应 Git 源码，不读取数据集或 checkpoint，不在 rank、worker 或训练热路径重复；具体耗时与读取字节数未测量。

## 人工长训练

ProTok 人工长训采用已有 `se3-long-v1-10k-full4491-seed42` 协议：fresh seed42、10k 更新、每 2k 全量开发集验证，保持原损失、mask、优化器、50k scheduler 和评价器。可以在指定基线上修改三个候选文件后直接长训，也可以设置 `allow_changes: false` 复验准确候选。两者均无需自动提名、Frontier 改善或新增短训成绩。

人工长训在独立可写子 Study 中运行。Plan 固定协议；Execute 的 Agent 仅实现候选，退出后由可信控制器冻结源码并调用受保护长训执行器；独立 Review 核查完整或部分轨迹。禁止通过这条入口更改受保护的训练和评价定义。

`budget` 包含以下字段，必须由提交者依据实际用户授权填写：

| 字段 | 定义 |
| --- | --- |
| `authorization` | 该任务实际授权的说明 |
| `gpu_seconds_per_run` | 本次单 GPU Slurm 分配上限，正整数且为 60 的倍数 |
| `total_gpu_seconds` | 任务总 GPU 秒，至少等于本次分配上限 |
| `deadline` | 进入新调查的绝对 Unix 时间戳 |

例如，以下预算结构中的 `deadline: 0` 已过期；提交实际任务时使用用户授权的截止时间和额度：

```json
{
  "version": 1,
  "id": "readout-long-001",
  "kind": "long_training",
  "instruction": "在指定候选上实现讨论确定的 readout 改动，直接执行固定 10k 长训，评审完整轨迹及限制。",
  "base_experiment": "0012",
  "allow_changes": true,
  "budget": {
    "authorization": "本任务的实际用户授权说明",
    "gpu_seconds_per_run": 10800,
    "total_gpu_seconds": 10800,
    "deadline": 0
  }
}
```

调度器管理主调查、自动提名长训和人工长训三个通道。父 Study 的主调查按轮次串行发布；每个长训子 Study 有独立异步 worker，内部串行执行 Plan → Execute → Review。独立预算的人工长训可同时执行。自动提名的 Plan/Review 可重叠，GPU 分配共用一个槽；未知或未结算费用继续阻止该池的新分配，等待不等于重新授权。Slurm 决定真实资源可用性和排队时间。

某个子 Study 出错后，调度器保留其状态、attempt、作业和费用，并继续调度其他可运行任务。初始化前的错误保存在该任务目录的 `scheduler-error.json`；重启不自动重试。只要其他 worker 仍在推进，汇总状态保持 running 并单列 blocked 子任务；排空所有可运行任务后才报告需要处理的剩余错误。未完成的父 Study 主调查不因此跳到下一轮。

父调查连续五次相同故障只结束该对象的自动重试，记录 `needs_attention`；调度器仍发现新任务，独立冻结输入的子任务不因父 Execute 等待核对而停止。费用按原 `job.json` 核对归属，不信任损坏回执声称的 task ID。A 的费用异常只隔离 A 及其依赖；任务身份无法核实时，所有不能排除的人工预算都保持未知。自动池未知费用只阻止该池分配，安全 Plan/Review 和独立人工授权继续有效。共享 ownership/授权/控制无效才发布全局 STOP，且先于等待 worker 退出。

人工长训使用 `control/manual-training-jobs/` 和任务自己的预算，自动长训继续使用 `control/long-training-jobs/` 及原预算。短训 `used_gpu_seconds` 和自动长训 `long_training_gpu_seconds` 不包含人工长训；`manual_training_gpu_seconds` 单列实际人工费用，`total_gpu_seconds` 包含所有通道。失败、取消和分配超时的实际费用均保留。核账不完整时 `accounting_status` 为 `partial`，受影响小计和总额为 `null`；`known_gpu_seconds` 仅为已知费用小计，不代表可用额度，也不释放未知作业预留。

入轮截止只阻止新调查。已接纳的调查可以在截止后完成训练和 Review；Slurm 分配及单次模型调用仍有自己的上限。排队等待不消耗 GPU 分配秒数。准备基线时过期、尚无调用或入轮记录的请求标为过期，后续有效任务仍可执行。已开始或效果未知的任务保留原身份等待核对。

## 提交与执行

以下命令在保存 Study 的主机上执行，Codex 可以通过已有连接调用。得到该主机的提交回执后，请求不依赖当前聊天存活。

```sh
uv run research-loop task submit /path/to/study --request /path/to/task.json
uv run research-loop task list /path/to/study
uv run research-loop task status /path/to/study readout-long-001
uv run research-loop task priority /path/to/study readout-long-001 5
uv run research-loop task cancel /path/to/study readout-long-001
```

优先级调整和取消仅适用于尚未绑定执行的任务。任务已绑定后，以实际 Study 阶段、完成回执和发布记录为准。运行中的 ProTok supervisor 自动启动两个后台训练通道；服务停止时新请求继续保留在队列中。也可显式推进一项人工长训：

```sh
uv run python -m integrations.protok.tasks --root /path/to/run
```

提交和运行是两个操作；CLI 不会自行部署或重启后台服务。原 STOP、ownership 和冻结 runtime 约束继续适用。

## 结果、知识与恢复

请求位于 `study/tasks/<ID>/request.json`，绑定记录指向主调查或 `study/tasks/<ID>/study/`。执行阶段和结果从原 Study 记录派生，不维护另一套执行游标。人工主调查直接更新原研究文档；人工长训的已发布评审在父 Study 下一轮边界冻结为 `context/task_results.json`。`research-loop knowledge STUDY` 的 `tasks` 字段也包含最后一轮之后完成的人工长训知识。

完整、可比且通过核查的主调查测量沿用原 Frontier 规则。人工长训维护自己的评价及 Frontier，不能参与短训排名；部分轨迹需要逐 checkpoint 独立接纳。已接纳的人工长训 checkpoint 可以使用原 `long-checkpoint:<L…>:<step>` 输入接口，保留实际作业和评审来源。

STOP 取消自有工作；HANDOFF 阻止新入轮并排空已接纳调查。未知、已准备或已提交的作业不自动重提。停止对应执行进程并核对 retained effects 后，显式恢复同一任务：

```sh
uv run python -m integrations.protok.tasks --root /path/to/run --recover readout-long-001
```

已存在作业时仅核对原作业，保留原 Execute attempt、费用与请求；不会申请第二次分配。候选已经完成 authoring、但尚未形成作业记录的中断，可在核对原 attempt、源码签名和无作业准备记录后完成首次提交，保留旧源码快照。STOP 未解除时只允许核对已存在作业，不允许首次提交或开始 Review。

恢复检查仅读取小型控制记录和候选/受保护源码：候选源码签名在 authoring 结束及显式恢复各检查一次，保护原候选不被后续编辑替换；该故障在本地注入测试中复现。数据集和 checkpoint 内容不被扫描、哈希或预热。检查耗时及生产源码字节数未测量，不重复到数据加载 worker。

恢复与普通推进使用同一个子任务锁，始终指向指定 ID。失败的只读阶段经检查后可先对 `study/tasks/<ID>/study/` 执行 `research-loop retry`，再显式推进该任务；仅初始化准备失败时，修复原因后直接指定 ID：

```sh
uv run python -m integrations.protok.tasks --root /path/to/run --task-id readout-long-001
```

指定 ID 不会清除未知 Execute 效果；这类执行仍使用上面的 `--recover` 核对原作业。调度器只在显式重试改变子 Study 状态或指定任务成功推进后解除保留的错误。STOP 仍取消全部自有工作；HANDOFF 阻止新入轮并等待所有已接纳子 Study 退出。

本地验证覆盖真实临时 Git/Study 发布、CLI 持久化、并发调度、预算隔离、恢复和看板。Slurm/容器/H100 真实执行及活动 runtime 升级仍需对应授权和实机验证。
