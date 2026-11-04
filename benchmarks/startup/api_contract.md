# Fixed startup experiment protocol

The candidate may change `SKILL.md`, `references/operations.md`,
`benchmarks/startup/client.py`, and `src/research_loop/submission.py`. The two
Python files run only in actor sandboxes: `client.py` in the outer workspace and
`submission.py` in the inner Study workspace. They must be standalone. Their CLI
and Python interfaces may expose discovery, preflight, preparation and launch,
waiting, and result inspection, including composed operations. Discover their
actual interface through `--help`, describe/help commands, and candidate docs.
The evaluator imports the fixed host source; candidate Python is copied as bytes.

The host protocol, scientific contract, scorer, and backend remain fixed. Each
case must perform actual remote preparation, a real Terra Plan/Execute/full
Review, one seed-43 three-update CUDA smoke with synthetic B=2, L=16, K=4 tensors,
and verified final dashboard scalars. Preserve the committed source model,
coordinate FM with frozen hidden conditioning, H readout, the equal sum of the
five original ESM3 losses, and the absence of FM loss or added hidden noise.
The diagnostic cannot establish research improvement in bbRMSD. No extra GPU
jobs, installations, SSH, arbitrary host commands, or model/source changes are
authorized through this protocol.

## Outer workspace

Publish `setup-request.json` atomically with exactly `{}` (at most 1,024 bytes).
The host performs actual remote setup and publishes `setup-result.json` with
`status: "prepared"`, `run_id`, and the bound `source_identity` after success;
an error has `status: "error"`. Wait for prepared status before publishing the
launch request. The two operations may be composed within a candidate API call.

Publish `launch-request.json` atomically as one regular JSON file of at most
50,000 bytes with exactly these fields:

```json
{"goal_text": "Nonempty user goal", "task": null, "selection_policy": null}
```

`goal_text` must contain 1–20,000 characters and non-whitespace content.
`selection_policy` is `null`, `"direct"`, or `"debate"`. `task` is `null` or an
actual TaskQueue request object, restricted to `version`, `id`, `kind`, `title`,
`instruction`, `route_id`, `base_experiment`, `priority`, `allow_changes`, and
`budget`. Its kind must be `"investigation"` (also the default when absent).
The canonical fixed TaskQueue validates the remaining request semantics.

The host publishes `launch-result.json` only after completion or failure.
Success has `status: "completed"`, `execution_identity`, `display`, and
`study_state`; completion requires full Review and verified final scalar
visibility. Failure has `status: "error"`, an error, retained execution identity
and available Study/backend state. Neither a timeout nor a missing result is
permission to retry an effect. Inspect the original request and retained result.
Host request files cannot be symlinks. Do not alter host result files.

## Inner Study workspace

The candidate submission API communicates with the fixed host submission module
through its canonical coordination files. Publish `evaluation-request.json`
atomically with exactly `{"action": "research"}`. The fixed host claims that
request as `evaluation-submission.json` before starting the single GPU job and
publishes `evaluation-result.json` afterward. The canonical file names also
include `.evaluation-submit.lock`, `evaluation-request.json.tmp`, and
`evaluation-parameter-errors.json`; preserve their locking and state semantics.
Requests and claimed receipts are limited to 1,024 bytes; result and rejection
files are limited to 1 MiB. Coordination files must be regular files.

Submission states are `not_submitted`, `pending`, `accepted`, `completed`, and
`failed`, with `parameter_error` for a pure preclaim rejection. Only a rejected,
unclaimed request may be corrected. A pending or accepted request is
already retained; never publish another request or remove its receipt. A
completed GPU receipt contains the actual fixed-backend result; an error is a
failure. Read retained results to wait or inspect rather than resubmitting.
The trusted backend owns execution identity, source identity, source validation,
GPU allocation, and telemetry publication. Candidate code cannot supply a
replacement GPU result, change the scalar projection, or bypass full Review.
