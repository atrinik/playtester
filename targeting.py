"""Provider-independent target selection policy."""

from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# A creature the server will not select (one whose removal never reached
# this view, such as a monster killed as a level-up redrew the map, or one
# it refuses to target) would otherwise be requested forever while the fight
# makes no progress. It is set aside once requests for it have gone
# unacknowledged for TARGET_ACK_TIMEOUT.
TARGET_ACK_TIMEOUT = 6.0
UNRESPONSIVE_TARGET_SECONDS = 120.0


async def select_target(client: Any, x: int, y: int, actor_count: int,
                        relative_depth: int = 0) -> bool:
    """Ask the provider to select an actor unless it has proven unselectable.

    Returns False, and sets the actor aside for UNRESPONSIVE_TARGET_SECONDS,
    once the server has not acknowledged it since requests began
    TARGET_ACK_TIMEOUT ago. Any acknowledgement of it, including one that
    arrived between requests, starts a fresh window, so a target lost to a
    heal or another selection gets a full timeout when it is retried.
    """
    state = client.state
    now = time.monotonic()
    # Minimal states (as some providers and tests keep) start with none yet.
    requested, since = getattr(state, "target_request", (0, 0.0))
    acked, acked_at = getattr(state, "target_ack", (0, 0.0))
    if (requested != actor_count or getattr(state, "target_id", 0) == actor_count or
            (acked == actor_count and acked_at >= since)):
        state.target_request = (actor_count, now)
    elif now - since >= TARGET_ACK_TIMEOUT:
        ignored = getattr(state.map, "ignored_targets", None)
        if ignored is not None:
            ignored[actor_count] = now + UNRESPONSIVE_TARGET_SECONDS
        state.target_request = (0, 0.0)
        log.warning(
            "target %s at view %s,%s was never acknowledged in %.0fs; "
            "ignoring it for %.0fs", actor_count, x, y, now - since,
            UNRESPONSIVE_TARGET_SECONDS)
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("target-unresponsive", f"actor_count={actor_count} view={x},{y}")
        return False
    if relative_depth:
        await client.target(x, y, actor_count, relative_depth)
    else:
        await client.target(x, y, actor_count)
    return True
