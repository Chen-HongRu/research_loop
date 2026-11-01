# 本次实现验证

日期：2026-10-31。以下区分代码行为测试、真实 Harness 接线与尚未验证的研究效果。

## 已观察到的结果

- 使用 `uv sync` 创建 Python 3.12.5 环境并安装项目，`uv run research-loop --help` 正常。
- `uvx ruff format src tests`、`uvx ruff check src tests` 通过。
- `uv run python -m unittest discover -s tests -v`：18 项测试通过，约 2.66 秒。
- 本机 Codex CLI `0.166.0-alpha.5.1`，`login status` 返回 `Logged in using ChatGPT`。未读取或复制凭据。
- 通过该 CLI 完成一轮真实的只读数学推导，三个阶段的进程均退出 0，返回对象通过本地协议校验，最终研究文档成功发布。

本环境把 uv 缓存和工具目录定向到 `/private/tmp/research-loop-uv-cache`、`/private/tmp/research-loop-uv-tools`，避免写全局工具目录。项目无第三方运行时依赖；构建使用 Hatchling。

## 真实接线案例

目标是判断非负整数 `n` 的 `n²+n` 是否总为偶数；要求执行完整的一轮，禁止把有限数值检查当作证明，不修改代码、不联网调查、不启动训练或远程实验。模型服务本身仍需网络请求。

请求配置为 `gpt-6.1-sol / medium`、每个 CLI 子进程 120 秒上限、最多一轮、全部使用 `read-only`。请求参数不等于已独立核验的服务端实际型号，记录中对此标注 `unverified`。

| 阶段 | 实测耗时 | 结构化结果 |
|---|---:|---|
| plan | 25.56 秒 | `investigate`，提出奇偶分类推导计划 |
| execute | 22.40 秒 | `completed`，给出两个分支和定义域覆盖证明 |
| review | 27.73 秒 | `supported / met / stop` |

合计约 75.69 秒。最终状态为 `stopped`、`stop_reason=review`。保留的事件流每阶段均包含 `turn.completed`，完成项只有 `agent_message`，未出现工具执行项。数学结论也可直接人工核查：`n²+n=n(n+1)`，两个相邻整数必有一个为偶数。

本机原始结果保存在 `studies/codex-proof-smoke/`（该运行目录被 `.gitignore` 排除，不随源码分发）：

- [研究文档](../studies/codex-proof-smoke/research.md)
- [控制状态](../studies/codex-proof-smoke/state.json)
- [执行证明](../studies/codex-proof-smoke/iterations/0001/execute.md)
- [评审报告](../studies/codex-proof-smoke/iterations/0001/review.md)
- [执行请求与收据](../studies/codex-proof-smoke/iterations/0001/execute/attempt-001/process.json)

## 自动测试实际保护什么

测试以临时目录和假 CLI 注入成功、错误、超时或控制器中断，检查：

- 没有 scalar 也能推进多轮；假设得到支持并不自动完成目标；执行不完整仍交评审，不能自动判为反证。
- 阶段/总轮数边界生效，只有显式允许的 execute 接受写权限。
- 失败执行不会自动重跑；显式重试保留旧 attempt；成功收据可在重启后接纳而不再调用模型。
- 初始化提交前中断可以恢复；研究文档发布后、游标提交前中断可以重复发布而不重复请求。
- review 不覆盖并发人工修改；第二个控制器被进程锁拒绝。
- Codex 参数与 stdin 协议；Pi 精确型号目录及终止事件；Pi 退出 0 但返回错误/中止仍失败；超时保留本地终止记录。

这些都是控制协议测试，固定回复不计作研究成果。独立代码评审指出的初始化恢复缺口已用故障注入复现并补齐。

## 尚未验证

- **Pi 真实调用**：本机 PATH 没有 Pi；未安装、登录或发起 Pi 模型请求。当前适配依据上游协议和假 CLI 测试，仍需安装兼容版本并用实际订阅验证。
- **研究收益**：没有真实 benchmark 成绩，没有对照实验或多轮优化收益。简单已知命题不检验发现能力。
- **执行工具与候选版本**：本次真实案例没有使用 bash、改代码或调用外部评估器；没有验证长任务、并行执行、远程取消、worktree 和最佳候选保护。
- **科学验证**：没有形式证明器、自动引用真实性检查或独立实验复现；同模型另开上下文不构成科学意义上的独立验证。
- **隔离**：没有攻击性沙箱测试；Pi 写模式不提供 OS 隔离，原始记录也不是防篡改存储。

下一步研究对照的最小设计见 [调研报告](harness-research.md#最小的下一步研究验证)。
