# ProTok adapter

For SQLite migration, `migration.py` supplies a pure explicit translator and
bound cleanup for imported operations; it does not launch training or cancel jobs.
The [migration guide](../../docs/runtime-migration.md) covers captured control
annexes and operator evidence. The [native application](../native_runtime/README.md)
can compose this cleanup with tool-free Pi roles; it is not the legacy runner.
The translator requires complete captured historical script coverage and binds
accounting to original jobs and authorization. A nested `original_job`
no-submission assertion must retain the exact captured job and original grant;
it remains an attributed operator assertion, not a new retirement proof.
Cleanup configuration validates frozen bindings without filesystem I/O before
admission; actual receipt observations remain separate.

New Studies use the version-3 Plan: scientific intent is frozen in `markdown`,
`timeout_seconds` bounds Execute, and optional top-level `execution` supplies
script inputs/outputs and allocation bounds. Inputs use `name` and trusted `source`,
without a `role` label. See [research execution](research-execution.md). Existing
version-1/2 Studies and their deployed runtimes are not migrated by this change.

The controller environment requires the root project's Pydantic v2 dependency;
provision it with `uv sync --locked` before using this runtime. Agent reports,
research requests and presentation outputs use models for CLI schemas and local
validation. Actual leases, job effects, source identity and accounting remain
controller-owned. Installing the dependency does not upgrade an active service.

This directory contains the ProTok task boundary: fixed candidate files,
evaluation protocol, backend receipts, GPU accounting, ownership lease,
supervisor and deployment. The reusable `research_loop` package has no ProTok
imports, paths, metrics or scientific prompts.

- `backend.py` validates candidate identity and fixed evaluation receipts.
- `continuation.py` runs the protected evaluator with the existing lease and
  accounting; it also executes directly under Python isolated mode.
- `run.py` bridges one investigation and publishes the reviewed best candidate.
- `server.py` drives the bounded lease and admits asynchronous Scout during normal
  research phases, including script investigations and work without a submitted
  GPU job. Scout defaults to one publication and five minutes between attempts;
  its independent lock, deadline, STOP and HANDOFF gates remain enforced.
- `deploy_runtime.py` switches a separate committed runtime at a published HANDOFF.

This is a task-specific adapter reference, not a self-contained benchmark.
It requires a separately provisioned ProTok repository, protected evaluator,
canonical data and an authorized resource lease. Private research inputs,
experiment reports and deployment snapshots are not distributed here.
See [Deployment configuration](deployment.md) for the required settings.

The [read-only dashboard](dashboard.md#阅读方式) reads generic Study objective
names, units, optimization directions and published Frontiers. Its source/protocol
selector keeps historical evaluations separate; generic Studies require neither a
ProTok project nor an accounting export. ProTok accounting and fixed long-training
views appear only for the applicable adapter. The dashboard places the current Frontier's code
ancestry on a horizontal mainline and branches other tasks from their actual
parents. It offers a fitted overview and zoomable local view. Hover or focus a
node to preview its task card; select it to update the progress and evidence
panel below. Touch users tap a node, then use the card's details button to jump
to the panel. Non-overlapping side branches reuse tracks without implying ancestry.

## Tagged live observations

The dashboard's TensorBoard link scopes run lists, metric tags and curve responses
to the selected task, independently in each browser tab. An explicit all-task
view remains available. See the [TensorBoard setup and scope contract](dashboard.md#启用-tensorboard).

Research scripts use `research_loop_events.emit` with optional `dashboard` tags.
The controller incrementally receives all tags during owned allocations; the live
panel filters presentation while Review retains an unfiltered frozen prefix.
Long training emits structured progress and the protected short trainer is observed
through its existing CSV/JSON outputs. See [Tagged live observations](../../docs/telemetry.md)
for the API, synchronization states and evidence boundaries.

## Parallel long training

Reviewed short candidates may be nominated into an independently authorized lane:
fresh seed42, 10k updates, full4491 validation every2k, CA RMSD/TM-score/CA-lDDT,
and low-priority `acd_u`. The child reuses Study Plan → Execute → Review, retains
remote checkpoints, and returns reviewed evidence to the parent at iteration
boundaries. Configuration, scheduling, knowledge, dashboard and checkpoint
interfaces are specified in [Parallel long training](../../docs/long-training.md).

Automatic and manual children have independent asynchronous workers and keyed
runner/recovery locks. A blocked child retains its attempts and costs while other
eligible children continue. Manual tasks also have independent allocation locks;
automatic children share one allocation slot and must wait for settled accounting.
Their Plan/Review calls can overlap. See [task dispatch and recovery](../../docs/task-queue.md).

## Recovery

Evaluation submission uses the reusable `research_loop.submission` API:

```sh
uv run python -m research_loop.submission submit --workspace WORKSPACE --action evaluate
uv run python -m research_loop.submission status --workspace WORKSPACE
```

The runtime supplies the equivalent absolute command to every Agent stage.
Only this API writes the request, staging file and lock; the controller atomically
claims it into a retained `evaluation-submission.json`. Repeated calls return the
same pending, accepted or terminal result. `accepted` means claimed, not GPU-started.
A crash after claiming needs reconciliation; it never permits a second submission.
Before Plan acceptance, script input references use the backend's existing local
resolver. Unknown/unsupported/future or genuinely unavailable references return
feedback to the same Plan stage within its original task, deadline and budgets.
Published-chain corruption and trusted identity/accounting/I/O failures block.
This check reads local control metadata only; remote existence remains a later
preparation check. Accepted Plans are never replaced by this feedback path.

Before claim, an inconsistent action or a script's missing entrypoint, authored
source-policy violation or Python syntax error returns `parameter_error` through
`status`, with retained feedback. The same live Agent may correct those authored
files/action within its original Execute timeout and frozen Plan. Preparation also
checks trusted input metadata without allocating compute. The controller keeps
rejected requests in `evaluation-parameter-errors.json`; protected source, frozen
Plan, pipeline, identity and accounting failures remain blocking. No job is
replayed. The [backend guide](../../docs/execution-backends.md) describes the
prepare/start/inspect contract and native dry-run API.
The controller clears unchanged parent-checkpoint interfaces once before Execute,
and retains all current-iteration submission evidence on reentry.

For fixed evaluation, an unsubmitted infrastructure interruption preserves the current investigation,
accepted scientific Plan and candidate workspace. It does not enter Review as a
scientific rejection. Script executions use the settlement command below and
cannot use this replay path. After explicit authorization and reconciliation:

```sh
uv run python integrations/protok/run.py --root RUN_ROOT --project PROJECT --resume-unsubmitted --authorization 'User authorized repair and continuation'
uv run python integrations/protok/deploy_runtime.py --root RUN_ROOT --runtime RUNTIME --resume-submission
```

A formal allocation with a confirmed Slurm `TIMEOUT`, frozen candidate identity
and fully settled accounting completes Execute as `incomplete`, with retained
failure evidence and no measurements. Independent Review records actual cost and
fixed-budget feasibility, then decides the next investigation. This path neither
repairs the candidate nor resubmits the timed-out job. Unknown effects, incomplete
accounting, other uncertified failures, user STOP and exhausted authorization
remain blocking.

An interrupted Execute can be closed from its trusted execution evidence with
one controller command:

```sh
uv run python integrations/protok/run.py --root RUN_ROOT --project PROJECT --reconcile-execute --authorization 'User authorized execution reconciliation'
uv run python integrations/protok/deploy_runtime.py --root RUN_ROOT --runtime RUNTIME --resume-execution
```

Reconciliation preserves the original Execute attempt and Agent files, accepted
Plan, usage and STOP. It publishes a controller receipt without another Agent
call or allocation. Completed and settled failed work reuse their verified
receipts; a fresh script execution may close without a job only when its durable
execution journal proves submission was never reached. Unknown effects block.
Closure is not permission to retry compute or extend any deadline. Deployment
requires the matching HANDOFF, unchanged contract and system-attention STOP, and
all runtime lanes idle. A user STOP remains blocking. Old incident-specific
reconciliation commands and historical-state migration are not supported.

Both commands use `Study.reconcile_execution` for normalized recovery records,
state transitions and completion publication. The adapter retains task-specific
identity, authorization and accounting checks. Core automatically includes the
recovery summary and proof references in subsequent Execute/Review contexts for
the same Plan, including same-iteration retry and published continuation. Full
private reconciliation snapshots are not embedded in prompts. Historical private
recovery records require separate verified handling; they are not auto-imported.

Preparation retains STOP and immutable per-attempt evidence. Deployment checks
the matching preparation and HANDOFF. An unpublished Execute continues in the
same iteration with only its unaccepted call pointer retired. An already published
incomplete investigation receives an append-only execution continuation: its
original scientific Plan and modified candidate checkpoint remain authoritative;
the execution directive is recorded separately. Neither path samples another
proposal, creates a Planner call, or resets usage/budgets/deadlines. Pending,
accepted or unknown formal/debug/research effects block resubmission.

Only a controller-confirmed syntax or candidate JSON error qualifies for a local
candidate rejection. Valid or unverified source with no submission is retained as
an interruption. Failure classification and explicit recovery validation inspect
the three already-read candidate source files (about 18 KB for the current
candidate; elapsed time unmeasured). They read no dataset or checkpoint and add no
normal execution startup work. Published continuation refuses demonstrated static
faults; same-iteration Execute may still repair its interrupted partial source.

Run status with the correct project path from the existing ownership record:

```sh
uv run python integrations/protok/run.py --root RUN_ROOT --project PROJECT --status
uv run python integrations/protok/run.py --root RUN_ROOT --project PROJECT --run-one
```

A writable Agent delegates into `attempt/agent/`; the outer completion receipt
is published only after fixed-result/source validation. Replaying an older raw
Agent receipt revalidates the same trusted job. Partial Execute remains stopped.

After independent Review, an interrupted control publication can be completed
without repeating evaluation:

```sh
uv run python integrations/protok/run.py --root RUN_ROOT --project PROJECT --repair-completed-control
```

This command finishes retained Review/control publication. It does not clear STOP,
reset costs or extend the lease. Execution reconciliation above likewise preserves
partial Agent evidence and requires authoritative backend evidence.

## Bounded candidate debugging

The host may explicitly authorize `control/debug-policy.json`:

```json
{"schema": 1, "gpu_seconds_per_investigation": 1800,
 "independent_of_formal_budget": true, "authorization": "user authorization"}
```

Each failed investigation and all its continuous repair attempts share one
`control/debug/ITERATION/debug-budget.json`. Charges are actual single-GPU Slurm
allocation seconds, including setup, compilation, failed checks and cancellation.
Debug charges are separate from the original formal allowance; physical totals
include both. Historical formal failures retain their original charges.

A known, fully accounted candidate-code failure enters repair under the same
scientific Plan. Repair calls can edit only the existing three candidate files.
The controller freezes each revision, runs the pinned unscored three-update smoke
and gates, and uses failure evidence for the next bounded repair. Every allocation
fits the remaining debug allowance and the original wall deadline. Slurm rounds
limits to minutes, so requests round down; a residual below 60 seconds ends debug.
Debug applies the formal preflight's real-update, native math-gate and compiled
parity checks using canonical inputs. The setup-only legacy baseline equality
suite does not constrain new architectures. No setup-test pass is synthesized;
formal evaluation still stages its inputs and repeats its own preflight.

Passing debug triggers a fresh full 30-minute formal allocation, including when
the source fingerprint already has a historical record. Its explicit attempt ID
uses a separate record under the original formal ledger. It retains the fixed
initialization, training, scorer and gates; it cannot import a cached score or
continue a debug checkpoint. The original failed records remain intact.

Exhausted or unsuccessful certified recovery produces incomplete Execute with
no measurements and proceeds to independent Review. Unknown effects,
infrastructure failures, missing accounting and overspend remain blocking.
Each new repair uses fresh Study call/time budgets and reserves one call for
independent Review. The original ownership and absolute wall deadline continue
to apply; time for Review completion is not guaranteed. Research scripts retain
their single-allocation policy and do not use these formal debug grants.

For a failure retained by an older runtime, `--begin-debug` certifies and archives
the original failure/interface, prepares the recovery state, and retains STOP.
An explicit `deploy_runtime.py --resume-debug` switch verifies that preparation
and the unchanged STOP before starting the new runtime.

## Runtime upgrade

Export a tested Git commit into a new runtime directory and save its full revision
in `SOURCE_COMMIT`. Request `control/HANDOFF` with `Upgrade to REVISION` and a
trailing newline. The supervisor finishes its current investigation and exits.
`deploy_runtime.py --root RUN_ROOT --runtime RUNTIME` requires an inactive
service, matching handoff request, fully published Review/control records, no
outstanding effects, and exclusive supervisor/runner locks. It preserves the
ownership and budget files and records the previous service configuration.

## Supervisor state

Settled timeouts and failed research scripts enter independent Review as incomplete
experiments with real resource charges and no invented measurement. The supervisor
keeps admission/effect failures observable in `control/supervisor-status.json`,
with bounded polling and no automatic Execute replay. Identical blocked polls are
counted by scope, target, stage and error in the persistent `failures` list.
After five observations the affected object is marked `needs_attention` and its
automatic operation retries stop. The scheduler keeps discovering independent
tasks; parent Execute attention does not revoke independently authorized children
with frozen inputs. Read-only accounting/readiness polling may detect reconciliation
but never replays Execute or changes attempts, frozen inputs or budget reservations.
Only success of the corresponding operation resets its counter; earlier successful
checks do not. Counts, first errors and diagnostics survive supervisor restart.
Only core-authorized read-only calls retry within persisted call/time budgets.
Shared ownership/authorization/control invalidity publishes `control/STOP` **before**
joining workers, writes the final summary and exits 2 (`global_attention`). Local
attention alone neither writes STOP nor causes a global nonzero exit. The short lease deadline is an
entry cutoff: a durably admitted investigation may finish after it, while no new
investigation may start. User STOP cancels owned work; handoff drains the active
investigation. Short and long GPU allowances remain independent.

Inspection and cleanup failures are recorded separately. `inspect()` and final
summaries distinguish `known`, `partial` and unavailable (`unknown`) accounting.
A faulty manual receipt is attributed using its original job, not its claimed
receipt task ID; only that task's grant becomes unknown. An unverifiable task
identity blocks all manual grants that cannot be excluded. Contradictory pool
identity blocks both long pools. An unknown automatic allocation blocks the
shared automatic pool, not independent manual grants or safe Plan/Review calls.
Unknown affected subtotals and the overall total are `null`; `known_gpu_seconds`
is only a known subtotal, never spending authority or proof of unused allowance.
Outstanding entries and reservations remain until explicit reconciliation. The
read-only dashboard retains healthy subtotals and labels partial accounting;
inspection success never means all accounting is known. No reader substitutes
zero or stale usage. These changes do not alter training/evaluation semantics.

### Initialization and readiness

Initialization is resumable and publishes only complete records. A retained
`baseline/` is reused only when it is clean at the acknowledged commit; the clone
and the seeded Study (`initialize`, historical narrative and three initial routes)
are staged under `.initializing/` and renamed into place. A nonblocking
`control/initialization.lock` serializes initializers. The retained
`.initializing/owner.json` identifies this adapter and the acknowledged baseline;
only its known unpublished `baseline/` and `study/` staging trees may be rebuilt.
Unknown nonempty directories (including older unmarked staging), published Studies
and mismatched baselines are preserved for explicit operator reconciliation.
Symlinks in these control/staging paths are refused, not followed for cleanup.
Run roots must remain private controller-owned directories; the lock coordinates
cooperative writers, not hostile concurrent filesystem mutation.

After all seeds exist, staging receives `study/protok-initialization.json` with
`version: 1`, `status: "complete"` and the frozen Git `baseline`. The Study is
validated before its final directory rename. Clone, core initialization, seed
write or rename interruptions leave no partially published Study; retry reuses
the validated baseline and rebuilds only owned unpublished staging. The supervisor
accepts an empty, nonsymlink `study/` as an initialization target and retries an
interrupted initializer up to five observations of the retained failure. At that
threshold it preserves the staging and reports parent-scoped attention while
independent authorized children remain live; it does not replay Execute or reset
budgets. A verified complete publication clears the initialization failure.
Nonempty unknown targets and symlinks still require reconciliation. This is
process-interruption recovery, not a power-loss/fsync durability guarantee.

Initialization, supervisor startup/polling and direct parent `--run-one` share
`run.ensure_study_ready(root)` / `initialization.ready(root)`. The marker is never
a substitute for current state: checks read the config, state, route/frontier
indexes and required research documents. At zero rounds the three initial seed
records must still exist. Without a marker, zero-round legacy Studies additionally
need their historical narrative; a core-only `study.json` and ready `state.json`
are incomplete and must not run. Already-running legacy Studies instead use the
current iteration metadata and its frozen config context as evidence, so retired
seed routes and evolving research remain valid. Readiness never writes a marker
to certify legacy data, resets budgets/attempts, or rewrites notes/routes.

These checks read a fixed set of local controller files only: JSON records are
limited to 4 MiB each, research documents to their first 128 characters, and only
the current iteration is inspected. There is no history-tree, dataset, checkpoint
or workspace scan. They check initialization, not scientific correctness or
backend authorization/accounting; normal per-task recovery and independent budget
isolation still apply. Missing/corrupt retained evidence requires reconciliation,
not deleting the published Study or inventing a completion record.

## Checks

```sh
PYTHONPATH=src:tests:. uv run python -m unittest discover -s integrations/protok/tests -v
```
