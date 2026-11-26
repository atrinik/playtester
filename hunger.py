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
  for ``OBSERVE_SECONDS`` in which she moved and gained experience, with no
  starvation damage at food 0 and never a fall of it, while she is not
  hungry with no food on her. An unknown environment value is logged as an
  error and means ``auto``.

When it is obsolete every hunger behaviour is a no-op: no food trips, no
temple food, no eating, no food gates on resting or mana, no starving
alert, and carried food is junk (``tasks.obsolete_food``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

SETTINGS = ("on", "off", "auto")
# Old servers drained about 15 food a minute while she fought: half an hour
# unchanged while moving and fighting cannot happen there.
OBSERVE_SECONDS = 30 * 60.0
# A hit within this many seconds of a health loss makes it an attack's.
ATTACK_SECONDS = 3.0


@dataclass(slots=True)
class FoodWatch:
    """Her food stat over this run, for the ``auto`` setting.

    The window starts when food is first seen while playing on a real map,
    and starts again whenever food changes (a meal too) or, at food 0, she
    loses health to something other than an attack: the old server's
    starvation, whose food stays 0. ``moved`` and ``fought`` (experience
    gained) count only within the current window. ``drained`` is set for
    good the first time food falls; cursed food lowers it too, which only
    keeps the old rules.
    """

    since: float = 0.0
    food: int | None = None
    drained: bool = False
    exp: Any = None
    hp: Any = None
    position: tuple | None = None
    moved: bool = False
    fought: bool = False

    def _restart(self, now: float, food: int, exp: Any,
                 position: tuple) -> None:
        self.since, self.food = now, food
        self.exp, self.position = exp, position
        self.moved = self.fought = False

    def observe(self, state: Any, now: float | None = None) -> None:
        if getattr(state, "phase", "") != "playing":
            return
        m = getattr(state, "map", None)
        stats = getattr(state, "stats", {}) or {}
        if not getattr(m, "path", "") or "food" not in stats:
            return
        now = time.time() if now is None else now
        food = int(stats.get("food") or 0)
        exp, hp = stats.get("exp"), stats.get("hp")
        position = (m.path, getattr(m, "world_x", 0), getattr(m, "world_y", 0))
        lost_hp = hp is not None and self.hp is not None and hp < self.hp
        self.hp = hp
        if self.food is None:
            self._restart(now, food, exp, position)
            return
        if food != self.food:
            if food < self.food:
                self.drained = True
            self._restart(now, food, exp, position)
            return
        if food <= 0 and lost_hp and not _attacked(state, now):
            self._restart(now, food, exp, position)
            return
        if exp is not None and self.exp is not None and exp > self.exp:
            self.fought = True
        if self.exp is None:
            self.exp = exp
        if position != self.position:
            self.moved = True

    def obsolete(self, state: Any = None, now: float | None = None) -> bool:
        """Whether the window proves food obsolete. Never while she is
        hungry with no food on her (``state``): that is when the old server
        would starve her, so the old rules stay until she has eaten."""
        now = time.time() if now is None else now
        return (self.food is not None and not self.drained and
                self.moved and self.fought and
                now - self.since >= OBSERVE_SECONDS and
                not (state is not None and _hungry_without_food(state)))


def _attacked(state: Any, now: float) -> bool:
    """Whether combat messages show something hit her just now (message
    times are wall-clock, as ``now`` is)."""
    for timestamp, _, _, raw in reversed(getattr(state, "messages", ()) or ()):
        if timestamp < now - ATTACK_SECONDS:
            break
        text = raw.casefold()
        if " hit you" in text or " misses you" in text:
            return True
    return False


def _hungry_without_food(state: Any) -> bool:
    """Low on food with no paid food carried (navigation.hungry's test,
    without its identification rule)."""
    from . import constants as c
    from .alerts import HUNGRY_FOOD

    stats = getattr(state, "stats", {}) or {}
    if int(stats.get("food", 1000) or 0) >= HUNGRY_FOOD:
        return False
    return not any(item.item_type == c.TYPE_FOOD and
                   not item.flags & c.ITEM_UNPAID
                   for item in getattr(state, "inventory", ()) or ())


def parse_setting(value: str, default: str = "on") -> str:
    """A setting from its environment spelling: empty is ``default``; an
    unknown value is logged as an error and means ``auto``, which decides
    from what she observes rather than guessing either way."""
    value = (value or "").strip().casefold()
    if not value:
        return default
    if value in ("1", "true", "yes"):
        return "on"
    if value in ("0", "false", "no"):
        return "off"
    if value in SETTINGS:
        return value
    log.error("unknown ATRINIK_BOT_FOOD_OBSOLETE value %r; using auto", value)
    return "auto"


def food_obsolete(client: Any) -> bool:
    """Whether food never drains on her server (module doc). Takes her
    client or its state."""
    state = getattr(client, "state", client)
    setting = getattr(state, "food_obsolete", "off")
    if setting == "on":
        return True
    if setting == "auto":
        watch = getattr(state, "food_watch", None)
        return bool(watch is not None and watch.obsolete(state))
    return False
