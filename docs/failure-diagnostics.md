# Failure diagnostics and recovery

The controller separates responsibility for a failure from permission to allocate
more compute. Reading logs does not authorize a new submission or a budget change.

| Responsibility | Evidence | Controller action |
| --- | --- | --- |
| Parameter | A designated pure validator rejects an unaccepted Agent output or an unclaimed request | Retain the rejection and return corrective feedback to its originating stage/Agent within the original task and remaining budgets. No computation is replayed. |
| Workspace | Authorized source/configuration or execution failed; identity and effects are reconciled | Return diagnostics to the same investigation's Agent. Use its original Plan, scope and remaining budgets. Publish an incomplete result for independent Review when bounded repair cannot finish. |
| Pipeline | Controller, protected evaluation, permissions, identity or accounting failed | Retain a blocking diagnostic for user/developer intervention. |
| Unknown | Submission, job identity, terminal state or accounting is unresolved | Preserve the original attempt and reconcile it. Do not submit a replacement or assume zero cost. |

The generic correction loop applies to unaccepted read-only stage responses.
Pydantic representation errors and designated pure cross-field checks are
correctable; an arbitrary `ValueError` from a controller or harness is not.
Execute response rejection does not authorize another Execute call. Adapter
request preparation may reject explicit action, missing entrypoint, authored-source
policy or syntax errors before claim. Frozen Plan, protected source and trusted
input failures remain blocking. Preparation allocates no compute; after claim,
the durable submission and existing effect-reconciliation rules remain authoritative.

A successful Agent response with `outcome=incomplete` and no measurements can enter
Review after the controller verifies scope and absence of submission effects. A
missing final response, failed Agent CLI or Agent claim that nothing was submitted
does not establish that condition. Source syntax errors are diagnostic evidence,
not a prerequisite for reporting incomplete work.

## Records and readers

Agent CLI invocations retain `process.json`, `stdout.jsonl`, `stderr.txt`, and any
preflight streams in their attempt directory. A failed invocation additionally
publishes `failure-diagnostics.json` with a public log catalog. A pipeline stop
records `state.json`'s `diagnostic_ref`; the referenced record identifies the phase,
iteration, attempt, cause chain and unresolved effects.

Controlled server commands retain stdout and stderr independently, including the
initial submission error and later recovery, collection or cleanup errors. ProTok
uses `control/command-diagnostics` for formal continuation commands and each
research/long job's `command-diagnostics` directory for its commands. Existing job
receipts remain authoritative for identity, terminal state and GPU accounting.
Supervisor inspection failures retain a public diagnostic in
`control/supervisor-status.json` and operator evidence in
`control/pipeline-diagnostics`. Identical blocked polls reuse the same failure
record rather than recording the same error on every poll.

Each diagnostic directory has `diagnostic-manifest.json`, operator-only `raw/`
streams and redacted `public/` streams. Explicit controller credentials and common
credential fields are removed from the public projection. Arguments, prompts,
ownership records and authorization grants are not copied into Agent diagnostics.
Raw streams remain available to operators through the protected filesystem.
Encoded artifact and telemetry transfer payloads remain in raw streams; collector
log tails are decoded and redacted before entering the public projection.
These projections do not replace the host's existing sandbox or filesystem access
policy; raw paths are not part of the Agent log-reading interface.

Execute results, repair context and Review context provide registered public log
IDs, bounded previews and a `read_command`. Append an ID and byte range to read
the needed part of a complete log:

```sh
uv run python -m research_loop.diagnostics read \
  --directory /path/to/command-diagnostics \
  --log-id REGISTERED_ID --offset 0 --limit 8192
```

Use `next_offset` to continue until `eof`. The byte limit may extend by up to three
bytes to finish a UTF-8 character; `next_offset` remains the actual byte position.
The reader accepts only registered IDs, refuses path traversal and symlinks, and
reads already-redacted streams so secrets cannot appear across page boundaries.
Each stream is an immutable snapshot; later commands receive new IDs. Previews are
not the complete evidence.

## Recovery and evidence

Repair uses existing call, time and GPU grants. A CPU-only repair does not grant a
smoke or formal allocation. Exhausted repair capacity produces incomplete evidence;
New repair calls use fresh Study budgets and reserve one call for independent
Review. Review still obeys the remaining time budget and original wall deadline;
its completion time is not guaranteed. An expired execution
window prevents new work and does not invalidate already-settled receipts.

Diagnostics do not change candidate eligibility, fixed evaluation, data roles or
Frontiers. Review distinguishes diagnostic messages, partial observations and
trusted measurements. Source/identity changes, pending submissions, unknown effects
and pipeline failures remain blocking even when a log suggests a candidate bug.

Collection errors are retained separately from the failure being investigated.
They neither erase the original cause nor change settled costs or certify unknown
work as complete. Logging and reading inspect command streams and small control
metadata; they do not scan datasets, caches or checkpoints.
