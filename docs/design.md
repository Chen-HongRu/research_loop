# Runtime design

The generic runtime in `src/research_loop/` owns configuration, Group state,
Agent execution, prepared-command receipts, interventions, queries, and atomic
publication. `src/research_workflows/` owns the reference research policy,
scientific schemas, expert instructions, Group programs, review requirements,
and completion rules. Generic code does not select an algorithm, dataset,
scientific metric, or research result shape.

## Execution path

`cli.py` adapts commands to `application.py`. Preparation resolves a strict YAML
through `configuration.py`, snapshots explicit declarations and managed source,
and writes the initial immutable revision. Inspection reads the same prepared
configuration. Starting invokes the prepared interpreter/controller rather than
re-resolving author files.

The controller loads the configured workflow through `protocol.py`. Workflow
policy determines capabilities, scheduling constraints, accepted work inputs,
result validation, and completion/publication proposals. `group_router.py`
constructs bounded Router packets and reference-constrained decision schemas,
calls the selected tool-free profile, and validates returned actions. Python
retains authoritative execution state.

`groups.py` implements isolated Group Runs and rounds. Programs export
`async run(ctx, payload)` and explicitly orchestrate their steps and Agents.
`group_agents.py` resolves Agent bindings and compatible per-Run sessions;
`execution.py` owns prepared command identities and settlement. The existing
Slurm backend keeps its external cumulative authorization ledger. Programs are
trusted cooperative Python code; arbitrary synchronous loops cannot be independently
killed within the controller process.

## Version and permission boundaries

`active.json` names a revision under `configurations/`. Each execution binds its
configuration revision at admission; later steps, calls and held-round resumes
retain that revision. Explicit revisions can replace declared programs, Agent
instructions and schemas for new executions. Shared imports use the fixed managed
implementation under `implementation/`; upgrading it requires stopping the whole
controller and preparing a matching implementation. External dependencies and
large data references are outside the explicit-file freeze.

Agent definitions are separate from sessions and calls. Session reuse requires
compatible Run, revision, Agent definition, profile, working directory and effective
tools. Programs choose new or reused context; research programs explicitly select
independent Critics. Workspace permissions constrain writes through the Codex
harness but do not isolate otherwise readable scientific evidence. Declarations
cannot expand runtime authorization.

## State, interventions, and recovery

Group execution transitions from running to returned, then held or closed.
Settled failures are failed; uncertain external effects require reconciliation.
Small explicit exports are immutable output boundaries; data/checkpoints stay
referenced in place. Input receipts persist before side effects. A controller
restart does not automatically replay an execution that was running.

External callers atomically submit intervention requests. A single controller
owns admission and application. Requests have stable IDs, targets, expected
revisions and queryable receipts. Pause blocks new admission while active Groups
reach a boundary. Stop closes service admission and requests cancellation across
all targets before waiting for individual cleanup. Continue reopens admission;
explicit resume makes fresh routing decisions from settled evidence. Neither
operation clears unknown effects or resets resource authorization.

Configuration activation uses expected-revision checks and explicit affected
executions/dependencies. Affected work must settle before replacement; unrelated
executions can retain their old revision. Workflow-specific recovery may package
selected settled evidence for a new execution without repeating its original
command. This is bounded receipt recovery, not arbitrary Python exactly-once
execution.

`publication.py` stages selected small result files and metadata, then atomically
activates `outputs/current`. STOP observed before activation prevents commit.
Committed publication remains valid if later status recording is interrupted;
recovery repairs status/acknowledgements from the active receipt. Workflow code
validates meaning and proposes content; it does not activate runtime state.

## Facts and operations

`records.py` persists explicit steps, calls, operations, handoffs and decisions.
`queries.py` pages existing small facts without scanning native histories or
inventing dependencies. Intervention application is distinct from scientific
completion. Native trace measurements and historical dashboards are projections,
not additional execution authorities.

Detached start retains status, a controller log and a terminal completion event.
It provides no automatic restart or external agent wakeup. Existing supervisors
can own foreground execution using the prepared implementation. Operational
commands and configuration fields are specified once in
[operations](../references/operations.md); program interfaces are in the
[author API](framework-api.md).
