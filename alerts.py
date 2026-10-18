"""Dashboard alerts: what needs the operator's attention right now.

Raised for repeated deaths, starvation, a character that is stuck while
autoplay runs, and a lost connection. The client keeps a ``ProgressWatch``
current; the dashboard serves the active alerts on every live poll.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

DEATH_WINDOW_SECONDS = 3600.0
REPEATED_DEATHS = 2
STUCK_SECONDS = 20 * 60.0
DISCONNECTED_SECONDS = 180.0
# Announcements of one death arrive this close together.
SAME_DEATH_SECONDS = 10.0


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
        exp = (getattr(state, "stats", {}) or {}).get("exp")
        if exp != self.exp:
            self.exp, self.exp_changed_at = exp, now
        m = getattr(state, "map", None)
        position = (getattr(m, "path", ""), getattr(m, "world_x", 0), getattr(m, "world_y", 0))
        if position != self.position:
            self.position, self.moved_at = position, now
        phase = getattr(state, "phase", "")
        if phase == "playing":
            self.disconnected_since = None
        elif phase == "disconnected" and self.disconnected_since is None:
            # Reconnect attempts pass through connecting/login phases; the
            # outage lasts until she is playing again.
            self.disconnected_since = now


def active(state: Any, decisions: Iterable[dict], watch: ProgressWatch, *,
           autoplay_running: bool, now: float | None = None) -> list[dict[str, str]]:
    """The alerts that apply right now, most severe first."""
    now = time.time() if now is None else now
    alerts: list[dict[str, str]] = []
    if (watch.disconnected_since is not None and
            now - watch.disconnected_since >= DISCONNECTED_SECONDS):
        minutes = int((now - watch.disconnected_since) // 60)
        alerts.append({"id": "disconnected", "level": "critical",
                       "text": f"Disconnected for {minutes} min"})
    stamps = sorted(
        float(record.get("time", 0.0) or 0.0) for record in decisions
        if isinstance(record, dict) and record.get("action") == "death" and
        now - float(record.get("time", 0.0) or 0.0) <= DEATH_WINDOW_SECONDS)
    # "defeated in combat" and "YOU HAVE DIED." announce one death.
    deaths = sum(1 for index, stamp in enumerate(stamps)
                 if index == 0 or stamp - stamps[index - 1] >= SAME_DEATH_SECONDS)
    if deaths >= REPEATED_DEATHS:
        alerts.append({"id": "deaths", "level": "critical",
                       "text": f"{deaths} deaths in the last hour"})
    stats = getattr(state, "stats", {}) or {}
    if (getattr(state, "phase", "") == "playing" and "food" in stats and
            int(stats.get("food") or 0) <= 0):
        alerts.append({"id": "starving", "level": "critical",
                       "text": "Starving: food is 0"})
    if (autoplay_running and getattr(state, "phase", "") == "playing" and
            now - watch.exp_changed_at >= STUCK_SECONDS and
            now - watch.moved_at >= STUCK_SECONDS):
        minutes = int((now - max(watch.exp_changed_at, watch.moved_at)) // 60)
        alerts.append({"id": "stuck", "level": "warn",
                       "text": f"No XP or movement for {minutes} min"})
    return alerts
