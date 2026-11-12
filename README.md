# Research Loop

Research Loop runs explicitly configured workflows through a tool-free Router,
versioned Groups, and OpenAI Agents using the pinned Codex harness. Python owns
execution state, resource receipts, interventions, and atomic publication. The
reference research workflow lives in `src/research_workflows/`; the reusable
runtime lives in `src/research_loop/`.

Use Python 3.12, [uv](https://docs.astral.sh/uv/), and an existing Codex ChatGPT
login. From a complete checkout:

```sh
uv sync --locked
uv run research-loop doctor
uv run research-loop prepare /path/to/private-run --config examples/research.yaml \
  --project /path/to/project --goal /path/to/goal.md
uv run research-loop inspect /path/to/private-run
```

Preparation fixes the configuration and managed implementation without model calls
or resource allocation. Inspect the selected models, tools, scientific contract,
and resource authorization before starting:

```sh
uv run research-loop start /path/to/private-run
uv run research-loop status /path/to/private-run
uv run research-loop query /path/to/private-run --kind runs
uv run research-loop stop /path/to/private-run
```

`start` launches a detached controller using the prepared implementation and
interpreter. Status, `controller.log`, and `completion.json` are durable local
records. The runtime provides no automatic restart or external Codex wakeup.
Model calls and compute require authorization; preparing a configuration grants
neither. Keep private goals, credentials, transcripts, data, and checkpoints
outside Git.

[Operations](references/operations.md) is the canonical command and configuration
reference, including experiment inputs, pause/continue, explicit recovery and
configuration revisions. See [installation](references/installation.md),
[Linux supervision](references/linux-deployment.md), [runtime design](docs/design.md),
[research Groups](docs/router-groups.md), and the
[workflow author API](docs/framework-api.md).

For offline measurements use `uv run research-loop efficiency RUN`; explicit
period bounds select comparison windows. Continuous collectors are standalone modules:
`uv run python -m research_loop.metrics --help` and
`uv run python -m research_loop.behavior_metrics --help`. They use the active frozen revision, explicit thread identities, and recorded
receipts; missing records remain coverage gaps. See
[measurement guidance](references/operations.md#measurements). Historical benchmark
results do not establish research or efficiency benefits for this refactor.

The existing read-only dashboard can be launched with
`uv run python -m research_loop.dashboard.server --study RUN --port 8768`.
It binds to loopback. Its API projects current configuration, recorded Agent
identities and output publication; `/api/execution` exposes paged Group execution
facts through the same query functions. See
[queries](references/operations.md#queries-and-publication). Restrict access to any
private records served.

Development uses `uv`, changed-file Ruff checks, relevant tests, and the smallest
real smoke. See [CONTRIBUTING.md](CONTRIBUTING.md).
