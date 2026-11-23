"""Watching the owner play: phase W1, passive recording.

The design is docs/LEARNING_AND_FEEDBACK.md 4.14. Behind the owner's
``watch_owner`` flag (off by default), while the configured operator
character (``ClientConfig.operator_character``, from
``ATRINIK_BOT_OPERATOR_CHARACTER``) is visible on her map and she is not
fighting herself, each engagement of that character becomes one
``owner_observations`` row: where it stood (map, square, terrain feature),
which monster kinds it hit in order, the most monsters next to it at once,
the damage numbers on its square and its targets', whether it stepped
after a ranged hit, its health before the pull and after (only while it
shares her party), kills and duration. Nothing here changes what she does.

**What the Classic server shows her** (server ``socket/request.c``):
every visible living object on MAP2 with its name and name colour, and a
tile animation where a living object took damage this round (the damage
number, ``ANIM_DAMAGE``) or where something died (``ANIM_KILL``). Combat
text, the owner's target, stats and consumables go only to the owner.
Health and mana percent arrive only for party members
(``CMD_PARTY_UPDATE``, server ``party.c``); the client keeps the operator
character's alone (``GameState.operator_vitals``).

**Attribution** is by position: a damage number on the operator
character's square is a hit on it; one on a monster next to it is its hit
on that monster (``kinds``); a kill marker next to it is its kill. A hit
on its square with no monster next to it is a ranged hit.

**Privacy.** Only the exact configured character is watched: its MAP2
name, less the server's bracketed tags (" [AFK]", permission tags;
``generate_quick_name``), must equal the configured name and carry a
player's name colour. Other players are only ever positions: while one is
within ``SHARED_RADIUS`` squares of the operator character the engagement
is shared and is dropped whole, so no other player's actions are ever
recorded. No name is stored: a row belongs to her own character, and a
monster kind is stored only when it is an authored creature name.

The recorder is fed once an engine step (``observe_owner``), reads only
new tile animations and, while the operator character might be in view,
one pass over the living objects in view; it never raises into play.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from . import constants as c
from .observations import is_player, plain
from .terrain import CELL_SIZE, Terrain, features

log = logging.getLogger(__name__)

# An engagement ends this long after the last damage number or kill
# marker on or next to the operator character (seconds).
QUIET_SECONDS = 10.0
# The longest engagement kept as one row.
MAX_ENGAGEMENT_SECONDS = 900.0
# Another player this close (squares, either axis) to the operator
# character makes the engagement shared: it is dropped whole.
SHARED_RADIUS = 6
# A step within this long after a ranged hit counts as stepping away.
STEP_AFTER_RANGED_SECONDS = 3.0
# Monster kinds kept per engagement, in order of first hit.
MAX_KINDS = 12
# A failing recorder logs its traceback once, then a count at most this
# often (seconds).
ERROR_REPORT_SECONDS = 600.0

# The server colours creature names by their level against hers
# (request.c get_living_level_color; libatrinik COLOR_*).
LEVEL_BANDS = {"999999": "gray", "00ff00": "green", "0080ff": "blue",
               "ffff33": "yellow", "ff9900": "orange", "ff3030": "red",
               "cc66ff": "purple"}
# The bracketed tags the server appends to a player's map name.
_NAME_TAGS = re.compile(r"(?:\s+\[[^\[\]]*\])+$")


def map_player_name(name: str) -> str:
    """A player's MAP2 name without the server's trailing bracketed tags
    (permission tags and " [AFK]", server ``generate_quick_name``)."""
    return _NAME_TAGS.sub("", str(name)).strip()


def enabled(client: Any) -> bool:
    """The owner's ``watch_owner`` flag (default off), with an operator
    character configured."""
    config = getattr(client, "config", None)
    return bool(getattr(config, "watch_owner", False)) and \
        bool(str(getattr(config, "operator_character", "")).strip())


def chebyshev(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


@dataclass(slots=True)
class Creature:
    """A monster in view, in world squares; never a player."""

    x: int
    y: int
    kind: str  # its authored name, folded, or "" when not authored
    band: str  # its level band against her, from the name colour


@dataclass(slots=True)
class OwnerView:
    """What one engine step shows the recorder."""

    now: float
    character: str  # hers
    map: str
    node: Any = None
    # The operator character's world square, or None when not in view.
    owner: tuple[int, int] | None = None
    creatures: Sequence[Creature] = ()
    # Other players' squares: positions only, never names.
    others: Sequence[tuple[int, int]] = ()
    # New tile animations on her map at her depth: (time, x, y, kind, value).
    animations: Sequence[tuple[float, int, int, int, int]] = ()
    # The operator character's health percent, when it shares her party.
    hp: int | None = None
    # Whether she is fighting herself.
    busy: bool = False


@dataclass(slots=True)
class Engagement:
    character: str
    started: float
    map: str
    x: int
    y: int
    terrain: Terrain
    last_contact: float
    kinds: list[list[str]] = field(default_factory=list)
    peak_adjacent: int = 0
    damage_dealt: int = 0
    damage_taken: int = 0
    hits_dealt: int = 0
    hits_taken: int = 0
    ranged_hits: int = 0
    stepped_after_ranged: int = 0
    kills: int = 0
    hp_before: int | None = None
    hp_low: int | None = None
    hp_after: int | None = None
    gap_seconds: float | None = None
    shared: bool = False
    # A ranged hit waiting for a step: (time, square).
    ranged_at: tuple[float, tuple[int, int]] | None = None

    def row(self, ended: float) -> dict[str, Any]:
        return {
            "character": self.character,
            "started": self.started, "ended": max(ended, self.started),
            "map": self.map, "x": self.x, "y": self.y,
            "cell": f"cell:{self.map}:{self.x // CELL_SIZE},{self.y // CELL_SIZE}",
            "feature": self.terrain.feature, "reach": self.terrain.reach,
            "kinds_json": json.dumps(self.kinds, separators=(",", ":")),
            "peak_adjacent": self.peak_adjacent,
            "hits_dealt": self.hits_dealt, "damage_dealt": self.damage_dealt,
            "hits_taken": self.hits_taken, "damage_taken": self.damage_taken,
            "ranged_hits": self.ranged_hits,
            "stepped_after_ranged": self.stepped_after_ranged,
            "hp_before": self.hp_before, "hp_low": self.hp_low,
            "hp_after": self.hp_after,
            "gap_seconds": None if self.gap_seconds is None else round(self.gap_seconds, 3),
            "kills": self.kills,
            "seconds": round(max(0.0, ended - self.started), 3),
        }


class OwnerWatchRecorder:
    """Turns the operator character's visible fights into rows.

    ``observe`` returns the rows of engagements that ended in that step;
    the caller stores them.
    """

    def __init__(self) -> None:
        self.engagement: Engagement | None = None
        # The tile animation position read up to.
        self.position: int | None = None
        # When the operator character was last in view (wall clock), and
        # when its last kept or dropped engagement ended while it stayed in
        # view since: the rest before the next pull.
        self.seen_at: float | None = None
        self.last_end: float | None = None
        # Its health at the previous step: "before" the pull.
        self.last_hp: int | None = None
        self.shared_dropped = 0
        self.failures = 0
        self.failure_reported_at: float | None = None

    @property
    def active(self) -> bool:
        return self.engagement is not None

    def reset(self) -> None:
        self.engagement = None
        self.seen_at = self.last_end = self.last_hp = None

    def observe(self, view: OwnerView) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        engagement = self.engagement
        if engagement is not None and (engagement.character != view.character or
                                       engagement.map != view.map or view.busy):
            # Her own fight, or another frame of squares: never the owner's
            # alone. Dropped, not kept.
            self.engagement = None
            engagement = None
        if view.busy or view.owner is None:
            if engagement is not None:
                rows.extend(self._end(engagement.last_contact))
            self.seen_at = self.last_end = None
            self.last_hp = None
            return rows
        owner = view.owner
        self.seen_at = view.now
        shared = any(chebyshev(owner, other) <= SHARED_RADIUS for other in view.others)
        adjacent = [creature for creature in view.creatures
                    if chebyshev(owner, (creature.x, creature.y)) == 1]
        by_square = {(creature.x, creature.y): creature for creature in adjacent}
        for stamp, x, y, kind, value in sorted(view.animations):
            distance = chebyshev(owner, (x, y))
            if distance > 1:
                continue
            if kind == c.ANIM_DAMAGE and distance == 0:
                engagement = self._contact(view, stamp)
                engagement.hits_taken += 1
                engagement.damage_taken += max(0, int(value))
                if not adjacent:
                    engagement.ranged_hits += 1
                    engagement.ranged_at = (stamp, owner)
            elif kind == c.ANIM_DAMAGE and (x, y) in by_square:
                engagement = self._contact(view, stamp)
                engagement.hits_dealt += 1
                engagement.damage_dealt += max(0, int(value))
                creature = by_square[(x, y)]
                entry = [creature.kind or "unknown", creature.band]
                if entry not in engagement.kinds and len(engagement.kinds) < MAX_KINDS:
                    engagement.kinds.append(entry)
            elif kind == c.ANIM_KILL and distance == 1:
                engagement = self._contact(view, stamp)
                engagement.kills += 1
        engagement = self.engagement
        if engagement is None:
            if view.hp is not None:
                self.last_hp = view.hp
            return rows
        engagement.shared = engagement.shared or shared
        engagement.peak_adjacent = max(engagement.peak_adjacent, len(adjacent))
        if view.hp is not None:
            engagement.hp_low = view.hp if engagement.hp_low is None else \
                min(engagement.hp_low, view.hp)
            engagement.hp_after = view.hp
            self.last_hp = view.hp
        if engagement.ranged_at is not None:
            at, square = engagement.ranged_at
            if owner != square:
                engagement.stepped_after_ranged += 1
                engagement.ranged_at = None
            elif view.now - at > STEP_AFTER_RANGED_SECONDS:
                engagement.ranged_at = None
        if view.now - engagement.last_contact >= QUIET_SECONDS:
            rows.extend(self._end(engagement.last_contact))
        elif view.now - engagement.started >= MAX_ENGAGEMENT_SECONDS:
            rows.extend(self._end(view.now))
        return rows

    def _contact(self, view: OwnerView, stamp: float) -> Engagement:
        engagement = self.engagement
        if engagement is None:
            assert view.owner is not None
            x, y = view.owner
            engagement = self.engagement = Engagement(
                view.character, stamp, view.map, x, y, features(view.node, x, y), stamp,
                hp_before=self.last_hp,
                gap_seconds=None if self.last_end is None else max(0.0, stamp - self.last_end))
        engagement.last_contact = max(engagement.last_contact, stamp)
        return engagement

    def _end(self, ended: float) -> list[dict[str, Any]]:
        engagement, self.engagement = self.engagement, None
        if engagement is None:
            return []
        self.last_end = ended
        if engagement.shared:
            self.shared_dropped += 1
            return []
        return [engagement.row(ended)]


def owner_view(client: Any, recorder: OwnerWatchRecorder) -> OwnerView | None:
    """What this step shows the recorder, or None while she is not playing.
    Living objects are read only while the operator character might be in
    view; other players become positions."""
    state = client.state
    m = state.map
    if state.phase != "playing" or not m.path or not state.player_name:
        recorder.position = None
        return None
    count = state.tile_animation_count
    if recorder.position is None or recorder.position > count:
        recorder.position = count
    fresh = state.tile_animations_since(recorder.position)
    recorder.position = count
    graph = getattr(client, "world_graph", None)
    node = getattr(graph, "nodes", {}).get(m.path) if graph is not None else None
    fight = getattr(client, "fight_recorder", None)
    busy = bool(state.combat) or bool(getattr(fight, "active", False))
    operator = str(client.config.operator_character).strip()
    view = OwnerView(now=time.time(), character=state.player_name, map=m.path,
                     node=node, busy=busy)
    if busy or not operator or operator == state.player_name:
        return view
    content = getattr(client, "authored_content", None)
    names = getattr(content, "names", None)
    own = plain(state.player_name)
    cx, cy = m.width // 2, m.height // 2
    owner: list[tuple[int, int]] = []
    creatures: list[Creature] = []
    others: list[tuple[int, int]] = []
    for x, y, obj in m.targets():
        square = (m.world_x + x - cx, m.world_y + y - cy)
        if is_player(obj, own, content):
            if map_player_name(obj.name) == operator:
                owner.append(square)
            else:
                others.append(square)
            continue
        name = plain(obj.name)
        if obj.is_friend or not name or name == own:
            continue
        creatures.append(Creature(*square, name if names is not None and name in names else "",
                                  LEVEL_BANDS.get(str(obj.name_color).casefold(), "")))
    if len(owner) != 1:
        return view
    square = owner[0]
    if node is not None and not (0 <= square[0] < node.width and 0 <= square[1] < node.height):
        # On a map tiled to hers: another frame of squares and terrain.
        return view
    view.owner, view.creatures, view.others = square, creatures, others
    view.animations = [(a.at, a.x, a.y, a.kind, a.value) for a in fresh
                       if a.map == m.path and a.depth == 0]
    vitals = state.operator_vitals
    view.hp = None if vitals is None else int(vitals[0])
    return view


def store(client: Any, row: dict[str, Any]) -> bool:
    """Keep one row for her own character, if her memory is open; a locked
    database waits at most memory.OWNER_OBSERVATION_SAVE_TIMEOUT."""
    character = getattr(client, "_memory_character", "")
    if not character or character != row.get("character") or \
            character != client.state.player_name:
        return False
    try:
        client._memory_store().record_owner_observation(character, row)
    except sqlite3.Error as error:
        log.warning("owner observation not kept: %s", error)
        return False
    except Exception:
        log.exception("owner observation not kept")
        return False
    return True


def observe_owner(client: Any) -> int:
    """Feed one engine step to the owner watch and keep the engagements
    that ended; returns how many were kept. With the flag off nothing is
    read. Watching changes nothing she does and never raises into play."""
    recorder = getattr(client, "owner_watch", None)
    if recorder is None:
        return 0
    if not enabled(client):
        if recorder.active or recorder.position is not None:
            recorder.reset()
            recorder.position = None
        return 0
    try:
        view = owner_view(client, recorder)
        if view is None:
            recorder.reset()
            return 0
        kept = sum(bool(store(client, row)) for row in recorder.observe(view))
    except Exception:
        now = time.monotonic()
        failures = recorder.failures + 1
        if recorder.failure_reported_at is None:
            log.exception("owner watch failed")
            recorder.failure_reported_at, failures = now, 0
        elif now - recorder.failure_reported_at >= ERROR_REPORT_SECONDS:
            log.error("owner watch failed %d more times", failures)
            recorder.failure_reported_at, failures = now, 0
        recorder.failures = failures
        recorder.reset()
        return 0
    if recorder.failure_reported_at is not None:
        if recorder.failures:
            log.error("owner watch failed %d more times", recorder.failures)
        recorder.failure_reported_at, recorder.failures = None, 0
    return kept
