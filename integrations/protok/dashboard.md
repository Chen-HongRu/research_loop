# 只读研究看板

## 阅读方式

- **Frontier**置于页首。双环标明当前正式成员，菱形标明各实例发布时进入 Frontier 的调查（包含持平），蓝点为其他已核查测量。当前成员只采用当前实例的正式 Frontier；历史实例的旧成员不会冒充当前成员。绿阶梯表示当前有效证据的累计最优。
- **精确 RMSD**可通过悬停、点击固定、滑块、前后按钮或键盘浏览。读数保留记录中的数值精度；图形标记和改善判断不使用显示舍入。支持 Frontier 演进、全部实验、最近 10 个时间线节点三种范围；纵轴自动缩放，极小变化以相对 RMSD 差值标轴。
- **完整历史**可接入多个旧 Study 和更早的评价账本，按已记录的创建时间排列。各来源的轮号、路线和引用独立命名；同一轮号不会互相覆盖。相同产物的导入单独连线并注明复用，不计为新评价结果。账本未记录的代码父版本不作推断。
- **早期账本**保留成功、失败、无测量和 `smoke-<fingerprint>` 冒烟检查记录。可读正式 RMSD 在“全部实验”中以灰色空心点显示，注明未经本 Loop 独立评审、未记录比较范围，不进入已评审 Frontier 曲线。冒烟检查单独标明，不生成正式质量测量。原始元数据可从对应节点查看，不生成不存在的 Plan 或 Review。
- **代码继承**按轮次倒序排列，每行对应一次调查。实线箭头从 `base_experiment` 指向本轮；分支轨道只用于排线，同列不表示同一研究路线。路线筛选突出对应节点，并保留其他节点作为代码上下文。
- **证据引用**可单独开启，以虚线连接 `references`，不表示代码继承。时间先后也不表示继承。
- **研究路线**按路线分组，组内按时间先后展示调查、路线判断和恢复条件。现有记录没有路线间的父子字段，页面不推定这种关系。
- **研究说明**默认逐步阅读：问题与目标、实际改动、观察与测量、结论边界、下一项验证；也可切换“30 秒简报”。各轮保留阅读步骤，刷新不重置。说明区分证据支持、待区分的解释和未测量项；重建改善不能代表已证明条件多样性。
- **实际修改**由冻结源码、声明的代码起点及已完成评审支撑。局部前后公式用本地 KaTeX 排版，附符号、维度、适用范围、信息流和源码依据；缺少任一侧源码时不生成前后公式。公式无法解析时明确标出，不展示无法可靠排版的原文作为公式。
- **权威测量**来自发布的候选和冻结协议，保留完整 RMSD 精度，并另外展示当前控制器资格；说明作者不能生成数值或修改资格。历史导入和同账本复用不计为新测量。验证建议标明本说明未执行，基于本轮冻结材料；后续轮次可能已有相近验证，建议不会自动反馈到研究决策。
- **核对原始记录**可展开 Plan、Review、提案、路线知识及已有执行调试回执。长合同与完整说明可继续展开，质疑和回应可选显示。重试仍属于同一次调查，不增加独立实验证据。默认说明不展示 Execution 的原始输出。
- **操作员恢复计划**依据本轮实际采用的 `plan_ref`、`operator_resume` 与 `operator-plan/operator.json` 单独展示，提供原研究 Plan 入口和完整恢复合同。这类计划不计为 Planner 模型调用，也不会因没有模型 process 回执被标为计划丢失；仅有未采用或身份不一致的文件不能作为已采用计划。
- **调用完成、控制器接纳、正式发布**分别标明。未完成的 `response.json` 不作为完成输出；历史发布引用决定实际采用的 Plan 和 Review，后续 attempt 不替换已发布结论。只有已发布且匹配候选身份、有效、已核查的测量进入指标图。
- **当前进度**根据状态、调用元数据、停止标记和后端回执说明停点与原因，保留来源。耗时不等于卡死判定，`running` 不证明进程存活，`ready` 不证明运行器正在运行。终态、目标达成和研究路线状态分开记录。
- **Scout**区分最近调用状态和仍保留的建议，分别显示时间、检索限制和外部主张核查状态。外部建议不自动成为实验证据。

页面每 5 秒读取一次选定目录。页首标明 Study 名称，悬停可查看绝对目录；归档模式明确显示“归档快照”及后端核账时间。刷新时间只代表读取时间，不代表远端作业进展。

## 启动

```sh
uv run python integrations/protok/dashboard.py \
  --root studies/protok-se3-takeover-20261101 --port 8765
```

归档目录需要 `control/backend-snapshot.json`，并显式传入 `--snapshot-only`。活动 Study 使用其实际目录，不加该参数。两种模式均只监听 `127.0.0.1`，不查询 HPC、不调用模型、不修改研究状态。

活动 Study 同时接入归档和前置账本：

```sh
uv run python integrations/protok/dashboard.py \
  --root /root/research-loop/runs/protok-resume-20261101 \
  --history-root /root/research-loop/deployments/protok-se3-20261101/archive \
  --records-root /root/research-loop/deployments/protok-se3-20261101/external-records \
  --port 8767
```

`--history-root` 可重复指定，每个历史来源都使用离线快照。`--records-root` 仅读取早于所有已接入 Study 首轮的小型账本元数据；缺失创建时间的记录不会被猜测为前置历史。页面列出来源、覆盖数量与缺失限制。全局 GPU 费用仍读取当前后端的统一账本，不将各历史快照相加。

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
integrations/protok/assets/**
src/research_loop/*.py
```

将包解压到新的看板版本目录，从该目录启动独立的只读服务即可。入口自行优先加载包内模块，不依赖控制器的 `PYTHONPATH`。保留原研究服务、冻结 runtime、预算与控制记录；看板升级不需要控制器交接。

阿里云活动目录和归档目录必须分别绑定。例如活动 Study 的独立看板启动命令为：

```sh
/root/research-loop/.venv/bin/python \
  /root/research-loop/dashboard-runtimes/<version>/integrations/protok/dashboard.py \
  --root /root/research-loop/runs/protok-resume-20261101 \
  --history-root /root/research-loop/deployments/protok-se3-20261101/archive \
  --records-root /root/research-loop/deployments/protok-se3-20261101/external-records \
  --port 8767
```

该命令只启动看板；服务管理和受保护的访问链路由部署端配置。历史归档看板继续使用其归档目录和 `--snapshot-only`。

同步看板时只切换 `research-loop-live-dashboard-v2.service` 的独立版本目录，保持 Nginx、认证、端口转发和研究服务不变。部署包可沿用已验证看板版本的只读依赖，再替换看板入口和页面，避免将其他聊天尚未完成的研究运行时改动一并部署。

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

本次历史归档另补回了 7 轮、21 个原始冻结候选源码文件：恢复前核对原作业 fingerprint 与归档候选 artifact.version，一共 126248 字节，不含数据集、检查点或新测量。恢复清单位于归档 `presentation/restored-source-metadata.json`。历史 0002 / 0003 仍没有可确认的实际冻结源码，保留材料缺失，不从其基线推定失败候选。

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
