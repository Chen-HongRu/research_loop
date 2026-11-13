# Operations

This is the canonical operator reference. Commands call the shared
[`application.py`](../src/research_loop/application.py) interface; queries read
persisted facts. Programs and configuration are trusted operator inputs. Establish
the research question, data/evaluation boundaries, resource authorization, and stop
conditions before any model or compute launch.

## Prepare, inspect, and start

```sh
uv run research-loop prepare /path/to/private-run --config examples/research.yaml \
  --project /path/to/project --goal /path/to/goal.md \
  --input-ref /path/to/evidence.json
uv run research-loop inspect /path/to/private-run
uv run research-loop start /path/to/private-run
uv run research-loop status /path/to/private-run
```

The run directory must be new. `--input-ref` repeats for initial evidence. Prepare
copies the goal, explicit small declarations/programs/instructions/schemas, and a
managed source snapshot. Inspect uses that same revision and shows actual workflow
Router policy, its schema template, profiles, declared permissions, and current
local environment observation. Prepare/inspect make no model calls or resource
allocations. Environment observation runs the selected interpreter's standard
library and reads installed distribution metadata; remote declarations remain
unverified.

`start` launches the recorded interpreter and frozen controller source in a new
process session. Its log is `RUN/controller.log`; terminal completion is recorded
in `RUN/completion.json`. Follow `status` and the completion record to distinguish
starting, active, finished, failed, and uncertain work. A PID alone is not evidence
of successful execution or settlement. Existing supervisors may use
`start RUN --foreground`; see [deployment](linux-deployment.md) for retaining the
prepared implementation. No automatic restart or event-to-Codex wakeup is supplied.

## Configuration

[`examples/research.yaml`](../examples/research.yaml) is a complete reference
research catalog. A single YAML declares:

- `version: 1` and `workflow: {program, settings}`.
- `router: {profile}` and `profiles: {name: {model, effort}}`.
- `agents: {name: {instructions, profile, tools, output_schema}}`.
- `groups: {name: {version, description, program, agents, settings}}`.
- Optional `execution: {environment, resource}` file references.

Agent `instructions` and optional `output_schema` reference text and JSON files;
resolved configuration holds their contents. Group `agents` maps program step
bindings to Agent IDs. `tools` is `workspace` or `none`. Effective tools are bounded
by both the declaration and runtime authorization. Workspace permissions do not
isolate host-readable evidence; independent review also requires the task's
explicit evidence contract.

Paths resolve relative to the YAML file. Paths inside environment/resource files
resolve against those files' own parents. There is no inheritance, interpolation,
provider selection, or implicit model fallback. Model identifiers are validated as
OpenAI identifiers; configuration parsing does not establish account availability
or harness support. An unsupported selection must fail explicitly at execution.
The reference profiles select `gpt-6.1-sol` with medium effort; change the author
configuration to select another authorized OpenAI model. Unknown fields, invalid
types, and missing bindings are rejected.

The active revision pointer is `RUN/active.json`. Normalized revisions are under
`RUN/configurations/REV/`, and `RUN/implementation/` holds managed controller and
package source with interpreter/version metadata. Explicit program revisions can
replace their small managed files; shared Python imports remain fixed by the
controller snapshot. Changing shared implementation requires stopping the entire
controller and preparing a matching implementation. External imports and data
references are not made immutable by copying an entry file. Preparation performs
no dataset, checkpoint, or dependency-content audit.

### Execution environments

An optional environment declaration selects an existing interpreter:

```json
{"python": "../.venv/bin/python"}
```

`examples/environment.json` selects this checkout's existing virtualenv. To use it,
add `execution.environment: ./environment.json` to a YAML beside that file, or
choose the corresponding relative/absolute path in your author declaration. The
runtime preserves the invoked virtualenv interpreter path. Without a declaration,
the controller interpreter is used. Optional `resource` environment declarations
provide a remote absolute `python` and a `packages` version map; they are unverified
and do not provision an environment.

`execution.resource` references the existing authorized Slurm JSON declaration:
`host`, `partition`, optional `account`, absolute `remote_root`,
`gpu_seconds_total`, `job_seconds`, and `ledger_path`, with optional `cpus`,
`memory`, and `grace_seconds`. Preparation normalizes the external ledger reference
without reading or allocating it. Group `settings.execution_backend` explicitly
chooses `local` or `resource`; the reference example uses local execution. Remote
code/data provisioning remains an operator responsibility under existing authority.

## Inputs and interventions

The reference research workflow accepts an explicit experiment request:

```sh
uv run research-loop intervene RUN --kind experiment --file /path/to/experiment.md \
  --id experiment-one --expected-revision r000001
uv run research-loop query RUN --kind interventions --id experiment-one
```

`--file` submits UTF-8 text as `{text: ...}`. `--payload /path/to/payload.json`
submits a JSON object instead. Work-input kinds and payloads are validated by the
selected workflow; arbitrary feedback kinds are not universally supported.
`--target` defaults to `run`, or identifies one Group Run. IDs make submissions
idempotent: identical content returns the existing receipt, conflicting content
is rejected. Receipts expose received/pending/applied/rejected states and any
recorded outcome. Applied means consumed or a control change took effect; it does
not mean an experiment or scientific goal is complete. Request submission records
the local process identity rather than trusting a claimed caller role in payloads.

Research request completion requires actual execution/evaluation and successful
publication. Failed or incomplete work retains its unfinished disposition. A later
request cannot be cleared by an earlier request's publication acknowledgement.

```sh
uv run research-loop pause RUN --id pause-one
uv run research-loop continue RUN --id continue-one
uv run research-loop stop RUN --target GROUP_RUN_ID --id stop-one
uv run research-loop resume RUN
```

Pause closes new Group/round admission; active programs may reach their next settled
output boundary. A pending pause is not a settled pause. Continue reopens admission
without resolving held help requests, failures, or unknown effects. Stop blocks new
work and interrupts selected active Groups, closing their service admission before
waiting for cleanup. It requests settlement and cannot prove a remote job ended
without the original backend receipt. Stop and pause may target `run` or a Group
Run. Run-level `resume` is explicit controller recovery: it creates new Router
decisions from valid evidence rather than replaying interrupted calls or sessions.
UNKNOWN effects must be reconciled before redispatch, and budgets are retained.

## Configuration revisions

```sh
uv run research-loop revise RUN --config /path/to/revised.yaml \
  --affected GROUP_RUN_ID --expected-revision r000001 --id revision-one
uv run research-loop query RUN --kind interventions --id revision-one
```

Repeat `--affected` for independent named executions, or use `--affected all` for a
workflow-wide change. Revise prepares a new immutable configuration and submits an
activation request. Editing source files never activates a revision. The controller
checks the expected revision, affected executions and their actual dependencies,
settles interrupted work, and activates at a decision boundary. In-flight old Router
decisions are discarded. Existing unaffected executions retain their original
configuration, Agents, profiles and subsequent calls. Interrupted work is replaced
by a linked new execution; its Python stack and Agent sessions are not resumed.

Original operation identities and receipts remain authoritative. A settled command
may supply a bounded workflow-specific recovery packet for evaluation without
rerunning it. Such a packet is unevaluated evidence, not a published scientific
result. Unknown operations cannot use this route to evade reconciliation. The
research recovery contract and selected-file limits are in the
[author API](../docs/framework-api.md).

## Queries and publication

```sh
uv run research-loop query RUN --kind runs --limit 20
uv run research-loop query RUN --kind runs --id GROUP_RUN_ID
uv run research-loop query RUN --kind steps --run-id GROUP_RUN_ID --limit 20
uv run research-loop query RUN --kind calls --run-id GROUP_RUN_ID --id CALL_ID
uv run research-loop query RUN --kind handoffs --run-id GROUP_RUN_ID
uv run research-loop query RUN --kind operations --run-id GROUP_RUN_ID
```

Lists return `items` and `next_cursor`; pass `--cursor` to request the next page.
Execution queries require `--run-id`; `--id` selects one record. Pages open only
selected small records. Group Run, round, step, Agent call, command operation,
handoff and intervention identities are separate. Parent IDs and handoffs describe
recorded relationships; ordering alone does not imply a dependency. Missing records
are unavailable, not inferred from native histories.

The dashboard's read-only `/api/execution` uses these same query functions:
`?kind=steps&run_id=GROUP_RUN_ID&limit=20`, optionally adding `id` or `cursor`.
Supported kinds are `runs`, `steps`, `calls`, `handoffs`, and `operations`; `study`
selects a registered run in a multi-run dashboard. This endpoint reads persisted
facts without scanning native histories or adding controller actions.

The workflow validates result meaning and proposes selected small publication
files. The generic publisher stages them, records metadata, and atomically activates
`RUN/outputs/current`. A stopped submission cannot activate after STOP is observed
at the commit boundary; an already active publication remains valid. Recovery of
that commit repairs receipts/status without rerunning scientific commands. Root
status and iteration archives describe execution; publication alone does not prove
scientific success. Large results and checkpoints remain external references.

## Measurements

The retained measurement modules read existing traces without model/resource calls:

```sh
uv run research-loop efficiency RUN --since 2026-01-01T00:00:00+00:00
uv run python -m research_loop.metrics watch RUN --output /path/to/private-metrics/phase \
  --since 2026-01-01T00:00:00+00:00 --phase phase
uv run python -m research_loop.metrics compare BASELINE_DIR OPTIMIZED_DIR
uv run python -m research_loop.behavior_metrics --help
```

These are on-demand trace collectors, distinct from the bounded execution queries.
Configuration discovery follows `active.json` to the normalized revision. Resource
ledgers come from that configuration or explicit `--ledger-path` overrides in
efficiency/metrics; the behavior collector has no ledger override. The offline
default window starts at the recorded controller `started_at` (or run creation)
and ends at terminal `ended_at` when present. Explicit bounds select other windows.
Thread membership uses `roles.json` Agent/Router identities and native metadata;
unmapped histories, missing mapped histories and absent usage receipts remain
coverage gaps. New collector phases require fresh output directories when their
source format differs; earlier checkpoints are not migrated.

Native tool failures are counted only from structured `isError`/`is_error`,
`success`, or `exit_code` outcomes. Plain text containing an error word remains an
unmeasured outcome. `efficiency` exposes `agents.tool_outcomes`; continuous samples
and comparisons expose `tools` and per-task outcomes. Recognized code-mode command
receipts add separate `nested_command_receipts` zero/nonzero exit counters; they
never relabel an outer `exec` outcome or establish infrastructure failure.
Wait durations come from
paired actual calls/returns; tool-free time is not labeled idle or waste.

Per-request usage is authoritative where recorded; cumulative counters are a
cross-check. Preserve accounting versions and missing coverage. Wait durations do
not establish waste, allocation duration is not GPU utilization, and phase totals
do not establish causal research improvement. Detailed historical collector formats
remain in [efficiency](efficiency.md) and [behavior observations](behavior-metrics.md);
use the module entrypoints above for their current command invocation.
