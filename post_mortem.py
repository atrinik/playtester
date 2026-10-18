"""Death post-mortems for the observer: what happened in the minute before.

Each death records the character's health, spell points and food over the
review window, the combat it took (attackers and damage by type, from the
same classifier the farm uses to learn), the last messages, and what the bot
was doing. The dashboard shows the recent ones so a death can be understood
without trawling the logs.
"""

from __future__ import annotations

import math
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
# Messages and samples this long after the notice still belong to the death
# (a PvP defeat's "Local medics" line, late stat packets).
AFTER_SECONDS = 2.0


def spread(samples: list, limit: int = MAX_VITALS) -> list:
    """At most ``limit`` samples spread evenly over all of them, the last kept."""
    if len(samples) <= limit:
        return samples
    step = (len(samples) - 1) / (limit - 1)
    return [samples[round(index * step)] for index in range(limit)]


def sample(stats: dict) -> list[float]:
    """One vitals sample: [time, hp, maxhp, sp, maxsp, food]."""
    return [time.time(), *(int(stats.get(key, 0) or 0) for key in VITAL_KEYS)]


def restore(entry: Any) -> dict[str, Any] | None:
    """A stored post-mortem in the shape the client and dashboard read, or None."""
    if not isinstance(entry, dict):
        return None
    try:
        when = float(entry["time"])
        x, y = int(entry.get("x", 0)), int(entry.get("y", 0))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(when):
        return None

    def records(key: str) -> list[dict]:
        value = entry.get(key)
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    def rows(key: str, size: int) -> list[list]:
        value = entry.get(key)
        return ([list(item) for item in value
                 if isinstance(item, (list, tuple)) and len(item) >= size]
                if isinstance(value, list) else [])

    def counts(key: str) -> dict[str, int]:
        value = entry.get(key)
        return ({str(name): count for name, count in value.items()
                 if isinstance(count, (int, float))} if isinstance(value, dict) else {})

    def number(key: str) -> int:
        value = entry.get(key)
        return int(value) if isinstance(value, (int, float)) and math.isfinite(value) else 0

    return {
        "time": when, "map": str(entry.get("map") or ""), "x": x, "y": y,
        "cause": str(entry.get("cause") or ""), "detail": str(entry.get("detail") or ""),
        "attackers": counts("attackers"), "peak_attackers": number("peak_attackers"),
        "damage_taken": number("damage_taken"), "damage_by_type": counts("damage_by_type"),
        "vitals": rows("vitals", 1 + len(VITAL_KEYS)), "messages": rows("messages", 2),
        "actions": records("actions"), "decisions": records("decisions"),
    }


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
    start, end = max(when - REVIEW_WINDOW_SECONDS, since), when + AFTER_SECONDS
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

    # What she was doing before she died: the post-death grace is for the
    # server's late lines, not for the review and respawn work that follows.
    def recent(records: Iterable[dict]) -> list[dict]:
        return [{key: record.get(key) for key in ("time", "action", "detail", "map")}
                for record in records
                if isinstance(record, dict) and _within(record.get("time"), start, when)][-25:]

    # The last sample before the window is the state at its start, so a
    # sudden death (steady, then one fatal hit) still charts from a baseline.
    # Only from this life: a window cut short by the previous death starts
    # at it, and that death's own fatal sample is no baseline.
    vitals = [list(value) for value in vitals]
    before = [value for value in vitals
              if _within(value[0], since, start) and since < float(value[0]) < start]
    inside = [value for value in vitals if _within(value[0], start, end)]
    if before:
        inside.insert(0, [start, *before[-1][1:]])

    return {
        "time": when, "map": path, "x": x, "y": y,
        "cause": cause, "detail": detail,
        "attackers": dict(review.attackers), "peak_attackers": review.peak_attackers,
        "damage_taken": review.damage_taken,
        "damage_by_type": dict(review.damage_by_type),
        "vitals": spread(inside),
        "messages": [[stamp, text] for stamp, text in texts][-40:],
        "actions": recent(actions),
        "decisions": recent(decisions),
    }
