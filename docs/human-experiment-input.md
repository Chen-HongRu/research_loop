# Human experiment input

The reference research workflow accepts priority experiment requests through the
shared intervention interface. The request selects work within the existing goal,
scientific protocol and resource authorization. It grants no additional compute
or model authority.

## Submit and inspect

```sh
uv run research-loop intervene RUN --kind experiment --file /path/to/experiment.md \
  --id experiment-one --expected-revision r000001
uv run research-loop query RUN --kind interventions --id experiment-one
```

The text file is read once by the caller and submitted as `{text: ...}`. The
workflow requires nonempty text. Python callers use
`application.intervene(root, kind="experiment", payload={"text": text}, ...)`.
Human and separately authorized Codex callers use the same interface; caller
identity comes from the submitting process, not a role claimed in the payload.

Submission persists an immutable request under `RUN/interventions/requests/`.
The same ID and identical content returns the existing receipt; conflicting
content is rejected. Submit a new ID for a changed request. Editing the original
text file does not alter an already submitted request. `expected_revision` guards
the requested contract; by default the application selects the target's current
revision. The standard experiment command targets the whole run, and the workflow
admits it once at a valid boundary.

## Admission and completion

The controller supplies the admitted request to the research workflow before
Direction chooses its experiment. Explicit experiment input skips autonomous
Scout exploration; the configured planning Agent completes missing design details.
Ambiguity that changes experiment meaning requires clarification. Incompatible,
blocked or incomplete work retains an explicit disposition.

Intervention states are received, pending, applied or rejected. An input receipt
is persisted before the admitted Group's side effects; applied is a projection of
that admission. It does not mean the experiment is complete or the scientific goal
is achieved.

Research completion requires the requested execution scope, independent evaluation,
analysis and successful publication. Negative or inconclusive scientific results
can complete a request; failed execution or missing required evidence cannot.
The published receipt exposes `work_outcome` separately from the intervention's
applied state. Publication acknowledgement identifies the admitted request ID,
so an older result cannot complete or clear a newer request.

Failure, STOP or unsuccessful publication does not acknowledge completion.
Recovery consults original input/operation/publication receipts rather than
redispatching a completed experiment. An uncertain command outcome requires
reconciliation; it cannot be repeated merely because an input remains pending.

## Integration and verification

`application.py` submits requests; `interventions.py` persists requests and
receipts; `controller.py` owns admission; `research_workflows/research.py` validates
experiment input and constructs the scientific context. The research contract
checks execution/evaluation evidence before proposing `input_outcome` to the
generic publisher. `publication.py` commits and recovers that outcome from the
activated `outputs/current` publication.

[Operations](../references/operations.md) defines control, queries and revisions.
Controller tests and local smokes must distinguish admission from completion,
retain unfinished requests, preserve newer inputs and recover committed outcomes
without repeating work. Independent Agent contexts and configured write roots do
not establish filesystem read isolation. Real model, permissions and remote
execution acceptance require their own authorized evidence.
