"""Deterministic fact parsers over her stored observations, and tile facts.

docs/LEARNING_AND_FEEDBACK.md 4.3.2, phase F2 ("F2 as built" there records
how this differs from the design). Each parser is a pure function of one
stored observation (``Observation.row()`` or a ``BotMemory.observations``
row) and the authored names she knows; none reads chat, the network or a
model. Every fact is a ``candidate`` until an outcome confirms it, except
tile facts, which a fixed server refusal confirms on first sight (4.11.1).

Facts come only from two places, so no other player's text reaches them:

- the text of an observation the ingestion filter already kept (authored
  sign, door, magic-mouth and book text, fixed server phrases);
- the fixed server refusals that ``tasks.spell_blocked_here`` attributes to
  the square of her cast (``tile_property``).

Nothing here changes what she does. ``tasks.spell_blocked_here`` reads tile
facts only when the owner sets ``persist_tile_facts``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Collection, Iterable

from .observations import MARKUP, normalize

# Fact kinds (4.2), and the dashboard's order.
KINDS = ("tile-property", "key-name", "key-holder", "lock-colour", "npc-key",
         "password", "direction", "trap-warning", "weakness", "immunity",
         "quest-gate", "quest-part", "unopenable", "generic-lock")
STATUSES = ("candidate", "confirmed", "refuted", "expired")

# Sources whose text is a lock's own hint, and sources she reads.
LOCK_SOURCES = frozenset({"door", "gate", "lever", "sign", "magic_mouth"})
READING_SOURCES = frozenset({"book", "scroll", "npc", "journal"})
TEXT_SOURCES = LOCK_SOURCES | READING_SOURCES

# The parser set: an observation it has read is not read again on a repeat
# (observations.facts_version). Bump it whenever a parser changes.
VERSION = "facts/1"

# Tile facts (4.11.1) lapse after a week unless a refusal refreshes them,
# and count only under the content revision they were seen with.
TILE_FACT_SECONDS = 7 * 24 * 3600.0
# With persist_tile_facts on, a remembered square or map is tried again
# with one ordinary cast this long after it was last confirmed or tried
# (and only when she is not hurt badly, TILE_PROBE_MIN_HEALTH); no refusal
# within the attribution window refutes the fact, a refusal refreshes it.
TILE_PROBE_SECONDS = 6 * 3600.0
TILE_PROBE_WINDOW_SECONDS = 60.0
TILE_PROBE_MIN_HEALTH = 0.5
TILE_PROBE_ANSWER_SECONDS = 10.0
# The longest a tile-fact decision waits to save the decision ledger.
TILE_DECISION_SAVE_SECONDS = 0.25

# Characters kept of a fact's value and of the sentence it came from.
VALUE_LIMIT = 120
DETAIL_LIMIT = 300

COLOURS = ("red", "orange", "yellow", "green", "blue", "purple", "violet",
           "white", "black", "grey", "gray", "brown", "pink", "silver",
           "golden", "gold", "bronze", "copper", "crimson", "azure")
# Attack types as the server names them (attack.c attack_name), and the
# words prose uses for some of them.
ATTACKS = {
    "impact": "impact", "slash": "slash", "cleave": "cleave", "pierce": "pierce",
    "fire": "fire", "flame": "fire", "flames": "fire",
    "cold": "cold", "frost": "cold", "ice": "cold",
    "electricity": "electricity", "lightning": "electricity",
    "poison": "poison", "acid": "acid", "magic": "magic", "mind": "mind",
    "blindness": "blind", "blind": "blind", "paralyze": "paralyze",
    "paralysis": "paralyze", "force": "force", "chaos": "chaos",
    "drain": "drain", "slow": "slow", "confusion": "confusion",
    "internal": "internal", "holy": "godpower", "godpower": "godpower",
}

_ATTACK = "|".join(sorted(ATTACKS, key=len, reverse=True))
_COLOUR = "|".join(COLOURS)
ARTICLES = re.compile(r"^(?:(?:a|an|the|this|that|some|your|my)\s+)+", re.I)
SENTENCE_END = re.compile(r"(?<=[.!?;])\s+")

# key-name and generic-lock: "You need Asteria Dungeon Key 2 to pass."
KEY_NEED = re.compile(
    r"\bYou need (?P<key>(?!to\b)[^.!?;]+?)"
    r"(?:\s+to\s+(?:open|pass|enter|unlock|get|go)\b[^.!?;]*)?\s*[.!?]*$", re.I)
GENERIC_KEYS = {"key": "unnamed-key", "special key": "special-key"}
# A lock text that names nothing: "The prison cell is locked."
LOCKED = re.compile(r"^The (?P<what>.+?) is locked(?: tight| shut)?\s*[.!]*$", re.I)
# unopenable: text that says it does not open (from here, or now).
UNOPENABLE = (
    (re.compile(r"\bnot possible to open from this side|"
                r"\bcan(?:not|'t) be opened from (?:this side|here)|"
                r"\bonly opens? from the other side", re.I), "from-this-side", 0.6),
    (re.compile(r"\bis currently (?:closed|not open)\b", re.I), "closed", 0.4),
    (re.compile(r"\bappears to be blocked\b", re.I), "blocked", 0.4),
)
# lock-colour: "The orange force field prevents you ...", "the orange stone
# giant key".
LOCK_COLOUR = re.compile(
    rf"\b(?P<colour>{_COLOUR})\s+(?:[a-z'-]+\s+){{0,3}}?"
    r"(?P<thing>force[ -]?fields?|keys?|gates?|doors?|barriers?)\b", re.I)
# key-holder: "[i]Xuhrexal, the stone giant[/i] is engraved on the door."
ENGRAVED = re.compile(
    r"\[i\](?P<name>[^\[\],]+?)(?:,\s*(?P<what>[^\[\]]+?))?\[/i\]\s+"
    r"(?:is|are)\s+(?:engraved|carved|inscribed|written|etched)\b", re.I)
# npc-key: an NPC mentions a key.
KEY_WORD = re.compile(r"\b(?P<phrase>(?:[a-z'-]+\s+){0,2}keys?)\b", re.I)
# Words dropped from the front of the phrase an NPC names a key with.
FUNCTION_WORDS = frozenset({
    "a", "an", "the", "about", "of", "for", "to", "with", "and", "or", "my", "your",
    "his", "her", "our", "their", "this", "that", "these", "those", "some", "any",
    "come", "came", "need", "want", "have", "has", "give", "get", "find", "is", "was"})
# password: the word itself when quoted, else a password with no value.
PASSWORD = re.compile(r"\bpass ?words?\b", re.I)
PASSWORD_VALUE = re.compile(
    r"\bpass ?word\s+(?:is|was|of)?\s*:?\s*[\"'“‘](?P<word>[^\"'”’]{1,40})"
    r"[\"'”’]|"
    r"\b(?:say|speak|whisper|utter)\s+(?:the\s+(?:pass ?)?word\s+)?[\"'“‘]"
    r"(?P<said>[^\"'”’]{1,40})[\"'”’]", re.I)
SPOKEN_WORD = re.compile(r"\b(?:magic|secret|pass) ?word\b", re.I)
# direction: a compass direction where something is done or found.
DIRECTION = re.compile(
    r"\b(?P<dir>(?:north|south)(?:[- ]?(?:east|west))?|east|west)(?:wards?)?\b(?!ern)", re.I)
DIRECTION_VERB = re.compile(
    r"\b(?:cast|go|goes|walk|head|travel|lies?|located|found|turn|follow|"
    r"move|search|look|dig|enter|marked)\b|\bis\b|\bare\b", re.I)
# trap-warning: a place to keep off.
TRAP = (
    re.compile(r"\b(?:stay|keep|get)\s+(?:off|away from|clear of)\s+(?:the\s+)?"
               r"(?P<place>[a-z][a-z' -]{1,40}?)(?=\s*(?:[,.!;]|$|\bit\b|\bor\b))", re.I),
    re.compile(r"\b(?:beware|be wary)(?:\s+of)?\s+(?:the\s+)?"
               r"(?P<place>[a-z][a-z' -]{1,40}?)(?=\s*(?:[,.!;]|$))", re.I),
    re.compile(r"\bthe\s+(?P<place>[a-z][a-z' -]{1,40}?)\s+is\s+a\s+(?:death\s+)?trap\b", re.I),
)
# weakness and immunity: "<creature> is weak to fire", "... immune to cold".
_WHO = r"(?P<who>[A-Za-z][\w' -]{0,60}?)"
WEAKNESS = re.compile(
    rf"{_WHO}\s+(?:is|are)\s+(?:very |extremely |especially |quite )?"
    rf"(?:weak (?:to|against)|vulnerable to|susceptible to)\s+(?P<attack>{_ATTACK})\b", re.I)
IMMUNITY = re.compile(
    rf"{_WHO}\s+(?:is|are)\s+(?:completely |totally |entirely )?"
    rf"(?:immune to\s+(?P<attack>{_ATTACK})\b|(?P<hyphen>{_ATTACK})-immune\b)", re.I)
# quest-gate: a gate, door or vault tied to a key, password or guard in prose.
GATE_PHRASE = re.compile(
    r"\b(?P<phrase>(?:(?:magic|magical|locked|enchanted|secret|iron|stone|great|"
    r"hidden|sealed) )?(?:gates?|doors?|vaults?|portals?))\b", re.I)
GATE_REASON = re.compile(
    r"\b(?:pass ?words?|keys?|guarded|protected|locked|sealed|opens?)\b", re.I)
# quest-part: places a journal part names.
PLACE = re.compile(
    r"\b(?:is|are|lies|lie|located)\s+(?:at|in|on|near|under|beyond|inside)\s+"
    r"(?P<place>the\s+[^.,;!?]{3,80})", re.I)
PROPER = re.compile(r"\b(?P<name>[A-Z][a-z'-]+(?:\s+[A-Z][a-z'-]+){0,3})")
COMMON_CAPITALS = frozenset({
    "the", "a", "an", "he", "she", "it", "they", "we", "you", "i", "his", "her",
    "their", "our", "your", "however", "but", "and", "if", "this", "that", "these",
    "those", "there", "then", "when", "while", "since", "so", "unfortunately",
    "using", "after", "before", "today", "one", "each", "all", "no"})

# tile-property: fixed server refusals tied to the square of her cast, or to
# the whole map (spell_util.c cast_spell, spell_effect.c cast_wor).
TILE_REFUSALS = (
    (re.compile(r"^Something blocks (?:your spellcasting|your spell|"
                r"the magic of your .+)\.$", re.I), "no_magic", False),
    (re.compile(r"^Powerful countermagic cancels all spellcasting here!$", re.I),
     "no_magic", True),
    (re.compile(r"^Powerful countermagic cancels all harmful magic here!$", re.I),
     "no_harm", False),
)
# The detail kept with a tile fact: only these exact phrases, never a line
# that names her item (a player can rename it).
TILE_DETAILS = frozenset({
    "something blocks your spellcasting.", "something blocks your spell.",
    "powerful countermagic cancels all spellcasting here!",
    "powerful countermagic cancels all harmful magic here!"})


@dataclass(slots=True, frozen=True)
class Fact:
    """One fact; ``BotMemory.record_fact`` takes ``row()``."""
    kind: str
    subject: str
    value: str
    confidence: float
    parser: str
    detail: str = ""
    status: str = "candidate"

    def row(self, *, at: float, observation_id: int | None = None, inputs: str = "",
            expires_at: float | None = None) -> dict[str, Any]:
        return {"kind": self.kind, "subject": self.subject[:VALUE_LIMIT * 2],
                "value": self.value[:VALUE_LIMIT],
                "confidence": max(0.0, min(1.0, float(self.confidence))),
                "status": self.status, "parser": self.parser,
                "detail": self.detail[:DETAIL_LIMIT], "observation_id": observation_id,
                "inputs": inputs, "expires_at": expires_at, "at": float(at)}


def tile_subject(map_path: str, x: int, y: int) -> str:
    """A square's subject, or the whole map's when ``x`` and ``y`` are -1."""
    return f"map:{map_path}" if (x, y) == (-1, -1) else f"tile:{map_path}:{x},{y}"


def parse_tile_subject(subject: str) -> tuple[str, int, int] | None:
    """The (map, x, y) of a tile or map subject; x = y = -1 for a whole map."""
    if subject.startswith("map:"):
        return (subject[len("map:"):], -1, -1) if len(subject) > 4 else None
    if not subject.startswith("tile:"):
        return None
    path, _, coords = subject[len("tile:"):].rpartition(":")
    x, comma, y = coords.partition(",")
    try:
        return (path, int(x), int(y)) if path and comma else None
    except ValueError:
        return None


def tile_property(text: str) -> tuple[str, bool] | None:
    """The tile property a fixed server refusal states, and whether it covers
    the whole map; None for any other line."""
    stripped = normalize(text)
    for pattern, value, whole_map in TILE_REFUSALS:
        if pattern.match(stripped):
            return value, whole_map
    return None


def tile_fact(map_path: str, x: int, y: int, value: str, text: str) -> Fact:
    """The confirmed fact a fixed refusal at this square (or map) gives."""
    detail = normalize(text) if normalize(text).casefold() in TILE_DETAILS else ""
    return Fact("tile-property", tile_subject(map_path, x, y), value, 1.0,
                "tile-property/1", detail, "confirmed")


def _plain(text: str) -> str:
    return normalize(MARKUP.sub("", text))


def _sentences(text: str) -> list[str]:
    return [s for s in (normalize(part) for part in SENTENCE_END.split(_plain(text))) if s]


def _bare(phrase: str) -> str:
    return normalize(ARTICLES.sub("", normalize(phrase)))


def _known(name: str, names: Collection[str] | None) -> bool:
    return bool(names) and normalize(name).casefold() in names


@dataclass(slots=True, frozen=True)
class _Context:
    kind: str
    subject: str
    text: str
    names: Collection[str] | None


def _key_name(ctx: _Context) -> Iterable[Fact]:
    if ctx.kind not in LOCK_SOURCES:
        return
    for sentence in _sentences(ctx.text):
        match = KEY_NEED.search(sentence)
        if not match:
            continue
        key = _bare(match.group("key"))
        folded = key.casefold()
        if folded in GENERIC_KEYS or not key or len(key.split()) > 8:
            continue
        yield Fact("key-name", ctx.subject, key, 0.8 if _known(key, ctx.names) else 0.6,
                   "key-name/1", sentence)


def _generic_lock(ctx: _Context) -> Iterable[Fact]:
    if ctx.kind not in LOCK_SOURCES:
        return
    for sentence in _sentences(ctx.text):
        need = KEY_NEED.search(sentence)
        if need and _bare(need.group("key")).casefold() in GENERIC_KEYS:
            yield Fact("generic-lock", ctx.subject,
                       GENERIC_KEYS[_bare(need.group("key")).casefold()], 0.9,
                       "generic-lock/1", sentence)
        elif LOCKED.match(sentence):
            yield Fact("generic-lock", ctx.subject, "locked", 0.9, "generic-lock/1", sentence)


def _unopenable(ctx: _Context) -> Iterable[Fact]:
    if ctx.kind not in LOCK_SOURCES:
        return
    for sentence in _sentences(ctx.text):
        for pattern, value, confidence in UNOPENABLE:
            if pattern.search(sentence):
                yield Fact("unopenable", ctx.subject, value, confidence,
                           "unopenable/1", sentence)


def _lock_colour(ctx: _Context) -> Iterable[Fact]:
    if ctx.kind not in TEXT_SOURCES:
        return
    for sentence in _sentences(ctx.text):
        for match in LOCK_COLOUR.finditer(sentence):
            colour = match.group("colour").casefold()
            colour = {"gray": "grey", "golden": "gold"}.get(colour, colour)
            yield Fact("lock-colour", ctx.subject, colour, 0.6, "lock-colour/1", sentence)


def _key_holder(ctx: _Context) -> Iterable[Fact]:
    if ctx.kind not in TEXT_SOURCES:
        return
    # The markup carries the name, so read the text before it is stripped.
    for match in ENGRAVED.finditer(normalize(ctx.text)):
        name = normalize(match.group("name"))
        what = normalize(match.group("what") or "")
        yield Fact("key-holder", ctx.subject, name, 0.8 if _known(name, ctx.names) else 0.6,
                   "key-holder/1", f"{name}, {what}" if what else name)


def _npc_key(ctx: _Context) -> Iterable[Fact]:
    if ctx.kind != "npc":
        return
    for sentence in _sentences(ctx.text):
        match = KEY_WORD.search(sentence)
        if match:
            words = match.group("phrase").casefold().split()
            while len(words) > 1 and words[0] in FUNCTION_WORDS:
                words.pop(0)
            yield Fact("npc-key", ctx.subject, " ".join(words), 0.5, "npc-key/1", sentence)


def _password(ctx: _Context) -> Iterable[Fact]:
    if ctx.kind not in TEXT_SOURCES:
        return
    for sentence in _sentences(ctx.text):
        if not (PASSWORD.search(sentence) or SPOKEN_WORD.search(sentence)):
            continue
        match = PASSWORD_VALUE.search(sentence)
        word = normalize((match.group("word") or match.group("said")) if match else "")
        if word or PASSWORD.search(sentence):
            # A password with no value is a fact too: the text promises one.
            yield Fact("password", ctx.subject, word, 0.6 if word else 0.4,
                       "password/1", sentence)


def _direction(ctx: _Context) -> Iterable[Fact]:
    if ctx.kind not in TEXT_SOURCES:
        return
    for sentence in _sentences(ctx.text):
        if not DIRECTION_VERB.search(sentence):
            continue
        for match in DIRECTION.finditer(sentence):
            direction = re.sub(r"[- ]", "", match.group("dir").casefold())
            yield Fact("direction", ctx.subject, direction, 0.5, "direction/1", sentence)


def _trap_warning(ctx: _Context) -> Iterable[Fact]:
    if ctx.kind not in TEXT_SOURCES:
        return
    for sentence in _sentences(ctx.text):
        for pattern in TRAP:
            for match in pattern.finditer(sentence):
                place = _bare(match.group("place")).casefold()
                if place and len(place.split()) <= 4:
                    yield Fact("trap-warning", ctx.subject, place, 0.6,
                               "trap-warning/1", sentence)


def _creature(who: str, names: Collection[str] | None) -> str:
    """The longest known creature name that ends ``who``, or ""."""
    if not names:
        return ""
    words = _bare(who).casefold().split()
    for start in range(len(words)):
        candidate = " ".join(words[start:])
        if candidate in names:
            return candidate
        # A plural ("trolls are weak to fire").
        if candidate.endswith("s") and candidate[:-1] in names:
            return candidate[:-1]
    return ""


def _weakness(ctx: _Context) -> Iterable[Fact]:
    if ctx.kind not in TEXT_SOURCES:
        return
    for sentence in _sentences(ctx.text):
        for pattern, kind in ((WEAKNESS, "weakness"), (IMMUNITY, "immunity")):
            for match in pattern.finditer(sentence):
                creature = _creature(match.group("who"), ctx.names)
                attack = match.groupdict().get("attack") or match.groupdict().get("hyphen")
                # Only a creature the content knows: "its wearer" is no subject.
                if creature and attack:
                    yield Fact(kind, creature, ATTACKS[attack.casefold()], 0.5,
                               f"{kind}/1", sentence)


def _quest_gate(ctx: _Context) -> Iterable[Fact]:
    if ctx.kind not in READING_SOURCES:
        return
    for sentence in _sentences(ctx.text):
        if not GATE_REASON.search(sentence):
            continue
        for match in GATE_PHRASE.finditer(sentence):
            yield Fact("quest-gate", ctx.subject, match.group("phrase").casefold(), 0.3,
                       "quest-gate/1", sentence)


def _quest_part(ctx: _Context) -> Iterable[Fact]:
    if ctx.kind != "journal":
        return
    for sentence in _sentences(ctx.text):
        for match in PLACE.finditer(sentence):
            yield Fact("quest-part", ctx.subject, normalize(match.group("place")).casefold(),
                       0.3, "quest-part/1", sentence)
        for match in PROPER.finditer(sentence):
            words = match.group("name").split()
            while words and words[0].casefold() in COMMON_CAPITALS:
                words.pop(0)
            name = " ".join(words).removesuffix("'s")
            if len(name) > 2:
                yield Fact("quest-part", ctx.subject, name, 0.3, "quest-part/1", sentence)


PARSERS: tuple[Callable[[_Context], Iterable[Fact]], ...] = (
    _key_name, _generic_lock, _unopenable, _lock_colour, _key_holder, _npc_key,
    _password, _direction, _trap_warning, _weakness, _quest_gate, _quest_part)


def parse(observation: dict[str, Any], names: Collection[str] | None = None) -> list[Fact]:
    """The facts one stored observation states, deduplicated.

    ``observation`` is ``Observation.row()`` or a ``BotMemory.observations``
    row; ``names`` the authored names she knows (``AuthoredContent.names``,
    case folded), or None without the index. System lines and examines
    give no text facts: tile facts come from ``tile_fact``.
    """
    kind = str(observation.get("source_kind") or "")
    subject = str(observation.get("source_key") or "")
    text = str(observation.get("text") or "")
    if not subject or not text or kind not in TEXT_SOURCES:
        return []
    ctx = _Context(kind, subject, text, names)
    seen: set[tuple[str, str, str]] = set()
    out: list[Fact] = []
    for parser in PARSERS:
        for fact in parser(ctx):
            key = (fact.kind, fact.subject, fact.value)
            if key not in seen:
                seen.add(key)
                out.append(fact)
    return out
