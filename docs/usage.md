# 运行、权限与研究记录

[返回 README](../README.md) · [AeroArm 接入](aeroarm.md) · [离线展示](showcase.md)

通过 Codex 自然语言使用时，先安装仓库根目录的 [research-loop skill](../SKILL.md)。安装后的所有通用命令使用 `python3 /path/to/installed/research-loop/scripts/research_loop.py` 替代本页的 `uv run research-loop` 前缀；启动器固定使用随 skill 安装的源码和锁文件，并保留调用目录。安装和环境检查见[安装指南](../references/installation.md)，继续、任务、停止与恢复见[操作指南](../references/operations.md)。本页的源码开发命令继续有效。

阶段结构化输出与研究执行请求通过 Pydantic v2 严格模型校验；同一模型生成 Codex/Pi CLI 使用的 JSON Schema。新增依赖由根目录 `uv sync --locked` 安装，不需要切换到 Agents SDK。控制器从冻结上下文派生动态选择和模型省略字段，首次调用与缓存接纳共用这些类型。预算、作业状态、身份、来源和核账判断仍在控制器中执行。

只读阶段的明确输出参数错误会带着拒收反馈返回原阶段，使用同一任务、冻结上下文和剩余预算。每次新调用照常计费并保留独立 attempt；不会因为纠错增加调用或时间额度。CLI/管线故障、来源/身份/核账错误及效果未知的作业仍然阻断，Execute 不会因输出不合规而自动重跑。

## Meta Loop

Meta Loop 复用同一 Study、Route、实验图与 Plan → Execute → Review，研究选择和评审策略。固定驱动冻结监督版与候选版，记录配对评估、总预算与作业回执，并独立决定晋升或保留监督版。首个候选仅修改策略配置，减少选择调用与 Review 重复表达。

普通 `init` 支持 `--selection-policy debate|direct` 与 `--review-policy full|concise`；默认 `debate/full`。Meta 入口为 `research-loop meta init/status/decide/rollback`。真实单批 Cactus runner、运行命令及边界见 [Meta Loop 实现与验证](meta-loop-verification.md)，协议见 [Meta Loop 设计](meta-loop-design.md)。单个开发配对不能触发正式晋升。

## 探索策略与研究 Scout

Study 可选用探索策略，为每轮分配研究类型，并在迭代边界冻结研究输入、Scout 建议和已发布洞见。Reviewer 综合本地证据与未解问题；宿主接入的只读 Scout 深入阅读论文与实现，提供独立外部视角。默认每推进一轮且至少间隔五分钟可调用，结果在下一轮边界供选择阶段审议，不改写当前 Plan。策略与边界见[探索策略](exploration.md)和 [Scout 说明](scout.md)。

通用库位于 `src/research_loop/`；任务后端、固定指标、资源租约和部署操作位于 `integrations/`。ProTok 的适配器保存在 `integrations/protok/`；实际研究输入和部署记录由项目方私下维护。

## 研究流程

```text
Study 目标与固定评价
  → 路线采样 → 两位 Advocate 独立提案 → 有限质疑与回应 → Judge
  → Plan 冻结调查合同与输入版本 → Execute → Review
  → 发布路线与全局研究文档、Frontier → 下一轮选择
```

路线组织一个问题、方法或局部目标；实验节点记录一次有边界的调查。采样使用洗牌轮转队列，提供已有 active 路线和新路线机会；新路线机会也可建议恢复暂停或关闭的路线。Judge 可选择具体行动、有限重采样或停止。Plan 无法落实选中行动时返回重选，不静默换题。

调查可以是优化、诊断、复验、文献核查或推导，不要求机制假设。执行完成、方法有效、知识变化、目标达到与是否继续分别判断。目标状态是 `met / not_met / undetermined`。提案、辩论、重试和不同 Agent 转述同一次结果不构成独立实验证据。

Global Frontier 按 Study 的全局评价版本和可比范围维护所有路线的已核查候选；Local Frontier 按路线、局部评价版本和范围维护。只有完整、可比、满足约束且经过 Review 核查的精确产物版本参与支配比较。单目标保留最优集合，多目标保留非支配集合；不同局部范围不直接排名。局部改善不能代替全局成功条件，候选被支配也不意味着路线应关闭。诊断或定性调查可以没有可排序候选。

## 实时观测接口

Plan/Execute 可自定义结构化日志与标签。控制器按游标增量同步，面板读取 `dashboard` 标签；Review 可按冻结字节边界读取全部标签。实时数据保持未评审状态，不直接进入 Frontier。接口、ProTok 接入和本地读取方式见[实时观测](telemetry.md)。

## 并行长训练

Study 可选启用长训策略。Review 为有实质 Frontier 改善或较大架构改变且取得改善的候选提出带对照和证据的提名；独立子 Study 复用 Plan → Execute → Review，与短训并行。已评审长训在下一轮边界冻结为知识输入，`research-loop knowledge STUDY` 也会包含最后一轮短训之后完成的长训知识。协议、授权、恢复和 ProTok 的 10k/全量验证接入见[并行长训练](long-training.md)。

## 人工实验队列

Codex 可通过 `research-loop task submit STUDY --request FILE` 提交讨论确定的实验。人工主调查在轮次边界优先进入 Plan → Execute → Review；人工长训支持新改动直接训练及既有候选复验，通过独立子 Study 与自动探索、自动提名长训并行，费用计入每项人工任务的独立预算。结果照常评审归档，并在后续轮次回流知识。CLI、预算字段、调度和恢复语义见[人工实验与任务调度](task-queue.md)。

ProTok supervisor 为各长训子 Study 分别调度；阻塞只保留在对应子 Study，独立人工任务可并发执行，自动长训共享分配槽和已结算预算门槛。父 Study 的主调查仍逐轮发布。

## 运行

要求 Python 3.11+、`uv`、已登录的 Codex 或 Pi。进程锁与取消机制面向 macOS/Linux。

```bash
uv sync
uv run research-loop init studies/proof-v2 \
  --goal examples/proof-goal.md \
  --harness codex --model gpt-6.1-sol --reasoning medium \
  --max-iterations 1 --max-calls 20 --max-seconds 900
uv run research-loop run studies/proof-v2
uv run research-loop status studies/proof-v2
uv run research-loop graph studies/proof-v2
uv run research-loop frontiers studies/proof-v2
```

`init` 不调用模型；`status`、`graph`、`frontiers` 只读。`run` 默认推进一轮，`--iterations 3` 最多推进三轮，还受总预算约束。默认总预算为 10 轮、100 次 CLI 调用、3600 秒；每次 Harness 调用默认超时 600 秒，Pi 的目录预检和实际请求共用此额度，初始化时用 `--timeout` 调整。没有后台无限运行。

路线选择默认 `--selection-seed 0 --debate-rounds 1 --max-resamples 1`。每位 Advocate 每轮各有一次质疑与回应；Plan 返回选择也使用同一重采样额度，调用均计入总预算。预算用尽只结束当前有界运行，不表示目标完成。

目标文件应写明目标、已有依据、允许访问和修改的范围、数据角色、成功条件、资源与停止条件。示例命题只检查端到端接线。指标调查可通过 `--evaluation examples/evaluation.json` 提供全局评价；该示例为开发集 AUC 最大化，使用前需将目标中的数据划分、评价器、聚合与候选选择规则固定为实际协议。定性调查可以不提供数值评价。

评价 JSON 使用固定字段：`id`、`comparison_scope`、`protocol`、`data_role`、`allow_feedback`、`objectives`、`constraints`。目标指标包含 `name / direction / unit`，方向为 `maximize` 或 `minimize`；约束包含 `name / operator / value / unit`，运算为 `le` 或 `ge`。开发评价可允许搜索反馈；最终验收评价须明确数据角色与反馈权限，禁止将仅供最终验收的结果回传搜索。Agent 无权修改全局评价或成功条件。

当前格式为 **version 3**。旧格式 Study 在任何状态写入前拒绝打开，不自动迁移；保留旧目录，在新目录重新 `init`，并在目标中明确引用仍适用的历史记录。

Plan 的 `markdown` 是权威科学合同，冻结问题、行动、条件、输入路径与版本、证据要求及解释边界。`timeout_seconds` 是控制器实际消费的顶层执行时限；输入声明不另存为结构化字段。`base_experiment`、历史引用和可选局部评价继续承担图关系、工作区恢复和 Frontier 比较职责。

## 客户端与权限

Codex 使用现有 CLI 登录，本项目不读取、复制或保存 token，也不创建 API key。Pi 使用其 `openai-codex` provider，请在 Pi 中自行 `/login`；不复用 Codex token。可用 `--executable /absolute/path/to/codex` 或 Pi 路径指定客户端。

Pi CLI 接线已验证 `@earendil-works/pi-coding-agent` 1.4.0。登录后可用以下命令创建独立 Study：

```bash
uv run research-loop init studies/pi-proof \
  --goal examples/proof-goal.md \
  --harness pi --executable /absolute/path/to/pi \
  --model gpt-6.1-sol --reasoning medium \
  --max-iterations 1 --max-calls 20 --max-seconds 900
uv run research-loop run studies/pi-proof
```

允许型号为 `gpt-6-astra / gpt-6.1-sol / gpt-6-luna`，默认 Sol/medium，不自动切换型号。Pi 请求前精确检查本地可用目录；目录缺少指定 provider/model 时停止，不改用其他 provider。两后端均通过 stdin 接收提示词，并使用相同的阶段校验和完成/恢复回执。Pi 仅在最终 `agent_settled` 未中止、最终 assistant 正常结束且 provider/model、有效 `thinkingLevel` 匹配请求后接纳 JSON；旧版缺少这些字段会明确失败。

目录匹配与 Pi 的配置回报不证明服务端实际采用该型号或推理强度；`process.json` 保留请求、客户端回报与未核实的服务端信息。真实 CLI 的本地固定响应验证和订阅调用的验证边界见 [Pi 验证记录](pi-harness.md)。后台 Scout 使用 Codex 的原生 web search，Pi 工具白名单不提供这一能力。

默认只读。需要修改代码或运行写文件程序时，在初始化授权 Execute：

```bash
uv run research-loop init studies/my-study-v2 \
  --goal /absolute/path/to/goal.md \
  --evaluation examples/evaluation.json \
  --workspace /absolute/path/to/experiment-workspace \
  --allow-write
```

选择、Plan 和 Review 保持只读。Execute 只能在授权实验工作区保存本次产物，不能覆盖旧证据或控制记录。控制器在执行前准备与合同输入版本一致的独立工作区；输入无法恢复时停止，不能用当前目录猜测替代。跨路线引用证据不会自动继承代码，融合候选须列明实际输入并重新评价。

可写研究使用干净的 Git 基线仓库；新建的空工作区会自动初始化。每个实验创建独立 worktree，并保存小型源码和配置的 Git 版本。数据与 checkpoint 用独立、不可覆盖的路径和版本引用保存，不扫描或哈希其全部内容。已跟踪的非源码数据发生变化时停止，保留现场供核对。

工作区不能等于或包含研究目录；默认基线位于研究目录下的 `workspace/`，实验 worktree 位于对应 iteration 的 `workspace/`。Codex 使用 `workspace-write` 沙箱，不自动批准提权，并关闭命令工具的网络访问；模型连接由 CLI 本身处理。需要远程执行时可接任务专用的受限接口，Cactus 示例采用可信文件请求桥。Pi 的工具白名单不是 OS 沙箱，启用写入会开放 bash/edit/write，需要隔离时在单独环境运行。

## 记录与恢复

```text
study/
  goal.md / study.json / state.json
  research.md / frontiers.json
  routes/index.json
  routes/R001/research.md / evaluation.json
  revisions/
  iterations/0001/
    iteration.json / selection.json
    context/                         本轮固定输入
    selection/round-001/              提案、辩论与 Judge attempts
    plan.md / execute.md / review.md
    route.after.md / global.after.md
    publication.json
    plan|execute|review/attempt-001/
      prompt.md / context.json / schema.json
      stdout.jsonl / stderr.txt
      response.json / completed.json / process.json
  workspace/                         按调查隔离的实验产物
```

当前 `research.md` 保存全局理解，各路线文档保存该路线的当前理解；详细事实、条件、失败与来源留在实验节点。图中的 `base_experiment` 与 `references` 指向已发布节点，轮号只表示时间，不自动继承代码或证据。输入、输出版本与评价引用绑定具体调查。

中断后再运行 `run` 会复用已有完成回执；未完成的只读阶段保留原输出后，以新的 attempt 继续。未完成 Execute 保留现场，需先核对外部效果。Agent 输出会实时写入日志，完成结果原子落盘；输出中途被打断时不承诺从最后一个 token 接续。具体边界见[恢复说明](recovery.md)。确认需要新 attempt 后执行：

```bash
uv run research-loop retry studies/my-study-v2
uv run research-loop run studies/my-study-v2
```

`retry` 本身不调用模型。成功执行后的控制器后处理重试保留执行收据；只有经核对的失败调用才产生新 attempt，研究文档冲突会重新评审，已准备的发布只补齐原清单。超时终止本地进程组，不能保证远程操作已取消。重试不是新实验或额外独立支持。

若完整 Review 已停止研究，但目标仍为 `not_met / undetermined`，用户后续明确要求继续时可执行 `research-loop resume STUDY --reason "实际继续请求"`，再用相应 runner 推进一轮。此命令不调用模型，记录继续原因并保留原预算、计数、证据和路线状态；预算耗尽、目标已达到、其他停止原因或未完成执行不能借此恢复。它不延长资源租约、不清除任务后端的 STOP，也不修改目标或评价。

Review 返回两份完整研究文档、路线状态和候选核查结果，由控制器按发布清单发布文档、定义、Frontier 和状态后推进游标。发布中断只补齐同一清单；发现其他修改时保留内容并停止，不让下一轮读取半发布状态。冲突后可用新文档重新 Review，沿用原合同与结果。请在空闲时修改目标、评价与研究文档。

Frontier 可从保留的评价定义、已发布测量与核查记录重建，不需要扫描原始数据或 checkpoint。显式重建命令会写入视图：

```bash
uv run research-loop rebuild-frontiers studies/my-study-v2
```

Review 可通过 `candidate_corrections` 对历史候选资格作有来源的纠正；控制器重建受影响视图并保留原测量。仅供最终验收的评价在循环外完成，不将最终分数送进 Review 或后续研究上下文。

ProTok SE3 的任务后端、运行接管与部署记录见[接入说明](../integrations/protok/README.md)。

## Cactus 真实接线

[任务脚本与受限接口](../benchmarks/cactus/README.md)将实际训练部署到显式指定的 SSH 任务主机，本机控制器调用已有登录的 Codex。入口为 `benchmarks/cactus/run_smoke.py`；参数包含新研究目录、远程任务目录、SSH 别名、已确认空闲的 GPU 和准确 Codex CLI 路径。一次运行只允许一个训练请求，冻结后由独立评分器评价 private test。数据准备与认证条件见任务说明，不运行完整 27 组对照。

## 开发检查

```bash
uvx ruff format src tests benchmarks/cactus
uvx ruff check src tests benchmarks/cactus
uv run python -m unittest discover -s tests -v
PYTHONPATH=src:tests:. uv run python -m unittest discover -s integrations/protok/tests -v
```

固定回复和假 CLI 测试用于验证控制、恢复与协议，不证明研究收益。Harness 选型与登录方式调研见 [调研记录](harness-research.md)。
