"""Her own observations: the ingestion filter, attribution and authored text.

What she reads or is told goes through two steps before anything keeps it
(docs/LEARNING_AND_FEEDBACK.md 4.3.1):

1. **The ingestion filter** drops other players' text: chat channels, lines
   carrying the player-name link, ``/who`` roster lines, books a player
   signed, unsigned books whose text matches nothing authored, renamed items,
   and any line naming another character she has seen. Her own name becomes
   the literal ``{activator.name}``, so dialogue reads as authored.
2. **Attribution** ties a line to the object that produced it. DRAWINFO has
   no source, so it uses her own bump, apply, step or examine in the last
   ``WINDOW`` seconds (``timing``), an authored object on or next to her
   square whose message equals the text (``content-match``), or the
   packet's own identity (``exact``: book packets, interface pages,
   examines). An unattributed line is kept, as ``system:<map>``, only when
   it is a fact line (``FACT_LINES``); otherwise it is not stored.

This module only decides; ``AtrinikClient`` persists what it returns
through ``BotMemory.record_observation``. Nothing here changes what she
does.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import constants as c
from .model import CHAT_MESSAGE_TYPES, PLAYER_NAME_LINK, GameState, InterfaceState, Item

log = logging.getLogger(__name__)

# Seconds after her own action in which a line is that action's answer.
WINDOW = 1.5
# Seconds between examine lines of one answer (one server tick sends them).
EXAMINE_GAP = 0.25
# Characters kept of any stored text.
TEXT_LIMIT = 4000
# Her own name in stored text: the form authored dialogue uses.
ACTIVATOR = "{activator.name}"
# Other characters' names remembered for the filter, newest kept.
MAX_PLAYER_NAMES = 500

NAVY = "00ffff"
WHITE = "ffffff"
# A living object named in one of these colours is a player: the server
# colours players white, or green (her party) or red (PvP), and creatures
# by level (request.c get_playername_color and get_living_level_color).
PLAYER_NAME_COLORS = frozenset({WHITE, "00ff00", "ff3030"})

# Authored object types whose message she can receive.
TYPE_DOOR, TYPE_HANDLE, TYPE_SIGN = 20, 93, 98

# Fixed server phrasings that confirm the kind of source (door.c,
# handle.c, sign.c). A door's own refusal text is its authored ``msg``.
DOOR_OPENED = re.compile(r"^You open the (?P<name>.+?) with the .+\.$")
DOOR_OPENED_FOR_YOU = re.compile(r"^The (?P<name>.+?) is opened for you\.$")
LEVER_PHRASES = (re.compile(r"^The (?P<name>.+?) is locked\.$"),
                 re.compile(r"^The (?P<name>.+?) won't budge\.$"),
                 re.compile(r"^You turn the (?P<name>.+?)\.$"))
SIGN_PHRASES = frozenset({"nothing is written on it.",
                          "you are unable to decipher the strange symbols."})

# /who (commands/player/who.c): one line a player, then the total.
WHO_LINE = re.compile(
    r"^(?P<name>[^\s\[\]]+(?: [^\s\[\]]+)*?) the (?:neuter|male|female|hermaphrodite) "
    r".+ \(lvl \d+\)(?: \[AFK\])?(?: \(route: .*\))?$")
WHO_TOTAL = re.compile(r"^There (?:is|are) \d+ players? online\.$")
# The sender a chat-style line names ("[a=#charname]Zoey[/a] says: ...").
NAME_LINK = re.compile(r"\[a=#charname\]([^\[]+)\[/a\]")
MARKUP = re.compile(r"\[/?[a-z]+(?:=[^\]]*)?\]", re.I)

# Server lines that are facts about her square even without a source
# (spell_util.c, spell_effect.c: a no-magic square). Later parsers add
# kinds; until then these are the only unattributed lines kept.
FACT_LINES = frozenset({
    "something blocks your spellcasting.",
    "something blocks your spell.",
})

# observations_dropped_total reasons.
DROP_REASONS = ("chat", "player-link", "who-list", "player-name", "signed-book",
                "unmatched-book", "book-unverified", "renamed-item")


def normalize(text: str) -> str:
    """Whitespace-collapsed text, as authored and received forms compare."""
    return " ".join(str(text).split())


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def plain(text: str) -> str:
    """Markup removed and case folded, for name matching."""
    return normalize(MARKUP.sub("", str(text))).casefold()


def _name_pattern(name: str) -> re.Pattern:
    return re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", re.I)


@dataclass(slots=True, frozen=True)
class BookPage:
    title: str
    body: str
    # A signed book carries its signer and date after the text (book.c).
    signed: bool


def parse_book(raw: bytes) -> BookPage | None:
    """A CLIENT_CMD_BOOK payload: ``[book]Title[/book]`` and the text, NUL
    ended, then for a signed book the signer and the date (book.c)."""
    data = bytes(raw)
    if not data.startswith(b"[book]"):
        return None
    end = data.find(b"[/book]")
    if end < 0:
        return None
    title = data[len(b"[book]"):end].decode("utf-8", "replace")
    rest = data[end + len(b"[/book]"):]
    body, nul, tail = rest.partition(b"\0")
    return BookPage(normalize(title), body.decode("utf-8", "replace"),
                    signed=bool(nul) and bool(tail.strip(b"\0")))


@dataclass(slots=True, frozen=True)
class AuthoredSource:
    """An authored map object that can send her text."""
    kind: str        # door | gate | lever | sign | magic_mouth
    name: str
    map: str
    x: int
    y: int
    msg_hash: str    # "" when it has no authored message

    @property
    def key(self) -> str:
        return f"{self.kind if self.kind != 'magic_mouth' else 'mouth'}:{self.map}:{self.x},{self.y}"


@dataclass(slots=True)
class AuthoredContent:
    """Authored text and the objects that send it, from locked content.

    ``texts`` holds the hash of every authored message (maps, archetypes,
    artifacts and the book message file); ``sources`` the doors, levers,
    signs and magic mouths by square; ``names`` every authored object and
    archetype name, case folded, so a creature in view is told from a
    player.
    """
    texts: set[str] = field(default_factory=set)
    sources: dict[tuple[str, int, int], list[AuthoredSource]] = field(default_factory=dict)
    names: set[str] = field(default_factory=set)

    def at(self, map_path: str, x: int, y: int) -> list[AuthoredSource]:
        return self.sources.get((map_path, x, y), [])

    @classmethod
    def scan(cls, root: Path, maps: Iterable[Path] | None = None) -> "AuthoredContent":
        """Scan ``root``'s ``arch`` and ``maps`` (or only ``maps``)."""
        content = cls()
        defaults = content._scan_arch(root / "arch")
        map_root = root / "maps"
        for path in (sorted(map_root.rglob("*")) if maps is None else maps):
            try:
                if not path.is_file():
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            if not data.startswith(b"arch map\n"):
                continue
            key = "/" + path.relative_to(map_root).as_posix()
            content._scan_map(key, data.decode("utf-8", "replace"), defaults)
        return content

    def _add_text(self, lines: list[str]) -> str:
        msg = "\n".join(lines)
        if not normalize(msg):
            return ""
        digest = text_hash(msg)
        self.texts.add(digest)
        return digest

    def _scan_arch(self, arch_root: Path) -> dict[str, dict[str, str]]:
        """Archetype defaults (type, name, msg hash, sys_object) by name."""
        defaults: dict[str, dict[str, str]] = {}
        if not arch_root.is_dir():
            return defaults
        for path in sorted(arch_root.rglob("*")):
            if not path.is_file() or not (
                    path.suffix in (".arc", ".art") or
                    path.name in ("messages", "artifacts")):
                continue
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            if path.name == "messages":
                # lib/messages: MSG ... ENDMSG (readable.c init_msgfile).
                body: list[str] | None = None
                for line in text.split("\n"):
                    line = line.rstrip()
                    if body is None:
                        if line == "MSG":
                            body = []
                    elif line == "ENDMSG":
                        self._add_text(body)
                        body = None
                    else:
                        body.append(line)
                continue
            current: dict[str, str] | None = None
            depth = 0
            body = None
            for raw in text.split("\n"):
                line = raw.rstrip("\r")
                if body is not None:
                    if line == "endmsg":
                        digest = self._add_text(body)
                        if current is not None and depth == 1 and digest:
                            current.setdefault("msg", digest)
                        body = None
                    else:
                        body.append(line)
                    continue
                if line == "msg":
                    body = []
                    continue
                key, _, value = line.strip().partition(" ")
                if key == "Object":
                    current = {"arch": value} if path.suffix == ".arc" else None
                    depth = 1
                    if current is not None:
                        defaults.setdefault(value, current)
                        self.names.add(value.replace("_", " ").casefold())
                elif key == "arch":
                    depth += 1
                elif key == "end":
                    depth = max(0, depth - 1)
                    if depth == 0:
                        current = None
                elif current is not None and depth == 1 and key in (
                        "type", "name", "sys_object"):
                    current.setdefault(key, value)
                    if key == "name" and value:
                        self.names.add(value.casefold())
        return defaults

    def _scan_map(self, key: str, text: str,
                  defaults: dict[str, dict[str, str]]) -> None:
        stack: list[dict[str, Any]] = []
        body: list[str] | None = None
        for raw in text.split("\n"):
            line = raw.rstrip("\r")
            if body is not None:
                if line == "endmsg":
                    if stack:
                        stack[-1]["msg"] = self._add_text(body)
                    body = None
                else:
                    body.append(line)
                continue
            if line == "msg":
                body = []
                continue
            word, _, value = line.partition(" ")
            if word == "arch":
                stack.append({"arch": value})
            elif word == "end":
                if stack:
                    self._map_object(key, stack.pop(), stack, defaults)
            elif stack and word in ("type", "name", "x", "y", "sys_object"):
                stack[-1][word] = value

    def _map_object(self, key: str, obj: dict[str, Any], parents: list[dict[str, Any]],
                    defaults: dict[str, dict[str, str]]) -> None:
        arch = obj.get("arch", "")
        if arch == "map":
            return
        base = defaults.get(arch, {})
        name = obj.get("name") or base.get("name") or arch.replace("_", " ")
        if name:
            self.names.add(str(name).casefold())
        try:
            obj_type = int(obj.get("type") or base.get("type") or 0)
        except ValueError:
            return
        if obj_type not in (TYPE_DOOR, TYPE_HANDLE, TYPE_SIGN):
            return
        # An inventory object stands where its top-level container does.
        x = obj.get("x")
        y = obj.get("y")
        for parent in reversed(parents):
            x = parent.get("x") if x is None else x
            y = parent.get("y") if y is None else y
        try:
            x, y = int(x or 0), int(y or 0)
        except ValueError:
            return
        if obj_type == TYPE_DOOR:
            kind = "gate" if "gate" in arch.casefold() else "door"
        elif obj_type == TYPE_HANDLE:
            kind = "lever"
        elif "magic_mouth" in arch or (obj.get("sys_object") or base.get("sys_object")) == "1":
            kind = "magic_mouth"
        else:
            kind = "sign"
        msg = obj.get("msg", base.get("msg", ""))
        self.sources.setdefault((key, x, y), []).append(
            AuthoredSource(kind, str(name), key, x, y, msg or ""))


def load_authored_content() -> AuthoredContent | None:
    """The authored-text index of the locked content, or None without it.

    Scans a few thousand map files once (about a second); call it at
    start-up, never from the tick loop.
    """
    try:
        try:
            from tools.world_content_audit import ROOT
        except ModuleNotFoundError:
            from world_content_audit import ROOT
        started = time.monotonic()
        content = AuthoredContent.scan(Path(ROOT))
        log.info("authored text index: %d texts, %d sources in %.1fs",
                 len(content.texts), len(content.sources), time.monotonic() - started)
        return content
    except Exception:
        log.exception("authored text index unavailable: books are not kept "
                      "and lines are attributed by timing only")
        return None


@dataclass(slots=True)
class Observation:
    """One stored reading; ``BotMemory.record_observation`` takes ``row()``."""
    at: float
    map: str
    x: int
    y: int
    source_kind: str
    source_key: str
    source_name: str
    attribution: str
    authored: bool
    text: str
    color: str = ""
    msg_type: int | None = None

    def row(self) -> dict[str, Any]:
        return {"at": self.at, "map": self.map, "x": self.x, "y": self.y,
                "source_kind": self.source_kind, "source_key": self.source_key,
                "source_name": self.source_name, "attribution": self.attribution,
                "authored": int(self.authored), "text_hash": text_hash(self.text),
                "text": self.text, "color": self.color, "msg_type": self.msg_type}


@dataclass(slots=True)
class _Action:
    at: float
    kind: str                      # move | apply | examine
    map: str
    origin: tuple[int, int]
    target: tuple[int, int]        # bump square; her square for an apply
    item_name: str = ""
    item_type: int = 0
    # An apply of an object on the ground under her, not one she carries.
    ground: bool = False


@dataclass(slots=True)
class _Examine:
    at: float
    last: float
    name: str
    map: str
    x: int
    y: int
    lines: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Deferred:
    at: float
    text: str
    color: str
    msg_type: int
    action: _Action


class ObservationRecorder:
    """Turns what her client receives into attributed observations.

    Each ``on_*`` method returns the observations to store (possibly none);
    ``dropped`` is called with a ``DROP_REASONS`` entry for each item the
    filter refused. ``content`` returns the authored-text index or None.
    """

    def __init__(self, content: Callable[[], AuthoredContent | None] = lambda: None,
                 dropped: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.time):
        self.content = content
        self.dropped = dropped or (lambda reason: None)
        self.clock = clock
        self._actions: deque[_Action] = deque(maxlen=16)
        self._examine: _Examine | None = None
        self._deferred: list[_Deferred] = []
        self._last_square: tuple[str, int, int] | None = None
        self._square: tuple[str, int, int] | None = None
        # Other characters she has seen (chat senders, /who): folded name to
        # when last seen. Never stored; it only drops lines naming them.
        self.player_names: dict[str, float] = {}
        self._patterns: dict[str, re.Pattern] = {}

    # Her own actions.

    def note_move(self, state: GameState, direction: int) -> None:
        dx, dy = c.DIRECTION_DELTAS.get(direction, (0, 0))
        m = state.map
        self._note(_Action(self.clock(), "move", m.path, (m.world_x, m.world_y),
                           (m.world_x + dx, m.world_y + dy)))

    def note_apply(self, state: GameState, item: Item | None) -> None:
        """An apply: of an object under her (a lever, a sign) or one she carries."""
        if item is None:
            return
        m = state.map
        here = (m.world_x, m.world_y)
        self._note(_Action(self.clock(), "apply", m.path, here, here,
                           item.name, item.item_type, ground=item.location == 0))

    def note_examine(self, state: GameState, item: Item | None) -> None:
        if item is None:
            return
        m = state.map
        here = (m.world_x, m.world_y)
        self._note(_Action(self.clock(), "examine", m.path, here, here,
                           item.name, item.item_type))

    def note_position(self, state: GameState) -> list[Observation]:
        m = state.map
        square = (m.path, m.world_x, m.world_y)
        if square != self._square:
            self._last_square, self._square = self._square, square
        return self.flush(state)

    def _note(self, action: _Action) -> None:
        self._actions.append(action)

    def _recent(self, now: float) -> list[_Action]:
        return [a for a in self._actions if 0 <= now - a.at <= WINDOW]

    # Names.

    def _remember_player(self, name: str, now: float) -> None:
        folded = plain(name)
        if len(folded) < 2:
            return
        self.player_names.pop(folded, None)
        self.player_names[folded] = now
        while len(self.player_names) > MAX_PLAYER_NAMES:
            del self.player_names[next(iter(self.player_names))]

    def _players_in_view(self, state: GameState) -> set[str]:
        content = self.content()
        own = plain(state.player_name)
        names: set[str] = set()
        for _, _, obj in state.map.targets():
            name = plain(obj.name)
            if not name or name == own:
                continue
            color = obj.name_color.casefold()
            if color == WHITE or (color in PLAYER_NAME_COLORS and (
                    content is None or name not in content.names)):
                names.add(name)
        return names

    def _names_another(self, text: str, state: GameState) -> bool:
        folded = plain(text)
        own = plain(state.player_name)
        for name in (*self.player_names, *self._players_in_view(state)):
            if name == own or name not in folded:
                continue
            pattern = self._patterns.get(name)
            if pattern is None:
                if len(self._patterns) > 2 * MAX_PLAYER_NAMES:
                    self._patterns.clear()
                pattern = self._patterns[name] = _name_pattern(name)
            if pattern.search(folded):
                return True
        return False

    def _clean(self, text: str, state: GameState) -> str | None:
        """Her name made authored, or None (counted) when it names another."""
        own = normalize(state.player_name)
        if own:
            text = _name_pattern(own).sub(ACTIVATOR, text)
        if self._names_another(text, state):
            self.dropped("player-name")
            return None
        return text[:TEXT_LIMIT]

    # Server text.

    def on_message(self, state: GameState, msg_type: int, color: str,
                   text: str) -> list[Observation]:
        now = self.clock()
        out = self.flush(state, now)
        text = str(text)
        if msg_type in CHAT_MESSAGE_TYPES or msg_type in c.PLAYER_CHAT_TYPES:
            for name in NAME_LINK.findall(text):
                self._remember_player(name, now)
            self.dropped("chat")
            return out
        if PLAYER_NAME_LINK in text:
            for name in NAME_LINK.findall(text):
                self._remember_player(name, now)
            self.dropped("player-link")
            return out
        stripped = normalize(text)
        who = WHO_LINE.match(stripped)
        if who or WHO_TOTAL.match(stripped):
            if who:
                self._remember_player(who.group("name"), now)
            self.dropped("who-list")
            return out
        color = str(color).casefold()
        consumed, finished = self._examine_line(state, msg_type, color, stripped, now)
        if finished is not None:
            out.append(finished)
        if consumed:
            return out
        observation = self._attribute(state, msg_type, color, text, now)
        if observation is not None:
            out.append(observation)
        return out

    def _examine_line(self, state: GameState, msg_type: int, color: str, text: str,
                      now: float) -> tuple[bool, Observation | None]:
        """Collect an examine answer: whether the line belongs to one, and
        the previous answer when this line ended it."""
        group = self._examine
        finished = None
        is_game = msg_type == c.CHAT_TYPE_GAME and color == WHITE
        if group is not None:
            if (is_game and now - group.last <= EXAMINE_GAP and
                    not text.startswith("That is ") and text.casefold() not in FACT_LINES):
                group.lines.append(text)
                group.last = now
                return True, None
            # Anything else ends the answer.
            finished = self._finish_examine(state)
        if not (is_game and text.startswith("That is ")):
            return False, finished
        action = next((a for a in reversed(self._recent(now)) if a.kind == "examine"), None)
        if action is None:
            return False, finished
        self._actions.remove(action)
        m = state.map
        self._examine = _Examine(now, now, action.item_name, m.path,
                                 m.world_x, m.world_y, [text])
        return True, finished

    def _finish_examine(self, state: GameState) -> Observation | None:
        group, self._examine = self._examine, None
        if group is None:
            return None
        if any(line.startswith("You name it ") for line in group.lines):
            self.dropped("renamed-item")
            return None
        if "(signed; permanently finalized)" in group.lines[0]:
            self.dropped("signed-book")
            return None
        name = normalize(MARKUP.sub("", group.name))
        if not name or "corpse" in name.casefold():
            return None
        text = self._clean("\n".join(group.lines), state)
        if text is None:
            return None
        return Observation(group.at, group.map, group.x, group.y, "examine",
                           f"examine:{name.casefold()}", name, "exact", False, text,
                           WHITE, c.CHAT_TYPE_GAME)

    def _attribute(self, state: GameState, msg_type: int, color: str, text: str,
                   now: float) -> Observation | None:
        m = state.map
        here = (m.path, m.world_x, m.world_y)
        recent = self._recent(now)
        content = self.content()
        digest = text_hash(text)
        stripped = normalize(text)
        source: AuthoredSource | None = None
        attribution = ""
        # Content match on or next to her square (and the square she left).
        if content is not None and m.path:
            squares = {(m.world_x + dx, m.world_y + dy)
                       for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
            if self._last_square and self._last_square[0] == m.path:
                squares.add(self._last_square[1:])
            matches = {s.key: s for x, y in squares for s in content.at(m.path, x, y)
                       if s.msg_hash == digest}
            if len(matches) == 1:
                source = next(iter(matches.values()))
                own = any(a.map == m.path and (source.x, source.y) in (a.origin, a.target)
                          for a in recent if a.kind in ("move", "apply"))
                on_square = (source.x, source.y) == (m.world_x, m.world_y) or (
                    self._last_square == (m.path, source.x, source.y))
                if own:
                    attribution = "timing"
                elif on_square or not self._players_in_view(state):
                    attribution = "content-match"
                else:
                    # A broadcast another player may have caused (sign.c
                    # trigger_func): never a hint of hers.
                    source = None
        if source is not None:
            return self._observation(state, now, source.map, source.x, source.y,
                                     source.kind, source.key, source.name, attribution,
                                     True, text, color, msg_type)
        action = recent[-1] if recent else None
        if action is not None and action.kind == "move" and action.map == m.path:
            tx, ty = action.target
            opened = DOOR_OPENED.match(stripped) or DOOR_OPENED_FOR_YOU.match(stripped)
            if color == WHITE and opened:
                return self._observation(state, now, m.path, tx, ty, "door",
                                         f"door:{m.path}:{tx},{ty}", opened.group("name"),
                                         "timing", False, text, color, msg_type)
            # A refused bump leaves her where she was.
            if color == NAVY and (m.world_x, m.world_y) == action.origin:
                known = content.at(m.path, tx, ty) if content is not None else None
                if known:
                    # Her bump reached an authored door or sign whose text
                    # differs from its authored message (a script set it).
                    s = known[0]
                    return self._observation(state, now, m.path, tx, ty, s.kind, s.key,
                                             s.name, "timing", False, text, color, msg_type)
                if content is None:
                    # Without the index: a door if she stayed, a magic mouth
                    # if she stepped on (decided once her square is known).
                    self._deferred.append(_Deferred(now, text, color, msg_type, action))
                    self._actions.remove(action)
                    return None
        if (action is not None and action.kind == "apply" and action.ground and
                action.map == m.path):
            x, y = action.target
            if action.item_type == TYPE_HANDLE and color == WHITE:
                named = next((p.match(stripped) for p in LEVER_PHRASES if p.match(stripped)), None)
                return self._observation(state, now, m.path, x, y, "lever",
                                         f"lever:{m.path}:{x},{y}",
                                         named.group("name") if named else action.item_name,
                                         "timing", False, text, color, msg_type)
            if action.item_type == TYPE_SIGN and (
                    color == NAVY or (color == WHITE and stripped.casefold() in SIGN_PHRASES)):
                return self._observation(state, now, m.path, x, y, "sign",
                                         f"sign:{m.path}:{x},{y}", action.item_name,
                                         "timing", False, text, color, msg_type)
        if stripped.casefold() in FACT_LINES and m.path:
            return self._observation(state, now, m.path, m.world_x, m.world_y, "system",
                                     f"system:{m.path}", "", "none", False, text,
                                     color, msg_type)
        return None

    def _observation(self, state: GameState, now: float, map_path: str, x: int, y: int,
                     kind: str, key: str, name: str, attribution: str, authored: bool,
                     text: str, color: str, msg_type: int | None,
                     stood: tuple[int, int] | None = None) -> Observation | None:
        """An observation of the source at (x, y); the row records where she
        stood (``stood``, else her square now), the key names the source."""
        cleaned = self._clean(text, state)
        if cleaned is None:
            return None
        m = state.map
        sx, sy = stood if stood is not None else (m.world_x, m.world_y)
        return Observation(now, map_path or m.path, sx, sy, kind, key,
                           normalize(MARKUP.sub("", name)), attribution, authored,
                           cleaned, color, msg_type)

    def flush(self, state: GameState, now: float | None = None) -> list[Observation]:
        """Finish answers whose time is up: an examine, a deferred bump."""
        now = self.clock() if now is None else now
        out: list[Observation] = []
        if self._examine is not None and now - self._examine.last > EXAMINE_GAP:
            finished = self._finish_examine(state)
            if finished is not None:
                out.append(finished)
        m = state.map
        keep: list[_Deferred] = []
        for item in self._deferred:
            action = item.action
            moved = m.path == action.map and (m.world_x, m.world_y) == action.target
            if not moved and now - item.at <= WINDOW and m.path == action.map:
                keep.append(item)
                continue
            if m.path != action.map:
                continue
            tx, ty = action.target
            kind = "magic_mouth" if moved else "door"
            key = f"{'mouth' if moved else 'door'}:{action.map}:{tx},{ty}"
            observation = self._observation(state, item.at, action.map, tx, ty, kind,
                                            key, "", "timing", False, item.text,
                                            item.color, item.msg_type, stood=action.origin)
            if observation is not None:
                out.append(observation)
        self._deferred = keep
        return out

    def on_interface(self, state: GameState, interface: InterfaceState) -> list[Observation]:
        now = self.clock()
        out = self.flush(state, now)
        title = normalize(MARKUP.sub("", interface.title or ""))
        if not normalize(interface.text) or not title:
            return out
        m = state.map
        if self._names_another(title, state):
            self.dropped("player-name")
            return out
        own = normalize(state.player_name)
        if own:
            title = _name_pattern(own).sub(ACTIVATOR, title)
        text = self._clean(interface.text, state)
        if text is None:
            return out
        out.append(Observation(now, m.path, m.world_x, m.world_y, "npc",
                               f"npc:{m.path}:{title}", title, "exact", False, text))
        return out

    def on_book(self, state: GameState, raw: bytes) -> list[Observation]:
        now = self.clock()
        out = self.flush(state, now)
        page = parse_book(raw)
        if page is None or page.title in ("Quest List",) or "No quests to speak of" in page.body:
            # The quest journal is read by quests.parse_quest_book.
            return out
        if page.signed:
            self.dropped("signed-book")
            return out
        content = self.content()
        if content is None:
            self.dropped("book-unverified")
            return out
        digest = text_hash(page.body)
        if digest not in content.texts:
            # Player-edited, or generated (bestiary, artifact and spell path
            # books are not indexed): never kept.
            self.dropped("unmatched-book")
            return out
        recent = [a for a in self._recent(now) if a.kind == "apply"]
        kind = "scroll" if recent and recent[-1].item_type == c.TYPE_SCROLL else "book"
        title = normalize(MARKUP.sub("", page.title))
        if self._names_another(title, state):
            self.dropped("player-name")
            return out
        text = self._clean(page.body.strip("\n"), state)
        if text is None:
            return out
        m = state.map
        out.append(Observation(now, m.path, m.world_x, m.world_y, kind,
                               f"book:{title}:{digest}", title, "exact", True, text))
        return out
