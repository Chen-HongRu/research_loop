# Installation

Use Python 3.12 and [uv](https://docs.astral.sh/uv/) from a complete checkout:

```sh
uv sync --locked
uv run research-loop doctor
```

The runtime pins Codex 0.167.0 and uses the Codex ChatGPT subscription with `gpt-6.1-sol` and medium reasoning. `doctor` checks the local setup; it does not establish that an authorized model call, native delegation, or a remote GPU job works. Model and remote execution require separate authorization. See [operations](operations.md).

The runtime environment and the Python used for agent shell commands may be
separate. Provision the latter with `uv`, then pass `--environment FILE` to
`init` to select its existing interpreter. Without that flag, agents use the
controller's Python. The loop observes installed distribution metadata and
shows it to the coordinator and delegates; it does not install dependencies.
See [execution environments](operations.md#execution-environments).

An OpenAI-compatible Responses provider can replace the subscription model without changing the harness. Pass `--provider NAME --provider-base-url URL --api-key-env VAR --model MODEL` to `init`; the key stays in `VAR` and is forwarded only to the isolated native process. This mode does not require `~/.codex/auth.json`. Because non-OpenAI gateways cannot read Codex V2 inter-agent task payloads, the generated model catalog selects the V1 delegation family, and hosted web search is disabled. See [provider operations](operations.md#openai-compatible-provider).
