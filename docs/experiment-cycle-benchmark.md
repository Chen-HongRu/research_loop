# Experiment-cycle benchmark

The benchmark measures a bounded experiment through Plan → Prepare → Execute →
Evaluate and an independent host correctness check. A case freezes its task,
starting workspace, data roles, permitted change, device, budgets, execution and
checker. The host transfers artifacts, validates deliveries and executes the
prepared command; it does not invent hypotheses or reinterpret results. Private
inputs and runtime evidence stay outside Git. The supplied numeric CPU sample is
synthetic and does not establish general research benefit.

## Orchestration contract

`benchmarks/experiment_cycle/orchestration.py` is the editable candidate. It
contains a nonempty `COMMON` string and `async def orchestrate(team)`. The host
exposes `await team.call(phase, role=..., context=..., instructions=...,
handoff=...)`, `await team.message(...)` with the same arguments and a message
result, and `await team.execute()`. Candidates can reuse role and context names,
pass JSON handoffs, and use conditionals and unrolled feedback. The restricted
Python language excludes imports, loops, extra functions, arbitrary attributes and
unawaited calls. Instructions are limited to 16 KiB. The host enforces at most
eight model calls and the phase order; `phase_protocol_pass` is a required gate.

The default uses three fresh specialist contexts and no coordinator. A candidate
can use a coordinator role, whose calls are charged like all others. Reused Native
contexts receive a stable phase-neutral developer guard and common instructions;
the current role and stage instructions arrive in each turn. All model
calls use `gpt-6.1-terra` at medium effort in the collection and optimization
study. Model, cases, GPU allocation, execution, receipt accounting and correctness
remain fixed operator controls. `--orchestration FILE` selects a candidate for the
runner and collection. `--strategy` is historical and rejected; the old JSON
source and loader remain for historical evidence.

| Phase | Responsibility | Required delivery |
| --- | --- | --- |
| Plan | Design the bounded experiment | Hypothesis, matched control, permitted change, evaluation rule and outcome interpretations |
| Prepare | Implement the plan without running the substantive experiment | Candidate, bounded smoke evidence and command |
| Execute | Host executes the prepared command once | Execution receipt and actual outputs |
| Evaluate | Interpret the plan and execution evidence | Structured conclusion, metrics, exact artifact references and limitations |

A failed delivery, command or protocol stops the attempt and retains recorded
costs. Missing evidence is not-evaluable; negative or inconclusive findings can
be correct. The checker independently recomputes checkable metrics against the
frozen case contract. It does not certify arbitrary scientific claims.

## Accounting and evaluation

The host records every model call in an explicit roster. Receipts, including
compaction and failed work, are attributed by phase, role and context. Shared
context histories use non-overlapping byte windows; response IDs are deduplicated.
Missing or ambiguous receipts are coverage gaps, not measured zero. Total token
traffic is input plus output, without adding cached input or reasoning twice.
Stage wall time includes model scheduling and tools; cycle wall time includes
runner and checker overhead. Concurrent evaluation reports batch wall time
separately from summed cycle time.

In the three-evaluation Research Loop pilot, the host evaluates the unmodified
seed first and saves `baseline.json`. A failed baseline stops the study; after a
passing baseline the outer loop can evaluate at most two improvements to
`work/orchestration.py`. Cases run
in parallel by assigned GPU, with one cycle at a time per GPU. All configured
cases must pass correctness, complete usage coverage and phase protocol before a
candidate can be compared. Failed and partial attempts retain recorded costs;
outer-loop optimization costs are separate. The operator privately configures
the authorized GPUs, indices 0 and 2, including their UUIDs and paths.

The collection contains ten fixed architecture interventions on one Cactus
development task, not ten independent benchmarks or held-out evidence. Its
architecture check freezes data, loss, training and evaluation code. New cases
include an `intervention_id` from 1 to 10. Before training, the remote worker
normalizes the layer constructors and requires the exact assigned intervention;
the checker requires its ID and verification in the receipt. Optimization rejects
old case packs lacking IDs; new packs are generated separately without rewriting
existing private cases. Remote
candidate execution is direct and has no OS filesystem or network sandbox; the
architecture check does not establish hidden-label isolation. The detailed
commands, resource conditions and continuation rules are in the [benchmark
guide](../benchmarks/experiment_cycle/README.md).

A real shared-context smoke implemented the assigned GELU change and passed all
four phases, correctness and receipt coverage. This one case does not establish
general research benefit. An earlier shared-context smoke passed metrics despite
a no-op and is ineligible under the new intervention gate.

Historical results retain their original scope. A prior remote execution-only
paired smoke completed both arms and produced scores; it did not validate a full
collection or general research benefit. The historical ten-case baseline used the
earlier metrics-only harness. Although the default prompt is unchanged, it does not validate
this engine, which requires a fresh baseline.
