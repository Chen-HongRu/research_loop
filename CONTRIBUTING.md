# Contributing

Start with a small Issue describing the observed behavior or desired capability,
its input/output contract, and the smallest check that demonstrates success.
For research changes, fix the model/loss, data roles, shapes, masks, time alignment,
evaluation and budget before changing behavior.

Use Python 3.12 and `uv sync --locked`. After changing Python files, run `uvx ruff format` and `uvx ruff check` on the changed files, relevant tests and the smallest real smoke. Agent, GPU and remote runs require the appropriate account and resource authorization.

For dashboard changes, also run `node tests/dashboard_frontend.cjs` to exercise
rendering, study selection, stale response handling and tool cache isolation
without a browser. This check does not verify visual layout.

The pinned native checks are opt-in. The first checks SDK metadata without a model call; the second calls the authorized subscription model for a synthetic Proposer → Experimenter → Critic smoke. It checks fresh contexts, an arithmetic plan and script, coordinator execution, and a read-only critique against native histories and output artifacts, without GPU or remote access:

```sh
RESEARCH_NATIVE_TEST=1 uv run pytest tests/test_native_integration.py::test_pinned_native_handshake_without_model
RESEARCH_MODEL_TEST=1 uv run pytest tests/test_native_integration.py::test_native_fresh_research_roles_and_local_command
```

These flags do not authorize a research loop or remote compute.

The environment smoke uses a separate, standard-library-only virtualenv with a
synthetic distribution marker. It checks unqualified Python execution and
environment context in a coordinator and one fresh role for each delegation
mode; resource metadata is synthetic and no cluster or GPU is contacted:

```sh
RESEARCH_ENVIRONMENT_MODEL_TEST=1 uv run pytest tests/test_environment_integration.py
```

It defaults to `gpt-6.1-terra` / medium; `RESEARCH_ENVIRONMENT_MODEL` may select an
explicitly authorized available model. The flag permits only those bounded
subscription model calls, not a research experiment or remote compute. Keep
model traces and temporary environment inventories private.

Keep private goals, raw Agent transcripts, deployment/SSH details, credentials,
data and checkpoints outside commits. Use synthetic fixtures in tests and demos.
Preserve prior evidence and other contributors' changes. A pull request should
state what changed, its relevant checks, and remaining limitations.

Before submitting, check every affected README, guide, specification and example
against the implementation; explain when a change needs no documentation update.
Keep the reusable controller and shared contracts independent of task algorithms,
metrics and deployment. Task details belong in adapters or benchmarks and their
scoped tests/examples. Use the [PR checklist](.github/PULL_REQUEST_TEMPLATE.md).

Codex GitHub Review follows the [repository review rules](AGENTS.md#code-review-rules).
Maintainers must separately enable automatic review and verify its trigger; see
[PR Review setup and limits](docs/pr-review.md). A review is supporting evidence,
not a guarantee of sanitization or a required merge check.
