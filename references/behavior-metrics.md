# Observational behavior metrics

`research_loop.behavior_metrics` is a local, single-writer sidecar. It reads the
Study's `status.json`, optional `config.json`, `events.jsonl`, and native session
JSONL histories. It writes only its own output directory, outside the Study. It
never invokes a model, changes a research budget, inspects datasets/checkpoints,
or launches compute. Native metadata and Host role mappings select Study threads. Roles use explicit
Host mappings or task-name-prefix inference; unknown roles stay unclassified.

```sh
uv run python -m research_loop.behavior_metrics watch /path/to/study \
  --output /path/to/metrics/behavior-baseline \
  --since 2026-01-01T00:00:00Z --phase baseline --interval 5

uv run python -m research_loop.behavior_metrics watch /path/to/study \
  --output /path/to/metrics/behavior-snapshot \
  --since 2026-01-01T00:00:00Z --phase snapshot --once \
  --until 2026-01-01T01:00:00Z
```

Timestamps require an explicit timezone. `--until` is an exclusive cutoff and
requires `--once`. Reuse the identical root, phase, and since with the same output
to resume; use a fresh output directory for each phase and for this collector
rather than the ordinary metrics collector. SIGINT/SIGTERM stop between polls.
No automatic event-to-agent monitoring is installed.

The read-only dashboard can display aggregate observations from an already
collected phase through `RUN/metrics/behavior/latest.json`. Keep the behavior
phase inside the directory resolved by `RUN/metrics`; an operator may link
`behavior` to that phase. Use a separate phase from the ordinary collector's
`current` output. The resources/metrics page shows availability, measurement
window and coverage without launching a collector. A missing or invalid file
does not become a zero measurement. Individual tool payloads and private traces
are not exposed by this aggregate projection.

## Data contract

`latest.json` contains a cumulative phase snapshot. `samples.jsonl` journals phase
snapshots, retaining task aggregates but omitting the growing per-call
`tool_outputs` and `usage_groups` arrays. `checkpoint.json` holds offsets and minimal
bookkeeping. Do not edit these files while the collector runs. A nonblocking file
lock enforces one writer. Checkpoint-first publication repairs an interrupted
journal write on restart without duplicating sample IDs. Outputs have private
filesystem modes. No output contents or command text are copied into reports;
read signatures hash only the visible invocation, never file contents.

Normal saving and restart repair share `sample_store.publish_pending` with the
ordinary collector. Behavior calculation, enrichment and history detail pruning
remain in `BehaviorCollector`; the shared code only completes the three output
files. It preserves the committed journal prefix and rejects a journal shorter
than the checkpoint position. Existing version 2 phases resume with the same
fields and sample IDs; no format migration is needed for this refactor.

`behavior.by_task` preserves thread ID, parent, task, role, start-before-phase and
right-censoring flags. `behavior.total`, `behavior.children`, and `behavior.by_role` give counts, wait,
byte and associated-token aggregates for all sessions, descendants, and roles.
Aggregates include only sessions that the original Collector marks
`active_in_phase` in that sample (a recorded source event in the phase). Full
`by_task` retains historical sessions with this flag explicit. Incomplete historical
sessions do not become active merely because their lifetime is right-censored.
Readiness missing counts and coverage lists include only active descendants; the
lead coordinator does not need a readiness record. Aggregated wait unions sum
per-session unions and can overlap across sessions. `resources` and `allocation` fields
are omitted; the ordinary metrics collector remains their authoritative sidecar.
`llm` retains the shared unique-response usage counters as context, with the same
request-usage acceptance rule as ordinary continuous metrics and offline schema
version 2 audits. Cumulative counters never replace missing request receipts. Input
includes cached input; output includes reasoning. These are tokens, not currency.
The underlying report/checkpoint version is now 2. Start a fresh output directory
for a version 1 phase; old receipts with absent usage fields cannot be corrected
from saved offsets alone. Historical samples are not rewritten.

- `wait_calls`: visible `wait`, `wait_agent`, or `sleep` calls started in the phase.
  Namespaced native tools are normalized to their terminal name. Returned waits
  include returns in the phase even when calls started earlier. `wait_seconds`
  sums completed wait intervals clipped to the phase and observed session lifetime.
  `wait_union_seconds` merges overlap; `wait_lifetime_fraction` divides that union
  by the session wall lifetime overlapping the phase. Open waits remain censored.
  The denominator ends at the latest `task_complete`, or the cutoff while running;
  an explicit `task_started` clears prior completion. Resumed sessions include
  intervening idle wall time. None of these values measures wasted compute.
- `created_at_proxy` uses `session_meta`, not an independently verified spawn
  receipt. Creation-to-first-tool and creation-to-first-delivery use the first
  observed tool call and final-answer message or `task_complete`. They span the
  entire observed session, including time before the phase. Missing observations
  produce JSON null. Tool-free latency may include reasoning and scheduling; it
  is never classified as waiting or waste.
- Every returned top-level tool call in the phase has its call ID, tool name,
  timestamps and `output_utf8_bytes` in `tool_outputs`. String bytes use UTF-8;
  structured outputs use compact JSON with `ensure_ascii=False`. These are visible
  payload bytes, not transport bytes or tokens. Missing output is null, counted
  separately, and excluded from byte sums. `over_16kib_count` uses strictly greater
  than 16,384 bytes. Its byte fraction divides large-output bytes by known output
  bytes; an empty denominator is null.
- Explicit truncation recognizes a boolean `truncated` on the output event or
  structured output, and literal native notices `Warning: truncated output`,
  `Output truncated`, or an integer followed by `tokens truncated`. No marker
  means null, not false; length alone never proves truncation.
- `usage_groups` connects all tool outputs since the previous accepted usage
  record to the next unique response ID in that thread. A batch shares one token
  receipt. `associated_usage` counts that receipt once within each category
  (`any_output`, `wait_return`, `large_output`, `repeated_visible_read`). Categories
  overlap and must not be added. Usage notification time determines phase
  inclusion, so an output before the phase can associate to phase usage. Pending
  outputs are reported without invented cost. Association does not prove marginal
  cost, causation, overhead, or unnecessary work.
- `repeated_visible_read_calls` means identical input to visible `read`,
  `read_file`, or `read_text`, or an exact repeat of a read-like shell/exec
  invocation containing `cat`, `sed`, `head`, `tail`, or `.read_text(`. Signatures
  include tool name and full visible arguments. Repetitions can reference a call
  before the phase. This conservative syntactic proxy does not verify execution,
  file identity, equal returned content, or redundant reading. Tool calls nested
  inside an exec result are not enumerated.

## Explicit readiness records

Readiness records require separately configured operator or coordinator
instrumentation. This collector reads the explicit records described below; it
does not install a helper, alter coordinator prompts or enforce admission.

No prompt text, encrypted spawn arguments, file existence, elapsed silence, or
scientific artifact is interpreted as dependency readiness. Without an explicit
record, `dependency_ready_status` is `missing` and readiness comparisons are null.
The coordinator may emit an `agent.dependency_ready` event into its own
`events.jsonl` with `at`, the child's `thread_id`, and `dependency_ready_at`.
Alternatively it may maintain an optional append-only
`work/dispatch-readiness.jsonl` with one JSON object per complete line:

```json
{"task_id":"analyst_one","ready_at":"2026-01-01T00:02:00Z","confirmed":true,"confirmed_by":"coordinator"}
```

The coordinator must write a record only after it actually confirms all required
dependencies according to the active task contract. The collector never creates
these records. `task_id` must uniquely match the native metadata's terminal task
name; ambiguous or unknown tasks remain unmatched and are reported. Records can
arrive before a session becomes discoverable and are reconciled on later polls.
Conflicting readiness timestamps produce `conflicting` and null comparisons.
`created_minus_dependency_ready_seconds` compares the creation proxy with the
explicit readiness time; a negative result means creation preceded the recorded
readiness. It is descriptive and does not itself establish a scientific violation.

## Coverage and operating cost

`coverage` exposes source issues, missing authoritative usage and unknown threads;
`behavior.coverage` additionally exposes missing readiness and unmatched readiness
records. Readiness absent from an uninstrumented study is a coverage limit, not
zero startup delay. Missing truncation markers and outputs awaiting usage stay
visible per task. Invalid JSON/timestamps, rotated/truncated histories, and invalid
behavior records remain gaps; incomplete final JSONL lines are retried. A cutoff
at a future-dated row defers that tail. Source timestamps must be chronological.
Snapshots are not atomic across files and cannot prove complete recording.

The first poll reads session history to recover cross-phase state. Subsequent
polls read appended source bytes, small status/configuration metadata, and first
lines of newly discovered sessions. The reader performs no content validation
scan or dataset hashing. State and latest publication scale with observed calls;
journal entries scale with task aggregates and poll count. `reader` exposes bytes
and elapsed/CPU collection time, excluding current durable publication; the next
sample includes the previous complete poll time. Runtime overhead, peak memory
and impact on a real study remain unmeasured by synthetic tests.

## Resource presentation metadata

`resource_presentations` observes phase-local `resource.tool` events for exactly
`gpu_exec` and `gpu_poll` in the same events tail pass. It does not reread events,
change source formatting, or add anything to LLM token totals. A record is
`eligible` only when its output is a dictionary containing an explicit boolean
`output_compacted` and nonnegative integer `output_bytes` and
`returned_output_bytes` (booleans are not integers here). Across eligible records,
`compacted` counts true markers; `original_output_bytes` sums `output_bytes`, and
`returned_output_bytes` sums the returned-byte metadata. These are recorded
presentation measurements, not recomputed payload sizes or demonstrated token
savings. No assumption that returned bytes are less than original bytes is added.

`observed_calls` includes all targeted phase events. Missing fields or non-dictionary
outputs count as `missing_or_legacy`; incorrect types or negative byte values count
as `invalid_metadata`. They never become eligible or contribute guessed bytes.
When eligible is zero, `compacted`, `original_output_bytes`, and
`returned_output_bytes` are null. `status` distinguishes `no_records`,
`observed_metadata`, and `partial_metadata`. Partial byte sums cover only the
eligible subset, not all observed calls.

The `behavior_schema` remains 1. New output directories start with
`coverage_mode: phase_history_and_append`, `read_from_at` equal to phase `since`,
and `read_from_at_basis: phase_event_time`. Existing checkpoints missing the new
state initialize it with `setdefault`, preserve all prior offsets, and report
`coverage_mode: existing_offsets_only`, `prior_source_bytes_not_replayed: true`,
`read_from_at` equal to enablement wall time, and
`read_from_at_basis: collector_wall_time`. This wall time is a reader activation
boundary, not a filter on event timestamps: appended records may carry earlier
timestamps. Historical records consumed by the older collector are not replayed
or represented as measured zero. The marker persists across later restarts. A
recovered pending sample created before instrumentation reports
`status: not_collected_in_this_sample`. Use a fresh phase directory to collect
complete phase history for this new metric. Running processes load the new code
only after restart; the baseline may continue to its planned boundary, where the
existing checkpoint can be resumed for final sealing without claiming historical
resource presentation coverage.

## Continuous descriptive comparison

```sh
uv run python -m research_loop.behavior_metrics compare \
  /path/to/metrics/baseline /path/to/metrics/improved

uv run python -m research_loop.behavior_metrics watch /path/to/study \
  --output /path/to/metrics/improved --since 2026-01-01T01:00:00Z \
  --phase improved --interval 5 --baseline /path/to/metrics/baseline
```

`compare` reads only the two `latest.json` snapshots. It reports phase windows,
configuration, source coverage and resource presentation metadata separately. Its
cohort includes active descendants with known starts in the phase, a completion,
and no right-censoring. Only roles with at least one such task in both snapshots
enter the shared-role cohort. Counts expose excluded mixed/incomplete tasks and
roles present on just one side. Historical sessions and the lead are excluded.
Roles use explicit Host mappings or task-name-prefix inference; shared roles do
not imply matched workloads.

Cohort reports include mean per-task and pooled wait-lifetime fractions, mean
output bytes for tasks with known output sizes, large-output count and fractions,
associated usage totals and per-complete-task means, and readiness observation
coverage with known creation comparisons. Usage means exclude tasks with pending
outputs or missing/unknown authoritative usage coverage. Missing numerators or
empty denominators remain null; an unobserved readiness comparison does not
become zero premature creations. Active-child readiness coverage is also shown
outside the completed cohort. Association remains token observation, never a
claim of savings or causal improvement. Resource metadata retains its own
coverage mode and may be unavailable in an older source snapshot.

`watch --baseline DIR` atomically replaces
`OUTPUT_PARENT/behavior-comparison.json` after each committed sample. Pass this
flag only to the improved-phase service: its writer owns that comparison file.
The baseline directory must differ from the output directory and remains
read-only. The two snapshots are read sequentially rather than atomically as a
pair; use a sealed baseline when a fixed comparison is required.
