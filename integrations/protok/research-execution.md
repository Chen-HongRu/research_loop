# Research script execution

New version-3 Studies enable structured execution bounds with
`"research_execution": {"version": 1}`. The ProTok controller additionally requires
`control/research-policy.json` containing `version: 1`, the user's `authorization`,
and `max_time_limit_seconds` (at most 1800). Enable these only between fully
published iterations; preserve earlier context snapshots.

Plan's `markdown` freezes the scientific question, allowed action, exact input
versions, measurement or derivation, evidence requirements and interpretation.
These are free text, without required research categories or explanation/observable
IDs. Judge and independent Review assess the actual design and evidence. The schema
checks executable paths and bounds; it does not certify scientific meaning.

Pydantic generates the Plan schema and validates the same execution model during
fresh and cached Plan acceptance. ProTok's model adds its supported GPU profile,
input-name/count and output-count constraints. The backend uses those same input
types; it separately checks the current policy, resolves trusted sources and
settles allocations. Generic Plan limits are whole minutes; the native backend
retains its scheduler rounding for internal requests.

The frozen top-level `execution` selects:

* `fixed_evaluation`: the existing protected scorer and official RMSD Frontier.
* `script`: an arbitrary Python entrypoint under `research/`, with named inputs,
  declared output files and a bounded allocation. A single script may perform
  several paired computations, inference, simulations, short updates or a new
  local benchmark. It does not need to train or produce an official score.

```json
{
  "mode": "script",
  "entrypoint": "research/probe.py",
  "gpu": true,
  "time_limit_seconds": 180,
  "inputs": [
    {"name": "cache", "source": "hidden_cache", "role": "development"},
    {"name": "checkpoint", "source": "checkpoint:0012:2000", "role": "model"}
  ],
  "outputs": ["probe.json", "probe.svg"]
}
```

Execute calls the controller-provided `submission.py submit --workspace . --action
research`. The request carries no mutable experiment specification: the controller
uses the accepted Plan. Repeated submissions observe the same request. Retained
unknown effects require recovery of the same job; they never trigger a new job.

The controller freezes protected dependencies, candidate files and research source.
The sandbox exposes `/workspace` and declared `/inputs/<name>` read-only, a manifest
at `/inputs.json`, and writable `/output` and temporary storage. In the manifest,
`mount` is the path usable by the script; `path` is host-side provenance. The script
runs in `/output` without network or host credentials. Its PID namespace terminates
descendants when the main process exits. Output collection occurs after terminal
Slurm accounting, is limited to declared regular files (16 MiB total), and retains
a bounded stdout tail. Source/configuration identities are hashed; datasets and
checkpoints are not scanned or hashed. Checkpoints are resolved from a completed,
published evaluation receipt, and must actually exist.

The operator's `control/execution-environment.json` supplies available tools, Python
paths, package versions and import names to every new proposal, Plan, Execute and
Review call. It distinguishes the lightweight Aliyun authoring environment, the
formal HPC environment and supplemental research libraries. Missing dependencies
are unmet prerequisites; agents do not install packages or assume that a library
available on one host is available on another.

An optional `control/research-environment.json` identifies a versioned supplemental
package directory, the existing Python interpreter and the verified package
inventory. Research jobs retain that manifest in their identity and receipt,
expose it as `/environment.json`, and mount its packages read-only at
`/research-libs`. Recovery uses the job's retained manifest. Formal evaluation
continues to use its original environment. Plotting uses the headless `Agg` backend;
Matplotlib, Numba and joblib use temporary cache directories.

Operators install supplements with `uv` into a new versioned directory. Resolve
against the existing base package versions, install only missing distributions,
and preserve the published directory. The install lock records transitive
dependencies; library files are not scanned or hashed at job startup.
`examples/library_probe.py` checks declared imports and versions, small numerical,
statistical, symbolic, tensor/gradient and file operations, plus headless plotting
inside the actual research sandbox. This is an operator environment check, not a
scientific investigation or an official score.

Supported inputs are `hidden_cache`, `targets`, `teacher` and
`checkpoint:<published iteration>:<0|1000|2000>` and independently reviewed long
checkpoints. Each input has only `name` and `source`; `source` determines the
trusted path and provenance. There is no input `role` label or declared-use label
gate. The current cache exposure remains the existing development train/val cache;
additional test/holdout splits and unknown sources are rejected. Final-only evidence
must not feed adaptive search. No new external data is fetched.

The research source record binds the entire accepted Plan, including its prose,
as well as the authored source signatures. A completed result cannot be accepted
against a changed Plan. Version-1/2 Studies retain their original records and must
use their original runtime; this format change does not migrate or resume them.

The initial compute profile is the existing H100 debug allocation, 8 CPU / 96 GiB,
in whole minutes up to 1800 seconds. CPU calculations also work in that profile
and consume single-GPU allocation time. A dedicated CPU-only scheduler profile is
not enabled. All research GPU allocation seconds share the existing formal budget,
deadline and reserve; independent failure-debug grants are not used. Missing
accounting blocks subsequent work.

The result distinguishes execution status from scientific support. A successful
script returns its exact evidence artifact with `measurements=[]`; a settled
failed script returns that artifact with `outcome=incomplete`. Slurm TIMEOUT is
one such result. Actual allocated GPU seconds are charged in full even when they
exceed the requested time limit during termination; the overrun is reported and
subsequent allocations must still fit the original shared budget and reserve.
A reporting Agent failure after a verified failed allocation does not resubmit
compute: the controller preserves the Agent attempt and publishes the settled
failure for Review. Unsettled effects or changed identity/source remain blocking.
Review can retain
negative or insufficient evidence without changing the official Frontier. Numerical
observables and new benchmark definitions remain explicit local evidence in the
artifact, not invented official RMSD measurements. The generic core still supports
separately defined local evaluations; this adapter's script path publishes evidence
artifacts without automatically ranking them.

`examples/path_gradient_probe.py` is a real-checkpoint executor smoke: one protein
prefix, trained radial encoder/projection weights, identical full/detach inputs and
fixed random cotangents. It checks CUDA, read-only inputs, forward parity and gradient
path removal. It does not measure an actual training-loss gradient or establish
that the path improves reconstruction.

For a failed script retained by an older runtime, use
`run.py --reconcile-research-execute --authorization TEXT` to prepare a separate
controller completion from the unchanged frozen Plan, source and settled receipt.
It retains the original attempt and STOP, consumes no new Agent call or allocation,
and preserves call/time counters. `deploy_runtime.py --resume-research` verifies
that preparation and the unchanged system-attention STOP before restoring service.
