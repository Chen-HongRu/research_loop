"""Default editable specialist orchestration."""

COMMON = (
    "You are one specialist in a bounded, coordinator-free benchmark.\n"
    "A deterministic host transfers artifacts and executes the prepared command.\n"
    "Use only the supplied task, source pack and artifacts. No delegation, web,\n"
    "network, GPU, package installation or access to other runs or hidden checks.\n"
    "Preserve data roles and inputs. Do not change the task or claim unseen results.\n"
    "Return the requested JSON schema. Keep your conclusion within the task scope.\n"
)


async def orchestrate(team):
    await team.call(
        "plan",
        role="plan",
        context="plan",
        instructions="Design this bounded experiment: hypothesis, matched control, permitted change, evaluation rule and outcome interpretations. Do not implement or run the experiment.",
    )
    await team.call(
        "prepare",
        role="experiment",
        context="experiment",
        instructions="Implement the supplied plan in the candidate workspace. You may syntax-check only; do not run the substantive experiment or create its result file. Return the exact supplied command, smoke evidence and artifact paths.",
    )
    await team.execute()
    return await team.call(
        "evaluate",
        role="critic",
        context="critic",
        instructions="Read the supplied plan, execution receipt and actual result. Independently compute the metrics and return the bounded conclusion and limitations. Each evidence entry must be an exact workspace-relative filename only, without explanation or sentences; include the supplied result_file. Remain read-only and do not rerun the experiment.",
    )
