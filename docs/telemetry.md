# Tagged live observations

Training and research code may emit structured observations while an allocation is
running. The controller retains every tag. The live panel consumes only records
whose `tags` include `dashboard`; Review uses the unfiltered record.

Tags control presentation. They do not grant access, verify a measurement, change
data roles or make a metric eligible for a Frontier. Live observations remain
unreviewed until the ordinary independent Review checks their evidence. Existing
fixed evaluation receipts and partial-checkpoint acceptance remain authoritative.

## Producer interface

Local code imports `research_loop.telemetry.emit`. ProTok stages the same standalone
stdlib implementation as `research_loop_events` in the read-only source snapshot:

```python
from research_loop_events import emit

emit(
    "train/loss",
    float(loss),
    step=optimizer_step,
    unit="loss",
    tags=["dashboard", "training"],
    context={"split": "development", "seed": 42},
)
emit("gradient/norm", gradient_statistics, step=optimizer_step, tags=["diagnostic"])
```

Plan specifies names, units, conditions, publication boundaries and intended tags
through its existing observable declarations. Execute may add explicitly exploratory
observations without changing the fixed scientific contract. Default tags are
`["internal"]`. Custom labels are retained without registration.

`emit(name, value, *, tags=("internal",), step=None, unit=None, context=None,
path=None)` appends one UTF-8 JSONL record. The target is the explicit `path` or
`RESEARCH_LOOP_EVENT_FILE`. The record has these fields:

| Field | Value |
| --- | --- |
| `version` | `1` |
| `id` | Producer-generated UUID |
| `time` | Source Unix timestamp; byte order remains the stream order |
| `name` | User-defined observation name |
| `value` | Small JSON scalar, object or array |
| `tags` | List of labels; `dashboard` opts into the live panel |
| `step` | Integer optimizer/test step, or null |
| `unit` | Unit string, or null |
| `context` | Conditions such as scope, split, configuration and seed |

The encoded record, including its newline, is at most 8192 bytes. Nonfinite floating
observations become explicit objects such as `{"nonfinite":"NaN"}`; they remain
evidence and are not numeric plot points. Tensor values require an intentional
small scalar/JSON representation. Do not emit datasets, caches, checkpoint tensors,
secrets or final-only evaluation data into this development feedback channel.

The writer uses a lock and an unbuffered append. If a prior writer left an incomplete
tail, it appends only the missing delimiter before the next record; prior bytes are
preserved. No per-event `fsync` or additional GPU synchronization is introduced.
This protects observations already written before a process exits or is killed;
it does not promise durability against node/power loss.

## Controller transport

Each owned job has immutable stream provenance: kind, Study experiment ID, source
identity and execution identity, plus run/job identifiers where applicable. The
trusted writer source is staged and version-bound without changing locked trainer
files or evaluation configuration.

ProTok reads at most 128 KiB per source stream approximately every 5 seconds. Only new
bytes are transferred; ordinary incomplete trailing records wait for their delimiter.
The controller stores raw received bytes at
`STUDY/telemetry/STREAM/events.jsonl` and small operational metadata at `meta.json`.
The actual local file length is the append cursor, including after a crash between
data append and metadata publication. Source identity, remote file identity and
truncation checks prevent another source from replacing the retained prefix.

Research scripts and long training use the supplied event writer. Fixed short
training remains unchanged: an adapter incrementally converts native training CSV
rows and scoped evaluation-completion JSON markers into observations. Native rows
retain their values and explicit step in one event. Raw/native cursor state is
separate from the converted local byte cursor. Unrelated stdout stays internal.

Transport errors preserve the received prefix and report synchronization state;
they do not cancel or resubmit training, change Slurm accounting, extend a grant or
alter STOP/cutoff rules. Finalization attempts to drain toward a captured remote
size under one bounded deadline. Incomplete coverage remains visible and can resume
from its retained cursor on reconciliation. Dataset and checkpoint contents are
never read or hashed by this transport.

## Readers and Review

`read_events(study_root, stream_id, after=..., until=..., tag=..., max_bytes=...,
limit=...)` pages bounded local bytes. Returned events include source and exact byte
offsets. A tag filter advances through the same raw stream; it does not delete
nonmatching evidence. Malformed or oversized records are reported with sanitized
offset ranges and do not block later valid observations. Genuine partial tail
records do not advance the reader cursor.

`snapshot` provides bounded recent previews, newest streams first. The dashboard's
`/api/telemetry` and ordinary snapshot use only `dashboard` records. They read local
copies without SSH, model calls, allocation or filesystem writes. Explicit stream
paging works beyond the preview window. History Study IDs and long-run IDs remain
separate, and curves group by source, observation name, unit and conditions.

Manual long-training tasks retain telemetry in
`study/tasks/TASK/study/iterations/ITERATION/telemetry`. Readers resolve the immutable
request and canonical binding; queued tasks have no stream yet. Running and reviewed
tasks share the same stable task identity. The selected-task API uses
`/api/telemetry?source=active&task=TASK` (or a configured `history-N` source),
optionally with `stream` and `after` for incremental paging. `task`, automatic
`run`, and main-study `experiment` selectors are mutually exclusive; existing
automatic/main queries keep their behavior. Manual responses include
`manual_task_id`; their actual execution run ID stays in stream provenance.
TensorBoard discovers the same bound locations and gives each manual task and
iteration its own lane, preserving source, Study and execution separation.

## Task progress and TensorBoard

Select a main investigation, research script or independent long-training task in
the Work tree. Its **实时回传指标** panel shows only that task's `dashboard`-tagged
observations, including numeric leaves of small JSON objects. The default overview
shows up to six recent series; the selector opens one series. Hover/focus reveals
the exact recorded value and step; click or Enter pins a point. Selection, pinned
values and expanded conditions survive the five-second dashboard refresh.

Sources, executions, original names, units and scientific conditions form separate
curves. Native CSV transport offsets do not split a scientific series. Explicit
null/nonfinite values and decreasing steps break lines. Records without a step use
their source timestamp. Arrays, strings and booleans are not inferred to be scalar
metrics. Each selected stream retains at most 256 recent dashboard events in the
browser; this is a live preview, not the complete evidence history.

The panel's **TensorBoard · 全量指标** button opens Scalars filtered to the selected
task at `/tensorboard/`. TensorBoard 2.21.0 selects no runs by default when more
than 40 match its run filter; the task filter keeps an experiment discoverable as
the total history grows. Existing explicit selections remain authoritative.
Install the optional CPU-only dependency and start its separate
service with the same active and history roots as the dashboard:

```sh
uv sync --extra dashboard
uv run --extra dashboard python -m integrations.protok.tensorboard \
  --root /path/to/run --history-root /path/to/archive \
  --logdir /path/to/derived/tensorboard --port 6006
uv run python integrations/protok/dashboard.py \
  --root /path/to/run --history-root /path/to/archive \
  --tensorboard-port 6006 --port 8767
```

The exporter reads only retained local telemetry. Each poll reads at most one
128 KiB / 200-event page per stream, including non-dashboard tags. It publishes
finite scalar/object fields to TensorBoard Scalars and each original JSON record
to Text. Arrays remain text; no histogram or image semantics are invented. Runs
use dashboard task identifiers: `active/#0026`, `history-1/#0001`,
`active/long/L…`, or `active/manual/TASK/#0001`. Additional executions of a task
have stable `(attempt N)` suffixes; training and evaluation streams from the same
execution share a run. Metrics, units, conditions, streams and axes remain
separate series, with readable display names and full provenance in descriptions
and Text evidence. Without a recorded step, a separate `event-order` series uses
retained event order; the original timestamp remains TensorBoard's wall time.
Experiments without retained telemetry cannot acquire historical training curves
from final scores alone.

The derived logdir must be outside the source Study. Only that directory receives
event files and incremental cursor journals. Layout v2 uses `events-v2/` and
`.export-state-v2/`; the previous `events/` and `.export-state/` remain intact.
The first v2 start incrementally replays retained telemetry into the new layout.
The derived layout is bound to its configured source roots; choose a new logdir
when changing roots. Restart completes an unfinished publication before newer
pages, without duplicate points, then catches up over successive polls. Published
filenames increase across streams so TensorBoard's existing reader sees appends.
`--once` exports one bounded page per stream and exits. No source log, control
state, data, cache or checkpoint is modified or read for validation.

The service binds only to `127.0.0.1`; the dashboard proxies it under the existing
protected origin. Do not expose port 6006 directly. Export and TensorBoard server
reload default to five seconds. TensorBoard 2.21.0 is pinned because the dashboard
bootstrap enables its native browser auto-reload on first visit, using that
version's persisted setting. Browser refresh defaults to 30 seconds; explicit
existing user preferences are preserved. TensorBoard can operate without
TensorFlow. It is an observation viewer; Review and Frontier eligibility still use
the existing frozen evidence boundaries.

## Review evidence

At the Review boundary, the Study publishes immutable
`iterations/NNNN/context/telemetry.json`. This manifest includes every stream for
that experiment, its provenance, absolute log reference, exact `byte_end`, sync
state and a bounded unfiltered preview. It does not copy or hash the full log.
Review receives this manifest in its `telemetry` context. Later appends do not extend
that Review's evidence boundary. The preview is not the complete history; all tags
can be paged through the captured boundary:

```sh
uv run python -B -m research_loop.telemetry \
  --study STUDY --stream STREAM --after 0 --until BYTE_END
```

Review distinguishes completed and partial observations, accounts for missing
coverage and source conditions, and preserves useful findings in research knowledge.
Repeated rows or different tags from one run do not constitute independent
replications. Display labels never replace complete fixed validation or authorize
scientific conclusions.

## Verification boundary

Local tests cover producer append/concurrency, live transfer before completion,
reconnection/cursor recovery, malformed and incomplete records, readonly filtered
HTTP access, all-tag Review access and immutable Review prefixes. GPU/Slurm runtime
operation and actual synchronization latency require a separately authorized smoke.
