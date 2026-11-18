"""Dashboard alerts: what needs the operator's attention right now.

Raised for repeated deaths, starvation, an idle character that cannot reach
food, farming suspended while unsafe (and money that blocks its end), a
character that is stuck while autoplay runs, a lost connection, and a task
engine whose steps keep failing.
The client keeps a ``ProgressWatch`` current; the dashboard serves the active
alerts on every live poll.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

DEATH_WINDOW_SECONDS = 3600.0
REPEATED_DEATHS = 2
STUCK_SECONDS = 20 * 60.0
DISCONNECTED_SECONDS = 180.0
# Below this food level, with at most one meal left, she must go and get
# food (navigation.hungry).
HUNGRY_FOOD = 180
# An idle character's failed food trip stays alerted this long unless she
# eats since; IdleSurvival retries at most hourly.
IDLE_FOOD_ALERT_SECONDS = 2 * 3600.0
IDLE_HUNGER_ACTIONS = ("idle-hunger", "idle-hunger-fed", "idle-hunger-failed")
# Consecutive failed task engine steps before the engine-error alert, and
# how long they last before it turns critical. A failing step runs no task
# and no idle survival, so she stands there.
ENGINE_ERROR_STEPS = 5
ENGINE_ERROR_CRITICAL_SECONDS = 10 * 60.0
# The farm circuit's unsafe-farming decisions (navigation.farming_unsafe_reason):
# suspended with its reason, money that blocks the way out, and resumed.
FARMING_SUSPENDED = "farming-suspended"
FARMING_RESUMED = "farming-resumed"
UNFUNDED_ACTIONS = ("cure-unfunded", "recall-unfunded")


class ProgressWatch:
    """When experience, position and the connection last changed."""

    def __init__(self, now: float | None = None):
        now = time.time() if now is None else now
        self.exp: Any = None
        self.exp_changed_at = now
        self.position: tuple | None = None
        self.moved_at = now
        self.disconnected_since: float | None = None

    def observe(self, state: Any, now: float | None = None) -> None:
        now = time.time() if now is None else now
        phase = getattr(state, "phase", "")
        m = getattr(state, "map", None)
        # A reconnect swaps in an empty placeholder map and clears stats; only
        # a playing character on a real map can move or gain experience.
        if phase == "playing" and getattr(m, "path", ""):
            exp = (getattr(state, "stats", {}) or {}).get("exp")
            if exp != self.exp:
                self.exp, self.exp_changed_at = exp, now
            position = (m.path, getattr(m, "world_x", 0), getattr(m, "world_y", 0))
            if position != self.position:
                self.position, self.moved_at = position, now
        if phase == "playing":
            self.disconnected_since = None
        elif phase == "disconnected" and self.disconnected_since is None:
            # Reconnect attempts pass through connecting/login phases; the
            # outage lasts until she is playing again.
            self.disconnected_since = now


def death_count(decisions: Iterable[dict], since: float) -> int:
    """Deaths recorded since ``since``.

    kill_player (server types/player.c) sends one notice per defeat, so each
    death record is one death. The client keeps each in its own record, with
    its own time; only a record persisted before that carries a ``count``.
    """
    return sum(max(1, int(record.get("count", 1) or 1))
               for record in decisions
               if isinstance(record, dict) and record.get("action") == "death" and
               float(record.get("time", 0.0) or 0.0) >= since)


def idle_food_failure(decisions: Iterable[dict], now: float) -> dict | None:
    """The idle survival guard's latest outcome, when it is a recent failure.

    Eating or a new trip since the failure clears it; ``active`` also
    clears it once her live state no longer needs food.
    """
    latest = None
    for record in decisions:
        if (isinstance(record, dict) and
                record.get("action") in IDLE_HUNGER_ACTIONS and
                (latest is None or float(record.get("time", 0.0) or 0.0) >=
                 float(latest.get("time", 0.0) or 0.0))):
            latest = record
    if (latest is None or latest.get("action") != "idle-hunger-failed" or
            now - float(latest.get("time", 0.0) or 0.0) >=
            IDLE_FOOD_ALERT_SECONDS):
        return None
    return latest


def farming_suspension(decisions: Iterable[dict], since: float
                       ) -> tuple[dict | None, dict | None]:
    """The suspension still in force and its money blocker, if any.

    Only records since ``since`` (the running autoplay's start) and after
    the latest farming-resumed count: a new run records its own.
    """
    suspended = unfunded = None
    for record in sorted(
            (record for record in decisions if isinstance(record, dict) and
             float(record.get("time", 0.0) or 0.0) >= since and
             record.get("action") in (FARMING_SUSPENDED, FARMING_RESUMED,
                                      *UNFUNDED_ACTIONS)),
            key=lambda record: float(record.get("time", 0.0) or 0.0)):
        action = record.get("action")
        if action == FARMING_RESUMED:
            suspended = unfunded = None
        elif action == FARMING_SUSPENDED:
            # A new reason starts a new episode: its money blocker, if
            # any, is recorded again after it.
            suspended, unfunded = record, None
        else:
            unfunded = record
    return suspended, unfunded if suspended is not None else None


def active(state: Any, decisions: Iterable[dict], watch: ProgressWatch, *,
           autoplay_started_at: float | None, task_running: bool = False,
           engine_error: dict | None = None,
           now: float | None = None) -> list[dict[str, str]]:
    """The alerts that apply right now, most severe first.

    ``autoplay_started_at`` is when the running autoplay task started, or
    None when autoplay is not running; ``task_running`` is whether any task
    runs; ``engine_error`` is the task engine's run of failed steps
    (``error``, ``count``, ``since``), or None.
    """
    now = time.time() if now is None else now
    alerts: list[dict[str, str]] = []
    if engine_error and int(engine_error.get("count", 0)) > ENGINE_ERROR_STEPS:
        since = float(engine_error.get("since", now) or now)
        minutes = int(max(0.0, now - since) // 60)
        alerts.append({
            "id": "engine-error",
            "level": ("critical" if now - since >= ENGINE_ERROR_CRITICAL_SECONDS
                      else "warn"),
            "text": (f"Task engine failing: {engine_error['count']} steps in a "
                     f"row for {minutes} min: {engine_error.get('error', '')}")})
    if (watch.disconnected_since is not None and
            now - watch.disconnected_since >= DISCONNECTED_SECONDS):
        minutes = int((now - watch.disconnected_since) // 60)
        alerts.append({"id": "disconnected", "level": "critical",
                       "text": f"Disconnected for {minutes} min"})
    deaths = death_count(decisions, now - DEATH_WINDOW_SECONDS)
    if deaths >= REPEATED_DEATHS:
        alerts.append({"id": "deaths", "level": "critical",
                       "text": f"{deaths} deaths in the last hour"})
    stats = getattr(state, "stats", {}) or {}
    if (getattr(state, "phase", "") == "playing" and "food" in stats and
            int(stats.get("food") or 0) <= 0):
        alerts.append({"id": "starving", "level": "critical",
                       "text": "Starving: food is 0"})
    # Only while she is still idle and still needs food: fed by hand, or by
    # a task the operator started, the failure no longer matters.
    failure = (idle_food_failure(decisions, now)
               if not task_running and
               getattr(state, "phase", "") == "playing" and "food" in stats and
               int(stats.get("food") or 0) < HUNGRY_FOOD else None)
    if failure is not None:
        alerts.append({"id": "idle-food", "level": "critical",
                       "text": "Idle and cannot reach food: "
                               f"{failure.get('detail', '')}"})
    suspended = None
    if autoplay_started_at is not None:
        suspended, unfunded = farming_suspension(decisions, autoplay_started_at)
        if unfunded is not None:
            action = str(unfunded.get("action"))
            label = "Cure" if action == "cure-unfunded" else "Recall"
            alerts.append({"id": action, "level": "critical",
                           "text": f"{label} unfunded: "
                                   f"{unfunded.get('detail', '')}"})
        if suspended is not None:
            alerts.append({"id": FARMING_SUSPENDED, "level": "warn",
                           "text": "Farming suspended: "
                                   f"{suspended.get('detail', '')}"})
    if autoplay_started_at is not None and getattr(state, "phase", "") == "playing":
        # Inactivity before this autoplay run started is not being stuck,
        # and neither is waiting out a farming suspension.
        since = max(watch.exp_changed_at, watch.moved_at, autoplay_started_at)
        if suspended is None and now - since >= STUCK_SECONDS:
            alerts.append({"id": "stuck", "level": "warn",
                           "text": f"No XP or movement for {int((now - since) // 60)} min"})
    return alerts
