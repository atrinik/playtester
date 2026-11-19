"""Learned route costs, trip budgets and the farm score (docs/ROUTING.md
4.4, 4.5, 4.9; phase P6).

Every cost is in expected seconds. Travel time is seconds; danger is
converted at the owner's rate, one expected death = 1800 s (30 minutes of
travel), so the two add up.

- **Travel time.** An edge costs the walk from where she arrives on its
  source map to its departure square (Chebyshev tiles, a lower bound on the
  walk) at her seconds per tile, plus a fixed transition overhead. Once she
  has crossed an edge with its time measured (route_events.py traversals),
  the measured mean replaces that prior, weighted down with its age
  (TRAVERSAL_HALF_LIFE).
- **Content priors.** Today's hazard tiers, graded: over-level spawns,
  aggro packs on every map (not only ``world_*``, G4), ranged or caster
  residents below level 18, and ``KNOWN_LETHAL_TRANSIT``. A prior steers the
  route choice only: it never counts toward a budget, a farm cap or care,
  so content danger can never stop a trip or pause farming without a
  death (4.5).
- **Learned danger.** Her route events, each weighted, halving with its
  signal's half-life, counted only while the kit she had then is not
  genuinely beaten by her kit now (farm_analyzer.survivability_unchanged,
  #94's predicate), and multiplied by the recovery her safe passes since
  earned (route_events.RECOVERY_FACTOR; deaths never recover).
  Contributions under a second are dropped.

A visit to a map M, left by edge E, costs M's learned danger (deaths,
recalls, damage) plus E's own failures (blocked, crossing_failed). Edge and
cell deaths are not added again: P5 records a death on the map, the edge
and a cell at once, so adding them would count one death two or three
times, and 4.5's arithmetic (one fresh death = 1800 s on every route
through that map) assumes it counts once. Cells are kept for P9.

Nothing here touches play. The model is read from her memory database at
most every MODEL_REFRESH_SECONDS (sooner after a write, never more often
than MODEL_MIN_REFRESH_SECONDS), each read waiting at most
route_events.WRITE_TIMEOUT; a failed read keeps the last model.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

from . import route_events
from .farm_analyzer import survivability_unchanged
from .route_blocks import edge_subject, map_subject

log = logging.getLogger(__name__)

# The owner's exchange rate (ROUTING 4.4): one expected death.
EXPECTED_DEATH_SECONDS = 1800.0
# Travel-time prior: seconds per tile when her speed is unknown, the bounds
# on it, and the fixed overhead of one transition (the click, the load).
DEFAULT_SECONDS_PER_TILE = 0.5
MIN_SECONDS_PER_TILE = 0.1
MAX_SECONDS_PER_TILE = 2.0
TRANSITION_SECONDS = 2.0
# A dialogue voyage: walking beside the NPC and following its pages.
VOYAGE_SECONDS = 20.0
# Content priors (4.4): graded versions of today's tiers.
PRIOR_LETHAL_SECONDS = 3600.0
PRIOR_OVERLEVEL_SECONDS = 900.0
PRIOR_OVERLEVEL_PER_LEVEL = 60.0
PRIOR_OVERLEVEL_MAX_SECONDS = 1800.0
PRIOR_RANGED_SECONDS = 900.0
RANGED_PRIOR_LEVEL = 18
PRIOR_PACK_MIN = 3
PRIOR_PACK_PER_MEMBER = 300.0
PRIOR_PACK_MAX_SECONDS = 1800.0
PRIOR_MAP_MAX_SECONDS = 3600.0
# Safety against time (4.5): the danger weight while she plays for safety,
# is depleted, has no ready recall, or the trip is optional.
SAFETY_LAMBDA = 2.0
# Budgets (4.5, owner decision): optional errands and quest steps decline a
# route whose learned danger is at or above one expected death.
ERRAND_BUDGET_SECONDS = 1800.0
# strict_safety's learned avoid set: maps with at least this much learned
# danger (half a death), replacing #94-era route_hazards.
STRICT_LEARNED_AVOID_SECONDS = 900.0
# The farm score (4.5).
FARM_VALUE_HORIZON_SECONDS = 3600.0
FARM_MAX_TRANSIT_DANGER_SECONDS = 3000.0
FARM_MAX_CHOKEPOINT_DANGER_SECONDS = 4500.0
FARM_ROUTE_CARE_SECONDS = 1800.0
# #94's chokepoint test (navigation.ROUTE_DEATH_CHOKEPOINT_SHARE): a map
# whose removal cuts off more than this share of the maps she can reach.
CHOKEPOINT_SHARE = 0.5
# Native search costs are integers: tenths of a second, at least one.
COST_SCALE = 10
MAX_SCALED_COST = 2 ** 40
# The model's re-read period, and the shortest gap after a write.
MODEL_REFRESH_SECONDS = 30.0
MODEL_MIN_REFRESH_SECONDS = 5.0
# Rows the model reads at most (the per-signal caps sum to 11,000).
MODEL_ROW_LIMIT = 20000
# The dominant subjects a plan reports.
WORST_SUBJECTS = 3

# What a farm objective's rate is, per analyzer view (atrinik/tools
# farming_analyzer cli.py rankings).
OBJECTIVE_RATES = {"xp": "xp_hour", "money": "money_hour", "loot": "loot_hour",
                   "balanced": "score", "safety": "safety_score"}

EARLY_ROUTE_HAZARD = re.compile(
    r"(?:rock thrower|battle thrower|skel(?:eton)? mage|"
    r"skeleton (?:novice )?archer|lost soul)", re.I)


def drain(generator: Iterator[Any]) -> Any:
    """Run a planning generator to the end; its return value."""
    while True:
        try:
            next(generator)
        except StopIteration as done:
            return done.value


def scaled(seconds: float) -> int:
    """A cost in seconds as the native search's integer cost."""
    if not math.isfinite(seconds) or seconds <= 0:
        return 1
    return max(1, min(MAX_SCALED_COST, int(round(seconds * COST_SCALE))))


def seconds_per_tile(stats: dict) -> float:
    """Her walking pace: one tile per server move at her speed."""
    try:
        speed = float(stats.get("speed", 0) or 0)
    except (TypeError, ValueError):
        speed = 0.0
    if speed <= 0 or not math.isfinite(speed):
        return DEFAULT_SECONDS_PER_TILE
    return max(MIN_SECONDS_PER_TILE, min(MAX_SECONDS_PER_TILE, 1.0 / speed))


def _kit(text: str) -> dict | None:
    if not text:
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def kit_counts(row_kit: str, current: dict | None) -> bool:
    """#94's kit rule (4.4): a danger row stops counting once she has a
    survivability gain and no material loss since it was made. Without
    either kit it counts."""
    earlier = _kit(row_kit)
    if earlier is None or not current:
        return True
    return survivability_unchanged(current, earlier)


@dataclass(slots=True)
class LearnedView:
    """The learned costs at one moment: dictionary lookups for the search."""

    at: float
    # map path -> learned danger seconds (every signal on its map subject)
    maps: dict[str, float] = field(default_factory=dict)
    # edge subject -> failures in seconds (blocked, crossing_failed)
    edges: dict[str, float] = field(default_factory=dict)
    # cell subject -> seconds (kept for P9 and the dashboard)
    cells: dict[str, float] = field(default_factory=dict)
    # edge subject -> (measured mean seconds, its weight 0..1)
    travel: dict[str, tuple[float, float]] = field(default_factory=dict)
    # map path -> the signals that make up its danger: {signal: seconds}
    signals: dict[str, dict[str, float]] = field(default_factory=dict)

    def map_danger(self, path: str) -> float:
        return self.maps.get(path, 0.0)

    def edge_danger(self, subject: str) -> float:
        return self.edges.get(subject, 0.0)

    def subjects(self) -> int:
        return len(self.maps) + len(self.edges) + len(self.cells)


EMPTY_VIEW = LearnedView(0.0)


@dataclass(slots=True)
class RouteCostModel:
    """Her learned route rows, folded for the planner (ROUTING 4.4)."""

    rows: list[tuple[float, str, str, float, str, float]] = field(default_factory=list)
    traversals: dict[str, dict[str, Any]] = field(default_factory=dict)
    version: int = 0
    loaded_at: float = 0.0
    character: str = ""
    _views: dict = field(default_factory=dict)

    def view(self, now: float, kit: dict | None = None) -> LearnedView:
        """The costs at ``now`` (cached per minute and kit)."""
        key = (int(now // 60), json.dumps(kit, sort_keys=True) if kit else "")
        cached = self._views.get(key)
        if cached is not None:
            return cached
        view = LearnedView(now)
        for at, subject, signal, magnitude, row_kit, recovery in self.rows:
            if signal not in route_events.WEIGHTS or not kit_counts(row_kit, kit):
                continue
            factor = 1.0 if signal == route_events.DEATH else max(0.0, min(1.0, recovery))
            value = route_events.contribution(signal, magnitude, now - at) * factor
            if value < route_events.MIN_CONTRIBUTION:
                continue
            kind = route_events.subject_kind(subject)
            if kind == "map":
                path = subject[len("map:"):]
                view.maps[path] = view.maps.get(path, 0.0) + value
                parts = view.signals.setdefault(path, {})
                parts[signal] = parts.get(signal, 0.0) + value
            elif kind == route_events.CELL:
                view.cells[subject] = view.cells.get(subject, 0.0) + value
            elif signal != route_events.DEATH:
                # An edge's own failures; its deaths are the map's.
                view.edges[subject] = view.edges.get(subject, 0.0) + value
        for subject, row in self.traversals.items():
            if route_events.subject_kind(subject) in ("map", route_events.CELL):
                continue
            timed, mean = int(row.get("timed", 0) or 0), float(row.get("mean_seconds", 0.0))
            if timed <= 0 or not (mean > 0 and math.isfinite(mean)):
                continue
            age = max(0.0, now - float(row.get("last_at", now) or now))
            weight = 0.5 ** (age / route_events.TRAVERSAL_HALF_LIFE)
            view.travel[subject] = (mean, weight)
        if len(self._views) > 8:
            self._views.clear()
        self._views[key] = view
        return view


def current_kit(client: Any) -> dict | None:
    """Her survivability now (route_events.KIT_FIELDS), or None."""
    text = route_events.kit_text(client)
    return _kit(text)


def learned_model(client: Any, *, now: float | None = None,
                  refresh: bool = False) -> RouteCostModel:
    """Her learned route model, re-read from memory at most every
    MODEL_REFRESH_SECONDS, sooner (but not within MODEL_MIN_REFRESH_SECONDS)
    after the recorder wrote. Never raises: a failed read keeps the last
    model (an empty one before the first)."""
    recorder = route_events.recorder(client)
    character = recorder.character if recorder is not None else ""
    model = getattr(client, "_route_cost_model", None)
    monotonic = time.monotonic()
    written = recorder.written if recorder is not None else 0
    state = getattr(client, "_route_cost_model_state", (None, -math.inf, -1))
    if isinstance(model, RouteCostModel) and model.character == character and not refresh:
        age = monotonic - state[1]
        if age < MODEL_MIN_REFRESH_SECONDS or (
                age < MODEL_REFRESH_SECONDS and state[2] == written):
            return model
    if not isinstance(model, RouteCostModel) or model.character != character:
        model = RouteCostModel(character=character)
    if not character:
        _keep(client, model, monotonic, written)
        return model
    now = time.time() if now is None else now
    try:
        store = client._memory_store()
        rows = store.route_cost_rows(character, since=now - route_events.EVENT_MAX_AGE,
                                     timeout=route_events.WRITE_TIMEOUT)
        traversals = store.route_traversal_rows(character,
                                                timeout=route_events.WRITE_TIMEOUT)
    except (sqlite3.Error, OSError, AttributeError, TypeError, ValueError) as error:
        log.debug("learned route costs not readable now: %s", error)
        _keep(client, model, monotonic, written)
        return model
    rows = rows[-MODEL_ROW_LIMIT:]
    if rows != model.rows or traversals != model.traversals:
        model = RouteCostModel(rows=rows, traversals=traversals, version=model.version + 1,
                               loaded_at=now, character=character)
    _keep(client, model, monotonic, written)
    return model


def _keep(client: Any, model: RouteCostModel, monotonic: float, written: int) -> None:
    try:
        client._route_cost_model = model
        client._route_cost_model_state = (model.character, monotonic, written)
    except AttributeError:
        pass


# Priors per graph (weakly held: a graph rebuilt or dropped takes its own
# with it), then per (level, ranged at any level, lethal maps).
_PRIORS: "weakref.WeakKeyDictionary[Any, dict[tuple, dict[str, float]]]" = \
    weakref.WeakKeyDictionary()


def content_priors(graph: Any, level: int, *, lethal: Iterable[str] = (),
                   any_level_ranged: bool = False) -> dict[str, float]:
    """Each map's content prior in seconds for a character of ``level``
    (cached per graph and level): today's tiers, graded (4.4)."""
    key = (int(level), bool(any_level_ranged), frozenset(lethal))
    try:
        per_graph = _PRIORS.setdefault(graph, {})
    except TypeError:
        per_graph = {}
    cached = per_graph.get(key)
    if cached is not None:
        return cached
    level = max(1, int(level))
    margin = max(3, (level + 4) // 5)
    priors: dict[str, float] = {}

    def add(path: str, seconds: float) -> None:
        priors[path] = min(PRIOR_MAP_MAX_SECONDS, priors.get(path, 0.0) + seconds)

    nodes = getattr(graph, "nodes", {})
    for path, identities in (getattr(graph, "map_monster_levels", {}) or {}).items():
        node = nodes.get(path)
        peaceful = node.peaceful_identities if node is not None else set()
        excess = [monster_level - (level + margin)
                  for identity, monster_level in identities.items()
                  if monster_level >= level + margin and identity not in peaceful]
        if excess:
            add(path, min(PRIOR_OVERLEVEL_MAX_SECONDS,
                          PRIOR_OVERLEVEL_SECONDS + PRIOR_OVERLEVEL_PER_LEVEL * max(excess)))
        if any_level_ranged or level < RANGED_PRIOR_LEVEL:
            for identity in identities:
                normalized = re.sub(r"\.\d+$", "",
                                    identity.casefold().strip().replace("_", " "))
                if EARLY_ROUTE_HAZARD.search(normalized) and normalized not in peaceful:
                    add(path, PRIOR_RANGED_SECONDS)
                    break
    estimator = getattr(graph, "transit_max_aggro_pack", None)
    if callable(estimator):
        for path in getattr(graph, "named_spawns", {}) or {}:
            pack = estimator(path)
            if pack >= PRIOR_PACK_MIN:
                add(path, min(PRIOR_PACK_MAX_SECONDS,
                              PRIOR_PACK_PER_MEMBER * (pack - PRIOR_PACK_MIN + 1)))
    for path in lethal:
        add(path, PRIOR_LETHAL_SECONDS)
    if len(per_graph) > 16:
        per_graph.clear()
    per_graph[key] = priors
    return priors


@dataclass(slots=True)
class EdgeCost:
    """One edge's part of a route's cost."""

    subject: str
    destination: str
    time: float
    prior: float
    learned: float


@dataclass(slots=True)
class RouteCost:
    """A route's cost: its time, its content prior and learned danger in
    seconds, and the subjects that dominate its learned danger."""

    time: float = 0.0
    prior: float = 0.0
    learned: float = 0.0
    lam: float = 1.0
    edges: list[EdgeCost] = field(default_factory=list)
    worst: list[tuple[str, float]] = field(default_factory=list)

    @property
    def total(self) -> float:
        return self.time + self.lam * (self.prior + self.learned)

    def describe(self) -> dict[str, Any]:
        return {"time": round(self.time), "prior": round(self.prior),
                "learned": round(self.learned), "total": round(self.total),
                "edges": len(self.edges),
                "worst": [[subject, round(value)] for subject, value in self.worst]}


@dataclass(slots=True)
class CostContext:
    """What one weighted plan prices edges with (4.5)."""

    graph: Any
    view: LearnedView
    priors: dict[str, float]
    pace: float = DEFAULT_SECONDS_PER_TILE
    lam: float = 1.0
    _subjects: dict = field(default_factory=dict)

    def subject(self, edge: Any) -> str:
        key = (edge.kind, edge.source, edge.destination, edge.x, edge.y, edge.label)
        subject = self._subjects.get(key)
        if subject is None:
            subject = self._subjects[key] = edge_subject(edge)
        return subject

    def edge_time(self, edge: Any, position: tuple[int, int]) -> float:
        dx, dy = route_events.departure(self.graph, edge)
        steps = max(abs(position[0] - dx), abs(position[1] - dy))
        prior = steps * self.pace + TRANSITION_SECONDS
        if getattr(edge, "kind", "") == "dialogue":
            prior += VOYAGE_SECONDS
        measured = self.view.travel.get(self.subject(edge))
        if measured is None:
            return prior
        mean, weight = measured
        return weight * mean + (1.0 - weight) * prior

    def edge_learned(self, edge: Any) -> float:
        return self.view.map_danger(edge.destination) + self.view.edge_danger(self.subject(edge))

    def edge_cost(self, edge: Any, position: tuple[int, int],
                  arrival: tuple[int, int]) -> float:
        """The search's cost of one transition (4.5)."""
        danger = self.priors.get(edge.destination, 0.0) + self.edge_learned(edge)
        return self.edge_time(edge, position) + self.lam * danger

    def evaluate(self, route: list, source_xy: tuple[int, int],
                 arrival: Callable[[Any], tuple[int, int]]) -> RouteCost:
        """``route``'s cost, edge by edge, from ``source_xy``."""
        cost = RouteCost(lam=self.lam)
        position = source_xy
        contributions: dict[str, float] = {}
        for edge in route:
            subject = self.subject(edge)
            time_ = self.edge_time(edge, position)
            prior = self.priors.get(edge.destination, 0.0)
            on_map = self.view.map_danger(edge.destination)
            on_edge = self.view.edge_danger(subject)
            cost.edges.append(EdgeCost(subject, edge.destination, time_, prior, on_map + on_edge))
            cost.time += time_
            cost.prior += prior
            cost.learned += on_map + on_edge
            if on_map:
                key = map_subject(edge.destination)
                contributions[key] = contributions.get(key, 0.0) + on_map
            if on_edge:
                contributions[subject] = contributions.get(subject, 0.0) + on_edge
            position = arrival(edge)
        cost.worst = sorted(contributions.items(), key=lambda item: (-item[1], item[0])
                            )[:WORST_SUBJECTS]
        return cost


# The farm score (4.5).

@dataclass(slots=True)
class FarmCandidate:
    """One analyzer spot as the farm score sees it."""

    key: str
    name: str
    objective: str
    position: int
    rank: int
    rate: float
    # Learned transit danger in seconds: the whole D, and the part on
    # chokepoint maps, per map. None while not known yet.
    danger: float | None = None
    chokepoints: dict[str, float] = field(default_factory=dict)
    maps: tuple[str, ...] = ()
    reason: str = ""

    @property
    def ordinary(self) -> float:
        return (self.danger or 0.0) - sum(self.chokepoints.values())


@dataclass(slots=True)
class FarmScore:
    """The farm score's choice, or why there is none."""

    choice: FarmCandidate | None
    objective: str = ""
    top_rate: float = 0.0
    scores: dict[str, float] = field(default_factory=dict)
    paused: dict[str, str] = field(default_factory=dict)
    care: bool = False


def spot_rate(spot: dict, objective: str) -> float:
    """The analyzer's predicted rate for ``objective``, clamped at 0."""
    value = spot.get(OBJECTIVE_RATES.get(objective, "score"))
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return 0.0
    return rate if rate > 0 and math.isfinite(rate) else 0.0


def farm_score(candidates: list[FarmCandidate]) -> FarmScore:
    """The usable spot with the highest score (4.5):

    score = FARM_VALUE_HORIZON_SECONDS × r / r_top − D

    Usable: D known, ordinary D under FARM_MAX_TRANSIT_DANGER_SECONDS, and
    every chokepoint's danger under FARM_MAX_CHOKEPOINT_DANGER_SECONDS.
    The first objective (by ``position``) with a usable spot is scored on
    its own; rates of two objectives are never compared. ``r_top`` is the
    highest rate among its usable spots; at r_top <= 0 the value term is 0.
    Ties keep the higher-ranked spot.
    """
    paused: dict[str, str] = {}
    usable: list[FarmCandidate] = []
    for candidate in candidates:
        if candidate.danger is None:
            paused[candidate.key] = candidate.reason or "transit danger not known yet"
            continue
        choke = [path for path, value in candidate.chokepoints.items()
                 if value >= FARM_MAX_CHOKEPOINT_DANGER_SECONDS]
        if choke:
            paused[candidate.key] = f"chokepoint {', '.join(sorted(choke))} at its cap"
        elif candidate.ordinary >= FARM_MAX_TRANSIT_DANGER_SECONDS:
            paused[candidate.key] = f"transit danger {candidate.ordinary:.0f} s at its cap"
        else:
            usable.append(candidate)
    if not usable:
        return FarmScore(None, paused=paused)
    first = min(candidate.position for candidate in usable)
    scored = [candidate for candidate in usable if candidate.position == first]
    top = max(candidate.rate for candidate in scored)
    scores = {}
    for candidate in scored:
        value = FARM_VALUE_HORIZON_SECONDS * candidate.rate / top if top > 0 else 0.0
        scores[candidate.key] = value - (candidate.danger or 0.0)
    choice = max(scored, key=lambda candidate: (scores[candidate.key], -candidate.rank))
    return FarmScore(choice, objective=choice.objective, top_rate=top, scores=scores,
                     paused=paused,
                     care=(choice.danger or 0.0) >= FARM_ROUTE_CARE_SECONDS)


# A plan with learned costs (4.5).

PLANNED, DECLINED, OVERRIDE, NO_ROUTE, BLOCKED = (
    "planned", "declined", "override", "no_route", "blocked")
# The trip kinds a plan reports (bounded: they label metrics). An essential
# trip reports its kind (navigation.ESSENTIAL_TRIPS).
FARM, ERRAND, QUEST, STRICT, OTHER = "farm", "errand", "quest", "strict", "other"


@dataclass(slots=True)
class LearnedPlan:
    """What the learned-cost planner decides for one trip.

    ``outcome`` is PLANNED, DECLINED (an optional trip over its budget),
    OVERRIDE (an essential trip over its budget, or whose strict plan
    failed, takes the cheapest route with CARE_BOUNDED care), NO_ROUTE or
    BLOCKED (an optional trip whose only route crosses an owner block).
    ``crossed`` are the owner blocks an essential trip crosses with care.
    """

    route: list | None
    outcome: str
    trip: str
    cost: RouteCost | None = None
    budget: float | None = None
    care: str = ""
    crossed: tuple[str, ...] = ()
    reason: str = ""
    strict_relaxed: bool = False

    def subjects(self) -> tuple[str, ...]:
        return tuple(edge_subject(edge) for edge in self.route or ())


@dataclass(slots=True)
class TripSnapshot:
    """A trip's planning inputs, taken once (so a shadow plan spread over
    several ticks plans what the live plan saw)."""

    graph: Any
    source: str
    source_xy: tuple[int, int]
    destination: str
    goals: list
    access: Any
    excluded: set
    walls: Any
    offer: Any
    strict: bool
    essential: bool
    trip: str
    strict_avoided: frozenset
    context: CostContext


def plan_budget(snapshot: TripSnapshot) -> float | None:
    """The trip's danger budget (4.5): none for farm trips (they choose by
    score) and strict trips (a per-map budget), one expected death for
    every other trip."""
    if snapshot.strict or snapshot.trip == FARM:
        return None
    return ERRAND_BUDGET_SECONDS
