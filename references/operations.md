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

`inspect` shows the main instructions, output schema, native permission plan, resource snapshot and tool schemas without a model call, account access or ledger write. The final result separates scientific `goal` from required `study_action` (`continue` or `stop`) and string `stop_reason`. A finished diagnostic or Review does not itself stop the Study. The lead either defines the next falsifiable experiment or names a Study-level reason to stop: overall goal satisfaction, exhausted resources, an external block, or no useful justified action remaining. The host follows that action without judging scientific quality.

For `continue`, the host settles and closes native and resource sessions, publishes the knowledge snapshot, then resumes the same original lead thread for the next iteration automatically. One writer lock spans all iterations. `iterations/NNNNNN/status.json` and `result.json` retain iteration history; root `status.json` and `result.json` show the latest state and returned result. `iteration-completed` means the snapshot was published and the Study intends to continue; `completed` means an explicit Study stop. A plain `run` returns a cached completed Study. Use `run RUN --continue` explicitly to continue a completed Study, including one created by the earlier single-iteration runtime, or to clear STOP. Original pre-upgrade status and result files are archived before replacement, and the original thread and resource ledger are retained. An interrupted published iteration advances to the next number; unfinished work resumes its original thread. STOP after publication preserves that iteration's archive and durable `next_iteration`, so continuation starts the following iteration without overwriting the published result.

Use the original ledger and job identity across attempts. An unknown submission must be reconciled before another allocation is made. Failure, unknown resource state or cleanup failure retains the draft and result without activating that iteration's knowledge or starting another iteration. STOP prevents new research dispatch, including between iterations, and attempts native and child cleanup; the physical resource receipt remains authoritative, because stopping a local agent does not prove a remote job has terminated. No failed or uncertain effect is automatically retried.

The active knowledge version has host publication metadata under `knowledge/publications/`. If interruption occurs after activation but before root status is recorded, the next invocation recovers the committed iteration from matching metadata without replaying it; a newer unfinished iteration is preserved. A physical STOP still requires explicit `--continue`. STOP detected during snapshot copying prevents activation and retains the draft.

The host records physical events and resource receipts. The lead coordinates research and dynamically invokes Scout, Proposer, Experimenter, Analyst and Critic as needed. Each invocation is a fresh native subagent with `fork_turns="none"` and a self-contained task; existing role contexts are not resumed, reused or followed up. Scientific proposals and analysis are performed by the roles, while research resource tools remain lead-only. Structured output validates shape, not scientific correctness. The runtime does not promise automatic recovery from arbitrary crashes. See [design](../docs/design.md) and the [history index](../docs/runtime-retirement.md).
