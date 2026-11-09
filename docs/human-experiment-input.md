# Human experiment input

Design contract v1. Status: approved for implementation in the task conversation.
The user confirmed that human input selects a priority experiment without changing
the Study goal, approved this contract, and added injection through a separately
user-authorized Codex session.

## Interface and lifecycle

`RUN/inputs/human.md` is an optional UTF-8 request file. Only the user authors or
revises requests, directly or through an explicitly authorized Codex session.
Research agents receive a read-only snapshot; the controller
may consume a completed request. File permissions must enforce the agent write
boundary, rather than relying only on role instructions.

At iteration admission, the controller reads the file at this fixed path. A
missing or whitespace-only file leaves autonomous planning unchanged. A nonempty
file is recorded with the iteration and supplied to the coordinator before
direction selection. Changes during an iteration apply to a subsequent iteration.
One snapshot is one request, possibly containing several related experiments.

The request takes priority over autonomous experiment selection within the
existing Study goal, scientific protocol and resource authorization. It may be
a question, hypothesis or experiment design. The Proposer supplies missing design
details; ambiguity that changes the experiment's meaning requires clarification.
An incompatible or blocked request remains pending with an explicit explanation.

Completion requires execution of the requested experiment scope, analysis and
summary, and successful publication of their evidence. Negative or inconclusive
scientific results can complete a request; execution failure or missing required
work cannot. The coordinator reports request disposition and evidence separately
from Study goal status and continuation. Publication alone is not a scientific
completion judgment.

After completion is durably published, the controller consumes the admitted
request. User-visible pending content is cleared only if doing so cannot discard
a newer user edit. Operator injection and editing coordinate with the controller
using `inputs/human.lock`. Direct edits are supported while the controller is
stopped; during execution, use `edit-experiment`, which holds the lock for the
whole editor session. Unlocked external writes cannot receive this guarantee.
The original snapshot and its result references remain in the iteration record.
Failure, STOP or unsuccessful publication retains the pending request. Recovery
uses the published request receipt so a crash after publication does not cause
the same completed experiment to be dispatched again.

## Integration

Observed in the current source: `loop.py:run_iteration` constructs iteration
input, settles work and publishes knowledge; `instructions.py` defines coordinator
policy and result schema; `roles.md` defines Proposer and other specialist roles.
The interface reuses these entry points and the existing iteration
archive and publication receipt. It adds no scheduler, task algorithm or resource
authorization. Existing uncommitted changes must be preserved.

## Acceptance

Focused controller tests and a tracker-free local smoke must demonstrate:

- Missing and empty requests preserve autonomous behavior.
- A request reaches planning before autonomous direction selection and retains
  its exact admitted snapshot.
- A completed, analyzed and published request is consumed, including when its
  scientific result is negative.
- Failed, blocked, interrupted and unpublished work retains the request.
- User edits made during execution or consumption are preserved.
- Restart after publication recognizes completion without rerunning the request.
- Lead and specialist write attempts against the user input are denied by the
  effective permission configuration.

Implementation and verification are local to this task. Acceptance evidence must
distinguish controller/CPU tests from actual model execution and permission
enforcement. Model or remote compute execution requires its existing authorization.
