# Installation

From a complete checkout, use Python 3.12 and
[uv](https://docs.astral.sh/uv/):

```sh
uv sync --locked
uv run research-loop doctor
uv run research-loop --help
```

The package pins the Codex SDK/runtime to `0.167.0`. Use the host's own existing
Codex ChatGPT login. `doctor` reports local SDK/binary/login metadata without
calling a model; it does not verify model availability, sandbox behavior, or remote
execution. YAML profiles select explicit OpenAI models and efforts without a
fallback. Real model and resource work require the relevant authorization.

Rebuild `.venv` from `uv.lock` independently on Linux and macOS; virtualenvs and
native binaries are platform-specific. The controller and Agent command Python
may differ. Provision an existing command environment separately, then select it
through `execution.environment` in the YAML. `examples/environment.json` points
to the checkout's `.venv`. Paths inside it resolve from its own directory, and the
virtualenv interpreter symlink is preserved. Configuration selection installs no
dependencies and probes no resource host.

Use [`examples/research.yaml`](../examples/research.yaml) to prepare a fresh
private run, following [operations](operations.md). Keep the run and canonical
inputs outside the checkout and protected control data outside writable workspace
ancestors. A prepared run records absolute paths and its implementation identity;
source edits do not update it. Older run formats are not migrated or accepted as
new prepared runs. Historical portability results describe their original
implementation and do not establish current cross-host acceptance.

For an existing authorized host supervisor, see
[Linux deployment](linux-deployment.md). Upgrading shared controller/package
implementation requires stopping the entire run and preparing a matching version.
