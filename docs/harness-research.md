# Harness 与现有研究循环系统调研

查阅日期：2026-10-31。范围是官方文档、作者仓库关键代码和原始论文；除本项目验证记录外，没有运行这些系统或复现论文实验。远端仓库以查阅时 main 为准，未固定 commit，因此这里是选型依据，不是版本兼容承诺。

## 当前建议

采用 **Codex 为先、Pi 为可替换后端的薄 Python 核心**。利用它们已有的登录、模型调用、文件与终端工具，研究循环只掌握目标、研究文档、每轮计划/证据/判断以及执行游标。

这是根据本项目约束作出的工程判断：你希望用 GPT 订阅，先验证一个小循环，并支持优化、诊断和推导；整体移植现有科研平台会同时引入其状态结构、任务类型和运行方式。当前没有证据表明，先加入多 agent、搜索树或知识图谱会改善你的任务。

Harness 负责“一次 agent 如何用工具完成任务”；研究循环负责“为何做这个调查、证据说明什么、下一步是什么”。这两层可以独立更换。文档是研究理解的入口，少量 JSON 是执行控制数据，并不要求把所有研究语义变成结构化对象。

## Codex 与 Pi

| 方面 | Codex | Pi | 对本项目的影响 |
|---|---|---|---|
| GPT 订阅 | 官方支持 ChatGPT 登录 | `openai-codex` provider 实现 ChatGPT Plus/Pro OAuth | 复用各自登录，不建立 API key 客户端、不复制凭据 |
| 一次无交互调用 | `codex exec` | `pi --print --mode json` | 首版直接启动 CLI，无须常驻服务 |
| 结果约束 | `--output-schema` 和 `--output-last-message` | JSON 模式输出事件，答案仍需提示与本地校验 | 核心对两者统一校验小型控制对象 |
| 原始轨迹 | `--json` 事件 | JSONL 事件 | 保存 stdout/stderr 与最终结果 |
| 权限 | 读写沙箱与审批策略 | 工具白名单，未提供 OS 沙箱 | Pi 可写调用需自行提供隔离环境 |
| 模型选择 | 显式 `--model` | 默认有模糊解析/custom model 行为 | Pi 加精确目录检查，拒绝自动替换 |
| 后续集成 | SDK、app-server 适合更深入客户端交互 | SDK、RPC、扩展适合嵌入式工具定制 | 暂无需求迫使我们承担这些接口的生命周期 |

Codex 认证与非交互参数来自 [认证文档](https://learn.chatgpt.com/docs/auth#openai-authentication)、[exec 文档](https://learn.chatgpt.com/docs/developer-commands#codex-exec)；同时检查了本机桌面 CLI 的帮助和登录方式。Pi 来自 [CLI 文档](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/cli.md)、[订阅 provider](https://github.com/earendil-works/pi/blob/main/packages/ai/src/providers/openai-codex.ts)、[安全说明](https://github.com/earendil-works/pi/blob/main/SECURITY.md)。

不能把“可以订阅登录”理解为“任何 GPT 型号和任意调用量都有保证”。账号权限、客户端版本和服务端支持仍需实际验证。核心记录请求型号，Codex 返回的实际服务型号尚未核实；Pi 的消息元数据匹配也不等于核验底层服务路由。

### 两个容易误接的协议细节

1. **Pi 退出码 0 不足以判定成功。** 要取得最终 assistant 的 `message_end`，检查 `stopReason == stop`，且有 `agent_settled`、`aborted == false`。`agent_end` 后仍可能自动重试。答案 JSON 再交给核心校验。旧版缺少该事件的 Pi 会明确失败，首版不猜测兼容。[事件协议](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/json.md)、[print 实现](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/modes/print-mode.ts)
2. **Pi 的型号字符串不是天然严格匹配。** 指定 provider 但型号未找到时，解析器还可能创建 custom model；所以本项目先用 `--offline --list-models` 检查精确 provider/model，再开始请求。目录包含型号仍不能证明订阅有权限。[型号解析](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/model-resolver.ts)、[列表实现](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/cli/list-models.ts)

Pi 适配禁用自动加载的扩展、skills、prompt templates、MCP 与审批扩展，使用无会话模式，保留项目上下文指令。Codex 使用 ephemeral 会话。两者都由核心提供研究文件位置，避免把隐藏会话历史变成唯一研究状态。

## 适合借鉴或改造的系统

| 系统 | 已有实现/主张 | 可借鉴部分 | 本项目是否整体引入 |
|---|---|---|---|
| **Arbor**，Apache-2.0 | 持久 ReAct coordinator、短任务 executor、IdeaTree 与 checkpoint | 调查和 insight 分开、执行隔离、在途任务恢复 | 暂不。JSON 树是权威，Markdown 是视图；改成文档驱动需要改核心 |
| **Microsoft RD-Agent**，MIT | 场景化研发循环，生成→编码→运行→反馈→记录 | Experiment/Feedback 边界、历史 Trace | 暂不。Scenario/Developer/Runner 和序列化流程状态较重 |
| **ARIS**，MIT | 研究 skills、流程状态、wiki、评审及写作工作流 | 证据报告模板、未解决项、完成与接受分离 | 摘取文档习惯，暂不整体 fork |
| **Karpathy autoresearch** | 固定训练文件、固定时间预算、指标驱动迭代 | 固定评估合同、快速闭环、保留可比较结果 | 作为优化类 benchmark 模板，不作为通用研究语义 |
| **LangGraph**，MIT | checkpoint、interrupt、恢复、图式/函数式编排 | 将来确需人工等待或并行任务时管理运行状态 | 当前串行循环无需引入；并非必须画固定 DAG |
| **Deep Agents**，MIT | 模型/工具调用、文件上下文、任务规划和 subagents | 若将来自建 agent 运行层，可减少重复实现 | 当前 Codex/Pi 已提供所需工具层，不为它们再包一层 |
| **MIRA**，论文 | 持久研究产物、Git、外层决策与内层执行、新上下文 | 用显式文档连接跨会话研究 | 设计参考；本次未确认可直接复用的完整公开运行框架 |

### Arbor：最接近“持续决定下一步”，但状态取向不同

检查了实际 [coordinator](https://github.com/RUC-NLPIR/Arbor/blob/main/src/coordinator/orchestrator.py)、[IdeaTree](https://github.com/RUC-NLPIR/Arbor/blob/main/src/coordinator/idea_tree.py) 和 [checkpoint](https://github.com/RUC-NLPIR/Arbor/blob/main/src/coordinator/checkpoint.py)。其六步科研循环主要是持续 agent 的提示与工具协议，并非六个固定 agent。树节点能保存结果与 insight，checkpoint 还覆盖消息、Git、在途 executor 和人类待答事项。

需要准确区分：节点分数允许为空，不能说它“只允许 scalar”；但 score/test_score、best artifact 和 dev/test 选择逻辑明显面向优化。其权威状态是 JSON 树，不是 Markdown。若直接改造，你需要明确保留这个树中心还是重写状态层；当前薄核心更容易检验文档驱动假设。

[Arbor 论文](https://arxiv.org/html/2606.11926v1)中的消融可以支持其特定实验设置下的设计选择，不能直接证明图结构优于你的等信息量文档方案。

### RD-Agent：研发流水线很完整，引入成本也最大

检查的 [RDLoop](https://github.com/microsoft/RD-Agent/blob/main/rdagent/components/workflow/rd_loop.py) 实现生成、编码、运行、反馈、记录；反馈有观察和解释，不限于单一分数。其 [LoopBase](https://github.com/microsoft/RD-Agent/blob/main/rdagent/utils/workflow/loop.py) 用序列化对象与会话目录恢复，部分 checkout 路径会截断后续会话，不能直接当作本项目“始终保留历史”的实现。

适合借 Experiment 与 Feedback 的职责分离。整体接入会带入场景类、runner、trace 和恢复格式；其 [LLM 配置](https://github.com/microsoft/RD-Agent/blob/main/rdagent/oai/llm_conf.py) 也不是我们已验证的 Codex/Pi 订阅入口。

### ARIS：更适合摘取研究操作习惯

本次还静态检查了本机 `Auto-claude-code-research-in-sleep` 对应的 [作者仓库](https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep)。[research-pipeline](https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep/blob/main/skills/research-pipeline/SKILL.md) 有固定五阶段；[run_state.py](https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep/blob/main/tools/run_state.py) 管理流程 JSON，[research-wiki](https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep/blob/main/skills/research-wiki/SKILL.md) 管理多类文档与边。

“执行完成”和“被评审接受”分离值得借鉴，但存了 reviewer 字符串不证明评审独立。其论文流程、知识分类和技能中的型号要求不能直接视为本项目需求。借报告格式比整个项目成为依赖更合适。

### 其他方案的适用位置

[autoresearch](https://github.com/karpathy/autoresearch) 将修改范围、训练时间与评估指标约束得很窄，反馈容易比较；这是建立第一个优化 benchmark 的好模板，但诊断结论和理论证明不能通用地转成 keep/discard 指标。

[LangGraph Functional API](https://docs.langchain.com/oss/python/langgraph/functional-api) 允许普通 Python 控制流，不一定强迫固定 DAG；其 [持久执行](https://docs.langchain.com/oss/python/langgraph/durable-execution) 需要认真处理副作用重放。只有出现跨进程长时等待、并发分支和人工中断需求时，才值得替换当前小游标；checkpoint 本身不等于远程操作恰好执行一次。

[Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview) 是构建 agent 运行层的现成方案，有文件、上下文和子任务能力。当前没有核实它能直接复用用户这两套订阅会话，因此没有用其模型 SDK 取代 Codex/Pi CLI。

[MIRA](https://arxiv.org/html/2610.02525v1#S3) 的持久研究文件与新上下文思想同你的设计接近。它说明“文档跨会话积累”有值得研究的先例，不足以保证相同方法在你的基准上有效，也不能用论文结果替代本项目实验。

## 当前最值得保留和质疑的设计

**保留：**详细记录与原始证据；研究状态和运行游标分开；假设支持和目标进展分开；失败保留；先做有限串行循环；执行方式不绑定某个项目。今天已有的 agent 工具层足以承担文件阅读与局部执行，无须先自建完整 Harness。

**需要验证：**

- “总结→假设→验证”是否真的比相同预算的普通 agent 更好，还是只是花更多 token。用同一模型、工具、总预算和 benchmark 做配对比较。
- 研究摘要会不会丢掉负结果或把推测变成事实。让评审逐项回指原始证据，并抽查关键结论的可追溯性；首版只提示这一点，没有自动事实核验器。
- 固定三次模型调用是否浪费简单任务的预算。记录阶段耗时/请求成本，若大量 plan/review 无新信息，再合并调用或只在关键节点评审。
- 单模型自评能否发现关键错误。先用确定性 evaluator、反例或人工抽查建立外部依据；只有观察到错误类型后再增加独立评审，不以“多一个 agent 同意”当真值。
- 只读/可写工具能否完成不同研究类型。当前接口没有任务恢复、候选版本管理和形式证明器；遇到实际任务的第一处断点再增加对应能力。

## 最小的下一步研究验证

当前的命题推导 smoke 只能说明 CLI、文件、状态与 JSON 接线能工作。要检验循环是否“work”，建议下一个单独授权的验证采用一个小型、可本地重复的代码优化或缺陷诊断 benchmark，不接 ProTok：

1. 预先固定任务、初始版本、评估器、允许改动、开发/最终检查的数据角色、总调用或时间预算；执行器不得改评估合同。
2. 比较同一个 Codex/Pi agent 的普通迭代与本核心循环，给相同工具和总预算。先跑一例检查流程，再用多个预先选定任务/重复判断是否有稳定差异；单次成功不作为结论。
3. 至少报告最终正确性/保留样本成绩、总耗时/调用量、失败次数，以及关键判断是否能回溯到真实证据。若优化，重复测量以区分噪声与提升；若诊断，采用已知原因的受控故障核对定位与排除依据。
4. 先回答“是否多花预算仍有可测收益”；结果出来后再决定要不要分支搜索、长期记忆索引或更复杂的 Harness。

这些是待执行的实验设计，不是已经获得的结果。
