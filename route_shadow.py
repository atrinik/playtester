"""Shadow planning with learned costs (docs/ROUTING.md 4.5, 4.8, P6).

With the owner's ``learned_route_costs`` flag off (the default), every live
plan and farm choice stays today's. Beside them, this module plans the same
trip, and scores the same farm spots, the way the flag would
(navigation.learned_route, navigation.farm_shadow), and records where the
two differ:

- ``route-shadow-diverge``: the shadow takes another route, or finds one
  where the live plan found none, or would farm another spot;
- ``route-shadow-decline``: the shadow would decline the trip (over its
  budget, no route, behind an owner block), would override it as an
  essential trip with care, or would pause farming at a cap.

Each comparison, agreements included, is also a row in her memory's
``route_shadow`` table (7 days, capped), which the ``route-shadow`` command
summarises per trip kind for the owner's divergence report (the P7 and P8
gates), and the dashboard's Tasks tab lists the last 20 differences.

Shadow work never slows navigation or raises into a tick:

- A request only takes a snapshot of the plan's inputs. The search runs as
  a generator, advanced by the task engine for at most SHADOW_STEP_SECONDS
  a step, so one whole-world search is spread over many ticks.
- At most one trip and one farm comparison are in flight. A trip is
  compared at most every SHADOW_TRIP_INTERVAL_SECONDS, and the same trip
  (kind, map, destination, live route) at most every
  SHADOW_TRIP_REPEAT_SECONDS; the farm score at most every
  SHADOW_FARM_SECONDS unless its inputs change, and not more than every
  SHADOW_FARM_MIN_SECONDS even then.
- Every hook catches its own errors; writes wait at most
  route_events.WRITE_TIMEOUT and a failed write keeps its rows (bounded).
"""

from __future__ import annotations

import collections
import logging
import math
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from . import route_costs, route_events
from .route_blocks import map_subject

log = logging.getLogger(__name__)

DIVERGE, DECLINE = "route-shadow-diverge", "route-shadow-decline"
# Results of one comparison (route_plans_total{result}, the table).
AGREED, DIVERGED = "agreed", "diverged"
# Per engine step, the longest the shadow searches.
SHADOW_STEP_SECONDS = 0.02
# Trip comparisons: at most one this often, the same trip this often.
SHADOW_TRIP_INTERVAL_SECONDS = 15.0
SHADOW_TRIP_REPEAT_SECONDS = 600.0
# The farm score: at most this often with unchanged inputs, and never more
# often than the second even when they change.
SHADOW_FARM_SECONDS = 300.0
SHADOW_FARM_MIN_SECONDS = 30.0
# A comparison still unfinished after this long (wall time) is dropped:
# its inputs are stale.
SHADOW_JOB_MAX_SECONDS = 180.0
# The same decision (kind, trip, places, result, shadow choice) is recorded
# at most this often; rows are kept for each comparison.
SHADOW_DECISION_REPEAT_SECONDS = 3600.0
# What the dashboard keeps and shows.
SHADOW_RECENT_LIMIT = 50
SHADOW_SHOWN = 20
# Rows waiting for a write, at most; written at most every FLUSH seconds.
SHADOW_PENDING_LIMIT = 500
SHADOW_FLUSH_SECONDS = 5.0
# Keys remembered for the repeat limits.
SHADOW_KEYS_LIMIT = 512


@dataclass(slots=True)
class _Job:
    kind: str
    generator: Iterator[Any]
    finish: Callable[[Any, Any], None]
    started: float
    spent: float = 0.0


@dataclass(slots=True)
class RouteShadow:
    """The shadow planner of one client."""

    clock: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic
    jobs: dict[str, _Job] = field(default_factory=dict)
    recent: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=SHADOW_RECENT_LIMIT))
    counts: dict[tuple[str, str, str], int] = field(default_factory=dict)
    pending: list[dict[str, Any]] = field(default_factory=list)
    last_cost: dict[str, float] = field(default_factory=dict)
    last_farm: dict[str, Any] | None = None
    _next_trip: float = -math.inf
    _trips_seen: dict[tuple, float] = field(default_factory=dict)
    _decided: dict[tuple, float] = field(default_factory=dict)
    _farm: tuple[Any, float] = (None, -math.inf)
    _next_flush: float = -math.inf
    _warned: bool = False
    skipped: int = 0
    dropped: int = 0

    # Requests.

    def request_trip(self, client: Any, task: Any, origin: Any, route: list | None,
                     error: str) -> None:
        """A live plan was made (or failed): compare it later."""
        if "trip" in self.jobs:
            self.skipped += 1
            return
        now = self.monotonic()
        if now < self._next_trip:
            self.skipped += 1
            return
        trip = task.trip_kind(client)
        source = origin[0] if origin else client.state.map.path
        live = tuple(route_costs.edge_subject(edge) for edge in route or ())
        key = (trip, source, task.destination, bool(task.strict_safety), live, error[:80])
        if now - self._trips_seen.get(key, -math.inf) < SHADOW_TRIP_REPEAT_SECONDS:
            self.skipped += 1
            return
        self._remember(self._trips_seen, key, now)
        self._next_trip = now + SHADOW_TRIP_INTERVAL_SECONDS
        snapshot = task._trip_snapshot(client, origin)
        from .navigation import learned_route

        context = {"trip": trip, "source": source, "destination": task.destination,
                   "strict": bool(task.strict_safety), "live": live,
                   "live_route": list(route or ()), "error": error,
                   "snapshot": snapshot}
        self.jobs["trip"] = _Job("trip", learned_route(snapshot),
                                 lambda client_, plan: self._finish_trip(client_, context, plan),
                                 self.clock())

    def request_farm(self, client: Any, graph: Any, live: tuple | None) -> None:
        """Autoplay chose a farm spot: score the same spots later."""
        if "farm" in self.jobs:
            return
        from .navigation import farm_shadow, farm_shadow_snapshot, farm_spot_key

        now = self.monotonic()
        last_key, last_at = self._farm
        if now - last_at < SHADOW_FARM_MIN_SECONDS:
            return
        analyzer = getattr(client, "farm_analyzer", None)
        model = route_costs.learned_model(client)
        ranking = tuple(
            (objective, tuple(str(spot.get("path") or spot.get("name") or "")
                              for spot in analyzer.ranking(objective)))
            for objective in ("xp", "money", "loot", "balanced", "safety")
        ) if analyzer is not None and callable(getattr(analyzer, "ranking", None)) else ()
        state = client.state
        key = (farm_spot_key(live[0], live[2]) if live else "", ranking, model.version,
               getattr(state, "savebed_map", ""), getattr(state, "farm_objective", ""),
               tuple(sorted((getattr(state, "farm_zone_quarantine", {}) or {}).items())),
               int(self.clock() // 600))
        if key == last_key and now - last_at < SHADOW_FARM_SECONDS:
            return
        self._farm = (key, now)
        snapshot = farm_shadow_snapshot(client, graph, live)
        if snapshot is None:
            return
        self.jobs["farm"] = _Job(
            "farm", farm_shadow(snapshot),
            lambda client_, result: self._finish_farm(client_, snapshot, live, result),
            self.clock())

    @staticmethod
    def _remember(keys: dict, key: Any, now: float) -> None:
        keys[key] = now
        if len(keys) > SHADOW_KEYS_LIMIT:
            for old in sorted(keys, key=keys.get)[:len(keys) - SHADOW_KEYS_LIMIT]:
                del keys[old]

    # Work.

    def step(self, client: Any, budget: float = SHADOW_STEP_SECONDS) -> None:
        """Advance the comparisons in flight for about ``budget`` seconds:
        each by at least one state expansion, then while time is left."""
        if self.jobs:
            started = self.monotonic()
            deadline = started + budget
            for kind in list(self.jobs):
                job = self.jobs[kind]
                if self.clock() - job.started > SHADOW_JOB_MAX_SECONDS:
                    del self.jobs[kind]
                    self.dropped += 1
                    continue
                result, done = None, False
                began = self.monotonic()
                try:
                    # At least one expansion a step, so a search always
                    # progresses; then more while the budget lasts.
                    next(job.generator)
                    while self.monotonic() < deadline:
                        next(job.generator)
                except StopIteration as stop:
                    result, done = stop.value, True
                except Exception:
                    log.exception("shadow %s plan failed", kind)
                    del self.jobs[kind]
                    self.dropped += 1
                    continue
                finally:
                    job.spent += self.monotonic() - began
                if done:
                    del self.jobs[kind]
                    job.finish(client, result)
                if self.monotonic() >= deadline:
                    break
        self.flush(client)

    def run(self, client: Any) -> None:
        """Finish every comparison in flight now (tests and tools)."""
        while self.jobs:
            self.step(client, budget=math.inf)

    # Results.

    def _finish_trip(self, client: Any, context: dict, plan: route_costs.LearnedPlan) -> None:
        snapshot = context["snapshot"]
        ctx = snapshot.context
        from .navigation import _edge_arrival

        live_route = context["live_route"]
        live_cost = (ctx.evaluate(live_route, snapshot.source_xy,
                                  lambda edge: _edge_arrival(snapshot.graph, edge))
                     if live_route else None)
        shadow = plan.subjects()
        if plan.route is None:
            result = plan.outcome
        elif not live_route or shadow != context["live"]:
            result = DIVERGED
        elif plan.outcome == route_costs.OVERRIDE:
            result = route_costs.OVERRIDE
        else:
            result = AGREED
        entry = {
            "at": self.clock(), "kind": "trip", "trip": context["trip"],
            "source": context["source"], "destination": context["destination"],
            "strict": context["strict"], "result": result, "outcome": plan.outcome,
            "care": plan.care, "budget": plan.budget, "reason": plan.reason[:300],
            "live": _route_summary(live_route, live_cost, context["error"]),
            "shadow": _route_summary(plan.route or [], plan.cost, ""),
        }
        if plan.crossed:
            entry["crossed"] = list(plan.crossed)
        if result == DIVERGED and live_route and plan.route:
            entry["first_difference"] = _first_difference(context["live"], shadow)
            entry["why"] = _why(live_cost, plan.cost)
        if plan.cost is not None:
            self.last_cost = {"time": plan.cost.time, "danger": plan.cost.prior + plan.cost.learned,
                              "learned": plan.cost.learned,
                              "budget": plan.budget if plan.budget is not None else 0.0}
        action = (DIVERGE if result == DIVERGED else
                  DECLINE if result != AGREED else "")
        decision_key = ("trip", context["trip"], context["source"], context["destination"],
                        result, shadow)
        self._record(client, entry, action, _trip_detail(entry), decision_key)

    def _finish_farm(self, client: Any, snapshot: Any, live: tuple | None,
                     result: tuple) -> None:
        score, chokepoints = result
        by_key = {candidate.key: candidate for candidate, _ in snapshot.candidates}
        live_key = snapshot.live
        choice = score.choice
        shadow_key = choice.key if choice is not None else ""
        if shadow_key == live_key:
            outcome = AGREED
        elif not shadow_key:
            outcome = route_costs.DECLINED
        else:
            outcome = DIVERGED
        entry = {
            "at": self.clock(), "kind": "farm", "trip": route_costs.FARM,
            "source": snapshot.origin, "destination": "", "result": outcome,
            "objective": score.objective, "top_rate": score.top_rate, "care": score.care,
            "live": _spot_summary(by_key.get(live_key), live, score),
            "shadow": _spot_summary(choice, None, score),
            "paused": dict(list(score.paused.items())[:8]),
            "chokepoints": sorted(chokepoints),
        }
        self.last_farm = entry
        action = (DIVERGE if outcome == DIVERGED else
                  DECLINE if outcome != AGREED else "")
        decision_key = ("farm", live_key, shadow_key, outcome)
        self._record(client, entry, action, _farm_detail(entry), decision_key)

    def note_live(self, client: Any, plan: route_costs.LearnedPlan) -> None:
        """A live plan with the flag on: its metrics and cost."""
        _count(client, plan.outcome, plan.trip)
        if plan.cost is not None:
            self.last_cost = {"time": plan.cost.time,
                              "danger": plan.cost.prior + plan.cost.learned,
                              "learned": plan.cost.learned,
                              "budget": plan.budget if plan.budget is not None else 0.0}

    def _record(self, client: Any, entry: dict, action: str, detail: str,
                decision_key: tuple) -> None:
        key = (entry["kind"], entry["trip"], entry["result"])
        self.counts[key] = self.counts.get(key, 0) + 1
        _count(client, "shadow_" + entry["result"], entry["trip"])
        if action:
            self.recent.append(entry)
            now = self.monotonic()
            if now - self._decided.get(decision_key, -math.inf) >= SHADOW_DECISION_REPEAT_SECONDS:
                self._remember(self._decided, decision_key, now)
                _decide(client, action, detail)
        self.pending.append(entry)
        del self.pending[:-SHADOW_PENDING_LIMIT]

    # Storage.

    def flush(self, client: Any, *, force: bool = False) -> bool:
        if not self.pending:
            return True
        now = self.monotonic()
        if not force and now < self._next_flush:
            return False
        self._next_flush = now + SHADOW_FLUSH_SECONDS
        recorder = route_events.recorder(client)
        character = recorder.character if recorder is not None else ""
        store = getattr(client, "_memory_store", None)
        if not character or not callable(store):
            return False
        try:
            store().record_route_shadow(character, self.pending, now=self.clock(),
                                        timeout=route_events.WRITE_TIMEOUT)
        except (sqlite3.Error, OSError, ValueError, TypeError) as error:
            if not self._warned:
                log.warning("route shadow rows not written yet (retrying): %s", error)
                self._warned = True
            return False
        self._warned = False
        self.pending = []
        return True

    # The dashboard.

    def summary(self, client: Any) -> dict[str, Any]:
        counts: dict[str, dict[str, dict[str, int]]] = {}
        for (kind, trip, result), count in sorted(self.counts.items()):
            counts.setdefault(kind, {}).setdefault(trip, {})[result] = count
        model = getattr(client, "_route_cost_model", None)
        return {
            "enabled": _flag(client),
            "counts": counts,
            "recent": list(reversed(list(self.recent)))[:SHADOW_SHOWN],
            "farm": self.last_farm,
            "in_flight": [{"kind": job.kind, "seconds": round(job.spent, 3)}
                          for job in self.jobs.values()],
            "skipped": self.skipped, "dropped": self.dropped,
            "pending_rows": len(self.pending),
            "model_version": getattr(model, "version", 0),
            "constants": {
                "errand_budget": route_costs.ERRAND_BUDGET_SECONDS,
                "strict_learned_avoid": route_costs.STRICT_LEARNED_AVOID_SECONDS,
                "farm_horizon": route_costs.FARM_VALUE_HORIZON_SECONDS,
                "farm_cap": route_costs.FARM_MAX_TRANSIT_DANGER_SECONDS,
                "chokepoint_cap": route_costs.FARM_MAX_CHOKEPOINT_DANGER_SECONDS,
                "farm_care": route_costs.FARM_ROUTE_CARE_SECONDS,
            },
        }


def _flag(client: Any) -> bool:
    return bool(getattr(getattr(client, "config", None), "learned_route_costs", False))


def _count(client: Any, result: str, trip: str) -> None:
    metrics = getattr(client, "metrics", None)
    if metrics is not None and callable(getattr(metrics, "route_plan", None)):
        metrics.route_plan(result, trip)


def _decide(client: Any, action: str, detail: str) -> None:
    recorder = getattr(client, "record_action", None)
    if not callable(recorder):
        return
    try:
        recorder(action, detail, save_timeout=route_events.WRITE_TIMEOUT)
    except TypeError:
        recorder(action, detail)


def _maps(route: list) -> list[str]:
    return [edge.destination for edge in route]


def _route_summary(route: list, cost: route_costs.RouteCost | None, error: str) -> dict:
    summary: dict[str, Any] = {"edges": len(route), "maps": _maps(route)[:40]}
    if cost is not None:
        summary.update(cost.describe())
    if error:
        summary["error"] = error[:300]
    return summary


def _first_difference(live: tuple, shadow: tuple) -> dict:
    index = next((i for i, (a, b) in enumerate(zip(live, shadow)) if a != b),
                 min(len(live), len(shadow)))
    return {"index": index, "live": live[index] if index < len(live) else "",
            "shadow": shadow[index] if index < len(shadow) else ""}


def _why(live: route_costs.RouteCost | None, shadow: route_costs.RouteCost | None) -> str:
    """What the shadow's route saves against the live one, by part."""
    if live is None or shadow is None:
        return ""
    parts = []
    for name, a, b in (("travel", live.time, shadow.time),
                       ("content prior", live.prior, shadow.prior),
                       ("learned danger", live.learned, shadow.learned)):
        delta = b - a
        if abs(delta) >= 1:
            parts.append(f"{delta:+.0f} s {name}")
    return ", ".join(parts) or "equal cost"


def _trip_detail(entry: dict) -> str:
    live, shadow = entry["live"], entry["shadow"]
    head = (f"{entry['trip']} trip {entry['source']} -> {entry['destination']}: "
            f"{entry['result']}")
    if entry["result"] == DIVERGED:
        return (f"{head}; live {live['edges']} edges ({live.get('error') or _cost_text(live)}), "
                f"shadow {shadow['edges']} edges ({_cost_text(shadow)}); "
                f"{entry.get('why') or 'live found no route'}")[:500]
    return (f"{head}; {entry['reason'] or entry['outcome']}"
            f"{'; care ' + entry['care'] if entry['care'] else ''}; "
            f"live {live['edges']} edges{' (' + live['error'] + ')' if live.get('error') else ''}"
            )[:500]


def _cost_text(summary: dict) -> str:
    if "total" not in summary:
        return "no cost"
    return (f"{summary['time']} s travel, {summary['prior']} s prior, "
            f"{summary['learned']} s learned")


def _spot_summary(candidate: route_costs.FarmCandidate | None, live: tuple | None,
                  score: route_costs.FarmScore) -> dict:
    if candidate is None:
        if live:
            spot = live[2]
            return {"key": f"{live[0]}:{spot.get('path') or spot.get('name') or ''}",
                    "name": str(spot.get("name") or ""), "danger": None,
                    "note": "not among the shadow's candidates"}
        return {}
    return {"key": candidate.key, "name": candidate.name, "objective": candidate.objective,
            "rank": candidate.rank, "rate": candidate.rate,
            "danger": None if candidate.danger is None else round(candidate.danger),
            "chokepoint_danger": {path: round(value)
                                  for path, value in candidate.chokepoints.items()},
            "score": (round(score.scores[candidate.key])
                      if candidate.key in score.scores else None),
            "reason": score.paused.get(candidate.key, candidate.reason)}


def _farm_detail(entry: dict) -> str:
    live, shadow = entry["live"], entry["shadow"]

    def spot(summary: dict) -> str:
        if not summary:
            return "none"
        return (f"{summary.get('name') or summary.get('key')} "
                f"(D {summary.get('danger')} s, score {summary.get('score')}"
                f"{', ' + summary['reason'] if summary.get('reason') else ''})")

    text = f"farm: {entry['result']}; live {spot(live)}, shadow {spot(shadow)}"
    if entry.get("care"):
        text += "; with farm-route care"
    if entry["result"] == route_costs.DECLINED and entry.get("paused"):
        text += "; paused: " + "; ".join(f"{key} {why}" for key, why in
                                         list(entry["paused"].items())[:3])
    return text[:500]


def plan_detail(plan: route_costs.LearnedPlan, destination: str) -> str:
    """A live learned plan's decision detail."""
    cost = plan.cost
    worst = ", ".join(f"{subject} {value:.0f} s" for subject, value in
                      (cost.worst if cost else ()))
    return (f"{plan.trip} trip to {destination}: {plan.outcome}"
            f"{f'; learned {cost.learned:.0f} s' if cost else ''}"
            f"{f' against a budget of {plan.budget:.0f} s' if plan.budget else ''}"
            f"{'; strict plan failed' if plan.strict_relaxed else ''}"
            f"{'; ' + worst if worst else ''}")[:500]


# Module hooks: each never raises into play.

def shadow(client: Any) -> RouteShadow | None:
    value = getattr(client, "route_shadow", None)
    return value if isinstance(value, RouteShadow) else None


def _guard(name: str, call: Callable[[RouteShadow], Any], client: Any) -> None:
    value = shadow(client)
    if value is None:
        return
    try:
        call(value)
    except Exception:
        log.exception("route shadow: %s failed", name)


def request_trip(client: Any, task: Any, origin: Any, route: list | None,
                 error: str) -> None:
    """A live plan (flag off): compare it with the learned plan later."""
    if _flag(client):
        return
    _guard("trip", lambda value: value.request_trip(client, task, origin, route, error),
           client)


def request_farm(client: Any, graph: Any, live: tuple | None) -> None:
    """Autoplay's farm choice (flag off): compare it with the farm score."""
    _guard("farm", lambda value: value.request_farm(client, graph, live), client)


def step(client: Any, budget: float = SHADOW_STEP_SECONDS) -> None:
    """One task-engine step: advance the comparisons for at most ``budget``."""
    _guard("step", lambda value: value.step(client, budget), client)


def note_live_plan(client: Any, plan: route_costs.LearnedPlan) -> None:
    """A live learned plan (flag on)."""
    _guard("live", lambda value: value.note_live(client, plan), client)


def summarise_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The divergence report from ``route_shadow`` rows: counts per kind,
    trip and result, the subjects that most often decide a difference, and
    the newest differences (route-shadow command)."""
    counts: dict[str, dict[str, dict[str, int]]] = {}
    reasons: collections.Counter = collections.Counter()
    differences = []
    for row in rows:
        detail = row.get("detail") or {}
        kind, trip, result = row.get("kind", ""), row.get("trip", ""), row.get("result", "")
        bucket = counts.setdefault(kind, {}).setdefault(trip, {})
        bucket[result] = bucket.get(result, 0) + 1
        if result == AGREED:
            continue
        differences.append(row)
        shadow = detail.get("shadow") or {}
        for subject, _ in shadow.get("worst") or ():
            reasons[subject] += 1
        live = detail.get("live") or {}
        for subject, _ in live.get("worst") or ():
            reasons[subject] += 1
        if kind == "farm":
            for path in (live.get("chokepoint_danger") or {}):
                reasons[map_subject(path)] += 1
    return {"rows": len(rows), "counts": counts,
            "subjects": reasons.most_common(15),
            "differences": differences[-SHADOW_SHOWN:]}

