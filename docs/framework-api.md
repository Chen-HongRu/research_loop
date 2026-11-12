# Workflow and Group author API

A declaration such as [`examples/research.yaml`](../examples/research.yaml) binds a
workflow program, Router profile, Agent instructions/profiles and named Group
programs. Paths are relative to the declaration. Preparation freezes these small
files. Shared Python imports use the fixed controller implementation; changing a
shared import requires stopping the controller and preparing a matching version.
The snapshot copies Python modules and regular package directories; non-code data
and symbolic links stay outside it. Declare task inputs explicitly as references.
External scientific inputs remain references.

A Group program exports `async run(ctx, payload)`. Its `ctx.work` is its output
workspace; `ctx.state` is persisted with `ctx.save_state()`. The payload contains
`task_ref`, `input_refs`, `feedback_refs`, `prompt`, admitted `interventions`,
optional `recovery`, and the opaque workflow context in `study_context`.
`ctx.config_revision` and `ctx.predecessor` identify the execution contract and
any replaced execution. Research Groups interpret that context; the generic runtime
does not interpret scientific result fields.

For example, a Group with the declaration binding `agents: {answer: writer}` can
make one structured call and export a small report:

```python
import json

SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


async def run(ctx, payload):
    with ctx.trace.step("answer", input_refs=[payload["task_ref"]]) as step_id:
        answer = await ctx.services.call(
            "answer", "answer", "Read the supplied task and answer its question.",
            {"task_ref": payload["task_ref"]}, SCHEMA, session="new", cwd=ctx.work, step_id=step_id,
        )
    report = ctx.work / "answer.json"
    report.write_text(json.dumps(answer) + "\n", encoding="utf-8")
    return {
        "summary": answer["summary"],
        "completion": "completed",
        "unfinished": [],
        "help": "",
        "artifacts": [str(report)],
    }
```

`call(binding, context, instructions, payload, schema=None, *, session="new",
cwd=None, allow_tools=True, max_output_chars=65536, step_id=None)` resolves the binding through
that Run's Group definition. Agent instructions and model/profile are frozen
configuration facts. A supplied schema constrains this step; otherwise the Agent's
configured schema applies. Tool access is bounded by the Agent declaration and
runtime authorization.

`session="new"` creates an independent conversation. `session="reuse"` requires
an existing context in the same Group Run with the same Agent definition, profile,
configuration revision and effective permissions, including working directory.
The research example binds Plan and Prepare to the same Agent and context, and
explicitly reuses that session during Prepare. Evaluate and all independent
Critic calls use new sessions. Independent conversations do not enforce host
filesystem isolation; scientific evidence scope remains an explicit task contract.

Programs record control steps with `ctx.trace.begin("step", label, **facts)` and
`ctx.trace.end(step_id, status, **facts)`, or `ctx.trace.step(...)`. Explicit
artifact transfers use `ctx.trace.handoff(sender_step_id, receiver_step_id,
input_refs, **facts)`. Calls and commands have separate execution identities. Pass `step_id` to `call`
to record the enclosing step explicitly.
Store small task/artifact references and actual branch/completion facts, never
hidden reasoning or raw conversation copies. A handoff must describe a transfer
the program actually performs.

Prepared commands use
`await ctx.services.execute_prepared(command, operation_id=..., backend=...)`.
The service owns dispatch admission, settlement and original operation identity.
An unknown result requires reconciliation. Groups must not bypass that service
to repeat uncertain execution. Programs must cooperate with async cancellation
and route model/command effects through managed services.

A workflow program exports `create(settings)` returning an object with these
methods and attributes:

| Member | Contract |
| --- | --- |
| `router_policy` | Router instruction text. |
| `max_active_groups` | Required positive integer admission limit; research sets one. |
| `result_schema(request)` | Optional workflow-owned helper; the research workflow uses it for its result schema. |
| `context(root, config, iteration, previous_result, request)` | JSON context passed unchanged to new Group executions. |
| `route(packet)` | Returns Router packet with workflow guidance and allowed actions; keeps reference IDs. |
| `advance(view)` | Pure/file-read lifecycle directive: hold, close, wait or finish_round; otherwise `None`. |
| `complete(view, result_ref)` | Validates the authored result and proposes publication; never activates runtime state. |
| `validate_input(kind, payload)` | Required when accepting work inputs; validates and normalizes supported kinds. |
| `recover(view, record)` | Optional hook freezing explicit settled evidence for a new execution. |

The loader requires `context`, `route`, `advance`, `complete`, `router_policy`, and
`max_active_groups`. The remaining hooks apply to the supported input/recovery
behavior of that workflow.

The view contains `root`, `config`, `iteration`, actual `runs`, current `admitted`
Run IDs, and `input` holding the workflow context. Completion returns
`{action: "continue" | "stop", result: {...}, artifacts: [...]}`. Each artifact is
`{source: absolute_small_export, destination: relative_publication_path}` or
`{content: text, destination: relative_publication_path}`. An optional
`input_outcome: {id, status}` links a work-input disposition to publication.
The generic publisher stages these mappings and activates the output pointer;
workflow validation itself does not publish. Active outputs are under
`outputs/current`.

The reference research plugin retains scientific task/data/evaluation boundaries,
independent review, authored knowledge manifests and human-request completion
checks. Its recovery hook accepts a settled successful Exp command, freezes only
selected small Plan/Prepare/result files, and returns a descriptor for a new Exp
Run. Recovery preserves the original operation and source revision and evaluates
without redispatch. Agent instruction revisions are allowed while the original
task, Plan, protocol settings and result schema remain fixed. Changed scientific
tasks or protocol settings are rejected. Recovery reads selected files once at
creation, each at most 4 MiB and together at most 16 MiB; it does not scan datasets
or checkpoints. This bounded recovery profile does not cover large result files.

For recovery-only Synthesis, the explicit report packet admits at most 65,536
bytes per report and 65,536 serialized characters in total. This model-input
limit is separate from the 16 MiB evidence bundle limit. Original report content
is retained. The Author returns an unreviewed candidate before the host requests
independent Critic review; only an accepted candidate can be exported for
publication.
