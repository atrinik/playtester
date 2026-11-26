"""Whether food still matters on the server she plays on.

Classic 5.119.0 (https://github.com/atrinik/classic/pull/655) removed
starvation: the food stat never drains, and food 0 has no effect
(server doc/HUNGER.md, ``player_do_some_living`` in types/player.c). The
wire protocol is unchanged, so the server reports nothing that tells the
two apart. ``food_obsolete`` decides it from one setting
(``ATRINIK_BOT_FOOD_OBSOLETE``, ``--food-obsolete``):

- ``on`` (the command line's default, for the live server): food never
  matters;
- ``off`` (the default of a constructed state): the hunger rules run as
  before;
- ``auto``: food is obsolete once ``FoodWatch`` has seen it stay unchanged
  for ``OBSERVE_SECONDS`` of play in which she moved and gained
  experience, and never once any fall of it has been seen.

When it is obsolete every hunger behaviour is a no-op: no food trips, no
temple food, no eating, no food gates on resting or mana, no starving
alert, and carried food is junk (``tasks.obsolete_food``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

SETTINGS = ("on", "off", "auto")
# Old servers drained about 15 food a minute while she fought: half an hour
# unchanged while moving and fighting cannot happen there.
OBSERVE_SECONDS = 30 * 60.0


@dataclass(slots=True)
class FoodWatch:
    """Her food stat over this run, for the ``auto`` setting.

    ``drained`` is set for good the first time food falls (cursed food
    lowers it too: that only keeps the old rules). ``since`` is when food
    was first seen while playing on a real map, with the experience and
    position then; ``moved`` and ``fought`` record a change of either since.
    """

    since: float = 0.0
    food: int | None = None
    drained: bool = False
    exp: Any = None
    position: tuple | None = None
    moved: bool = False
    fought: bool = False

    def observe(self, state: Any, now: float | None = None) -> None:
        if getattr(state, "phase", "") != "playing":
            return
        m = getattr(state, "map", None)
        stats = getattr(state, "stats", {}) or {}
        if not getattr(m, "path", "") or "food" not in stats:
            return
        now = time.time() if now is None else now
        food = int(stats.get("food") or 0)
        exp = stats.get("exp")
        position = (m.path, getattr(m, "world_x", 0), getattr(m, "world_y", 0))
        if self.food is None:
            self.since, self.food = now, food
            self.exp, self.position = exp, position
            return
        if food < self.food:
            self.drained = True
        self.food = food
        if exp is not None and self.exp is not None and exp > self.exp:
            self.fought = True
        if self.exp is None:
            self.exp = exp
        if position != self.position:
            self.moved = True

    def obsolete(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return (self.food is not None and not self.drained and
                self.moved and self.fought and
                now - self.since >= OBSERVE_SECONDS)


def parse_setting(value: str, default: str = "on") -> str:
    """A setting from its environment or command-line spelling."""
    value = (value or "").strip().casefold()
    if value in ("1", "true", "yes"):
        return "on"
    if value in ("0", "false", "no"):
        return "off"
    return value if value in SETTINGS else default


def food_obsolete(client: Any) -> bool:
    """Whether food never drains on her server (module doc). Takes her
    client or its state."""
    state = getattr(client, "state", client)
    setting = getattr(state, "food_obsolete", "off")
    if setting == "on":
        return True
    if setting == "auto":
        watch = getattr(state, "food_watch", None)
        return bool(watch is not None and watch.obsolete())
    return False
