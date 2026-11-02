# Exploration policy

An optional `exploration` object in `study.json` allocates research kinds to
future iterations:

```json
{"version": 1, "start_iteration": 1,
 "cycle": ["architecture", "diagnostic", "refinement"], "priority_routes": []}
```

The cycle allocates iterations. The study goal defines admissible methods,
evaluation, resources and the meaning of each kind. Architecture and diagnostic
slots offer an underexplored active priority route and a new-route opportunity.
Refinement uses ordinary route rotation. Paused routes remain paused.

Proposals carry a `research_design`: question, structural change, prior attempts,
precise difference, contrasting predictions, minimal test, scope requirements and
cited Scout insight IDs (or historical candidate IDs). The response schema fixes
the allocated kind. Judge
assesses the proposed intervention and relevant external challenges; it may defer
or reject a Scout interpretation with reasons. Scout supplies external research,
not an action decision. An insight need not contain an executable experiment.
Review records observations, supported
conclusions, unresolved alternatives, the next discriminating test and allocation
compliance in `insight.json`. It synthesizes conditional explanations across
related experiments and retains important questions for external investigation.
Scout reads the last three published insight records and may independently
challenge their framing. Allocation failure does not erase a valid metric.

At the iteration boundary, the controller snapshots `study/research_inputs.json`,
`control/scout/latest.json` and the last three published insights. An arriving
Scout report cannot change an active Plan. Missing, oversized or malformed Scout
input does not block the loop; oversized input is explicitly marked unavailable.
Only Reviewer insight records with a publication receipt and reviewed iteration
status enter subsequent decisions. The policy does not expand
tool permission, change the evaluation contract or admit extra experiment work.

Scout scheduling, continued reading, source attribution and output bounds are
specified in [External research Scout](scout.md).
