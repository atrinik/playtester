"""The first feedback producers (docs/LEARNING_AND_FEEDBACK.md 4.11.5, F5).

Each producer is a function of what she already keeps: a stored
observation row (authored text only, after the ingestion filter), her own
stats and skills, and her own gear's effective stats (equipment.py). None
reads chat, the network or another player's text. They return ``Finding``s
for ``feedback.note``:

- **"+N with no effect"** (4.8.3): an examined item of hers whose name shows
  "+N" on a type and base that gain nothing from it. One aggregate
  ``clarity`` draft for ``atrinik/classic`` lists the families she met.
- **Book experience at low Int** (4.11.2): an authored book she read at Int
  10 or lower, where ``book_exp_mod`` is negative. One ``bug`` draft for
  ``atrinik/classic``.
- **Typos** in authored lock and sign text, from a small list of common
  slips. One ``clarity`` draft per source for ``atrinik/content``.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Collection, Iterable

from . import constants as c
from .feedback import Finding, excerpt, name, prose, utc_day
from .observations import MARKUP

# The locked inputs and this build, for each draft's "Where".
_VERSIONS: dict[str, str] | None = None
LOCK_PATH = Path(__file__).with_name("dependencies.lock.json")


def versions() -> dict[str, str]:
    """Locked content and Classic server releases and this playtester's
    build. The server's is the locked source release: her client cannot
    read the live server's build. The lock is read as JSON, not through
    the downloader module."""
    global _VERSIONS
    if _VERSIONS is None:
        found: dict[str, str] = {}
        try:
            inputs = json.loads(LOCK_PATH.read_text(encoding="utf-8"))["inputs"]
            for key, lock_key in (("content", "content"), ("server", "classic_server")):
                release = inputs.get(lock_key, {}).get("release")
                if isinstance(release, str) and release:
                    found[key] = release
        except Exception:
            pass
        try:
            from .build_info import build_info
            build = build_info()
            if build.get("commit"):
                found["playtester"] = (f"{build['version'] or 'unreleased'} "
                                       f"({build['short_commit']})")
        except Exception:
            pass
        _VERSIONS = found
    return dict(_VERSIONS)


def public_map(path: Any) -> str | None:
    """A map path a draft may name: an authored map path ("/..."), never a
    player's unique map (``/players/``, an apartment) or anything else the
    client was told."""
    path = str(path or "")
    if not path.startswith("/") or "/players/" in path or path.startswith("/players"):
        return None
    return path


def _stat(stats: dict[str, Any], key: str) -> int | None:
    value = stats.get(key)
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def state_parts(stats: dict[str, Any], extra: Iterable[str] = ()) -> list[Any]:
    """Her own state: level, extras such as "Int 9", health."""
    parts = []
    level = _stat(stats, "level")
    if level:
        parts.append(f"Level {level}")
    parts.extend(extra)
    hp, maxhp = _stat(stats, "hp"), _stat(stats, "maxhp")
    if maxhp:
        parts.append(f"HP {hp or 0}/{maxhp}")
    return prose("; ".join(parts) + ".") if parts else []


# "+N with no effect" (4.8.3).
PLUS_N_SUBJECT = "presentation:plus-n-no-effect"
PLUS_N_CLAIM = "an item's name shows +N where its type and base stats gain nothing from it"
# EffectiveStats notes (equipment.effective_stats) that say exactly that.
NO_EFFECT_NOTES = (re.compile(r"^[+-]\d+ on .+ adds no AC: "),
                   re.compile(r"^[+-]\d+ on .+ adds nothing: "))


def plus_n_no_effect(entries: Iterable[tuple[str, int, int, Any]], *,
                     stats: dict[str, Any], at: float) -> Finding | None:
    """One aggregate finding from her gear: ``entries`` are (family name,
    item type, "+N", EffectiveStats) of identified items she carries. An
    entry counts when its effective stats come from her examine (an
    artifact can add base stats the archetype lacks; a light needs none,
    since it adds nothing at all) and a note says the "+N" adds nothing.
    The family is the authored archetype name (or the generic family), never
    the shown name, which a player could have set."""
    items = []
    for family, item_type, magic, effective in entries:
        if not magic or effective is None or not family:
            continue
        if effective.source != "examine" and item_type != c.TYPE_LIGHT_APPLY:
            continue
        if not any(p.match(note) for note in effective.notes for p in NO_EFFECT_NOTES):
            continue
        why = ("a light gives no stats when worn" if item_type == c.TYPE_LIGHT_APPLY
               else "it has no base AC, WC, damage, block or absorb, and examine shows none")
        items.append({"key": f"{family}|{magic:+d}",
                      "text": prose(name(family), f" shown as {magic:+d}: {why}.")})
    if not items:
        return None
    families = sorted({item["key"].split("|")[0] for item in items})
    return Finding(
        kind="clarity", repo="atrinik/classic", subject=PLUS_N_SUBJECT, claim=PLUS_N_CLAIM,
        title="Item names show “+N” on gear it adds nothing to in combat",
        evidence={
            "what": prose(
                "Her identified gear shows a magic bonus (“+N”) in its name "
                "on items whose type and base stats gain nothing from it. The bonus "
                "adds only to AC, WC, damage, block and absorb the item already has "
                "(a launcher's goes to its arrows), so on these items it means a "
                "lighter weight and a higher price, not a combat bonus, and nothing "
                "on the item says so."),
            "where": prose("Items she carried; the server's item names and descriptions."),
            "versions": versions(),
            "tried": [prose(f"{utc_day(at)}: examined her gear; ",
                            *[part for index, family in enumerate(families)
                              for part in ((", " if index else ""), name(family))],
                            " showed a “+N” with no combat effect.")],
            "items": items,
            "expected": prose(
                "Either no “+N” on such items, or a description that says "
                "what the bonus does on them."),
            "server": [
                prose(name("server/src/living.c"), " lines 564-593 add the bonus only "
                      "to a non-zero base AC, block, absorb, WC and damage."),
                prose(name("server/src/treasure.c"), " lines 833-854 make an item with "
                      "a bonus lighter and more valuable."),
                prose(name("server/src/item.c"), " lines 1003-1011 describe the base "
                      "values without the bonus."),
            ],
            "state": state_parts(stats),
        },
        labels=("enhancement", "component: server"), issue_type="Feature",
        occurrence=f"{','.join(families)}|{time.strftime('%Y-%m-%d', time.gmtime(at))}",
    )


# Book experience at low Int (4.11.2): types/book.c book_exp_mod, Int 0-10.
BOOK_EXP_MOD = (-3.00, -2.00, -1.90, -1.80, -1.70, -1.60, -1.50, -1.40, -1.30,
                -1.20, -1.10)
BOOK_SUBJECT = "formula:book_exp_mod"
BOOK_CLAIM = "book_exp_mod is negative below Int 11, so a book gives no experience"


def book_experience(row: dict[str, Any], *, stats: dict[str, Any], literacy: int | None,
                    players_present: int = 0) -> Finding | None:
    """A finding when she read an authored book (a stored ``book``
    observation) at an Int whose ``book_exp_mod`` is negative."""
    if row.get("source_kind") != "book" or not row.get("authored"):
        return None
    intelligence = _stat(stats, "int")
    if intelligence is None or not 0 <= intelligence < len(BOOK_EXP_MOD):
        return None
    at = float(row.get("at") or row.get("last_at") or time.time())
    factor = BOOK_EXP_MOD[intelligence]
    title = str(row.get("source_name") or "")
    book = ["the book ", name(title)] if title else ["an authored book"]
    where = public_map(row.get("map"))
    extra = [f"Int {intelligence}"] + ([f"Literacy {literacy}"] if literacy else [])
    return Finding(
        kind="bug", repo="atrinik/classic", subject=BOOK_SUBJECT, claim=BOOK_CLAIM,
        title="Books give no experience below Int 11: book_exp_mod is negative",
        evidence={
            "what": prose(
                "Reading a book at Int 10 or lower gives no experience at all, and "
                "the book's one-time experience is used up. ", name("book_exp_mod"),
                " is negative for Int 0 to 10, so the book's experience becomes "
                "negative, which counts as none, and the book is still marked read. "
                "The table's comment says low Int should give less experience, not "
                "none; the values read as if their sign were wrong."),
            "where": (prose("She read ", *book, " on map ", name(where), ".") if where
                      else prose("She read ", *book, ".")),
            "versions": versions(),
            "tried": [prose(f"{utc_day(at)}: read ", *book, f" at Int {intelligence}.")],
            "notes": [prose(f"At Int {intelligence} the factor is ",
                            name(f"{factor:.2f}"), ".")],
            "expected": prose("Less experience from books at low Int, not none."),
            "server": [
                prose(name("server/src/types/book.c"), " lines 73-76 (", name("book_exp_mod"),
                      "): -3.00 at Int 0 rising to -1.10 at Int 10, then 1.00 from Int 11."),
                prose(name("server/src/types/book.c"), " line 163 multiplies the book's "
                      "experience by it; line 175 marks the book read."),
                prose(name("server/src/skill_util.c"), " lines 192-194: ",
                      name("calc_skill_exp"), " returns 0 for negative experience."),
            ],
            "state": state_parts(stats, extra),
            "players_present": players_present,
        },
        labels=("bug", "component: server"), issue_type="Bug",
        occurrence=f"book:{row.get('text_hash') or title}",
    )


# Typos (4.11.5): a small list of common slips, matched as whole words in
# authored lock and sign text with markup removed.
TYPO_SOURCES = {"door": "door", "gate": "gate", "lever": "lever", "sign": "sign",
                "magic_mouth": "magic mouth"}
_SMALL_WORDS = "a|an|the|to|is|of|and|you|it|in|on|be|for|with|are|was"
TYPO_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(rf"\b({_SMALL_WORDS})\s+\1\b", re.I), "a repeated word"),
    (re.compile(r"\b(?:a|an)\s+(?:the|a|an)\b", re.I), "two articles"),
)
MISSPELLINGS = {
    "teh": "the", "recieve": "receive", "recieved": "received", "seperate": "separate",
    "untill": "until", "wich": "which", "thier": "their", "occured": "occurred",
    "definately": "definitely", "accross": "across", "acheive": "achieve",
    "beleive": "believe", "existance": "existence", "wierd": "weird", "alot": "a lot",
    "tommorow": "tomorrow", "begining": "beginning", "sucess": "success",
    "neccessary": "necessary", "truely": "truly", "noone": "no one",
    "posession": "possession", "gaurd": "guard", "entrence": "entrance",
    "dissapear": "disappear", "dissapeared": "disappeared", "occassion": "occasion",
}
_WORD = re.compile(r"[A-Za-z']+")


def find_typos(text: str) -> list[tuple[str, str]]:
    """(slip as written, what it is) for each slip, in order, once each."""
    plain = " ".join(MARKUP.sub("", str(text)).split())
    found: dict[str, str] = {}
    for pattern, what in TYPO_PATTERNS:
        for match in pattern.finditer(plain):
            found.setdefault(match.group(0), what)
    for match in _WORD.finditer(plain):
        correct = MISSPELLINGS.get(match.group(0).casefold())
        if correct:
            found.setdefault(match.group(0), f"probably “{correct}”")
    return list(found.items())


_SOURCE_KEY = re.compile(r"^(?P<prefix>[a-z_]+):(?P<map>/[^:]*):(?P<x>-?\d+),(?P<y>-?\d+)$")


def typo(row: dict[str, Any], *, names: Collection[str] = (), stats: dict[str, Any] | None = None,
         players_present: int = 0) -> Finding | None:
    """A finding for slips in an authored lock or sign text (a stored
    observation of a door, gate, lever, sign or magic mouth whose text is
    exactly authored). ``names`` are other characters she has seen: excerpt
    lines naming one are dropped."""
    label = TYPO_SOURCES.get(str(row.get("source_kind") or ""))
    if label is None or not row.get("authored"):
        return None
    key = str(row.get("source_key") or "")
    where = _SOURCE_KEY.match(key)
    if where is None or public_map(where.group("map")) is None:
        return None
    # Slips only from the lines that may be quoted: none from a dropped one.
    lines = excerpt([str(row.get("text") or "")], names)
    slips = find_typos("\n".join(lines))
    if not slips:
        return None
    map_path, x, y = where.group("map"), int(where.group("x")), int(where.group("y"))
    at = float(row.get("at") or row.get("last_at") or time.time())
    listed = [part for index, (slip, what) in enumerate(slips)
              for part in (("; " if index else ""), name(slip), f" ({what})")]
    return Finding(
        kind="clarity", repo="atrinik/content", subject=key, claim="a typo in its text",
        title=f"Typo in the {label} text at {map_path} ({x},{y})",
        evidence={
            "what": prose(f"The {label}'s text has {'a slip' if len(slips) == 1 else 'slips'}: ",
                          *listed, "."),
            "where": prose("Map ", name(map_path), f" ({x},{y})."),
            "versions": versions(),
            "tried": [prose(f"{utc_day(at)}: read the {label}'s text.")],
            "excerpts": lines,
            "expected": prose("The same text without the slip."),
            "state": state_parts(stats or {}),
            "players_present": players_present,
        },
        labels=("bug", "maps"), issue_type="Bug",
        occurrence=time.strftime("%Y-%m-%d", time.gmtime(at)),
    )
