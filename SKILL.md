---
name: research-loop
description: Set up and run bounded evidence-driven research, inspect reviewed evidence, or recover an authorized Study. Discussion alone does not start experiments.
---

# Research Loop

Use the installed skill directory as `SKILL_ROOT`; the target repository and Study are separate. Check the complete installation and build the root Node package as described in [installation](references/installation.md). Use `node SKILL_ROOT/dist/cli.js` with absolute paths; do not assume a global command refers to this release.

Before research, inspect the target's instructions, active code, existing changes and evidence. Establish the scientific question, model/loss, data roles, shapes, masks, time alignment, fixed evaluation, resource authorization and stop conditions. Ask when missing information changes the experiment's meaning or exceeds authorization. Keep private data, raw sessions and runtime artifacts outside Git.

Prepare a complete authorized Study configuration and separate source and input directories. Fix the evaluation and development feedback boundary before running; final-only feedback is rejected. The [CPU fixture](benchmarks/cpu_regression/README.md) illustrates the contract. Preserve the user's provider and model choice. `init` creates the Study without a model call; `run` may call the model and launch authorized local CPU jobs. Exercise the smallest real path, then report receipts, independent Review, state and limitations. Do not infer goal achievement from a completed command or exhausted budget.

For commands, knowledge and recovery use [operations](references/operations.md) and the [runtime guide](runtime/README.md). Do not start a second Study to bypass an unknown submission, refresh an expired authorization, or replay an external job. Read the [retirement guide](docs/runtime-retirement.md) before handling old Studies; history inspection is read-only and does not migrate or close jobs. Stop after the authorized work; additional model or compute use requires authorization.
