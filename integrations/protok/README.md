# ProTok adapter

This directory contains the ProTok task boundary: fixed candidate files,
evaluation protocol, backend receipts, GPU accounting, ownership lease,
supervisor and deployment. The reusable `research_loop` package has no ProTok
imports, paths, metrics or scientific prompts.

- `backend.py` validates candidate identity and fixed evaluation receipts.
- `continuation.py` runs the protected evaluator with the existing lease and
  accounting; it also executes directly under Python isolated mode.
- `run.py` bridges one investigation and publishes the reviewed best candidate.
- `server.py` drives the bounded lease and admits Scout only during submitted work.
- `deploy_runtime.py` switches a separate committed runtime at a published HANDOFF.

The active cloud run is `/root/research-loop/runs/protok-resume-20261101`.
Historical deployment evidence is retained in [deployment.md](deployment.md),
`research-inputs/`, and `archive/exploration-v1/verification/`. The archived
manifest describes the original snapshot and is not a current deployment check.

## Recovery

Run status with the correct project path from the existing ownership record:

```sh
uv run python integrations/protok/run.py --root RUN_ROOT --project PROJECT --status
uv run python integrations/protok/run.py --root RUN_ROOT --project PROJECT --run-one
```

A writable Agent delegates into `attempt/agent/`; the outer completion receipt
is published only after fixed-result/source validation. Replaying an older raw
Agent receipt revalidates the same trusted job. Partial Execute remains stopped.

After confirming there is no running local CLI or unknown backend effect,
operators can use retained evidence without launching another evaluation:

```sh
uv run python integrations/protok/run.py --root RUN_ROOT --project PROJECT --reconcile-completed-execute
uv run python integrations/protok/run.py --root RUN_ROOT --project PROJECT --repair-completed-control
```

The first command requires a fully completed, eligible, candidate-bound backend
record and creates a separately labeled operator attempt. It preserves the
interrupted Agent output and proceeds through independent Review. Known failed
wrapper receipts, changed source/configuration, unsettled accounting and unknown
effects block repair. The second finishes retained Review/control publication.
Neither command clears STOP, resets costs or extends the lease.

## Runtime upgrade

Export a tested Git commit into a new runtime directory and save its full revision
in `SOURCE_COMMIT`. Request `control/HANDOFF` with `Upgrade to REVISION` and a
trailing newline. The supervisor finishes its current investigation and exits.
`deploy_runtime.py --root RUN_ROOT --runtime RUNTIME` requires an inactive
service, matching handoff request, fully published Review/control records, no
outstanding effects, and exclusive supervisor/runner locks. It preserves the
ownership and budget files and records the previous service configuration.

## Checks

```sh
PYTHONPATH=src:tests:. uv run python -m unittest discover -s integrations/protok/tests -v
```
