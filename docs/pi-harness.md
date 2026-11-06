> Historical record: commands and results describe the former runtime and do not validate the current system. See [current entry](../README.md) and [retirement](runtime-retirement.md).

# Pi Harness

验证日期：2026-11-02。兼容接线版本为 `@earendil-works/pi-coding-agent` 1.4.0，Node.js 要求以该包的 `engines` 为准。使用 Pi 自己的 `openai-codex` 登录；控制器不读取或复制 Codex 凭据。

## 调用合同

`HarnessConfig(backend="pi", executable="/absolute/path/to/pi")` 通过 `make_harness` 接入普通 Study。选择、Plan、Review 只提供 `read,grep,find,ls`；显式授权写入的 Execute 另提供 `bash,edit,write`。每次调用禁用扩展、skills、prompt templates、MCP，使用 `--no-session`，保留工作区的上下文指令。

预检运行 `--offline --list-models`，必须包含准确的 `openai-codex` 与请求型号；缺失时不发起模型请求，不选择其他型号或 provider。目录只列本地可用模型，缺失时应检查 Pi 登录和客户端版本。预检和请求共用本次调用的超时额度；Study 仍按一次 Harness 调用计费到调用预算。

完整提示词及 schema 通过 stdin 传递，实际提示保存在 `prompt.md`。stdout/stderr 原样流式写入文件，JSONL 按 LF 分帧。只有最终 run 的最终 assistant 消息正常结束、最终事件为 `agent_settled(aborted=false)`、provider/model 与有效 `thinkingLevel` 匹配请求时，才解析 JSON 对象并发布原子完成回执。新的 run、turn 或 message 不能继承旧答案。错误、中止、截断或未稳定的事件流保留失败诊断，不发布完成回执。

Pi 的 `thinkingLevel` 表示客户端按模型能力调整后的配置；`providerThinkingLevel` 可能经过不同命名映射，只作记录。两者和 `responseModel` 都不能独立验证服务端实际路由或推理强度。Pi 没有原生最终响应 schema 约束，控制器继续以与 Codex 相同的阶段 schema、研究合同和任务适配器校验接纳结果。

完成回执与 Study 恢复路径和 Codex 共用：控制器完成 Execute 后中断，恢复时复用该结果，不再执行；未完成 Execute 的外部效果仍须核对后显式重试。超时终止本地进程组，不宣称远程请求已取消。

## 自定义模型配置

SQLite [原生应用](https://github.com/Chen-HongRu/research_loop/blob/0d81cbc44025e0e8742a9818fc18326484023790/integrations/native_runtime/README.md)的 Pi 接线独立于上述普通 Study Harness。对于 Pi 1.5 的 `openai-completions` 自定义模型，若服务采用 DeepSeek thinking 协议，模型需要声明 `reasoning: true` 和 `compat.thinkingFormat: "deepseek"`，才能让 `--thinking off` 发送 `thinking: {type: "disabled"}`。仅配置 ID 和名称时，`reasoning` 默认为 `false`，不会发送该关闭字段。配置示例及输出上限见原生应用指南。

服务端仍可能忽略该字段；客户端模式元数据不能证明推理已关闭。可检查已报告的 reasoning-token 用量，无需读取思维文本。协议参见 [DeepSeek thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/)。

## 验证

普通检查不安装 Pi 或连接模型服务：

```bash
uv run python -m unittest discover -s tests -p 'test_*harness*.py' -v
```

假 CLI 走真实 subprocess，覆盖长 stdin、Unicode 行/段分隔符、工具与配置参数、自动重试、损坏和不完整的事件流、配置不符、共享超时。相同 Study 在 Codex/Pi、只读/可写、正常/Execute 完成后中断恢复的八种组合下，均完成一次发布，消耗六次调用，Execute 只调用一次。

真实 CLI smoke 可独立安装并显式运行：

```bash
npm install --prefix /tmp/research-loop-pi-smoke --no-audit --no-fund \
  @earendil-works/pi-coding-agent@1.4.0
PI_HARNESS_EXECUTABLE=/tmp/research-loop-pi-smoke/node_modules/.bin/pi \
  uv run python -m unittest discover -s tests -p test_pi_cli.py -v
```

已观察：1.4.0 的本地 smoke 通过，包含只读/medium 与可写/max 两种调用，实际执行 Pi 的 read/write 工具，验证模型请求字段、工具结果、长提示、Unicode 事件与完成回执。测试使用独立 `PI_CODING_AGENT_DIR` 和 localhost 固定 SSE 响应，在临时模型配置中将传输 API 设为 `openai-completions`；没有使用真实订阅或模型。这证明 CLI 与工具接线，不证明 ChatGPT OAuth、Codex Responses 传输、账号额度或研究收益。默认测试集跳过该显式启用的 smoke。

## 能力边界

Pi 原生没有 OS 沙箱。只读工具列表和禁用扩展不提供与 Codex `read-only` / `workspace-write` 沙箱相同的文件系统或命令网络隔离；可写 Pi 应在符合任务权限要求的外部隔离环境运行。`--no-approve` 控制本地资源信任交互，不是沙箱。

后台 Scout 使用 Codex 的原生 web search，Pi 的内置工具列表没有对应能力。任务专用的 Codex bridge、ProTok 部署入口及已有 Study 不会因本次 Harness 修改而自动切换后端。

官方协议依据：[CLI](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/cli.md)、[JSON 事件](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/json.md)、[模型配置](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/models-configuration.md)、[安全说明](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/security.md)。
