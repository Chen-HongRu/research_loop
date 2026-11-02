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
references. Negative and incomplete settled results remain knowledge. Unknown
external effects remain unresolved and block this lane until reconciliation.

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
checkpoints remain at the unique remote run path. Only small completion and
metric receipts are collected; datasets and checkpoints are not scanned, hashed
or copied for validation.

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
`contract.timeout_seconds`. Queueing does not consume that GPU allowance and has
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

The supervisor schedules the lane independently. `--run-long-one` provides the
same bounded manual operation. If short work ends first, the long lane can finish
eligible published nominations within its own entry deadline and allowance.
HANDOFF prevents new children and drains the current one. User STOP/signals
cancel owned GPU work through the existing polling path. Runtime handoff refuses
unsettled long effects. A Slurm allocation is released when its batch exits.

After inspecting retained effects, `--recover-long-run --long-run-id ID`
reconciles only the same owned allocation, retains the original Execute attempt
and advances it to Review. It never resubmits an uncertain/prepared job or resets
the grant. STOP can remain set for reconciliation; Review then remains pending
until the parent is explicitly resumed. A read-only Review interruption can be
retried with ordinary `research-loop retry` against the child Study.

The dashboard has a separate long-training panel for nominations, status,
reviewed curves, aligned metrics, knowledge and source/checkpoint references.
Explanations use the parent's frozen evidence snapshot. A subsequent research
script may declare a model input such as
`long-checkpoint:L0123456789abcdef:6000`; resolution requires a completed,
accounted allocation and an independently published accepting Review. The symbol
resolves to the original remote checkpoint; no checkpoint-content validation runs
at startup.

## Verification boundary

Local tests exercise real Study/Git publication and boundary feedback, source and
receipt identity, allocation recovery, concurrent scheduling, all five
full-validation records, all three metric bindings, checkpoint resolution and
dashboard rendering. Remote H100 execution, cluster entitlement, sandbox mounts
and metric-library availability require a real authorized smoke before rollout.
No GPU run is implied by enabling source support or passing local tests.
