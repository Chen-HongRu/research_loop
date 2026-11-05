# Native script runtime application

This application runs Scout, Advocate, Judge, Plan, Execute and Review through the
existing [tool-free Pi transport](../native_runtime/README.md). It adds typed,
bounded script operations to the SQLite runtime. It contains no scientific task,
metric, dataset, model architecture or deployment defaults. Installing a Pi
network extension does not enable it here: calls retain `--no-extensions`,
`--no-tools`, `--no-mcp`, disabled ambient context and exact configured model checks.

Select an immutable copy of `application.py` as described in the
[concurrent runtime guide](../../docs/concurrent-runtime.md), with worker adapter
`{"id":"native-script-runtime-v1","version":"1"}`. The application configuration
has exactly these fields:

```json
{
  "transport": {
    "backend": "pi",
    "executable": "/path/to/pi",
    "provider": "configured-provider",
    "model": "configured-model",
    "reasoning": "medium",
    "timeout_seconds": 120,
    "private_calls_dir": "/path/to/private-calls"
  },
  "scientific_contract_source": "provided:science",
  "backend_handlers": {
    "script": {"handler": "script", "config": "REPLACE_WITH_OPERATOR_CONFIG_OBJECT"}
  }
}
```

The placeholder is not runnable configuration. Prepare the explicit backend
configuration described below; validation performs no network or model calls.
Freeze scientific instructions and baseline source bodies as private JSON control
artifacts in `config.context_sources`, assigned to the required roles and marked
`required=true`, `allow_feedback=true`. See the
[context guide](../../docs/context-feedback.md). The scientific contract object
must contain `baseline_ref` and the exact original `global_evaluation` (which can
be null). It may contain `local_evaluation`, protocol, constraints and limitations;
the entire object is frozen. An absent local evaluation means null. A non-null
local evaluation requires the native evaluation schema and an explicit task
`route_id`, and cannot reuse the fixed global evaluation ID. Provide baseline
source separately, for example as `provided:baseline_source`, together with the
editable/protected scope and canonical input aliases needed to plan the task.
The application never reads private source files into prompts or adds bodies
outside the selected ContextPackage. Backend configuration is not prompt context.

A Study can further narrow each role through
[`config.role_access`](../../docs/context-feedback.md#role-access-in-a-sqlite-study).
Include the contract source in the Plan and Execute scopes. An Execute that
submits proposed files requires both `operations` and `source_edits` in its
effective actions. The application validates the file edit permission before
the controller admits an operation.

Plan returns `timeout_seconds`, `local_evaluation`, `experiment_definition`,
`baseline_ref`, `backend`, `scientific_contract`, and an `operations` list.
An empty list explicitly selects qualitative investigation with no external
execution or allocation. Its `experiment_definition` must identify a concrete
research question and the available context used to answer it; it must not
invent an experiment or result. A task that requires an actual bounded diagnostic
run must retain its script operations.
Each planned operation has the core request fields `id`, `backend`,
`authorization_id`, `resources`, `cost_bounds`, `timeout_seconds`, `input_artifacts`
and `parameters`. Planned parameters have exactly `baseline_ref` and `execution`.
Execution has exactly `mode="script"`, a `research/*.py` entrypoint, `gpu=true`,
`time_limit_seconds` (1–1800), `inputs=[{"name":"alias","source":"configured-key"}]`
and relative `outputs` paths. Plan may name a new entrypoint for Execute to create.

Execute returns `operations` and `artifacts=[]`. An empty accepted Plan requires
an empty Execute operation list. For script execution, every operation must
match accepted Plan exactly, adding only
`parameters.source_files=[{"path":"research/run.py","content":"..."}]`. Execute
cannot alter execution, inputs, outputs, baseline, backend, grant, resources,
costs or timeout. Protected paths and edits outside the trusted source scope are
rejected. The backend compiles staged Python before submission. The only accepted
resource vector is `{configured_resource_pool: 1}` and the cost bound is
`{configured_cost_unit: ceil(time_limit_seconds / 60) * 60}`. Lifecycle timeout
must cover that rounded allocation plus `staging_headroom_seconds`, which must
exceed 60 seconds. Admission reserves this staging and queue window once;
preparation and staging consume it. Core runtime authorizations remain
authoritative for capacity, total cost and absolute deadlines. Submission requires
the rounded allocation plus a fixed 60-second safety margin to fit before the
frozen operation deadline, rather than reserving the full headroom again.

The backend configuration is the exact `CONFIG_FIELDS` object validated in
[backend.py](backend.py). It explicitly provides private journal/study/baseline
roots, baseline reference, source/protected paths and edit roots, runtime files,
a positive reconciliation gate for prior shared GPU exposure, resource/cost names,
SSH target, Slurm user/partition/account/timezone/CPU/memory settings, remote root
and Python, a trusted `launcher_source`, canonical `input_mappings`, small metadata
checks, report suffix/byte limits and callback/headroom bounds. Each input mapping
has `{path, mount, kind}` with kind `file` or `directory`; no arbitrary agent input
path is accepted. Use the remote Slurm host's actual IANA `slurm_timezone`.
The trusted launcher receives `RUN` containing remote location, execution, mapped
inputs, rounded seconds and deadline. It must enforce the operator's actual
sandbox, environment, read-only canonical inputs and output persistence contract;
this adapter is not an operating-system sandbox. Do not use a demonstration
launcher as a production environment. Freeze operator configuration and private
scientific artifacts outside Git, and authorize accounts/resources separately.

Each callback performs a bounded scheduler action. The native durable submission
permit binds its token and original owner; uncertain submission is reconciled
without replay. Journals retain the original job identity through inspection,
cancellation, collection and settlement. Slurm may expose an allocation before
its identity metadata or final accounting is complete. Missing identity fields
retain the original job and resource hold without confirming terminal state or
authorizing cancellation; nonempty contradictory identity fields remain errors.
Settlement returns pending costs until the same job has been inspected terminal
and complete allocated GPU accounting is available; missing fees are never zero.
Collection includes only explicitly allowed small report formats; it does not
download datasets or checkpoints.
Reports enter Review-only `research_evidence` as unverified interpretation inputs.
They do not become evaluator measurements or Frontier scores. Review may publish
interpretation with uncertainty and limitations; it must not turn script-reported
numbers into verified scientific claims. Imported legacy cleanup uses its separate
application and cannot be routed through this native submission handler.

When the frozen Review context has no registered measurements, its response schema
requires empty `reviews`, `corrections` and knowledge `measurement_ids` arrays.
Report-backed interpretation knowledge remains available through allowed evidence
references; an operation or job ID cannot substitute for a measurement ID.

Review uses the native lossless `shared-json-subtrees-v1` prompt encoding when
repeated large nested objects can reduce bytes. Complete shared values and
explicit replacement paths reconstruct the same frozen package; report wrappers,
manifest and citation permissions stay literal. This is representation sharing,
not a summary or new evidence; shared IDs cannot be cited. Plan, Execute and the
other roles retain their ordinary prompt package format.

Run focused offline checks with:

```sh
PYTHONPATH=src:tests:. uv run python -m unittest test_native_script_application test_native_script_backend -v
```

These synthetic tests exercise typed Plan/Execute validation, local source staging
and fake scheduler responses. They establish no model, account, network, GPU,
sandbox or scientific performance result.
