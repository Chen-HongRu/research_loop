# Dashboard redesign contract

Version: 1. Status: approved for dashboard-only implementation. Baseline: `8ff7ad3`.
Approval: the user requested implementation of this proposal in the originating
chat, requiring reuse of publication, observations and Agent JSON and no research
core changes. Approval does not include deployment.

## Scope

Provide a concise Study overview, a bounded browser for long iteration histories,
and separate operational views. Use the current controller contracts without
changing research, publication, delegation, token collection or resource state.
Keep deployment-specific records and private research content outside Git.

## Pages

The Study selector, execution state, current iteration and last successful refresh
remain visible across pages. Selection is URL-addressable. Refresh failure keeps
the selected Study's previous data visible with its observation timestamp.

| Page | Default content | Detail interaction |
| --- | --- | --- |
| Overview | Current execution state and error/blocker, most recent reported result with iteration and source, recorded next action, current task counts | Open the corresponding iteration or operational page |
| Iterations | Newest first; ten rows per page; iteration, execution state, result availability, short summary | Select one iteration to show its full result, limitations, evidence references and recorded next action |
| Tasks | Current iteration by default; role and iteration filters; twenty rows per page | Select one task for its available public messages and return value |
| Activity | Coordinator messages and retained tool records, newest first; twenty rows per page | Expand a message or fetch an individual tool record |
| Resources and metrics | Resource budget and receipts; token totals, window, accounting version and coverage | Expand collector diagnostics and available descriptive comparisons |
| Context | Goal, optional historical curves, recorded plan and working documents | Expand individual documents with source and freshness information |

The overview contains no full history, role roster, raw messages, tool list or
collector diagnostics. Long summary and next-action text are visually bounded
with explicit expansion. Historical recommendations are labeled as recorded
recommendations, never presented as active execution commands.

Iteration selection and page/filter state survive refresh. Switching Study clears
selection, detail caches and pagination. A late response cannot populate another
Study or replace a newer iteration selection. An unavailable selection produces
an explicit error rather than substituting another record.

Iteration order is chronological recording order. Do not draw causal edges,
scientific progress percentages or inferred phases from that order. Grouping by
scientific phase requires explicit authoritative phase data and is deferred.

## Read-only integration

The HTTP server owns projections; the frontend formats and navigates them. Reuse
`Studies`, `State`, `research_history`, the existing session/host-role joins and
`metrics_snapshot`. No separate database, framework or background collector.

Proposed narrow routes, all scoped by the existing opaque `study` identifier:

| Route | Contract |
| --- | --- |
| `GET /api/studies` | Existing allowlisted catalog and default selection |
| `GET /api/overview?study=ID` | Observation timestamp, execution status, current iteration, activity, summary/next action with source iteration, current task counts and budget summary; excludes transcripts and full history |
| `GET /api/history?study=ID&page=1` | Ten newest-first rows, counts, page metadata and bounded plain-text summary previews; full text remains in iteration detail |
| `GET /api/iteration?study=ID&iteration=N` | One iteration's full public result, execution status, result availability, source paths and publication evidence |
| `GET /api/agents?study=ID&iteration=N&role=ROLE&page=1` | Twenty task previews and filter/count metadata; unknown iteration associations remain explicitly unknown |
| `GET /api/agent?study=ID&id=ID` | One selected task's public detail; missing native history remains visible as unavailable detail |
| `GET /api/activity?study=ID&page=1` | Twenty coordinator message previews and twenty retained tool previews, with independent counts/retention labels |
| `GET /api/message?study=ID&id=ID` | One selected public coordinator message |
| `GET /api/tool?study=ID&id=ID` | Existing selected-Study tool detail and 404 for unavailable/expired records |
| `GET /api/metrics?study=ID` | Existing metrics plus schema/accounting authority, sampling window, measurability and coverage |
| `GET /api/state?study=ID` | Existing full snapshot; retained for context and existing callers, excluded from overview/history polling |

Page sizes are fixed server-side. Invalid page/filter/iteration inputs return 400;
unknown Study or detail returns 404. Empty valid collections return 200 with empty
rows. API responses retain no-store and existing Host restrictions. The server
must not serialize full transcript bodies merely to produce an overview or list.

Poll overview every five seconds. Fetch other pages on navigation; refresh only
the active page, retaining selection and scroll. Read the context snapshot on
navigation rather than polling it in the background. Poll metrics only while its
page is open. Raw detail is fetched on demand.

## State semantics

- Execution status, scientific `goal`, `study_action` and publication are separate
  fields. `completed` means the Study stopped explicitly, not scientific success.
- A returned result is reported evidence. Successful knowledge publication is
  established from the activated knowledge pointer and matching publication
  metadata, using the controller's existing publication read contract.
- Earlier archived completed status and matching own-version publication metadata
  identify historical publication records; metadata alone does not establish
  activation. Historical records must not imply that each version is currently active. Legacy records without
  publication evidence are labeled unknown, not rejected or silently certified.
- Summary freshness compares source iteration with current iteration. Freshness
  cannot establish publication or independent scientific validation. The current
  `research.summary.provisional` flag is insufficient for either claim.
- A stopped iteration without a result stays visible. The overview may show the
  preceding reported result, with its source iteration explicitly labeled.
- Task role/status follows explicit host records where available. Inferred role
  and time-derived iteration association remain labeled. Missing history does
  not hide the task or establish fresh-context compliance.
- Metrics schema 2 uses unique request receipts as token authority. Legacy schema
  1 and unversioned snapshots retain explicit legacy/unknown labels. Do not merge
  or compare incompatible authorities silently. Unavailable values are not zero.
- Allocation time is not GPU core utilization. Collector coverage describes its
  recorded scope, not completeness of all Study activity. No token value may be
  inferred from task counts. Metric collection remains an external sidecar.
- The dashboard remains read-only; no resume, stop, request-submission or resource
  dispatch control is introduced.

## Evidence

Observed in baseline source:

- `dashboard/index.html` puts research history, coordination, metrics, team and
  next steps on the first page.
- `dashboard/app.js:renderHistory` renders all iteration summaries; `render`
  rebuilds all page sections after any snapshot change, including hidden views.
- `dashboard/server.py:handler_for` attaches complete history to every state
  response. `State.snapshot` includes public coordinator/task histories.
- `publication.commit` and `Knowledge.published_state` define the publication
  boundary. A result is written before cleanup/publication in the loop.
- `metrics.VERSION` is 2; `metrics_snapshot` currently omits schema version and
  accounting authority.

Observed against an authorized existing remote dashboard: a long Study reached
47 iterations with 46 reported results, hundreds of task/message records and a
state response over 4 MB. Its current stopped iteration had no result; its newest
summary came from the preceding iteration. An existing metrics artifact used
schema 1, which the API did not expose. Private records remain outside this file.
Remote deployment revision could not be established from the inspected directory.

Inferred: separating views and bounded projections should reduce navigation and
serialization work. Synthetic smoke measurements do not establish throughput or
latency improvements for a real Study or deployment.

## Acceptance

1. A synthetic 50-iteration Study with hundreds of tasks/messages initially shows
   only overview content. Initial/poll requests do not include full histories or
   task/message bodies. History has at most ten previews; only the selected
   iteration's full result is rendered.
2. A stopped current iteration without a result displays the stopped state and
   preceding result's source iteration separately. Result-written but unpublished
   work is not labeled published. Legacy publication evidence stays unknown.
3. A Study switch during overview/detail fetch cannot display the old Study's
   response. Refresh retains page, filters and selection; removed Studies and
   expired tool IDs produce explicit unavailable states.
4. Host-role tasks with missing session history remain listed; time-based
   associations and unknown roles are not silently promoted to certainty.
5. Schema 1, schema 2, unversioned, partial and missing metrics show distinct
   accounting/coverage states. Existing data is not rewritten to satisfy the UI.
6. Run focused Python dashboard/publication/token tests and
   `node tests/dashboard_frontend.cjs`; run Ruff on changed Python files. Perform
   a local HTTP/browser smoke with synthetic records at desktop and narrow widths,
   checking overview, history pagination/detail, Study switch and refresh failure.
   No model/GPU run is required. Do not put remote transcripts into fixtures.
7. Update README dashboard guidance and affected operations/design documentation
   with the actual routes, page behavior and semantics in the implementation PR.

## Handoff

Implement the approved contract on an isolated branch, preserving unrelated
working-tree files. Remote publication and deployment require separate approval;
the design and local implementation do not imply either action.
