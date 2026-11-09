# Continuous efficiency measurements

Use the operator collector alongside an already authorized Study:

```sh
uv run research-loop metrics watch /path/to/study \
  --output /path/to/private-metrics/baseline \
  --since 2026-01-01T08:00:00+08:00 --phase baseline
```

The output directory must be outside the Study. The collector discovers native
sessions, reads existing trace history once to establish identities and counters,
then reads only appended complete JSONL records. Its default interval is five
seconds. `--interval`, `--sessions-dir` and `--ledger-path` select the interval or
operator-supplied snapshot sources. `--once --until TIMESTAMP` makes a fixed-window
measurement. All timestamps require a timezone; intervals are start-inclusive and
end-exclusive.

Run this command under the host's existing process supervisor. The collector is
deterministic and has no model, network, GPU or resource-tool calls. It never
changes the Study, training inputs, checkpoints, authorization or research
controller. SIGTERM stops collection. Restart with the same output, phase,
sources and start time to continue from its checkpoint; a second writer is
rejected. Use a new directory for a new phase. Keep all outputs private.

Each output directory contains `latest.json`, cumulative `samples.jsonl` and a
restart checkpoint. Samples are cumulative observations, so do not sum their
totals. History retains aggregate fields; current task details stay in
`latest.json`. The collector records its bytes read and elapsed/CPU time separately from
research work. Source gaps, partial tails and unrecognized or missing usage are
coverage conditions, not evidence of zero consumption. A successful sample and a
new source event are different observations.

Usage comes from unique native `token_usage_record` response IDs, including
compaction. Cumulative token notifications are a cross-check and are never added
again. Input includes cached input; output includes reasoning. The report keeps
role/task totals, startup and early-response cohorts, observed waiting, resource
results and allocation receipts. Roles are inferred from task names. Background
loading is a fixed heuristic associating visible goal/knowledge/instruction reads
with the next response; handoff text or summaries can move work outside that
heuristic. It is not a measure of proven waste. Allocation duration is not GPU
core utilization, and concurrent waits are not additive wall time.

To compare a strategy change, first collect a baseline on the same host, model,
effort, scientific protocol, backend and current knowledge. Observe complete tasks
in the stages being optimized. Record source versions, policy activation time,
knowledge sizes and any goal-text edits. Switch at a safe experimental boundary,
preserving the original Study, coordinator thread, ledger and completed work.
Start another collector with a new phase directory:

```sh
uv run research-loop metrics watch /path/to/study \
  --output /path/to/private-metrics/focused \
  --since 2026-01-01T09:00:00+08:00 --phase focused \
  --baseline /path/to/private-metrics/baseline
uv run research-loop metrics compare \
  /path/to/private-metrics/baseline /path/to/private-metrics/focused
```

`--baseline` refreshes `comparison.json` in the parent metrics directory after
each sample. The read-only dashboard can display the current snapshot and this
comparison through an operator-maintained `RUN/metrics` link to that directory,
with `current` pointing to the active phase. Preserve the dashboard's existing
authentication, loopback binding and reverse-proxy access controls.

Compare completed, unmixed tasks of the same role and research stage. Retain the
sample count, all input/cache/output usage, result artifacts and source coverage;
also inspect coordinator costs so work moved out of a child is not mistaken for
savings. Existing threads crossing the boundary are mixed, and unfinished tasks
are pending rather than completed observations. Whole-task costs and delivery
latency matter more than response averages alone. Different live research tasks
provide descriptive evidence, not a causal A/B result. Missing comparable samples
remain missing; no extra scientific computation is required just to fill a table.

For Scout, keep stable hypothesis IDs and distinguish proposed, reviewable,
dispatch-ready, executed and explicitly deferred work in the existing coordination
records. Classify scientific contributions and relevant scheduling opportunities
from their actual artifacts. Token counts, literature volume and a new best score
do not automatically quantify exploration or its value.
