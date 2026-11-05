# Native runtime application

Role output fields are documented with Pydantic `Field(description=...)`.
The generated JSON schema carries these descriptions into the role prompt and
the `submit_result` tool parameters. Prompts supply role instructions and bound
context, while Python validators enforce evidence and execution constraints.

This trusted SQLite runtime application uses the Pi CLI for Scout, Advocate,
Judge, Plan, Execute, Review and Knowledge Manager. Each role has a
[definition](roles/) binding instructions, inputs, libraries, tools and output.
The controller binds the scientific input, instructions, directory grants and output contract.
Knowledge files use a stable live directory throughout the invocation.
See [Study knowledge](../../docs/study-knowledge.md) for configuration,
maintenance and the read-only context preview.
The initial Execute is qualitative only: its operations and artifacts lists are
empty. It does not edit source or start training. Existing legacy Harness role
schemas are separate and are not automatically adapted.

Review prompts may encode repeated large nested context objects once using
`shared-json-subtrees-v1`, only when this reduces prompt bytes. The prompt includes
the complete shared objects and explicit nonoverlapping replacement paths; exact
substitution reconstructs the original package. The manifest, report wrappers,
original evidence references and output validation remain unchanged. Shared IDs
are representation labels, never evidence. Strings remain opaque, and other
roles keep the ordinary package format. Frozen inputs and retries are unchanged;
this encoding does not summarize or drop evidence or add authority.

The Review response schema lists only measurement IDs visible in the original
frozen context. When none are registered, `reviews`, `corrections` and each
knowledge entry's `measurement_ids` must be empty. Interpretation knowledge may
still cite allowed evidence; operation, job and artifact IDs are not measurements.

Set `runtime.worker_adapter` to `{"id": "native-runtime-v1", "version": "1"}`
and select a versioned immutable copy of `application.py` with `--application`.
Its package and dependencies must be available in the chosen runtime environment.
An application configuration for qualitative work has this shape:

```yaml
transport:
  backend: pi
  executable: /path/to/pi
  provider: configured-provider
  model: configured-model
  reasoning: 'off'
  timeout_seconds: 120
  private_calls_dir: /path/to/private-calls
backend_handlers: {}
```

`private_calls_dir` is optional and, when present, absolute. It retains invocation metadata and outcome counts. The private dispatch record retains the role prompt and accepted output; native file-tool transcripts are not archived. `reasoning` must be one of `off`, `minimal`, `low`, `medium`, `high`
or `xhigh`, subject to the selected model's actual supported settings. For a
custom model without declared reasoning support, use `off`; that catalog flag
does not itself disable reasoning at the endpoint. The executable's
offline catalog and response must match the exact configured provider, model and
reasoning; there is no fallback. Pi runs with a fixed system prompt and empty
appended prompt, and disables context files, so ambient AGENTS, CLAUDE, SYSTEM
and APPEND files do not enter the call. Configuration validation is pure and
makes no model call. Initialize and operate as described in
[concurrent runtime](../../docs/concurrent-runtime.md).

For a Pi 1.5 custom `openai-completions` model using the DeepSeek thinking
protocol, declare its capability and protocol even when requesting `off`:

```json
{
  "id": "configured-deepseek-model",
  "name": "Configured DeepSeek model",
  "reasoning": true,
  "maxTokens": 16384,
  "compat": {"thinkingFormat": "deepseek"}
}
```

Replace the example ID with the exact model exposed by the endpoint. This makes
Pi send `thinking: {type: "disabled"}` for `--thinking off`.
An entry containing only ID/name defaults to `reasoning: false` and sends no
DeepSeek disable field. Keep `maxTokens` within the operator-authorized output
bound; 16384 is an example cap, not a general recommendation. An endpoint can
still ignore the field: client mode metadata does not prove server behavior.
Check reported reasoning-token usage where available, without inspecting thought
text. See the [DeepSeek thinking protocol](https://api-docs.deepseek.com/guides/thinking_mode/).

Deploy the application package, including [submit_result.ts](submit_result.ts), in
its versioned runtime. Pi supplies `ls/find/grep/read`. They read a stable
`knowledge/<alias>/files/` directory directly, including files absent from any Index. Directory contents may change inside
a single tool call. The Manager also receives `edit/write` for a private draft.
`submit_result` validates and terminates a role's structural response. Source
edits and backend operations still require their original role and Study grants.

The process requires a managed or npm Pi installation, Node.js, and local
`rg` and `fd` for the corresponding native tools. macOS uses `sandbox-exec`;
Linux requires bubblewrap (`bwrap`) and an enabled user namespace. Missing
isolation prevents model admission. Install dependencies and the Pi runtime
outside private Study, deployment and credential directories. The sandbox can
read its trusted runtime dependencies and authorized knowledge directories.
It can never use the operator's original Pi profile as its working directory.

Use an environment or API-key model profile. The launcher selects only the
configured provider and keeps the original credentials outside the sandbox.
OAuth credential files and shell-based credential values require a different
credential boundary and are rejected. Use a versioned copy of the old
application for old in-flight Studies. The isolated Pi session uses SSE,
disables provider retries and compaction, and accounts for each model request
before sending it. Choose a provider supporting tool calls and the exact
requested model/reasoning. No production account is covered by the local smoke.

An explicit `backend_handlers` entry can select `protok_cleanup` with `config`
containing the frozen `inventory`, `backend_controls`, and optionally an absolute
`receipt_path`. The cleanup is restricted to imported ProTok operations with
matching original bindings: inspection, collection marked unavailable, and
settlement. Its configuration and frozen control bindings are validated without
filesystem I/O before admission. It cannot cancel, prepare, submit, or start
training. See the
[ProTok adapter](../protok/README.md) and [migration guide](../../docs/runtime-migration.md).

The following opt-in test uses an actual installed Pi CLI, a temporary SQLite
Study, and a synthetic loopback HTTP endpoint. It exercises all seven roles,
a Manager publishing while Plan is reading, explicit citations and historical
inspection. The transport suite also exercises filesystem denial and request
budgets. No paid endpoint, Slurm job or GPU is used.

```sh
NATIVE_PI_CLI=/path/to/pi PYTHONPATH=src:tests:. uv run python -m unittest -v test_runtime_knowledge_loop test_native_pi_submission
```
