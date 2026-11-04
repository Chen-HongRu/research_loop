# Knowledge publication

This is stage 3 of [#77](https://github.com/Chen-HongRu/research_loop/issues/77),
available through Python APIs on new SQLite Studies. The serial CLI retains its
existing document format. [Planning](task-planning.md) consumes its frozen knowledge
and publication events. Concurrent experiment dispatch, production role adapters,
backend receipt collection and explicit migration are later stages. SQLite schema 4
rejects earlier prototype schemas; no existing Study is converted automatically.

Measurement registration and combined Frontier/document reconstruction require a
valid, non-null, frozen `config.global_evaluation` using the existing Evaluation
schema. Local-only measurements still require that Study contract. StateStore and
knowledge-only publication can exist without it; the minimal state/context API
examples therefore do not by themselves configure these two stage-3 operations.

## Measurements

```python
from research_loop.runtime.measurements import MeasurementRegistry

measurement = MeasurementRegistry(owner).register(iteration_id, receipt_ref)
```

Only the trusted backend/controller invokes registration. It supplies a canonical
immutable artifact reference containing these exact evaluator receipt fields:

```json
{
  "version": 1,
  "iteration_id": "iteration-identity",
  "execute_attempt_id": "accepted-execute-identity",
  "candidate_id": "candidate-identity",
  "artifact": {"path": "candidates/model", "version": "artifact-version"},
  "evaluation_id": "evaluation-version",
  "comparison_scope": "fixed-comparison-scope",
  "evaluator": {"id": "evaluator-identity", "version": "evaluator-version"},
  "metrics": {"score": 0.5}
}
```

The iteration must have accepted context-bound Plan and Execute outputs. The
receipt names that exact Execute and one of its declared artifact versions.
Evaluation ID and comparison scope resolve to the frozen Study global definition
or accepted Plan local definition. Local evaluation requires the task's stable
`specification.route_id`; a local definition cannot reuse the global ID. Within
one route, a local evaluation ID cannot change definition. Different routes have
independent local comparisons, even if their evaluation IDs match.

Only feedback-enabled development evaluations enter this registry. Provided
metrics must be finite numbers, excluding booleans. Missing objectives or
constraints remain incomplete observations and are excluded from Frontier
membership. Extra finite diagnostic metrics do not become optimization targets.
Execute and Review narrative scores never supply or replace these numeric values.

Registration must finish before the first Review reservation; failed Review
attempts do not reopen the measurement set. The same receipt replays unchanged,
including after Review or restart. A second receipt for the same
`(iteration_id, candidate_id, evaluation_id)` conflicts. New measurements after
Review require a separate authorized investigation.

The receipt records evaluator attribution, not cryptographic authenticity. The
backend must authenticate origin, verify that the evaluator honored the declared
protocol and exact input artifact, and perform any required repeat aggregation.
This layer does not authenticate arbitrary filesystem writers, inspect remote
checkpoint contents, certify numeric truth or infer statistical significance.

## Accepted knowledge and eligibility

```python
from research_loop.runtime.publication import Publisher

receipt = Publisher(owner).publish(call_identity)
snapshot = Publisher(owner).snapshot()
```

Only an accepted, context-bound Scout or Review call can publish. Its trusted role
validator normalizes the result to exactly `knowledge`, `reviews`, `corrections`
lists. Raw reports, including original source-access metadata, remain in the
accepted proposal artifact. Publication uses that artifact without copying a
whole Study state or treating a report summary as additional evidence.

Each knowledge entry has exactly:

| Field | Contract |
| --- | --- |
| `id`, `expected_revision` | Stable entry identity; zero creates, positive revision updates |
| `kind` | `external`, `local_measurement` or `interpretation`; revisions cannot change kind |
| `statement` | Nonblank attributed claim or observation |
| `conditions` | Nonempty list of explicit applicability conditions |
| `source_refs` | Accepted local evidence references or HTTP(S) source URLs without credentials |
| `measurement_ids` | Registered measurements; their receipts must be accepted evidence |
| `interpretation` | Separate text; empty is permitted |
| `limitations` | Nonempty list of explicit uncertainties or limitations |

External entries need sources. URLs are attributed claims; publication does not
fetch them or certify their assertions. Local-measurement entries require Review
and registered measurements. Historical measurements need published eligibility;
Review may also cite its own newly registered measurements. Scout cannot originate
measurement eligibility. Interpretation remains marked as unverified in research
views; categorization alone does not prove a claim.

Each initial `reviews` item is exactly
`{measurement_id, valid, verified, reason}`. Both flags are booleans and reason is
nonblank. Review must cover every registered measurement of its iteration exactly
once. A qualitative investigation may have no measurements and an empty list.

Each `corrections` item adds `expected_revision` (positive integer) and nonempty
`evidence_refs` to those fields. It targets previously published eligibility and
cites the original evaluator receipt plus any additional accepted evidence.
Corrections only append eligibility decisions; they never alter metrics,
artifact versions, evaluator identity or evaluation definitions. A separate
qualitative investigation can review historical eligibility without retraining.

The predecessor revision must have been visible at the call's frozen knowledge
version and must still be current. Guessing a newer revision cannot overwrite a
decision the caller did not observe. Typed historical `measurement_ids` also need
eligibility visible at that frozen version; the current Review's own measurements
are the explicit exception. A receipt present in the evidence allowlist alone
does not imply published historical eligibility.

Target revision, kind or frozen-provenance conflicts become durable records while independent entries
in the same publication may append. Conflict records identify the payload array
(`knowledge`, `reviews`, `corrections`), index, expected/actual revision and reason.
They preserve rejected requests for later resolution; they are not accepted
knowledge. Publication sequence is independent of iteration identity. An exact
duplicate call returns its original publication receipt. STOP, limits, spent calls
and other iterations are never restored from the worker's context.
Each new publication emits one durable planning trigger. Duplicate publication
replay does not emit another. Review completion also advances the pool version;
planning itself does not automatically create another planning trigger.

## Derived views

```python
from research_loop.runtime.projections import ProjectionWriter

pointer = ProjectionWriter(owner).rebuild()
```

`build_views(snapshot, global_evaluation)` and
`render_research(snapshot, publication_payloads)` in `runtime.publication_views`
are pure projections. Frontier calculation uses all historical measurements and
their latest published eligibility, so disqualifying a winner can restore a
previously dominated candidate. Global comparisons require the exact user-frozen
definition. Local comparisons are partitioned by route and accepted definition.
Local improvement does not change Global Frontier membership.

The research document shows current accepted knowledge revisions, attribution,
conditions, sources, limitations, measurement eligibility and correction reasons,
and pending conflicts. Original claims remain attributable after qualification
changes; the current eligibility section makes their status explicit. It does
not create a new scientific summary or rewrite the recorded numeric evidence.

Rebuild writes a complete `views/<version>-<generation>/` bundle containing
`frontiers.json`, `research.md`, and `manifest.json`, then atomically updates
`views/current.json` with `version`, `frontiers_ref`, and `research_ref`. Consumers
read that one pointer and its two immutable referenced files. If publication
advances during generation, rebuild raises `ConflictError` and preserves the
previous pointer. The caller can rebuild the newer version. A crash before pointer
replacement leaves a complete prior view; unreferenced partial bundles are caches.
Missing or corrupted derived files are rebuilt from authority, never imported as
state. The pointer may lag the database; compare its version to
`StateStore.snapshot()["knowledge_version"]` when freshness matters.

The controller reads the specific accepted proposal/evaluator JSON needed for
registration or document reconstruction outside write transactions. These are
small control inputs, not dataset/checkpoint validation scans. Referenced control
files receive metadata checks to reject substitution; no hashes or training data
rereads occur. Checks run once per relevant controller operation, not per rank or
loader worker, and read zero dataset/checkpoint bytes. View reconstruction scales
with publication history. Latency and memory have not been benchmarked.

## Local smoke

```bash
uv run python examples/runtime_publication.py
```

The disposable smoke uses real local SQLite and immutable files with synthetic
role decisions and evaluator receipts. It publishes two Reviews out of creation
order, corrects a winner's eligibility without modifying its numeric record, and
rebuilds the restored Frontier and research document. It starts no model, training
process or remote job; it demonstrates control semantics, not research benefit.
