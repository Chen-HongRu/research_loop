---
name: research-loop
description: Use Codex to set up and run evidence-driven research in a code repository from a natural-language question. Prepare scripts and fixed evaluation, start a bounded Study, inspect evidence, continue, submit investigations, or recover interrupted work. Discussion alone does not start experiments.
---

# Research Loop

This entire directory is the installed skill: instructions, matching controller
source, locked dependencies, examples and task adapters. Resolve this file's
directory as SKILL_ROOT; never assume the user's repository contains the runtime.
The target repository and Study are separate from this installed directory.

Check installation completeness before running: `scripts/research_loop.py`, `src/`
and `uv.lock` must exist. Some skill-installer Git versions install only top-level
files for `--path .`; if this new installation has `.git` and is sparse, run
`git -C SKILL_ROOT sparse-checkout disable` to materialize its checked-out revision,
then verify HEAD and the files. Do not run this repair in the user's target repo.
Archive installs do not have or need Git metadata.

Use `python3 SKILL_ROOT/scripts/research_loop.py ...` from the target repository
to invoke the bundled CLI. Replace SKILL_ROOT with its actual absolute path and
quote paths. The launcher uses uv with this release's lockfile, preserves the
current working directory and forwards CLI arguments and exit status. Do not use
a globally installed `research-loop` which may be a different release.

## Identify the Study format

Before operating an existing Study, inspect its directory: `runtime.json` with
`format: sqlite-v1` or `runtime.sqlite3` identifies the SQLite runtime; `study.json`
and `state.json` identify the legacy document controller. If markers conflict or
the format is unclear, stop and inspect rather than guessing. Route SQLite Studies
to `runtime ...` commands in [concurrent runtime](docs/concurrent-runtime.md) and
[operations](references/operations.md); use the legacy commands below only for
legacy Studies. Do not automatically convert either format. For an explicitly requested migration,
read [runtime migration](docs/runtime-migration.md), start with read-only preview,
and follow its authorized operator translation, fencing and activation workflow.

## Choose the operation

| User request | Action |
| --- | --- |
| Install or check setup | Read [installation](references/installation.md); run `doctor` |
| Research a new question | Follow the setup workflow below, then actually run one bounded iteration |
| Show progress, findings or best candidates | Read `status`, `knowledge`, `graph`, `frontiers`; distinguish live observations from reviewed evidence |
| Continue | Confirm the Study and runner, inspect status and remaining authorization; use `resume --reason` for an eligible reviewed stop, then `run --iterations 1` |
| Run a specific follow-up | Read [operations](references/operations.md); submit a task with frozen source, then advance through the correct runner |
| Pause, stop, recover, extend, update or remove | Read [operations](references/operations.md) before acting |

Read only the relevant references. Existing task adapters have their own runner,
authorization and reconciliation rules; use those instead of generic `run`.
Never initialize a second Study just to bypass an interruption or spent budget.

The workflow below creates a legacy document Study. For a requested SQLite Study,
use the explicit trusted application and initialization contract in
[concurrent runtime](docs/concurrent-runtime.md); the legacy Harness is not
automatically integrated and its role schemas are different.
The [native application](integrations/native_runtime/README.md) supports tool-free
Pi roles and qualitative Execute, within its stated limits.

## Turn a question into a runnable Study

1. **Recover the actual contract.** Inspect the target repository's AGENTS.md,
   active entry points, dependency configuration, Git status, relevant evidence
   and any existing Study. Preserve uncommitted work. Establish the question,
   model/loss where applicable, data roles, tensor/time/mask semantics, permitted
   changes, evaluation, compute authorization and stopping conditions. Infer
   routine choices from code; ask only when missing information changes research
   meaning or exceeds authorization. Continue independent preparation meanwhile.

2. **Write the research inputs.** Save a self-contained `goal.md` outside the
   installed skill, containing the question, prior observations, exact input
   locations/versions, permitted tools and changes, actual execution commands,
   resource limits and success/stop criteria. For numeric comparison generate
   `evaluation.json` using [the schema example](examples/evaluation.json) and
   [usage](docs/usage.md): fix metric directions, split, aggregation, constraints
   and checkpoint selection before observing results. Do not copy the example
   metric into unrelated tasks. Final-only data/scores stay outside search.
   Diagnostics and theory need not produce ranked candidates or an optimization
   hypothesis. See [design](docs/evidence-driven-design.md) if semantics are unclear.
   Carry applicable repository research constraints into the goal or frozen baseline
   instructions: child stages have no access to this chat's implicit agreements.

3. **Implement the missing experimental path.** Reuse existing scripts; generate
   only the missing run/evaluate/smoke entry points in the target experiment
   workspace. Use uv for Python. Scripts must run from isolated Git worktrees
   without relying on the original checkout's uncommitted files, current directory
   or writable output paths. Record real configuration, raw measurements, candidate
   identity and failures in fresh output paths. Do not overwrite canonical data or
   the only checkpoint. Do not invent training scripts for a pure proof task.

4. **Verify the smallest real path.** Exercise actual inputs, model/loss and
   evaluator with the smallest meaningful smoke; distinguish smoke from a study
   result. Do not add full-content scans/hashes of data, caches, manifests,
   checkpoints or derived artifacts, including smoke/validation; use version
   references and cheap metadata. Do not add repeated startup validation.
   Generic Codex Execute is workspace-write, command-network disabled and cannot
   approve escalation; selection, Plan and Review are read-only. Prepare dependencies
   and data beforehand. Verify device, input and output access in the execution
   environment; host-shell success alone does not prove sandbox access. Remote
   jobs/protected scoring need a task adapter: read [execution backends](docs/execution-backends.md).
   Missing credentials, permissions or compute authorization are concrete blockers,
   not reasons to switch model, device or evaluator silently.

5. **Freeze a recoverable baseline.** Writable Studies require clean Git source.
   If the target has unrelated changes, create a separate experiment repository
   containing only selected small source/configuration and the new scripts. Record
   its source revision and any deliberately included uncommitted changes; never
   commit or clean the user's entire dirty checkout. Keep large data/checkpoints
   external and version-referenced. The baseline workspace cannot equal or contain
   the Study control directory. Use sibling paths for clarity. All experiment
   outputs and experiment environments belong outside the installed skill; uv may
   prepare the bundled controller's own `.venv` inside the installation.

6. **Initialize, then start.** Inspect `init --help` and [usage](docs/usage.md).
   Use actual absolute paths for Study, goal, baseline and evaluation. Set explicit
   total iterations/calls/time and per-call timeout suitable for the authorized
   first investigation. Preserve the user's model; absent a choice, use
   `gpt-6.1-sol`/`medium`, without fallback. Add `--allow-write` only for authorized
   writes; omit `--evaluation` for qualitative work. `init` makes no model call.
   Start with `run STUDY --iterations 1` through the launcher, or the task runner
   when required. If nested CLI startup is blocked by the outer command sandbox,
   use the normal host approval path described in [installation](references/installation.md),
   preserving the child-stage sandboxes. Run it as a supervised process whose status can be polled, so
   Codex can report progress and honor stop requests. Do not launch an untracked
   background loop. A request to begin research authorizes this bounded path, not
   an arbitrary paid/shared GPU allocation.

7. **Verify publication and hand off.** Inspect `status`, `graph`, `knowledge` and
   `frontiers`. Report actual state: initialized, running, reviewed, stopped or
   needs attention. A zero exit code or initialized directory alone does not prove
   a published investigation. Name the Study path and runtime path, useful commands,
   real smoke outcome, reviewed findings and limitations. Retain raw transcripts
   locally; do not publish them into Git. Stop after the authorized bounded run;
   extending budgets or a remote lease requires its own authorization.

## Controller boundaries

New legacy document Studies use version 3; SQLite Studies use schema 8. In the
legacy controller, Plan's `markdown` freezes the scientific contract;
top-level `timeout_seconds` bounds execution. Execute produces evidence; independent
Review verifies it; only the controller publishes research documents and Frontiers.
Realtime observations are unreviewed. Local improvement is not global success,
budget exhaustion is not goal completion, and retry is not independent evidence.

Never modify control records to force progress. Preserve frozen evaluations,
accepted Plans, artifact identities, original budgets and unknown external effects
across recovery. Read [recovery](docs/recovery.md) for interrupted work and
[failure diagnostics](docs/failure-diagnostics.md) for error responsibility.
