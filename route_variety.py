"""Route variety: she takes a less-used route when one is about as good
(docs/ROUTING.md 4.11).

Every trip used to take the single cheapest route, so she walked the same
roads for ever; the owner saw her find secret ways out of Greyton they had
forgotten and wants her to see more of the world. This module keeps what
the choice needs and decides it; ``NavigateTask._vary_route`` finds the
candidates on the planner worker.

- **Counts.** Per character, how often she entered each map and crossed
  each edge (``route_blocks.edge_subject``), in ``GameState`` and in her
  character memory next to her route learning (``route_visits``). A count
  halves every ``HALF_LIFE_SECONDS`` and is forgotten after
  ``FORGET_SECONDS`` (a week) without use; each is capped at ``COUNT_CAP``
  and the tables at ``MAX_MAPS`` and ``MAX_EDGES`` (least recently used out).
- **Choice** (``choose``). Among routes no more than the tolerance (15 %,
  ``ClientConfig.route_variety_tolerance``) above the best route's walking
  cost, entering no hazard, route-death or quarantined map the best route
  does not enter, walking no longer than it on any hazard map they share
  (the start and destination included) and crossing no route-death,
  quarantined or deadly map, she takes the one with the largest share of
  unvisited ways, then the least used, a small random draw breaking ties.
- **Discoveries.** A map she enters, or an exit she takes, for the first
  time in her memory is recorded for the dashboard (``route-new-map``,
  ``route-new-exit``; ``route-secret-exit`` for an exit authored invisible).

Nothing here plans or reads the world graph.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .route_blocks import edge_subject

HALF_LIFE_SECONDS = 24 * 3600.0
FORGET_SECONDS = 7 * 24 * 3600.0
COUNT_CAP = 20.0
MAX_MAPS = 500
MAX_EDGES = 1000
SUBJECT_LIMIT = 300
# Counts are written to her memory at most this often (any other save of
# her character memory writes them too).
PERSIST_SECONDS = 300.0
# An edge whose count has decayed below this is unvisited.
UNVISITED_BELOW = 0.5
DEFAULT_TOLERANCE = 0.15
# A tolerance above this is a configuration mistake: capped.
MAX_TOLERANCE = 0.5
# An exit or voyage costs this many steps of walking (route_costs'
# TRANSITION_SECONDS at its DEFAULT_SECONDS_PER_TILE); a tile seam is one
# step, with no map load.
TRANSITION_STEPS = 4
# Alternatives searched per trip: one route avoiding each of this many
# edges of the best route, and one priced by her counts.
MAX_CANDIDATES = 8
# The whole search, on the planner worker, gives up after this long and
# keeps the best route.
SEARCH_SECONDS = 2.0
# Below this share of her maximum HP a trip never varies.
LOW_HP_SHARE = 0.7

ROUTE_VARIETY = "route-variety"
NEW_MAP = "route-new-map"
NEW_EXIT = "route-new-exit"
SECRET_EXIT = "route-secret-exit"


def decayed(entry: Sequence[float] | None, now: float) -> float:
    """A stored (count, last use) as it stands at ``now``."""
    if not entry:
        return 0.0
    count, at = entry
    age = max(0.0, now - at)
    if age > FORGET_SECONDS:
        return 0.0
    return count * 0.5 ** (age / HALF_LIFE_SECONDS)


def bump(table: dict[str, tuple[float, float]], key: str, now: float,
         limit: int) -> bool:
    """Count one use of ``key``; whether it was unseen (or forgotten)."""
    fresh = decayed(table.get(key), now) <= 0.0
    table[key] = (min(COUNT_CAP, decayed(table.get(key), now) + 1.0), now)
    if len(table) > limit:
        for old in sorted(table, key=lambda name: table[name][1])[:len(table) - limit]:
            del table[old]
    return fresh


def _valid(raw: Any, now: float, limit: int) -> dict[str, tuple[float, float]]:
    table: dict[str, tuple[float, float]] = {}
    if not isinstance(raw, dict):
        return table
    for key, value in raw.items():
        if (not isinstance(key, str) or not key or len(key) > SUBJECT_LIMIT or
                not isinstance(value, (list, tuple)) or len(value) != 2):
            continue
        count, at = value
        if (isinstance(count, bool) or isinstance(at, bool) or
                not isinstance(count, (int, float)) or
                not isinstance(at, (int, float))):
            continue
        try:
            count, at = float(count), float(at)
        except (OverflowError, ValueError, TypeError):
            continue
        if (not math.isfinite(count) or not math.isfinite(at) or count <= 0 or
                at > now + 60.0 or now - at > FORGET_SECONDS):
            continue
        table[key] = (min(COUNT_CAP, count), min(at, now))
    if len(table) > limit:
        kept = sorted(table, key=lambda name: table[name][1])[len(table) - limit:]
        table = {key: table[key] for key in kept}
    return table


def load(raw: Any, now: float | None = None
         ) -> tuple[dict[str, tuple[float, float]], dict[str, tuple[float, float]]]:
    """Her stored ``route_visits`` (maps, edges), validated: a malformed,
    non-finite, future or forgotten entry is dropped, counts are capped and
    each table keeps its most recent entries."""
    now = time.time() if now is None else now
    raw = raw if isinstance(raw, dict) else {}
    return (_valid(raw.get("maps"), now, MAX_MAPS),
            _valid(raw.get("edges"), now, MAX_EDGES))


def dump(maps: dict[str, tuple[float, float]],
         edges: dict[str, tuple[float, float]]) -> dict[str, Any]:
    return {"maps": {key: [round(count, 3), at] for key, (count, at) in maps.items()},
            "edges": {key: [round(count, 3), at] for key, (count, at) in edges.items()}}


def pair(subject: str) -> str:
    """The (source, destination) maps of an edge subject, as
    "pair:SOURCE>DESTINATION": the twin doors of one entrance, or an exit
    and a seam between the same two maps, are one way."""
    kind, _, rest = subject.partition(":")
    if kind == PAIR:
        return subject
    head, _, destination = rest.rpartition(">")
    return f"{PAIR}:{head.split(':', 1)[0]}>{destination}"


PAIR = "pair"


def pair_uses(edge_uses: dict[str, tuple[float, float]], now: float) -> dict[str, float]:
    """Her decayed use of each (source, destination) pair: the sum over
    its subjects (route_variety's counts)."""
    uses: dict[str, float] = {}
    for subject, entry in edge_uses.items():
        value = decayed(entry, now)
        if value > 0:
            key = pair(subject)
            uses[key] = uses.get(key, 0.0) + value
    return uses


def note_crossing(client: Any, edge: Any, *, exact: bool = True,
                  now: float | None = None,
                  save_timeout: float | None = None) -> None:
    """She crossed ``edge`` on a trip (on the loop): count the edge and the
    map it entered, record a first visit for the dashboard, and keep the
    counts in her memory. ``exact`` is False when where she arrived does
    not show she took that very exit (another of its maps' exits, a
    displaced arrival): only the pair of maps is counted, and no exit is
    recorded. ``save_timeout`` bounds the decision ledger's save (a tick
    never waits SQLite's usual 10 s for it).

    An exit is new only when its pair of maps is (the second square of a
    double door is no new way). A new exit is a secret one (SECRET_EXIT)
    when it is authored invisible and leaves a map she already knew: a new
    way out of a familiar place. ``invis_exit`` also authors ordinary cave
    mouths and house doors, so a hidden exit from a map new to her is only
    a new exit."""
    state = getattr(client, "state", None)
    edges = getattr(state, "route_edge_uses", None)
    maps = getattr(state, "route_map_visits", None)
    if not isinstance(edges, dict) or not isinstance(maps, dict):
        return
    now = time.time() if now is None else now
    subject = edge_subject(edge)
    way = pair(subject)
    known_source = decayed(maps.get(edge.source), now) > 0.0
    new_pair = way not in pair_uses(edges, now)
    bump(edges, subject if exact else way, now, MAX_EDGES)
    new_map = (edge.destination != edge.source and
               bump(maps, edge.destination, now, MAX_MAPS))
    recorder = getattr(client, "record_action", None)
    if recorder is not None:
        timeout = {} if save_timeout is None else {"save_timeout": save_timeout}
        if new_map:
            recorder(NEW_MAP, f"{edge.destination} (from {edge.source})", **timeout)
        if exact and new_pair and getattr(edge, "kind", "exit") == "exit":
            secret = bool(getattr(edge, "hidden", False)) and known_source
            recorder(SECRET_EXIT if secret else NEW_EXIT,
                     f"{subject} ({edge.label or 'exit'})", **timeout)
    remember = getattr(client, "remember_route_visits", None)
    if remember is not None:
        remember(now=now)


def tolerance(client: Any) -> float:
    """Her configured tolerance (a share of the best route's cost); 0
    turns variety off."""
    value = getattr(getattr(client, "config", None), "route_variety_tolerance",
                    DEFAULT_TOLERANCE)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TOLERANCE
    if not math.isfinite(value) or value <= 0:
        return 0.0
    return min(MAX_TOLERANCE, value)


@dataclass(frozen=True, slots=True)
class Candidate:
    """A route (its edges), its walking cost in steps, the edge subjects
    it crosses, the maps it enters, in order and as a set, the map it
    starts on, and its walk on each map it walks (start and destination
    included)."""
    edges: tuple
    cost: float
    subjects: tuple[str, ...]
    maps: frozenset[str]
    sequence: tuple[str, ...] = ()
    start: str = ""
    walks: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True, slots=True)
class Choice:
    """The candidate taken, whether it is not the best route, why, how
    many alternatives were eligible, and the best route's cost."""
    candidate: Candidate
    varied: bool
    reason: str
    eligible: int
    best_cost: float


def usage(uses: dict[str, float], subjects: Iterable[str]) -> tuple[int, float, float]:
    """(unvisited ways, their share, mean decayed use) of a route's
    subjects, each counted by its pair of maps (``pair_uses``)."""
    values = [uses.get(pair(subject), 0.0) for subject in subjects]
    if not values:
        return 0, 0.0, 0.0
    unvisited = sum(value < UNVISITED_BELOW for value in values)
    return unvisited, unvisited / len(values), sum(values) / len(values)


def choose(best: Candidate, candidates: Sequence[Candidate], *,
           edge_uses: dict[str, tuple[float, float]], danger: Iterable[str],
           tolerance: float, now: float, hard: Iterable[str] = (),
           rng: random.Random | None = None) -> Choice:
    """The route to take: ``best`` (the planner's) or one of
    ``candidates``.

    A candidate is eligible when all of these hold:

    - its cost is at most ``tolerance`` above the best route's;
    - it enters no ``danger`` map (a hazard of any tier, a route death, a
      quarantined spot) that the best route does not, the start map
      counting as entered;
    - on every danger map it shares with the best route (the start and
      the destination included) its walk costs no more than the best
      route's walk there, so it never spends longer among hazards;
    - it crosses no ``hard`` map (a map that killed her, a route death, a
      quarantined spot) on the way, even one the best route crosses;
    - it crosses other maps than the best route (another square of the
      same doors is no other way).
    Among the best route and the eligible ones, the largest share of
    unvisited edges wins (a share, so a route is never preferred only for
    having more edges), then the least mean use, then a random draw."""
    rng = rng or random.Random()
    danger = frozenset(danger)
    hard = frozenset(hard)
    allowed = (best.maps | {best.start}) & danger
    best_walks = dict(best.walks)
    destination = best.sequence[-1] if best.sequence else ""
    limit = best.cost * (1.0 + tolerance)

    def safer(candidate: Candidate) -> bool:
        entered = (candidate.maps | {candidate.start}) & danger
        if not entered <= allowed:
            return False
        # The start is exempt only while she does not come back to it, the
        # destination only as the last map.
        if set(candidate.sequence[:-1]) & hard:
            return False
        walks = dict(candidate.walks)
        return all(walks.get(path, 0.0) <= best_walks.get(path, 0.0)
                   for path in entered)
    seen = {best.sequence}
    eligible = [best]
    longer = dangerous = 0
    for candidate in candidates:
        if candidate.sequence in seen:
            continue
        seen.add(candidate.sequence)
        if candidate.cost > limit:
            longer += 1
            continue
        if not safer(candidate):
            dangerous += 1
            continue
        eligible.append(candidate)

    uses = pair_uses(edge_uses, now)

    def key(candidate: Candidate) -> tuple:
        _, share, mean = usage(uses, candidate.subjects)
        return (round(share, 3), -round(mean, 2), rng.random())

    chosen = max(eligible, key=key)
    unvisited, _, mean = usage(uses, chosen.subjects)
    reason = (f"{len(eligible) - 1} alternative(s) within {tolerance:.0%}"
              f" ({longer} longer, {dangerous} more dangerous left out);"
              f" {unvisited} unvisited edge(s), mean use {mean:.1f}")
    return Choice(chosen, chosen is not best, reason, len(eligible) - 1, best.cost)


def candidate(edges: Sequence[Any], cost: float,
              walks: dict[str, float] | None = None, *, start: str = "") -> Candidate:
    return Candidate(tuple(edges), float(cost),
                     tuple(edge_subject(edge) for edge in edges),
                     frozenset(edge.destination for edge in edges),
                     tuple(edge.destination for edge in edges), start,
                     tuple(sorted((walks or {}).items())))
