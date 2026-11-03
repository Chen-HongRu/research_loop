# Operate an existing Study through Codex

Use the bundled launcher for all generic commands below, preserving absolute paths.
Identify the Study and runtime from the earlier handoff or repository records;
inspect status before mutations. Multiple plausible Studies require clarification.
Task-specific runners supersede generic `run` and have separate resource controls.

| Intent | CLI or evidence |
| --- | --- |
| Status and failures | `status STUDY`; inspect current attempt receipts/logs as needed |
| Current findings | `knowledge STUDY`, then cited published experiment records |
| Experimental history | `graph STUDY` |
| Best comparable candidates | `frontiers STUDY`; preserve evaluation/scope distinctions |
| Continue autonomous work | `run STUDY --iterations 1` within remaining authorization |
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

## Pause, stop and recovery

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
Only after effects are reconciled and a fresh attempt is authorized use
`retry STUDY`, then `run`. Unknown effects, active processes and unsettled accounting
remain blocking. Do not edit state, clear locks, delete attempts or reinitialize to
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
