# Contributing

Describe the observed behavior or requested capability, its input/output contract,
and the smallest check that demonstrates success. For research changes, recover
the model/loss, data roles, tensor shapes, masks/time alignment, frozen evaluation,
state transitions and resource contract before editing.

Use Python 3.12 and `uv sync --locked`. After changing Python files, run
`uvx ruff format` and `uvx ruff check` on those files, relevant tests, and the
smallest real end-to-end smoke. Default verification uses synthetic services and
local CPU work without model/remote calls. For example:

```sh
uv run pytest tests/test_configuration.py tests/test_cli.py tests/test_queries.py
uv run pytest tests/test_framework_runtime.py tests/test_groups.py tests/test_publication.py
uv run python -m research_loop.cli --help
```

These checks verify runtime behavior within their fixture coverage, not live
OpenAI harness correctness or scientific benefit. The migrated native checks are
opt-in:

```sh
RESEARCH_NATIVE_TEST=1 uv run pytest tests/test_native_integration.py::test_pinned_native_handshake_without_model
RESEARCH_MODEL_TEST=1 uv run pytest tests/test_native_integration.py
```

The first checks native account/model metadata without a model turn. The second
exercises current Agent/profile/tool bindings, shared author context, and fresh
independent evaluation with authorized OpenAI calls and local CPU work. The
separate environment smoke requires `RESEARCH_ENVIRONMENT_MODEL_TEST=1` and
`RESEARCH_ENVIRONMENT_PYTHON` naming an existing interpreter; it installs nothing.
`RESEARCH_MODEL` selects the explicitly authorized model for these fixtures.
Real model, GPU, remote and service-deployment acceptance still requires the
relevant account/resource authority and retained evidence; flags do not grant it.

For dashboard changes, also run `node tests/dashboard_frontend.cjs`; it checks
frontend rendering/state logic without proving browser layout or current-runtime
projection completeness.

Keep the generic `src/research_loop/` package independent of scientific roles,
algorithms, datasets and metrics. Research policy, task-specific schemas and
programs belong in `src/research_workflows/` or scoped integrations. Follow
[the author API](docs/framework-api.md), and preserve the immutable configuration,
original operation identities, independent review and bounded publication contract.
Avoid dataset/checkpoint scans or dependency-content audits during startup.

Keep private goals, transcripts, credentials, host/deployment details, datasets and
checkpoints outside commits. Use synthetic fixtures and neutral paths. Preserve
other contributors' changes. A PR should report changed behavior, relevant checks,
observed evidence and remaining limitations. Check affected READMEs, guides,
examples and executable commands together; the canonical operator contract is
[operations](references/operations.md).

Use the [PR checklist](.github/PULL_REQUEST_TEMPLATE.md). Reviews follow
[repository rules](AGENTS.md#code-review-rules); see
[review setup](docs/pr-review.md) and [public release](docs/public-release.md).
Review evidence does not certify scientific correctness or public release safety.
