"""Death post-mortems for the observer: what happened in the minute before.

Each death records the character's health, spell points and food over the
review window, the combat it took (attackers and damage by type, from the
same classifier the farm uses to learn), the last messages, and what the bot
was doing. The dashboard shows the recent ones so a death can be understood
without trawling the logs.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

from .self_improvement import REVIEW_WINDOW_SECONDS, review_failure

# Post-mortems kept per character.
KEEP = 20
# Vital samples kept; the client samples on every health/SP/food change.
VITALS_SECONDS = 600.0
VITAL_KEYS = ("hp", "maxhp", "sp", "maxsp", "food")
# Vital samples stored per post-mortem, spread over its whole window.
MAX_VITALS = 120
# The respawn transfer lands this soon before the death notice.
RESPAWN_SECONDS = 5.0


def spread(samples: list, limit: int = MAX_VITALS) -> list:
    """At most ``limit`` samples spread evenly over all of them, the last kept."""
    if len(samples) <= limit:
        return samples
    step = (len(samples) - 1) / (limit - 1)
    return [samples[round(index * step)] for index in range(limit)]


def sample(stats: dict) -> list[float]:
    """One vitals sample: [time, hp, maxhp, sp, maxsp, food]."""
    return [time.time(), *(int(stats.get(key, 0) or 0) for key in VITAL_KEYS)]


def _within(stamp: Any, start: float, end: float) -> bool:
    try:
        return start <= float(stamp) <= end
    except (TypeError, ValueError):
        return False


def build(when: float, path: str, x: int, y: int, vitals: Iterable[list],
          messages: Iterable[tuple], actions: Iterable[dict],
          decisions: Iterable[dict], *, since: float = 0.0,
          name: str = "") -> dict[str, Any]:
    """The post-mortem of ``name``'s death at ``when`` on ``path`` (x, y).

    ``since`` is the previous death: nothing from that earlier life counts.
    """
    start, end = max(when - REVIEW_WINDOW_SECONDS, since), when + 2.0
    messages = list(messages)
    decisions = list(decisions)
    # The server tells everyone "<name> starved to death." as the whole
    # message. Chat quoting it, or another player starving, is not hers, so
    # it must not reach the classifier's starvation test.
    starved = f"{name} starved to death.".casefold()

    def hers(entry: tuple) -> bool:
        text = str(entry[-1]).strip().casefold()
        return "starved to death" not in text or bool(name) and text == starved

    # The farm decides whether a death was "on the way"; a post-mortem only
    # describes the fight, so the death map counts as the spot.
    review = review_failure(
        [entry for entry in messages
         if _within(entry[0], start, end) and hers(entry)],
        [record for record in decisions
         if isinstance(record, dict) and _within(record.get("time"), start, end)],
        {"time": when, "action": "death", "map": path}, [path])
    texts = [(float(entry[0]), str(entry[-1])) for entry in messages
             if _within(entry[0], start, end)]
    cause, detail = review.cause, review.detail

    def recent(records: Iterable[dict]) -> list[dict]:
        return [{key: record.get(key) for key in ("time", "action", "detail", "map")}
                for record in records
                if isinstance(record, dict) and _within(record.get("time"), start, end)][-25:]

    return {
        "time": when, "map": path, "x": x, "y": y,
        "cause": cause, "detail": detail,
        "attackers": dict(review.attackers), "peak_attackers": review.peak_attackers,
        "damage_taken": review.damage_taken,
        "damage_by_type": dict(review.damage_by_type),
        "vitals": spread([list(value) for value in vitals if _within(value[0], start, end)]),
        "messages": [[stamp, text] for stamp, text in texts][-40:],
        "actions": recent(actions),
        "decisions": recent(decisions),
    }
