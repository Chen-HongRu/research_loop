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
runtime must be installed and authenticated; the model is `gpt-6.1-sol` at medium.
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
independent checker. The sample checker recomputes paired control/candidate MSE
from prediction rows and frozen targets and verifies the restricted conclusion.
It does not certify arbitrary scientific claims or computation provenance.

Historical cases must first recover the pre-plan workspace, intended inputs,
allowed change and original device/data protocol. Do not copy the old plan,
patch, outputs or transcripts into the starting workspace. Version 1 supports
only the narrow paired-prediction CPU case/checker contract in `case.py`; a
historical AUC/training experiment cannot be relabeled as this MSE task. No real
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
