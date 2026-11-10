# Coordinator-free experiment cycle

From the repository root, validate the sample without a model call:

```sh
uv run python -m benchmarks.experiment_cycle inspect benchmarks/experiment_cycle/example/case.json
```

Run the three specialist tasks and one sandboxed local process:

```sh
uv run python -m benchmarks.experiment_cycle run benchmarks/experiment_cycle/example/case.json \
  --output /path/to/private-cycle-run --allow-model
```

`--allow-model` explicitly enables the bounded subscription calls. The pinned
runtime must be installed and authenticated; the default model is `gpt-6.1-terra` at medium.
Each specialist has a 300-second sample limit; the CPU command has a 10-second
limit. These limits are time ceilings, not token or currency budgets. The sample
is synthetic and tests only this bounded numeric task, not general research value.
No GPU or remote compute is used. Do not run inside a sandbox that cannot start
the pinned CLI's filesystem sandbox; failure remains recorded, with no fallback
to unrestricted execution.

The deterministic flow is Plan → Prepare → Execute → Evaluate. Three fresh model
contexts hand off files without a coordinator, nested agents, live search or
shell network. Execute calls the prepared command under the same pinned Codex
permissions machinery. It receives the candidate workspace, not the private
checker. Plan and Evaluate are read-only; Prepare may write candidate code but
fixed inputs and host handoffs remain read-only. Prepare may compile the script
but must leave the substantive experiment to Execute.

The specialist prompt is supplied explicitly; `project_doc_max_bytes=0` prevents
ambient AGENTS.md discovery from changing the benchmark prompt or failing on
ancestor permissions. Keep live output directories outside a Git checkout to
avoid unrelated workspace-routing discovery.

Each run creates a new output directory; an existing one is never overwritten.
`report.json` and `report.md` show all four stages, token traffic, wall time,
correctness and source coverage. `tasks.json` identifies the exact model histories;
`stages.json` records stage outcomes. Native histories and command logs remain
private under that output. Tokens include recorded compaction and failed work,
with response IDs deduplicated across the explicit roster. Missing receipts are
coverage gaps rather than measured zero. Coordinator is explicitly absent.
Stage time includes model scheduling, tools and settlement; it is not inference
time. Cycle time also includes workspace preparation, accounting and checking;
the final report serialization is outside the measured interval.

A failed stage stops the attempt and keeps its cost; later stages are not-run.
The CLI exits nonzero on stage failure, incorrect/not-evaluable output, or
incomplete usage coverage. A successful process exit alone never passes the
independent checker. Evaluation evidence entries are exact workspace-relative filenames, not explanatory sentences; the native output schema constrains them to the supplied artifact paths. Malformed references remain not-evaluable and are never silently repaired. The sample checker recomputes paired control/candidate MSE
from prediction rows and frozen targets and verifies the restricted conclusion.
It does not certify arbitrary scientific claims or computation provenance.

Historical cases must first recover the pre-plan workspace, intended inputs,
allowed change and original device/data protocol. Do not copy the old plan,
patch, outputs or transcripts into the starting workspace. The numeric adapter in `case.py` supports the narrow paired-prediction CPU
contract; `cactus_case.py` separately supports the fixed Cactus development
contract described below. An AUC/training experiment cannot be relabeled as MSE. No real
historical case has yet been recovered. Keep any private dataset outside Git.
This is a development benchmark; tuning on it does not establish held-out gains.

Deterministic verification (no model calls):

```sh
uv run python -m pytest tests/test_cycle_accounting.py tests/test_cycle_case.py tests/test_cycle_runner.py
```

These tests use scripted specialists and explicitly synthetic token receipts;
the successful path executes a real small CPU process. They verify measurement
and checking, not live model quality. The separate real sandbox probe in task
acceptance checks allowed execution plus oracle-read denial.


## Editable orchestration and frozen measurement

`strategy.json` is the only optimization target. It contains common/stage
instructions and each stage's `paths` or `inline` handoff mode. The host validates
its exact schema and snapshots it into every run. Use `--strategy FILE` to compare
a candidate. Model, budgets, fixed stage guards, filesystem permissions, command
execution, samples, usage accounting and correctness rules are outside this file.
`policy.py` loads data only; a strategy cannot import Python or replace a checker.

## Ten Cactus development cycles (prepare first)

The collection adapter prepares ten predefined architecture interventions on
Aerial Cactus Identification, an MLE-Bench Lite task. Each cycle performs Plan →
Prepare → paired control/candidate Execute → Evaluate with `gpt-6.1-terra`.
The ten interventions share one task and development split; they are development
cases, not ten independent benchmarks or official MLE-Bench leaderboard results.
The architecture-only AST check freezes data, loss, training and evaluation code.
Each arm runs from scratch for one epoch, seed 0, batch 128, Adam lr .001, four
CPU threads, RGB 32×32 inputs, on one assigned GPU per cycle. Negative AUC deltas are valid
results; the checker verifies the signed delta and matching interpretation.

Keep deployment configuration, source snapshots, datasets, labels and runtime
artifacts outside Git. The operator-owned deployment JSON contains
`starting_source`, `data_metadata` (relative to that JSON), and `remote` with
`ssh_host`, `project_root`, `data_root`, `controller_root`, `python`, and either
`gpu_uuid` or a nonempty `gpu_uuids` list. The ten fixed cases are assigned to
the list in round-robin order: three GPUs receive four, three and three cases.

The remote project also needs `evidence/benchmark-collection/config.json` with
`data_root`, `controller_root`, `python`, `baseline_script`, and `grade_script`.
For multiple GPUs, its operator-owned `gpu_profiles` maps each allowed UUID to
an object containing only `gpu_index` and an absolute `gpu_lock` path. A legacy
single-GPU `gpu_uuid`, `gpu_index`, and optional `gpu_lock` are also supported.
These private paths and GPU identities are never strategy fields.

```sh
# Creates ten case directories and collection.json; no model/GPU work.
uv run python -m benchmarks.experiment_cycle.collection prepare \
  --deployment /path/to/private/deployment.json --output /path/to/private/cases

# Read-only availability check; no model calls and no training.
uv run python -m benchmarks.experiment_cycle.collection preflight /path/to/private/cases

# Later, from an authenticated controller with SSH access to the executor:
uv run python -m benchmarks.experiment_cycle.collection run /path/to/private/cases \
  --output /path/to/private/baseline-01 --allow-model --allow-gpu
```

Run these commands from the repository root. The local controller needs the
pinned authenticated Codex runtime and SSH access; the remote executor needs the
prepared task's Python, data and grader environment. `prepare` does not start
models or training.

Preflight checks every distinct host and assigned GPU before output creation or
model dispatch. Each cycle checks again before starting, and the worker checks
under its GPU lock before training. A GPU must match the configured UUID and
index, report no compute process, use less than 100 MiB and have at least
8192 MiB free. The lock coordinates cooperating launchers; it cannot reserve a
GPU against unrelated jobs. Each worker selects its assigned UUID, uses a 25%
PyTorch allocator fraction and executes the control and candidate serially with
a 600-second limit per arm.

The remote worker runs the fixed Python command directly; its receipt records
`execution_mode: direct`. The architecture AST check restricts candidate changes,
but remote execution has no operating-system filesystem or network sandbox and
does not establish hidden-label isolation. Local specialist sandbox permissions
remain separate from the remote execution environment.

One collection worker runs per host/GPU pair, with at most one cycle on each
GPU. A failed, blocked or incomplete cycle prevents queued cycles from starting;
already-running cycles settle. There is no automatic retry, GPU polling or silent
sample replacement. `collection-status.json` retains attempted reports and
recorded costs, and distinguishes the sum of cycle wall times from batch wall
time. If SSH loses its receipt, reconcile the recorded `remote-operation.json`
operation ID using the remote worker's `status` action and durable logs before
any retry. Unknown cleanup is recorded as uncertain, not successful completion.

To continue a collection with already accepted cycles, pass `--completed-report`
once per prior report alongside the ordinary `run` arguments:

```sh
uv run python -m benchmarks.experiment_cycle.collection run /path/to/private/cases \
  --output /path/to/private/continuation --allow-model --allow-gpu \
  --completed-report /path/to/private/prior-cycle/report.json
```

The operator must select reports from the same fixed case and inputs; the
collection matches case IDs but does not establish content identity. Each report
must show a completed cycle, passing correctness, complete usage coverage, the
configured model, all four completed stages and the same strategy snapshot.
Carried cycles are not rerun. Their recorded tokens and cycle wall times enter the
aggregate; `batch_wall_seconds` measures only the current invocation, as stated
by `batch_wall_scope`. Retain original reports and supporting evidence when using
an independently rechecked report. The execution receipt uses `candidate_path`
for the local candidate source path and `candidate` for the remote candidate
result, preserving both.

A real remote execution-only paired smoke completed both arms (8.18 seconds for
the baseline and 7.73 seconds for the candidate) and produced scores. This
checks that execution path only; it does not validate a complete collection or
establish research benefit.

## Research Loop optimization entry

Configure a Study without model calls (repeat `--case` for all frozen cases):

```sh
uv run python -m benchmarks.experiment_cycle.optimization init \
  --case /path/to/private/cases/cycle-01/case.json \
  --study /path/to/private/strategy-study --max-evaluations 3

# Later; GPU cases also require --allow-gpu:
uv run python -m benchmarks.experiment_cycle.optimization run \
  /path/to/private/strategy-study --allow-model --allow-gpu
```

The existing Research Loop runs the optimizer with Terra and host delegation.
Only `work/strategy.json` is the candidate. Its lead receives a no-argument
`evaluate_candidate` tool; fixed operator configuration selects cases, execution
and checker. Every dispatched candidate evaluation is charged before execution,
including failures; resource-blocked checks do not charge the evaluation budget.
Each evaluation replays every configured case. Compare only candidates with
correctness pass and complete token coverage, minimizing recorded cycle tokens
first and cycle wall time second. The loop's own optimization cost is separate
from the measured inner cycles and must be included when assessing net benefit.
Evaluation outputs stay private outside the Study; only aggregate gates/totals
are returned. Repeated optimization on these cases is development evidence.

Verify without models or GPUs:

```sh
uv run python -m pytest -q tests/test_cycle_*.py
```
