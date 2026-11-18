"""Her own observations: the ingestion filter, attribution and authored text.

What she reads goes through two steps before anything keeps it
(docs/LEARNING_AND_FEEDBACK.md 4.3.1; "F1 as built" there records how
this differs from the design):

1. **The ingestion filter** drops other players' text: chat channels, lines
   carrying the player-name link, ``/who`` roster lines, signed books,
   renamed items, examines that name a player, and any line naming another
   character she has seen in this process. Her own name becomes the
   literal ``{activator.name}``.
2. **Exact matching against authored content.** Only what can be matched
   exactly against the authored-text index is kept: sign, door and magic
   mouth text equal to the authored message of an object on or next to her
   square (``timing`` when her own bump, apply or step reached it, else
   ``content-match``); a fixed server phrase answering her one bump or
   apply, naming the authored door, gate or lever where it reached;
   authored books, under their authored title; examines of items whose
   shown name is an authored name, every line of a known examine form.
   Dialogue pages are not kept (phase F13). An unattributed line is kept,
   as ``system:<map>``, only when it is a fact line (``FACT_LINES``).

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
from .model import CHAT_MESSAGE_TYPES, PLAYER_NAME_LINK, GameState, Item

log = logging.getLogger(__name__)

# Seconds after her own action in which a line is that action's answer.
WINDOW = 1.5
# Characters kept of any stored text.
TEXT_LIMIT = 4000
# Her own name in stored text: the form authored text uses.
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
DOOR_OPENED = re.compile(r"^You open the (?P<name>.+?) with the (?P<key>.+)\.$")
# Book lines with the same opening (book.c): never a door's.
NOT_DOOR = re.compile(r"(?:and start reading|and find it empty)\.$")
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

# Examine output after "That is ..." (types/player.c examine), as forms;
# "You name it ...", "... soulbound to NAME." and "The object has a story:"
# are handled apart, and a blank line ends the answer.
_IT = r"(?:It|They)"
_IS = r"(?:It is|They are)"
EXAMINE_LINES = tuple(re.compile(pattern) for pattern in (
    rf"^{_IS} (?:good|evil|neutral) aligned\.$",
    r"^It needs a level of \d+(?: in [a-z ]+)? to use\.$",
    r"^Qua: \d+ Con: (?:\d+|Indestructible)\.$",
    r"^Something is written in it\.$",
    r"^It seems to contain (?:no|tiny bits of|a small amount of|an average amount of|"
    r"a moderate amount of|a fair amount of|a great amount of) knowledge you could "
    r"learn from\.$",
    r"^It contains a level \d+ [a-z]+ spell\.$",
    r"^It requires a minimum level of \d+ in literacy to learn\.$",
    r"^It can hold only [a-z ]+(?: and its weight limit is [\d.]+ kg)?\.$",
    r"^Its weight limit is [\d.]+ kg\.$",
    r"^It (?:increases|decreases) the weight of items inside by [\d.]+%\.$",
    r"^It contains [\d.]+ kg(?:, (?:increased|decreased) to [\d.]+ kg)?\.$",
    r"^It has \d+ charges left\.$",
    r"^It has capacity of [\d.]+k?(?: and is [a-z ]+)?\.$",
    rf"^{_IS} made of: [a-z ]*$",
    rf"^{_IT} weighs? [\d.]+ kg(?: and is carrying [\d.]+ kg)?\.$",
    rf"^{_IT} would become soulbound to you\.$",
    rf"^{_IS} soulbound without an owner\.$",
    rf"^{_IT} would cost (?:you [a-z0-9 ,]+|nothing)\.$",
    r"^(?:It is a god-given item|They are god-given items)\.$",
    rf"^(?:But )?(?:{_IS}|it is|they are) worth(?:less| [a-z0-9 ,]+)\.$",
    r"^This shop will pay you [a-z0-9 ,]+\.$",
))
SOULBOUND_TO = re.compile(rf"^{_IS} soulbound to .+\.$")
# Prefixes and suffixes of an item's shown name (item.c object_get_name,
# object_get_short_name; race.c item_races) around its authored name.
COUNT_WORDS = frozenset((
    "a", "an", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
    "eighteen", "nineteen", "twenty"))
ITEM_RACES = frozenset(("dwarven", "elven", "gnomish", "drow", "orcish", "goblin",
                        "kobold", "giant", "tiny", "demonish", "draconish", "ogre"))
TRAILING_PARTS = re.compile(r"(?:\s*\([^()]*\)|\s+[+-]\d+|\s+\*)+$")

# observations_dropped_total reasons.
DROP_REASONS = ("chat", "player-link", "who-list", "player-name", "signed-book",
                "unmatched-book", "book-unverified", "renamed-item",
                "examine-interrupted", "unauthored-item", "examine-unverified",
                "book-examine")


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
    player and an examined item is known to be authored; ``book_titles``
    the authored names of the books carrying each message; ``materials``
    the material names the server prefixes to item names.
    """
    texts: set[str] = field(default_factory=set)
    sources: dict[tuple[str, int, int], list[AuthoredSource]] = field(default_factory=dict)
    names: set[str] = field(default_factory=set)
    book_titles: dict[str, set[str]] = field(default_factory=dict)
    materials: set[str] = field(default_factory=set)

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

    def book_title(self, digest: str) -> str:
        """The authored name of a book with this message, or ""."""
        titles = self.book_titles.get(digest)
        return min(titles) if titles else ""

    def _add_book(self, digest: str, name: str, title: str) -> None:
        if digest and name:
            full = normalize(f"{name} {title or ''}")
            self.book_titles.setdefault(digest, set()).add(full)

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
                    path.name in ("messages", "artifacts", "materials")):
                continue
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            if path.name == "materials":
                # lib/materials: the real materials' names (material.c).
                for line in text.split("\n"):
                    key, _, value = line.strip().partition(" ")
                    if key == "name" and value:
                        self.materials.add(normalize(value).casefold())
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
                        if current is not None and current.get("type") == "8":
                            self._add_book(current.get("msg", ""),
                                           current.get("name") or current["arch"],
                                           current.get("title", ""))
                        current = None
                elif current is not None and depth == 1 and key in (
                        "type", "name", "name_pl", "sys_object", "title"):
                    current.setdefault(key, value)
                    if key in ("name", "name_pl") and value:
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
            elif stack and word in ("type", "name", "name_pl", "title", "x", "y", "sys_object"):
                stack[-1][word] = value

    def _map_object(self, key: str, obj: dict[str, Any], parents: list[dict[str, Any]],
                    defaults: dict[str, dict[str, str]]) -> None:
        arch = obj.get("arch", "")
        if arch == "map":
            return
        base = defaults.get(arch, {})
        name = obj.get("name") or base.get("name") or arch.replace("_", " ")
        for shown in (name, obj.get("name_pl")):
            if shown:
                self.names.add(str(shown).casefold())
        try:
            obj_type = int(obj.get("type") or base.get("type") or 0)
        except ValueError:
            return
        if obj_type == c.TYPE_BOOK:
            self._add_book(obj.get("msg", base.get("msg", "")), str(name),
                           obj.get("title", base.get("title", "")))
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


def authored_item_name(content: AuthoredContent, shown: str) -> str | None:
    """The authored name inside an item's shown name, case folded, or None.

    The server shows [count] [item race] [material] name [title or
    "+N"] [(state)...]; the name between must be an authored object or
    archetype name, and anything else (a player's title) gives None.
    """
    # Not markup-stripped: a "[rank]" title is a player's, not markup.
    shown = TRAILING_PARTS.sub("", normalize(shown).casefold())
    words = shown.split()
    if words and (words[0] in COUNT_WORDS or words[0].isdigit()):
        words = words[1:]
    if words and words[0] in ITEM_RACES:
        words = words[1:]
    for start in range(len(words)):
        if start and " ".join(words[:start]) not in content.materials:
            continue
        name = " ".join(words[start:])
        if name in content.names:
            return name
    return None


def load_authored_content() -> AuthoredContent | None:
    """The authored-text index of the locked content, or None without it.

    Scans a few thousand map files once (about 1.3 s); call it at
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
        log.exception("authored text index unavailable: only unattributed "
                      "fact lines are kept")
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
    # The previous line was "The object has a story:"; this one must be
    # authored text.
    story_next: bool = False
    # A line made the whole answer unsafe to keep.
    rejected: str = ""
    item_type: int = 0


class ObservationRecorder:
    """Turns what her client receives into attributed observations.

    Each ``on_*`` method returns the observations to store (possibly none);
    ``dropped`` is called with a ``DROP_REASONS`` entry for each item the
    filter refused. ``content`` returns the authored-text index or None.

    It fails closed: a line is kept only when it is certainly the source's
    own answer: authored text (its hash is in the index), a fixed server
    phrasing that answers the one action it consumes and names the authored
    source where it reached, or an examine of an item whose shown name is
    authored, every line of a known form.
    """

    def __init__(self, content: Callable[[], AuthoredContent | None] = lambda: None,
                 dropped: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.time):
        self.content = content
        self.dropped = dropped or (lambda reason: None)
        self.clock = clock
        self._actions: deque[_Action] = deque(maxlen=16)
        self._examine: _Examine | None = None
        self._last_square: tuple[str, int, int] | None = None
        self._square: tuple[str, int, int] | None = None
        # Other characters she has seen in this process (chat senders, /who,
        # players in view): folded name to when last seen. Never stored, and
        # empty again after a restart; it only drops lines naming them.
        self.player_names: dict[str, float] = {}
        self._patterns: dict[str, re.Pattern] = {}

    # Her own actions.

    def note_move(self, state: GameState, direction: int) -> None:
        dx, dy = c.DIRECTION_DELTAS.get(direction, (0, 0))
        m = state.map
        self._actions.append(_Action(self.clock(), "move", m.path, (m.world_x, m.world_y),
                                     (m.world_x + dx, m.world_y + dy)))

    def note_apply(self, state: GameState, item: Item | None) -> None:
        """An apply: of an object under her (a lever, a sign) or one she carries."""
        if item is None:
            return
        m = state.map
        here = (m.world_x, m.world_y)
        self._actions.append(_Action(self.clock(), "apply", m.path, here, here,
                                     item.name, item.item_type, ground=item.location == 0))

    def note_examine(self, state: GameState, item: Item | None) -> None:
        if item is None:
            return
        m = state.map
        here = (m.world_x, m.world_y)
        self._actions.append(_Action(self.clock(), "examine", m.path, here, here,
                                     item.name, item.item_type))

    def note_position(self, state: GameState) -> list[Observation]:
        m = state.map
        square = (m.path, m.world_x, m.world_y)
        if square != self._square:
            self._last_square, self._square = self._square, square
        self._remember_view(state)
        return self.flush(state)

    def _recent(self, now: float) -> list[_Action]:
        return [a for a in self._actions if 0 <= now - a.at <= WINDOW]

    def _consume(self, action: _Action) -> None:
        try:
            self._actions.remove(action)
        except ValueError:
            pass

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

    def _remember_view(self, state: GameState) -> set[str]:
        """Players in view now, remembered for as long as the process runs."""
        names = self._players_in_view(state)
        now = self.clock()
        for name in names:
            self._remember_player(name, now)
        return names

    def _names_another(self, text: str, state: GameState) -> bool:
        folded = plain(text)
        own = plain(state.player_name)
        self._remember_view(state)
        for name in self.player_names:
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
            # Her name split by markup ("[b]Se[/b]ra") is not replaced: drop.
            if _name_pattern(plain(own)).search(plain(text)):
                self.dropped("player-name")
                return None
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
        consumed, finished = self._examine_line(state, msg_type, color, text, now)
        if finished is not None:
            out.append(finished)
        if consumed:
            return out
        observation = self._attribute(state, msg_type, color, text, now)
        if observation is not None:
            out.append(observation)
        return out

    # Examine answers (types/player.c examine): "That is ...", lines of the
    # forms below, and a blank line last. Any other line, or no blank line
    # in time, drops the whole answer.
    def _examine_line(self, state: GameState, msg_type: int, color: str, text: str,
                      now: float) -> tuple[bool, Observation | None]:
        """Collect an examine answer: whether the line belongs to one, and
        the finished answer when this line completed it."""
        stripped = normalize(text)
        is_game = msg_type == c.CHAT_TYPE_GAME and color == WHITE
        group = self._examine
        if group is not None and is_game and stripped.startswith("That is "):
            # The next answer began before this one's blank line.
            self._abandon_examine()
            group = None
        if group is not None:
            if not is_game:
                self._abandon_examine()
                return False, None
            if not stripped:
                return True, self._finish_examine(state)
            if group.story_next:
                group.story_next = False
                content = self.content()
                if content is None or text_hash(text) not in content.texts:
                    group.rejected = group.rejected or "examine-interrupted"
                group.lines.append(text)
            elif stripped.startswith("You name it "):
                group.rejected = "renamed-item"
            elif SOULBOUND_TO.match(stripped):
                group.rejected = "player-name"
            elif stripped == "The object has a story:":
                group.story_next = True
                group.lines.append(stripped)
            elif any(pattern.match(stripped) for pattern in EXAMINE_LINES):
                group.lines.append(stripped)
            else:
                # Not examine output (a broadcast, a combat line): the
                # answer can no longer be told apart from it.
                self._abandon_examine()
                return False, None
            group.last = now
            return True, None
        if not (is_game and stripped.startswith("That is ")):
            return False, None
        # Answers come in the order she asked: the oldest unanswered examine.
        action = next((a for a in self._recent(now) if a.kind == "examine"), None)
        if action is None:
            return False, None
        self._consume(action)
        m = state.map
        self._examine = _Examine(now, now, action.item_name, m.path,
                                 m.world_x, m.world_y, [stripped],
                                 item_type=action.item_type)
        if "(signed; permanently finalized)" in stripped:
            self._examine.rejected = "signed-book"
        return True, None

    def _finish_examine(self, state: GameState) -> Observation | None:
        group, self._examine = self._examine, None
        if group is None:
            return None
        if group.rejected:
            self.dropped(group.rejected)
            return None
        # The shown name must be an authored one: a player can title a
        # book or a container, and that text is never kept. The row names
        # the item by its authored name, not by the line she received.
        # A player can name a book anything, an authored name included
        # (book_edit.c): books are kept from their packets, never examines.
        if group.item_type == c.TYPE_BOOK:
            self.dropped("book-examine")
            return None
        content = self.content()
        if content is None:
            self.dropped("examine-unverified")
            return None
        name = authored_item_name(content, group.lines[0][len("That is "):])
        if not name or "corpse" in name:
            self.dropped("unauthored-item")
            return None
        text = self._clean("\n".join([f"That is {name}", *group.lines[1:]]), state)
        if text is None:
            return None
        return Observation(group.at, group.map, group.x, group.y, "examine",
                           f"examine:{name}", name, "exact", False, text,
                           WHITE, c.CHAT_TYPE_GAME)

    def _attribute(self, state: GameState, msg_type: int, color: str, text: str,
                   now: float) -> Observation | None:
        m = state.map
        recent = self._recent(now)
        content = self.content()
        stripped = normalize(text)
        # Authored text from a source on or next to her square (or the one
        # she just left): the only way sign, door and magic mouth text is kept.
        if content is not None and m.path:
            digest = text_hash(text)
            if digest in content.texts:
                squares = {(m.world_x + dx, m.world_y + dy)
                           for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
                if self._last_square and self._last_square[0] == m.path:
                    squares.add(self._last_square[1:])
                matches = {s.key: s for x, y in squares for s in content.at(m.path, x, y)
                           if s.msg_hash == digest}
                if len(matches) == 1:
                    source = next(iter(matches.values()))
                    own = next((a for a in recent if a.kind in ("move", "apply") and
                                a.map == m.path and
                                (source.x, source.y) in (a.origin, a.target)), None)
                    on_square = (source.x, source.y) == (m.world_x, m.world_y) or (
                        self._last_square == (m.path, source.x, source.y))
                    if own is not None:
                        self._consume(own)
                        attribution = "timing"
                    elif on_square or not self._remember_view(state):
                        attribution = "content-match"
                    else:
                        # A broadcast another player may have caused (sign.c
                        # trigger_func): never a hint of hers.
                        return None
                    return self._observation(state, now, source.map, source.kind,
                                             source.key, source.name, attribution, True,
                                             text, color, msg_type)
        # Fixed server phrasings, each the answer to one action it consumes,
        # and only when an authored source of that kind and name stands
        # where the action reached. With two bumps or two applies in the
        # window, which one the line answers is unknown: nothing is kept.
        if color == WHITE and content is not None and not NOT_DOOR.search(stripped):
            kinds = [(a.kind, a.ground) for a in recent]
            for action in recent:
                if action.map != m.path or kinds.count((action.kind, action.ground)) != 1:
                    continue
                source = None
                if action.kind == "move":
                    opened = DOOR_OPENED.match(stripped) or DOOR_OPENED_FOR_YOU.match(stripped)
                    # The key's name, when the phrase gives one, must be
                    # authored too (key.c names it by its base name).
                    key = opened.groupdict().get("key") if opened else None
                    if opened and (key is None or authored_item_name(content, key)):
                        tx, ty = action.target
                        source = self._source_named(
                            content, m.path, {(tx + dx, ty + dy) for dx in (-1, 0, 1)
                                              for dy in (-1, 0, 1)},
                            ("door", "gate"), opened.group("name"))
                elif action.kind == "apply" and action.ground:
                    named = next((p.match(stripped) for p in LEVER_PHRASES
                                  if p.match(stripped)), None)
                    if action.item_type == TYPE_HANDLE and named:
                        source = self._source_named(content, m.path, {action.target},
                                                    ("lever",), named.group("name"))
                    elif action.item_type == TYPE_SIGN and stripped.casefold() in SIGN_PHRASES:
                        source = self._source_named(content, m.path, {action.target},
                                                    ("sign",), None)
                if source is not None:
                    self._consume(action)
                    return self._observation(state, now, m.path, source.kind, source.key,
                                             source.name, "timing", False, text, color,
                                             msg_type)
        if stripped.casefold() in FACT_LINES and m.path:
            return self._observation(state, now, m.path, "system", f"system:{m.path}", "",
                                     "none", False, text, color, msg_type)
        return None

    @staticmethod
    def _source_named(content: AuthoredContent, map_path: str,
                      squares: set[tuple[int, int]], kinds: tuple[str, ...],
                      name: str | None) -> AuthoredSource | None:
        """The one authored source of these kinds (and name) on these squares."""
        found = {s.key: s for x, y in squares for s in content.at(map_path, x, y)
                 if s.kind in kinds and (name is None or
                                         s.name.casefold() == normalize(name).casefold())}
        return next(iter(found.values())) if len(found) == 1 else None

    def _observation(self, state: GameState, now: float, map_path: str, kind: str,
                     key: str, name: str, attribution: str, authored: bool,
                     text: str, color: str, msg_type: int | None) -> Observation | None:
        """An observation of ``key``'s source; the row records where she stood."""
        cleaned = self._clean(text, state)
        if cleaned is None:
            return None
        m = state.map
        return Observation(now, map_path or m.path, m.world_x, m.world_y, kind, key,
                           normalize(MARKUP.sub("", name)), attribution, authored,
                           cleaned, color, msg_type)

    def flush(self, state: GameState, now: float | None = None) -> list[Observation]:
        """Drop an examine answer whose blank last line never came."""
        now = self.clock() if now is None else now
        if self._examine is not None and now - self._examine.last > WINDOW:
            self._abandon_examine()
        return []

    def _abandon_examine(self) -> None:
        group, self._examine = self._examine, None
        if group is not None:
            self.dropped(group.rejected or "examine-interrupted")

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
        # The received title is never kept: a player can retitle an unsigned
        # copy of an authored book. The row carries the authored title.
        title = content.book_title(digest)
        # The normalized form that matched, not the received body: its
        # spacing is the sender's, a side channel.
        text = self._clean(normalize(page.body), state)
        if text is None:
            return out
        m = state.map
        out.append(Observation(now, m.path, m.world_x, m.world_y, kind,
                               f"book:{title}:{digest}", title, "exact", True, text))
        return out
