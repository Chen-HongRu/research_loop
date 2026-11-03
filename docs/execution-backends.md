# Execution backends

An execution backend connects an accepted Plan to one external execution. The
controller owns the Plan, submission claim, Agent deadline, budgets and Review.
The adapter owns source/input preparation, transport, durable execution identity,
accounting and evidence. Task code stays outside `src/research_loop/`.

`research_loop.execution.ExecutionBackend` has three operations:

```python
class Backend:
    def prepare(self):
        # Read bounded authored source and trusted input metadata.
        # Raise ParameterError only for files/arguments Execute may legally fix.
        # Return a snapshot binding the frozen Plan, source and input identity.
        return prepared

    def start(self, prepared):
        # Verify authorization/deadline/budget and the exact frozen source.
        # Persist the execution identity BEFORE any staging or submission.
        # Start once, then return the same execution's trusted receipt.
        return receipt

    def inspect(self):
        # Read/reconcile the retained execution. Never submit a replacement.
        return receipt
```

The existing submission bridge supplies correction feedback; a backend does not
need its own retry loop. For example, inside the controller's original live loop:

```python
from research_loop.submission import claim_request

prepared = {}

def preflight(request):
    validate_action_against_frozen_plan(request)
    prepared["value"] = backend.prepare()

request = claim_request(workspace, preflight=preflight)
if request is not None:
    receipt = backend.start(prepared.pop("value"))
```

`None` means no pending request or an explicit parameter rejection. Continue
polling within the original Execute deadline so the same Agent can read status,
repair authorized source and submit again. Any other exception stops this path.
The backend must recheck the original deadline before claim/start. Local source
freezing may retain its own record; durable execution identity must precede
external staging and submission.

An adapter may implement `validate_plan(result, *, iteration)` to check input
references before Plan acceptance. The controller invokes this optional hook
within its existing Plan validation and parameter-correction path, including for
cached unaccepted replies. Only explicit `ParameterError` requests correction;
other adapter errors, including Pydantic errors from trusted metadata, block.
The accepted Plan is not rechecked or replaced by this hook. Every correction
uses the same task/context, original absolute timeout and existing call/time budget.

ProTok reuses the execution input resolver for this check. Unknown source names,
invalid checkpoint syntax/steps, future sources and genuinely unavailable or
unpublished checkpoints return Plan feedback. Published evidence of a script or
incomplete investigation with no fixed measurements also makes that reference
unavailable. Corrupt or missing evidence in an already published chain, mismatched
identity/accounting and I/O failures block. The check reads only local trusted
configuration/publication/receipt metadata; it neither probes remote inputs nor
scans data or checkpoints. Remote file existence remains an execution-preparation
check, so Plan acceptance does not guarantee later input availability.

`prepare` is the dry-run boundary and the actual start path uses its result. It
may inspect small root manifests and file metadata, including through remote
read-only commands. It does not stage files, reserve compute or submit jobs.
It cannot prove installed imports, runtime behavior or scheduler availability.
Datasets, caches and checkpoints are not scanned or hashed. Compilation checks syntax with the controller's Python without importing or
executing authored code; the target interpreter may differ.

The controller calls `prepare` before claiming a request. An explicit
`ParameterError` retains feedback for the same live Execute Agent: fix the
reported authored source or request action and submit again within the original
Plan and deadline. Correction does not launch another Agent or renew any budget.
A frozen Plan's invalid input or policy, protected-source change, I/O failure,
controller exception or arbitrary `ValueError` blocks; it is not parameter
feedback. Source changed after preparation also blocks. Do not catch all backend
exceptions and turn them into repair instructions.

`start` must bind the prepared snapshot to the bytes actually executed. Comparing
the live workspace alone is insufficient: check the frozen source before remote
effects, including when the workspace changes during freezing and changes back.
Persist identity before staging/submission. After a submission with an unknown
result, retain its state and reconcile its owned job; never clear the identity and
call start again. This provides at-most-once start, not exactly-once scheduler
execution. Cancellation, where supported, must preserve identity and settle costs.

The active ProTok implementation is `ScriptExecutionBackend` in
`integrations/protok/research_execution.py`; the watcher passes its prepared value
to `start`. Its `inspect` accepts an already settled result. Owned-job recovery
uses `research_backend.run_script(recover=True)`, which reconciles the retained
job and refuses to submit even a retained prepared job. `prepare_script(...)` is
the native dry-run API; `run_script(..., prepared=prepared)` uses the same metadata.
These are Python APIs, not additional command-line commands.

A known settled compute failure permits repair only under its existing grant.
ProTok formal candidate repair has a bounded recovery path; every new repair
checks fresh call/time budgets and leaves one call for independent Review.
The original wall deadline still applies; reserving a call does not guarantee
enough time for Review to finish. Research scripts currently authorize one shared
allocation. A settled failed script goes to Review with incomplete evidence;
its grant does not authorize another allocation or use formal debug grants.

A new adapter's minimum checks are: a repairable missing source can be corrected
before claim with zero allocations; changed prepared/frozen identity blocks;
unknown submission effects cannot start a second job; settled costs survive
reconciliation; and bounded repair preserves independent Review. Use a small
local transport fixture first and obtain resource authorization before a real run.
