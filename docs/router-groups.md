# Research Groups

The reference workflow in [`src/research_workflows/`](../src/research_workflows)
binds scientific policy to the generic runtime through
[`examples/research.yaml`](../examples/research.yaml). Use the commands and
configuration rules in [operations](../references/operations.md), and the
[author API](framework-api.md) when changing programs. Existing runs from older
execution formats are not migrated by this refactor.

## Scientific responsibilities

| Group | Responsibility |
| --- | --- |
| Direction | Compare scientific alternatives and export one bounded experiment task; explicit experiment inputs skip autonomous Scout exploration. |
| Exp | Plan and Prepare with an explicitly shared Agent context, execute one prepared command, and independently Evaluate its evidence. |
| Synthesis | Integrate admitted evidence and authored knowledge, obtain independent criticism, and author the scientific result and publication manifest. |
| Diagnosis | Investigate a bounded fault and export evidence or a patch without changing the controller or scientific design. |

The YAML binds program steps to Agent definitions and profiles. Role-specific
instructions and scientific schemas live in the research package. The reusable
Agent service does not recognize Scout, Critic, or any fixed scientific role.
Critic calls use independent sessions; that does not impose filesystem read
isolation. Completed plan/execution evidence is retained rather than regenerated
as a side effect of assistance or re-routing.

Author phases return explicit `status: ready|blocked`; informational `help` text
does not determine progress. Independent review returns `accept|revise`.
Required artifacts and a settled successful execution remain prerequisites for
evaluation and publication, including recovery. Status-less historical phase
receipts are not accepted by the current research package.

## Run and publication boundaries

A Group has its own workspace, persistent local state and immutable round
exports. Its output contains exactly `summary`, `completion`, `unfinished`,
`help`, and `artifacts`. These describe a settled boundary, not overall scientific
success. A Run requesting assistance can be held at that boundary; exported
feedback may support another round using its original revision. An interrupted
Run is replaced by a linked new execution after settlement, not resumed inside
its old Python stack or conversation.

The reference workflow admits one active Group while the runtime supports
independent concurrent Groups. Python handles lifecycle and checks the configured
result author; Synthesis owns scientific integration and the continue/stop
judgment. A diagnostic completion alone does not complete the research run.
The scientific goal remains distinct from execution termination.

Synthesis exports `study-result.json`, a knowledge manifest and the selected small
knowledge files. Its Author first returns a complete, unreviewed candidate with
`status: ready`; the host then calls the independent Critic. Waiting for that
downstream verdict cannot block Author's return, and only acceptance permits the
final export. Missing upstream evidence still blocks the candidate.
The workflow checks those contents and maps them to generic
publication proposals. The publisher activates `outputs/current` only after
settlement. Actual execution/evaluation receipts and successful publication gate
human experiment completion; a receipt marked applied is not that scientific
completion. Historical and negative evidence remain part of the authored record.

Large data and checkpoints are referenced in place. Prepared commands retain
original operation identity and backend receipts. UNKNOWN execution blocks
redispatch until reconciliation. The bounded recovery hook can export settled
successful command evidence for independent evaluation in a new Exp execution;
it does not repeat the original command or establish scientific validity. Recovery
retains the original scientific task, Plan, protocol settings and schema while
allowing accepted Agent instruction revisions. See the author API for exact
packet and selected-file limits.

## Historical validation boundary

The evidence below describes the earlier Router integration and its comparison
implementations. It is retained as historical evidence and does not validate the
current configuration, controller, intervention or Agent-service refactor.

## Evidence and limits

This design reuses Router strategy principles and H11 instruction text, not the prior implementation verbatim. Historical performance results do not establish a benefit for the new integration. Earlier engine CPU and recovery attempts exposed reference-format rejection and streaming output without usage receipts; their failed costs remain part of the private evidence.

A subsequent bounded recovery comparison completed both paths on the same settled Exp evidence: independent Critics accepted the candidates, human requests were consumed, knowledge and the original assessment were published, and no experiment was rerun. The Router profile used deterministic admission to the fixed Synthesis Group. Its baseline was the controlled Lead sharing the Python recovery/publication API, not the unmodified historical Lead. Each arm ran once; elapsed time and token counts have cache and ordering effects. This validates that recovery case and does not establish general efficiency, autonomous Direction, new-experiment integration or resource/GPU behavior.

The explicit-task workflow was subsequently verified on Linux and macOS from
separate clean checkouts of the same revision, unchanged lock/config files and
the same startup command. Real Router/specialist calls drove Direction, one local
execution of the supplied repository auditor, independent Evaluate, Synthesis
review and publication. Both completed with consumed requests, clean subjects,
resolvable knowledge links and complete recorded usage coverage. No-model native
sandbox probes also verified project reads, work writes and rejection of project
writes. This covers the bounded repository audit on those two hosts; autonomous
Scout exploration, running-Study relocation and resource/GPU behavior remain
unverified. Unit-test fixtures inside the audit are distinct from the real outer
model workflow and do not establish general research benefit.
