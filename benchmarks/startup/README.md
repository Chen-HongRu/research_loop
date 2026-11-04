# Fresh-context startup benchmark

This benchmark measures fresh backend setup and startup derived from the fixed-hidden
ProTok coordinate-FM use case. A fresh `gpt-6.1-terra` / medium CLI session reads the
selected runtime's skill, requests a fresh backend environment/configuration, and
prepares a launch. Existing GPU hardware, login and calibrated dependency layers
are prerequisites; every case receives a new directory, uv environment and Study.
The host runs a real Study with
fresh Terra stage calls and one fixed, real CUDA smoke. The GPU job uses synthetic
inputs, unchanged Frontier0032-H model modules and the original five ESM3 losses.
It is not a scientific performance experiment or a repeated download of the GPU
software stack. Model calls remain local; GPU execution and its raw outputs live
on the configured remote host.

The primary metric is elapsed time from outer Terra launch, including backend
preparation, to reviewed completion and verified final dashboard scalars. A successful case additionally requires an independently
reviewed iteration and clean outer completion. Keep failures, outer preparation,
inner call count, GPU duration, full-round time and token usage separate.
The adapter implements `ExecutionBackend.prepare/start/inspect`, consumes the
canonical `submission.py` research request, and imports canonical telemetry into
the real Study. `verify_dashboard.py` reads the existing dashboard projection and
checks final loss, five loss terms, the diagnostic and four performance scalars. Count
model/transport failures as failures rather than dropping them from timing results.

## Prepare once, outside measured time

- Use isolated before/after runtime snapshots. Apply identical model-availability
  compatibility changes to both when an older snapshot does not allow Terra.
- `gpu_smoke.py` lists the exact small source subset in `SOURCE_FILES`; stage it as
  `source/` together with that script. Do not copy real datasets or checkpoints.
  Preserve the selected source revision and any deliberately included edits.
- Provide a separate remote environment with Torch/CUDA, NumPy, SciPy, einops,
  typing_extensions, huggingface_hub, zstd, biotite, brotli, msgpack, msgpack_numpy,
  biopython and cloudpathlib. Keep other environments unchanged.
- The operator-owned `prepare_case.sh RUN_ID` creates the new case source/config
  and isolated uv environment without allocating a GPU, returning a bound manifest.
  Its fixed dependency layers may be reused without changing another environment.
- The operator-owned `run_gpu.sh GPU RUN_ID` must validate its arguments, lock the
  selected GPU, check it is idle, create a unique output directory, and execute
  `gpu_smoke.py --source-root source --output RUN_ID/result.json --seed 43` with
  a 30-second hard timeout plus bounded kill grace. Return its JSON on stdout.
  It must never accept model-provided code, commands, paths or extra jobs.
- Calibrate this entrypoint once and verify actual finite CUDA updates. Retain the
  receipt. Synthetic denoising NCAC bbRMSD is diagnostic only; there is no Euler100
  sampling, real-data comparison or Frontier performance claim in this fixture.
- The Codex wrapper used by inner stages must ignore user configuration/rules,
  disable web search and command network, and exclude general temporary roots
  from writable sandbox paths. Set TMPDIR inside the current workspace. Verify
  network and outside-workspace writes are actually denied. Outer `runner.py`
  applies these options itself. Authentication remains with the local CLI.

## Run one case

From a prepared environment (no model/GPU fallback):

```sh
uv run --locked python benchmarks/startup/evaluate.py \
  --root /path/to/new/private-case \
  --runtime-root /path/to/frozen/runtime \
  --prepared-source /path/to/prepared-bundle \
  --host configured-host --gpu 0 \
  --remote-root /path/to/remote-bundle \
  --remote-python /path/to/remote-bundle/.venv/bin/python \
  --codex-wrapper /path/to/isolated-codex-wrapper \
  --source-revision exact-runtime-revision --timeout-seconds 420
```

`remote-python` records the operator's interpreter; the fixed remote wrapper owns
its actual invocation. The actor must call `client.py prepare` before `client.py launch`. It can use the
actual task queue or autonomous selection. Record which it chose: this can make a CLI default change irrelevant.

The private result directory contains the launch request, committed baseline,
Study and stage receipts, raw model events and trusted GPU output. Do not commit
it or the private source bundle. Compare at least three independent contexts per
variant, keep the scorer and scenario fixed, and disclose fixture corrections and
excluded calibration runs before reporting a comparison. A timeout with unresolved
work requires reconciliation, never an automatic GPU retry.

This tests a new backend environment/Study on already available hardware. First
installation of shared dependencies, shared-cluster
queue delays, large cache loading and the historical deployment's recovery defects
are outside its measured scope. Requested model identity is recorded; the client
response does not independently prove the provider's internal serving identity.

## Bounded instruction research Study

`research.py` runs an actual two-iteration research-loop Study: an unchanged
baseline followed by one instruction-only optimization. Sol/medium owns the outer
Plan, Execute and full Review; the fixed evaluator uses fresh Terra/medium contexts.
Each candidate gets three serial v3 cases, including actual fresh remote case
environment/source preparation, one real synthetic GPU smoke per case, independent
Review and final scalars verified through the existing dashboard telemetry view.

```sh
uv run --locked python benchmarks/startup/research.py \
  --root /path/to/new/private-research \
  --source /path/to/clean/committed/runtime \
  --prepared-source /path/to/prepared-bundle \
  --host configured-host --gpu 0 \
  --remote-root /path/to/remote-bundle \
  --remote-python /path/to/remote-bundle/.venv/bin/python \
  --codex-wrapper /path/to/isolated-codex-wrapper
```

Only `SKILL.md` and `references/operations.md` may change. Controller-owned snapshots
use baseline Git HEAD plus those Markdown overlays; Python, scorer, backend, model,
loss, dashboard and success requirements stay fixed. Operator baseline checks are
not repeated for each text-only candidate. Models use local CLI processes; remote
backend/GPU execution remains behind the fixed existing interfaces.

The objective is mean launch-to-dashboard `display_ready_seconds` across all three
cases, with `success_rate >= 1` and `scalar_visibility_rate >= 1`. Each failed case
costs 420 seconds; failures are retained. Cases have a 420-second evaluator budget,
outer stage calls at most 1500 seconds, and the Study at most two iterations,
10 calls, 3600 seconds and six GPU jobs. Unknown or unresolved effects stop further
cases and require reconciliation rather than retry. The runner writes terminal
`summary.json`, canonical Study publications, protected runtime snapshots and raw
case receipts under the new root. Three development contexts support only a
bounded comparison; synthetic bbRMSD is not a scientific performance result.

## Additional authorized wall-time budget

A completed Study is immutable. `research.py --prior-study /path/to/completed-run`
starts a linked follow-on Study, importing only the best valid, independently
reviewed instruction files. Keep `--source` at the same clean original revision.
Use `--budget-seconds 36000 --max-iterations 60 --baseline-every 5` for an explicitly
authorized ten-hour campaign. The wall budget governs admission; it does not
reserve continuous GPU training. Each admitted round still runs three fresh
Terra cases and full Review. Original-instruction controls check timing drift.

Run the adapter under `benchmarks/startup/supervise.py --root NEW_ROOT
--budget-seconds 36000 --host HOST --remote-archive REMOTE.tar.gz -- COMMAND...`.
The supervisor writes a separate `NEW_ROOT-supervisor.json`, reaps its owned
process on timeout/stop, and records unresolved effects rather than retrying.
Terminal status is saved before archive transfer, so a transfer failure cannot
leave a finished run labelled running. Its final local receipt records transfer
success or failure. GPU outputs already live remotely; the terminal archive adds
Study/model evidence and supervisor logs, excluding environments and temporary
files. Run this supervisor detached from a short-lived terminal and retain its
PID/logs. It has no automatic event-to-chat wakeup mechanism.
