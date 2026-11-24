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
shares her party), kills, its death and duration. Nothing here changes
what she does.

**What the Classic server shows her** (server ``socket/request.c``):
every visible living object on MAP2 with its name and name colour, and a
tile animation where a living object took damage this round (the damage
number, ``ANIM_DAMAGE``) or where something died (``ANIM_KILL``, which
replaces the killing blow's number). Combat text, the owner's target,
stats and consumables go only to the owner. Health and mana percent
arrive only for party members (``CMD_PARTY_UPDATE``, server ``party.c``);
the client keeps the operator character's alone
(``GameState.operator_vitals``).

**Frames.** Each published MAP2 generation is one frame, captured as it is
published (``capture``, from the client): the operator character's square,
the monsters in view, whether another player is in view, whether she is
fighting, and that generation's tile animations. Every animation is scored
against its own frame, so a player who walks in, strikes and leaves
between two engine steps still marks the engagement shared, and an owner
who moved between frames is credited on the right square.

**Attribution** is by position within a frame: a damage number on the
operator character's square is a hit on it; one on a monster next to it
is its hit on that monster (``kinds``); a kill marker on a square where a
monster stood next to it in that frame or the one before is its kill and
killing blow; a kill marker on its own square is its death. A hit on its
square with no monster next to it is a ranged hit.

**Privacy.** Only the exact configured character is watched: its MAP2
name, less the server's bracketed tags (" [AFK]", permission tags;
``generate_quick_name``), must equal the configured name and carry a
player's name colour. Other players are only ever a flag: while one is in
her view (any distance, any level she sees) in any frame the engagement
is shared and is dropped whole; so is a possible player (an authored name
in a player colour that could be one, ``_maybe_player``) within
``MAYBE_PLAYER_RADIUS`` squares. A player she cannot see is not detected.
No name is stored: a row belongs to her own character, and a monster kind
is stored only when it is an authored creature name.

The engine drains the frames once a step (``observe_owner``); capture and
the step never raise into play.
"""

from __future__ import annotations

import collections
import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from . import constants as c
from .observations import is_player, plain
from .self_improvement import from_server, parse_hit
from .terrain import _CONTACT, CELL_SIZE, Terrain, features

log = logging.getLogger(__name__)

# An engagement ends this long after the last damage number or kill
# marker on or next to the operator character (seconds).
QUIET_SECONDS = 10.0
# The longest engagement kept as one row.
MAX_ENGAGEMENT_SECONDS = 900.0
# A possible player (an authored name in a player colour) this close to
# the operator character (squares, either axis) makes the engagement
# shared; a certain player does anywhere in view.
MAYBE_PLAYER_RADIUS = 6
# A step within this long after a ranged hit counts as stepping away.
STEP_AFTER_RANGED_SECONDS = 3.0
# Party vitals older than this are not used (seconds): the server sends
# them on change, but a stale reading is no "before".
VITALS_MAX_AGE_SECONDS = 30.0
# Her own contact lines this recent make a frame hers (seconds).
OWN_CONTACT_SECONDS = 10.0
# The newest messages a frame looks through for her own contact.
OWN_CONTACT_SCAN = 100
# Monster kinds kept per engagement, in order of first hit.
MAX_KINDS = 12
# Frames held between engine steps; an overflow makes the engagement
# shared (frames it did not see may have held a player).
MAX_FRAMES = 256
# A failing recorder logs its traceback once, then a count at most this
# often (seconds).
ERROR_REPORT_SECONDS = 600.0

# The server colours creature names by their level against hers
# (request.c get_living_level_color; libatrinik COLOR_*).
LEVEL_BANDS = {"999999": "gray", "00ff00": "green", "0080ff": "blue",
               "ffff33": "yellow", "ff9900": "orange", "ff3030": "red",
               "cc66ff": "purple"}
GREEN, RED = "00ff00", "ff3030"
# The bracketed tags the server appends to a player's map name.
_NAME_TAGS = re.compile(r"(?:\s+\[[^\[\]]*\])+$")


def map_player_name(name: str) -> str:
    """A player's MAP2 name without the server's trailing bracketed tags
    (permission tags and " [AFK]", server ``generate_quick_name``)."""
    return _NAME_TAGS.sub("", str(name)).strip()


def name_digest(name: str) -> str:
    """The in-memory key of a party member's name (never stored)."""
    return hashlib.sha256(plain(name).encode("utf-8")).hexdigest()


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
class Frame:
    """One published MAP2 generation, as the recorder needs it."""

    at: float
    character: str  # hers
    map: str
    node: Any = None
    # The operator character's world square, or None when not in view.
    owner: tuple[int, int] | None = None
    creatures: list[Creature] = field(default_factory=list)
    # Whether another (possible) player was in view: a flag, never who.
    others: bool = False
    # This generation's tile animations on her map at her depth:
    # (x, y, kind, value) in world squares.
    animations: list[tuple[int, int, int, int]] = field(default_factory=list)
    # The operator character's health percent, when it shares her party.
    hp: int | None = None
    # Whether she was fighting herself.
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
    owner_died: bool = False
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
            "kills": self.kills, "owner_died": int(self.owner_died),
            "seconds": round(max(0.0, ended - self.started), 3),
        }


class OwnerWatchRecorder:
    """Turns the operator character's visible fights into rows.

    ``note`` takes each frame as the client publishes it; ``observe``, once
    an engine step, scores the frames since the last step and returns the
    rows of engagements that ended; the caller stores them.
    """

    def __init__(self) -> None:
        self.engagement: Engagement | None = None
        self.frames: collections.deque[Frame] = collections.deque()
        self.frames_lost = False
        # Frames were lost: every engagement until a frame without one is
        # shared, as a lost frame may have held a player.
        self.tainted = False
        # When the operator character was last in view (wall clock), and
        # when its last engagement ended while it stayed in view since: the
        # rest before the next pull.
        self.seen_at: float | None = None
        self.last_end: float | None = None
        # Its health in the previous frame: "before" the pull.
        self.last_hp: int | None = None
        # The monsters next to it in the previous frame, by square: a kill
        # marker replaces the killing blow, and the monster is gone.
        self.previous_adjacent: dict[tuple[int, int], Creature] = {}
        self.shared_dropped = 0
        self.failures = 0
        self.failure_reported_at: float | None = None

    @property
    def active(self) -> bool:
        return self.engagement is not None

    def reset(self) -> None:
        self.engagement = None
        self.frames.clear()
        self.frames_lost = self.tainted = False
        self.seen_at = self.last_end = self.last_hp = None
        self.previous_adjacent = {}

    def note(self, frame: Frame) -> None:
        if len(self.frames) >= MAX_FRAMES:
            self.frames.popleft()
            self.frames_lost = True
        self.frames.append(frame)

    def observe(self, now: float, character: str, busy: bool = False
                ) -> list[dict[str, Any]]:
        """Score the frames since the last step; ``busy`` is her own fight
        as the engine sees it now."""
        rows: list[dict[str, Any]] = []
        frames, self.frames = list(self.frames), collections.deque()
        if self.frames_lost:
            self.tainted = True
        self.frames_lost = False
        for frame in frames:
            rows.extend(self._frame(frame))
        engagement = self.engagement
        if engagement is None:
            return rows
        if busy or engagement.character != character:
            self._drop()
        elif now - engagement.last_contact >= QUIET_SECONDS:
            rows.extend(self._end(engagement.last_contact))
        elif now - engagement.started >= MAX_ENGAGEMENT_SECONDS:
            rows.extend(self._end(now))
            # A cut fight's successor had no rest before it.
            self.last_end = None
        return rows

    def _drop(self) -> None:
        """Her own fight, or another frame of squares: never the owner's
        alone. Dropped, not kept."""
        self.engagement = None
        self.last_end = None

    def _frame(self, frame: Frame) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        engagement = self.engagement
        if engagement is not None and (engagement.character != frame.character or
                                       engagement.map != frame.map or frame.busy):
            self._drop()
            engagement = None
        if engagement is not None and frame.at - engagement.last_contact >= QUIET_SECONDS:
            rows.extend(self._end(engagement.last_contact))
            engagement = None
        if frame.busy or frame.owner is None:
            if engagement is not None:
                rows.extend(self._end(engagement.last_contact))
            self.seen_at = self.last_end = self.last_hp = None
            self.previous_adjacent = {}
            return rows
        owner = frame.owner
        self.seen_at = frame.at
        adjacent = {(creature.x, creature.y): creature for creature in frame.creatures
                    if chebyshev(owner, (creature.x, creature.y)) == 1}
        for x, y, kind, value in frame.animations:
            square = (x, y)
            distance = chebyshev(owner, square)
            if distance > 1:
                continue
            if distance == 0 and kind == c.ANIM_DAMAGE:
                engagement = self._contact(frame)
                engagement.hits_taken += 1
                engagement.damage_taken += max(0, int(value))
                if not adjacent:
                    engagement.ranged_hits += 1
                    engagement.ranged_at = (frame.at, owner)
            elif distance == 0:
                engagement = self._contact(frame)
                engagement.owner_died = True
            elif kind == c.ANIM_DAMAGE and square in adjacent:
                engagement = self._contact(frame)
                engagement.hits_dealt += 1
                engagement.damage_dealt += max(0, int(value))
                self._kind(engagement, adjacent[square])
            elif kind == c.ANIM_KILL:
                # Only a monster that stood next to it: a kill by position
                # alone could be anyone's.
                victim = adjacent.get(square) or self.previous_adjacent.get(square)
                if victim is None:
                    continue
                engagement = self._contact(frame)
                engagement.kills += 1
                # The marker replaces the killing blow's damage number.
                engagement.hits_dealt += 1
                engagement.damage_dealt += max(0, int(value))
                self._kind(engagement, victim)
        self.previous_adjacent = adjacent
        engagement = self.engagement
        if engagement is None:
            self.last_hp = frame.hp
            self.tainted = False
            return rows
        engagement.shared = engagement.shared or frame.others or self.tainted
        engagement.peak_adjacent = max(engagement.peak_adjacent, len(adjacent))
        if frame.hp is not None:
            engagement.hp_low = frame.hp if engagement.hp_low is None else \
                min(engagement.hp_low, frame.hp)
            engagement.hp_after = frame.hp
        self.last_hp = frame.hp
        if engagement.ranged_at is not None and engagement.ranged_at[0] < frame.at:
            at, square = engagement.ranged_at
            if owner != square:
                engagement.stepped_after_ranged += 1
                engagement.ranged_at = None
            elif frame.at - at > STEP_AFTER_RANGED_SECONDS:
                engagement.ranged_at = None
        return rows

    @staticmethod
    def _kind(engagement: Engagement, creature: Creature) -> None:
        entry = [creature.kind or "unknown", creature.band]
        if entry not in engagement.kinds and len(engagement.kinds) < MAX_KINDS:
            engagement.kinds.append(entry)

    def _contact(self, frame: Frame) -> Engagement:
        engagement = self.engagement
        if engagement is None:
            assert frame.owner is not None
            x, y = frame.owner
            engagement = self.engagement = Engagement(
                frame.character, frame.at, frame.map, x, y, features(frame.node, x, y),
                frame.at, hp_before=self.last_hp,
                gap_seconds=None if self.last_end is None else max(0.0, frame.at - self.last_end))
        engagement.last_contact = max(engagement.last_contact, frame.at)
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


def _maybe_player(obj: Any, names: Any, pvp: bool, party: Any) -> bool:
    """Whether a living object with an authored name could be a player
    anyway. Green and red are player colours (her party, a PvP area) as
    well as creature level bands (request.c). Outside PvP every player is
    her friend (``is_friend_of``, types/monster.c), so a green name that is
    her friend, or that a party member carries (she may be berserk), may
    be a party member; a red name may be a player only in a PvP area (or
    on a map she does not know)."""
    if names is None or plain(obj.name) not in names:
        return False  # is_player decides
    color = str(obj.name_color).casefold()
    if color == GREEN:
        return bool(obj.is_friend) or name_digest(map_player_name(obj.name)) in party
    return color == RED and pvp


def _her_own_fight(client: Any, state: Any, m: Any, now: float, fresh: list) -> bool:
    """Whether she is fighting herself in this frame: her combat mode, an
    active fight record, a damage number or kill marker on her square, or
    one of her own contact lines in the last OWN_CONTACT_SECONDS."""
    if state.combat or getattr(getattr(client, "fight_recorder", None), "active", False):
        return True
    if any(chebyshev((x, y), (m.world_x, m.world_y)) == 0 for x, y, *_ in fresh):
        return True
    for entry in reversed(state.messages[-OWN_CONTACT_SCAN:]):
        if float(entry[0]) < now - OWN_CONTACT_SECONDS:
            break
        if not from_server(entry):
            continue
        text = str(entry[-1]).strip()
        if parse_hit(text) is not None or _CONTACT.match(text):
            return True
    return False


def frame_of(client: Any, m: Any) -> Frame | None:
    """The frame of a just-published generation ``m``, or None while she
    is not playing. Other players become a flag, never names."""
    state = client.state
    if state.phase != "playing" or not m.path or not state.player_name:
        return None
    now = time.time()
    cx, cy = m.width // 2, m.height // 2
    fresh = [(m.world_x + x - cx, m.world_y + y - cy, kind, value)
             for x, y, depth, _sub, kind, value in m.animations if depth == 0]
    graph = getattr(client, "world_graph", None)
    node = getattr(graph, "nodes", {}).get(m.path) if graph is not None else None
    frame = Frame(at=now, character=state.player_name, map=m.path, node=node,
                  busy=_her_own_fight(client, state, m, now, fresh))
    operator = str(client.config.operator_character).strip()
    if frame.busy or not operator or operator == state.player_name:
        return frame
    content = getattr(client, "authored_content", None)
    names = getattr(content, "names", None)
    party = getattr(state, "party_member_digests", set())
    own = plain(state.player_name)
    pvp = node is None or bool(getattr(node, "pvp", False))
    owner: list[tuple[int, int]] = []
    creatures: list[Creature] = []
    others = False
    maybe: list[tuple[int, int]] = []
    for depth, x, y, obj in m.depth_targets():
        square = (m.world_x + x - cx, m.world_y + y - cy)
        if depth != 0:
            # Another level: only whether a player is there matters.
            if is_player(obj, own, content):
                others = True
            elif _maybe_player(obj, names, pvp, party):
                maybe.append(square)
            continue
        if _maybe_player(obj, names, pvp, party):
            # Perhaps a player with an authored name (or a friendly NPC or
            # pet): never a creature, and shared when close.
            maybe.append(square)
            continue
        if is_player(obj, own, content):
            if map_player_name(obj.name) == operator:
                owner.append(square)
            else:
                others = True
            continue
        name = plain(obj.name)
        if obj.is_friend or not name or name == own:
            continue
        creatures.append(Creature(*square, name if names is not None and name in names else "",
                                  LEVEL_BANDS.get(str(obj.name_color).casefold(), "")))
    if len(owner) != 1:
        return frame
    square = owner[0]
    if node is not None and not (0 <= square[0] < node.width and 0 <= square[1] < node.height):
        # On a map tiled to hers: another frame of squares and terrain.
        return frame
    frame.owner, frame.creatures = square, creatures
    frame.others = others or any(chebyshev(square, other) <= MAYBE_PLAYER_RADIUS
                                 for other in maybe)
    frame.animations = fresh
    vitals = state.operator_vitals
    if vitals is not None and now - float(vitals[2]) <= VITALS_MAX_AGE_SECONDS:
        frame.hp = int(vitals[0])
    return frame


def _failed(recorder: OwnerWatchRecorder) -> None:
    now = time.monotonic()
    failures = recorder.failures + 1
    if recorder.failure_reported_at is None:
        log.exception("owner watch failed")
        recorder.failure_reported_at, failures = now, 0
    elif now - recorder.failure_reported_at >= ERROR_REPORT_SECONDS:
        log.error("owner watch failed %d more times", failures)
        recorder.failure_reported_at, failures = now, 0
    recorder.failures = failures
    try:
        recorder.reset()
    except Exception:
        pass


def capture(client: Any, m: Any) -> None:
    """Take the frame of a just-published MAP2 generation (the client calls
    this on publish). With the flag off nothing is read. Never raises."""
    recorder = getattr(client, "owner_watch", None)
    if recorder is None:
        return
    try:
        if not enabled(client):
            return
        frame = frame_of(client, m)
        if frame is not None:
            recorder.note(frame)
    except Exception:
        _failed(recorder)


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
    """Score the frames since the last engine step and keep the engagements
    that ended; returns how many were kept. With the flag off nothing is
    read. Watching changes nothing she does and never raises into play."""
    recorder = getattr(client, "owner_watch", None)
    if recorder is None:
        return 0
    try:
        state = client.state
        if not enabled(client) or state.phase != "playing" or not state.player_name:
            if recorder.active or recorder.frames or recorder.seen_at is not None:
                recorder.reset()
            return 0
        busy = bool(state.combat) or bool(
            getattr(getattr(client, "fight_recorder", None), "active", False))
        rows = recorder.observe(time.time(), state.player_name, busy)
        kept = sum(bool(store(client, row)) for row in rows)
    except Exception:
        _failed(recorder)
        return 0
    if recorder.failure_reported_at is not None:
        if recorder.failures:
            log.error("owner watch failed %d more times", recorder.failures)
        recorder.failure_reported_at, recorder.failures = None, 0
    return kept
