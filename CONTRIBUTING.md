# Contributing

Start with a small Issue describing the observed behavior or desired capability,
its input/output contract, and the smallest check that demonstrates success.
For research changes, fix the model/loss, data roles, shapes, masks, time alignment,
evaluation and budget before changing behavior.

Use the root Node package: `npm ci`, `npm run build`, `npm run check` and relevant tests. Native tests require `RESEARCH_NATIVE_SANDBOX_TEST=1` on macOS; see [README](README.md#development). Use uv for Python environments and run `uvx ruff format` and `uvx ruff check` on changed Python files, relevant tests and the smallest real smoke. Agent, GPU and remote runs require the appropriate account and resource authorization.

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
