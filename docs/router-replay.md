# Router candidate and prefix replay

The opt-in `research_loop.router` policy emits structured routing decisions.
Specialists retain design, implementation, interpretation and independent review.
The running research coordinator is unchanged. This candidate is not a drop-in
replacement for the live Study result schema or resource executor.

`benchmarks.router_packets` extracts input-anchored opportunities from completed
Lead rounds. It excludes current response commentary, parallel sibling actions,
later results and final status from each decision prefix. Repeated unchanged
pending deliveries are merged. Unknown grouping, opaque evidence and bounded
prefix truncation are retained as coverage limitations.

```sh
uv run python -m benchmarks.router_packets /path/to/private-snapshot \
  --iteration 6 --output /path/to/new-packets.json
```

The replay watcher consumes the private observer's `observer-status.json` and
its immutable snapshots. Each selected completed attempt is admitted once in a
separate ledger. An uncertain admission requires inspection rather than retry.
It does not modify the observer or original Study, run experiments or dispatch
the assignments returned by Router.

The operator-owned observer is an external input, not started by this command.
Its status object supplies `status` (`collecting` while new rounds may arrive)
and `latest_report` (an absolute report path). That report supplies
`rounds: [{"round": 6, "cost_evaluable": true}]`; its sibling parent contains
`snapshot/` with native histories, goal, round status and snapshot cutoff. Publish
the status pointer only after the immutable report and snapshot are ready.

```sh
uv run python -m benchmarks.router_replay /path/to/private-baseline \
  --output /path/to/unused-output \
  --deadline "${AUTHORIZED_DEADLINE:?Set the existing authorized ISO-8601 deadline}" \
  --freeze-bootstrap /path/to/controlled-synthetic-native-smoke \
  --bootstrap /path/to/private-bootstrap.json --attest-controlled-input \
  --forbidden-root /path/to/private-baseline

uv run python -m benchmarks.router_replay /path/to/private-baseline \
  --output /path/to/private-replay \
  --bootstrap /path/to/private-bootstrap.json \
  --deadline "${AUTHORIZED_DEADLINE:?Set the existing authorized ISO-8601 deadline}" \
  --allow-model
```

Set `AUTHORIZED_DEADLINE` to the existing authorization's timezone-aware deadline;
this command does not extend it. This command uses
the pinned official runtime with `gpt-6.1-terra` / medium. It performs a real
Linux filesystem isolation probe before admitting model calls. Each inference
has a fresh context and isolated roots; tools, web and delegation are disabled.
Fixed runtime capability descriptions may remain visible and are matched against
the calibrated bootstrap; no tool invocation is accepted.
Observed tool activity, injected inputs or missing history rejects the sample
while retaining its usage. Run the CPU-only probe with `--probe-only` and a new
output directory, without `--allow-model`. This Linux replay entry is not a
macOS sandbox compatibility layer.

A blind assessor first identifies acceptable actions from the larger prior-input
prefix (up to 100,000 characters). Router receives a compact subset from the same
decision boundary (up to 12,000 characters); a separate adjudication compares both
actions with the frozen assessment. This is an explicit context-reduction
intervention. Truncation and omitted evidence are recorded. All inputs, outputs, source references,
token receipts, coverage and provisional judgments remain in the private output.
Model judgments require human review; they are not ground truth. Missing prefixes
and unknown grouping are not scored as correct. Reviewer token costs are separate
from Router costs and original Lead costs.

One-step replay follows the original trajectory. Its request count is defined by
the replay sampling protocol. It does not measure how many requests a live Router
would make, whether delegated work succeeds, or end-to-end team savings. Those
claims require matched execution from a shared starting state. A separate systemd
service can own the watcher; terminal files do not automatically wake an agent.

Bootstrap calibration reads an operator-attested, controlled synthetic trace from
the same pinned Native runtime/profile, including its `input.json` and `policy.txt`.
It makes no model call. Never calibrate from research inputs or candidate answers.
The ordered standard setup messages are frozen, with only the supplied policy,
own runtime root and generated native argument-directory name normalized. Ordered
tool/configuration metadata is matched too, ignoring only generated top-level IDs. New
input or changed setup context rejects inference. Keep this manifest private;
its fingerprint is part of the immutable candidate definition.

```sh
uv run pytest tests/test_router.py tests/test_router_packets.py tests/test_router_replay.py
```

## Same-budget state trial

`router_state.enrich_packet` projects only the already delivered evidence prefix.
Explicit task/operation observations come from whitelisted JSON return envelopes;
prose and plans remain attributed quotations. Missing tasks, readiness and review
obligations remain unknown. The projection preserves source IDs and supporting
entity/status excerpts, with omissions reported. The entire Router input stays
within 12,000 characters, including up to 4,000 characters of projected state.
It does not read final role maps, later results, actions or assessor judgments.

The bounded development trial fixes one prior regression and one seeded
both-correct control from each of six completed rounds before inference. It uses
cached v1 recommendations and fresh state-enriched v2 recommendations with the
unchanged Router policy/model/schema. Both are anonymously regraded under the
same clarified contract: host coordination is a recommendation, and independent
review is required before consequential claims rather than at every parallel
dispatch. A conflicting frozen blind label makes both recommendations unknown.

```sh
uv run python -m benchmarks.router_state_trial /path/to/private-replay \
  --output /path/to/new-state-trial --bootstrap /path/to/private-bootstrap.json \
  --deadline "${TRIAL_DEADLINE:?Set a bounded authorized ISO-8601 deadline}" \
  --allow-model
```

The trial makes at most 24 fresh inference attempts, with no automatic retry or
experiment execution. Preserve the same verified proxy/TLS/locale environment
when launching under a supervisor. `selected-cases.json`, `trial.json`, calls,
progress and the report remain private. Fresh Router and grading costs are
separate from cached v1 consumption, including failures and missing receipts.
These are stratified development cases; their scores cannot estimate population
accuracy or be compared directly with the previous full-sample percentages.

If the native platform's fixed instructions/catalog change, the old bootstrap
must reject the call. Preserve that attempt and calibrate the new platform only
with controlled synthetic input. Do not relax the audit using research responses.
`--fresh-v1 --limit 6` instead fixes one case per source round, alternating the
original regression/control strata (three each), and generates both versions
fresh before anonymous grading. This mode permits at most 18 fresh calls, splits
fresh v1/v2/grade costs, and keeps cached predictions for provenance only. Both
versions must match the same newly calibrated bootstrap. Count calibration and
failed attempts separately against the enclosing authorization; a new output
directory does not reset that budget or extend its deadline.

```sh
uv run pytest tests/test_router_state.py tests/test_router_state_trial.py
```
