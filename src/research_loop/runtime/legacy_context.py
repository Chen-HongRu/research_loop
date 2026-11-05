"""Capture the existing serial controller inputs without changing its prompts."""

from .context import ContextSnapshot, build_context


def capture_call(*, study_id, iteration_ref, attempt_ref, role, context, state):
    """Describe already-built inputs; never reread source or experiment artifacts."""
    frozen = f"{iteration_ref}/context"
    origins = {
        "goal": f"{frozen}/goal.md",
        "global_research": f"{frozen}/global.before.md",
        "global_evaluation": f"{frozen}/study.before.json",
        "frontiers": f"{frozen}/frontiers.before.json",
        "routes": f"{frozen}/routes.before.json",
        "manual_task": f"{frozen}/task.json",
        "task_results": f"{frozen}/task_results.json",
        "long_training_results": f"{frozen}/long_training_results.json",
        "route_research": f"{frozen}/route.before.md",
        "research_inputs": f"{frozen}/research_inputs.json",
        "scout_inputs": f"{frozen}/scout_inputs.json",
        "recent_insights": f"{frozen}/recent_insights.json",
        "plan": state.get("plan_ref"),
        "execute": state.get("execution_ref"),
    }
    knowledge = f"{frozen}/global.before.md"
    if role == "review":
        knowledge = f"{iteration_ref}/review.before.json"
        origins["global_research"] = f"{knowledge}#/research.md"
        origins["route_research"] = "controller:review-route-research"
    continuation = context.get("execution_continuation")
    if continuation:
        scientific = "/".join(continuation["plan_ref"].split("/")[:2])
        for key in ("research_inputs", "scout_inputs", "recent_insights"):
            origins[key] = f"{scientific}/context/{key}.json"
    sources = [
        {
            "id": origins.get(key) or f"controller:{key}",
            "key": key,
            "ref": f"{attempt_ref}/context.json#/{key.replace('~', '~0').replace('/', '~1')}",
            "revision": attempt_ref,
            "reason": f"Existing {role} input frozen for this invocation",
            # The serial controller has already selected its feedback inputs.
            # Evaluation definitions here are constraints, not final-only scores.
            "allow_feedback": True,
        }
        for key in context
    ]
    snapshot = ContextSnapshot.capture(
        study_id=study_id,
        revisions={
            "study": f"{frozen}/study.before.json",
            "task": f"{frozen}/task.json" if context.get("manual_task") else None,
            "knowledge": knowledge,
            "iteration": iteration_ref,
            "execution": attempt_ref,
        },
        inputs=context,
        sources=sources,
        protected_keys=list(context),
        allowed_evidence=[source["ref"] for source in sources],
    )
    return build_context(
        snapshot, role, context.get("manual_task"), "legacy-context-v1", {}
    )
