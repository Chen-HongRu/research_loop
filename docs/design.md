# Runtime design

The application uses the official pinned Codex SDK and CLI. A Study has a Python
controller, specialist threads, an optional resource adapter and versioned research
documents. The default Router mode uses fresh, tool-free routing contexts and independent Group Runs; the legacy Lead mode uses one persistent Lead thread. The reusable package is independent of the research
algorithm, dataset and metric.

## Router Groups

The Router coordinates versioned, problem-scoped Groups through independent Group Runs. The reference workflow provides direction, experiment, synthesis and diagnosis capabilities. Synthesis authors the Study result and scientific knowledge manifest; Python validates and installs its explicitly exported files before the existing publication settlement. The legacy Lead and its host/native delegation remain separate from this mode. See the [Router Group contract](router-groups.md) for interfaces, isolation, handoff and recovery boundaries.

## Legacy Lead research team

[`instructions.py`](../src/research_loop/instructions.py) defines the coordinator
policy and final result schema. It includes the common contract and five role
templates from [`roles.md`](../src/research_loop/roles.md):

| Role | Responsibility |
| --- | --- |
| Scout | Primary-source research and alternative directions |
| Proposer | Falsifiable hypotheses, controls and executable plans |
| Experimenter | Research implementation, execution preparation and diagnostics |
| Analyst | Result interpretation, competing explanations and ablations |
| Critic | Independent scientific challenge and concrete evidence requests |

The coordinator manages dependencies, useful concurrency, resource dispatch,
tradeoffs and summaries. Native delegation uses a new native child with
`fork_turns="none"`, including revisions; host delegation starts a new thread per
assignment. Handoffs contain the common contract,
selected role, goal and project paths, relevant positive and negative evidence,
file ownership, output and completion condition. Children do not inherit or read
parent chats, resume old assignments or create further delegates. Necessary
scientific artifacts remain available to new contexts.

Role invocation order is dynamic. Direction decisions compare alternatives with
continuing the incumbent. Scientific proposals, interpretation and code belong
to specialists; the coordinator executes prepared commands and integrates their
reports. Role selection, fresh spawning and read-only review are prompt contracts
in native delegation; host delegation additionally caps concurrency, disables
native subagent tools, and fails a role that attempts native delegation. Children
inherit native filesystem permissions.

Assignments identify exact relevant evidence and completed work rather than
copying the entire knowledge history. Roles start with the goal, project
instructions, short knowledge index and assigned evidence, expanding retrieval
when needed. A useful preliminary audit can finish before a separate result
analysis starts; roles are not created merely to wait for dependencies. Runners
retain complete evidence in artifacts and emit concise summaries for coordination.

## Iterations and state

`init` copies the goal and freezes configuration without a model call. It can also
freeze an explicit OpenAI-compatible provider (name, base URL, API-key environment
variable, model catalog) and a delegation mode; the run records store only the
variable name, never the key value. In Lead mode, provider runs default to host delegation:
the lead receives `delegate_task`/`wait_tasks`, and the host runs each role in a
new plain-message thread with the canonical role contract. Host-mode catalogs
disable native Codex subagent tools; native spawns remain available as an
explicit comparison mode. Each turn receives paths to the goal, project, work, draft and published
index, a resource snapshot, iteration number and previous result. The agent reads referenced files
with native tools; their contents are not automatically embedded in that input.
`inspect` exposes these application instructions, schema, tools and permissions.

The result contains `summary`, scientific `goal` (`achieved`, `not-achieved`, or
`unknown`), `evidence`, `limitations`, `next_action`, `study_action` (`continue` or
`stop`) and string `stop_reason`. The host validates shape and follows the explicit
Study action without judging scientific quality. A diagnostic ending does not
end the Study. Budget is a ceiling, not a requirement to spend it.

One writer lock spans all iterations on a host. Each iteration settles native
work and resource sessions and publishes its draft. Lead mode resumes the same coordinator thread when continuing; Router mode creates fresh routing contexts. Specialist contexts remain isolated.
`iterations/NNNNNN/` retains status and result; root records show the latest state.
Failure, STOP, unsettled resources or cleanup errors halt admission and retain the
draft. Explicit `run --continue` can resume after an operator stop; it does not
reset budget or establish that uncertain external effects are absent.

Publication copies regular draft files into a new independent version. It stages
publication metadata before atomically replacing the current pointer, checking
STOP at that boundary. The committed pointer permits recovery if root status was
not written after publication. Newer unfinished work is preserved. Publication
and iteration records do not guarantee recovery from arbitrary crashes.

`publication.py` owns the two iteration publication entry points: `commit`
activates the knowledge version and acknowledges its human request; `recover`
replays acknowledgement and repairs stale result/status records from the active
version. The controller owns result validation, resource settlement, admission,
and final status recording on every exit. Both entry points run under its Study
writer lock. Filesystem publication and input revision locking remain in
`state.py` and `human_input.py`.

## Execution environment

An optional initialization-time environment JSON selects an existing local
Python interpreter and declares an optional resource interpreter/package map.
Local paths are normalized relative to that file without resolving away the
virtualenv interpreter symlink. The normalized settings are copied into Study
configuration. Missing settings retain the controller interpreter default.

Inspection and iteration admission observe local executable, Python version,
prefixes and installed distribution metadata using the selected interpreter's
standard library. They do not import numerical libraries or access resource
hosts. The coordinator and fresh role contexts receive the same snapshot;
resource declarations are explicitly unverified and are separate from resource
configuration, authorization and physical receipts. The selected interpreter
and prefixes are readable through the native permission profile, with protected
credentials/control paths still denied. The isolated shell prioritizes its bin
directory, identifies a virtualenv when applicable and disables user-site imports.

This is environment selection and disclosure, not dependency installation,
environment repair, scientific validation or a resource grant. Device debugging
uses the existing authorized resource tools and budget. Running deployments are
not modified or restarted by initialization or a repository update.

## Resource and permission boundaries

| Boundary | Protected condition |
| --- | --- |
| Native authentication, exact model and permissions | Use the configured authentication (ChatGPT subscription by default, or an explicit OpenAI-compatible provider), model, delegation mode and effort without implicit provider fallback; use ordinary workspace-write with default reads, work/temporary writes and shell network disabled. Read scope and conversation isolation remain task contracts, not OS secrecy. |
| Host-delegated roles | Start every role in a new thread with a plain assignment; reject native delegation from a role, cap concurrency, and retain unfinished tasks before publication. |
| Shared resource ledger and lock | Reserve and settle the original authorization across allocations. A prompt cannot atomically reserve shared budget. |
| Original operation identity, UNKNOWN and STOP | Reconcile uncertain effects without duplicate dispatch; prevent new research dispatch after STOP. Physical receipts establish job state. |
| Single writer and atomic publication | Prevent concurrent publication and partial knowledge snapshots. The writer lock is host-local, not a cross-host ownership service. |

In Lead mode, the Slurm adapter registers five lead-only tools. Children return commands and
artifact requests to the coordinator:

| Tool | Responsibility |
| --- | --- |
| `gpu_status` | Inspect budget, reservations and sessions without dispatch |
| `gpu_exec` | Execute in the original authorized allocation; no automatic retry |
| `gpu_poll` | Read incremental session output and optionally send stdin |
| `gpu_reconcile` | Query original job identity and terminal accounting |
| `gpu_release` | Release the allocation and reconcile exact accounting |

The ledger and its lock must be outside writable work/draft directories. UNKNOWN
blocks duplicate allocation. Cleanup attempts do not prove a remote job stopped;
original receipts remain authoritative. The adapter accounts for its allocations,
not unrelated jobs submitted elsewhere by the account.

The host contains no research task pool, fixed role scheduler, scientific
acceptance gate, runtime self-repair or automatic version activation. Host transfer
uses the [Linux deployment procedure](../references/linux-deployment.md).

## Human experiment input

The user authors `inputs/human.md` directly or through a separately authorized
Codex session. The operator `experiment` and `edit-experiment` commands share
`inputs/human.lock` with admission and acknowledgement. Direct editing during
execution must use this lock; arbitrary unlocked editor writes are not coordinated.
Research Lead and specialist write roots exclude inputs and iteration snapshots in the supported layout. Do not place the Study inside an agent-writable ancestor: ordinary workspace-write does not add protective deny overlays. Only the controller consumes requests.

At iteration admission, a nonempty UTF-8 request is read once, identified by its
file revision and text, archived under the iteration and supplied as
`human_request` before direction selection. Missing or empty input leaves
autonomous planning unchanged. The request selects a priority experiment within
the existing goal, protocol and resource authorization. Mid-iteration changes
belong to later admissions. The coordinator delegates design, implementation and
analysis through the existing roles; it returns a matching request ID, disposition,
evidence and reason independently of Study action and goal status.

Completion requires execution, analysis and summary, including negative findings.
The host validates disposition shape and identity, not scientific correctness.
Blocked requests halt for intervention; unfinished requests remain pending.
Publication metadata carries the admitted snapshot and result. Only activated
publication commits a completed request. Afterwards, acknowledgement clears only
the still-matching revision under the input lock. Recovery replays acknowledgement
from the active publication before admission, preserving any newer edit and
avoiding replay of published work. Failed or unpublished work is not consumed.
See the [contract](human-experiment-input.md) and
[operator interface](../references/operations.md#human-experiments).

An optional [efficiency collector](../references/efficiency.md) runs separately
from the controller. It reads native traces and resource records incrementally,
writes private aggregate observations outside the Study, and never calls a model
or allocates resources. Measurement phase labels are operator annotations;
observed differences do not establish a causal improvement or scientific value.

`identity.resolve` supplies offline efficiency reports and incremental metrics
with Study membership, task names, roles and attribution sources from already-read
metadata and Host role mappings. Readers retain input validation, diagnostics,
incremental offsets and cached-task update behavior. Attribution sources remain
internal and add no report or checkpoint fields. The dashboard retains its
own role interpretation.

`token_usage.accept_record` supplies the shared request-usage validation and
Study-wide response ID deduplication rule. Offline schema version 2 and continuous
metrics use accepted receipts as their Token totals; cumulative notifications
remain separate reconciliation evidence. Readers apply the notification-time
window after accepting receipts, so pre-window duplicates are not counted again.
Consumption evidence without any valid receipt marks that thread's usage
incomplete; this does not prove that other threads have every request recorded.
Receipts without any recognized usage field are rejected without reserving their
ID. Continuous reports and checkpoints use version 2; version 1 outputs remain
historical and require a fresh output directory to recompute under this rule.

`sample_store.publish_pending` completes a checkpointed sample under the
collector's single-writer lock, both after a poll and during restart. Collectors
own sample calculation, enrichment and history projection. Their shared poll
path first saves the pending sample and source offsets in the checkpoint.
Publication checks the committed journal length, truncates any uncommitted tail,
appends and fsyncs the projected history, replaces `latest.json`, then saves the
new journal position without the pending sample. A journal shorter than the
committed position is an error. Both collectors retain their version 2 formats.

## Dashboard

`python -m research_loop.dashboard.server --study RUN` serves a read-only loopback
view of public messages, role activity, actual work records, result provenance
and resource metadata. Hidden reasoning and encrypted content are excluded. Role
cards come from native spawn linkage or explicit host-delegation mappings in
`roles.json`. Host lifecycle status comes from that mapping; public replies come
from the mapped native history. Missing histories retain a role card with no
invented reply. A thread appears once even when both sources identify it. Explicit
host roles, native roles inferred from task names, and unknown/missing records are
labeled separately. Host start events supply iteration timing when history is
unavailable. These projections do not change either delegation runtime.
Historical and current iteration results remain distinct. Raw tool details load
only when requested. Assets and API calls support a reverse-proxy path prefix;
external access control belongs to the deployment.

Repeated `--study` arguments register explicit directories. `--studies-dir` opts
into dynamic discovery of immediate, non-symlink children with a regular
`status.json`. `/api/studies` lists stable opaque IDs and recorded status;
All data routes accept a `study` ID. Omitting the ID
selects the first registered Study for compatibility; invalid IDs return 404.
Each Study owns its incremental readers. The browser scopes tool caches and
in-flight responses to its selection and preserves that ID in the URL.

The full `/api/state` response includes a numerically ordered iteration history projected
from archived status and result records, with source paths and explicit missing
results. Result fields are allowlisted; no reasoning, encrypted content or raw
transcripts enter this projection. Legacy root results are used only without
archive directories. The timeline displays reported summaries, goal judgments,
continuation decisions, next steps, limitations and evidence references; it does
not infer experimental success or synthesize new conclusions.

The browser uses `/api/overview` for bounded current state, `/api/history` for ten
newest-first iteration previews per page, and `/api/iteration` for one full public
result. `/api/agents` returns twenty task previews per page, filtered by role and
iteration; `/api/agent` returns the selected task. `/api/activity` returns paged
coordinator and retained tool previews; `/api/message` and `/api/tool` load details
on demand. These projections are assembled without first serializing full
transcript bodies. Context uses the full snapshot on navigation. Operational
refreshes keep page/filter/selection state; responses are scoped to Study and
current navigation to prevent late responses replacing another selection.

Publication display reads the existing activated knowledge pointer and matching
publication metadata. A reported result alone does not establish publication;
older iterations can show a separate historical publication record when their
completed archive status and own version match publication metadata. Metadata
alone does not prove activation. Legacy or unavailable evidence remains unknown.
This adds no publication writer
or controller recovery action. `/api/metrics` preserves schema version and token
authority alongside measurement coverage, and may read optional behavior
observations from `metrics/behavior/latest.json`. Collectors remain independent;
the dashboard does not start them or infer missing measurements.

If `supervisor.json` identifies a systemd service, the dashboard resolves its live
`MainPID` and `ActiveState`; a saved PID must not override that service state.
Inspection failure is unknown, not proof of process exit. Unmanaged processes
and hosts without systemd use the recorded PID.
