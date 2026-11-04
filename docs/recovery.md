# Interruption and recovery

This guide describes the serial document controller. The new
[SQLite runtime foundation](transactional-runtime.md) preserves unresolved
attempts across owner epochs and does not yet expose recovery or migration in
the CLI. Legacy entry points refuse its marker or database instead of replaying
`state.json` over authoritative SQLite state.

See [Failure diagnostics and recovery](failure-diagnostics.md) for workspace versus
pipeline responsibility, Agent-readable server logs and operator blocking records.

Every Agent attempt has its own directory. CLI stdout/stderr stream directly to
files during generation. A successful parsed response is atomically published in
`response.json` and an authoritative `completed.json` before the harness returns.
Existing successful `process.json` plus `response.json` receipts remain readable.
Downstream state and document publication can be retried from these receipts.

| Interrupted state | Next `research-loop run` |
| --- | --- |
| Completed Agent receipt, controller bookkeeping interrupted | Reuses the result without another Agent call |
| Partial read-only selection, Plan or Review | Preserves the attempt and starts a new budgeted attempt |
| Persisted CLI process still alive | Stops to prevent overlapping attempts |
| Incomplete Execute or unknown external effects | Stops for explicit reconciliation |
| Prepared Review publication | Completes the same publication |
| Unaccepted read-only response rejected by pure output validation | Retains the rejected response and returns field errors to the same stage in a new budgeted attempt |
| Unclaimed script request rejected by explicit authored-source/entrypoint/syntax validation | Returns feedback to the same live Execute within its original Plan/deadline, without allocation |
| Response rejected by protected source, scope, identity, accounting or trusted-effect checks | Preserves the failure for explicit reconciliation |

An interrupted model generation does not resume at its last token. Partial JSON
and final-looking text without a completion receipt are evidence, not completed
results. Execute is never automatically repeated on this basis. A task adapter
validates existing external receipts through `Study.reconcile_execution`; the
controller records the recovery and publishes the verified completion in the
original Execute attempt. Raw Agent evidence is retained and reconciliation does
not count as another Agent invocation.

Parameter correction keeps the original task and frozen context. Each new Agent
call consumes the existing Study call/time budget; rejection does not reset either
budget or make an additional experiment. The correction policy catches only errors
from designated pure output validators. CLI failures, controller failures and
unknown effects remain blocking. An invalid Execute response cannot trigger a new
Execute invocation, even if its shape could be corrected.

`ParameterCorrection` owns only invocation → pure validation → rejection feedback.
Its caller supplies invocation, validation and rejection callbacks, and admits every
new call through the existing budgets. The original invocation's absolute timeout
is retained in `parameter-budget.json`; corrections cannot renew that window even
when an adapter supplies a per-call allowance. Rejected responses and bounded,
redacted field errors are retained in `rejection.json`. A successor's `feedback.json`
preserves the correction link before it becomes active, including across interruption.

An optional backend `validate_plan` hook checks input references before acceptance
through this same correction path, including cached unaccepted replies. Explicit
reference `ParameterError` is correctable; trusted-metadata Pydantic errors and
other infrastructure failures block. Accepted Plans remain frozen. The ProTok
hook reuses its local execution resolver without remote probes or data reads.

An adapter can explicitly certify a settled candidate-code rejection with
`RecoverableExecutionError(details=...)`. The controller retains that failure and
enters the adapter's `recover_execution` handler. Repair Agent calls go through
the ordinary call/time budgets and have separate Study-contained receipts. The
handler's final response is independently validated before normal Review. Every
new repair admission refreshes the persisted Study budgets and leaves one call
for independent Review; it does not extend the original wall deadline or guarantee
Review completion time.
Resource limits and task-specific debug checks belong to the adapter; the generic
controller never infers recoverability from an error string or job failure alone.

`DebugBudget` persists one investigation's allocation reservations and actual GPU
charges. Restarts reopen the same scope. Unknown submissions or missing terminal
accounting block new allocation; consumed seconds cannot be refunded or reset.

`research-loop retry STUDY` retries read-only phases or reuses a completed Execute
receipt. An unfinished Execute requires the trusted `Study.reconcile_execution`
API; a bare retry cannot certify that external effects never happened. A fresh
attempt requires the original process to be stopped. Replaying an unaccepted
Execute through a custom harness invokes its
`validate_completed(result, workspace=..., run_dir=...)` hook before acceptance.

A completed Review may stop research while the goal remains `not_met` or
`undetermined`, for example while waiting for further operator direction.
After an explicit request to continue, `research-loop resume STUDY --reason TEXT`
records that direction in `state.json` and makes the Study ready. It calls no model
and resets no counters. Resume requires the published Review stop and remaining
original iteration/call/time budget; it refuses completed goals, other stop causes,
partial execution and active/debug attempts. Follow with the appropriate bounded
runner; adapter resource leases and STOP controls remain authoritative. Route
statuses remain as reviewed. Resume does not change the goal or evaluation.

```sh
uv run research-loop run /path/to/study
uv run research-loop status /path/to/study
```

Call counts and completed outputs survive restart. A hard process kill can lose
the most recent unsaved elapsed-time interval; `max_seconds` is not an exact
crash-time meter. Task hosts requiring an absolute deadline enforce and persist
that deadline independently. The ProTok adapter retains its existing absolute
lease and GPU accounting across recovery.

Backend preparation, start and retained-execution inspection are specified in the
[execution backend guide](execution-backends.md). Preparation is shared with the
dry-run API; only explicit, legally repairable authoring errors return feedback.

## Controller-owned recovery evidence

`Study.reconcile_execution(recovery_id=..., certify=...)` acquires the Study lock
and calls trusted controller code with a copy of the current state. The callback
validates backend identity, effects, authorization, deadline, accounting and STOP
policy and returns a bounded certificate; it must not mutate Study state or launch
computation. This is a Python controller API, not an Agent tool or a CLI accepting
unverified JSON. See the [certificate contract](execution-backends.md#recovery-certificates).

The controller binds the original Study, iteration, Execute attempt and frozen
Plan, persists an immutable recovery record, and applies one of these actions:

- `retry`: only positive `never_submitted` evidence permits retirement of an
  unaccepted attempt. Existing submission interfaces are archived before clearing
  the Execute pointer; the lock inode remains in place. The next invocation uses
  a new attempt under the same Plan and remaining budgets. Its original Execute
  absolute deadline in `parameter-budget.json` is retained; missing, malformed or
  expired deadline evidence cannot authorize another call.
- `settle`: verified terminal effects, or proven non-submission, produce a
  completion receipt in the original attempt. This authorizes no new allocation.
- `prepare_continuation`: a published unmeasured incomplete Execute with proven
  non-submission is prepared for the existing append-only continuation path.
  The original publication and Plan remain unchanged. This existing new-iteration
  path has a new per-call timeout while the original Study budgets and backend
  absolute resource lease remain authoritative.

`recoveries/<recovery_id>.json` retains the transition. Reusing the same ID must
agree with its original certificate and state; it cannot name another operation.
A crash after record publication replays only the retained transition. The prepared
state retains a reference to its canonical recovery evidence; missing or changed
evidence blocks further execution. Proof references point to retained backend evidence; they are not evidence by themselves.
No missing job ID, absent queue entry, error string or `accepted` controller request
establishes an external computation outcome.

Subsequent Execute and Review contexts automatically include `execution_recovery`
for the original Plan lineage, including same-iteration retry and continuation.
The projection contains only bounded summaries, identities and evidence references,
not private authorization/configuration snapshots. A maximum of 32 records, 4096
UTF-8 bytes per summary and 16 proof references per certificate bounds the prompt;
overflow blocks rather than silently dropping history. Independent Review still
judges evidence and may stop for unresolved limitations. Existing Review attempts
are never rewritten, and arbitrary historical private records are not imported.

Run the real local Study/Git example with scripted responses and no compute service:

```sh
uv run python examples/recovery_demo.py --output /tmp/new-recovery-demo
```

It retains an accepted first request, certifies failure before computation, retries
once and verifies that the actual Review context and prompt contain the proof.
This demonstrates wiring, not model judgment or a live scheduler's behavior.
