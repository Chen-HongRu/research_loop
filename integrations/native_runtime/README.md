# Native runtime application

This trusted SQLite runtime application uses the Pi CLI for Scout, Advocate,
Judge, Plan, Execute and Review. It passes each frozen ContextPackage through a
tool-free prompt and validates the structured response and evidence references.
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

```json
{
  "transport": {
    "backend": "pi",
    "executable": "/path/to/pi",
    "provider": "configured-provider",
    "model": "configured-model",
    "reasoning": "off",
    "timeout_seconds": 120,
    "private_calls_dir": "/path/to/private-calls"
  },
  "backend_handlers": {}
}
```

`private_calls_dir` is optional and, when present, absolute; prompts and streams
are private. `reasoning` must be one of `off`, `minimal`, `low`, `medium`, `high`
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

An isolated native Pi model profile can request JSON syntax with
`samplingParams: {"response_format": {"type": "json_object"}}`. Pi 1.5 forwards
this parameter without enabling extensions, MCP or tools. Use a separate profile
selected through `PI_CODING_AGENT_DIR` in the trusted executable wrapper when
interactive sessions should retain their ordinary output mode. The complete
`{output, evidence_refs}` schema is still supplied in the prompt and validated
by the application; JSON mode alone does not enforce that schema. Provider support
must be checked in the target environment. See
[DeepSeek response formats](https://api-docs.deepseek.com/api/create-chat-completion/).

An explicit `backend_handlers` entry can select `protok_cleanup` with `config`
containing the frozen `inventory`, `backend_controls`, and optionally an absolute
`receipt_path`. The cleanup is restricted to imported ProTok operations with
matching original bindings: inspection, collection marked unavailable, and
settlement. Its configuration and frozen control bindings are validated without
filesystem I/O before admission. It cannot cancel, prepare, submit, or start
training. See the
[ProTok adapter](../protok/README.md) and [migration guide](../../docs/runtime-migration.md).

Local tests exercise executable Pi stubs, process-group cleanup, coordinator
recovery and actual CPU subprocess acceptance. An optional installed-Pi source
check verifies ambient prompt-file isolation without contacting a model. These
checks do not validate a deployment's account, endpoint or GPU access. Verify the
exact configured model in its target environment before activating new calls;
a connection smoke does not establish research correctness.
Keep deployed runtime installations and private application configuration separate
from repository examples.
