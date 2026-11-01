# Research exploration policy

The opt-in `exploration` field in `study.json` gives each future iteration an
explicit scientific purpose. The initial ProTok policy is:

```json
{
  "version": 1,
  "start_iteration": 7,
  "cycle": ["architecture", "diagnostic", "architecture", "refinement", "architecture"],
  "priority_routes": ["R0002", "R0003"]
}
```

The deployment derives `start_iteration` from the completed iteration; the
example is not a hardcoded rollout assumption. The 60/20/20 split allocates
iterations, not GPU hours. It is an initial research policy, not an empirically
optimal allocation. Architecture and diagnostic slots offer an underexplored
active priority route and a new-route opportunity. Refinement retains the usual
route rotation. Paused routes are not automatically reopened.

Proposal responses include a typed `research_design`: scientific question,
structural change, prior attempts, precise difference, contrasting predictions,
minimal test, scope requirements, and cited scout candidate IDs. The schema
requires the allocated kind. Judge must assess the actual mechanism and reject
scalar-only tuning mislabeled as architecture. This semantic judgment is not a
mechanical novelty guarantee. Review audits the actual diff and publishes an
`insight.json` with observations, supported conclusions, unresolved alternatives,
the next discriminating test and allocation compliance. A compliance failure
does not erase a valid experimental score. The next three-Review context retains
these audits; the initial policy does not automatically repay missed slots.

The controller snapshots `study/research_inputs.json`, `control/scout/latest.json`
and the last three published insights at the iteration boundary. Late scout
output cannot alter an active Plan. Missing, oversized or corrupt scout input
does not block experimentation. Only a fully published Review's insight enters
future decisions. Raw evidence and historical publications remain intact.

The ProTok supervisor runs a separate ScoutScheduler thread, polling every three
seconds while the main runner is busy. ScoutManager starts only after a GPU job
has been submitted, with independent locks, cadence and a 600-second cap. See
[scout.md](scout.md) for search provenance, failure isolation and input paths.
Operators can add inspiration/evidence to `study/research_inputs.json`; the next
iteration freezes it and future scout searches incorporate it. It does not
authorize changes to the fixed evaluation contract.

Selection uses the existing `direct` strategy and concise Review: two proposals,
one Judge, Plan, Execute and independent Review (six ordinary calls instead of
ten). This removes four serial debate calls and makes literature work overlap
GPU execution. It does not guarantee uninterrupted GPU occupancy: planning,
review, queueing and candidate preparation still take time. No second GPU job
or extra evaluation is introduced.

The scientific goal distinguishes the current fixed 449-protein reconstruction
proxy from full-validation parity and conditional generative quality. Historical
ProTok full-set results are attributed evidence, never interchangeable scores.
The initial seeded scout report includes one admissible conditioning experiment
and two directions with explicit missing checkpoint/diagnostic requirements.
No diversity measurement or historical result is fabricated.

`integrations/protok/deploy_exploration.py` validates a hashed runtime, waits on
the existing runner lock, requires fully published Review and completed job
receipts with no outstanding effects, then switches systemd at that boundary.
It archives prior configuration, goal, research and service content. Ownership,
lease, GPU budget, evaluator and candidate receipts remain unchanged. An existing
STOP is not cleared. Future deployments can request `control/HANDOFF`, checked
by the new supervisor only between complete iterations.
