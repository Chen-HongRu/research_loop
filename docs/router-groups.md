# Router Groups

This document records the abstract contract approved by the user in this chat, questions Q1–Q15, and the subsequent authorization to implement it. The design is opt-in; the default lead coordinator retains its existing behavior.

## Groups and catalog

A Group specializes in a problem scope and may contain one or more experts. A single expert uses the same interface. Internal orchestration may be linear or dynamic. Research, experiment and review Group classifications are deferred; there is no fixed built-in three-category system. The Router sees the versioned Group Definition and its capability description, while the internal program template is hidden from it.

The catalog is a JSON array of objects with `id`, `version`, `description`, `program` (a path), and optional `settings`. Relative program paths resolve against the catalog directory. Initialization copies each program into the Study, fixing the source used by that Study. The single-file program is trusted operator code; this interface does not sandbox arbitrary Python. Explicit shared data inputs remain references and are not scanned or hashed. A program exports `async run(ctx, payload)` and returns exactly:

```json
{ "summary": "", "completion": "", "unfinished": [], "help": "", "artifacts": [] }
```

`summary`, `completion`, and `help` are strings; `unfinished` and `artifacts` are lists of strings. `ctx` provides `root` and `work` Paths, `run_id`, `round`, `definition` (including settings), `state`, `save_state()`, `previous_output`, and `services`. `payload` contains `task_ref`, `input_refs`, `prompt`, and `feedback_refs`. Group-internal Codex sessions may share context for plan refinement and implementation; analyst and critic sessions are independent. An already complete scientific plan is not redesigned unnecessarily.

## Run and handoff

Each task has an independent Group Run, with no shared writable workspace or context across runs. Its Group version remains stable during execution and hold/resume. Input and output artifacts are handed off through the filesystem; the Router forwards prompts and references without retelling their contents. Each Run has its own work directory. Explicitly designated small deliverables cross the output boundary as independent read-only copies. Large data and checkpoints stay in place and are referenced by reports; they are not copied or hashed for handoff.

A hold takes effect only at an output boundary, after subexperts and execution have settled. A request outside a Group's responsibility returns `help`; the Router can assign another Group. That assisting Group produces a patch or report in its own directory and does not alter the original Run.

The conceptual API is `start(group_id, task_ref, input_refs, prompt)`, `resume(run_id, feedback_refs, prompt)`, `observe`, `hold`, and `close`. These are runtime operations, not additional CLI commands. Start and resume are asynchronous; observe, hold and close are synchronous, and wait is event-based. `observe()` returns an object containing `stopped` and `runs`; each run record includes `run_id`, `group_id`, `status`, and `output`. A completed execution reaches `returned`, then the Router may mark it `held` or `closed`. A settled failure has status `failed` and exports a small diagnostic report; Router may route diagnosis and close it, but cannot hold/resume it. Unknown effects have `needs-reconciliation` and block further coordination until operator diagnosis. Runtime control state is stored under `study/group-runs`. A crash while running produces `needs-reconciliation` rather than an automatic rerun.

## Coordination and publication

The Router chooses Group-level actions: start, resume, hold, close, wait, finish_round, and escalate. It does not stop a Group mid-run; runtime STOP and fault protection remain separate. `finish_round` uses `result_ref` to reference a complete `RESULT_SCHEMA` JSON exported by a closed Group Run admitted in the current iteration. Python validates it and publishes the explicitly exported small artifacts with a mechanical file index; the Router does not perform scientific integration. Existing human request consumption, writer lock, resource ledger and publication settlement remain in force. Legacy host/native role delegation is not a Group.

## Native service and bounded experiment example

`ctx.services.call(role, context, instructions, payload, schema)` runs a leaf Codex specialist. A context name resumes only within its owning Run. Plan refinement and implementation may share a context; Analyst and Critic remain independent. Native calls retain their histories and usage events in the Study; Group identities are recorded in `roles.json`.

`execute_prepared(command, operation_id=..., backend=...)` records intent before dispatch and preserves the original receipt. `resource` uses the Study's existing resource adapter and serializes prepared commands across Runs. Commands use a Run-specific directory under `remote_root/.group-runs/`; local source files are not automatically uploaded there. Remote inputs and executable code must already be provisioned under the existing resource authorization. `local` explicitly runs a trusted prepared CPU command in the Run work directory. Neither backend is an OS sandbox for arbitrary commands. Unknown dispatch or SSH transport loss blocks the output boundary; a known failed command remains a failed experiment and is not silently rerun.

The [bounded experiment program](../examples/groups/exp.py) accepts a scientific plan as `task_ref`, implements it, executes the prepared command, and independently analyzes it, with optional critique. It is one example Group, not a fixed taxonomy. Assistance feedback retries only a blocked stage and retains completed execution. A known nonzero execution result requires a separately routed diagnosis/repair task, rather than pretending resume reruns the command. This adapter does not itself produce a Study-level final result: route scientific integration to an appropriately configured Group that exports `RESULT_SCHEMA`.

## Usage

```sh
uv run research-loop init /path/to/study --project /path/to/project --goal /path/to/goal.md --coordinator router --groups /path/to/groups.json
uv run research-loop inspect /path/to/study
uv run research-loop run /path/to/study
```

Use a catalog and programs appropriate to the authorized Study. For the experiment example, `examples/groups/exp.json` is a catalog; it requires an explicit scientific plan and provisioned resource inputs. No default Group is silently selected. Model calls and remote compute require separate authorization.

Router packets contain up to 4,000 characters of the exact current request plus its full file reference and a truncation flag. Capability descriptions and current-iteration Run boundary metadata must fit the 12,000-character packet limit; excess is rejected rather than silently losing task state. Historical closed Runs remain on disk and their artifacts are available through published knowledge without embedding their full history into every routing call.

## Evidence and limits

This is the approved design and implementation contract, not evidence of deployment, scientific benefit, cost savings, or real-model research performance. Mechanisms are selectively reused from prior research and Router work; their historical results do not validate this mode.
