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

## Linux and macOS fresh Studies

Use the same project files, `uv.lock` and relative environment declaration on both
systems. From a clean checkout, the same command installs the locked runtime and
its platform-specific Codex binary, selects the checkout's Python, and starts the
real repository audit:

```sh
uv run --locked python examples/portability/run.py /path/to/new-private-study --run
```

The example needs `uv`, Git and the host's own existing Codex ChatGPT login.
Authentication is local; do not copy credentials between
machines. Python dependencies and the native binary are rebuilt from the lock,
so do not copy a macOS virtualenv to Linux or edit interpreter paths per host.
The example uses the unchanged `examples/environment.json`. Omit `--run` to
prepare without model calls; `--run` needs authorization for real model calls
and one local CPU audit. It does not use network/GPU during the audit or install
packages inside a Group. Outputs belong in a new private directory outside source.

The task and implementation live in [the portability example](../examples/portability/goal.md).
It tests actual repository behavior; some underlying unit tests use scripted model
responses, while the outer workflow uses real Router/specialist calls. Pass/fail
results are observations, not proof of general research quality. Each host creates
a new Study. Moving a running Study or its native sessions is a separate operation;
see [Linux continuation](linux-deployment.md).

An OpenAI-compatible Responses provider can replace the subscription model without changing the harness. Pass `--provider NAME --provider-base-url URL --api-key-env VAR --model MODEL` to `init`; the key stays in `VAR` and is forwarded only to the isolated native process. This mode does not require `~/.codex/auth.json`. Because non-OpenAI gateways cannot read Codex V2 inter-agent task payloads, the generated model catalog selects the V1 delegation family, and hosted web search is disabled. See [provider operations](operations.md#openai-compatible-provider).
