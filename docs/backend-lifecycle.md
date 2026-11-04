# Backend lifecycle and reservations

Stage 5 of [#77](https://github.com/Chen-HongRu/research_loop/issues/77) provides
the `Operations` authority and `BackendDriver` through Python APIs for new SQLite
Studies. Schema 5 rejects older prototype databases. Concurrent scheduling,
SQLite CLI dispatch and explicit migration remain subsequent work. The serial
controller's separate launcher is documented in [execution backends](execution-backends.md).

## Accepted requests and authority

An accepted, context-bound Execute may contain a finite `operations` list.
Acceptance seals the list and its accepted Plan in the same transaction as the
Execute receipt. Each operation has an independent revision; backend progress
does not keep an Agent call open or modify its accepted output. Missing or empty
`operations` preserves direct artifact execution.

Each operation has exactly these fields:

```json
{
  "id": "candidate-run",
  "backend": "configured-backend",
  "authorization_id": "development-grant",
  "resources": {"worker-slot": 1},
  "cost_bounds": {"accounting-unit": 20},
  "timeout_seconds": 600,
  "input_artifacts": [{"path": "checkpoints/source", "version": "fixed-version"}],
  "parameters": {"entry": "task-owned-entry"}
}
```

IDs are unique 1–64 character slugs within Execute. Durable operation identity is
`<execute_attempt_id>:<id>`. Artifact versions and backend parameters are opaque
to the generic engine; the trusted adapter validates their meaning. The engine
retains their immutable accepted request reference rather than copying payloads
into SQLite. Resource quantities are positive integers. Cost bounds are
nonnegative integers in explicitly configured units; no currency conversion is
implied. Empty resource/cost vectors are permitted for explicitly authorized
operations that need neither.

The immutable Study configuration supplies:

```json
{
  "execution_backends": {
    "configured-backend": {
      "version": "adapter-v1",
      "environment": {"id": "execution-environment", "version": "fixed-v1"}
    }
  },
  "resource_pools": {"worker-slot": 2},
  "operation_authorizations": {
    "development-grant": {
      "backends": ["configured-backend"],
      "max_operations": 4,
      "resource_limits": {"worker-slot": 1},
      "cost_limits": {"accounting-unit": 60},
      "deadline": null
    }
  }
}
```

These three configuration fields are protected Plan/Execute context inputs when
configured. Operations require an accepted `Plan.timeout_seconds`; each requested
timeout must fit it. Acceptance freezes the absolute operation deadline at the
earlier of acceptance time plus timeout and the authorization deadline. Waiting,
recovery and later submission do not restart this clock. A non-null authorization
deadline is a finite UTC Unix timestamp.

The backend implementation's version/environment must match the frozen binding.
The configuration identifies authority already granted by the operator; an Agent
cannot create or enlarge it by proposing an operation.

## One external submission

```python
from research_loop.runtime.backends import BackendDriver
from research_loop.runtime.operations import Operations

operations = Operations(owner)
driver = BackendDriver(owner, {"configured-backend": trusted_adapter})
driver.prepare(operation_id)
driver.submit(operation_id)
# Later, including under a new coordinator owner:
driver.inspect(operation_id)
```

The adapter implements `prepare`, `submit`, `inspect`, `cancel`, `collect`, and
`settle`. Each returns `{event_key, data}` from its trusted journal. It owns durable
external identity, bounded callbacks, environment isolation, task validation and
actual deadline enforcement. Python interfaces are not an OS sandbox. The driver
makes no automatic model call, retry loop or repeated training request.

| Action | Contract |
| --- | --- |
| Prepare | Allocate no compute and incur no charge. Return `ready`, `reason`, and `effective_parameters`. Work that cannot meet this condition belongs in submit after reservation. |
| Submit | Commit one stable token, original owner epoch, operation count and holds before calling the backend. Return `outcome`, `job_id`, `proof`, `reason`. |
| Inspect | Address the original operation/token. Return `job_id`, `terminal`, `outcome`, `physical_released`, `not_submitted_proof`. |
| Cancel | Request cancellation of that identity; return `job_id`, `acknowledged`, `reason`. Acknowledgement releases nothing. |
| Collect | Return actual immutable `artifacts`, `evaluations`, and optional `unavailable_reason`. Partial artifacts may accompany an explicit unavailable result. |
| Settle | Return `final_costs`, mapping known units to their authoritative final totals. Missing units remain unresolved. |

`prepare` moves pending work to prepared or records a known pre-submit failure.
`reserve_submit(id, expected_revision)` admits only its first intent, moving to
submitting. Submit responses are accepted, rejected, unknown or not_submitted;
accepted responses require a real job ID. An exception or malformed outer result
from the submission callback records unknown. Process interruption after intent
leaves that intent unresolved. Neither case permits another submit.

Inspection can establish succeeded, failed, cancelled, timed_out or not_submitted.
No-submit proof is `{basis, reference}`. After intent, basis must establish
`sender_retired` or `backend_no_late_create`; a missing job, empty queue, elapsed
lease or newer coordinator epoch is insufficient. Before intent,
`no_submission_intent` can certify that no sender was admitted. Proof references
are trusted adapter assertions; shape checks do not authenticate them. A genuine
no-submit outcome can have `job_id: null`.

New submissions also check Study STOP, wall budget, admission deadline and the
frozen operation deadline. Operation counts are separate from Agent call counts
and are charged irreversibly at submit admission, including later rejection or
unknown outcome. Inspection, cancellation, collection, settlement and feedback
intake remain available after STOP or exhaustion.

## Holds, receipts and contradictions

Admission requires both the shared physical pool and the authorization's own
resource limits to fit. For every cost unit:

```text
settled actual cost + unresolved cost holds + new bound <= authorized limit
```

Resource release and fee settlement are independent. Positive physical release
can free a slot while fees remain unknown. A missing actual cost is never zero;
explicit zero settles that unit. Authoritative overruns are recorded even when
they exceed a bound or total authorization, and block further admissions under
that authorization. They are not rejected to preserve a misleading budget.

Receipts bind operation/request, accepted Plan/Execute, backend version,
environment, original submission token/epoch, and their payload. Raw receipts
are immutable files; SQLite stores references, identities and derived control
metadata. New receipts use operation revision checks. Exact stable event-key
replay returns its original receipt without another charge, release or settlement.
Changed facts under an existing event key conflict.

A new observation contradicting proven non-submission or terminal release appends
an inconsistency, retains the historical decision, restores conservative exposure
holds and quarantines its authorization. A different job ID under the same
operation/token preserves the original ID and records the unexpected IDs. Because
additional allocation and fees are unknown, all affected physical pools are then
blocked across authorizations; the restored vector is only a conservative minimum.
Closing or settling one job cannot discharge that aggregate exposure. Cleanup may
inspect/cancel jobs and record known costs, but explicit repair is needed to
resolve the quarantine. This stage supplies no automatic repair or unblock API.
Historical Review/publications remain unchanged; quarantined operations cannot
admit new measurements. An already-excluded historical wall interval may be
uncertain; no duration is invented.

The indexed `external_open` flag tracks execution/allocation exposure even when
no pooled resource units were requested. A gracefully closed coordinator gap is
charged once on reopening while such exposure remains. Prepared work without
intent and terminal, physically released work with only fees/collection pending
do not charge that gap. An unclean owner interval remains conservatively charged.

## Collection and Review

Collection descriptors are:

```json
{
  "artifacts": [{"path": "outputs/candidate", "version": "actual-version"}],
  "evaluations": [{"receipt_ref": "artifacts/receipt-id.json", "allow_feedback": true}],
  "unavailable_reason": null
}
```

An `unavailable_reason` declares missing outputs and makes collection final. A
genuine evaluator receipt for an exact artifact in the collected subset remains
eligible for normal registration; missing outputs never imply measurements.
Collection itself is not a measurement. Each allowed evaluator receipt must be
registered through `MeasurementRegistry.register(iteration_id, receipt_ref)` or
explicitly rejected through `operations.reject_evaluation(receipt_ref, reason)`.
Denied descriptors are rejected without reading their evaluator bodies. The
trusted adapter must mark final-only sources denied; collection cannot make them
feedback-accessible.

An operation evaluator receipt uses version 2 and exactly the
[version 1 fields](knowledge-publication.md#measurements), plus `operation_id` and
`collection_event_key`. Its operation, collection event, accepted Plan/Execute,
artifact version and frozen evaluation must all match. The collection event key
is known before publication, avoiding circular file references. Version 1 remains
the direct accepted `Execute.artifacts` path. Collected artifacts never append to
that frozen list.

```python
operations.finalize_feedback(execute_attempt_id)
```

This seals feedback only after all operations have known terminal outcomes,
collection has completed or explicitly failed, and every evaluator intake has a
final disposition. Review reservation checks that seal in its transaction. New
measurements are then refused even before Review dispatch; exact registered
receipt replay remains valid. Review may proceed while release confirmation or
fees are outstanding, with their holds still constraining future work.

## Local verification

```bash
uv run python examples/backend_lifecycle.py
```

The temporary example launches one bounded local Python child, intentionally
loses its submit response, closes/reopens coordinator ownership, inspects the
same job, collects its arithmetic result and registers a version 2 evaluator
receipt. Review starts before zero-cost settlement. It asserts one submission
and no remaining holds. Synthetic arithmetic validates wiring, not scientific
benefit or production backend behavior; it uses no model, network or GPU.

Request/receipt bodies and adapter callbacks run outside write transactions.
Transaction checks read control records and small file identity metadata, never
dataset/checkpoint contents or hashes. They run in the controller, not per rank
or loader worker. Backend-specific artifact/protocol checks remain adapter work;
their costs and production throughput have not been measured here.
