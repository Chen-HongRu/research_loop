# Runtime framework decision — Stage A (#94 / #95)

Default candidate: **Pi Durable 1.5.0**, with its published native coding tools,
public Task/checkpoints and Node SQLite storage. DBOS + Pi remains a selection
spike only. Stage B must use the winner; production still uses the existing
runtime until Stage B and the Stage C handoff have passed.

This decision combines real framework/storage/native-tool fault tests with a
synthetic provider, and separate user-authorized **DeepSeek Flash** coding runs.
Both real coding runs passed independent CPU tests. It establishes these bounded
coding/recovery behaviors, not general research benefit. No GPU, remote compute
platform or production switch has been exercised by this evidence.

## Reproduce

The two implementations, exact installation/start/restart/status/cancel commands
and fault matrix are in [the spike guide](../spikes/persistent-coding/README.md).
Versions come from published npm packages, pinned by `package-lock.json`:

| Component                           | Fixed version                    | Observed local environment                       |
| ----------------------------------- | -------------------------------- | ------------------------------------------------ |
| Pi Durable / Pi SDK / Pi AI / Chord | 1.5.0                            | Node 26.0.0, macOS arm64                         |
| DBOS TypeScript SDK                 | 5.4.0                            | PostgreSQL 18.6, loopback-only temporary cluster |
| CPU fixture                         | Repository source/test constants | uv 0.11.19, Python 3.12.5                        |

The PostgreSQL verification used actual prebuilt PostgreSQL binaries via
`embedded-postgres@18.6.0-beta.19` and the matching darwin-arm64 package, outside
the repository. This is verification tooling, not a production dependency.
Docker was installed but its daemon was unavailable; no remote database was used.
Other operating systems, Node versions and storage backends are unverified.

## Observed recovery

Each normal run made seven synthetic model requests, two reads, two edits and two
bash commands. The first bash test failed on reversed bounds; its actual failure
entered the next model input, and the corrected candidate passed an independent
host-side test. Native prompts/tool declarations differ between frameworks.

| Scenario                                         | Pi Durable                                               | DBOS + Pi                                                            |
| ------------------------------------------------ | -------------------------------------------------------- | -------------------------------------------------------------------- |
| Completed restart                                | Original result; zero new requests/tools                 | Original step result; zero new requests/tools                        |
| Host killed during model request                 | One interrupted request, then seven requests to complete | Original session uncertain; no new request; manual reconciliation    |
| Host killed after actual edit, before result ACK | Interrupted tool result; same conversation completes     | Original file retained; no new request; manual reconciliation        |
| Host killed during bash after marker append      | Interrupted tool result; marker remains once             | Original process/effect retained; marker once; manual reconciliation |
| Episode answer saved, outer ACK lost             | Stable submission ID returns original answer             | Matching completed Pi session returns original answer                |
| STOP / original deadline                         | Aborts owned framework work; no new request on restart   | Aborts Pi; rejects restart; status remains available                 |
| Normal SIGTERM                                   | Closes framework, pending work can resume                | Aborts Pi, retains session, requires manual reconciliation           |
| Workspace missing                                | Explicit error, no replacement or request                | Explicit error, no replacement or request                            |
| Model claims success without fixing program      | Independent tests fail; run exits nonzero                | Independent tests fail; workflow/CLI fail                            |

The bash fault has a two-second bounded child. Tests observe its end and one
marker; this does not establish that arbitrary killed-host subprocesses stop.
No command/side effect rollback or exactly-once execution is claimed. A native
model may explicitly ask to repeat a command, requiring a separate controlled
tool for high-cost operations in Stage B.

Raw transcripts and temporary workspaces are outside Git. The checked-in
`observations.json` contains only synthetic fixture counters and elapsed times;
elapsed times include process startup, deliberate sleeps and fault handling.
They are not a throughput comparison. See
[observations](../spikes/persistent-coding/observations.json).

The authorized real runs use the locally configured `deepseek-flash` model on the
same private provider, with requested thinking `off` (the registry marks it
non-reasoning). Provider configuration and credentials stay outside Git. Each
candidate made four real model requests, two reads, one edit and one bash test;
both passed, and each completed restart added zero requests/tools. Measured run
durations were approximately 5.2 s and 9.5 s, respectively, but the native prompts
differ and one sample is not a latency or coding-ability comparison. Provider
token usage is saved in `realCoding`; verified prices/actual billing and server
model routing/thinking enforcement are unmeasured. An earlier eight-request pilot
exposed a needless model-JSON formatting requirement; the final adapter constructs
structured results from trusted tests and keeps model text separately. Total real
requests across the pilot and final runs: 16.

## Custom state and maintenance cost

Both spikes count their runtime glue, shared runner/fixture, prompt, schema and
package/configuration. The synthetic provider and tests are separate. The
parser-based command `node loc.mjs` counts nonblank token-bearing source lines,
excluding comments, generated lockfile and prose; multiline prompts and inline
Python programs remain included. The report's `loc` field records the measured
totals and exact file breakdown: **517 runtime/config lines for Durable, 640 for
DBOS + Pi**, each including 206 shared lines; the synthetic provider and tests
add 591 lines, and the LOC reporting tool adds 70. These are minimum spikes, not replacements for
the historical approximately 14,858-line runtime or promises of equivalent APIs.

| Responsibility                     | Pi Durable                                                                                                | DBOS + Pi                                                                                                                                                                   |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Project-owned execution structure  | One public Task, three checkpoints: start/coding/verify                                                   | One workflow, two steps: episode/verify                                                                                                                                     |
| Persistent identity                | One framework document points to the Task; child conversation and request ID                              | Workflow/executor identity plus one Pi session identity entry                                                                                                               |
| Authoritative framework storage    | One SQLite database, including model/tool tasks                                                           | PostgreSQL step state plus Pi JSONL conversation                                                                                                                            |
| Shared inputs/control              | Original config/workspace marker and STOP                                                                 | Same inputs/control                                                                                                                                                         |
| Diagnostic storage                 | JSONL trace; not execution authority                                                                      | Same trace; not execution authority                                                                                                                                         |
| Recovery-specific custom decisions | Reattach stable Task/submission; trust native unsafe-tool interruption                                    | Claim canonical Pi log, strictly validate identity/version/tail/final answer, reuse completed or stop for manual reconciliation                                             |
| Required concepts                  | Harness, Task/checkpoint/commit, ownership, submission identity, tool replay policy, ExecutionEnv, SQLite | Workflow/step recovery, executor/application version, cancellation, PostgreSQL, AgentSession, SessionManager/tree, exclusive session claim, terminal-session reconciliation |

Both explicitly handle unavailable model, missing workspace, STOP/deadline,
empty final answer and failed independent tests. Durable relies on framework
task outcomes for failures; DBOS additionally handles empty/damaged/foreign or
unfinished Pi sessions. Neither recreates Coordinator, dispatch journal, worker
supervision or a generic dual-backend API. Normal debugging requires the shared
input/provider modules and the candidate's one implementation; failure handling
also requires the framework concepts listed above.

`npm run check` passed, and the real PostgreSQL run of both candidates' tests
passed **25/25**, with no skipped cases. The independent review's false-success
finding is covered by the negative test in the final matrix. No CI workflow is
configured in this repository; these are local checks. DBOS/pg emitted an upstream
concurrent-query deprecation warning; no pg@9 compatibility is claimed.

**Why Durable:** the observed process-crash recovery already reaches model/tool
tasks and durable submissions, so the app can remain one small Task. Local
deployment needs one database file and no PostgreSQL service or second session
completion protocol. DBOS is a viable outer workflow, but its step boundary
cannot resolve an interrupted Pi episode; meeting stronger automatic coding
recovery would add custom internal machinery contrary to this refactor's goal.

The common credential/model facade currently imports coding-agent's
`ModelRuntime` for both candidates. This dependency and its glue remain counted;
the winning spike does not claim to have eliminated the coding-agent package.

**Accepted limits:** Pi Durable labels its API Experimental and may change
between releases; upgrades need this bounded matrix and a new version decision.
SQLite WAL `synchronous=NORMAL` provides process-crash recovery, not a guarantee
against losing the newest commits on power/host failure. One writable host and
stable filesystem paths are assumed. SDK extensions/tools need an explicit
Durable extension/ExecutionEnv adapter; coding-agent skills/resource discovery
are not automatically imported by this spike. Multi-host, production deployment,
OAuth behavior, other model providers and arbitrary external effects remain unverified.

## Stage B / C gates

Stage B can directly reuse the fixed Harness, SQLite storage, native tools,
Task/owned-conversation creation, stable submission IDs and cancellation path.
Its additional contract is fixed evaluation, trusted receipts, independent Review,
dynamic file Knowledge with durable serial Manager work, and a controlled
experiment tool. A new read may see a new atomic file; no call-level knowledge
snapshot or duplicate SQL/file authority is required.

The user authorized the existing Pi DeepSeek Flash API configuration for testing;
a real two-round CPU research run is still required for #96. Its protection boundary must use an actual execution
environment, because the Stage A disposable directory is not a sandbox. Stage C
must await that acceptance before deleting old code or changing defaults. Current
legacy entry-point inventory is preparation only; no Study/service has migrated,
no old writer or restart policy has been stopped, and #97/#94 are not complete.

## Primary references

- [Pi Durable 1.5.0 README](https://github.com/earendil-works/pi/blob/v1.5.0/packages/durable/README.md)
- [Pi SDK 1.5.0](https://github.com/earendil-works/pi/blob/v1.5.0/packages/coding-agent/docs/sdk.md)
- [DBOS 5.4 release](https://github.com/dbos-inc/dbos-transact-ts/releases/tag/v5.4)
- [DBOS workflow management](https://docs.dbos.dev/typescript/tutorials/workflow-management)
- [DBOS step semantics](https://docs.dbos.dev/typescript/tutorials/step-tutorial)
