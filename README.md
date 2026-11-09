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

An optional OpenAI-compatible provider can drive the same native harness instead of the Codex ChatGPT subscription. Supply it at initialization; the API key is read from the named environment variable at run time and is never written to the run records:

```sh
uv run research-loop init RUN --project PROJECT --goal GOAL \
  --provider NAME --provider-base-url https://provider.example/v1 \
  --api-key-env PROVIDER_API_KEY --model MODEL --effort medium \
  --delegation host
```

Provider runs default to `--delegation host`: the host starts each role as a new Codex thread, injects the canonical role contract, sends the assignment as a plain user message, and gives the lead `delegate_task`/`wait_tasks` tools. `wait_tasks` is non-blocking, so the lead can continue useful coordination while roles run; up to four role tasks run concurrently. Host threads are recorded in `roles.json`, which both `efficiency` and `metrics watch` use for role attribution. Host-mode catalogs omit `multi_agent_version` so native Codex subagent tools are disabled for the lead and roles. `--delegation native` keeps the Codex V1 spawn path as a comparison; its catalog declares `multi_agent_version = "v1"` and `supports_search_tool = false` because non-OpenAI Responses gateways cannot read Codex V2 inter-agent task payloads. Every provider catalog disables `supports_search_tool`; a supplied catalog must match the selected delegation mode. Unless `--model-catalog FILE` supplies one, the host generates a catalog declaring the model and `--model-context-window` (default 131072). Hosted web search is disabled in provider mode. Provider mode needs the pinned Codex runtime but no ChatGPT login. Model and remote execution still require explicit authorization, and provider-mode results are not evidence of parity with the subscription baseline. See [operations](references/operations.md#openai-compatible-provider).

An optional `--resource FILE` on `init` supplies authorized Slurm resources. `inspect` shows the resolved run configuration without a model call; a completed Study is cached unless explicitly continued with `run RUN --continue`, including Studies completed by the earlier single-iteration runtime. Continuation preserves the original thread and ledger. See [installation](references/installation.md), [operations](references/operations.md), [Linux deployment](references/linux-deployment.md), and the [design](docs/design.md). Model calls and remote compute require explicit authorization. Keep private inputs, credentials, runtime artifacts and checkpoints outside Git.

To view a Study locally, run `uv run python -m research_loop.dashboard.server --study RUN --port 8768` and open `http://localhost:8768/`. The read-only dashboard displays Study messages, work records and tool details. It includes both native delegates and `roles.json`-mapped host roles, with recorded versus inferred roles labeled separately; use it only where those records may be viewed. It binds to loopback and refreshes every five seconds. Optional historical curves can be supplied in `RUN/inputs/historical-curves.json`.

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
