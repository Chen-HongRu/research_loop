# Research Loop

By default, Research Loop coordinates an evidence-driven Study with a tool-free Router and independent, versioned Group Runs. The reference workflow selects scientific directions, runs individual experiments, synthesizes evidence and diagnoses out-of-scope faults. Python manages publication, events and authorized resources.

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

The same checkout configuration works on Linux and macOS: rebuild `.venv` with
`uv sync --locked` on each machine rather than copying it. Relative interpreter
declarations are resolved on the current machine when creating a fresh Study.
To run the real repository regression audit through the complete Group workflow,
use `uv run --locked python examples/portability/run.py /path/to/new-private-study --model gpt-6.1-terra --run`
after authorizing its model calls and CPU execution. This runs the actual selected
test suites once, obtains independent evaluation and publishes its findings. See
[portable fresh Studies](references/installation.md#linux-and-macos-fresh-studies).

New Studies use the reference Group catalog by default. Bounded recovery publication and the explicit-task Direction → Exp → Synthesis workflow are verified on Linux and macOS; autonomous exploration and resource/GPU acceptance remain pending. See the validation status in the Group contract. Pass `--groups /path/to/groups.json` to replace it, or `--coordinator lead` to select the legacy Lead path. Existing Study configuration is unchanged; host/native delegation is separate from Group coordination. See the [Router Group contract and usage](docs/router-groups.md). The reference workflow handles Group lifecycle and completion mechanically. For publication recovery from a closed, settled experiment without rerunning it, initialize a new Study with `--recover-exp /path/to/closed-exp-run`; the Group contract describes its evidence and review requirements.

Each iteration settles native work and authorized resources, publishes an independent knowledge snapshot, and follows the explicit `study_action` supplied by the Lead or a Group-authored result: `continue` starts the next iteration (resuming the same Lead thread only in Lead mode); `stop` ends the Study with a `stop_reason`. Scientific goal status is reported separately. Completing one diagnostic does not complete the Study. Results and status are retained under `iterations/NNNNNN/`; root records show the latest iteration. Failed work, UNKNOWN resources and STOP halt continuation and retain drafts.

To prioritize your own experiment, run `uv run research-loop experiment RUN --file experiment.md` or ask a separately user-authorized Codex session to inject it with that command. The next iteration reads `RUN/inputs/human.md` before autonomous planning, within the existing goal and authorization. The controller clears the admitted request only after execution, analysis and successful publication; failures and blocked work retain it. Use `uv run research-loop edit-experiment RUN` to edit safely while the loop runs. See [human experiments](references/operations.md#human-experiments) for editing, restart and completion semantics.

An optional OpenAI-compatible provider can drive the same native harness instead of the Codex ChatGPT subscription. Supply it at initialization; the API key is read from the named environment variable at run time and is never written to the run records:

```sh
uv run research-loop init RUN --project PROJECT --goal GOAL \
  --provider NAME --provider-base-url https://provider.example/v1 \
  --api-key-env PROVIDER_API_KEY --model MODEL --effort medium \
  --coordinator lead --delegation host
```

In Lead mode, provider runs default to `--delegation host`: the host starts each role as a new Codex thread, injects the canonical role contract, sends the assignment as a plain user message, and gives the lead `delegate_task`/`wait_tasks` tools. `wait_tasks` is non-blocking, so the lead can continue useful coordination while roles run; up to four role tasks run concurrently. Host threads are recorded in `roles.json`, which both `efficiency` and `metrics watch` use for role attribution. Host-mode catalogs omit `multi_agent_version` so native Codex subagent tools are disabled for the lead and roles. `--delegation native` keeps the Codex V1 spawn path as a comparison; its catalog declares `multi_agent_version = "v1"` and `supports_search_tool = false` because non-OpenAI Responses gateways cannot read Codex V2 inter-agent task payloads. Every provider catalog disables `supports_search_tool`; a supplied catalog must match the selected delegation mode. Unless `--model-catalog FILE` supplies one, the host generates a catalog declaring the model and `--model-context-window` (default 131072). Hosted web search is disabled in provider mode. Provider mode needs the pinned Codex runtime but no ChatGPT login. Model and remote execution still require explicit authorization, and provider-mode results are not evidence of parity with the subscription baseline. See [operations](references/operations.md#openai-compatible-provider).

An optional `--resource FILE` on `init` supplies authorized Slurm resources. `inspect` shows the resolved run configuration without a model call; a completed Study is cached unless explicitly continued with `run RUN --continue`, including Studies completed by the earlier single-iteration runtime. Continuation preserves the original thread and ledger. See [installation](references/installation.md), [operations](references/operations.md), [Linux deployment](references/linux-deployment.md), and the [design](docs/design.md). Model calls and remote compute require explicit authorization. Keep private inputs, credentials, runtime artifacts and checkpoints outside Git.

To select an existing Python environment for agent shell commands, add
`--environment FILE` to `init`. For example, after `uv sync --locked` in this
checkout, `--environment examples/environment.json` selects its `.venv`.
The coordinator and fresh role agents receive the selected interpreter and
installed package versions. Local metadata is observed without importing those
packages; optional resource-environment declarations are labeled unverified.
No dependencies are installed and no remote host or GPU is contacted by this
feature. See [execution environments](references/operations.md#execution-environments)
for configuration, shell behavior and resource debugging. Existing Studies
without environment configuration keep using the controller's Python environment.

To view a Study locally, run `uv run python -m research_loop.dashboard.server --study RUN --port 8768` and open `http://localhost:8768/`. The read-only dashboard separates a concise overview from iteration history, tasks, activity, resources/metrics and research context. Iteration history shows ten previews per page; task and activity lists show twenty records per page, with details fetched on demand. The overview distinguishes current execution from the most recent reported result and its source iteration; publication does not establish scientific success. It includes both native delegates and `roles.json`-mapped host roles, with recorded versus inferred roles labeled separately; use it only where those records may be viewed. It binds to loopback and refreshes the active operational view every five seconds; context loads when opened. Optional historical curves can be supplied in `RUN/inputs/historical-curves.json`.

Metrics retain their accounting version, authority and coverage; legacy or unversioned snapshots are labeled explicitly. The dashboard only reads existing collector outputs. Optional behavior observations can be exposed through `RUN/metrics/behavior/latest.json`, pointing to a separately collected behavior phase within the configured metrics root; missing observations remain unavailable. See [continuous metrics](references/efficiency.md) and [behavior observations](references/behavior-metrics.md).

To switch among running and historical Studies, repeat `--study RUN_A --study RUN_B`, or use `--studies-dir STUDIES` to discover immediate child directories with `status.json`. These options can be combined; only include Studies whose records may be viewed by everyone with dashboard access. Discovery refreshes every five seconds and skips symlink children. The selector keeps the chosen Study in the page URL; messages, tools and metrics follow that selection. Unknown or removed selections show an error rather than another Study's records.

The overview includes a chronological research timeline from `iterations/NNNNNN/status.json` and `result.json`: reported summaries, goal judgments, next steps, limitations and evidence references. Status-only rounds are marked as having no result. Legacy root results appear only when there are no archived rounds. This is a projection of saved reports, not an independently verified scientific conclusion or a generated synthesis. It does not reconstruct experiments that were never recorded in those reports. Optional historical curves remain separate background material.

The repository supports only the pinned native Codex runtime. The Python host, Slurm resource adapter and read-only dashboard use that runtime. The synthetic goal in [examples/goal.md](examples/goal.md) demonstrates its research contract.

Run `uv run research-loop efficiency RUN` for an offline JSON audit of recorded
token usage, delegate activity, waiting, resource calls and settled allocations.
Use `--since` and `--until` with timezone-aware ISO 8601 timestamps to compare
periods. This reads existing traces on demand, makes no model or remote call,
and leaves the Study unchanged. Both offline and continuous Token totals use
deduplicated per-request usage; cumulative counters are only a cross-check.
Missing request records mark coverage incomplete and never supply a cumulative
fallback. Offline reports now use schema version 2; version 1 totals used
cumulative counter differences and should not be compared as the same measure.
Continuous collectors also use version 2; version 1 checkpoints require a fresh
output directory to recompute from available trace history.
Cached input is reported separately; allocation
time is not GPU utilization. See [the metric definitions](references/operations.md#efficiency-diagnostics)
for coverage and interpretation limits.

For continuous measurement, run `uv run research-loop metrics watch RUN --output
/path/to/private-metrics/baseline --since 2026-01-01T08:00:00+08:00 --phase baseline`.
The separate collector tails new trace bytes, checkpoints its progress and makes
no model or resource calls. Compare saved phases with `uv run research-loop
metrics compare BASELINE_DIR OPTIMIZED_DIR`; see [continuous measurements](references/efficiency.md).

For child-agent waiting, explicit dependency-ready receipts and visible tool-output
sizes, use the separate [behavior collector](references/behavior-metrics.md).

## Development

Use `uv` for Python environments. After changing Python files, run `uvx ruff format` and `uvx ruff check` on the changed files, relevant tests and the smallest real smoke. See [Contributing](CONTRIBUTING.md).
