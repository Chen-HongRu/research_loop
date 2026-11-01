# Background research Scout

`ScoutManager` produces bounded, read-only external advice for a run root
containing `study/` and `control/`. It owns `control/scout/` only. The host supplies
a finite deadline, explicitly allowed relative source files, and any task-specific
admission condition:

```python
scout = ScoutManager(run_root, deadline=deadline, source_files=("model.py",))
try:
    scout.launch_if_due()
finally:
    scout.shutdown()
```

`launch_if_due()` returns immediately. It admits an executing, running iteration,
uses an independent lock, and caps each attempt at 600 seconds including context
preparation. Subsequent attempts need three more published iterations and 1,800
seconds since the last attempt. Failed searches consume the same cadence.
The host applies stricter job, lease or budget conditions before calling it.

Inputs are bounded excerpts of the goal, published research, routes, recent
reviews and candidates, operator inputs, and explicitly named source files in
the best candidate workspace. Scout does not scan datasets or checkpoints.
Production uses `CodexHarness(config, web_search=True)` in the read-only sandbox.

Each attempt retains context, prompt, schema, streaming CLI output, response,
process receipts, report and status under `control/scout/runs/<id>/`. Reports
contain at most three candidate ideas with observations, hypotheses, prior
attempts, contrasting predictions, minimal experiments and scope requirements.
Ideas requiring more authority cannot claim to be ready to test.

External sources require public HTTP(S) URLs without credentials. A local source
must be a `prior_record`, marked `context_only`, and name an exact path already
present in the frozen input. The report records that provenance explicitly;
local citations do not count as web retrieval. Completed web-tool events support
retrieval status, while source claims remain unverified advice for Plan/Review.

A useful completed or degraded report atomically replaces `latest.json`.
Exceptions and degraded reports without candidates preserve the previous report.
The main loop consumes reports only at the next iteration boundary.

`close()` requests cancellation without blocking. Hosts call `shutdown()` before
exiting; it settles the worker and cancellation watcher and cleans the owned CLI
process group, including descendants. STOP or deadline cancellation prevents late
publication. Search failures never propagate into the main experiment.
