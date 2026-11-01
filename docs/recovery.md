# Interruption and recovery

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
| Completed response rejected by schema, scope or trusted adapter checks | Preserves the failure for explicit correction |

An interrupted model generation does not resume at its last token. Partial JSON
and final-looking text without a completion receipt are evidence, not completed
results. Execute is never automatically repeated on this basis. A task adapter
may validate existing external receipts and create a separately labeled operator
reconciliation attempt; it must preserve the original Agent attempt.

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
