"""Death post-mortems for the observer: what happened in the minute before.

Each death records the character's health, spell points and food over the
review window, the combat it took (attackers and damage by type, from the
same classifier the farm uses to learn), the last messages, and what the bot
was doing. The dashboard shows the recent ones so a death can be understood
without trawling the logs.
"""

from __future__ import annotations

import heapq
import itertools
import math
import time
from typing import Any, Iterable

from .self_improvement import REVIEW_WINDOW_SECONDS, review_failure

# Post-mortems kept per character.
KEEP = 20
# Vital samples kept; the client samples on every health/SP/food change.
VITALS_SECONDS = 600.0
# The client keeps every message of this span (bounded in count too) for the
# review, independent of the dashboard's 500-message buffer.
REVIEW_HISTORY_SECONDS = REVIEW_WINDOW_SECONDS + 5.0
REVIEW_HISTORY_LIMIT = 20000
VITAL_KEYS = ("hp", "maxhp", "sp", "maxsp", "food")
# Vital samples stored per post-mortem, spread over its whole window.
MAX_VITALS = 120
# Attackers or damage types kept in a restored report.
MAX_COUNTS = 25
# The largest stored post-mortem list ever decoded: KEEP bounded reports are a
# few megabytes at most, so anything larger is not one of ours.
MAX_STORED_BYTES = 8 * 1024 * 1024
# A count is a whole number of hits; a merged total never exceeds this.
MAX_COUNT_VALUE = 10**9
# Kept messages and action or decision records per report, and how many
# attacker or damage entries a restore looks at.
MAX_MESSAGES, MAX_RECORDS, MAX_COUNT_SCAN = 40, 25, 4096
# Characters kept in any restored text field.
MAX_TEXT = 500


def spread(samples: list, limit: int = MAX_VITALS, keep: Iterable[int] = ()) -> list:
    """At most ``limit`` samples spread evenly over all of them, the first
    (the window's baseline) and the last kept.

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
            # Drop the nearest even point that is neither kept nor an end.
            spare = min((point for point in chosen
                         if point not in keep and point not in (0, len(samples) - 1)),
                        key=lambda point: abs(point - index), default=None)
            if spare is not None:
                chosen.discard(spare)
            chosen.add(index)
    return [samples[index] for index in sorted(chosen)]


def sample(stats: dict) -> list[float]:
    """One vitals sample: [time, hp, maxhp, sp, maxsp, food]."""
    return [time.time(), *(int(stats.get(key, 0) or 0) for key in VITAL_KEYS)]


# Every number must be finite: json.loads accepts NaN, and one bare NaN
# in the state response makes the browser reject the whole poll.
def _finite(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large for a float
        return False


# Strings bounded too: no server line or report field is anywhere near this.
# And encodable: JSON allows a lone surrogate ("\ud800") that UTF-8 cannot
# carry, which would fail every response that includes it.
# Only textual scalars: a stored list or object is never stringified
# whole before truncation.
def _text(value: Any, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    return str(value)[:limit].encode("utf-8", "replace").decode("utf-8")


def _records(items: Iterable[Any]) -> list[dict]:
    """Action or decision records in their reported shape: a finite time and
    bounded, encodable text, whatever a persisted ledger held."""
    return [{"time": item["time"], "action": _text(item.get("action")),
             "detail": _text(item.get("detail")), "map": _text(item.get("map"))}
            for item in items if isinstance(item, dict) and _finite(item.get("time"))]


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

    finite, text = _finite, _text

    # Only a bounded tail of each stored list is ever traversed (a few times
    # what is kept, for invalid entries), so an oversized value costs no more
    # than a well-formed one.
    def tail(key: str, keep: int) -> list:
        value = entry.get(key)
        return value[-4 * keep:] if isinstance(value, list) else []

    def records(key: str) -> list[dict]:
        return _records(tail(key, MAX_RECORDS))

    def vitals_rows() -> list[list]:
        return [list(item) for item in tail("vitals", MAX_VITALS)
                if isinstance(item, (list, tuple)) and len(item) == 1 + len(VITAL_KEYS)
                and all(finite(part) for part in item)]

    def message_rows() -> list[list]:
        return [[item[0], text(item[1])] for item in tail("messages", MAX_MESSAGES)
                if isinstance(item, (list, tuple)) and len(item) == 2 and finite(item[0])]

    # Bounded like the arrays: the largest MAX_COUNTS of a bounded slice of
    # entries, short names; never a sort of an oversized map.
    def counts(key: str) -> dict[str, int]:
        value = entry.get(key)
        if not isinstance(value, dict):
            return {}
        # Names that become equal once shortened or made encodable are one
        # label: their counts add up, never overwrite each other.
        # Whole numbers, capped: a sum of finite floats can overflow to inf.
        totals: dict[str, int] = {}
        for name, count in itertools.islice(value.items(), MAX_COUNT_SCAN):
            if finite(count):
                label = text(name, 80)
                # Never negative: a hit count cannot be, nor subtract.
                totals[label] = min(MAX_COUNT_VALUE,
                                    totals.get(label, 0) + min(MAX_COUNT_VALUE, max(0, int(count))))
        return dict(heapq.nlargest(MAX_COUNTS, totals.items(), key=lambda item: item[1]))

    def number(key: str) -> int:
        value = entry.get(key)
        return min(MAX_COUNT_VALUE, max(0, int(value))) if finite(value) else 0

    return {
        "time": when, "map": text(entry.get("map")), "x": x, "y": y,
        "cause": text(entry.get("cause")), "detail": text(entry.get("detail")),
        "attackers": counts("attackers"), "peak_attackers": number("peak_attackers"),
        "damage_taken": number("damage_taken"), "damage_by_type": counts("damage_by_type"),
        # The builder's own bounds, so a stored report can never grow larger.
        "vitals": spread(vitals_rows()), "messages": message_rows()[-MAX_MESSAGES:],
        "actions": records("actions")[-MAX_RECORDS:], "decisions": records("decisions")[-MAX_RECORDS:],
    }


def _extremes(samples: list, when: float) -> list[int]:
    """Indices of each charted vital's lowest sample (health, spell points,
    food) and of the last one at the death."""
    if not samples:
        return []
    indices = range(len(samples))
    lowest = [min(indices, key=lambda index: samples[index][1] / max(1, samples[index][2])),
              min(indices, key=lambda index: samples[index][3] / max(1, samples[index][4])),
              min(indices, key=lambda index: samples[index][5])]
    at_death = max((index for index, sample in enumerate(samples) if float(sample[0]) <= when),
                   default=lowest[0])
    return [*lowest, at_death]


def _within(stamp: Any, start: float, end: float) -> bool:
    try:
        return start <= float(stamp) <= end
    except (TypeError, ValueError, OverflowError):
        return False


def build(when: float, path: str, x: int, y: int, vitals: Iterable[list],
          messages: Iterable[tuple], actions: Iterable[dict],
          decisions: Iterable[dict], *, since: float = 0.0,
          name: str = "", recorded: float | None = None) -> dict[str, Any]:
    """The post-mortem of ``name``'s death at ``when`` on ``path`` (x, y).

    ``since`` is the previous death: nothing from that earlier life counts.
    ``recorded`` is when the death's own bookkeeping (its death and depletion
    records, written after the notice) ended: records count up to it.
    """
    # A previous death at or after this one (a clock stepped back, a bad
    # record) bounds nothing: the normal window applies.
    since = since if since < when else 0.0
    # Up to the notice itself: after it come the respawn and the next life.
    start, end = max(when - REVIEW_WINDOW_SECONDS, since), when
    booked = when if recorded is None else max(when, recorded)
    messages = list(messages)
    decisions = list(decisions)
    # The server tells everyone "<name> starved to death." as the whole
    # message. Chat quoting it, or another player starving, is not hers, so
    # it must not reach the classifier's starvation test.
    starved = f"{name} starved to death.".casefold()

    def hers(entry: tuple) -> bool:
        text = str(entry[-1]).strip().casefold()
        return "starved to death" not in text or bool(name) and text == starved

    # The ledgers are normalized (bounded, finite, encodable) before anything
    # reads them, the death classifier included: a persisted ledger can hold
    # anything.
    actions, decisions = _records(actions), _records(decisions)
    # The farm decides whether a death was "on the way"; a post-mortem only
    # describes the fight, so the death map counts as the spot.
    review = review_failure(
        [entry for entry in messages
         if _within(entry[0], start, end) and hers(entry)],
        [record for record in decisions
         # Decisions stop at the death: work after it never sets the cause.
         if isinstance(record, dict) and _within(record.get("time"), start, booked)],
        {"time": when, "action": "death", "map": path}, [path])
    texts = [(float(entry[0]), str(entry[-1])) for entry in messages
             if _within(entry[0], start, end)]
    cause, detail = review.cause, review.detail

    # What she was doing before she died.
    def recent(records: Iterable[dict]) -> list[dict]:
        return [{key: record.get(key) for key in ("time", "action", "detail", "map")}
                for record in records
                if isinstance(record, dict) and _within(record.get("time"), start, booked)][-25:]

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

    # Through the same normalizer as a stored report: every field it carries,
    # including the separately persisted decision ledger's, is finite,
    # encodable and bounded.
    return restore({
        "time": when, "map": path, "x": x, "y": y,
        "cause": cause, "detail": detail,
        "attackers": dict(review.attackers), "peak_attackers": review.peak_attackers,
        "damage_taken": review.damage_taken,
        "damage_by_type": dict(review.damage_by_type),
        "vitals": spread(inside, keep=_extremes(inside, when)),
        "messages": [[stamp, text] for stamp, text in texts][-40:],
        "actions": recent(actions),
        "decisions": recent(decisions),
    })
