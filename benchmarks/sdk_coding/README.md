# Native SDK coding demo

This demo uses the controller's default explicit-input mode. Applications that
require Agents to retrieve their own knowledge can use the optional
`run_research(prepare_input=...)` boundary and attach knowledge tools to all
roles; see the [SDK execution contract](../../python/README.md#execution-contract).
The demo does not configure that application-owned knowledge service.
It also retains single-file Markdown publication. A whole-tree knowledge
application owns draft tools, immutable Evidence and atomic activation through
`publish_knowledge`; the SDK does not write through its live directory pointer.

From the repository root, on macOS with `/usr/bin/sandbox-exec` available:

```sh
uv sync --locked --project python
uv run --locked --project python python benchmarks/sdk_coding/run.py /private/tmp/research-sdk-demo
```

The output directory must be new and outside Git. Defaults are `--rounds 3`,
`--investigations 2`, `--experiments 1`, and `--seconds 180`; all must be positive.
The timeout bounds the whole study. Use a fresh directory for every invocation.

The default uses the SDK's `agents.testing.ScriptedModel` with native `Agent`,
`Runner`, structured outputs and function tools. It makes no model/network calls.
Scripted responders inspect actual role inputs and tool results; test outcomes
come from real restricted subprocesses, including an independent host evaluation.

Two independent human tasks start concurrently in separate candidate workspaces.
Two parallel Advocates advise a Judge, which admits incremental autonomous work
with a human dependency that blocks execution until its Review is published. The pool prioritizes
ready human tasks and protects them from Judge edits. Each investigation runs
Plan → Execute → fresh Review. Execute tools share the experiment semaphore;
independent host evidence uses the same limit. The dependent candidate starts
from valid reviewed evidence and still runs its own SDK tools and fixed tests.
The background Scout reads a **synthetic local** reference, not the web. A serial
asynchronous Knowledge Manager incorporates new Review/Scout batches into prior
Markdown notes, preserving record IDs, sources and limitations.

The default three investigations exercise both human paths and an autonomous
recheck. Review deliberately reports `not-achieved` and continues: passing these
fixed development tests does not establish a general research result. Smaller
round limits can finish before dependent work; larger limits are upper bounds,
not a promise to manufacture that many tasks.

Local tracing is configured before the first SDK call. Inspect `trace.jsonl`,
`research/result.json`, `research/pool.json`, per-record `plan.json`,
`evidence.json`, `record.json` and candidate files, planning receipts, Scout
reports, Knowledge Manager inputs and `research/research.md`. These contain
inputs and candidate source; keep the run directory private and outside commits.
Ordinary investigation failures produce per-record `error.json` receipts and
`research/result.json` includes a `failures` list; independent work continues,
while dependencies on failed tasks remain blocked. Manager failures retain prior
notes and pending Evidence, then retry the pending batch with new inputs. The
optional application diagnosis hook can retry sooner after a verified repair. Inspect these failures and
pool states before interpreting completion. Cancellation and application-declared
`FatalResearchError` still stop the study; see the [failure contract](../../python/README.md#execution-contract).

Explicit external mode uses an exact Chat Completions model ID:

```sh
OPENAI_API_KEY=... OPENAI_BASE_URL=https://example.invalid/v1 \
  uv run --locked --project python python benchmarks/sdk_coding/run.py \
  /private/tmp/research-sdk-external --model YOUR_AUTHORIZED_MODEL_ID
```

Set the standard environment variables to an authorized provider (omit
`OPENAI_BASE_URL` for the default OpenAI endpoint). External mode uses
`AsyncOpenAI(max_retries=0, timeout=30)`, 2,048 output tokens per request and at
most 12 turns per role call. It can incur provider charges and its decisions may
differ from scripted mode; the run timeout is not a spending cap. No external
model execution is required for this demo's local smoke.

This is a bounded process-local demonstration, not a durable service. DBOS,
arbitrary crash recovery, GPUs and a trace server are deferred. The generic
controller also supports application-owned stage correction and explicit safe
handoffs; this finite demo does not configure those callbacks. The existing macOS
subprocess boundary is specific to this fixture; portability and production
isolation are not demonstrated. No private dataset or holdout evaluation is used.
