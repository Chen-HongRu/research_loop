# Operations

Create a run using an existing project and goal, optionally supplying a resource configuration:

```sh
uv run research-loop init RUN --project PROJECT --goal GOAL --resource FILE
uv run research-loop inspect RUN
uv run research-loop run RUN
uv run research-loop run RUN --continue
uv run research-loop status RUN
uv run research-loop stop RUN
```

Omit `--resource FILE` when remote resources are not authorized. Resource JSON specifies `host`, `partition`, `remote_root`, `ledger_path`, `gpu_seconds_total`, and `job_seconds`; `account`, `cpus`, `memory`, and `grace_seconds` are optional. Initialization freezes the parsed configuration and resolves `ledger_path` relative to the source configuration file. The shared ledger and its lock must be outside the agent-writable work and draft directories.

## Router Groups

New Studies default to the reference Router Group workflow. Pass `--groups /path/to/groups.json` to replace its catalog, or `--coordinator lead` to select the legacy coordinator. Existing Study configuration is unchanged. Inspect the Study before running it; the catalog and program interface, Group Run boundaries, and crash reconciliation are described in the [Router Group contract](../docs/router-groups.md). Host/native delegation is separate from Group coordination. Model and remote execution require explicit authorization.

## Read-only dashboard

Run `uv run python -m research_loop.dashboard.server --study RUN --port 8768`
and open `http://localhost:8768/`. Register multiple Studies with repeated
`--study` or opt into immediate-child discovery with `--studies-dir STUDIES`.
Only expose records authorized for everyone with access to this loopback service
or its configured reverse proxy.

The overview shows current execution separately from the latest reported result.
Use iteration history for paged results and evidence, tasks for role/iteration
filters, activity for coordinator messages and retained tools, resources/metrics
for existing measurements, and context for the goal and working documents.
Publication is read from the activated knowledge pointer and matching metadata;
a result file alone does not establish publication or scientific success.
Only the active operational page refreshes; context loads on navigation. Links
retain the Study and selected view. Missing detail or removed Studies show an
explicit unavailable state. No dashboard action continues/stops a Study or
dispatches compute. See [metrics setup](efficiency.md) and
[optional behavior observations](behavior-metrics.md) for existing sidecar outputs.

## Execution environments

Provision the environment once, outside the research loop, using `uv`. Then
select its existing interpreter when initializing the Study:

```sh
uv run research-loop init RUN --project PROJECT --goal GOAL \
  --environment environment.json
uv run research-loop inspect RUN
```

The configuration accepts only these fields:

```json
{
  "python": "/path/to/local-venv/bin/python",
  "resource": {
    "python": "/path/to/resource-venv/bin/python",
    "packages": {"example-library": "1.2.3"}
  }
}
```

`python` is required. A relative local interpreter path is resolved against the
environment JSON file's directory, preserving a virtualenv's interpreter
symlink. The optional `resource` section describes a remote environment;
its interpreter must be an absolute POSIX path, and `packages` may be omitted.
Do not put credentials, arbitrary environment variables or installation commands
in this file. Initialization copies the normalized configuration into
`RUN/config.json`; later edits to the source JSON do not update that Study.
The repository's [example](../examples/environment.json) selects the checkout's
existing `.venv` after `uv sync --locked`.

`inspect` and iteration admission observe local interpreter/version/prefix and
installed distribution names/versions with Python's standard-library metadata.
They do not import numerical libraries, load models, inspect datasets or probe
CUDA. The same snapshot is supplied to the coordinator and fresh role agents.
Agent shells and prepared local commands use the same execution overrides. PATH
prioritizes the selected interpreter's bin directory, followed by automatically
resolved host `uv`/`rg` directories when available and system command directories.
Prepared commands clear conflicting `PYTHONHOME` and stale `VIRTUAL_ENV` values;
virtualenv
identity is explicit and user-site packages are disabled. Use that Python for
tests instead of creating another environment or selecting system Python.
Package inventory is an observation, not a lockfile or an import/GPU test. The
loop never installs or synchronizes dependencies automatically; use `uv` to
maintain the chosen environment outside the agent's read-only environment mount.

Resource interpreter/package information is **declared and unverified**. The
feature does not connect to the resource host or establish that those packages
import there. It also displays the actual configured partition, resource-tool
names and budget snapshot when resources are configured. With the goal's
authorization and remaining budget, the coordinator may use the exposed resource
tools for real device/debug tests and experiments, including a configured
`debug` partition. Specialists return prepared commands and findings to the
coordinator; they do not bypass the resource adapter. Missing CUDA on the
coordinator is not evidence that an authorized resource test cannot run.

No environment file is required for old or new Studies: the default remains the
controller's current Python environment. Adding this feature does not rewrite
old configuration, restart controllers, change ledgers or deploy code to running
hosts. When moving a configured Study, provision the corresponding interpreter
on the new host and inspect it before explicit continuation; do not copy a
platform-specific virtualenv.

## Human experiments

Submit a UTF-8 question, hypothesis or experiment design to an initialized Study:

```sh
uv run research-loop experiment RUN --file experiment.md
uv run research-loop experiment RUN --stdin < experiment.md
EDITOR=vi uv run research-loop edit-experiment RUN
```

These are local operator commands without a model call or resource dispatch.
You may ask a separate Codex session to run the injection command on your behalf;
that session must have your authorization and write access to the Study inputs.
The running lead and specialist agents cannot inject, revise or clear requests.
Injection refuses to overwrite a nonempty pending request. Use the editor command
to revise or clear it. It uses `VISUAL`, then `EDITOR`, and requires a foreground
editor (for GUI editors, configure their wait option).

The input is `RUN/inputs/human.md`. Direct file editing is supported while the
controller is stopped. During a run, use `edit-experiment`: it holds
`RUN/inputs/human.lock` for the whole edit, coordinating with admission and
consumption. Arbitrary editors or Codex writes that bypass this lock cannot be
protected against concurrent clearing. An open editor may delay iteration
admission or acknowledgement until it exits. Keep the input directory and file
as regular local paths, not symlinks.

A missing or whitespace-only file preserves autonomous planning. A nonempty
request is snapshotted at iteration start and takes priority over autonomous
experiment selection within the existing Study goal, scientific protocol and
resource authorization. The Proposer fills in design details; ambiguity that
changes experiment meaning requires clarification. Edits during execution become
input to a later iteration and do not interrupt the admitted experiment. One file
is one request; if it lists multiple experiments, all requested work is its scope.

The input packet includes `human_request` (`id`, `text`) and
`human_request_file`. For an active request, the output schema additionally
requires `human_request` with the matching `id`, `status` (`completed`, `pending`
or `blocked`), `evidence` references and a nonempty `reason`. `completed` means
the requested scope was executed, analyzed and summarized in the draft, including
negative or inconclusive findings. Missing work or execution failure is not
completion. The host checks acknowledgement shape and identity; scientific
completion remains the team's judgment. `blocked` requires a Study stop and an
explanation for user intervention; it does not silently fall back to autonomous
experiments. A pending request may continue in another iteration, carrying exact
progress so completed operations are not dispatched again.

After native work and resources settle and knowledge publication succeeds, the
controller clears the request only if its revision still matches the admitted
snapshot. Rewriting even identical text counts as a new revision. Original
snapshots remain at `iterations/NNNNNN/human-request-ID.json`, with acknowledgement
and evidence in that iteration's result and publication metadata. Failure, STOP
before publication, UNKNOWN resources or unsuccessful publication retains the
request. An activated publication commits completion even if STOP arrives later.
Restart replays that receipt's acknowledgement without rerunning the experiment;
a newer revision is preserved. This does not promise recovery from arbitrary
crashes or scientific deduplication after unpublished work.

Submitting a request does not start or resume the Study. A running Study reads it
at its next admission. Start an initialized Study with `run RUN`; explicitly
resume a stopped or completed Study with `run RUN --continue`. Existing resource
authorization is unchanged. Human completion is independent of Study completion.

## OpenAI-compatible provider

`init` can point the same native harness at an OpenAI-compatible Responses endpoint instead of the Codex ChatGPT subscription:

```sh
uv run research-loop init RUN --project PROJECT --goal GOAL \
  --provider NAME --provider-base-url https://provider.example/v1 \
  --api-key-env PROVIDER_API_KEY --model MODEL --effort medium \
  --model-context-window 131072 --coordinator lead --delegation host
```

`--provider`, `--provider-base-url` and `--api-key-env` are required together, and `--model` is then required. Only the environment variable name is recorded; `config.json`, `call.json` and the event journal never contain the key value. Initialization fails early when the variable is unset, and `run` rechecks it before launching the native process. The key is forwarded only through the isolated process environment, not the agent shell or the sandbox filesystem policy.

In Lead mode, provider runs default to `--delegation host`. The host starts each role as a new Codex thread, injects the canonical common contract and role template as developer instructions, sends the assignment as the first plain user message, and exposes `delegate_task(role, task_name, message)` and `wait_tasks(task_ids)` to the lead. `delegate_task` returns immediately; `wait_tasks` returns the current status and final summaries without blocking, so the lead can continue useful coordination. At most four tasks run concurrently. Every task is a new context and cannot be resumed: a revision is another delegation. Host-mode catalogs omit `multi_agent_version`, which disables native Codex subagent tools for the lead and roles; if such a tool still appears, an attempt fails the role task. Host threads are written to `RUN/roles.json`, and the dashboard, `efficiency` and `metrics watch` use that mapping for role attribution. Dashboard cards label these roles as recorded, take lifecycle status from the mapping, and read public summaries from the mapped native histories; missing histories do not hide the task or invent a result. The host mode also works with the subscription path when both modes must be compared; there the built-in catalog may retain native spawn tools, so the role guard and lead child tracking remain the boundary.

`--delegation native` keeps the native Codex spawn path. Provider runs that select it, and any supplied catalog, must declare `multi_agent_version = "v1"` and `supports_search_tool = false`; non-OpenAI Responses gateways cannot decrypt the V2 `encrypted_content` task payload. Every provider catalog disables `supports_search_tool`; a supplied catalog must match the selected delegation mode. The harness disables hosted web search in provider mode. A lead or Scout that needs external sources must report the missing retrieval instead of citing recalled material.

Unless `--model-catalog FILE` points at an existing Codex catalog, `run` generates `RUN/native/models.json` for the configured model, effort levels and `--model-context-window` (default 131072).

`inspect` reports the delegation mode, provider name, base URL, API-key variable name and the supplied catalog path (null when the catalog is generated at run time) without a model call. Each iteration's `call.json` records the effective native permissions, including `web_search: false` and the delegation mode. `verify` accepts provider mode without a ChatGPT account but still requires the configured model and reasoning effort to appear in the catalog. Provider mode is a separate execution baseline: do not present its results as equivalent to the subscription model without a controlled comparison.

`inspect` shows the main instructions, output schema, native permission plan, resource snapshot and tool schemas without a model call, account access or ledger write. The final result separates scientific `goal` from required `study_action` (`continue` or `stop`) and string `stop_reason`. A finished diagnostic or Review does not itself stop the Study. The lead either defines the next falsifiable experiment or names a Study-level reason to stop: overall goal satisfaction, exhausted resources, an external block, or no useful justified action remaining. The host follows that action without judging scientific quality.

For `continue`, the host settles and closes native and resource sessions and publishes the knowledge snapshot. Lead mode then resumes the original lead thread; Router mode uses fresh routing contexts. One writer lock spans all iterations. `iterations/NNNNNN/status.json` and `result.json` retain iteration history; root `status.json` and `result.json` show the latest state and returned result. `iteration-completed` means the snapshot was published and the Study intends to continue; `completed` means an explicit Study stop. A plain `run` returns a cached completed Study. Use `run RUN --continue` explicitly to continue a completed Study, including one created by the earlier single-iteration runtime, or to clear STOP. Original pre-upgrade status and result files are archived before replacement, and the original thread and resource ledger are retained. An interrupted published iteration advances to the next number; unfinished work resumes its original thread. STOP after publication preserves that iteration's archive and durable `next_iteration`, so continuation starts the following iteration without overwriting the published result.

An allocation with no job identity can be reconciled as `NOT_DISPATCHED` only when both scheduler queries are empty and its protected allocator log contains exactly the OpenSSH banner-timeout diagnostic. Banner exchange precedes authentication and remote command execution. Missing accounting, partial or mixed output, and other transport failures remain UNKNOWN. This check runs only during reconciliation, reading at most 4,097 bytes from the original local log; it does not resubmit an allocation.

Use the original ledger and job identity across attempts. An unknown submission must be reconciled before another allocation is made. Failure, unknown resource state or cleanup failure retains the draft and result without activating that iteration's knowledge or starting another iteration. STOP prevents new research dispatch, including between iterations, and attempts native and child cleanup; the physical resource receipt remains authoritative, because stopping a local agent does not prove a remote job has terminated. No failed or uncertain effect is automatically retried.

The active knowledge version has host publication metadata under `knowledge/publications/`. If interruption occurs after activation but before root status is recorded, the next invocation recovers the committed iteration from matching metadata without replaying it; a newer unfinished iteration is preserved. A physical STOP still requires explicit `--continue`. STOP detected during snapshot copying prevents activation and retains the draft.

The host records physical events and resource receipts. In legacy Lead mode, the lead coordinates research and dynamically invokes Scout, Proposer, Experimenter, Analyst and Critic as needed. Native delegation uses a fresh native subagent with `fork_turns="none"`; host delegation starts a new plain-message thread per task. Existing role contexts are not resumed, reused or followed up in either mode. Scientific proposals and analysis are performed by the roles, while research resource tools remain lead-only. Structured output validates shape, not scientific correctness. The runtime does not promise automatic recovery from arbitrary crashes. See [design](../docs/design.md).

## Efficiency diagnostics

For a restartable, continuously running collector and before/after comparisons,
see [continuous measurements](efficiency.md). The command below is a one-time audit.

```sh
uv run research-loop efficiency RUN
uv run research-loop efficiency RUN --since 2026-01-01T08:00:00+08:00 --until 2026-01-01T12:00:00+08:00
```

This operator command reads existing `events.jsonl` and recorded native sessions from `native/sessions/`, `groups/router/*/native/sessions/` and `group-runs/*/native-runtime/native/sessions/`. Membership follows the coordinator, confirmed descendants and `roles.json` mappings, including every Router and Group specialist. It also reads the frozen configuration's optional
`resource.ledger_path` (falling back to `RUN/gpu-budget.json` for older records). It prints
aggregate JSON without a model call, remote access, Study writes, lifecycle hooks
or changes to the running controller. It scans trace files only when requested;
it does not scan datasets, caches or checkpoints. `--sessions-dir PATH` selects
only the operator-supplied native trace snapshot; `--ledger-path PATH` overrides the
configured ledger when auditing a copied snapshot. Keep traces and reports private.

The default period starts at the first recorded `lead.input` and ends at the
command's start time. Explicit bounds require a timezone and use
`since <= event_time < until`. Usage is attributed by its notification time,
without prorating responses crossing a boundary. Token totals use valid
`token_usage_record` receipts, deduplicated by `response_id` across the selected
Study threads, including receipts observed before the window. Both the top-level
record and `event_msg` forms are accepted; compaction requests count when they
have receipts. Input already includes cached input; output already includes
reasoning. Cumulative `token_count` differences appear only in
`llm.reconciliation.cumulative_usage` and per-task `cumulative_window`;
`record_minus_cumulative` subtracts those differences from request totals.
The two sources are never added or substituted for each other.

An accepted receipt requires a nonempty string response ID and a usage object
with at least one recognized Token field. Counts must be nonnegative integers
(not booleans), and cached input cannot exceed input. Omitted individual counters
default to zero; an explicit zero count is valid. Rejected receipts do not reserve
their ID, so a later corrected receipt can be accepted.

Offline reports use `schema_version: 2`. `llm.authority` names the request-usage
source; `responses` and `duplicate_responses` replace the former
`usage_updates` and `duplicate_updates` fields in total, role and task usage.
`llm.reconciliation.record_usage` was removed because request usage is now the
main total; `unique_response_records` counts accepted requests in the window.
Malformed cumulative notifications are skipped without resetting the previous
snapshot, and are reported as coverage gaps.
Version 1 reports used cumulative differences as their main total. Preserve their
historical interpretation rather than treating them as version 2 measurements.

Threads showing consumption evidence but no valid request receipts are listed in
`coverage.usage_records_missing_threads`, with `coverage.complete` and
`llm.measurable` false. Threads with no such evidence await their first receipt
and do not invent usage. `usage_records_observed` counts accepted receipts across
the available history before the cutoff, whereas `responses` counts only the
window. A recorded zero is not a measured zero for a missing-receipt thread.
This detection cannot establish that every request in an otherwise recorded
thread was captured. Invalid receipts, counter resets and partial tails are
reported as coverage limitations. Complete JSON without a terminating newline
also remains pending. Readers expect chronological timestamps within each source.
Both stop Token accounting for a source at the exclusive cutoff. Global duplicate
IDs are assigned to the first encountered session; initial discovery uses file order.
For copied IDs across threads, later discovery or Host mapping changes can alter
attribution relative to a fresh offline snapshot. Matching attribution requires
the same selected histories and encounter order; unique request IDs avoid this
ambiguity.

Host-delegated roles use `roles.json`; other delegate roles are inferred from task names. Tool counts include top-level native
calls; calls nested inside code execution are not counted again. Observed waiting
can overlap across delegates and does not itself prove wasted work. Resource
call failures are distinct from scientific command failures. Allocation seconds
are full settled elapsed time for reservations dispatched in the period, rather
than precise period overlap or GPU core utilization; a cancelled allocation can
contain a successful experiment. The report also gives its own trace-reading
time and bytes. It does not infer billing, scientific quality, KB comprehension
or exploration from token and tool counts.

Assess Scout exploration using deduplicated hypotheses and their actual
disposition: new mechanism or model family, evidence inspected, smallest
discriminating test, readiness, execution identity, result, and decision changed.
Repeated proposals and renamed variants count once. Distinguish a quality
experiment from a source or feasibility check, and record why a ready alternative
was deferred. A new best score is not required for useful exploration; a negative
result can rule out an extension. Hypothesis classification and scientific impact
still require artifact review, and are not automatically scored by this command.

In the existing coordination plan, give each candidate a stable hypothesis ID
and its next evidence milestone. Record when it was proposed, reviewable and
ready to dispatch, then link its actual reservation/session or explicit deferral
and reason. Distinguish Scout introducing a route, adding a discriminating
prediction, changing a scheduling decision, and reconfirming an existing route.
For scheduling fairness, count only opportunities when an alternative was ready,
authorized and competing for the same available resource. No such opportunity
means no comparison is available; an unready or newly proposed route is not a
failed conversion. Source checks, numerical gates and quality experiments have
different milestones and must not share a single conversion-rate denominator.
