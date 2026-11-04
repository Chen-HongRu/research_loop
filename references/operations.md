# Operate an existing Study through Codex

Use the bundled launcher for all generic commands below, preserving absolute paths.
Identify the Study and runtime from the earlier handoff or repository records;
inspect status before mutations. Multiple plausible Studies require clarification.
Task-specific runners supersede generic `run` and have separate resource controls.

For SQLite Studies, prefix the commands below with the bundled launcher followed
by `runtime`. Read [concurrent runtime](../docs/concurrent-runtime.md) for the
trusted application and complete command contract. Use `runtime status STUDY`,
`runtime events STUDY --consumer NAME`, or `runtime dashboard STUDY` for read-only
inspection. Use `runtime run STUDY` to drive work within the original authorization;
`--watch` keeps the owner available while idle. Idle is not goal completion.
Mutations (`task create/edit/cancel`, `stop`, `scout`, `plan`, `retry-review`,
`cancel-operation`, `retry-action`, `ack-event`) submit durable intents. Inspect
`runtime request-status STUDY REQUEST_ID`: only `status: applied` means the
command took effect; `not_recorded` can still mean queued. Scout and Review retry
stay pending without spending a call until `runtime run` admits them. Use the same request ID only for the
same intent. STOP is permanent; there is no resume API. Interrupting the driver
does not request or clear STOP. Reconcile unknown effects and holds rather than
resubmitting. Events and acknowledgements are durable, but no agent wakeup consumer
or automatic follow-up is installed. The remaining commands in this guide apply
only to the legacy document controller.

The generic launcher does not dispatch adapter scripts. Adapter guides assume a
repository-root working directory: use `uv run --locked --directory SKILL_ROOT
python -m integrations.protok.tasks ...` (or the guide's script path) with all
Study/project/input/output arguments absolute. `--project` alone preserves the
external cwd and does not expose root-level `integrations` modules. Apply each
adapter's own dependency instructions. For an already deployed runtime, keep its
existing runner and host; installing this skill does not authorize switching or
redeploying that runtime.

| Intent | CLI or evidence |
| --- | --- |
| Status and failures | `status STUDY`; inspect current attempt receipts/logs as needed |
| Current findings | `knowledge STUDY`, then cited published experiment records |
| Experimental history | `graph STUDY` |
| Best comparable candidates | `frontiers STUDY`; preserve evaluation/scope distinctions |
| Continue autonomous work | `run STUDY --iterations 1` within remaining authorization |
| Reopen an unfinished reviewed stop | `resume STUDY --reason "Actual user request"`, then the correct runner |
| Queue a specified investigation | `task submit STUDY --request FILE` |
| Inspect requests | `task list STUDY`, `task status STUDY ID` |
| Reprioritize/cancel an unbound request | `task priority STUDY ID NUMBER`, `task cancel STUDY ID` |

For requested investigations, read [task fields and dispatch](../docs/task-queue.md).
Generate the request JSON from the discussion, not a template for the user to fill.
Freeze a recoverable committed baseline: uncommitted source cannot enter a queued
task. Reuse an ID only for an identical request; intentional replication needs a
new ID. Submission is durable but does not start computation. Generic `run` consumes
pending main investigations; long training requires an authorized task adapter and
scheduler. Do not promise background execution just because submission succeeded.

The ProTok scheduler dispatches long-training children asynchronously and isolates
retained failures by child ID. Other independent tasks can proceed while one child
awaits reconciliation. Automatic children still share their allocation/accounting
gate; manual task budgets are independent. A parent Study's incomplete iteration
remains serial. Follow the task guide for exact-ID recovery or explicit advancement
after fixing a preparation error; do not clear errors or replay unknown Execute.

## Pause, stop and recovery

If Review stopped an unfinished Study awaiting a later instruction, an explicit
user request to continue authorizes `resume STUDY --reason ...`. This records the
request and makes the Study ready without a model call. It requires a published
Review, an unfinished goal and remaining original iteration/call/time budgets;
it preserves all counters, evidence and route statuses. It cannot reopen goal-met,
budget, Plan/Judge or interrupted-execution stops. `run` remains a separate bounded
action, subject to the existing adapter's admission, lease and STOP checks.

A generic `run` is bounded and has no persistent daemon or generic pause/stop CLI.
If idle, do not start another iteration. If active, signal the exact owned runner
process with SIGTERM/interrupt, retain its logs and wait for termination. Do not
kill unrelated Codex processes. Cancellation of the local process group does not
prove a remote job stopped. Use the adapter's owned-job cancellation/status API and
retain unknown effects until reconciled. Task cancellation affects only unbound
requests, not a running experiment.

Read [recovery](../docs/recovery.md) and the adapter's instructions before acting.
Completed receipts/publication and partial read-only phases can normally resume via
`run`; incomplete Execute requires inspecting retained work and external effects.
Use `retry STUDY`, then `run`, for read-only failures or completed-receipt replay.
An unfinished Execute requires the backend's trusted `Study.reconcile_execution`
certificate; a bare retry cannot authorize a replacement execution. Unknown
effects, active processes and unsettled accounting remain blocking. Do not edit state, clear locks, delete attempts or reinitialize to
force a replay. Task adapters may reconcile the original execution without a new
Agent call; use their existing interface.

Budget exhaustion is a stop. Do not reset counters or silently increase limits to
satisfy a request to continue. Obtain authorization for specific added resources
and use the project's supported extension workflow, if any; generic CLI has no
budget-extension command. Goal/evaluation changes can change research meaning;
make them only when authorized and idle, preserving history and comparability.

## Optional capabilities and maintenance

Load only when asked or needed: [Scout](../docs/scout.md),
[telemetry](../docs/telemetry.md), [long training](../docs/long-training.md),
[Meta Loop](../docs/meta-loop-design.md). None is enabled automatically by this skill.

For an update, finish or stop/reconcile owned work, retain the old runtime and
Studies, install the new exact ref into a separate directory, run doctor, and check
the Study version against its usage guide. Do not migrate Studies implicitly or
overwrite an installed runtime while it is executing. Switch the discovery link
only once the replacement is ready and the user authorized the update.

For removal, stop/reconcile owned processes, unlink only this skill's discovery
link, and remove only its installed runtime if requested. Studies, data and
checkpoints remain in their external paths; deleting those requires explicit scope.
