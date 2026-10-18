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
# Announcements of one death ("defeated in combat", "YOU HAVE DIED.") arrive
# this close together; they describe a single death.
SAME_DEATH_SECONDS = 10.0
# Vital samples kept; the client samples on every health/SP/food change.
VITALS_SECONDS = 600.0
VITAL_KEYS = ("hp", "maxhp", "sp", "maxsp", "food")


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
          decisions: Iterable[dict], *, since: float = 0.0) -> dict[str, Any]:
    """The post-mortem of a death at ``when`` on ``path`` (x, y).

    ``since`` is the previous death: nothing from that earlier life counts.
    """
    start, end = max(when - REVIEW_WINDOW_SECONDS, since), when + 2.0
    messages = list(messages)
    decisions = list(decisions)
    # The farm decides whether a death was "on the way"; a post-mortem only
    # describes the fight, so the death map counts as the spot.
    review = review_failure(
        [entry for entry in messages if _within(entry[0], start, end)],
        [record for record in decisions
         if isinstance(record, dict) and _within(record.get("time"), start, end)],
        {"time": when, "action": "death", "map": path}, [path])
    texts = [(float(entry[0]), str(entry[-1])) for entry in messages
             if _within(entry[0], start, end)]
    if any("starved to death" in text.casefold() for _, text in texts):
        cause, detail = "starvation", "ran out of food"
    else:
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
        "vitals": [list(value) for value in vitals if _within(value[0], start, end)][-120:],
        "messages": [[stamp, text] for stamp, text in texts][-40:],
        "actions": recent(actions),
        "decisions": recent(decisions),
    }
