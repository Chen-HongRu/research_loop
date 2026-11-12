"""Explicit scientific step and artifact handoff facts for the reference programs."""

from contextlib import contextmanager


def references(assignment):
    fields = (
        "task_ref",
        "input_refs",
        "feedback_refs",
        "scout_report",
        "plan_ref",
        "plan_artifact",
        "prepare_ref",
        "execution_ref",
        "analysis",
        "diagnosis_ref",
        "direction_artifacts",
        "candidate_artifacts",
        "diagnosis_artifacts",
    )
    refs = []
    for key in fields:
        value = assignment.get(key)
        if isinstance(value, str) and value:
            refs.append(value)
        elif isinstance(value, list):
            refs.extend(ref for ref in value if isinstance(ref, str) and ref)
    return list(dict.fromkeys(refs))


@contextmanager
def step(ctx, name, inputs, *, after=(), **facts):
    identity = ctx.trace.begin("step", name, input_refs=inputs, **facts)
    steps = ctx.state.setdefault("research_steps", {})
    for predecessor in after:
        source = steps.get(predecessor)
        if source is not None:
            ctx.trace.handoff(
                source["step_id"],
                identity,
                inputs,
                sender_run_id=source["run_id"],
                receiver_run_id=ctx.run_id,
            )
    steps[name] = {"step_id": identity, "run_id": ctx.run_id}
    ctx.save_state()
    try:
        yield identity
    except BaseException as error:
        ctx.trace.end(
            identity,
            "failed",
            error={"type": type(error).__name__, "message": str(error)},
        )
        raise
    else:
        ctx.trace.end(identity)
