# Concurrent runtime

The concurrent coordinator runs on new, isolated SQLite schema 6 Studies through
Python APIs. Older prototype databases are rejected; there is no automatic
migration. The existing serial CLI is unchanged. A new SQLite CLI, skill and
dashboard are phase 7; explicit migration is phase 8.

A single owner holds the Study coordinator lock and drives finite scheduling
steps. Local worker processes receive detached JSON and publish durable results;
the owner validates and accepts them. Configure trusted role validators for
`plan`, `execute`, and `review`, plus `advocate` and `judge` when planning is
enabled. A Scout requires its own validator and an explicit `request_scout`.
Validators must enforce the application's actual contracts; identity validators
in synthetic examples do not establish scientific validity.

## Configuration and use

The frozen `config.runtime` requires `worker_slots` (positive integer),
`worker_adapter` (nonblank `id` and `version`), positive finite
`call_timeout_seconds`, `action_timeout_seconds`, and
`inspection_interval_seconds`, nonnegative integer `action_retries`, and an
explicit boolean `planning`. Parallel planning needs at least two worker slots.
The invoker identity must equal the frozen adapter. Backend operations additionally
require the execution backend, resource pool and authorization configuration
described in [backend lifecycle](backend-lifecycle.md).

The executable synthetic example creates a Study, configures validators and a
`LocalProcessInvoker`, and drives the coordinator:

```sh
uv run python examples/concurrent_runtime.py
```

A trusted local Python module supplies `invoke(kind, payload, config)`. Agent
callbacks receive `kind == "agent"` and return an object with exactly `output`
and `evidence_refs`. Backend callbacks receive `kind == "backend"` and return
exactly `event_key` and `data`, following the relevant lifecycle receipt contract.
The invoker receives the Study root, module file path, adapter identity and a
JSON-object configuration. Callbacks are trusted: they must not spawn untracked
submitters or delegate a submission beyond the tracked callback's lifetime.

```python
from research_loop.runtime.coordinator import ConcurrentCoordinator
from research_loop.runtime.invocations import LocalProcessInvoker

# store, module_path, adapter, validators and callback_config are application inputs.
with store.coordinator() as owner:
    invoker = LocalProcessInvoker(store.root, module_path, adapter, callback_config)
    runner = ConcurrentCoordinator(owner, invoker, validators)
    result = runner.run_until_idle(max_ticks=100)
    print(result["waiting"], result["blocked"], result["next_wake"])
```

`tick()` and `run_until_idle()` are finite and do not sleep. The latter returns
on no immediate progress or its tick limit. Waiting for workers or external
backend actions is not Study completion. An embedding application must arrange
later ticks, including at `next_wake` where provided, and inspect blocked work.
`worker_slots` limits concurrent callback invocations; operation resource pools
independently limit external resources and remain held according to their lifecycle.
Pending planning retains enough callback capacity for its two Advocates before
refilling autonomous work. Human tasks retain priority. An unmanaged active
attempt remains visible as blocked and requires explicit reconciliation.

## Recovery and stopping

Dispatch intent and identity are durable. An unresolved sender is inspected or
retired, not redispatched; ambiguous submit outcomes do not trigger resubmission.
A new owner can reconcile the exact recorded completion of an earlier owner after
its worker retires, subject to identity and revision checks. Failed Review is
retried only by an explicit `DispatchLedger.retry_phase` request with its current
revision, reason, proof reference and request ID; the frozen Review input is
reused. This does not retry Execute or submit a new operation.
If positive `not_started` proof establishes that the failed Review never bound a
context, the retry selects its first context normally. A retry reserves a new
call and preserves the original attempt and budget charges.

Cleanup failures are retried at the configured inspection interval up to
`action_retries` additional calls after the first failure. Exhaustion emits a
durable attention event; `BackendScheduler.retry_action` explicitly authorizes
another bounded cleanup batch on the same operation. Submit is never retried.

`runner.stop(reason, cancel_running=True)` records STOP and requests cancellation
of open operations. STOP prevents new agent calls and prepare/submit launches,
while reconciliation, inspection, collection, settlement and requested
cancellation can continue. Local worker retirement is proof about the local
callback, not proof that an external job stopped; backend receipts determine
external state and resource release.

`RuntimeEvents(owner).pending(consumer)` and `.ack(consumer, event_id)` expose
a durable event outbox with per-consumer acknowledgements. No agent wakeup consumer
or automatic follow-up is installed: an embedding application must consume events
and arrange its own authorized response. See [transactional runtime](transactional-runtime.md),
[task planning](task-planning.md), and [knowledge publication](knowledge-publication.md)
for the underlying contracts.

The example and tests exercise real local processes with synthetic callbacks and
receipts. They do not validate scientific outcomes or a live external backend.
