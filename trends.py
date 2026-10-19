"""Per-minute trend samples for the observer.

The client folds every stats packet, heal and death into one-minute buckets
kept for a day: experience, food, health, depletion, deaths and heals. The
dashboard draws the derived hourly rates, so a regression (dying every hour,
healing constantly, XP stalling, food running out) shows at a glance.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

BUCKET_SECONDS = 60
KEEP_SECONDS = 24 * 3600
# Points drawn per series: one per five minutes over the day.
MAX_POINTS = 288
# Last known values carried into each new minute.
LEVELS = ("exp", "level", "food", "hp", "depletion")
# Buckets kept: a day of minutes. Retention counts buckets rather than
# trusting the newest timestamp, so a clock that briefly jumps ahead cannot
# wipe the real history; the summary shows only the day before the poll.
MAX_BUCKETS = KEEP_SECONDS // BUCKET_SECONDS


class Trends:
    def __init__(self, buckets: Iterable[dict] | None = None):
        self.buckets: list[dict] = sorted(
            (dict(bucket) for bucket in (buckets or ()) if isinstance(bucket, dict) and
             isinstance(bucket.get("t"), (int, float))), key=lambda bucket: bucket["t"])

    def _bucket(self, now: float) -> tuple[dict, bool]:
        start = int(now // BUCKET_SECONDS * BUCKET_SECONDS)
        # A bucket after this minute came from a clock that ran ahead. Kept,
        # it would replay its stale deaths and heals once real time reached
        # it, so it is dropped and the buckets only ever grow at the end.
        while self.buckets and self.buckets[-1]["t"] > start:
            self.buckets.pop()
        if self.buckets and self.buckets[-1]["t"] == start:
            return self.buckets[-1], False
        previous = self.buckets[-1] if self.buckets else {}
        bucket = {"t": start, "deaths": 0, "heals": 0,
                  **{key: previous.get(key) for key in LEVELS}}
        self.buckets.append(bucket)
        del self.buckets[:-MAX_BUCKETS]
        return bucket, True

    def observe(self, state: Any, now: float | None = None) -> bool:
        """Fold the current stats in; True when a new minute started."""
        bucket, started = self._bucket(time.time() if now is None else now)
        stats = getattr(state, "stats", {}) or {}
        if "exp" in stats:
            bucket["exp"] = int(stats.get("exp") or 0)
        if "level" in stats:
            bucket["level"] = int(stats.get("level") or 0)
        if "food" in stats:
            bucket["food"] = int(stats.get("food") or 0)
        maxhp = int(stats.get("maxhp", 0) or 0)
        if maxhp > 0:
            bucket["hp"] = round(int(stats.get("hp", 0) or 0) / maxhp, 3)
        if getattr(state, "depletion_points_known", False):
            bucket["depletion"] = int(getattr(state, "depletion_points", 0) or 0)
        return started

    def count(self, key: str, now: float | None = None) -> bool:
        """Count one death or heal; True when a new minute started."""
        bucket, started = self._bucket(time.time() if now is None else now)
        bucket[key] = int(bucket.get(key, 0) or 0) + 1
        return started

    def summary(self, now: float | None = None) -> dict[str, Any]:
        """Hourly rates and levels over the day, downsampled for drawing."""
        now = time.time() if now is None else now
        buckets = [bucket for bucket in self.buckets
                   if now - KEEP_SECONDS <= bucket["t"] <= now]
        # The latest row is always the current minute, so the hourly rates
        # age against the poll time even when no new minute was recorded.
        current = int(now // BUCKET_SECONDS * BUCKET_SECONDS)
        if buckets and buckets[-1]["t"] < current:
            buckets.append({"t": current, "deaths": 0, "heals": 0,
                            **{key: buckets[-1].get(key) for key in LEVELS}})
        rows = []
        first_index = 0
        for index, bucket in enumerate(buckets):
            # The trailing hour by time: a gap (offline) leaves fewer buckets.
            while buckets[first_index]["t"] <= bucket["t"] - 3600:
                first_index += 1
            window = buckets[first_index:index + 1]
            first = next((b for b in window if b.get("exp") is not None), None)
            span = (bucket["t"] - first["t"]) / 3600 if first else 0
            xp_rate = (round((bucket["exp"] - first["exp"]) / span)
                       if first and bucket.get("exp") is not None and span >= 0.25 else None)
            rows.append({
                "t": bucket["t"],
                "xp_per_hour": xp_rate,
                "deaths_per_hour": sum(b.get("deaths", 0) for b in window),
                "heals_per_hour": sum(b.get("heals", 0) for b in window),
                "depletion": bucket.get("depletion"),
                "food": bucket.get("food"),
                "hp": bucket.get("hp"),
            })
        step = max(1, -(-len(rows) // MAX_POINTS))
        points = rows[::step]
        if rows and points[-1] is not rows[-1]:
            points.append(rows[-1])
        return {"bucket_seconds": BUCKET_SECONDS * step, "points": points}
