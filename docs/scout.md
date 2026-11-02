# External research Scout

`ScoutManager` supplies independent, read-only external research to a run root
containing `study/` and `control/`. It owns `control/scout/` only. The host supplies
a finite deadline and explicitly allowed relative source files:

```python
scout = ScoutManager(run_root, deadline=deadline, source_files=("model.py",))
try:
    scout.launch_if_due()
finally:
    scout.shutdown()
```

## Responsibilities

Reviewer checks local evidence and synthesizes conditional explanations across
experiments. It preserves observations, competing explanations, contradictions,
conditions and questions that external research could help assess. Scout examines
those questions through papers and implementations, and independently originates
questions about neglected mechanisms, alternative formulations and gaps between
the development proxy and the Study goal. Its inquiry is not limited to the
current winning architecture or Reviewer's interpretation.

Scout reads primary papers, authors' code and relevant community implementations,
follows references, compares counterevidence and implementation details, and
explains what changes the research picture. It distinguishes external findings,
local observations and its own interpretation. External empirical results do not
establish the cause of a local result; formal implications require matching
assumptions. Previous Scout conclusions remain revisable advice.

Advocates turn relevant findings into research opportunities. Judge selects an
action and addresses relevant Scout challenges, including reasons to defer or
reject them. Plan freezes the executable contract. Scout cannot change an active
Plan, evaluation, budget or permission. It may identify valuable directions that
require a future scope decision. No Scout performance score, adoption target or
numerical exploration penalty is defined.

## Context and continuation

The bounded snapshot contains the goal, contract, global research, research and
operator inputs, the **complete route index**, and up to eight route excerpts.
Recent published routes receive detail; remaining slots rotate with the published
iteration count. The last three published Reviews, candidate records and
structured `insight.json` files are included. Missing or truncated excerpts are
explicit. Source files are explicitly named files in a previous best candidate's
workspace; Scout never scans datasets or checkpoints.

When available, the current iteration's immutable `context/` goal, contract,
global research and route index take precedence over mutable Study documents.
Route excerpts follow published `research_ref` paths. An already published
current-iteration winner is excluded from the older snapshot.

`control/scout/continuation.json` points to the latest meaningful, valid,
uncancelled investigation's immutable `runs/<id>/report.json`, even when it contains only
unfinished questions. The next Scout receives it as `previous_scout`; this
supports continued reading without fabricating a new insight. Otherwise the last
useful report supplies context. Legacy seeded advice without a retained report
is provided as text without a mutable citation path.

## Output and evidence

Version 2 reports contain `summary`, `search_status`, `search_notes`, up to twenty
`sources`, up to three `insights`, and `open_questions`. An insight can be an
explanation, challenge, new direction or synthesis. Its fields separate:

- `local_observations`, `external_findings` and `interpretation`;
- `applicability`, `relation_to_goal` and decision `implications`;
- supporting `source_ids` and optional `follow_up` (empty is allowed);
- ordinary `execution_conditions` from actual additional `scope_requirements`.

There is no `ready_to_test` field. An insight need not propose an executable
experiment. The controller assigns `<run-id>:I<number>` identifiers; proposals
cite them in the existing `research_design.scout_idea_ids`. Historical candidate
reports remain readable and their existing identifiers remain valid references.

Strings have a 2,000-character limit and string lists at most eight entries. The
entire UTF-8 report, including controller metadata, must fit 128 KiB, matching the
main-loop input limit. Oversized reports produce a retained failure status and do
not replace useful advice. A legacy oversized input is explicitly unavailable to
the next iteration rather than silently becoming null.

External references require public HTTP(S) URLs without credentials. Sources
label papers, official code, community code or prior records, and what was read.
A local citation requires `prior_record`, `context_only` and an exact path in
`local_evidence`. Previous Scout citations use immutable retained reports, never
`latest.json`. Context-only citations do not count as web retrieval. Completed
web-tool events establish retrieval activity, not source-claim verification;
reports retain `source_claims_verified=false` for main-loop assessment.

## Scheduling and publication

`launch_if_due()` returns immediately. It admits a `running` Study in selection,
Plan, Execute or Review. Defaults require **one additional published iteration and
300 seconds since the previous attempt**, including failed attempts. There is at
most one executing Scout across managers, enforced by its own lock. Each attempt
is capped at 600 seconds including preparation and clipped by the host deadline.
The ProTok host polls during normal supervisor operation, including independent
research scripts and work without a GPU job; blocked/failed supervisors cannot
admit a search. STOP, HANDOFF, closed manager and exhausted deadline prevent new
launches. HANDOFF drains an existing search through normal host shutdown.

Production uses `CodexHarness(config, web_search=True)` in the read-only sandbox.
The prompt forbids shell commands, local file access beyond the supplied snapshot,
installation, training and other external actions. Each attempt retains context,
prompt, schema, streaming output, response, process receipts, report and status.

A valid report with insights atomically replaces `latest.json`. Empty reports,
exceptions and cancellation preserve the previous useful advice. Valid empty
reports with open questions can still advance the reading continuation; a search
without insights or questions preserves it. The main loop freezes useful
advice **only at the next iteration boundary**; asynchronous arrival cannot change
the active investigation.

`close()` requests cancellation without blocking. Hosts call `shutdown()` before
exiting to settle the worker and cancellation watcher and clean the owned CLI
process group, including descendants. STOP/deadline cancellation prevents late
publication. Search failures never propagate into the main experiment.
