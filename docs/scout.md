# Background research scout

`ScoutManager` prepares external research advice while a submitted GPU experiment
is running. It owns `control/scout/` only. It never modifies study research, routes,
source code, evaluator configuration, job submission, or scientific authority.

```python
from research_loop.scout import ScoutManager

scout = ScoutManager(run_root)
# Call periodically from the server's scheduler, outside the control lock.
scout.launch_if_due()
# At server shutdown; returns without waiting for the search.
scout.close()
```

The root contains `study/` and `control/`. The first search is due immediately
after the active iteration has `phase=execute`, `status=running`, and its job has
`status=submitted`. Subsequent searches need both three more published iterations
and 1,800 seconds since the last attempt. The worker uses its own advisory file
lock across manager instances. Failed searches consume the same cadence so an
outage does not cause rapid retries.

Each attempt has at most 600 seconds, including context preparation, and ends
before the smaller of the continuation deadline and ownership lease expiry.
An optional explicit `deadline` can further shorten that window. A launch needs
at least 30 seconds of usable time. STOP and `close()` asynchronously terminate
only the scout CLI's process group. Results arriving after stop or the attempt
deadline remain in the run directory and are not published. Errors are recorded
without propagating into GPU execution.

`close()` requests cancellation asynchronously. If a standalone host process
exits immediately afterward, its daemon monitor can exit before signalling the
detached CLI; the host must provide process-group cleanup on shutdown.

Inputs are a bounded frozen copy of the published study research and goal,
route notes, the last three published reviews and candidate records,
`study/research_inputs.json`, optional run-root `research_inputs.json`, and the
current best candidate's model, loss, and configuration. The scout does not scan
data or checkpoints. Historical ProTok evidence and operator inspiration can be
added to `study/research_inputs.json`; its first 14,000 characters are included.

Production uses `CodexHarness(config, web_search=True)` in the read-only sandbox
with live search enabled. `config` defaults to the study's harness configuration.
Tests can supply `harness_factory(config)` and a clock; no network is required.

Outputs live in `control/scout/runs/<id>/`: context, prompt, schema, CLI process
receipt, stdout/stderr, response, report, and final status. A successful or
explicitly degraded response is atomically published as `control/scout/latest.json`.
An exception or a degraded search with no candidates preserves the previous
report. `control/scout/status.json` describes
the last attempt, and `launch_error.json` records admission/read errors.

The report carries up to three candidates, each with source URLs, observations
separate from hypotheses, similar prior experiments, the precise difference,
discriminating outcomes, a minimal experiment, scope requirements, and a readiness
label. Candidate IDs are `<run id>:C1`, `:C2`, and `:C3`. A candidate requiring
authority outside the frozen contract cannot claim to be immediately testable.

Search claims are checked against completed web-tool events in the CLI log. An
absence of observable retrieval forces a degraded report; unavailable search is
explicit. Event presence does not prove a paper's claims or implementation
compatibility, so every report has `source_claims_verified=false` and is external
advice requiring Plan/Review. The main loop freezes `latest.json` only at the next
iteration boundary; an arriving report cannot mutate an active iteration.
