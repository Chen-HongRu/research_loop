# Runtime retirement and history

The root Node package is the active source entry. No hidden compatibility layer or automatic full migration platform is supplied.

| Retired or retained surface | Prior caller | Source decision and current path |
| --- | --- | --- |
| `src/research_loop/` serial and SQLite controllers | Python API and CLI consumers | Entire tree retired; use the root Node CLI for new authorized Studies and `history` for supported read-only snapshots. |
| Root Python package, lock, launcher, examples and old tests | Installed wheel, console script and example users | Retired; use the root Node package and runtime tests. Existing installations require operator inventory. |
| Native and script runtime application layers | Pi role and script-backend callers | Retired entirely; the local CPU runtime does not replace remote backend operation or settlement. |
| ProTok controller, backend, service, supervisor, deployment/automatic restart and tests | ProTok operators and active jobs | Source retired; reconcile deployed services and original jobs at their actual backend. README and assets remain historical only. |
| Cactus bridge, smoke and meta-loop runners | Cactus controller callers | Retired; independent `prepare.py`, `check_contract.py`, `evaluate.py`, `train.py` and task environment remain. |
| Persistent-coding spikes | Framework investigation readers | Historical evidence only, not a production entry. |

Within the retired `src/research_loop/runtime/` tree, the responsibility mapping is:

| Removed modules | Prior callers | Replacement or boundary |
| --- | --- | --- |
| `coordinator`, `dispatch`, `invocations`, `worker_process`, `backend_scheduler` | CLI and application runner | Pi Durable Task ownership/checkpoints; official pi-env child lifecycle. |
| `store`, `controls`, `model_requests`, `updates`, `format`, `status` | Coordinator, model bridge and status views | Framework SQLite, original authorization and immutable research records. No second phase/attempt journal. |
| `application`, `role_assembly`, `role_access`, `call_context`, `context*`, `handoff_scope`, `source_scope` | Dynamic applications and role assemblers | Fixed role tools and explicit workspace/read-only grants in `runtime/src/engine.ts`. Native/script transport and tool bridges are deleted with their adapters. |
| `knowledge`, `live_knowledge`, `knowledge_manager`, `atomic_directory`, `publication*`, `projections` | Research roles, Manager and dashboards | Reviewed records plus one serial Manager publishing a Markdown file by atomic replacement. |
| `task_pool`, `planning`, `planning_state` | Scheduler and planning CLI | Bounded task configuration and framework Loop/Manager tasks. |
| `operations`, `operation_contract`, `measurements`, `backends` | Backend scheduler and adapters | Local CPU `run_experiment` intent, original-job query/bind/collect and trusted scoring. Remote effects require their original platform. |
| `migration*`, `legacy*`, `dashboard`, `config_yaml`, `files`, `cli` | Migration/status commands, browser views and Python entry | Supported metadata and original-text `history` inspection only; no old execution imports or automatic state migration. |

The new [Cactus remote training path](../integrations/cactus_remote/README.md) is a separate fixed-program adapter with [two-round evidence](autodl-cactus-evidence.md): Pi and research agents remain local on macOS, while only training and prediction use the existing AutoDL instance. It does not restore the retired bridges or establish that existing services or jobs have been migrated.

The table records source decisions, not verified deployment state. Migration of each caller requires an authorized operator decision; the new local runtime is not a drop-in replacement for old external effects.

The user selected a new Cactus Study for deployment acceptance: local Pi and research control, with fixed training in a fresh AutoDL project subdirectory. The installed Node release contains none of the retired writable Python, native/script or ProTok entry points; the Study uses the new native format and its two original GPU operations were collected to terminal results. This validates the selected new deployment. No existing active Study was selected for migration. Other installed wheels, services, supervisors and original job IDs require their own operator inventory and are outside this run's evidence. [#94](https://github.com/Chen-HongRu/research_loop/issues/94) and [#97](https://github.com/Chen-HongRu/research_loop/issues/97) remain open while the draft delivery awaits review and merge.

## Read-only history

First stop the original writer and its automatic restart under the operator's control. Make a complete, consistent snapshot using the platform's native method, preserving SQLite WAL and associated files; copying only the main database is insufficient. Make the snapshot root and files read-only, including effective write access, without changing permissions on a running Study. Keep real hosts, credentials and paths outside the repository.

```sh
node dist/cli.js history /path/to/read-only-snapshot
node dist/cli.js history /path/to/read-only-snapshot --file relative/path
```

`history` inspects supported legacy JSON, Markdown and schema-8 SQLite snapshots without importing old code, initializing a Study or writing STOP. `--file` reads the specified relative UTF-8 text as original text, bounded to 4 MiB. Writable, unsupported or incomplete snapshots and missing tables return unknown; unknown is not evidence that no deployment or job is active. Verify original job references with the actual backend, including null job IDs with `external_open` and unexpected IDs, which remain unknown until reconciled. History inspection does not close a remote platform job, rerun training or resubmit a model request.

Do not create a new Study to bypass unknown old effects or refresh deadlines and resources. For rollback, stop new admission first and reconcile existing effects; do not restart an old writer to replay them. Any deployment or migration requires separate authorization and a plan grounded in the actual environment.

## Source and package verification

The stage C [observations](../runtime/retirement-observations.json) record 33 native tests passed with no skips on local macOS arm64, including legacy command refusal before writes, read-only schema-8 WAL inspection, original-job recovery and Manager publication. The built application archive was extracted into an independent directory and installed using `npm ci --omit=dev`. Its compiled CLI and registered bin passed `doctor`; a real compiled CPU job produced a valid trusted receipt, started once and returned the same receipt when collected again. The archive smoke made no model call and used no GPU or remote platform. The earlier [DeepSeek Flash research](durable-research-evidence.md) remains separate evidence.

The initial stage C snapshot measured 3,205 token-bearing core source lines, including CLI, doctor, history, packaging, configuration, SDK glue, prompts and local platform code. The CPU task adapter is 220 lines; tests and synthetic provider are 1,883, Python tests 53 and reporting tool 110, counted separately. The counting method and per-file totals are in that snapshot; later remote-adapter evidence is reported [separately](autodl-cactus-evidence.md). In the snapshot, generated lockfiles, compiled copies and prose are excluded. Execution authority remains the framework Task/checkpoints: seven Loop phases and two Manager phases. The application keeps research records and original CPU submission identities; history adds no execution state machine. Knowledge Markdown is a derived reading aid. Required framework concepts are Task/checkpoint, owned conversation/tool replay, cancellation, SQLite storage and the official remote execution environment.

The removed source no longer supplies a Python console script, dynamic application loader or supervisor. The initial source/package check did not inspect a deployed target. The later [new-Study deployment evidence](autodl-cactus-evidence.md) records the user-selected local-Pi/AutoDL path; it does not claim that other installed wheels, services, launchers or Studies stopped or migrated.

## Historical material

The [pre-retirement source and guides at baseline](https://github.com/Chen-HongRu/research_loop/tree/0d81cbc44025e0e8742a9818fc18326484023790) are historical reference only. The dated verification reports, [framework decision](runtime-framework-decision.md), [stage B evidence](durable-research-evidence.md), and [showcase](showcase.md) preserve their original scope and do not validate the current deployment.
