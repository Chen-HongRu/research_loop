"""Iteration commit and recovery under the controller's Study writer lock."""

import asyncio

from . import human_input
from .state import Knowledge, now, record_status, write_json


async def commit(root, state, baseline):
    """Publish settled work and acknowledge its request; update the live state.

    The caller settles native work/resources and checks admission first. Knowledge
    still checks STOP immediately before activation. The controller's finalizer
    records the final live status, including failures after pointer activation.
    """
    continuing = state["result"]["study_action"] == "continue"
    publication = {
        **state,
        "status": "iteration-completed" if continuing else "completed",
        "ended_at": now(),
    }
    if continuing:
        publication["next_iteration"] = state["iteration"] + 1
    version = await asyncio.to_thread(
        Knowledge(root).publish, baseline, publication=publication
    )
    state.update(status=publication["status"], knowledge_version=version)
    await asyncio.to_thread(human_input.consume, root, publication)
    if continuing:
        state["next_iteration"] = publication["next_iteration"]
    return state


async def recover(root, state):
    """Replay acknowledgement and repair stale records from the active pointer.

    A newer input revision is never cleared. An older publication may
    acknowledge its own request but must not replace a newer unfinished iteration.
    """
    published = Knowledge(root).published_state()
    if published:
        await asyncio.to_thread(human_input.consume, root, published)
    if (
        published
        and published["iteration"] >= state.get("iteration", 0)
        and state.get("knowledge_version") != published["knowledge_version"]
    ):
        # The pointer commits the iteration even if its live status was not saved.
        state = published
        write_json(root / "result.json", state["result"])
        record_status(root, state)
    return state
