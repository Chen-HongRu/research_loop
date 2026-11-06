# Codex PR Review

本仓库使用原生 Codex GitHub Review，评审规则在根目录
[AGENTS.md](../AGENTS.md#code-review-rules)。它检查 PR 的文档同步、数据脱敏和
通用控制器与任务适配器的边界；Research Loop 内部的 Plan → Execute → Review
属于研究执行协议，与这里的代码评审分开。

## 启用自动评审

仓库文件只定义规则，**不会打开账号或仓库的自动评审开关**。维护者按
[OpenAI 官方设置说明](https://learn.chatgpt.com/docs/third-party/github)
完成以下步骤：

1. 将 GitHub 仓库连接到 Codex，并确认拥有该仓库的 push 或 admin 权限。
2. 打开 [Codex code review 设置](https://chatgpt.com/settings/code-review)，选择
   仓库，在 **Review code** 下启用 **Automatic review**。
3. 在 **Personal preferences** 中启用 **Automatic review**，用 **Review trigger**
   选择覆盖项目所需 PR 作者和事件的触发范围。不要假定开启一次就覆盖所有贡献者
   或每次 push；以设置实际支持的范围和一次新提交的验证为准。
4. 规则合并后，在有代表性改动的 PR 下评论 `@codex review`，确认 Codex 响应并
   发布 review；再用匹配触发条件的 PR 事件确认自动评审无需评论即可运行。
   本规则 PR 本身也可用作初次手动检查，但不能仅凭它证明后续自动触发已启用。

这条路径使用 Codex 的 GitHub 集成，不需要在仓库配置 OpenAI API key、
新增 Actions workflow 或运行 Research Loop。模型调用仍受账号可用性与额度限制。
若没有响应，检查仓库连接、仓库开关、个人开关和触发条件；手动请求使用完整的
`@codex review`。设置状态和实际触发结果需在账号与 PR 中核实，不能从文件存在推断。

## 评审范围

| 维度 | 检查内容 | 允许的情况 |
| --- | --- | --- |
| 文档同步 | 对照实现检查所有受影响 README、指南、设计、参数、示例和链接；检查缺失更新及错误的新文档 | 无文档合同影响的内部修复不必改 README；明确标记的历史报告保留当时条件 |
| 数据脱敏 | 检查新增/修改文件及引入的资产，定位凭据、个人信息、私有研究内容和部署信息；评论不得复述敏感值 | 合成测试、通用路径占位符、可公开且注明来源的资料 |
| 模型抽象 | 检查通用包、共享协议及默认配置是否依赖具体算法、指标、数据布局或部署环境 | 隔离在 integrations/、benchmarks/ 及相应测试/示例里的任务细节 |

例如：删除 CLI 参数却保留相关 README 命令、提交真实部署访问配置，或让
`runtime/src/` 按任务名选择固定指标，均需指出具体影响和最小修复。
仅在 ProTok 适配器内使用该任务自己的指标，不构成抽象破坏。

贡献者提交前使用 [PR 模板](../.github/PULL_REQUEST_TEMPLATE.md) 逐项自检，
说明实际检查与未验证项。维护者核对 review 对应的 head；若 PR 更新，确认新 head
已重新评审。格式检查、相关测试和最小真实 smoke 仍按
[贡献指南](../CONTRIBUTING.md)执行。

## 能力与发行边界

原生评审以高优先级问题为主；官方文档说明 GitHub review 只标记 P0/P1。
普通文档遗漏可能达不到该级别，因此三类规则是评审关注点，不能保证每项检查
都返回一条评论或逐项通过报告。不要提高问题等级来绕过这个限制。

模型 review 不能证明全部文档正确、全部私有信息已清除或架构对所有算法通用，
也不是确定性的 required status check。没有评论不代表通过脱敏认证；是否合并
仍由维护者决定。需要合并门禁时应另行配置相应检查和分支保护。

这里覆盖 PR 新增或扩大的暴露面，不替代完整发行审查或 Git 历史清理。现有历史
边界与无历史源码包导出方法见[公开分发说明](public-release.md)。不得因为新增
review 规则或收到一次无发现的 review 就认定原仓库可直接公开。
