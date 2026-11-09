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

## OpenAI-compatible provider

`init` can point the same native harness at an OpenAI-compatible Responses endpoint instead of the Codex ChatGPT subscription:

```sh
uv run research-loop init RUN --project PROJECT --goal GOAL \
  --provider NAME --provider-base-url https://provider.example/v1 \
  --api-key-env PROVIDER_API_KEY --model MODEL --effort medium \
  --model-context-window 131072
```

`--provider`, `--provider-base-url` and `--api-key-env` are required together, and `--model` is then required. Only the environment variable name is recorded; `config.json`, `call.json` and the event journal never contain the key value. Initialization fails early when the variable is unset, and `run` rechecks it before launching the native process. The key is forwarded only through the isolated process environment, not the agent shell or the sandbox filesystem policy.

Unless `--model-catalog FILE` points at an existing Codex catalog, `run` generates `RUN/native/models.json` for the configured model, effort levels and `--model-context-window`. Generated catalogs set `multi_agent_version = "v1"` and `supports_search_tool = false`: non-OpenAI Responses gateways cannot decrypt the V2 `encrypted_content` task payload, so V1 delegation is used to deliver each role assignment as a plain user message. The harness disables Codex V2 delegation and hosted web search in provider mode. A supplied catalog must declare the same `multi_agent_version = "v1"` and `supports_search_tool = false` for the configured model; `run` rejects one that does not. Roles still run in fresh contexts because V1 `spawn_agent` does not inherit the lead conversation unless asked to. A lead or Scout that needs external sources must report the missing retrieval instead of citing recalled material.

`inspect` reports the provider name, base URL, API-key variable name and catalog path without a model call. Each iteration's `call.json` records the effective native permissions, including `web_search: false`. `verify` accepts this mode without a ChatGPT account but still requires the configured model and reasoning effort to appear in the catalog. Provider mode is a separate execution baseline: do not present its results as equivalent to the subscription model without a controlled comparison.

`inspect` shows the main instructions, output schema, native permission plan, resource snapshot and tool schemas without a model call, account access or ledger write. The final result separates scientific `goal` from required `study_action` (`continue` or `stop`) and string `stop_reason`. A finished diagnostic or Review does not itself stop the Study. The lead either defines the next falsifiable experiment or names a Study-level reason to stop: overall goal satisfaction, exhausted resources, an external block, or no useful justified action remaining. The host follows that action without judging scientific quality.

For `continue`, the host settles and closes native and resource sessions, publishes the knowledge snapshot, then resumes the same original lead thread for the next iteration automatically. One writer lock spans all iterations. `iterations/NNNNNN/status.json` and `result.json` retain iteration history; root `status.json` and `result.json` show the latest state and returned result. `iteration-completed` means the snapshot was published and the Study intends to continue; `completed` means an explicit Study stop. A plain `run` returns a cached completed Study. Use `run RUN --continue` explicitly to continue a completed Study, including one created by the earlier single-iteration runtime, or to clear STOP. Original pre-upgrade status and result files are archived before replacement, and the original thread and resource ledger are retained. An interrupted published iteration advances to the next number; unfinished work resumes its original thread. STOP after publication preserves that iteration's archive and durable `next_iteration`, so continuation starts the following iteration without overwriting the published result.

An allocation with no job identity can be reconciled as `NOT_DISPATCHED` only when both scheduler queries are empty and its protected allocator log contains exactly the OpenSSH banner-timeout diagnostic. Banner exchange precedes authentication and remote command execution. Missing accounting, partial or mixed output, and other transport failures remain UNKNOWN. This check runs only during reconciliation, reading at most 4,097 bytes from the original local log; it does not resubmit an allocation.

Use the original ledger and job identity across attempts. An unknown submission must be reconciled before another allocation is made. Failure, unknown resource state or cleanup failure retains the draft and result without activating that iteration's knowledge or starting another iteration. STOP prevents new research dispatch, including between iterations, and attempts native and child cleanup; the physical resource receipt remains authoritative, because stopping a local agent does not prove a remote job has terminated. No failed or uncertain effect is automatically retried.

The active knowledge version has host publication metadata under `knowledge/publications/`. If interruption occurs after activation but before root status is recorded, the next invocation recovers the committed iteration from matching metadata without replaying it; a newer unfinished iteration is preserved. A physical STOP still requires explicit `--continue`. STOP detected during snapshot copying prevents activation and retains the draft.

The host records physical events and resource receipts. The lead coordinates research and dynamically invokes Scout, Proposer, Experimenter, Analyst and Critic as needed. Each invocation is a fresh native subagent with `fork_turns="none"` and a self-contained task; existing role contexts are not resumed, reused or followed up. Scientific proposals and analysis are performed by the roles, while research resource tools remain lead-only. Structured output validates shape, not scientific correctness. The runtime does not promise automatic recovery from arbitrary crashes. See [design](../docs/design.md).

## Efficiency diagnostics

For a restartable, continuously running collector and before/after comparisons,
see [continuous measurements](efficiency.md). The command below is a one-time audit.

```sh
uv run research-loop efficiency RUN
uv run research-loop efficiency RUN --since 2026-01-01T08:00:00+08:00 --until 2026-01-01T12:00:00+08:00
```

This operator command reads existing `events.jsonl`, the lead and confirmed
descendants under `native/sessions/`, and the frozen configuration's optional
`resource.ledger_path` (falling back to `RUN/gpu-budget.json` for older records). It prints
aggregate JSON without a model call, remote access, Study writes, lifecycle hooks
or changes to the running controller. It scans trace files only when requested;
it does not scan datasets, caches or checkpoints. `--sessions-dir PATH` selects
an operator-supplied native trace snapshot; `--ledger-path PATH` overrides the
configured ledger when auditing a copied snapshot. Keep traces and reports private.

The default period starts at the first recorded `lead.input` and ends at the
command's start time. Explicit bounds require a timezone and use
`since <= event_time < until`. Usage is attributed by its notification time,
without prorating responses crossing a boundary. Cumulative token notifications
are differenced per thread, with duplicate values removed. Input already includes
cached input; output already includes reasoning. The separately deduplicated
response-usage records are a cross-check, not another total to add: differences
can expose compaction usage or boundary timing. Missing records, partial tails
and counter resets are reported as coverage limitations.

Delegate roles are inferred from task names. Tool counts include top-level native
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
