# Parallel long training

A parent Study uses short training to choose investigations. With an enabled
`long_training` policy, its Review also returns a `long_training_decision`: a
reason and zero or more nominations. Each nomination identifies the current
candidate, an earlier comparison candidate, its evidence and one basis:

- `significant_frontier_gain`: a practically substantial gain over an earlier
  Frontier member, with the new candidate on the global Frontier.
- `architecture_gain`: an improvement associated with a substantial change to
  representation, readout, conditioning, message computation or factorization.

Review judges scientific value, magnitude and uncertainty. The controller checks
that both candidates are valid, verified, complete and comparable; the new result
must improve at least one objective without worsening another. A single seed or
Frontier transition does not establish statistical significance or causality.

Nominations refer to published candidate versions. The queue deduplicates the
artifact version, long-training recipe and evaluation definition. A correction
that withdraws the source's eligibility before allocation withdraws its queued
request. A failed or unknown Execute is not a new candidate or permission to submit
again.

## Independent studies

Each nomination creates `study/long-runs/<ID>/request.json` and an independent
child Study at `study/long-runs/<ID>/study/`. The selected candidate and recipe are
fixed. Ordinary Study control runs Plan → trusted Execute → independent Review,
with the same completion receipts, attempts, publication and recovery rules as a
short investigation. No further selection-model calls are needed for the fixed
candidate. Each lane handles one investigation at a time; short and long lanes
run concurrently.

Generic Study policy fields are `version: 1`, `recipe`, `evaluation`, `max_runs`
and `max_seconds_per_run`. The recipe contains `total_steps`,
`validation_interval`, `validation_split: "full"`, `initialization: "fresh"` and
`seed`. Long evaluation must be a feedback-enabled development protocol with a
different ID and comparison scope from the short evaluation. Use
`research-loop init --long-training POLICY.json` to freeze this policy. A task
adapter supplies the authorized executor and resource accounting.

Published child output is recorded in `long-runs/<ID>/result.json`, including the
independent Review, exact source, recipe, Execute, trajectory and evidence
references. Negative and incomplete settled results remain knowledge. A new child
Review returns one `observation_reviews` entry (`step`, `valid`, `verified`,
`reason`) for every complete retained validation point, including rejected points.
The review context freezes the exact observations used for acceptance and later
knowledge publication. Unknown external effects remain unresolved and block this
lane until reconciliation.

The parent freezes reviewed results into
`iterations/NNNN/context/long_training_results.json` at the next iteration
boundary. Selection, Plan and Review receive that immutable snapshot. The child
never overwrites parent research documents during an active Review. Parent Review
incorporates relevant conclusions into route/global research. The read-only
`research-loop knowledge STUDY` command combines current parent research with all
published child knowledge, including long runs completed after the final short
iteration. Long measurements stay in the child evaluation and never enter the
short Frontier.

## ProTok

The fixed long protocol is `se3-long-v1-10k-full4491-seed42`:

- Fresh seed42 initialization, 10,000 total optimizer steps.
- Original optimizer, loss, masks, data/noise seeds and 50k scheduler horizon.
- All 4,491 development proteins at steps 2k, 4k, 6k, 8k and 10k; Euler100.
- Canonical equal-protein CA RMSD, TM-score and CA-lDDT at every checkpoint,
  retaining per-protein CSV evidence remotely. Final endpoint metrics all refer
  to step10k. A best-over-curve value remains a separately identified observation.
- One H100 per allocation, Slurm partition `acd_u` (low priority), established
  assigned-GPU sandbox, read-only canonical inputs and independent output paths.

The protected 2k/subset trainer is unchanged. `long_train.py` reuses its frozen
training/evaluation helpers in an explicit 10k loop. All five validation
checkpoints remain at the unique remote run path. `long-curve.json` is atomically
published at initialization, every ten completed optimizer updates and validation
boundaries. It records actual progress and the last training observation; a full
validation point is appended only after checkpoint publication and complete
4491-protein evaluation. An interruption during8k validation therefore retains
complete2k/4k/6k points, while8k and10k remain missing.

After any settled termination, including TIMEOUT or cancellation, the controller
collects the two bounded completion/progress JSON records and at most64KiB each
from training CSV and stdout tails. It does not read datasets, per-protein tables
or checkpoint contents for validation. Missing/corrupt or source-mismatched
progress cannot supply accepted validation observations; collected logs and exact
errors remain evidence. Actual Slurm allocation charges are retained even when
they exceed the requested allocation.

An incomplete execution has `endpoint_complete: false`, lists missing validation
steps and returns no10k endpoint measurement. Its complete intermediate metrics
and training/log evidence still reach independent Review, parent knowledge and
the dashboard. Even when no full validation completes, available training progress
and logs remain visible. Final10k endpoint eligibility still requires full successful
execution; a timeout alone is not evidence that the scientific method is ineffective.
Performance research scripts must likewise publish a declared small JSON artifact
initially and atomically update completed subtests at useful boundaries. Only
persisted observations can be collected after process termination; unwritten memory
and interrupted measurements cannot be recovered or treated as complete.

Create an explicitly authorized `control/long-training-policy.json` before
enabling the lane. Its fields are:

| Field | Meaning |
| --- | --- |
| `schema` | `1` |
| `authorization` | Actual user/operator authorization for this separate grant |
| `gpu_seconds_per_run` | Full single-GPU allocation allowance, a positive multiple of60 |
| `total_gpu_seconds` | Total independent long-training allowance |
| `deadline` | Absolute Unix timestamp after which no new long child may enter |

Each new allocation must fit the remaining independent GPU allowance. Actual
`sacct` allocation seconds, including failed and cancelled work and any overrun,
are charged in `control/long-training-jobs/`; unknown effects do not release
budget. A child's fixed Plan specifies its Slurm GPU allocation in
top-level `timeout_seconds`, injected by the controller and omitted from the model's
Plan schema. Queueing does not consume that GPU allowance and has
no separate wall cutoff. An admitted child can finish and publish Review after
the entry deadline. `used_gpu_seconds` retains short/research grant accounting;
`long_training_gpu_seconds` reports the separate charge and `total_gpu_seconds`
includes short, research, debug and long training.

For a new Study the initialization reads this grant. For an existing Study, stop
the supervisor at a published HANDOFF boundary and enable the lane before
restarting the tested runtime:

```sh
uv run python integrations/protok/run.py --root RUN_ROOT --project PROJECT --enable-long-training
```

The supervisor dispatches each child asynchronously. A failed child is retained
for explicit handling while other eligible children progress; failures before
initialization are retained in its `scheduler-error.json`. Each child keeps its
own runner/recovery lock and serial Plan → Execute → Review. Automatic children
claim `max_runs` capacity before preparation; failed preparation releases its
unused slot. They share one allocation slot: unsettled costs block new allocations in that pool,
while settled children can finish Review alongside another child's work.
`--run-long-one` provides the same bounded manual operation, skipping retained
failures; add `--long-run-id ID` to explicitly advance an inspected child.
If short work ends first, the long lane can finish
eligible published nominations within its own entry deadline and allowance.
HANDOFF prevents new admission and drains all admitted children. User STOP/signals
cancel owned GPU work through the existing polling path. Runtime handoff refuses
unsettled long effects. A Slurm allocation is released when its batch exits.

After inspecting retained effects, `--recover-long-run --long-run-id ID`
reconciles only the same owned allocation, retains the original Execute attempt
and advances it to Review. It never resubmits an uncertain/prepared job or resets
the grant. STOP can remain set for reconciliation; Review then remains pending
until the parent is explicitly resumed. A read-only Review interruption can be
retried with ordinary `research-loop retry` against the child Study.
Then explicitly advance that same ID; recovery never selects another pending child.

The dashboard places automatic and manual long-training tasks in its horizontal
Work tree, with a fitted global relationship graph and node-triggered task cards.
Zoom or switch to the compact local view to inspect dense branches. Hover or select a node to preview its card; select the node to read nominations, status, reviewed curves, aligned
metrics, knowledge and source/checkpoint references below the tree. Long-training
measurements keep their independent protocol and do not enter the short-run Frontier.
Explanations use the parent's frozen evidence snapshot. A subsequent research
script may declare a model input such as
`long-checkpoint:L0123456789abcdef:6000`; a partial run requires a settled,
accounted allocation and independently published acceptance of that exact complete
checkpoint observation. The allocation need not reach10k. Reuse compares the
receipt/artifact to the observations frozen in the accepting Review; changing live
metrics after Review cannot reuse the old acceptance. The symbol resolves to the
original remote checkpoint; no checkpoint-content validation runs at startup.

## Verification boundary

Local tests exercise real Study/Git publication and boundary feedback, source and
receipt identity, allocation recovery, concurrent scheduling, all five
full-validation records, all three metric bindings, checkpoint resolution and
dashboard rendering. Remote H100 execution, cluster entitlement, sandbox mounts
and metric-library availability require a real authorized smoke before rollout.
No GPU run is implied by enabling source support or passing local tests.
