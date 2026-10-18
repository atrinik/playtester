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
# Attackers or damage types kept in a restored report.
MAX_COUNTS = 25
# Characters kept in any restored text field.
MAX_TEXT = 500


def spread(samples: list, limit: int = MAX_VITALS, keep: Iterable[int] = ()) -> list:
    """At most ``limit`` samples spread evenly over all of them, the last kept.

    The ``keep`` indices (the fatal low, the sample at the death) are always
    in it, in time order, in place of the nearest evenly spread ones.
    """
    if len(samples) <= limit:
        return samples
    keep = sorted({index for index in keep if 0 <= index < len(samples)})
    step = (len(samples) - 1) / (limit - 1)
    chosen = {round(index * step) for index in range(limit)}
    for index in keep:
        if index not in chosen:
            # Drop the nearest even point that is neither kept nor the last.
            spare = min((point for point in chosen
                         if point not in keep and point != len(samples) - 1),
                        key=lambda point: abs(point - index), default=None)
            if spare is not None:
                chosen.discard(spare)
            chosen.add(index)
    return [samples[index] for index in sorted(chosen)]


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

    # Every number must be finite: json.loads accepts NaN, and one bare NaN
    # in the state response makes the browser reject the whole poll.
    def finite(value: Any) -> bool:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        try:
            return math.isfinite(value)
        except OverflowError:  # an int too large for a float
            return False

    def records(key: str) -> list[dict]:
        value = entry.get(key)
        return [{"time": item["time"], "action": text(item.get("action")),
                 "detail": text(item.get("detail")), "map": text(item.get("map"))}
                for item in value if isinstance(item, dict) and finite(item.get("time"))
                ] if isinstance(value, list) else []

    def vitals_rows() -> list[list]:
        value = entry.get("vitals")
        return ([list(item) for item in value
                 if isinstance(item, (list, tuple)) and len(item) == 1 + len(VITAL_KEYS)
                 and all(finite(part) for part in item)]
                if isinstance(value, list) else [])

    def message_rows() -> list[list]:
        value = entry.get("messages")
        return ([[item[0], text(item[1])] for item in value
                 if isinstance(item, (list, tuple)) and len(item) == 2 and finite(item[0])]
                if isinstance(value, list) else [])

    # Strings bounded too: no server line or report field is anywhere near this.
    def text(value: Any) -> str:
        return str(value or "")[:MAX_TEXT]

    # Bounded like the arrays: the largest MAX_COUNTS entries, short names.
    def counts(key: str) -> dict[str, int]:
        value = entry.get(key)
        valid = sorted(((str(name)[:80], count) for name, count in value.items() if finite(count)),
                       key=lambda item: -item[1]) if isinstance(value, dict) else []
        return dict(valid[:MAX_COUNTS])

    def number(key: str) -> int:
        value = entry.get(key)
        return int(value) if finite(value) else 0

    return {
        "time": when, "map": text(entry.get("map")), "x": x, "y": y,
        "cause": text(entry.get("cause")), "detail": text(entry.get("detail")),
        "attackers": counts("attackers"), "peak_attackers": number("peak_attackers"),
        "damage_taken": number("damage_taken"), "damage_by_type": counts("damage_by_type"),
        # The builder's own bounds, so a stored report can never grow larger.
        "vitals": spread(vitals_rows()), "messages": message_rows()[-40:],
        "actions": records("actions")[-25:], "decisions": records("decisions")[-25:],
    }


def _extremes(samples: list, when: float) -> list[int]:
    """Indices of the lowest-health sample and the last one at the death."""
    if not samples:
        return []
    lowest = min(range(len(samples)),
                 key=lambda index: samples[index][1] / max(1, samples[index][2]))
    at_death = max((index for index, sample in enumerate(samples) if float(sample[0]) <= when),
                   default=lowest)
    return [lowest, at_death]


def _within(stamp: Any, start: float, end: float) -> bool:
    try:
        return start <= float(stamp) <= end
    except (TypeError, ValueError, OverflowError):
        return False


def build(when: float, path: str, x: int, y: int, vitals: Iterable[list],
          messages: Iterable[tuple], actions: Iterable[dict],
          decisions: Iterable[dict], *, since: float = 0.0,
          name: str = "") -> dict[str, Any]:
    """The post-mortem of ``name``'s death at ``when`` on ``path`` (x, y).

    ``since`` is the previous death: nothing from that earlier life counts.
    """
    # A previous death at or after this one (a clock stepped back, a bad
    # record) bounds nothing: the normal window applies.
    since = since if since < when else 0.0
    # Up to the notice itself: after it come the respawn and the next life.
    start, end = max(when - REVIEW_WINDOW_SECONDS, since), when
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
         # Decisions stop at the death: work after it never sets the cause.
         if isinstance(record, dict) and _within(record.get("time"), start, when)],
        {"time": when, "action": "death", "map": path}, [path])
    texts = [(float(entry[0]), str(entry[-1])) for entry in messages
             if _within(entry[0], start, end)]
    cause, detail = review.cause, review.detail

    # What she was doing before she died.
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
        "vitals": spread(inside, keep=_extremes(inside, when)),
        "messages": [[stamp, text] for stamp, text in texts][-40:],
        "actions": recent(actions),
        "decisions": recent(decisions),
    }
