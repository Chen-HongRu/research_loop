# Research Loop

Research Loop runs an evidence-driven investigation with one main Codex agent coordinating native role agents: Scout, Proposer, Experimenter, Analyst and Critic. The lead manages dependencies, efficiency, resources and summaries; the roles conduct scientific proposal, execution, analysis and critique. Each role invocation starts in a fresh independent Codex context. The Python host starts the lead, records events and manages authorized remote resources.

Python 3.12, [uv](https://docs.astral.sh/uv/) and a Codex ChatGPT subscription are required. The runtime pins Codex 0.167.0 and uses `gpt-6.1-sol` with medium reasoning. From the repository root:

```sh
uv sync --locked
uv run research-loop doctor
uv run research-loop init RUN --project PROJECT --goal GOAL
uv run research-loop inspect RUN
uv run research-loop run RUN
uv run research-loop status RUN
uv run research-loop stop RUN
```

Each iteration settles native work and authorized resources, publishes an independent knowledge snapshot, and follows the lead's explicit `study_action`: `continue` starts the next iteration in the same thread; `stop` ends the Study with a `stop_reason`. Scientific goal status is reported separately. Completing one diagnostic does not complete the Study. Results and status are retained under `iterations/NNNNNN/`; root records show the latest iteration. Failed work, UNKNOWN resources and STOP halt continuation and retain drafts.

An optional `--resource FILE` on `init` supplies authorized Slurm resources. `inspect` shows the resolved run configuration without a model call; a completed Study is cached unless explicitly continued with `run RUN --continue`, including Studies completed by the earlier single-iteration runtime. Continuation preserves the original thread and ledger. See [installation](references/installation.md), [operations](references/operations.md), [Linux deployment](references/linux-deployment.md), and the [design](docs/design.md). Model calls and remote compute require explicit authorization. Keep private inputs, credentials, runtime artifacts and checkpoints outside Git.

To view a Study locally, run `uv run python -m research_loop.dashboard.server --study RUN --port 8768` and open `http://localhost:8768/`. The read-only dashboard displays Study messages, work records and tool details; use it only where those records may be viewed. It binds to loopback and refreshes every five seconds. Optional historical curves can be supplied in `RUN/inputs/historical-curves.json`.

The repository supports only the pinned native Codex runtime. The Python host, Slurm resource adapter and read-only dashboard use that runtime. The synthetic goal in [examples/goal.md](examples/goal.md) demonstrates its research contract.

Run `uv run research-loop efficiency RUN` for an offline JSON audit of recorded
token usage, delegate activity, waiting, resource calls and settled allocations.
Use `--since` and `--until` with timezone-aware ISO 8601 timestamps to compare
periods. This reads existing traces on demand, makes no model or remote call,
and leaves the Study unchanged. Cached input is reported separately; allocation
time is not GPU utilization. See [the metric definitions](references/operations.md#efficiency-diagnostics)
for coverage and interpretation limits.

For continuous measurement, run `uv run research-loop metrics watch RUN --output
/path/to/private-metrics/baseline --since 2026-01-01T08:00:00+08:00 --phase baseline`.
The separate collector tails new trace bytes, checkpoints its progress and makes
no model or resource calls. Compare saved phases with `uv run research-loop
metrics compare BASELINE_DIR OPTIMIZED_DIR`; see [continuous measurements](references/efficiency.md).

## Development

Use `uv` for Python environments. After changing Python files, run `uvx ruff format` and `uvx ruff check` on the changed files, relevant tests and the smallest real smoke. See [Contributing](CONTRIBUTING.md).
