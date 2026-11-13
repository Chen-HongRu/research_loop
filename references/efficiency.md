# Continuous efficiency measurements

Collect recorded activity alongside an already authorized run:

```sh
uv run python -m research_loop.metrics watch /path/to/run \
  --output /path/to/private-metrics/baseline \
  --since 2026-01-01T08:00:00+08:00 --phase baseline
```

Output must be outside the run. The collector discovers Router histories under
`groups/router/*/native/sessions/` and per-Agent histories under
`group-runs/*/native-runtime/**/native/sessions/`. It reads initial selected history
once, then tails appended complete JSONL records. `--sessions-dir` selects only an
explicit trace snapshot. Agent/Router `roles.json` mappings establish membership;
native ancestry admits recorded descendants without inventing a scientific role.
Unmapped histories and mapped threads without histories are coverage gaps.

The active pointer and normalized configuration supply revision, profiles and the
resource ledger. `--ledger-path` explicitly selects a copied ledger when needed.
There is no root configuration or ledger fallback. `--interval` defaults to five
seconds; `--once --until TIMESTAMP` creates a fixed-window sample. Timestamps need
timezones, with start-inclusive/end-exclusive notification windows.

This deterministic sidecar calls no model, network or resource tool and changes
only its own private output. Use an existing supervisor for long collection.
SIGTERM stops it. Restart with the same output, phase, sources and start time to
continue a current-format checkpoint. A second writer is rejected. A different
source/checkpoint format requires a new phase directory; old samples remain
historical and are not migrated.

Outputs are `latest.json`, cumulative `samples.jsonl`, and a restart checkpoint.
Never sum cumulative samples. History retains aggregates; current per-task details
remain in `latest.json`. Reader bytes/time/CPU measure collection separately from
research work. A saved sample is distinct from a newly observed source event.
Checkpoint-first publication repairs an uncommitted history tail after interruption;
a journal shorter than the committed position is an error.

## Accounting and observed behavior

Offline `research-loop efficiency RUN` and continuous collection share the
request-usage acceptance rule: valid `token_usage_record` receipts are deduplicated
by `response_id` across selected threads, including pre-window receipts and
compaction requests. Input includes cached input; output includes reasoning.
Cumulative notifications are a cross-check, never a fallback or added total.
Missing receipts remain incomplete cost coverage. Both readers require complete
newline-terminated records and chronological sources; compare them at the same
cutoff and selected histories.

Reports retain accounting schema version 2. Historical version 1 offline reports
used cumulative differences and represent a different measure. Neither changing
source discovery nor displaying a historical sample retroactively verifies its
coverage or accounting.

Current samples expose configuration revision/profiles, usage by recorded
role/task, startup/early-response cohorts, paired wait durations, resource calls
and settled allocation receipts. `tools` aggregates explicitly returned native
outcomes; each task has `tool_outcomes`. Failure requires structured `isError`,
`is_error`, `success` or `exit_code` evidence. Plain output text is unmeasured,
including text containing an error word. Resource call failure is distinct from a
scientific command's nonzero result.

Code-mode `exec` outputs can include JSON command receipts in `input_text` blocks,
directly or inside a `fulfilled`/`value` wrapper. When recognized,
`tool_outcomes.nested_command_receipts` reports `observed_receipts`,
`zero_exit_receipts`, and `nonzero_exit_receipts`; aggregate `tools` retains the
same fields. These count emitted receipt observations, not outer `exec` failures
or independently verified infrastructure errors. A search with no matches can
return nonzero. Command stdout remains opaque, and ordinary text is not parsed
for error words or nested receipts.

Wait intervals can overlap across tasks and do not establish wasted work. Tool-free
time is not automatically idle. Background-read association is a visible-read
heuristic rather than demonstrated waste. Allocation duration is not GPU
utilization, billing or scientific value. Missing activity cannot be reconstructed
from model text. The [behavior collector](behavior-metrics.md) provides clipped
wait unions, output sizes and explicit readiness observations.

## Phase comparisons

Keep host, model/effort, scientific protocol, backend and workload comparable.
Activate policy/configuration changes at the runtime's explicit revision boundary,
retain operation identities and completed evidence, then create a new phase:

```sh
uv run python -m research_loop.metrics watch /path/to/run \
  --output /path/to/private-metrics/focused \
  --since 2026-01-01T09:00:00+08:00 --phase focused \
  --baseline /path/to/private-metrics/baseline
uv run python -m research_loop.metrics compare \
  /path/to/private-metrics/baseline /path/to/private-metrics/focused
```

`--baseline` refreshes a descriptive comparison beside the phase directory. Compare
completed, unmixed tasks with retained counts and coverage. Router work is separate
from Agent cohorts; an Agent with no native parent still belongs to its recorded
Group. Existing sessions crossing a phase boundary are mixed, and unfinished tasks
remain pending. Compare whole-task costs and latency alongside response averages;
work moved between components is not a demonstrated saving.

Different research workloads yield descriptive observations, not a causal A/B
result. Missing comparable samples remain missing. Exploration quality requires
reviewing actual hypotheses, evidence, discriminating tests and decisions; token
counts and best scores do not independently measure it.

The historical dashboard can read operator-supplied collector snapshots through
its metrics configuration. Its display preserves accounting versions and coverage;
missing measurements remain unavailable. Access control belongs to deployment,
and display does not start a collector or certify current-runtime coverage.
