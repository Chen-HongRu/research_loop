# 只读研究看板

## 阅读方式

- **Frontier**置于页首。双环标明当前正式成员，菱形标明各实例发布时进入 Frontier 的调查（包含持平），蓝点为其他已核查测量。当前成员只采用当前实例的正式 Frontier；历史实例的旧成员不会冒充当前成员。绿阶梯表示当前有效证据的累计最优。
- **精确 RMSD**可通过悬停、点击固定、滑块、前后按钮或键盘浏览。读数保留记录中的数值精度；图形标记和改善判断不使用显示舍入。支持 Frontier 演进、全部实验、最近 10 个时间线节点三种范围；纵轴自动缩放，极小变化以相对 RMSD 差值标轴。
- **完整历史**可接入多个旧 Study 和更早的评价账本，按已记录的创建时间排列。各来源的轮号、路线和引用独立命名；同一轮号不会互相覆盖。相同产物的导入单独连线并注明复用，不计为新评价结果。账本未记录的代码父版本不作推断。
- **早期账本**保留成功、失败、无测量和 `smoke-<fingerprint>` 冒烟检查记录。可读正式 RMSD 在“全部实验”中以灰色空心点显示，注明未经本 Loop 独立评审、未记录比较范围，不进入已评审 Frontier 曲线。冒烟检查单独标明，不生成正式质量测量。原始元数据可从对应节点查看，不生成不存在的 Plan 或 Review。
- **代码继承**按轮次倒序排列，每行对应一次调查。实线箭头从 `base_experiment` 指向本轮；分支轨道只用于排线，同列不表示同一研究路线。路线筛选突出对应节点，并保留其他节点作为代码上下文。
- **证据引用**可单独开启，以虚线连接 `references`，不表示代码继承。时间先后也不表示继承。
- **研究路线**按路线分组，组内按时间先后展示调查、路线判断和恢复条件。现有记录没有路线间的父子字段，页面不推定这种关系。
- **Plan / Review / 提案与决策**展示各 Agent 的关键输出和 attempt 来源。长合同与完整说明可展开阅读，质疑和回应可选显示。重试仍属于同一次调查，不增加独立实验证据。Execution 仅显示阶段与结果状态。
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

## 开发验证

```sh
uvx ruff format integrations/protok/dashboard.py integrations/protok/tests/test_protok_dashboard.py
uvx ruff check integrations/protok/dashboard.py integrations/protok/tests/test_protok_dashboard.py
PYTHONPATH=src:tests:. uv run python -m unittest discover \
  -s integrations/protok/tests -p test_protok_dashboard.py -v
node --test integrations/protok/tests/dashboard.test.cjs
```

另用实际研究目录检查 HTTP 快照、Agent 详情、Git 历史、路线视图、知识文档与自动刷新。测试证明展示和记录语义，不证明研究收益。
