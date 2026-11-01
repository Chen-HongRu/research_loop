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
cited Scout candidate IDs. The response schema fixes the allocated kind. Judge
assesses the proposed intervention. Review records observations, supported
conclusions, unresolved alternatives, the next discriminating test and allocation
compliance in `insight.json`. Allocation failure does not erase a valid metric.

At the iteration boundary, the controller snapshots `study/research_inputs.json`,
`control/scout/latest.json` and the last three published insights. An arriving
Scout report cannot change an active Plan. Missing, oversized or malformed Scout
input does not block the loop. Only insights with a publication receipt and
reviewed iteration status enter subsequent decisions. The policy does not expand
tool permission, change the evaluation contract or admit extra experiment work.
