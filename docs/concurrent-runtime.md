# Concurrent runtime

The concurrent coordinator runs on new, isolated SQLite schema 7 Studies through
Python APIs and the runtime CLI. Older prototype databases are rejected; there is
no automatic migration. The existing serial CLI is unchanged. Explicit migration
and deployment remain phase 8.

A single owner holds the Study coordinator lock and drives finite scheduling
steps. Local worker processes receive detached JSON and publish durable results;
the owner validates and accepts them. For direct Python use, configure trusted role validators for
`plan`, `execute`, and `review`, plus `advocate` and `judge` when planning is
enabled. A Scout requires its own validator and an explicit `request_scout`.
Validators must enforce the application's actual contracts; identity validators
in synthetic examples do not establish scientific validity.

## CLI and trusted application

Use `research-loop runtime --help` and each subcommand's `--help` for arguments.
The bundled skill launcher accepts the same `runtime` arguments. Initialize a new
empty Study directory with an explicit JSON configuration and trusted application:

```sh
research-loop runtime init /path/to/study --config /path/to/config.json \
  --application /path/to/versioned/application.py \
  --application-config /path/to/private-application-config.json \
  --max-iterations 10 --max-calls 40 --max-seconds 3600
research-loop runtime run /path/to/study
research-loop runtime status /path/to/study
```

`--application-config` is optional. `--max-iterations` and `--max-calls` accept a
nonnegative integer or `none`; `--max-seconds` is required, and
`--admission-deadline` is optional. Configuration requires a nonblank `goal`,
explicit object `permissions`, and the runtime fields below. Initialization imports
the application but invokes no callbacks. Keep private application configuration
in the user's Study environment, outside Git.

The explicitly trusted application exports `APPLICATION_IDENTITY` with nonblank
`id` and `version` matching `runtime.worker_adapter`,
`invoke(kind, payload, config)`, and `validate(role, output, context, config)`.
The validator returns a normalized object; no default identity validators are
provided. Module imports and validators must be pure and make no external calls.
Callbacks must keep local children in their process group and must not spawn
untracked submitters. Use a versioned immutable module path outside the Study
control directory and installed skill. Initialization freezes its absolute path,
constant-time file metadata and application configuration; loading rejects changed
source metadata. This is not a cryptographic freeze: dependencies and the
execution environment remain part of the trusted version contract. The legacy
Harness creates its own session and is not automatically integrated; its role
schemas and scientific contract differ.

`runtime run` drives deterministic ticks until idle; `--max-ticks` bounds ticks,
and `--watch` keeps the owner available while idle. Polling alone makes no LLM
calls. Idle does not mean the Study goal is complete. Ctrl-C or SIGTERM retains
intents, holds and running callbacks for later reconciliation; neither requests
nor clears STOP.

Mutating commands `task create`, `task edit`, `task cancel`, `stop`, `scout`,
`plan`, `retry-review`, `cancel-operation`, `retry-action`, and `ack-event` submit
durable inbox intents. A command attempts owner drain if ownership is free;
otherwise the request remains queued. Only a receipt with `status: applied`
means the command took effect. Scout and Review retry requests stay `pending`
without reserving calls until the actual running coordinator admits them.
`request-status STUDY REQUEST_ID` distinguishes pending/applied/rejected receipts
from `not_recorded`, which can still mean queued. Reuse a request ID only for the
same intent; conflicting reuse is rejected. Concurrent clients have no global
FIFO ordering; use expected revisions and task dependencies for ordering.

`status` and `dashboard` are read-only and acquire no owner or task code; they do
not expose raw transcripts. Dashboard serves GET only on `127.0.0.1` by default.
Its status keeps fees, physical resource release, callback state, Review and
publication as distinct facts. `events STUDY --consumer NAME` and `ack-event`
provide durable outbox consumption and acknowledgement; no agent wakeup consumer
or automatic follow-up is installed.

Run the synthetic CLI demonstration with:

```sh
uv run python examples/runtime_cli.py
```

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

`runtime stop STUDY --reason REASON --cancel-running` queues STOP and cancellation
requests for original open operations. STOP is permanent; there is no resume API.
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
