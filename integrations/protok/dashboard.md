# 只读研究看板

## 阅读方式

- **Frontier**置于页首。双环标明当前正式成员，菱形标明各实例发布时进入 Frontier 的调查（包含持平），蓝点为其他已核查测量。当前成员只采用当前实例的正式 Frontier；历史实例的旧成员不会冒充当前成员。绿阶梯表示当前有效证据的累计最优。
- **精确 RMSD**可通过悬停、点击固定、滑块、前后按钮或键盘浏览。读数保留记录中的数值精度；图形标记和改善判断不使用显示舍入。支持 Frontier 演进、全部实验、最近 10 个时间线节点三种范围；纵轴自动缩放，极小变化以相对 RMSD 差值标轴。
- **完整历史**可接入多个旧 Study 和更早的评价账本，按已记录的创建时间排列。各来源的轮号、路线和引用独立命名；同一轮号不会互相覆盖。相同产物的导入单独连线并注明复用，不计为新评价结果。账本未记录的代码父版本不作推断。
- **早期账本**保留成功、失败、无测量和 `smoke-<fingerprint>` 冒烟检查记录。可读正式 RMSD 在“全部实验”中以灰色空心点显示，注明未经本 Loop 独立评审、未记录比较范围，不进入已评审 Frontier 曲线。冒烟检查单独标明，不生成正式质量测量。原始元数据可从对应节点查看，不生成不存在的 Plan 或 Review。
- **Work tree**占据整行，统一展示主循环调查、研究脚本、自动/人工长训和历史账本。上方只画节点、分支与连线，默认将整棵树适配到可视区域，同时显示所有分支；下方独立排列实验卡片，卡片宽度不影响树的间距。横轴按任务创建时间从早到晚等距排列，不表示耗时比例；未记录时间的任务置于末尾，按原任务顺序排列。每个任务从等待、进行中到完成保留同一个节点；点击节点或卡片联动选中，在两区下方查看完整阶段、调用耗时、实时指标、Plan、Review、作业回执与结果。
- **分支与任务关系**用实线表示 `base_experiment` 代码继承，绿色点线表示执行续跑，紫色虚线表示来源候选的独立长训验证。一个来源的额外分叉与独立验证在纵向增加轨道，不限制为两条；首条代码延续可沿用父轨道，无继承关系的独立节点也可共用排布轨道。同轨道不等于同一路线，继承只由明确连线表示，路线见标签。“进行中/需要处理”筛选保留任务来源和已选节点作为上下文；归档中的运行状态不计入当前并发数量。
- **浏览与手机阅读**“全局概览”适合看整体分叉，密集节点的名称可在“局部细看”中阅读，也可使用加减按钮缩放。“定位所选”将树和卡片分别定位到同一任务；所选任务名称始终列在树下方。树和卡片独立滚动，卡片区的“较早/较新”按钮只移动卡片，“查看所选任务详情”跳到完整进展。自动刷新保留缩放模式、所选任务与两区各自的浏览位置；补入较早记录时保持可见任务的相对位置。开启“跟随主循环”后，新主循环任务会自动定位。路线等筛选框限制在可用宽度内，长名称不会撑宽页面；详情以整行自然排布，不覆盖任务树。
- **长训任务详情**复用同一个详情区，直接提供 Plan、Review，显示独立 10k/完整开发集协议、提名依据、已收集观测及 checkpoint 引用。最终10k、曲线最优和每个验证点的精确指标分别展示，三个指标绑定同一 checkpoint。未评审观测明确标记；长训指标不进入短训 Frontier。后续调查冻结的 `long_training_results.json` 提供知识回流引用。
- **证据引用**可单独开启，以虚线连接 `references`，不表示代码继承。时间先后也不表示继承。
- **研究路线**按路线分组，组内按时间先后展示调查、路线判断和恢复条件。现有记录没有路线间的父子字段，页面不推定这种关系。
- **研究说明**默认逐步阅读：问题与目标、实际改动、观察与测量、结论边界、下一项验证；也可切换“30 秒简报”。各轮保留阅读步骤，刷新不重置。说明区分证据支持、待区分的解释和未测量项；重建改善不能代表已证明条件多样性。
- **实际修改**由冻结源码、声明的代码起点及已完成评审支撑。局部前后公式用本地 KaTeX 排版，附符号、维度、适用范围、信息流和源码依据；缺少任一侧源码时不生成前后公式。公式无法解析时明确标出，不展示无法可靠排版的原文作为公式。
- **权威测量**来自发布的候选和冻结协议，保留完整 RMSD 精度，并另外展示当前控制器资格；说明作者不能生成数值或修改资格。历史导入和同账本复用不计为新测量。验证建议标明本说明未执行，基于本轮冻结材料；后续轮次可能已有相近验证，建议不会自动反馈到研究决策。
- **核对原始记录**可展开 Plan、Review、提案、路线知识及已有执行调试回执。长合同与完整说明可继续展开，质疑和回应可选显示。重试仍属于同一次调查，不增加独立实验证据。默认说明不展示 Execution 的原始输出。
- **执行续跑**用绿色点线连接保留候选的上一条执行记录，并标明原研究编号。Plan 面板直接展示原调查已接纳的 Planner 合同与真实调用回执；恢复授权和执行指令单独折叠，不增加 Planner 调用计数。读取 `continuation` 或已发布的旧 `operator_resume` 链时，必须核对原始发布引用与完成回执，不能根据相同代码猜测研究身份。旧记录保留不变；缺少可确认原 Plan 的旧操作员记录只展示已保存的恢复凭证。
- **研究脚本**依据 `control/research-jobs/<轮次>/` 的小型控制回执展示作业状态、GPU 费用与证据产物路径，不读取产物内容。Plan 直接展示研究问题、观测量与判断合同；无 RMSD 的研究轮次展示为研究证据，不进入质量 Frontier。正式评分、研究计算和调试费用分别列出；没有 Study 节点的操作员验证作业只计入资源和作业状态。
- **调用完成、控制器接纳、正式发布**分别标明。未完成的 `response.json` 不作为完成输出；历史发布引用决定实际采用的 Plan 和 Review，后续 attempt 不替换已发布结论。只有已发布且匹配候选身份、有效、已核查的测量进入指标图。
- **任务进度**根据所选任务的状态、调用元数据和本地回执说明停点与原因，保留来源。主循环与长训的进度分别归属各自任务，顶部仅汇总任务数量、费用与主循环入轮截止。耗时不等于卡死判定，`running` 不证明进程或 GPU 活性；未记录的训练 step 不作推断。终态、目标达成和研究路线状态分开记录。刷新和任务切换保留各任务、各页签的展开状态与阅读位置。
- **实时回传指标**位于所选任务的“任务进展”内，仅展示该任务的 `dashboard` 标签。默认展示最多六条近期标量曲线，支持对象中的数值字段、指标筛选、悬停/键盘查看精确值和点击固定。来源、执行、单位和观测条件分别成线；没有 Step 时按原始上报时间绘图。显式缺失/非有限值及 Step 回退处断线。浏览器每条来源最多保留最近 256 条观测；固定值与条件展开状态在刷新后保留。
- **TensorBoard 全量指标**按钮打开同一访问入口下的真实 TensorBoard。所有标签的有限标量和对象数值字段进入 Scalars，原始小型记录进入 Text；数组保留文本，不猜测直方图或图片含义。主循环、长训与历史来源按各自身份区分；这些观测仍需独立 Review，不进入短训 Frontier。
- **Scout**区分最近调用状态和仍保留的建议，分别显示时间、检索限制和外部主张核查状态。新版展示外部洞见、适用条件及未解问题，旧版候选建议仍可读取。外部建议不自动成为实验证据。

页面每 5 秒读取一次选定目录。页首标明 Study 名称，悬停可查看绝对目录；归档模式明确显示“归档快照”及后端核账时间。刷新时间只代表读取时间，不代表远端作业进展。

`/api/snapshot` 的 `nodes` 保留用于短训质量历史，`tasks` 提供统一任务视图；长训任务标识为 `<来源>:long:<ID>`，详情接口为 `/api/task/<来源>/long/<ID>`。来源必须属于启动时配置的活动或历史 Study。长训任务只读取子 Study 调用凭据和本地作业元数据：普通控制记录上限为 96,000 字节，包含部分观测与日志的回执上限为 4 MiB；按原作业身份核对回执，不返回控制记录中的 ownership、授权快照或令牌。

## 启动

```sh
uv run python integrations/protok/dashboard.py \
  --root runs/example-study --port 8765
```

归档目录需要 `control/backend-snapshot.json`，并显式传入 `--snapshot-only`。活动 Study 使用其实际目录，不加该参数。两种模式均只监听 `127.0.0.1`，不查询 HPC、不调用模型、不修改研究状态。

活动 Study 同时接入归档和前置账本：

```sh
uv run python integrations/protok/dashboard.py \
  --root /path/to/run \
  --history-root /path/to/archive \
  --records-root /path/to/external-records \
  --port 8767
```

`--history-root` 可重复指定，每个历史来源都使用离线快照。`--records-root` 仅读取早于所有已接入 Study 首轮的小型账本元数据；缺失创建时间的记录不会被猜测为前置历史。页面列出来源、覆盖数量与缺失限制。全局 GPU 费用仍读取当前后端的统一账本，不将各历史快照相加。

### 启用 TensorBoard

使用独立的 CPU 服务，源目录与看板保持一致，派生日志单独存放：

```sh
uv sync --extra dashboard
uv run --extra dashboard python -m integrations.protok.tensorboard \
  --root /path/to/run \
  --history-root /path/to/archive \
  --logdir /path/to/tensorboard-data \
  --port 6006
```

看板原启动命令增加 `--tensorboard-port 6006` 后显示按钮。TensorBoard 仅监听本机，由看板在 `/tensorboard/` 转发，沿用现有认证与访问路径；不对外开放 6006 端口。服务未配置或暂不可用时，任务进展仍可读取回传数据。

按钮默认打开 Scalars 并过滤到所选任务。Run 使用面板编号，例如 `active/#0026`；同一执行的指标和条件分别作为曲线，不再生成数百个 run。其他执行使用 `(attempt N)` 区分。TensorBoard 2.21.0 在匹配 run 超过 40 个时默认全不选，任务过滤避免历史增长再次造成空图，并保留用户的显式选择。没有保留遥测的旧实验不能仅凭最终分数补出训练曲线。

升级导出器与看板时需一起更新 `tensorboard.py` 与 `assets/task-metrics.js`。原 logdir 可继续使用；新版在 `events-v2/` 与 `.export-state-v2/` 增量重建派生视图，保留旧目录供回退，TensorBoard 只观察新版目录。切换来源目录时使用新的 logdir。只需重启两个展示服务，无需重启研究 Runner。

导出器每五秒增量读取各来源的一页本地 JSONL，并只向派生目录写入 TensorBoard event 文件与续传游标；重启后继续补传，不重复写点，不读取训练数据或 checkpoint。TensorBoard 网页首次打开默认开启原生自动刷新，周期 30 秒；保留用户已有的显式偏好。依赖固定为 TensorBoard 2.21.0，更新版本时须重新核对首次访问的设置接口。详细数据映射及读取边界见 [telemetry.md](../../docs/telemetry.md#task-progress-and-tensorboard)。

## 与冻结控制器独立部署

看板使用当前的 `integrations.protok.backend` 和 `research_loop.harness`。旧冻结运行版本可能仍使用 `research_loop.protok`，不能只把新的 `dashboard.py` 放进旧运行版本后沿用其模块路径。

独立看板包包含以下文件，保持目录结构：

```text
integrations/__init__.py
integrations/protok/__init__.py
integrations/protok/backend.py
integrations/protok/dashboard.py
integrations/protok/dashboard.html
integrations/protok/explanation.py
integrations/protok/plan_provenance.py
integrations/protok/tensorboard.py
integrations/protok/task_telemetry.py
integrations/protok/assets/**
src/research_loop/*.py
pyproject.toml
uv.lock
```

将包解压到新的看板版本目录，从该目录启动独立的只读服务即可。入口自行优先加载包内模块，不依赖控制器的 `PYTHONPATH`。保留原研究服务、冻结 runtime、预算与控制记录；看板升级不需要控制器交接。

启用 TensorBoard 时在新版本目录用 `uv sync --extra dashboard` 创建独立环境，不向研究服务共用的环境安装依赖。TensorBoard 单独管理进程；看板与它分别重启，均无需重启研究 Runner。

部署时活动目录和归档目录必须分别绑定。例如活动 Study 的独立看板启动命令为：

```sh
/path/to/dashboard/.venv/bin/python \
  /path/to/dashboard-runtime/integrations/protok/dashboard.py \
  --root /path/to/run \
  --history-root /path/to/archive \
  --records-root /path/to/external-records \
  --port 8767
```

该命令只启动看板；服务管理和受保护的访问链路由部署端配置。历史归档看板继续使用其归档目录和 `--snapshot-only`。

同步看板时只切换其独立版本目录，保留研究服务与控制记录。受保护的访问入口由部署端单独配置。

## 独立说明缓存

HTTP 服务只读取说明缓存，不启动模型。使用研究现有 Codex 可执行文件与登录态，单独启动说明作者：

```sh
uv run python -m integrations.protok.explanation /path/to/study --watch
```

活动 Study 与每个历史 Study 分别启动一个进程。作者固定使用 GPT-6.1 Sol / medium、只读沙箱、无网络工具和 240 秒调用超时；只写入对应 Study 的 `presentation/`，不写控制器、训练或候选文件，不占训练 GPU，不计入正式研究调用数。

每个已发布轮次先冻结一份有大小上限的解释材料，再生成结构化说明。材料只包含小型控制记录、已接纳完成回执和候选源码；不读数据集或 checkpoint。配置和文件 stat 元数据形成失效键，不扫描或哈希科研产物内容。普通单文件上限 96KB；已观察到早期 publication 元数据大于此值，因此仅其读取上限设为 256KB。材料总量上限 600KB、说明内容上限 60KB。普通读取核对已存依赖的 stat，读取缓存材料，不重新采集源码；watch 也跳过未变化的完成项。超限或损坏的已发布记录保留在覆盖索引，明确标记材料不足，不消失。

覆盖索引按来源展示完成、待生成、运行中、失败、过期和材料不足，标出最新发布与最新已说明的轮号；后者不表示早期没有缺口。失败保留，不随刷新自动重试。显式重试优先复用输入未变且通过当前校验的已完成回执；需要重新调用时保留原回执。已记录的轮号引用与测量数字分开校验，RMSD 数值仍只由权威面板呈现：

```sh
uv run python -m integrations.protok.explanation /path/to/study \
  --once --iteration 0012 --retry-failed
```

`--once` 输出状态，只有指定范围全部 ready 才返回成功。早期账本没有 Loop 的 Plan / Review，不伪造说明。归档若没有冻结源码，仍可说明已保存的研究记录，并明确缺失，不能生成架构前后公式。

跨主机归档若保留了旧机器的可执行文件路径，可显式指定本机现有入口，不修改冻结配置：

```sh
uv run python -m integrations.protok.explanation /path/to/archive \
  --watch --codex-executable /usr/local/bin/codex
```

配置路径、实际路径及覆盖选项均保存在说明执行凭证中；模型仍为 GPT-6.1 Sol / medium，不更换供应方或凭据。

归档缺少可确认的实际冻结源码时，保留材料缺失；不能从基线推定失败候选的源码。

云端说明 CLI 若发生网络超时，可保留失败回执，以同一冻结材料包完成只读协作复核。此类缓存明确保存 `assisted_review`、原材料身份、原缓存和审阅结果，并由页面提供“说明来源与复核记录”；不能伪造原调用成功凭证。发布前仍须通过结构、来源、数字及当前输入身份校验。说明整理和验证建议均不改写研究结论或执行实验。

## 开发验证

```sh
uvx ruff format integrations/protok/dashboard.py integrations/protok/tests/test_protok_dashboard.py
uvx ruff check integrations/protok/dashboard.py integrations/protok/tests/test_protok_dashboard.py
PYTHONPATH=src:tests:. uv run python -m unittest discover \
  -s integrations/protok/tests -p test_protok_dashboard.py -v
node --test integrations/protok/tests/dashboard.test.cjs
```

另用实际研究目录检查 HTTP 快照、Agent 详情、Git 历史、路线视图、知识文档与自动刷新。测试证明展示和记录语义，不证明研究收益。

人工长训练的实时指标按稳定任务 ID 查询（`/api/telemetry?source=active&task=TASK`），从请求绑定的 `study/tasks/TASK/study/iterations/ITERATION/telemetry` 读取。运行中和已评审任务使用同一位置；历史 Study、自动长训和各执行来源分别隔离。TensorBoard 增量导出同一批本地来源的全部标签，重启沿用游标。
