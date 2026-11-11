# Router Groups

This document specifies the approved Router Group design, including the subsequent authorization to migrate the complete Lead workflow. New Studies use the reference Router workflow by default. Existing Study configuration is unchanged; `--coordinator lead` selects the legacy path for a new Study.

## Groups and catalog

A Group specializes in a problem scope and may contain one or more experts. A single expert uses the same interface. Internal orchestration may be linear or dynamic. The replaceable reference workflow supplies direction, experiment, synthesis and diagnosis Groups; custom catalogs may define other capabilities. The Router sees the versioned Group Definition and its capability description, while the internal program template is hidden from it.

The catalog is a JSON array of objects with `id`, `version`, `description`, `program` (a path), and optional `settings`. Relative program paths resolve against the catalog directory. Initialization copies each program into the Study, fixing the source used by that Study. The single-file program is trusted operator code; this interface does not sandbox arbitrary Python. Explicit shared data inputs remain references and are not scanned or hashed. A program exports `async run(ctx, payload)` and returns exactly:

```json
{ "summary": "", "completion": "", "unfinished": [], "help": "", "artifacts": [] }
```

`summary`, `completion`, and `help` are strings; `unfinished` and `artifacts` are lists of strings. `ctx` provides `root` and `work` Paths, `run_id`, `round`, `definition` (including settings), `state`, `save_state()`, `previous_output`, and `services`. `payload` contains `task_ref`, `input_refs`, `prompt`, and `feedback_refs`. Group-internal Codex sessions may share context for plan refinement and implementation; analyst and critic sessions are independent. An already complete scientific plan is not redesigned unnecessarily.

## Run and handoff

Each task has an independent Group Run, with no shared writable workspace or context across runs. Its Group version remains stable during execution and hold/resume. Input and output artifacts are handed off through the filesystem; the Router forwards prompts and references without retelling their contents. Each Run has its own work directory. Native specialists use ordinary Codex workspace-write with default filesystem reads, explicit work/native scratch and temporary writes, and shell network disabled. This separates contexts and writable work; it does not hide unrelated readable files. Specialists must use only admitted evidence and avoid other conversations. Keep Study/control state and read-only inputs outside writable ancestors, including system temporary directories. Router remains tool-free. Explicitly designated small deliverables cross the output boundary as independent read-only copies. Large data and checkpoints stay in place and are referenced by reports; they are not copied or hashed for handoff.

A hold takes effect only at an output boundary, after subexperts and execution have settled. A request outside a Group's responsibility returns `help`; the Router can assign another Group. That assisting Group produces a patch or report in its own directory and does not alter the original Run.

The conceptual API is `start(group_id, task_ref, input_refs, prompt)`, `resume(run_id, feedback_refs, prompt)`, `observe`, `hold`, and `close`. These are runtime operations, not additional CLI commands. Start and resume are asynchronous; observe, hold and close are synchronous, and wait is event-based. `observe()` returns an object containing `stopped` and `runs`; each run record includes `run_id`, `group_id`, `status`, and `output`. A completed execution reaches `returned`, then the Router may mark it `held` or `closed`. A settled failure has status `failed` and exports a small diagnostic report; Router may route diagnosis and close it, but cannot hold/resume it. Unknown effects have `needs-reconciliation` and block further coordination until operator diagnosis. Runtime control state is stored under `study/group-runs`. A crash while running produces `needs-reconciliation` rather than an automatic rerun.

## Coordination and publication

The Router chooses Group-level actions: start, resume, hold, close, wait, finish_round, and escalate. It does not stop a Group mid-run; runtime STOP and fault protection remain separate. `finish_round` uses `result_ref` to reference a complete `RESULT_SCHEMA` JSON exported by a closed Group Run admitted in the current iteration. In the reference workflow only the configured `study_result_group`, `synthesis`, authors the Study result. Custom catalogs may designate one result author with `settings.produces_study_result: true`; without that designation, any custom Group may author the result. The Router does not perform scientific integration. Synthesis exports `knowledge-manifest.json` with `{"files": [{"source": "relative/work/path", "target": "relative/knowledge/path"}]}`. Each source must be explicitly exported and `index.md` is required. Synthesis role reports may cite explicitly admitted input files; those remain references and are not exported again as new Synthesis files. Unknown external output paths are rejected. Python installs the authored scientific files into the existing draft, preserving other historical files. It retains a separate mechanical `group-artifacts.md` with links to copied evidence under `groups/<run>/<round>/` and appends its discovery link to `index.md`. These reserved paths cannot be replaced by the knowledge manifest. Publication and human-request consumption use the existing atomic path. Existing human request consumption, writer lock, resource ledger and publication settlement remain in force. Legacy host/native role delegation is not a Group.

## Native service and bounded experiment example

`ctx.services.call(role, context, instructions, payload, schema, *, common=None, include_role_template=True, cwd=None)` runs a leaf Codex specialist. `cwd`, when supplied, is inside the Run work directory. The service supplies `study_context.input` and `study_context.output_schema`, including the admitted human-request snapshot and a fixed published knowledge version. A context name resumes only within its owning Run. Plan refinement and implementation may share a context; Analyst and Critic remain independent. Native calls retain their histories and usage events in the Study; Every Router context and Group specialist is recorded in `roles.json` for complete role attribution.

`execute_prepared(command, operation_id=..., backend=...)` records intent before dispatch and preserves the original receipt. `resource` uses the Study's existing resource adapter and serializes prepared commands across Runs. Commands use a Run-specific directory under `remote_root/.group-runs/`; local source files are not automatically uploaded there. Remote inputs and executable code must already be provisioned under the existing resource authorization. `local` explicitly runs a trusted prepared CPU command in the Run work directory. Neither backend is an OS sandbox for arbitrary commands. Unknown dispatch or SSH transport loss blocks the output boundary; a known failed command remains a failed experiment and is not silently rerun.

The reference workflow programs are in [`group_programs`](../src/research_loop/group_programs):

- **Direction** uses Scout for autonomous exploration and Proposer to select a question and its scientific constraints. An explicit human experiment skips Scout.
- **Experiment** uses H11 Plan and Prepare in one context, host execution, and independent Critic Evaluate including analysis of the individual experiment. It returns reports or assistance requests, not a Study result.
- **Synthesis** uses Analyst and a fresh Critic to integrate evidence across experiments, maintain scientific knowledge and author the strict Study result and knowledge manifest. Its Critic returns an explicit `accept` or `revise` verdict and a reason; only `revise` creates an assistance request.
- **Diagnosis** handles out-of-scope faults and produces a diagnosis, patch or report in its own workspace.

The H11 common and three phase instructions retain the text from the evaluated source; Group boundaries, permissions and schemas are a new integration. Published scientific files carry the frontier and positive and negative results across rounds, rather than relying on Lead conversation history. Assistance feedback retries a blocked stage and retains completed execution. A known nonzero execution requires a separate diagnosis or repair task.

## Usage

```sh
uv run research-loop init /path/to/study --project /path/to/project --goal /path/to/goal.md
uv run research-loop inspect /path/to/study
uv run research-loop run /path/to/study
```

Pass `--groups /path/to/groups.json` to replace the reference catalog, or `--coordinator lead` for the legacy coordinator. `examples/groups/exp.json` points to the actual package program and demonstrates only the experiment Group; it does not produce a complete Study result. Model calls and remote compute require separate authorization.

Router packets contain up to 4,000 characters of the exact current request plus its full file reference and a truncation flag. The capability catalog and current Run state must fit the 12,000-character packet limit; the `references` object maps each reference ID to a Study-relative path (or an absolute external path) once; the runtime resolves IDs back to absolute paths, and actions use IDs resolved by the host. Excess is rejected rather than silently losing task state. Historical closed Runs remain on disk; selected scientific knowledge is published without embedding the full Run history into every routing call.

## Evidence and limits

This design reuses Router strategy principles and H11 instruction text, not the prior implementation verbatim. Historical performance results do not validate this integration or establish a cost benefit. A planned engine comparison uses two new isolated Studies with the same inputs and authorized Terra medium model for a CPU cost-mechanism task; it does not restart the historical Study or use a GPU. The comparison has not been executed, and real-model or scientific performance remains unverified.
