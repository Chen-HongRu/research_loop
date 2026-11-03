# ProTok adapter

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

The [read-only dashboard](dashboard.md#阅读方式) places the current Frontier's code
ancestry on a horizontal mainline and branches other tasks from their actual
parents. It offers a fitted overview and zoomable local view. Hover or focus a
node to preview its task card; select it to update the progress and evidence
panel below. Touch users tap a node, then use the card's details button to jump
to the panel. Non-overlapping side branches reuse tracks without implying ancestry.

## Tagged live observations

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
The controller clears unchanged parent-checkpoint interfaces once before Execute,
and retains all current-iteration submission evidence on reentry.

An unsubmitted infrastructure interruption preserves the current investigation,
accepted scientific Plan and candidate workspace. It does not enter Review as a
scientific rejection. After explicit authorization and reconciliation:

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

For an older runtime stopped on this settled timeout, retain the existing failed
Agent attempt and prepare a separate controller reconciliation:

```sh
uv run python integrations/protok/run.py --root RUN_ROOT --project PROJECT --reconcile-timed-out-execute --authorization 'User authorized failure Review and continuation'
uv run python integrations/protok/deploy_runtime.py --root RUN_ROOT --runtime RUNTIME --resume-timeout
```

Preparation preserves STOP, the accepted Plan, candidate and original invocation
and charges. Deployment requires the matching committed-runtime HANDOFF and
unchanged prepared state, Plan, ownership, Study configuration and lease. Only the
retained system attention STOP may be cleared; a newer or user STOP blocks resume.
The original absolute deadline and all counters continue to apply.

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

After confirming there is no running local CLI or unknown backend effect,
operators can use retained evidence without launching another evaluation:

```sh
uv run python integrations/protok/run.py --root RUN_ROOT --project PROJECT --reconcile-completed-execute
uv run python integrations/protok/run.py --root RUN_ROOT --project PROJECT --repair-completed-control
```

The first command requires a fully completed, eligible, candidate-bound backend
record and creates a separately labeled operator attempt. It preserves the
interrupted Agent output and proceeds through independent Review. Known failed
wrapper receipts, changed source/configuration, unsettled accounting and unknown
effects block repair. The second finishes retained Review/control publication.
Neither command clears STOP, resets costs or extends the lease.

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
The original ownership and absolute wall deadline continue to apply.

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
with bounded polling and no automatic Execute replay. Only core-authorized read-only
calls retry within persisted call/time budgets. The short lease deadline is an
entry cutoff: a durably admitted investigation may finish after it, while no new
investigation may start. User STOP cancels owned work; handoff drains the active
investigation. Short and long GPU allowances remain independent.

Inspection and cleanup failures are recorded separately. Final summaries use
`null` for unavailable accounting; the read-only dashboard retains study records
and labels unavailable totals. Neither service substitutes zero or stale usage.

## Checks

```sh
PYTHONPATH=src:tests:. uv run python -m unittest discover -s integrations/protok/tests -v
```
