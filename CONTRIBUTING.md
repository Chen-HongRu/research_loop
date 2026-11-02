# Contributing

Start with a small Issue describing the observed behavior or desired capability,
its input/output contract, and the smallest check that demonstrates success.
For research changes, fix the model/loss, data roles, shapes, masks, time alignment,
evaluation and budget before changing behavior.

Use Python 3.11+ and `uv sync --locked`. Run `uvx ruff format` and
`uvx ruff check` on changed Python files, relevant unittest suites, and the
smallest real smoke that exercises the change. The commands are in
[README](README.md#开发). Agent/GPU/remote runs require the appropriate account
and resource authorization; ordinary unit tests and the offline demo need none.

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
