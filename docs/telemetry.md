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
