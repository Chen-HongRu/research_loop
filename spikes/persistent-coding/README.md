# Persistent Coding comparison (#95)

Two independent, runnable selection spikes. These are evidence for
[the framework decision](../../docs/runtime-framework-decision.md), not production
Research Loop backends. No research tasks, Review, Manager or old-runtime switch
is implemented here. The default provider is explicitly synthetic; file tools,
subprocess tests, SQLite and PostgreSQL are real. A separate authorized
`deepseek-flash` coding run passed on both candidates; see the decision record.

## Install

Use Node >=22.19.0, uv, an installed Python >=3.11 and a POSIX shell.
Commands below run in `spikes/persistent-coding/`:

```sh
npm ci --registry=https://registry.npmjs.org
npm run check
```

`package-lock.json` fixes the published Pi packages at 1.5.0 and DBOS at 5.4.0.
Neither candidate uses a source checkout or a `main` API. If uv's default cache
is unwritable, set `UV_CACHE_DIR` to a writable directory outside the repository.
`uv run --no-project --offline python --version` must work before starting.

## Pi Durable

```sh
spike_parent=$(mktemp -d)
node --experimental-strip-types durable.ts init "$spike_parent/durable"
node --experimental-strip-types durable.ts run "$spike_parent/durable"
node --experimental-strip-types durable.ts status "$spike_parent/durable"
node --experimental-strip-types durable.ts run "$spike_parent/durable"
```

The second `run` reattaches to the same completed framework Task. It makes no
additional model or tool calls. `durable.sqlite` owns execution, conversation,
submission and tool state; `run.json` holds original inputs and deadline.
Keep the entire directory, including `workspace/`, when moving or restarting a
host. Paths are absolute: relocating a run is not supported by these spikes.

There are three public Task phases: create the owned coding conversation, submit
and await the request with its stable `requestId`, then independently test the
workspace. The task pointer is stored in the same framework commit as task
creation. The host's ordinary `await` is not the recovery mechanism.

## DBOS + Pi

Provision an **isolated, authorized local PostgreSQL** database and set
`DBOS_SYSTEM_DATABASE_URL` outside Git. DBOS 5.4.0 has no SQLite system database;
Docker is optional. This spike needs permission to create its DBOS schema/tables.
Never point fault tests at a production database.

```sh
: "${DBOS_SYSTEM_DATABASE_URL:?Set an isolated local PostgreSQL URL}"
node --experimental-strip-types dbos.ts init "$spike_parent/dbos"
node --experimental-strip-types dbos.ts run "$spike_parent/dbos"
node --experimental-strip-types dbos.ts status "$spike_parent/dbos"
node --experimental-strip-types dbos.ts run "$spike_parent/dbos"
```

DBOS owns the two steps (coding episode, independent test), keyed by a stable
workflow ID and fixed application/executor versions. Pi owns `dbos/episode.jsonl`.
The host atomically claims that canonical Pi file before requesting a model.
An identity entry binds the session to the workspace, original config and prompt.

On step re-entry a complete, matching Pi session returns its original answer,
including when the DBOS step ACK was lost. Empty, interrupted, damaged or
mismatched sessions stop with `MANUAL_RECONCILIATION_REQUIRED`; they are never
prompted again automatically. Inspect the original log, workspace and effects.
Do not delete/clear the file to retry. Any new work after manual reconciliation
needs a new, separately authorized run/workspace. This is an accepted limitation
of the losing spike, not automatic intra-episode recovery.

## Interrupt, cancel and inspect

Only one writable host may drive a run directory. These spikes do not implement
a scheduler, daemon supervisor or multi-host deployment.

- `SIGTERM` normally closes the owning host. Durable leaves pending framework
  work for restart. DBOS aborts the live Pi episode and stops; its incomplete Pi
  session requires manual reconciliation.
- `node <candidate>.ts cancel RUN_DIRECTORY` persists `STOP`. A running host
  observes it and aborts its episode; do not remove STOP to restart work.
  A stopped Durable host can run once more to commit its abort, without a new
  model request. DBOS refuses `run` with STOP set; `status` remains available.
- `SIGINT` also requests cancellation. The original deadline is five minutes
  after `init`. `run` never refreshes it. DBOS refuses expired runs even if a
  result is cached; use `status` to inspect that result.
- `SIGKILL` cannot run cleanup. A bash subprocess can outlive the host, and
  existing edits are not rolled back. Durable reports unsafe interrupted tools
  as possibly partially executed; it does not automatically replay them. A model
  can issue another command, so ordinary bash is unsuitable for costly jobs.
- Missing or replaced workspace identity fails explicitly. Neither candidate
  creates a replacement directory during recovery.

`trace.jsonl` records requests, native tool starts/results and host verification.
It is diagnostic output, not a parallel execution authority. Keep it and raw
sessions outside Git. Non-completed runs and failed independent tests exit nonzero.
`status` opens no new model request; the Durable reader does not resume tasks and
the DBOS reader uses `DBOSClient` without launching a workflow executor.

The workspace is disposable but **not an OS sandbox**. Native bash retains the
host user's access. There are no private data/checkpoints in the fixture; do not
reuse this spike as an isolation or expensive-operation platform.

## Repeat the evidence

```sh
npm test
node loc.mjs
```

Without `DBOS_SYSTEM_DATABASE_URL`, DBOS integration cases are omitted/skipped;
that result proves only the Durable matrix and DBOS read-only session checks.
With the isolated database variable set, `npm test` exercises both candidates.
Set `RL_SPIKE_REPORT` to an external JSON path to save the sanitized matrix
summary. Full per-run traces remain in temporary directories.

The synthetic provider reads real tool feedback and deliberately makes a wrong
edit before correcting it. Both candidates receive the same prompt, initial
Python source/tests, faux/coding model and medium thinking setting. Their native
system prompts and tool schemas differ; this is not a coding-quality or latency
benchmark. Faux token/usage estimates are not paid model usage.

Tests inject interrupted model requests, edit results before framework ACK,
non-idempotent bash effects, completed episodes before outer ACK, STOP, deadline,
normal shutdown, lost workspaces and false model success. `RL_SPIKE_FAULT` is
test-only; unset it for ordinary commands. These tests execute no GPU or remote
jobs and spend no model quota.

After explicit model-account/budget authorization, pass an exact provider/model
and optional `off|medium` thinking setting as `init`'s last arguments. The SDK uses its normal credential configuration;
the repository never copies credentials. There is no provider/model fallback.
The host returns structured test results and saves the model's final text as a
summary; it does not infer validity from the model's output format or self-report.
Real coding and Stage B's real two-round research remain separate gates.
