# Minimal experiment-cycle benchmark

Status: approved for implementation in this task: after accepting the coordinator-free design, the user requested implementation. The implementation and its evidence are described in `benchmarks/experiment_cycle/README.md`.
Source baseline: Research Loop `8ff7ad34a4f76f76f303a64a48f9f017c8797d45`,
Codex `0.167.0`.

## Unit and execution

One case is one predefined experimental question. A deterministic benchmark
runner directly invokes three specialist tasks and executes the prepared command:

    case -> Plan Agent -> Prepare Agent -> local process -> Evaluate Agent
                                                        -> host correctness check

There is no coordinator Agent. The runner transfers artifact references, checks
delivery shape, starts and waits for processes, records usage/time, and produces
the report. Scientific design belongs to Plan; implementation belongs to Prepare;
interpretation belongs to Evaluate. The runner does not choose new hypotheses,
reinterpret results, design repairs or rewrite the experiment goal.

Use one fresh context per specialist task with `gpt-6.1-sol` and medium reasoning.
Each task has a fresh Native runtime instance, settled and closed before the next
stage. Reuse current role responsibilities through benchmark-specific assignments
that identify the runner as executor; omit coordinator delegation policy and
nested delegation. Work products, not chats, connect stages.

This is a simplified architecture using the current pinned runtime, not the
current autonomous coordinator topology. It does not establish that open-ended
research can dispense with scientific coordination. A future coordinator
comparison uses the same case, deliverables, checker, resources and accounting;
the measurement unit requires neither topology. No general adapter framework
is needed for this first baseline.

## Case contract

Start with one recoverable historical experiment executable locally under its
original device/data protocol. Prefer a genuine CPU case. Keep private cases
and runtime evidence outside tracked source files.

| Material | Required contents |
| --- | --- |
| Metadata | Case ID, source reference, input revision, device, budgets and stopping conditions |
| Task | Question, allowed changes, data roles, objectives, deliverables and permitted claim scope |
| Starting workspace | Pre-plan code/configuration and explicit minimal original inputs |
| Prior sources | Frozen material available before the target experiment |
| Host-only checker | Independent deterministic checks, evaluation inputs/answers as appropriate, and reference evidence |

Agents cannot read the target experiment's historical plan, patch, results,
conclusions, transcripts or future Git history. Disable live retrieval and shell
network access for all tasks; verify effective configuration. Missing recoverable
starting conditions make a case ineligible. Do not replace a GPU experiment with
a CPU toy while claiming the same task. Intake/startup does not scan or hash
dataset/checkpoint contents.

Historical results are evidence, not ground truth or a required action sequence.
The checker resides outside Agent/project read permissions. Candidate code runs
with only intended inputs, never the checker's private-file authority. The first
case is development data; repeated optimization on it is not held-out evidence,
even when the checker is hidden from running agents.

## Stage contract

| Stage | Owner and input | Output |
| --- | --- | --- |
| Plan | Plan Agent reads the question and prior evidence | Hypothesis, matched control, permitted change, evaluation rule and outcome interpretations |
| Prepare | Prepare Agent reads plan and starting code | Candidate/configuration, bounded smoke evidence, command and output schema |
| Execute | Runner starts the prepared process | Candidate/config/input identity, command status, timing, logs and measurement outputs |
| Evaluate | Evaluate Agent reads plan and execution artifacts | Structured conclusion, metric deltas, evidence and limitations |

Freeze the allowed smoke before replay. Prepare cannot execute the substantive
experiment under a smoke label. Capture the actual experiment command and outputs.
Cross-stage substantive work is a phase-protocol violation, not a valid stage-cost
decomposition. All work settles before the next stage starts.

The first runner performs one forward pass. A failed delivery, command or stage
stops the attempt and retains its costs; later stages are not-run. It does not
ask a coordinator to invent recovery. Within-task implementation work stays in
that task's bounded assignment. A later repair policy must be specified and
charge every attempt rather than silently retry.

## Token accounting

The host records case_id, attempt_id, stage and task/thread identity before each
invocation. This explicit roster is authoritative: a coordinator thread, native
parent link or role-name inference is not required. Existing Study collectors
expect a lead and cannot be reused unchanged as this benchmark's team selector.
Reuse `token_usage.accept_record` against the roster's actual histories instead
of creating a new usage authority.

Attribute all receipts in a specialist task to its stage, including recorded
compactions and failed work. Deduplicate response IDs across the attempt. Report
input, cached input, uncached input, output and reasoning. Total traffic is input
plus output; cached input and reasoning are not added a second time.

    cycle_tokens = sum(stage_tokens) + boundary_tokens + unassigned_tokens

Buckets are disjoint and reconcile per raw token field against attempt receipts.
Missing/invalid receipts and ambiguous associations remain coverage conditions,
not zero consumption. The direct-role roster proves that no coordinator was
invoked: report coordinator as absent, with zero model calls. This differs from
a coordinator with unmeasured usage.

The Execute process and deterministic runner/checker make no model calls. Any
future auxiliary model call must enter the same roster and totals.

## Time accounting and report

Use host monotonic durations and UTC timestamps for trace joins. Stage timing
starts before task/process startup and ends after completion and settlement.
Attempt timing covers startup through final accounting/checking; final report serialization is excluded:

    cycle_wall = sum(stage_wall) + runner_and_check_overhead_wall

Report four stage rows with tokens, wall time, model-call count, outcome and
artifacts. Show runner/checker overhead and unassigned usage separately. Include
whole-cycle totals, correctness and coverage. Execute normally has zero model
tokens but nonzero process time. Runner time is not model consumption. Agent
wall time includes tools and model scheduling, not just inference. A future
concurrent topology must not add overlapping task lifetimes as cycle wall time.

## Correctness and acceptance

Freeze the checker before replay. Verify allowed inputs/data roles, matched
control, actual execution and required metrics. Independently recompute checkable
metrics from execution-bound evidence. Compare discrete values exactly and
floating values with case-declared tolerances.

Evaluate returns supported, contradicted or insufficient evidence, with metric
deltas and permitted claim scope. Check this against the case rule. Negative or
inconclusive findings can be correct; process success or model agreement alone
cannot establish correctness. Missing required evidence is not-evaluable, never
pass. Open-ended uncheckable claims remain unverified.

Before model execution, deterministic fixtures must demonstrate token/time
conservation, duplicate/late/missing receipt treatment, failed-stage accounting,
and checker acceptance of a valid reference versus rejection of a wrong candidate.
Then replay one eligible historical case with actual specialist calls and local
execution. Preserve failed attempts in comparisons. One case/run is a feasibility
baseline, not a general efficiency claim; old live-run costs are not a matched
causal baseline.

Keep the small runner/report under `benchmarks/`; reuse Native, artifact handoffs
and receipt logic. Do not change production scheduling or introduce a scheduler
database, model-driven monitoring or plugin framework. Historical input recovery,
case correctness rules and resource limits must be verified before each historical replay. The supplied synthetic case checks only the bounded development task.
