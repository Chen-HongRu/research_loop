# Runtime design

The application uses the official pinned Codex SDK and CLI. A Study has one
coordinator thread, native specialist threads, an optional resource adapter and
versioned research documents. The reusable package is independent of the research
algorithm, dataset and metric.

## Research team

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
variable name, never the key value. Provider runs default to host delegation:
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
work and resource sessions, publishes its draft, and then either resumes the same
coordinator thread or completes the Study. Specialist contexts remain fresh.
`iterations/NNNNNN/` retains status and result; root records show the latest state.
Failure, STOP, unsettled resources or cleanup errors halt admission and retain the
draft. Explicit `run --continue` can resume after an operator stop; it does not
reset budget or establish that uncertain external effects are absent.

Publication copies regular draft files into a new independent version. It stages
publication metadata before atomically replacing the current pointer, checking
STOP at that boundary. The committed pointer permits recovery if root status was
not written after publication. Newer unfinished work is preserved. Publication
and iteration records do not guarantee recovery from arbitrary crashes.

## Resource and permission boundaries

| Boundary | Protected condition |
| --- | --- |
| Native authentication, exact model and permissions | Use the configured authentication (ChatGPT subscription by default, or an explicit OpenAI-compatible provider), model, delegation mode and effort without implicit provider fallback; prevent agent access to protected paths. Prompts cannot enforce filesystem isolation. |
| Host-delegated roles | Start every role in a new thread with a plain assignment; reject native delegation from a role, cap concurrency, and retain unfinished tasks before publication. |
| Shared resource ledger and lock | Reserve and settle the original authorization across allocations. A prompt cannot atomically reserve shared budget. |
| Original operation identity, UNKNOWN and STOP | Reconcile uncertain effects without duplicate dispatch; prevent new research dispatch after STOP. Physical receipts establish job state. |
| Single writer and atomic publication | Prevent concurrent publication and partial knowledge snapshots. The writer lock is host-local, not a cross-host ownership service. |

The Slurm adapter registers five lead-only tools. Children return commands and
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

An optional [efficiency collector](../references/efficiency.md) runs separately
from the controller. It reads native traces and resource records incrementally,
writes private aggregate observations outside the Study, and never calls a model
or allocates resources. Measurement phase labels are operator annotations;
observed differences do not establish a causal improvement or scientific value.

## Dashboard

`python -m research_loop.dashboard.server --study RUN` serves a read-only loopback
view of public messages, role activity, actual work records, result provenance
and resource metadata. Hidden reasoning and encrypted content are excluded. Role
cards come from native spawn linkage; host-delegated roles are attributed in
`roles.json` and in `efficiency`/`metrics`, not in the dashboard. Role inference
from task names and unknown/missing records are labeled explicitly.
Historical and current iteration results remain distinct. Raw tool details load
only when requested. Assets and API calls support a reverse-proxy path prefix;
external access control belongs to the deployment.

If `supervisor.json` identifies a systemd service, the dashboard resolves its live
`MainPID` and `ActiveState`; a saved PID must not override that service state.
Inspection failure is unknown, not proof of process exit. Unmanaged processes
and hosts without systemd use the recorded PID.
