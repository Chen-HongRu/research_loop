# Task pool and planning

This is stage 4 of [#77](https://github.com/Chen-HongRu/research_loop/issues/77),
available on new SQLite Studies through Python APIs. The existing serial task CLI is unchanged. Current SQLite
schema 8 rejects previous prototype databases. The [backend lifecycle APIs](backend-lifecycle.md)
provide operation actions after accepted Execute requests. The [concurrent
coordinator](concurrent-runtime.md) dispatches work through Python APIs; the SQLite CLI and dashboard are available. [Explicit migration](runtime-migration.md) and deployment require operator evidence.
No automatic migration, deployment or new compute authority is introduced.

## Tasks and ownership

```python
from research_loop.runtime.task_pool import TaskPool

pool = TaskPool(owner)
receipt = pool.create_human(
    "operator-check", {"instruction": "Inspect the specified condition"},
    request_id="operator-request-1", priority=100, dependencies=[],
)
view = pool.snapshot()
iteration = owner.claim_next()
```

The trusted controller/human entry point owns `create_human`, `edit_human`, and
`cancel_human`. Workers receive a call identity and immutable context package,
never the coordinator capability. These Python APIs are not a sandbox against
arbitrary Python with direct database access.

`edit_human(id, expected_revision, *, request_id, specification, priority,
dependencies)` replaces a pending request with an exact new revision.
`cancel_human(id, expected_revision, *, request_id)` appends a cancellation.
Exact request-ID replay returns the original receipt, even after claiming or
restart; different intent under that ID conflicts. Human intervention promotes a
pending autonomous task to `source="human"`. Ownership never returns to Judge;
immutable history retains the original autonomous provenance.

Task IDs use 1–64 letters, digits, underscores or hyphens, starting with a letter
or digit. Task specifications are finite JSON objects. Current records include
identity, revision, source, specification, integer priority, dependencies and
cancellation. Dependencies reference existing task IDs; cycles are rejected.
Cancellation permanently reserves the ID. Once an iteration exists, all task
edits and cancellation are rejected, including after terminal execution. A new
investigation uses a new task identity.

`claim_next()` selects a dependency-ready task and reserves its iteration budget
atomically. Human ownership is a separate priority tier; even an autonomous
task with a lower numeric priority cannot outrank ready human work. Within a tier,
smaller numeric priority comes first, then stable ID. A blocked human task does
not prevent an unrelated ready task from proceeding. Claims do not preempt jobs.
Direct `claim_task(id, expected_revision)` enforces the same ordering; an exact
already-claimed replay returns its original iteration without another charge.

`pool.snapshot()` returns `{version, tasks}` from one read transaction. Each task
includes `status` (`pending`, `claimed`, `cancelled`), its `iteration` or null,
`ready`, and `blocked_reasons`. Dependency reasons distinguish cancelled, failed,
unclaimed and unpublished prerequisites. Inspection changes no counters, events
or admission state. The planning snapshot uses the underlying immutable task
envelopes plus separate iteration records.

The optional Study config field `task_pool.max_pending` sets the number of
unclaimed, uncancelled tasks the pool can hold, including dependency-blocked
tasks. An omitted field or `null` means unlimited. Zero blocks new tasks.
Claiming or cancelling a pending task frees a slot. The cap applies in the same
transaction as creation; Judge records a per-change `max_pending` conflict
and continues its ordered batch. Migration retains all imported tasks.
`runtime status` reports `task_pool.pending`, `ready` and `max_pending`.
Worker slots govern concurrent callbacks; resource pools govern external jobs.

## Judge changes

Judge receives the protected frozen pool and in-flight iteration metadata. Its
normalized output contains exactly one `changes` list:

```json
{
  "changes": [
    {"action": "create", "id": "proposal-1", "specification": {"instruction": "Synthetic investigation"}, "priority": 100, "dependencies": []},
    {"action": "update", "id": "pending-2", "expected_revision": 1, "specification": {"instruction": "Revised autonomous request"}, "priority": 50, "dependencies": []},
    {"action": "cancel", "id": "pending-3", "expected_revision": 2}
  ]
}
```

Each action has exactly the shown fields. Source/ownership is never an Agent
field. Creates are autonomous and require an unused ID. Update covers both
research requirements and ranking. A batch may target each ID only once.
Malformed actions, duplicate targets, unauthorized fields and invalid field types
are rejected before acceptance by `PlanningRunner`, in addition to the explicitly
supplied scientific/policy validator.

`TaskPool.apply_judge(call_identity)` applies an accepted context-bound Judge
from its reserved planning slot. Each update/cancellation requires a target in
that exact protected snapshot, `expected_revision == shown_revision == current_revision`,
current autonomous ownership, and no claim or cancellation. Guessing a future
revision or editing a later human request cannot bypass these checks. Changes are
processed in list order; newly created earlier dependencies may be used later in
the batch. Dependency and target conflicts are recorded per entry, while unrelated
valid entries commit. Human requests with invalid dependencies fail atomically.

The durable receipt is `{attempt_id, applied: [{id, revision}], conflicts:
[{index, id, reason}]}`. Exact batch replay returns it without new revisions,
model calls or output artifacts. The captured pool, Advocate results, bound
control context and accepted evidence references are checked before applying or
replaying. The optional generic Publisher/Frontier projection retains its
captured version. Native Study libraries remain live and are not included in
the frozen pool or bound to a task knowledge baseline.
An accepted but unapplied Judge from an explicitly failed round cannot later
change the pool. Low-level manual dispatch must validate the same canonical
change envelope before accepting Judge output; use `PlanningRunner` for this
composition and deterministic settlement behavior.

## Events and one planning round

```python
from research_loop.runtime.planning import PlanningRunner
from research_loop.runtime.planning_state import PlanningLedger

ledger = PlanningLedger(owner)
ledger.trigger("opportunity-1", "pool_low", {"reason": "New research opportunity"})
runner = PlanningRunner(owner, invoke, validators, policy=context_policy)
result = runner.run_once()
```

`invoke(call_identity, context_package)` returns exactly
`{"output": object, "evidence_refs": [immutable_reference, ...]}`.
Explicit callable validators for `advocate` and `judge` are required. They run on
the coordinator thread, outside write transactions. The trusted invocation adapter
owns bounded process execution, timeout enforcement and authorized tool access.
The runner supplies no OS isolation or generic cancellation of arbitrary Python;
a callback exception does not prove that remote work stopped.

Event producer keys are unique: identical key/kind/payload replay is idempotent,
while changed intent conflicts. Human mutations, new knowledge publications and
iteration failures emit events atomically with their state changes. The scheduler
may explicitly signal insufficient pending work using a stable key for that
observed condition. Judge's own pool writes do not emit planning events, preventing
self-triggered model loops. No automatic repeat training is involved.

`run_once()` captures the current event range, pool, in-flight metadata and
generic Publisher projection version. This does not capture a native library
snapshot. Native Advocates and Judge can read newly published files during
the same round. It admits exactly one active round and reserves both Advocate
calls atomically in the ordinary Study call ledger. Exhaustion during pair
reservation rolls back the entire admission, without refunding earlier calls.
Artifact construction and role calls occur outside write transactions.

Two Advocate callbacks run concurrently. One failure leaves the other result and
the existing pool intact. Once both are terminal and at least one is accepted,
Judge gets their frozen outputs or failure summaries and a separately budgeted
call. Both failing ends the round without Judge. STOP or exhausted call/time budget
before Judge reservation also ends that round; already reserved results may still
be recorded. Context failures before dispatch resolve those unstarted reservations
as failed and retain their call charges.

A round covers only its captured event high-water mark, on success or terminal
failure. Events arriving during work remain pending for a later explicit
`run_once()`. Idle calls return null and invoke no model. Failed rounds do not
create their own retry trigger or change existing tasks. Priority and dependencies
are checked by the deterministic claim path; resource release does not require a
new Judge call. Physical resource release and fee settlement are separate
backend transitions; a task's requested priority cannot grant or reset either
budget. Execute operation requests retain the configured authorization, while
Review waits for collected/resolved/sealed feedback rather than for billing
alone. Planning does not submit or resubmit an operation.

## Recovery and validation

Restart does not redispatch active calls. A prior-owner unresolved round remains
visible and blocks new planning rounds pending reconciliation; it does not cancel
or invalidate the existing task pool. The [concurrent coordinator](concurrent-runtime.md) reconciles exact old
completions. If Judge was already accepted, use
`runner.settle_accepted_judge(round_id)` to apply/finish that exact decision without
invoking or reserving another call, including after owner restart. A committed
batch must settle as applied; it cannot be relabeled as failed. Inputs, outputs,
revision history and original budget charges remain intact.

Task claim checks read scheduling/dependency metadata without parsing every task
specification. Model/payload reads, policy and role validation stay outside SQLite
write transactions. Evidence identity checks use metadata, not content hashes.
These controller operations read no dataset/checkpoint content and run neither
per training rank nor loader worker. Dependency checks scale with the relevant
task graph; planning capture scales with control and publication history. Timing
and memory have not been benchmarked.

```bash
uv run python examples/task_planning.py
```

This temporary smoke uses real SQLite and two overlapping local Advocate
subprocesses followed by one local Judge subprocess, all with deterministic
synthetic replies. It verifies three charged calls, zero additional idle calls,
human-first claiming and unchanged human requirements. It starts no model, GPU or
remote computation and establishes no scientific benefit.
