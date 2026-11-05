# Transactional runtime

This is the SQLite state foundation for [#77](https://github.com/Chen-HongRu/research_loop/issues/77).
SQLite schema 5 is available through Python APIs for new, isolated Studies.
The current `research-loop init/run` commands still use the serial document
controller. A new SQLite CLI, concurrent experiment coordinator and explicit
migration remain subsequent deliverables tracked in #77. The
[backend lifecycle](backend-lifecycle.md) provides `Operations` and `BackendDriver`
for explicit asynchronous operation transitions.
The [Context / Feedback interface](context-feedback.md) binds immutable inputs
and validates output before accepting context-bound attempts.
The [knowledge publication API](knowledge-publication.md) appends Scout/Review
records and projects research documents and Frontiers without replacing state.
The [task pool and planning APIs](task-planning.md) add protected human ownership,
pending revisions, dependency-ready claims and event-driven parallel Advocates.

## Authority and identities

`runtime.sqlite3` on the controller's local disk is the authoritative control
state. SQLite transactions contain only local bookkeeping. No model call, remote
request, training wait or artifact generation runs inside a transaction. Database
connections enable foreign keys, WAL and full synchronous commits.
SQLite owns the adjacent `-wal` and `-shm` sidecars; they belong to the database,
not to a second application state store.

| Identity | Scope |
| --- | --- |
| Study | Frozen configuration, authorization limits, counters and STOP |
| Task and revision | Research request, source, priority, input specification and dependencies |
| Iteration | One claim of one exact task revision, with its own phase and revision |
| Attempt | One reserved Agent invocation, with a request identity and worker token |
| Coordinator epoch | One host lock owner; fences results from earlier owners |

The configuration stored in SQLite is not editable through the state API.
Snapshots are detached JSON-serializable values. Modifying a snapshot or a
`state.json` file cannot modify the database. No whole-state replacement API is
provided. Knowledge publication and backend operations have independent append
transitions; neither may restore an old Study snapshot. Operations bind the
accepted Plan, Execute, backend/environment and authorization. Their trusted
receipts and resource/cost holds remain separate from Agent narratives.

## Ownership and transitions

`StateStore.coordinator()` holds the Study's `.lock` with a nonblocking exclusive
host lock. Acquiring ownership increments the persisted epoch. Every mutation
checks the live session, process identity and epoch in a short write transaction.
Closing or forking a session cannot transfer its write authority. Workers receive
attempt identities, not writable state handles.
Worker processes must use spawn/exec; the lock descriptor is closed on exec.
A fork-only child can retain the parent's open lock descriptor and block takeover
after the parent dies until that child exits. PID fencing prevents child writes
or explicit unlock, but does not remove this OS-level inheritance. The later
scheduler must not launch fork-only workers while holding ownership.

Claiming a pending task freezes its revision and creates one independent
iteration. Claim and iteration-budget reservation commit together. Dependencies
must have published results. Repeating the same claim returns the existing
iteration and does not admit a second investigation.

Pending tasks can be revised through the task pool; every revision is retained.
Claimed envelopes remain immutable even after their iteration finishes. Human
intervention promotes an autonomous pending task to protected human ownership.
`claim_next()` filters blocked/cancelled/claimed tasks, then orders ready human
tasks before autonomous tasks, with ascending numeric priority and stable ID
within each tier. Direct `claim_task()` obeys that same order for new claims.
The pool version advances on revisions, claims and iteration terminal outcomes.

Iteration phases are `plan → execute → review → publication`. Each phase permits
one active attempt. An accepted artifact advances only that iteration and its
revision. A failed attempt retains its reason and fails only its associated
iteration. Standalone Scout, Advocate and Judge attempts share the Study ledger
without acquiring an iteration. Publishing an accepted Review advances only its
iteration from `publication` to `done`, with status `published`.

An Execute may be accepted with a finite `operations` list before those external
operations complete. Request admission and the operation group commit with that
accepted Execute; its artifact list is never extended by later collection.
`BackendDriver` calls trusted adapters outside write transactions. Durable
submission intent precedes the callback, and an unknown outcome retains the same
identity and holds rather than authorizing resubmission. Physical release and
fee settlement are independent. Review requires terminal operation knowledge,
terminal collection, resolved evaluator intake and an explicitly sealed feedback
group; unresolved fees alone do not block it. Direct v1 Execute-artifact
measurements remain supported. See the [lifecycle contract](backend-lifecycle.md).

`begin_attempt` atomically reserves one call before returning its identity. A
repeated request ID must have the same intent and returns the original record,
including its original epoch; it is not permission to dispatch again. Call
reservations are not refunded on timeout, failure or restart.

`accept_attempt` checks the worker token, owner epoch, iteration revision and
phase before recording an artifact reference. An exact duplicate returns the
same acceptance; a changed result or stale revision is a conflict. Prior-owner
committed receipts can be replayed unchanged after restart. Unresolved attempts
from a prior owner cannot be accepted or replaced
without a later explicit reconciliation transition. This layer does not infer
whether a backend submitted work and does not retry Execute.

## Budgets and STOP

Every role uses the same Study call ledger unless a separate authorization is
explicitly introduced. Iteration and call limits may be `None`; the time limit
is finite and positive. Admission checks, counter increments and claims are one
transaction, so two contenders cannot both spend the final call.

Shared elapsed time counts the coordinator's active interval once, regardless of
the number of concurrent workers. The wall-clock anchor and consumed total are
persisted. Graceful close records the final interval. Its subsequent gap is
excluded only when no operation has `external_open=1`. Live or unknown submission
intent, and terminal work whose physical release is still unconfirmed, keep that
gap chargeable on reacquisition even after graceful close. A billing-only gap
after confirmed physical release is excluded when ownership was closed. After an
unclean exit the open owner interval, including downtime, is charged
conservatively. Backward clock movement never refunds time.
This is local wall accounting, not a guarantee about an administrator changing
the host clock.

The optional absolute `admission_deadline` prevents new iteration claims and
backend submission admission. It is
distinct from the shared time budget and each operation's frozen backend
deadline. Reopening ownership does not renew an operation deadline.
STOP is a persisted latch. STOP or exhausted admission budgets prevent new work,
but do not prevent recording results or failures for previously reserved attempts.
The foundation has no budget-reset, STOP-clear or automatic retry API.

## Files and format boundary

`publish_artifact(root, value)` writes a new read-only JSON artifact under
`artifacts/<uuid>.json`. The file and containing directory are synced before its
reference can be committed. Existing filenames cannot be overwritten. Unreferenced
files, including remnants of failed publication, are not accepted evidence.

Acceptance checks one file's path, type, permissions and identity metadata. It
does not reread or hash contents. These O(1) checks run once per attempted
acceptance in the controller, not in dataset loaders or training workers; they
read zero dataset/checkpoint bytes. They protect against accepting an incomplete,
mutable or substituted control record. Actual syscall latency is unmeasured.
The host controller and its filesystem remain trusted; file mode bits are not a
sandbox against a malicious process with the same filesystem authority.

New runtime initialization publishes `runtime.json` with `format: sqlite-v1` and
the database Study identity. The serial `Study` and `TaskQueue` reject either this
marker or `runtime.sqlite3`, including partially initialized directories. The
runtime refuses in-place creation over legacy `study.json` or `state.json`.
The current SQLite schema is 5; earlier prototype schemas are explicitly rejected.
Existing Studies require the later explicit migration procedure; creating a
database beside existing files is not migration. Old deployed binaries must be
stopped during that procedure; they cannot be controlled by a check they lack.

## API example

This records a synthetic Plan output. It does not execute an Agent or experiment.

```python
from pathlib import Path
from research_loop.runtime import initialize, publish_artifact

root = Path("/path/to/new-test-study")
store = initialize(root, config={"goal": "Synthetic state transition check"})
with store.coordinator() as owner:
    owner.create_task("T1", {"instruction": "Check a control transition"})
    iteration = owner.claim_task("T1", expected_revision=1)
    attempt = owner.begin_attempt(
        "plan", request_id="T1-plan-1", iteration_id=iteration["id"],
        expected_revision=iteration["revision"],
    )
    reference = publish_artifact(root, {"synthetic": True})
    owner.accept_attempt(
        attempt["id"], attempt["worker_token"], reference,
        expected_revision=iteration["revision"],
    )
```

This low-level example uses an unbound attempt. Real role calls use the
[Updater](context-feedback.md), which requires a bound context and an explicit
role validator. Raw acceptance cannot bypass an existing binding. Neither state
acceptance nor structural output validation proves a scientific claim. The
existing document controller retains its current scientific validators.
