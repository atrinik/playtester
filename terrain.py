"""Terrain features of the squares she fights from, and her fight records.

The design is docs/LEARNING_AND_FEEDBACK.md 4.7, phase F10: each fight she
takes part in becomes one ``fight_positions`` row saying where she stood
(the square's terrain feature and how many squares a melee attacker could
reach her from), how many attackers hit her at once, the ranged monsters in
view, the damage she took and how it ended. Nothing here changes what she
does: the rows only feed the dashboard's Terrain panel and, later (F11),
tie-breakers behind a flag.

**Features** come from the authored walkability she plans with
(``MapNode.walkable``: terrain, static blockers and locked gates), over a
5x5 window around her square:

- ``reach``: how many of her 8 neighbours a walking monster could stand on.
  The Classic server lets a monster hit whatever is within one square in
  both axes on its own level (``can_hit``, server ``types/monster.c``)
  without checking walls, so a diagonal neighbour reaches her past a wall
  corner and all 8 count. A door counts as standable: monsters that open
  doors stand in the open doorway.
- ``corridor``: ``reach`` 2, and the two squares opposite each other.
- ``doorway``: a door on or next to her square, or a one-wide gap (walls
  on two opposite sides) with a room on one side.
- ``open``: ``reach`` 7 or 8 and at least 20 standable squares in the
  window.
- ``room``: anything else; ``unknown`` off the world graph.

**Cover** from a ranged or caster monster in view: a wall or door on the
straight line to it, through the cells of the server's projectile flight
(``tasks.ray_cells``, the ray ``bullet_ray_clear`` aims with).

The recorder is fed once an engine step (``tasks.observe_fight``) and never
raises into play: its work a step is the new server lines and, during a
fight, one pass over the creatures in view; a fight keeps at most
``MAX_HITS`` hits and lasts at most ``MAX_FIGHT_SECONDS``. Other players
are only ever counted: no name leaves the recorder, and the attacker names
it holds in memory for the pack count are dropped with the fight.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, NamedTuple, Sequence

from .self_improvement import (PACK_WINDOW_SECONDS, from_server, parse_hit,
                               peak_attackers)

FEATURES = ("corridor", "doorway", "room", "open")
UNKNOWN = "unknown"
OUTCOMES = ("won", "retreated", "recalled", "died")
NEIGHBOURS = tuple((dx, dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dx or dy)
WINDOW = 2  # squares each way: a 5x5 window
OPEN_REACH = 7
OPEN_WINDOW_SQUARES = 20
# A one-wide gap opens into a room when at least this many of the 10 squares
# in the two rows beyond it (5 wide) are standable.
ROOM_SIDE_SQUARES = 7
CELL_SIZE = 8  # ROUTING's cells: cell:{path}:{x//8},{y//8}

# A fight ends this long after the last contact (a hit, miss, block or kill
# either way), when she leaves the map, or when she dies.
QUIET_SECONDS = 10.0
# The longest fight kept as one row: a longer one is cut and the next
# contact starts another.
MAX_FIGHT_SECONDS = 900.0
# Hits a fight keeps for its pack count; later ones only add damage.
MAX_HITS = 2000
# Notes (death, emergency recall) waiting for the next step.
MAX_NOTES = 16

# Rates on the panel decay with this half-life (seconds).
HALF_LIFE_SECONDS = 7 * 24 * 3600.0

# Combat lines that show a fight is going on (server attack.c, skills):
# an attack on her or hers on a creature. "<who> hit you" is parse_hit.
_CONTACT = re.compile(
    r"^(?:.+? misses you\b|you (?:block|dodge|parry) |you hit .+? for \d|you killed )",
    re.I)
_KILL = re.compile(r"^you killed ", re.I)


class Terrain(NamedTuple):
    """A square's terrain feature and melee reach."""

    feature: str
    reach: int | None
    standable: int = 0  # standable squares in the window, hers included


def _standable(node: Any, x: int, y: int, tiled: bool) -> bool:
    if 0 <= x < node.width and 0 <= y < node.height:
        return bool(node.walkable(x, y))
    # Off the map: another map continues there only on a tiled map.
    return tiled


def features(node: Any, x: int, y: int) -> Terrain:
    """The terrain feature of world square (x, y) on ``node`` (a MapNode)."""
    if node is None:
        return Terrain(UNKNOWN, None)
    tiled = any(getattr(edge, "kind", "") == "tile" for edge in node.edges)
    ok = {(dx, dy): _standable(node, x + dx, y + dy, tiled)
          for dy in range(-WINDOW, WINDOW + 1) for dx in range(-WINDOW, WINDOW + 1)}
    open_neighbours = [offset for offset in NEIGHBOURS if ok[offset]]
    reach = len(open_neighbours)
    standable = sum(ok.values())
    if reach == 2:
        (ax, ay), (bx, by) = open_neighbours
        if (ax, ay) == (-bx, -by):
            return Terrain("corridor", reach, standable)
    doors = getattr(node, "doors", ())
    if any((x + dx, y + dy) in doors for dx, dy in ((0, 0), *NEIGHBOURS)):
        return Terrain("doorway", reach, standable)
    for wall, side in (((1, 0), (0, 1)), ((0, 1), (1, 0))):
        if ok[wall] or ok[(-wall[0], -wall[1])]:
            continue
        for sign in (1, -1):
            room = sum(ok[(side[0] * sign * depth + wall[0] * across,
                           side[1] * sign * depth + wall[1] * across)]
                       for depth in (1, 2) for across in range(-WINDOW, WINDOW + 1))
            if room >= ROOM_SIDE_SQUARES:
                return Terrain("doorway", reach, standable)
    if reach >= OPEN_REACH and standable >= OPEN_WINDOW_SQUARES:
        return Terrain("open", reach, standable)
    return Terrain("room", reach, standable)


def covered(node: Any, origin: tuple[int, int], target: tuple[int, int],
            ray: Callable[[int, int, int], Sequence[tuple[int, int]]]) -> bool:
    """Whether a wall or door stands on the straight line between world
    squares ``origin`` and ``target``: a cell of the projectile flight
    (``ray``, offsets from the origin) before the target's own."""
    if node is None:
        return False
    dx, dy = target[0] - origin[0], target[1] - origin[1]
    span = max(abs(dx), abs(dy))
    if span <= 1:
        return False
    blocked, doors = node.blocked, node.doors
    for cell in ray(dx, dy, span):
        if cell == (dx, dy) or max(abs(cell[0]), abs(cell[1])) > span:
            break
        world = (origin[0] + cell[0], origin[1] + cell[1])
        if world in blocked or world in doors:
            return True
    return False


@dataclass(slots=True)
class Hostile:
    """A creature in view, in world squares; never a player."""

    x: int
    y: int
    ranged: bool = False


@dataclass(slots=True)
class FightView:
    """What one engine step shows the recorder."""

    now: float
    character: str
    map: str
    x: int
    y: int
    node: Any = None
    # New server lines since the last step: (time, type, colour, text).
    messages: Sequence[tuple] = ()
    hostiles: Sequence[Hostile] = ()
    players: int = 0
    # Fills hostiles and players when a fight needs them, so a step without
    # a fight never looks at the creatures in view.
    scan: Callable[[], tuple[Sequence[Hostile], int]] | None = None


@dataclass(slots=True)
class Fight:
    character: str
    started: float
    map: str
    x: int
    y: int
    terrain: Terrain
    last_contact: float
    hits: list[tuple[float, str]] = field(default_factory=list)
    damage: int = 0
    kills: int = 0
    ranged: int = 0
    ranged_covered: int = 0
    hostiles: int = 0
    players: int = 0
    second_attacker: bool = False
    recalled: bool = False
    # Kept until the step after the snapshot: re-read from the view.
    pending_snapshot: bool = True

    def row(self, ended: float, outcome: str) -> dict[str, Any]:
        return {
            "character": self.character,
            "started": self.started, "ended": max(ended, self.started),
            "map": self.map, "x": self.x, "y": self.y,
            "cell": f"cell:{self.map}:{self.x // CELL_SIZE},{self.y // CELL_SIZE}",
            "feature": self.terrain.feature, "reach": self.terrain.reach,
            "peak_attackers": peak_attackers(self.hits),
            "ranged_attackers": self.ranged, "ranged_covered": self.ranged_covered,
            "hostiles_in_view": self.hostiles, "players_in_view": self.players,
            "kills": self.kills, "damage_taken": self.damage,
            "seconds": round(max(0.0, ended - self.started), 3),
            "outcome": outcome,
        }


class FightRecorder:
    """Turns her combat lines and position, step by step, into fight rows.

    ``observe`` returns the rows of fights that ended in that step; the
    caller stores them. ``note`` takes her own "death" and
    "emergency-recall" decisions as they are recorded.
    """

    def __init__(self, ray: Callable[[int, int, int], Sequence[tuple[int, int]]]
                 | None = None):
        self.ray = ray
        self.fight: Fight | None = None
        # The message position read up to (GameState.message_count).
        self.position: int | None = None
        self._notes: list[tuple[float, str]] = []

    @property
    def active(self) -> bool:
        return self.fight is not None

    def note(self, action: str, at: float) -> None:
        """A death or emergency recall of hers, applied at the next step."""
        if action in ("death", "emergency-recall") and len(self._notes) < MAX_NOTES:
            self._notes.append((float(at), action))

    def reset(self) -> None:
        """Forget the fight in progress (a new character, a reconnect)."""
        self.fight = None
        self._notes.clear()

    def observe(self, view: FightView) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        fight = self.fight
        if fight is not None and fight.character != view.character:
            self.reset()
        notes = sorted(self._notes)
        self._notes.clear()
        for entry in view.messages:
            if not from_server(entry):
                continue
            stamp, text = float(entry[0]), str(entry[-1]).strip()
            while notes and notes[0][0] <= stamp:
                rows.extend(self._apply_note(*notes.pop(0)))
            hit = parse_hit(text)
            contact = hit is not None or bool(_CONTACT.match(text))
            if not contact:
                continue
            if self.fight is None:
                self.fight = self._start(view, stamp)
            fight = self.fight
            fight.last_contact = max(fight.last_contact, stamp)
            if hit is not None:
                fight.damage += hit[1]
                if len(fight.hits) < MAX_HITS:
                    fight.hits.append((stamp, hit[0]))
                if not fight.second_attacker and self._second_attacker(fight, hit[0], stamp):
                    # The feature at the moment the second attacker arrived.
                    fight.second_attacker = True
                    if fight.map == view.map:
                        fight.x, fight.y = view.x, view.y
                        fight.terrain = features(view.node, view.x, view.y)
                        fight.pending_snapshot = True
            elif _KILL.match(text):
                fight.kills += 1
        for note in notes:
            rows.extend(self._apply_note(*note))
        fight = self.fight
        if fight is None:
            return rows
        if fight.map != view.map:
            rows.append(self._end(fight.last_contact,
                                  "recalled" if fight.recalled else "retreated"))
            return rows
        self._watch(fight, view)
        if view.now - fight.last_contact >= QUIET_SECONDS:
            rows.append(self._end(fight.last_contact, self._quiet_outcome(fight)))
        elif view.now - fight.started >= MAX_FIGHT_SECONDS:
            rows.append(self._end(view.now, self._quiet_outcome(fight)))
        return rows

    @staticmethod
    def _second_attacker(fight: Fight, who: str, stamp: float) -> bool:
        """Whether another attacker than ``who`` hit her in the 8 seconds
        before ``stamp``: the newest hits only, so a long fight against
        one creature costs a short scan a hit."""
        for at, other in reversed(fight.hits):
            if at < stamp - PACK_WINDOW_SECONDS:
                return False
            if other != who and at <= stamp:
                return True
        return False

    @staticmethod
    def _quiet_outcome(fight: Fight) -> str:
        if fight.recalled:
            return "recalled"
        return "won" if fight.kills else "retreated"

    def _start(self, view: FightView, stamp: float) -> Fight:
        return Fight(view.character, stamp, view.map, view.x, view.y,
                     features(view.node, view.x, view.y), stamp)

    def _apply_note(self, at: float, action: str) -> list[dict[str, Any]]:
        fight = self.fight
        if fight is None:
            return []
        if action == "death":
            return [self._end(at, "died")]
        fight.recalled = True
        return []

    def _end(self, ended: float, outcome: str) -> dict[str, Any]:
        fight, self.fight = self.fight, None
        assert fight is not None
        return fight.row(ended, outcome)

    def _watch(self, fight: Fight, view: FightView) -> None:
        """The creatures and players in view, and cover at the snapshot."""
        if view.scan is not None:
            view.hostiles, view.players = view.scan()
            view.scan = None
        ranged = [hostile for hostile in view.hostiles if hostile.ranged]
        fight.hostiles = max(fight.hostiles, len(view.hostiles))
        fight.ranged = max(fight.ranged, len(ranged))
        fight.players = max(fight.players, int(view.players))
        if fight.pending_snapshot:
            fight.pending_snapshot = False
            if self.ray is not None:
                fight.ranged_covered = sum(
                    covered(view.node, (fight.x, fight.y), (hostile.x, hostile.y), self.ray)
                    for hostile in ranged)


def rates(buckets: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Learned rates per (feature, peak attackers, ranged in view) from
    ``memory.fight_buckets``: fights, damage a second and the share that
    ended in death, each day's fights weighted by a 7-day half-life (at
    the middle of their day). Peak attackers are 0, 1, 2 or 3 for three
    or more."""
    groups: dict[tuple[str, int, bool], list[float]] = {}
    for bucket in buckets:
        try:
            weight = math.pow(0.5, (int(bucket["age_days"]) + 0.5) * 86400.0
                              / HALF_LIFE_SECONDS)
            key = (str(bucket["feature"]), int(bucket["attackers"]), bool(bucket["ranged"]))
            fights = int(bucket["fights"])
            seconds, damage = float(bucket["seconds"]), float(bucket["damage"])
            deaths = float(bucket["deaths"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not all(math.isfinite(value) for value in (weight, seconds, damage, deaths)):
            continue
        total = groups.setdefault(key, [0, 0.0, 0.0, 0.0, 0.0])
        total[0] += fights
        total[1] += weight * fights
        total[2] += weight * seconds
        total[3] += weight * damage
        total[4] += weight * deaths
    order = {name: index for index, name in enumerate((*FEATURES, UNKNOWN))}
    result = []
    for (feature, attackers, ranged), (count, weight, seconds, damage, deaths) in sorted(
            groups.items(), key=lambda item: (order.get(item[0][0], 99), item[0])):
        result.append({
            "feature": feature, "attackers": attackers, "ranged": ranged,
            "fights": int(count), "weight": round(weight, 3),
            "damage_per_second": round(damage / seconds, 2) if seconds > 0 else None,
            "death_rate": round(deaths / weight, 3) if weight > 0 else None,
        })
    return result
