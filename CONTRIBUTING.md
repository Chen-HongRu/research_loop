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
