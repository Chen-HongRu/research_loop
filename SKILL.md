---
name: research-loop
description: Set up and run bounded evidence-driven research, or inspect an authorized run. Discussion alone does not start experiments.
---

# Research Loop

Install the repository as described in [installation](references/installation.md). Before research, inspect the target instructions, active code, changes and evidence. Establish the scientific question, model and loss, data roles, shapes, masks, time alignment, fixed evaluation, resource authorization and stop conditions. Ask when missing information changes experiment meaning or exceeds authorization. Keep private inputs, raw sessions and runtime artifacts outside Git.

Initialize and inspect a run before starting it; see [operations](references/operations.md). The main Codex agent coordinates dependencies, efficiency, resources and summaries, dynamically invoking the Scout, Proposer, Experimenter, Analyst and Critic role templates for scientific work. Native delegation uses a fresh native subagent with `fork_turns="none"`; host delegation starts a new plain-message thread per task. Either way the task is self-contained; do not resume, reuse or follow up in an existing role context. Research resource tools are available to the lead only. Report evidence and limitations, and distinguish incomplete work or exhausted resources from achieving the goal. Independent context does not create a separate sandbox.

For optional operator-side continuous efficiency measurements and descriptive
before/after comparisons, see [measurements](references/efficiency.md). Collection
does not start research, call a model or authorize additional resources.

Use only the pinned native Codex runtime. Do not launch model, GPU or remote work without authorization. Do not repeat an uncertain remote submission or reset its budget. Inspect the original operation and job identity and reconcile unknown effects. An alternative OpenAI-compatible provider changes the model baseline and disables hosted web search; provider runs default to host delegation (`delegate_task`/`wait_tasks`), which preserves fresh role contexts and concurrent tasks. See [operations](references/operations.md#openai-compatible-provider) before interpreting such a Study.
