# Execution backends

An execution backend connects an accepted Plan to one external execution. The
controller owns the Plan, submission claim, Agent deadline, budgets and Review.
The adapter owns source/input preparation, transport, durable execution identity,
accounting and evidence. Task code stays outside `src/research_loop/`.

## Unified launcher

Launch an initialized Study through the shared command:

```bash
uv run research-loop run /absolute/study --iterations 10 \
  --backend /absolute/task_adapter.py \
  --backend-config /absolute/backend.json
```

The trusted adapter is a Python module, not an Agent tool or a per-Study runner.
Its immutable JSON configuration identifies the task's data, backend and grant.
The launcher registers both absolute paths and a configuration fingerprint in
`study/execution-backend.json`; later `run` commands automatically use that
registration. A changed adapter path or configuration blocks. Version the deployed
adapter and runtime explicitly; the registration does not hash Python source.

The module exports this factory:

```python
def create_backend(config, *, study_root, workspace, run_dir, context, stop, deadline):
    return Backend(...)
```

The factory is side-effect-free. `context` is the original frozen Execute input;
`run_dir` is the controller attempt, and `deadline` is its original absolute Unix
deadline. `stop` is a threading Event. The bound backend implements the three
operations below. `start` and `inspect` return a complete native Execute response
(`outcome`, `markdown`, `artifacts`, `measurements`) built from trusted receipts.
`inspect` returns the same terminal response or raises when identity, accounting,
or effects remain unresolved. It never submits another operation. Both preparation
and start must recheck the stop signal and original deadline. Adapters must bound
transport calls, cooperate with cancellation, and settle/account their own jobs;
the generic bridge cannot forcibly terminate a Python backend thread.

Only `Plan.execution.mode=script` uses this bridge's computation path. The Agent
gets a report-only schema, authors permitted source, and uses the shared submission
API with `action=research`. A single bounded worker watches requests, calls
side-effect-free `prepare()` before atomic claim, then calls `start(prepared)` once.
Only explicit `ParameterError` from preparation permits live source/request
correction before claim. A retained claimed receipt is never cleared to retry.
The Agent runs on the main thread so native interruption terminates its process
group and signals the backend worker. Unknown effects block completion and Review.

The bridge retains raw Agent context/output under `attempt/agent/`, a separate
`backend-response.json`, and the verified outer completion. The workspace interface
contains `status` plus that trusted response. Neither Agent prose nor Agent numbers
construct measurements. Start, inspection, retained response and workspace interface
must agree before completion. Read-only stages use the configured Codex/Pi harness.
An explicit null Plan execution is qualitative: this enforcing bridge runs Execute
read-only, invokes no backend, and accepts only empty artifacts/measurements.
Ordinary harnesses retain their existing computational Plan requirements.

Before the first Agent in a new iteration workspace, the bridge removes only
bounded transport files that exactly match files inherited from Git HEAD. A durable
initialization record makes that operation restartable. It preserves the submission
lock inode and never clears fresh or changed interfaces. Reentry in the same
iteration cannot erase an accepted receipt.

The module may additionally export `create_controller(config, *, study_root)`.
Construction is side-effect-free. Its optional existing controller hooks are
`admit_iteration(iteration)`, `commit_iteration(iteration, token)`,
`call_allowance(remaining)`, `validate_plan(result, *, iteration)`,
`response_schema(phase, context, schema)`, `validate_completed(result, *, workspace,
run_dir)`, `diagnostic_context(iteration)`, and `close()`. Admission activates the
explicit task grant under the Study lock. Grant creation, absolute wall deadline,
GPU accounting and owned-job settlement belong to the adapter; the launcher grants
no resources. Validation must return the unchanged trusted response. Diagnostic
context contains bounded public evidence and accounting references, never secrets
or protected configuration dumps. Cleanup affects only jobs owned by that
invocation. Cleanup runs before the native Study releases its controller lock;
a duplicate launcher that cannot acquire that lock performs no cleanup. The native
Study continues to own model-call/time budgets and Review.

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

## Execution settlement

Fresh script executions retain a controller-owned journal binding the original Execute
attempt and accepted Plan before preparation. The backend durably marks submission
intent before calling the external submitter. A missing job ID, an empty queue or
a transport exception is not proof that submission failed. Once intent is marked,
unknown effects remain blocking; even a nonzero submit exit can reflect a lost
scheduler response rather than a proven rejection. The external native submitter
currently provides no trusted positive-rejection certificate, so this case cannot
release reserved capacity through the new closure path. Inspection never submits
a replacement.

The ProTok controller uses one `--reconcile-execute --authorization ...` entry
point for retained results. It verifies that the original Agent is stopped,
validates the backend's terminal evidence, and publishes a controller completion
in the original attempt. Raw Agent output, Plan, deadline, call count, charges and
STOP are retained. Repeating reconciliation reuses the same publication. It does
not authorize another allocation or reset any budget. Independent Review is the
next ordinary stage only when the original authorization and budgets permit it.

A proven never-submitted operation closes as incomplete with no measurements and
zero allocation cost, without inventing a job ID or Slurm accounting record.
Accepting that incomplete response still enforces protected workspace and fixed
candidate constraints; proof of no computation does not waive source permissions.
If staging already created a native job record, a bound `not_started` receipt
closes its outstanding reservation with zero charge and no scheduler fields;
repeated closure does not credit the budget again. An allocated operation requires
its real terminal accounting. Effects that cannot
be established remain unresolved. Pipeline failures do not automatically become
Agent retries or scientific evaluation results; closure is an explicit operation.

For settled scripts, verify the frozen executed source and retained receipt
separately from the current workspace. Extra reports created after submission do
not erase an already verified execution. They are not added to the frozen source
or treated as measured output. The originally executed files must still match, and protected workspace changes
remain blocked. Source/configuration/receipt identity remains mandatory. Reconciliation has no historical-state import or migration mode.

Result reconciliation concerns the retained execution. Runtime replacement is
broader: deployment checks every affected lane, including independent long jobs.
Neither path changes the allocation grants of other lanes.

## Recovery certificates

Use the shared controller for recovery instead of writing a per-study runner to
edit `state.json`, publish completion receipts or append evidence to Review prompts.
Keep experiment preparation, transport, input/source checks and accounting in the
backend. A recovery callback runs under the Study lock, so do not acquire that
lock again. Host-level supervisor/runner locks remain the adapter's responsibility.

```python
def certify(state):
    # Trusted backend code: inspect this exact retained attempt, verify original
    # authorization, immutable Plan/source, deadline and positive effect evidence.
    proof = backend.inspect()  # never submits a replacement
    verify_recovery_contract(state, proof)
    return {
        "source_iteration": f"{state['iteration']:04d}",
        "source_attempt": state["calls"]["execute"],
        "plan_ref": state["plan_ref"],
        "action": "settle",
        "effect": "terminal",
        "summary": "Original execution is terminal and its costs are settled.",
        "proof_refs": [proof.reference],
        "response": response_from_verified_receipt(proof),
    }

study.reconcile_execution(recovery_id="execution-settlement-0001", certify=certify)
```

This sketch's verification/response functions are task-specific; the runnable
[offline example](../examples/recovery_demo.py) demonstrates the complete shared
recovery path. `source_attempt` and `plan_ref` are Study-relative references.
`proof_refs` identify retained backend evidence and must not contain secrets.
Use a stable recovery ID per operation, and return the same certificate on a retry
of interrupted recovery. The callback may retain its private evidence/authorization
record; do not include those full snapshots in `summary`.

For `retry` or `prepare_continuation`, `effect` must be `never_submitted` backed by
positive evidence from before the external submission boundary. For `settle`, it
may be `terminal` or `never_submitted`; include a validated Execute response.
Unresolved effects cannot be certified. Neither a terminal failure nor a zero-cost
closure grants another compute allocation. A new attempt still faces original
Study budgets and the backend's unchanged absolute deadline and resource grant.
Same-iteration retry also retains the failed Execute's original per-call absolute
deadline; it cannot obtain another full timeout window. Published continuation is
the existing new-iteration path with a new per-call timeout, subject to the original
Study and backend resource limits.

A controller request marked `accepted` is distinct from external submission.
For a certified retry the core uses `retire_unstarted_request` to retain the
original interface contents and certification under the old attempt before
allowing another request. The shared helper preserves its lock and can finish an
interrupted archive without touching a later request. Adapters must not delete
claimed receipts as a substitute for proving non-submission.

ProTok's existing `--resume-unsubmitted` and `--reconcile-execute` commands now
submit certificates to this API. Their resource/identity/STOP checks remain
adapter-owned; normalized recovery state and evidence delivery belong to core.
The formal resume path still refuses retained pending/accepted interfaces; this
change does not import arbitrary private retirement files or widen its grants.
The controller reads only small recovery/control metadata at recovery and context
construction, with no dataset/checkpoint scans or per-rank/worker checks.
