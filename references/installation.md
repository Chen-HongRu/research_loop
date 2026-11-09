# Installation

Use Python 3.12 and [uv](https://docs.astral.sh/uv/) from a complete checkout:

```sh
uv sync --locked
uv run research-loop doctor
```

The runtime pins Codex 0.167.0 and uses the Codex ChatGPT subscription with `gpt-6.1-sol` and medium reasoning. `doctor` checks the local setup; it does not establish that an authorized model call, native delegation, or a remote GPU job works. Model and remote execution require separate authorization. See [operations](operations.md).
