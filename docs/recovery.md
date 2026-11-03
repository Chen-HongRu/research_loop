# Interruption and recovery

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
may validate existing external receipts and publish a separately labeled controller
reconciliation receipt in the original Execute attempt. It must preserve the raw
Agent evidence and must not count reconciliation as another Agent invocation.

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

`research-loop retry STUDY` authorizes a fresh attempt after effects have been
checked. It preserves completed Execute receipts and refuses an active local
process. Replaying an unaccepted Execute through a custom harness invokes its
`validate_completed(result, workspace=..., run_dir=...)` hook before acceptance.

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
