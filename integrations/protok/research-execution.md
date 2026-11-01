# Research script execution

`study.json` enables the generic frozen research contract with
`"research_execution": {"version": 1}`. The ProTok controller additionally requires
`control/research-policy.json` containing `version: 1`, the user's `authorization`,
and `max_time_limit_seconds` (at most 1800). Enable these only between fully
published iterations; preserve earlier context snapshots.

Plan defines the question, purpose, observables, measurement inputs/method/timing,
aggregation, expected cost, interpretation and insufficient-evidence conditions.
Mechanism work also defines competing explanations and differing predictions.
The schema checks declarations and references; Judge and Review assess whether
the observations actually discriminate the explanations. Theory may specify a
claim, assumptions and derivation instead of competing mechanisms.

The frozen `research_contract.execution` selects:

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

Supported inputs are `hidden_cache`, `targets`, `teacher` and
`checkpoint:<published iteration>:<0|1000|2000>`. Data roles are `development` or
`exploratory`; model inputs may also use `model` or `reference`. The current cache
exposure is the existing development train/val cache. Final/holdout roles and
additional hidden cache splits are rejected. No new external data is fetched.

The initial compute profile is the existing H100 debug allocation, 8 CPU / 96 GiB,
in whole minutes up to 1800 seconds. CPU calculations also work in that profile
and consume single-GPU allocation time. A dedicated CPU-only scheduler profile is
not enabled. All research GPU allocation seconds share the existing formal budget,
deadline and reserve; independent failure-debug grants are not used. Missing
accounting blocks subsequent work.

The result distinguishes execution status from scientific support. A successful
script returns its exact evidence artifact with `measurements=[]`; a settled
failed script may return that artifact with `outcome=incomplete`. Review can retain
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
