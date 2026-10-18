"""Learn from deaths and emergency recalls instead of abandoning a farm spot.

The farming analyzer chooses where to farm. When something it could not model
goes wrong there, the bot reviews the combat leading up to the failure,
classifies why it happened, and adapts how it farms that spot rather than
excluding it:

* ``pack``: two or more creatures were attacking at once;
* ``resources``: healing ran out (not enough spell points for the next heal);
* ``damage-type``: one non-physical damage type did most of the damage;
* ``starvation``: she starved to death (no food);
* ``route``: the failure happened on the way to the spot, not on it;
* ``out-damaged``: a single creature out-damaged the character.

Each failure raises that spot's adaptation level, which tightens the recovery
margins used there. It also records longer-term goals that equipment and spell
choices can act on (protection against a damage type, more offence, area
spells for packs). Only when every tactic is already at its maximum and the
character still fails repeatedly does the spot get a short pause.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

# Physical attacks are covered by armour; others need specific protection.
PHYSICAL = frozenset({"impact", "slash", "cleave", "pierce"})
REVIEW_WINDOW_SECONDS = 60.0
PACK_WINDOW_SECONDS = 8.0
# Adaptation level at which recovery margins stop tightening.
MAX_LEVEL = 4
FLEE_STEP, FLEE_CAP = 0.03, 0.90
HEAL_STEP, HEAL_CAP = 0.02, 0.98
# The last resort: this many failures within the window at the maximum level.
PAUSE_FAILURES, PAUSE_WINDOW_SECONDS = 3, 3600.0
PAUSE_SECONDS = 3600.0
KEEP_REVIEWS = 10

_HIT = re.compile(
    r"^(?P<who>.+?) hit you for (?P<amount>\d+)(?: \(-?\d+\))?"
    r"(?: (?:(?P<type>[a-z][a-z ]*?) )?damage)?[.!]?$", re.I)
_DISENGAGE = re.compile(r"sp=(?P<sp>\d+) next-heal=(?P<heal>\d+)")


@dataclass
class FailureReview:
    """What happened in the minute before a death or emergency recall."""

    time: float
    kind: str
    map: str
    cause: str
    detail: str
    attackers: dict[str, int] = field(default_factory=dict)
    peak_attackers: int = 0
    damage_by_type: dict[str, int] = field(default_factory=dict)
    damage_taken: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "time": self.time, "kind": self.kind, "map": self.map,
            "cause": self.cause, "detail": self.detail,
            "attackers": dict(self.attackers), "peak_attackers": self.peak_attackers,
            "damage_by_type": dict(self.damage_by_type),
            "damage_taken": self.damage_taken,
        }


def review_failure(messages: Iterable[tuple], decisions: Iterable[dict],
                   failure: dict, spot_paths: Iterable[str]) -> FailureReview:
    """Classify a failure from the combat messages and decisions before it."""
    when = float(failure.get("time", 0.0) or 0.0)
    start = when - REVIEW_WINDOW_SECONDS
    hits: list[tuple[float, str, int, str]] = []
    for entry in messages:
        stamp, text = float(entry[0]), str(entry[-1])
        if not start <= stamp <= when + 2.0:
            continue
        match = _HIT.match(text.strip())
        if match:
            hits.append((stamp, match.group("who").strip(), int(match.group("amount")),
                         (match.group("type") or "physical").strip().casefold()))
    attackers: dict[str, int] = {}
    damage_by_type: dict[str, int] = {}
    for _, who, amount, kind in hits:
        attackers[who] = attackers.get(who, 0) + 1
        damage_by_type[kind] = damage_by_type.get(kind, 0) + amount
    peak = 0
    for index, (stamp, _, _, _) in enumerate(hits):
        window = {who for when_hit, who, _, _ in hits[index:]
                  if when_hit - stamp <= PACK_WINDOW_SECONDS}
        peak = max(peak, len(window))
    damage = sum(damage_by_type.values())
    starved = any(
        record.get("action") == "emergency-disengage" and
        start <= float(record.get("time", 0.0) or 0.0) <= when + 2.0 and
        (match := _DISENGAGE.search(str(record.get("detail", "")))) is not None and
        int(match.group("heal")) > 0 and int(match.group("sp")) < int(match.group("heal"))
        for record in decisions)
    elemental = {kind: amount for kind, amount in damage_by_type.items()
                 if kind not in PHYSICAL and kind != "physical"}
    top_elemental = max(elemental.items(), key=lambda item: item[1], default=None)
    failure_map = str(failure.get("map", ""))
    starved_to_death = any(
        start <= float(entry[0]) <= when + 2.0 and
        "starved to death" in str(entry[-1]).casefold()
        for entry in messages)
    if starved_to_death:
        cause, detail = "starvation", "ran out of food"
    elif failure_map and failure_map not in set(spot_paths):
        cause, detail = "route", f"failed on {failure_map}, on the way to the spot"
    elif peak >= 2:
        cause, detail = "pack", f"{peak} attackers at once: " + ", ".join(sorted(attackers))
    elif starved:
        cause, detail = "resources", "not enough spell points left for the next heal"
    elif top_elemental and damage and top_elemental[1] >= 0.4 * damage:
        cause = f"damage-type:{top_elemental[0]}"
        detail = f"{top_elemental[0]} did {top_elemental[1]} of {damage} damage"
    else:
        who = max(attackers.items(), key=lambda item: item[1])[0] if attackers else "unknown"
        cause, detail = "out-damaged", f"{who} out-damaged the character"
    return FailureReview(
        time=when, kind=str(failure.get("action", "")), map=failure_map,
        cause=cause, detail=detail, attackers=attackers, peak_attackers=peak,
        damage_by_type=damage_by_type, damage_taken=damage)


def spot_key(spot: dict | None, legs: Iterable[tuple[str, str]]) -> str:
    """A stable identity for a farm spot across rankings and restarts."""
    if spot and spot.get("path"):
        return str(spot["path"])
    return "|".join(path for path, _ in legs)


def margins(level: int, flee: float, heal: float) -> tuple[float, float]:
    """Recovery margins after ``level`` adaptations of a spot."""
    level = max(0, min(MAX_LEVEL, int(level)))
    return (min(FLEE_CAP, flee + FLEE_STEP * level),
            min(HEAL_CAP, heal + HEAL_STEP * level))


def remedies(review: FailureReview) -> tuple[str, ...]:
    """What the character should improve, beyond tighter margins at the spot."""
    if review.cause == "pack":
        return ("isolated pulls", "area spells for packs")
    if review.cause == "resources":
        return ("heal earlier", "more spell points and healing")
    if review.cause.startswith("damage-type:"):
        return (f"protection against {review.cause.split(':', 1)[1]}",)
    if review.cause == "out-damaged":
        return ("better weapon and armour",)
    if review.cause == "starvation":
        return ("keep food stocked",)
    return ("cautious route",)


def learn(record: dict, review: FailureReview, *, now: float | None = None) -> tuple[dict, bool]:
    """Fold a review into a spot's adaptation record; return (record, pause)."""
    now = time.time() if now is None else now
    record = dict(record or {})
    level = int(record.get("level", 0) or 0)
    # Only failures that happen with every tactic already at its maximum count
    # toward the last-resort pause; failures on the way up do not.
    failures = [stamp for stamp in record.get("failures", ())
                if now - float(stamp) <= PAUSE_WINDOW_SECONDS]
    if level >= MAX_LEVEL:
        failures.append(review.time)
    causes = dict(record.get("causes", {}))
    causes[review.cause] = int(causes.get(review.cause, 0)) + 1
    reviews = list(record.get("reviews", ()))[-(KEEP_REVIEWS - 1):]
    entry = review.summary()
    entry["remedies"] = list(remedies(review))
    reviews.append(entry)
    pause = level >= MAX_LEVEL and len(failures) >= PAUSE_FAILURES
    record.update(level=min(MAX_LEVEL, level + 1), failures=failures[-PAUSE_FAILURES:],
                  causes=causes, reviews=reviews, updated_at=now)
    return record, pause


def improvement_goals(adaptations: dict) -> dict[str, Any]:
    """Aggregate longer-term goals across spots for gear and spell choices."""
    goals: dict[str, Any] = {"area_spells": False, "protection": {},
                             "offence": 0, "healing": 0}
    for record in (adaptations or {}).values():
        for cause, count in (record.get("causes") or {}).items():
            if cause == "pack":
                goals["area_spells"] = True
            elif cause == "resources":
                goals["healing"] += int(count)
            elif cause == "out-damaged":
                goals["offence"] += int(count)
            elif cause.startswith("damage-type:"):
                kind = cause.split(":", 1)[1]
                goals["protection"][kind] = goals["protection"].get(kind, 0) + int(count)
    return goals
