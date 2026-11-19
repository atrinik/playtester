"""Her route events: what crossing the world cost her (docs/ROUTING.md 4.4, P5).

While a ``NavigateTask`` is under way, the recorder keeps what happens on
the way, one signal at a time, in her memory database:

- ``death``: a death in transit, against the map, the edge she was walking
  toward, and the 8x8 cell she fell in when that square is within
  ``NEAR_PATH_TILES`` of her planned local path (``on_death_decision`` and
  ``on_post_mortem``). A death to another player records nothing, and
  neither does a PvP-area defeat;
- ``recall``: an emergency recall or disengage in transit, against the map
  and, near the path, the cell;
- ``damage``: the hits she took on a map she crossed, as a share of her
  maximum HP, against the map and the cells near the path (hits from
  players are not counted);
- ``blocked``: an exit held by a pack until she gave it up, in seconds;
- ``crossing_failed``: a seam with no crossing left, a rejected seam
  departure, an exit or seam the click path could not reach;
- ``traversal``: an edge she crossed and the map she crossed to reach it,
  with the seconds she spent on that map, aggregated per subject; an exit or
  seam crossed is also a ``passed`` requirement outcome.

Subjects are ROUTING 4.2's identities, the same strings as route_blocks.py
uses for owner blocks (``map:``, ``exit:``, ``seam:``), plus ``cell:`` for a
corner of a map, so P6 can join blocks and events.

Recording never disturbs play. Every hook catches its own errors, writes wait
at most ``WRITE_TIMEOUT`` for a locked database, a failed write keeps the
rows in memory (bounded, deaths kept first) for the next attempt, and the
work per tick is a few comparisons; the database is written at most every
``FLUSH_SECONDS``, except for a death, which is written at once.

History from before P5 is migrated once, in the first write, under a
``metadata`` marker: #94's route deaths in ``farm_adaptations``
``kit_failures`` and the ``route_hazards`` that no such death explains
become ``death`` events, and the decision ledger's map visits seed
map-level traversal counts (the cold start of 4.4). Only history from
before this process first loaded her counts: everything since comes from
the live recorder, so no death is counted twice.

The learned cost model (route_costs.py, P6) reads these rows. The weights
and half-lives below are 4.4's proposed defaults, kept in one place for it;
the Learned routes summary uses them for a provisional cost, without the kit
rule or recovery, which route_costs.py applies.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .route_blocks import edge_subject, map_subject
from .self_improvement import from_server

log = logging.getLogger(__name__)

DEATH, RECALL, DAMAGE, BLOCKED, CROSSING_FAILED = (
    "death", "recall", "damage", "blocked", "crossing_failed")
SIGNALS = (DEATH, RECALL, DAMAGE, BLOCKED, CROSSING_FAILED)
TRAVERSAL = "traversal"
# Rows kept per character and signal: a flood of one signal never evicts
# another's rows (4.4).
CAPS = {DEATH: 1000, RECALL: 1000, DAMAGE: 5000, BLOCKED: 2000, CROSSING_FAILED: 2000}
# 4.4's proposed weights (expected seconds per unit of magnitude) and
# half-lives (seconds). One expected death is 1800 s (owner decision).
WEIGHTS = {DEATH: 1800.0, RECALL: 900.0, DAMAGE: 300.0, BLOCKED: 1.0,
           CROSSING_FAILED: 120.0}
HALF_LIVES = {DEATH: 6 * 3600.0, RECALL: 6 * 3600.0, DAMAGE: 6 * 3600.0,
              BLOCKED: 1800.0, CROSSING_FAILED: 1800.0}
TRAVERSAL_HALF_LIFE = 24 * 3600.0
# A safe pass (a crossing with no damage or recall on the way) multiplies
# the subject's earlier rows of these signals by this (4.4). Deaths never
# recover: they decay with time only, so 4.5's death-rate bounds hold.
RECOVERY_FACTOR = 0.7
RECOVERING_SIGNALS = (RECALL, DAMAGE, BLOCKED)
# A contribution below this many seconds is dropped.
MIN_CONTRIBUTION = 1.0
# Event rows older than this are deleted; traversal rows idle this long too.
EVENT_MAX_AGE = 7 * 24 * 3600.0
TRAVERSAL_MAX_IDLE = 30 * 24 * 3600.0
# How long a requirement outcome lasts (4.3, 4.4).
REJECTED_SECONDS = 24 * 3600.0
PASSED_SECONDS = 30 * 24 * 3600.0
PASSED, REJECTED = "passed", "rejected"
EVIDENCE_LIMIT = 200
# Cells are 8x8 tiles; a death, recall or hit counts against its cell only
# within this many tiles of the planned local path (4.4).
CELL_TILES = 8
NEAR_PATH_TILES = 3
# The longest a write waits for a locked database (seconds). A death's
# write uses the post-mortem's own limit (client.POST_MORTEM_SAVE_TIMEOUT).
WRITE_TIMEOUT = 0.25
DEATH_WRITE_TIMEOUT = 0.25
# Pending rows are written at most this often; a failed write is retried
# no sooner than this.
FLUSH_SECONDS = 5.0
RETRY_SECONDS = 5.0
# The most rows held for a later write; past it the oldest go first,
# deaths last.
PENDING_LIMIT = 500
# A trip whose task has not ticked for this long is not under way (an
# abandoned task object, a reconnect). NavigateThenTask keeps its trip
# fresh while a fight or a recovery hold stops its navigation (note_hold).
TRANSIT_STALE_SECONDS = 120.0
# A crossing counts only when she arrives within this many tiles of the
# edge's arrival square (an exit) or of the border it crosses to (a seam).
ARRIVAL_TILES = 2
# One recall a map within this long: a disengage repeats while she flees.
RECALL_REPEAT_SECONDS = 60.0
# A damage magnitude is HP lost over maximum HP, at most this.
MAX_DAMAGE_MAGNITUDE = 10.0
# Cells kept per map visit for damage, and squares per cell.
MAX_DAMAGE_CELLS = 64
MAX_CELL_SQUARES = 8
# The migration (4.4): #94's hazard lasts this long after its learn time,
# and a route_hazards entry is the same death as a migrated kit failure
# whose time is at most this much before its learn time.
ROUTE_HAZARD_SECONDS = 6 * 3600.0
MIGRATION_MATCH_SECONDS = 600.0
# Shadow planning rows (route_shadow.py, P6) kept per character, and for
# how long.
SHADOW_ROW_CAP = 10000
SHADOW_MAX_AGE = 7 * 24 * 3600.0
# What the dashboard lists, and how often its summary is re-read (seconds).
LEARNED_LIMIT = 25
LEARNED_REFRESH_SECONDS = 30.0
# Survivability fields kept with a danger row: the ones #94's kit predicate
# compares (CombatProfile.no_survivability_gain). No item names.
KIT_FIELDS = ("armour_class", "character_level", "max_health", "protections")
# kill_player's PvP-area notice (client.DEATH_NOTICES): a defeat by a player.
PVP_DEFEAT = "you have been defeated in combat!"

_HIT = re.compile(r"^(?P<who>.+?) hit you for (?P<amount>\d{1,9})(?!\d)", re.I)
_HELD = re.compile(r"\bheld=(?P<seconds>\d{1,7}(?:\.\d+)?)s\b")
_ARTICLE = re.compile(r"^(?:the|an?) ", re.I)
# Decisions that are a route signal while a trip is under way.
DECISION_SIGNALS = {
    "navigation-pack-exit-bypass": BLOCKED,
    "navigation-seam-excluded": CROSSING_FAILED,
    "navigation-seam-retry": CROSSING_FAILED,
    "navigation-click-failed": CROSSING_FAILED,
    # A dialogue voyage (ROUTING P4) whose NPC did not answer or whose walk
    # beside it stalled: against its talk: edge, like a failed click.
    "navigation-voyage-failed": CROSSING_FAILED,
    "emergency-recall": RECALL,
    "emergency-disengage": RECALL,
}


CELL = "cell"


def cell_subject(path: str, x: int, y: int) -> str:
    return f"{CELL}:{path}:{x // CELL_TILES},{y // CELL_TILES}"


def subject_kind(subject: str) -> str:
    return subject.split(":", 1)[0]


def departure(graph: Any, edge: Any) -> tuple[int, int]:
    """The square she leaves ``edge``'s source map from."""
    if getattr(edge, "kind", "exit") != "tile":
        return edge.x, edge.y
    node = getattr(graph, "nodes", {}).get(edge.source)
    if node is None:
        return edge.x, edge.y
    return (max(0, min(node.width - 1, edge.x)), max(0, min(node.height - 1, edge.y)))


def arrived_by(graph: Any, edge: Any, square: tuple[int, int]) -> bool:
    """Whether arriving at ``square`` on ``edge``'s destination map is that
    edge's crossing, not a recall, a respawn or another way in."""
    nodes = getattr(graph, "nodes", {})
    destination = nodes.get(edge.destination)
    x, y = square
    if getattr(edge, "kind", "exit") != "tile":
        ax, ay = edge.destination_x, edge.destination_y
        if destination is not None:
            ax = ax if ax >= 0 else destination.enter_x
            ay = ay if ay >= 0 else destination.enter_y
        return max(abs(x - ax), abs(y - ay)) <= ARRIVAL_TILES
    source = nodes.get(edge.source)
    if source is None or destination is None:
        return False
    sides = []
    if edge.x < 0:
        sides.append(x >= destination.width - 1 - ARRIVAL_TILES)
    elif edge.x >= source.width:
        sides.append(x <= ARRIVAL_TILES)
    if edge.y < 0:
        sides.append(y >= destination.height - 1 - ARRIVAL_TILES)
    elif edge.y >= source.height:
        sides.append(y <= ARRIVAL_TILES)
    return bool(sides) and all(sides)


def pvp_map(graph: Any, path: str) -> bool:
    """Whether players may attack each other on ``path`` (its header)."""
    node = getattr(graph, "nodes", {}).get(path)
    return bool(getattr(node, "pvp", False))


def near(square: tuple[int, int], path: Iterable[tuple[int, int]]) -> bool:
    return any(max(abs(square[0] - x), abs(square[1] - y)) <= NEAR_PATH_TILES
               for x, y in path)


def kit_text(client: Any) -> str:
    """Her survivability now (KIT_FIELDS), or '' when not known."""
    profile = getattr(getattr(client, "farm_analyzer", None), "profile", None)
    describe = getattr(profile, "describe", None)
    if not callable(describe):
        return ""
    return kit_from_description(describe())


def kit_from_description(described: Any) -> str:
    if not isinstance(described, dict):
        return ""
    kit = {key: described[key] for key in KIT_FIELDS if key in described}
    if not kit:
        return ""
    try:
        return json.dumps(kit, sort_keys=True, separators=(",", ":"))[:500]
    except (TypeError, ValueError):
        return ""


def player_attacker(client: Any, who: str) -> bool:
    """Whether the attacker named ``who`` is another player, as far as she
    knows: a name seen on a player (observations.py) and not an authored
    creature name."""
    observations = getattr(client, "observations", None)
    if observations is None:
        return False
    from .observations import plain

    name = plain(who)
    bare = _ARTICLE.sub("", name)
    names = getattr(observations, "player_names", {}) or {}
    try:
        in_view = observations._players_in_view(client.state)
    except Exception:
        in_view = set()
    players = set(names) | set(in_view)
    if not ({name, bare} & players):
        return False
    content = getattr(client, "authored_content", None)
    authored = getattr(content, "names", None) or ()
    return not ({name, bare} & set(authored))


@dataclass(slots=True)
class _Transit:
    """The trip under way, as the recorder last saw it."""

    task: Any
    map: str
    arrival: tuple[int, int]
    arrived_wall: float
    # Whether she was seen arriving on this map (so its time is known).
    timed: bool
    ticked: float
    edge: Any = None
    damage: float = 0.0
    damage_cells: dict[str, list] = field(default_factory=dict)
    recalled_at: float = float("-inf")
    path_key: tuple | None = None
    path: list = field(default_factory=list)
    # A death on this map: nothing more is attributed to the trip here, and
    # her respawn is no crossing.
    lost: bool = False
    # The first square seen on a map she has not been ticked on yet
    # (path, square, wall time), sampled on every NavigateThenTask tick and
    # forgotten once she is seen back on this map.
    landing: tuple | None = None

    def enter(self, path: str, arrival: tuple[int, int], wall: float) -> None:
        self.map, self.arrival, self.arrived_wall = path, arrival, wall
        self.lost = False
        self.landing = None
        self.timed = True
        self.edge = None
        self.damage = 0.0
        self.damage_cells = {}
        self.recalled_at = float("-inf")
        self.path_key, self.path = None, []


@dataclass(slots=True)
class _Snapshot:
    """The trip at a death notice."""

    map: str
    arrival: tuple[int, int]
    edge: Any
    path: list


class RouteRecorder:
    """Her route events, kept in memory until written (one per client)."""

    def __init__(self, *, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic):
        self.clock, self.monotonic = clock, monotonic
        self.character = ""
        # When this process first loaded her: the migration takes only
        # history from before it.
        self.installed_at = 0.0
        self.migrated = False
        self.events: list[dict[str, Any]] = []
        # subject -> [count, timed count, timed seconds, last at]
        self.traversals: dict[str, list[float]] = {}
        self.requirements: dict[str, dict[str, Any]] = {}
        # Safe passes not yet written: (subject, since), RECOVERY_FACTOR each.
        self.recoveries: list[tuple[str, float]] = []
        self.transit: _Transit | None = None
        self._death: tuple[dict, _Snapshot | None] | None = None
        self._next_flush = float("-inf")
        self._retry_at = float("-inf")
        self._warned = False
        self._learned: tuple[str, float, dict] | None = None
        self.written = 0
        self.dropped = 0

    # Her character.

    def load(self, client: Any, character: str) -> None:
        """She logs in as ``character``: rows pending for another are tried
        once more, then dropped."""
        if character == self.character:
            return
        if self.character and self._pending():
            self.flush(client, force=True)
            if self._pending():
                log.warning("dropping %s unwritten route events of %s",
                            self._pending(), self.character)
        self.character = character
        self.installed_at = self.clock()
        self.migrated = False
        self.events, self.traversals, self.requirements = [], {}, {}
        self.recoveries = []
        self.transit, self._death, self._learned = None, None, None

    def _pending(self) -> int:
        return (len(self.events) + len(self.traversals) + len(self.requirements) +
                len(self.recoveries))

    # The trip under way.

    def active(self, client: Any) -> _Transit | None:
        """The trip under way, or None."""
        transit = self.transit
        if transit is None:
            return None
        status = getattr(getattr(transit.task, "status", None), "value", None)
        if status != "running" or self.monotonic() - transit.ticked > TRANSIT_STALE_SECONDS:
            return None
        if client.state.map.path != transit.map or transit.lost:
            return None
        # Arrived: on the destination map with no edge left to walk.
        if transit.edge is None and transit.map == getattr(transit.task, "destination", None):
            return None
        return transit

    def tick(self, client: Any, task: Any) -> None:
        m = client.state.map
        path, square = m.path, (m.world_x, m.world_y)
        wall, now = self.clock(), self.monotonic()
        transit = self.transit
        if transit is None or transit.task is not task:
            transit = self.transit = _Transit(task, path, square, wall, False, now)
        elif path == transit.map:
            # Back where the trip was (a chase across a seam and back): an
            # earlier landing elsewhere is no longer this crossing's.
            transit.landing = None
        else:
            # Where she first stood on this map, if a hold saw it before
            # this tick: she may have walked on since.
            landing = transit.landing
            if landing is not None and landing[0] == path:
                square, wall = landing[1], landing[2]
            edge = transit.edge
            if (edge is not None and not transit.lost and
                    edge.source == transit.map and edge.destination == path and
                    now - transit.ticked <= TRANSIT_STALE_SECONDS
                    and arrived_by(getattr(task, "graph", None), edge, square)):
                self._traversal(client, transit, edge, wall)
            transit.enter(path, square, wall)
        transit.ticked = now
        route = getattr(task, "route", None) or ()
        transit.edge = None if transit.lost else next(
            (edge for edge in route[:2] if edge.source == path), None)
        self.flush(client)

    def hold(self, client: Any, task: Any) -> None:
        """``task``'s trip is held (a fight, a rest, looting), not abandoned;
        the first square she is seen on after a map change is kept."""
        transit = self.transit
        if transit is None or transit.task is not task:
            return
        transit.ticked = self.monotonic()
        m = client.state.map
        if m.path == transit.map:
            transit.landing = None
        elif transit.landing is None or transit.landing[0] != m.path:
            transit.landing = (m.path, (m.world_x, m.world_y), self.clock())

    def _lose(self, transit: _Transit) -> None:
        """A death: the trip is no longer where it was walking."""
        transit.lost = True
        transit.edge = None
        transit.damage, transit.damage_cells = 0.0, {}

    def note_edge(self, task: Any, edge: Any) -> None:
        """The edge ``task`` walks toward now (before a decision about it)."""
        transit = self.transit
        if transit is not None and transit.task is task and edge is not None and \
                edge.source == transit.map:
            transit.edge = edge

    def _planned_path(self, client: Any, transit: _Transit) -> list:
        """Her planned local path on this map: from where she arrived to the
        edge she walks toward, or to the destination square. Empty when not
        known."""
        task = transit.task
        graph = getattr(task, "graph", None)
        if transit.edge is not None:
            goal = departure(graph, transit.edge)
        elif transit.map == getattr(task, "destination", None) and \
                getattr(task, "destination_xy", None) is not None:
            goal = tuple(task.destination_xy)
        else:
            return []
        key = (transit.map, transit.arrival, goal)
        if transit.path_key != key:
            allow = getattr(task, "_allow_locked", None)
            access = allow(client) if callable(allow) else False
            transit.path = list(graph.local_path(transit.map, transit.arrival, goal,
                                                 allow_locked=access) or ())
            transit.path_key = key
        return transit.path

    def _near_path(self, client: Any, transit: _Transit,
                   squares: Iterable[tuple[int, int]]) -> bool:
        try:
            path = self._planned_path(client, transit)
        except Exception:
            log.exception("planned path for route events failed")
            return False
        return bool(path) and any(near(square, path) for square in squares)

    # Signals.

    def _event(self, client: Any, signal: str, subject: str, magnitude: float,
               at: float | None = None) -> None:
        if not (magnitude > 0 and math.isfinite(magnitude)):
            return
        row = {"at": self.clock() if at is None else at, "subject": subject,
               "signal": signal, "magnitude": float(magnitude),
               "kit": kit_text(client) if signal in (DEATH, RECALL, DAMAGE) else "",
               "content": str(getattr(client, "content_inputs", "") or "")[:200]}
        self.events.append(row)
        if len(self.events) > PENDING_LIMIT:
            # The oldest go first, deaths last.
            index = next((i for i, row in enumerate(self.events) if row["signal"] != DEATH), 0)
            del self.events[index]
            self.dropped += 1

    @staticmethod
    def _count(client: Any, signal: str) -> None:
        """One occurrence of ``signal`` (however many subject rows it has)."""
        metrics = getattr(client, "metrics", None)
        if metrics is not None and callable(getattr(metrics, "route_event", None)):
            metrics.route_event(signal)

    def _traversal(self, client: Any, transit: _Transit, edge: Any, wall: float) -> None:
        seconds = wall - transit.arrived_wall if transit.timed else None
        for subject in (edge_subject(edge), map_subject(transit.map)):
            entry = self.traversals.setdefault(subject, [0, 0, 0.0, wall])
            entry[0] += 1
            if seconds is not None and seconds >= 0 and math.isfinite(seconds):
                entry[1] += 1
                entry[2] += seconds
            entry[3] = wall
        subject = edge_subject(edge)
        self.requirements[subject] = {"outcome": PASSED, "at": wall,
                                      "until": wall + PASSED_SECONDS,
                                      "inputs": "", "evidence": ""}
        if transit.timed and transit.damage <= 0 and transit.recalled_at == float("-inf"):
            # A safe pass: her earlier recall, damage and blocked rows on
            # the edge and the map recover (4.4); this visit's never do.
            for safe in (subject, map_subject(transit.map)):
                self.recoveries.append((safe, transit.arrived_wall))
            del self.recoveries[:-PENDING_LIMIT]
        if transit.damage > 0:
            self._damage(client, transit)

    def _damage(self, client: Any, transit: _Transit) -> None:
        maxhp = float(client.state.stats.get("maxhp", 0) or 0)
        if maxhp <= 0:
            return
        self._count(client, DAMAGE)
        self._event(client, DAMAGE, map_subject(transit.map),
                    min(MAX_DAMAGE_MAGNITUDE, transit.damage / maxhp))
        for subject, (amount, squares) in transit.damage_cells.items():
            if self._near_path(client, transit, squares):
                self._event(client, DAMAGE, subject,
                            min(MAX_DAMAGE_MAGNITUDE, amount / maxhp))
        transit.damage, transit.damage_cells = 0.0, {}

    def on_message(self, client: Any, msg_type: int, text: str) -> None:
        """A server line: a hit she takes on the way counts as damage."""
        if " hit you for " not in text:
            return
        transit = self.active(client)
        if transit is None or not from_server((0, msg_type, "", text)):
            return
        match = _HIT.match(text.strip())
        if match is None or player_attacker(client, match["who"].strip()):
            return
        # Where players may fight, an unseen one or their spell can hit her
        # under any name: no damage counts there.
        if pvp_map(getattr(transit.task, "graph", None), transit.map):
            return
        amount = int(match["amount"])
        if amount <= 0:
            return
        m = client.state.map
        transit.damage += amount
        subject = cell_subject(m.path, m.world_x, m.world_y)
        cell = transit.damage_cells.get(subject)
        if cell is None:
            if len(transit.damage_cells) >= MAX_DAMAGE_CELLS:
                return
            cell = transit.damage_cells[subject] = [0.0, set()]
        cell[0] += amount
        if len(cell[1]) < MAX_CELL_SQUARES:
            cell[1].add((m.world_x, m.world_y))

    def on_decision(self, client: Any, record: dict) -> None:
        """A recorded action or decision (client.record_action)."""
        action = record.get("action")
        if action == "death":
            self.on_death_decision(client, record)
            return
        signal = DECISION_SIGNALS.get(action)
        if signal is None:
            return
        transit = self.active(client)
        if transit is None:
            return
        if signal == RECALL:
            now = self.clock()
            if now - transit.recalled_at < RECALL_REPEAT_SECONDS:
                return
            transit.recalled_at = now
            self._count(client, RECALL)
            self._event(client, RECALL, map_subject(transit.map), 1.0)
            square = (int(record.get("x", 0) or 0), int(record.get("y", 0) or 0))
            if self._near_path(client, transit, [square]):
                self._event(client, RECALL, cell_subject(transit.map, *square), 1.0)
        elif transit.edge is not None:
            if signal == BLOCKED:
                held = _HELD.search(str(record.get("detail", "")))
                magnitude = float(held["seconds"]) if held else 1.0
            else:
                magnitude = 1.0
            self._count(client, signal)
            self._event(client, signal, edge_subject(transit.edge), magnitude)
        self.flush(client)

    def on_death_decision(self, client: Any, record: dict) -> None:
        """The death notice's decision: the trip as it stands now, kept for
        the post-mortem that follows it."""
        transit = self.active(client)
        if str(record.get("detail", "")).strip().casefold() == PVP_DEFEAT:
            self._death = (record, None)
            if transit is not None:
                self._lose(transit)
            return
        snapshot = None
        if transit is not None:
            try:
                path = self._planned_path(client, transit)
            except Exception:
                log.exception("planned path for route events failed")
                path = []
            snapshot = _Snapshot(transit.map, transit.arrival, transit.edge, list(path))
            # Her respawn is no crossing.
            self._lose(transit)
        self._death = (record, snapshot)

    def on_post_mortem(self, client: Any, report: dict | None) -> None:
        """The death's post-mortem: a death in transit not caused by another
        player becomes ``death`` events, written at once."""
        pending, self._death = self._death, None
        if pending is None or report is None:
            return
        record, snapshot = pending
        if snapshot is None:
            return
        if any(player_attacker(client, who) for who in (report.get("attackers") or {})):
            return
        path = str(report.get("map") or "")
        if path != snapshot.map:
            return
        at = float(report.get("time") or self.clock())
        self._count(client, DEATH)
        self._event(client, DEATH, map_subject(path), 1.0, at=at)
        if snapshot.edge is not None:
            self._event(client, DEATH, edge_subject(snapshot.edge), 1.0, at=at)
        square = (int(report.get("x", 0) or 0), int(report.get("y", 0) or 0))
        if snapshot.path and near(square, snapshot.path):
            self._event(client, DEATH, cell_subject(path, *square), 1.0, at=at)
        self.flush(client, force=True, timeout=DEATH_WRITE_TIMEOUT)

    def note_requirement(self, client: Any, subject: str, outcome: str, *,
                         inputs: str = "", evidence: str = "") -> None:
        """A requirement outcome for one edge (4.3): ``rejected`` needs the
        server line that refused her as ``evidence``."""
        if outcome not in (PASSED, REJECTED) or (outcome == REJECTED and not evidence):
            return
        now = self.clock()
        self.requirements[subject] = {
            "outcome": outcome, "at": now,
            "until": now + (PASSED_SECONDS if outcome == PASSED else REJECTED_SECONDS),
            "inputs": str(inputs)[:200], "evidence": str(evidence)[:EVIDENCE_LIMIT]}

    # Writing.

    def migration(self, client: Any) -> "Migration":
        state = client.state
        return build_migration(
            getattr(state, "farm_adaptations", {}) or {},
            getattr(state, "route_hazards", {}) or {},
            getattr(client, "decision_history", ()) or (),
            before=self.installed_at,
            content=str(getattr(client, "content_inputs", "") or "")[:200])

    def flush(self, client: Any, *, force: bool = False,
              timeout: float = WRITE_TIMEOUT) -> bool:
        """Write what is pending; whether it was written (or nothing was)."""
        if not self.character or (self.migrated and not self._pending()):
            return True
        now = self.monotonic()
        if not force and (now < self._next_flush or now < self._retry_at):
            return False
        self._next_flush = now + FLUSH_SECONDS
        store = getattr(client, "_memory_store", None)
        if not callable(store):
            return False
        traversals = [{"subject": subject, "count": int(count), "timed": int(timed),
                       "mean_seconds": (total / timed) if timed else 0.0, "last_at": last}
                      for subject, (count, timed, total, last) in self.traversals.items()]
        try:
            migration = None if self.migrated else self.migration(client)
            store().record_route_events(
                self.character, events=self.events, traversals=traversals,
                requirements=self.requirements, migration=migration,
                now=self.clock(), recoveries=list(self.recoveries), timeout=timeout)
        except (sqlite3.Error, OSError, ValueError) as error:
            self._retry_at = now + RETRY_SECONDS
            if not self._warned:
                log.warning("route events not written yet (retrying): %s", error)
                self._warned = True
            return False
        self.written += len(self.events)
        self.migrated = True
        self._warned = False
        self.events, self.traversals, self.requirements = [], {}, {}
        self.recoveries = []
        self._learned = None
        return True

    # The dashboard.

    def learned(self, client: Any, *, now: float | None = None,
                refresh: bool = False) -> dict[str, Any]:
        """Her learned subjects (read-only), re-read every
        LEARNED_REFRESH_SECONDS."""
        character = self.character
        monotonic = self.monotonic()
        cached = self._learned
        if (not refresh and cached is not None and cached[0] == character and
                monotonic - cached[1] < LEARNED_REFRESH_SECONDS):
            return cached[2]
        now = self.clock() if now is None else now
        summary: dict[str, Any] = {"character_loaded": bool(character),
                                   "pending": self._pending(),
                                   "migrated": self.migrated}
        if character:
            try:
                store = client._memory_store()
                rows = store.route_events(character, since=now - EVENT_MAX_AGE,
                                          timeout=WRITE_TIMEOUT)
                traversed = store.route_traversals(character, timeout=WRITE_TIMEOUT)
            except (sqlite3.Error, OSError) as error:
                # Cached too: a busy database is not asked again on every poll.
                summary["error"] = f"not readable now: {error}"
            else:
                summary.update(learned_summary(rows, traversed, now=now))
        self._learned = (character, monotonic, summary)
        return summary


@dataclass(slots=True)
class Migration:
    """History from before P5, written once with the first write."""

    events: list[dict[str, Any]]
    traversals: list[dict[str, Any]]
    installed_at: float


def build_migration(adaptations: dict, route_hazards: dict, decisions: Iterable[Any], *,
                    before: float, content: str = "") -> Migration:
    """#94's route deaths, the route_hazards they do not explain, and the
    ledger's map visits, all from before ``before`` (4.4)."""
    deaths: dict[tuple[float, str], dict[str, Any]] = {}
    for record in (adaptations or {}).values():
        if not isinstance(record, dict):
            continue
        for entry in record.get("kit_failures", ()) or ():
            if not (isinstance(entry, dict) and entry.get("cause") == "route" and
                    entry.get("kind") == "death" and entry.get("map")):
                continue
            try:
                at = float(entry.get("time", 0.0) or 0.0)
                hurt = int(entry.get("map_damage", 1) or 0) > 0
            except (TypeError, ValueError, OverflowError):
                continue
            if not (hurt and math.isfinite(at) and 0 < at < before):
                continue
            path = str(entry["map"])
            # One death can sit in more than one spot's record.
            deaths[(at, path)] = {"at": at, "subject": map_subject(path), "signal": DEATH,
                                  "magnitude": 1.0,
                                  "kit": kit_from_description(entry.get("kit")),
                                  "content": content}
    events = list(deaths.values())
    by_map: dict[str, list[float]] = {}
    for at, path in deaths:
        by_map.setdefault(path, []).append(at)
    for path, until in (route_hazards or {}).items():
        try:
            learned = float(until) - ROUTE_HAZARD_SECONDS
        except (TypeError, ValueError, OverflowError):
            continue
        if not (isinstance(path, str) and path and math.isfinite(learned) and
                0 < learned < before):
            continue
        if any(learned - MIGRATION_MATCH_SECONDS <= at <= learned
               for at in by_map.get(path, ())):
            continue
        events.append({"at": learned, "subject": map_subject(path), "signal": DEATH,
                       "magnitude": 1.0, "kit": "", "content": content})
    return Migration(events, seed_traversals(decisions, before=before), before)


def seed_traversals(decisions: Iterable[Any], *, before: float) -> list[dict[str, Any]]:
    """Map-level traversal counts from the decision ledger: each run of
    records on one map is one visit. Untimed: the ledger does not say how
    long a crossing took."""
    counts: dict[str, list[float]] = {}
    previous = None
    for record in decisions:
        if not isinstance(record, dict):
            continue
        path = record.get("map")
        try:
            at = float(record.get("time", 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            continue
        if not (isinstance(path, str) and path.startswith("/") and math.isfinite(at)) \
                or at >= before:
            continue
        if path != previous:
            entry = counts.setdefault(path, [0, at])
            entry[0] += 1
        counts[path][1] = max(counts[path][1], at)
        previous = path
    return [{"subject": map_subject(path), "count": int(count), "timed": 0,
             "mean_seconds": 0.0, "last_at": last}
            for path, (count, last) in counts.items()]


def contribution(signal: str, magnitude: float, age: float) -> float:
    """One row's provisional cost in expected seconds after ``age`` seconds
    (no kit rule or recovery: those are P6's)."""
    value = WEIGHTS.get(signal, 0.0) * magnitude * 0.5 ** (max(0.0, age) / HALF_LIVES[signal])
    return value if value >= MIN_CONTRIBUTION else 0.0


def _cost(rows: list[tuple[float, str, float]], when: float) -> float:
    return sum(contribution(signal, magnitude, when - at) for at, signal, magnitude in rows)


def learned_summary(rows: Iterable[tuple], traversed: dict[str, int], *,
                    now: float) -> dict[str, Any]:
    """The LEARNED_LIMIT subjects with the highest provisional cost: their
    signals, last event, and when their cost falls below half."""
    by_subject: dict[str, list[tuple[float, str, float]]] = {}
    totals = {signal: 0 for signal in SIGNALS}
    for at, subject, signal, magnitude in rows:
        if signal not in WEIGHTS:
            continue
        by_subject.setdefault(subject, []).append((float(at), signal, float(magnitude)))
        totals[signal] += 1
    subjects = []
    for subject, events in by_subject.items():
        cost = _cost(events, now)
        if cost <= 0:
            continue
        low, high = now, now + 64 * max(HALF_LIVES.values())
        for _ in range(40):
            middle = (low + high) / 2
            if _cost(events, middle) > cost / 2:
                low = middle
            else:
                high = middle
        last = max(events)
        counts: dict[str, int] = {}
        for _, signal, _ in events:
            counts[signal] = counts.get(signal, 0) + 1
        subjects.append({"subject": subject, "kind": subject_kind(subject),
                         "cost_seconds": round(cost), "signals": counts,
                         "last_at": last[0], "last_signal": last[1],
                         "half_at": high, "traversals": int(traversed.get(subject, 0))})
    subjects.sort(key=lambda item: (-item["cost_seconds"], item["subject"]))
    return {"subjects": subjects[:LEARNED_LIMIT], "learned": len(subjects),
            "events": totals, "traversed": len(traversed)}


# Module hooks: each never raises into play.

def recorder(client: Any) -> RouteRecorder | None:
    value = getattr(client, "route_recorder", None)
    return value if isinstance(value, RouteRecorder) else None


def _guard(name: str, call: Callable[[RouteRecorder], Any], client: Any) -> None:
    value = recorder(client)
    if value is None:
        return
    try:
        call(value)
    except Exception:
        log.exception("route events: %s failed", name)


def note_tick(client: Any, task: Any) -> None:
    """A NavigateTask tick (every tick of a trip under way)."""
    _guard("tick", lambda value: value.tick(client, task), client)


def note_hold(client: Any, task: Any) -> None:
    """A NavigateThenTask tick: its trip is still under way, even while a
    fight or a recovery hold keeps ``task`` from ticking."""
    _guard("hold", lambda value: value.hold(client, task), client)


def note_edge(client: Any, task: Any, edge: Any) -> None:
    """The edge a decision about to be recorded concerns."""
    _guard("edge", lambda value: value.note_edge(task, edge), client)
