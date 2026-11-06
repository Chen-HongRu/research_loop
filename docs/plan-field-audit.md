> Historical record: this describes the former runtime. See [current entry](../README.md) and [retirement](runtime-retirement.md).

# Plan field consumers

The version-3 Plan keeps scientific intent in the existing frozen `markdown`.
Execute receives that Plan; independent Review receives the same Plan and actual
evidence. Text should specify the question, allowed action and fixed conditions,
input paths/versions, measurement or derivation, evidence and interpretation as
needed. Scientific correctness is judged from evidence, not string completeness.

Pydantic v2 models define the structured fields. `OutputSchema` projects each
model into the existing CLI JSON interface and uses that model for strict local
validation. Dynamic choices and controller-owned omissions derive models before
schema generation; there is no schema-to-model reconstruction. Validation returns
the original JSON dictionary, retaining its numeric representation and receipts.
Cross-field Python validators are local checks, not promises that every condition
can be encoded in the model-facing JSON Schema.

ProTok supplies its execution-model constraints through the controller's
`response_schema` hook. Both initial calls and cached completion acceptance use
that model. Budget, source identity, external effects and accounting remain
controller checks. CLI harnesses, Study version and the field set are unchanged
by the Pydantic migration.

Before accepting a fresh or cached unaccepted Plan, the optional `validate_plan`
hook reuses ProTok's local execution input resolver. Explicit unknown, invalid or
unavailable references use existing Plan correction within the original task,
deadline and budgets. Trusted publication/receipt corruption blocks. The check
reads local metadata only; remote input existence remains execution preparation.
Accepted Plans are not rechecked or replaced by the hook.

| Retained field | Concrete consumer |
| --- | --- |
| `decision`, `stop_reason` | Core chooses execution, reselection or stop and records the reason. |
| `markdown` | Core writes `plan.md` and passes the frozen scientific contract to Execute and Review. |
| `route_research` | Core initializes the separate document for a new route. |
| `base_experiment` | Controller supplies the selected base; core restores its committed workspace. |
| `references` | Core verifies earlier reviewed DAG nodes and publishes evidence edges. |
| `local_evaluation` | Core binds allowed measurements, eligibility and Frontier comparison. |
| `timeout_seconds` | Core bounds Execute; fixed long-training controllers supply their authorized allocation value. |
| `execution.mode`, `entrypoint` | Adapter routes fixed evaluation versus an authored script and selects its entrypoint. |
| `execution.gpu`, `time_limit_seconds` | Generic declaration and ProTok admission enforce the supported allocation profile and limit. |
| `execution.inputs[].name`, `.source` | Adapter validates references before Plan acceptance, then resolves trusted inputs, mounts named paths and records provenance during preparation. |
| `execution.outputs` | Adapter collects only declared bounded output files. |

The removed `contract` prose fields and research purpose/question/explanations/
observables/interpretation/inconclusive fields had no computation or scheduling
consumer. Their validators checked strings, counts and ID links; their scientific
content is now free text in `markdown`. `input_artifacts` was stored/displayed but
did not restore or verify inputs; exact external versions belong in the text,
while actual workspace restoration still uses the controller's base revision.

Removing input `role` intentionally removes a declared-use label gate, including
rejection of final/holdout labels. It does not permit final data: trusted source
resolution, train/val-only cache metadata, fixed evaluation feedback rules and
the prohibition on feeding final-only evidence into search remain. Labels never
selected files, changed mounts or supplied permissions.

ProTok freezes the entire accepted Plan along with source signatures, so prose
cannot be replaced after submission. Source/effect/accounting checks, STOP and
independent Review remain required. New initialization uses Study format 3;
older formats are rejected before runtime state writes. Existing evidence is not
rewritten or migrated. The read-only dashboard can still display old records.
