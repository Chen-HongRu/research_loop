# Router Framework 重构设计合同 v2

状态：已批准实施；真实任务验收尚未完成。批准来源为本会话中用户确认设计并要求实施。
实施基线：`codex/133-router-groups`，`4ad182b`。

## 交付目标

在同一仓库分离通用执行机制与 research workflow，删除旧执行路径、依赖和兼容入口；完整运行一个真实任务；通过执行证据验证并减少 Agent 空转与工具调用失败。

通用机制位于 `src/research_loop/`，参考研究流程位于 `src/research_workflows/`。仅使用 OpenAI 模型和 Codex harness，不提供 provider 选择或隐式模型回退。环境配置选择已有环境与授权资源。界面重设计、安装环境、Docker 和自动部署不属于运行机制。

## 职责与接口

| 组件 | 所有权 |
| --- | --- |
| configuration | 解析严格 YAML、解析相对路径、固定声明和显式引用文件；不调用模型或分配资源 |
| application | prepare / inspect / start / status / intervene / stop / resume / revise；CLI、Skill、未来 UI 共用 |
| controller | 唯一状态写入者；接纳工作、应用干预、调度 Router/Group、结算与发布 |
| workflow | Router 策略、能力目录、允许动作、并发上限、输入和结果契约、业务完成及发布内容 |
| Router | 在 workflow 允许的动作和实际引用中提出路由决策，无工具；不写业务结论 |
| GroupRuntime | Group Run/round、状态转换、准入、事件、停止、结算与导出边界 |
| Group 程序 | `async run(ctx, payload)`；显式步骤、分支、并发、等待、会话及 handoff |
| Agents | 绑定 Agent/profile/有效权限，启动或复用会话，记录真实调用；不识别固定研究角色 |
| Commands | local/Slurm 操作身份、原始回执、取消和未知结果；资源预算沿用授权账本 |
| publication | 接纳 workflow 提案，原子激活输出指针，恢复提交事实与回执投影 |
| queries | 按稳定 ID 分页读取事实；不成为第二份权威状态 |

依赖方向为 application → controller → workflow 与通用服务；Group 程序通过注入的服务执行模型和命令。通用包不导入研究实现，不包含科学提示词、算法、数据角色、指标或研究成功条件。Python workflow 通过 `create(settings)` 提供 `context`、`route`、`advance`、`complete`、`router_policy` 和 `max_active_groups`。支持工作输入时提供 `validate_input`，支持恢复时提供 `recover`。

`complete` 返回 `{action: continue|stop, result, artifacts, input_outcome?}`。业务结果由 workflow 校验。研究数据/评估角色、独立 Critic、科学结论和知识清单均由研究包负责。具体参数、payload 和可执行示例以 [Framework API](framework-api.md) 为准。

## 声明与固定实现

主 YAML 声明 workflow、profiles、Agents、Groups、environment 和 resource。显式文件路径相对主声明解析；没有继承、模板插值、隐式覆盖或 provider 配置。完整参考配置为 [research.yaml](../examples/research.yaml)。凭据沿用 Codex 登录，不写入声明或 Git。

prepare 接收配置、项目、目标、初始证据和新运行目录，生成不可变配置修订及 Python 实现快照。快照只包含受管 Python 包的模块和子包；数据、缓存、checkpoint 与外部证据保留引用。共享导入必须属于受管包或已安装环境依赖。外部数据引用不宣称不可变。

inspect、前台启动和后台启动使用同一准备好的解释器、实现快照和配置修订。inspect 区分固定配置、当前环境观察和动态执行实例，展示实际 Router 策略、schema 模板、模型配置及权限规则；动态 reference ID/schema 与实际权限另记调用记录。prepare/inspect 不调用模型或分配资源。

Group 执行是版本绑定单位。已接纳 Group 的所有后续步骤、会话和调用继承原配置；编辑作者文件不影响已准备运行。显式修订只替换声明及显式程序、指令、schema 文件；共享 Python 导入与控制器实现保持固定，改变它们需要停止整个运行并准备匹配实现。修订不热升级调用栈或状态格式。启动检查只读少量版本、路径和文件元数据，不扫描或哈希数据与依赖树。

Agent 定义与会话实例分离。会话身份绑定 Group Run、Agent、profile、有效工具权限和配置修订。`new` 创建会话；`reuse` 仅复用同一 Group 内兼容身份。不同步骤可用同一 Agent，但独立审查必须显式创建新会话。`tools: none|workspace` 是能力声明，有效权限取声明与运行授权的交集；workspace 不能被展示为 OS 强制只读，独立会话也不代表宿主文件读取隔离。

## 执行与完成

Group 状态为 running → returned → held/closed；已结算失败为 failed，未知副作用为 needs-reconciliation。held 表示等待帮助，pause 是独立准入标记。恢复时遇到未结算 running/UNKNOWN 不自动重派。

通用 runtime 支持并发，workflow 指定上限。参考研究流程保持一个活跃 Group，Group 内可并发。完成覆盖本轮已接纳工作的结算和资源回收；单个 Group 返回不等于整个运行完成。Router 在无可决策新事件、达到并发上限或暂停时不重复调用；等待使用本地事件。

研究 author 阶段以 `ready|blocked` 表示能否推进，`help` 保留说明文字；独立审查使用 `accept|revise`。缺失必需产物、未执行成功和未知操作不能由说明文字绕过。Plan/Prepare 显式共享会话，Evaluate 使用独立会话，命令由 host 执行一次。

Synthesis 的 ready 表示未审查候选已完成，足以交给 host 随后调用的独立 Critic；Author 不等待下游审查才返回。Critic 接受后才导出最终结果，缺失上游证据仍阻塞候选。

发布前关闭本轮工作并确认资源结算。输出仅接纳显式选定的小型产物，发布指针激活是提交事实。提交路径在激活前观察到 STOP 则拒绝激活；已激活的发布保持有效。提交后恢复只重建结果、输入完成状态和下一轮路由投影，不重做实验。

## 干预与修订

统一请求为 `{id, target, kind, payload, expected_revision}`；target 是整个运行或具体 Group Run。调用方事实由本地操作进程记录，不信任 payload 自报的人类/Agent 身份。请求与回执单独持久化，可查询 received、pending、applied、rejected；同 ID 同请求返回原回执，冲突内容拒绝。

applied 表示控制生效或输入已投递，业务完成通过独立 `work_outcome` 和发布回执确认。准入身份先写入目标 round 的 input receipt，再派发工作；applied 是该事实的投影。面向整个运行的输入明确投递一次，不广播给所有 Groups；目标已结束、未知或版本失配产生可查询拒绝。待投递普通输入在暂停与发布期间保留，不覆盖已有任务；旧完成回执不能清除更新输入。

| 操作 | 语义 |
| --- | --- |
| pause | 阻止对应范围的新 start/resume；活跃 Group 继续到当前输出边界，结算后才达到 paused |
| continue | 仅解除对应 pause；不恢复 held、failed、UNKNOWN 或已中断调用栈；同类控制按接收顺序生效 |
| stop | 立即关闭目标的新调用准入，取消全部目标，再等待各自清理；未知资源/执行保持 needs-reconciliation |
| resume | 基于有效边界重新调用 Router，新执行关联 predecessor；保留旧执行终止事实、操作身份与预算 |
| revise | 校验 expected_revision 和显式影响范围，结算受影响执行后激活不可变配置；其后新 Group 使用新版本 |

控制器具有独立于模型返回和 Group 输出的本地唤醒路径。一个目标的清理不能延迟其他目标接收取消；本地进程或原生会话中断不证明远端操作终止。Group 程序必须受信任、异步让出事件循环，所有模型/命令副作用通过受管服务执行；同进程任意无 await 死循环不在独立强杀保证内。

修订依据实际 handoff 依赖扩展影响范围，停止受影响的活跃下游；无法明确范围则拒绝并要求显式范围。独立 Group 可继续使用旧版本。正在生成的旧 Router 结果作废；发布期间修订延后到边界。workflow 决定不同修订的结果是否可共同形成结论，已发布成果保留原来源。预算修订不清空既有账本或扩大未授权资源。

命令已结算而尚未导出/评估时，可生成最小恢复包：原 operation identity、原始所属 Group、任务/准备信息、原始回执和必要结果引用，并标记未评估/未发布。新 Group 可采用这份回执继续独立评估，不再次执行原命令。重复恢复始终保留最初 operation 来源。未导出的其他部分工作仅供诊断；UNKNOWN 不生成可重派授权。不承诺任意 Python/外部副作用的 exactly-once。

## 记录与查询

区分运行、Group Run、round、step、session、call、handoff、operation、intervention。记录实际开始/结束、失败、输入、引用、配置版本、上下文复用和父子关系；同一会话多次调用仍是多条 call。handoff 记录真实发送/接收方、输入包和结果关联，不从时间先后推断依赖。

查询提供分页和按需详情，复用 round、回执、原生历史及实际角色映射，不先读取全部原始会话。CLI 与 dashboard `/api/execution` 使用同一查询层；完整 trace 界面后续设计。不收集隐藏推理，私有记录不入 Git，大型数据不复制或哈希。

## 运行监督与验收

后台启动产生独立控制器进程，文件锁确保唯一写入者；前台模式可接入现有 supervisor。交付运行 ID、日志、状态和持久 completion 事件。停止或研究失败不自动重启。未建立事件到外部 Codex 的接入时，只声明可查询和人工复查，不承诺自动唤醒，也不安装周期模型轮询。

验收覆盖以下可观察行为：

1. 旧 Lead/provider/委派入口和依赖退出；通用包无研究反向依赖；合成非研究 Group 跑通。
2. YAML 引用/类型与实际配置一致；源文件修改不改变 inspect/start；旧 Group 后续调用和新修订不串用。
3. 定向输入不串线且不重复消费；pause/continue/stop、发布竞争、旧 Router 结果丢弃和下游修订符合上述边界。
4. 未知操作不重派；已结算命令在评估前中断可在新 Group 中继续评估，保留原身份；资源未知时禁止发布。
5. 线性、并行、分支、等待和跨 Group handoff 记录可查询，角色与工具失败来自实际记录。
6. 无模型本地 prepare→inspect→start→干预→停止→显式恢复完整运行；真实 OpenAI 调用验证共享会话、独立评估、命令执行和发布。
7. 选取实际任务完整运行新框架，报告输入、实际执行、独立评估、发布、Agent 调用与工具失败证据；历史或合成测试不替代这项验收。

Python 改动使用 uv、Ruff 和相称测试。模型、远端及 GPU 操作沿用任务有效授权和资源限额；框架验证使用独立运行目录，不接管另一个活动控制器。效率改进针对已观察的空转和失败，不扩大到训练性能优化、数据治理或全量验证。
