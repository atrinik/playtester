"""The playtester's feedback queue: deduplicated drafts with evidence.

docs/LEARNING_AND_FEEDBACK.md 4.10, phase F5 ("F5 as built" there records
how this differs from the design). A producer turns something she saw into
a ``Finding``; ``note`` fingerprints it, merges it into the stored draft of
the same fingerprint (or starts one) and renders the draft's body from its
evidence with one template.

Nothing here leaves the machine. This module opens no socket and imports
no network library: drafts are rows in her memory database, shown on the
dashboard's Feedback tab and by ``atrinik-playtester feedback list|show``.
Approval and filing are phase F6.

Privacy (4.10.4): drafts carry only authored text and her own state.

- Text from the game appears only as excerpts, in a fenced code block whose
  fence is longer than any backtick run in it, with ``<!--``, ``-->`` and
  control characters removed, so a ``@name``, ``#123``, link or forged
  fingerprint line in it is inert. Names (items, books, maps) appear only
  as inline code, the same way.
- Every excerpt line naming another character she has seen is dropped, not
  masked.
- Before a draft is stored, its title and body are checked against every
  private term (her account, character and server address, the invite
  code, other characters' names): a draft containing one is not stored.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Collection, Iterable

log = logging.getLogger(__name__)

KINDS = ("bug", "hint", "balance", "qol", "clarity")
REPOS = ("atrinik/content", "atrinik/classic", "atrinik/playtester")
STATUSES = ("draft", "approved", "checking", "needs-review", "filing", "filed",
            "filed-unlabelled", "rejected", "duplicate", "snoozed", "failed")

# Excerpts: at most this many lines and characters in a draft (4.10.3).
EXCERPT_LINES = 10
EXCERPT_CHARS = 1500
# Lists merged across occurrences, newest kept: what she tried, and the
# items of an aggregate draft.
TRIED_LIMIT = 10
ITEMS_LIMIT = 50

FINGERPRINT_PREFIX = "Playtester fingerprint: "
FOOTER = "Filed by the Atrinik playtester after owner review."
SHORT_FINGERPRINT = re.compile(r"^pt-[0-9a-f]{12}$")

# Removed from anything rendered: HTML comment delimiters (a comment would
# hide text from the reader but not from the filer), control characters
# and the invisible formatting characters that reorder or hide text.
_COMMENT = re.compile(r"<!--|-->")
_INVISIBLE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u00ad\u061c\u180e\u200b-\u200f\u2028-\u202e"
    "\u2060-\u2069\ufeff\U000e0000-\U000e007f]")
# Game markup ("[b]", "[a=#charname]"): removed before names are compared.
_MARKUP = re.compile(r"\[/?[a-z]+(?:=[^\]]*)?\]", re.I)
_BACKTICKS = re.compile(r"`+")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


class PrivateText(ValueError):
    """A draft would contain a private term; it is not stored."""


def fingerprint(kind: str, repo: str, subject: str, claim: str) -> str:
    """sha256 of (kind, repo, subject, normalized claim), hex (4.10.2)."""
    material = "\n".join((kind, repo, subject, " ".join(claim.split()).casefold()))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def short_fingerprint(digest: str) -> str:
    """The form drafts and issues show: ``pt-`` and 12 hex digits."""
    return f"pt-{digest[:12]}"


def inert(text: str) -> str:
    """Text with comment delimiters and invisible characters removed; a
    removal that forms a new delimiter ("<!<!---->") is removed too."""
    text = _INVISIBLE.sub("", str(text))
    while True:
        cleaned = _COMMENT.sub("", text)
        if cleaned == text:
            return cleaned
        text = cleaned


def _longest_run(text: str) -> int:
    return max((len(run) for run in _BACKTICKS.findall(text)), default=0)


def code(text: Any) -> str:
    """Inline code: a delimiter longer than any backtick run inside, padded
    when the text starts or ends with a backtick (CommonMark code spans)."""
    body = " ".join(inert(text).split())
    if not body:
        return "` `"
    fence = "`" * (_longest_run(body) + 1)
    pad = " " if body.startswith("`") or body.endswith("`") else ""
    return f"{fence}{pad}{body}{pad}{fence}"


def quote(lines: Iterable[str]) -> str:
    """A fenced code block holding ``lines`` verbatim, made inert."""
    body = "\n".join(inert(line).rstrip() for line in lines)
    fence = "`" * max(3, _longest_run(body) + 1)
    return f"{fence}text\n{body}\n{fence}"


def _term_pattern(term: str) -> re.Pattern:
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.I)


def fold(text: Any) -> str:
    """Text as names are compared: NFKC, invisible characters and game
    markup removed, whitespace collapsed, case folded."""
    text = unicodedata.normalize("NFKC", str(text))
    text = _MARKUP.sub(" ", _INVISIBLE.sub("", text))
    return " ".join(text.split()).casefold()


def private_terms(values: Iterable[Any]) -> list[str]:
    """The distinct, folded, non-trivial terms to keep out of drafts."""
    terms = {fold(value) for value in values if value}
    return sorted(term for term in terms if len(term) >= 2)


def names_private(text: str, terms: Collection[str]) -> str | None:
    """The first private term ``text`` contains as a word, or None; both
    are compared folded (``fold``), so "[b]Zo[/b]" or a full-width or
    zero-width-split name still matches."""
    folded = fold(inert(text))
    folded_joined = fold(_MARKUP.sub("", unicodedata.normalize("NFKC", str(text))))
    for candidate in (folded, folded_joined):
        for term in terms:
            if term in candidate and _term_pattern(term).search(candidate):
                return term
    return None


def excerpt(texts: Iterable[str], names: Collection[str]) -> list[str]:
    """Excerpt lines from authored texts: blank lines dropped, every line
    naming one of ``names`` (other characters she has seen, case folded)
    dropped whole, then at most ``EXCERPT_LINES`` lines and
    ``EXCERPT_CHARS`` characters."""
    terms = private_terms(names)
    kept: list[str] = []
    size = 0
    for text in texts:
        for line in str(text).splitlines():
            line = inert(line).strip()
            if not line or names_private(line, terms):
                continue
            room = EXCERPT_CHARS - size
            if room <= 0 or len(kept) >= EXCERPT_LINES:
                return kept
            if len(line) > room:
                line = line[:max(0, room - 1)].rstrip() + "…"
            kept.append(line)
            size += len(line)
    return kept


# Prose in evidence is a list of parts: a plain string is the playtester's
# own wording, ``{"code": text}`` a name or value from the game, rendered as
# inline code.
Part = Any


def name(text: Any) -> dict[str, str]:
    """A part holding a name or value from the game (rendered as code)."""
    return {"code": str(text)}


def prose(*parts: Part) -> list[Part]:
    return [part if isinstance(part, dict) else str(part) for part in parts]


def _parts(parts: Any) -> str:
    if isinstance(parts, str):
        parts = [parts]
    out = []
    for part in parts or ():
        if isinstance(part, dict) and "code" in part:
            out.append(code(part["code"]))
        else:
            out.append(inert(part))
    return "".join(out)


def _day(at: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(at))


def utc_day(at: float) -> str:
    """The day of a time, UTC: drafts never carry the time of day she played."""
    return time.strftime("%Y-%m-%d", time.gmtime(at))


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing a producer found: ``subject`` and ``claim`` (with kind and
    repo) are its fingerprint; ``evidence`` renders its body; a repeat with
    an ``occurrence`` key already counted adds no occurrence."""
    kind: str
    repo: str
    subject: str
    claim: str
    title: str
    evidence: dict[str, Any] = field(default_factory=dict)
    labels: tuple[str, ...] = ()
    issue_type: str = ""
    occurrence: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown feedback kind {self.kind!r}")
        if self.repo not in REPOS:
            raise ValueError(f"unknown feedback repository {self.repo!r}")
        if not self.subject or not self.claim or not self.title:
            raise ValueError("a finding needs a subject, a claim and a title")

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.kind, self.repo, self.subject, self.claim)


def merge_evidence(stored: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """The new evidence, keeping what earlier occurrences added: ``tried``
    lines (newest ``TRIED_LIMIT``) and aggregate ``items`` (by ``key``,
    newest ``ITEMS_LIMIT``)."""
    merged = dict(new)
    if not isinstance(stored, dict):
        return merged
    tried = [line for line in stored.get("tried", []) if line not in new.get("tried", [])]
    merged["tried"] = (tried + list(new.get("tried", [])))[-TRIED_LIMIT:]
    items: dict[str, Any] = {}
    for item in [*stored.get("items", []), *new.get("items", [])]:
        if isinstance(item, dict) and item.get("key"):
            items.pop(item["key"], None)
            items[item["key"]] = item
    if items:
        merged["items"] = list(items.values())[-ITEMS_LIMIT:]
    return merged


def render(evidence: dict[str, Any], *, digest: str, occurrences: int, days: int,
           first_seen: float, last_seen: float) -> str:
    """The draft body (4.10.3), from its evidence and counts."""
    out: list[str] = []

    def section(title: str, lines: list[str], block: str = "") -> None:
        for line in lines:
            # A paragraph never opens with a code span: a name there could
            # read as a fence opening to a lenient parser (producer bug).
            if line.startswith(("`", "~")):
                raise ValueError(f"a {title!r} line starts with a code span")
        if block or lines:
            out.extend((f"### {title}", *([block] if block else []), *lines, ""))

    section("What happened", [_parts(evidence.get("what"))])
    where = [_parts(evidence.get("where"))] if evidence.get("where") else []
    versions = evidence.get("versions") or {}
    shown = [f"{label} {code(versions[key])}" for key, label in (
        ("content", "Content"), ("server", "Classic server"),
        ("playtester", "playtester")) if versions.get(key)]
    if shown:
        where.append(", ".join(shown) + ".")
    section("Where", [" ".join(where)] if where else [])
    section("What she tried", [f"- {_parts(line)}" for line in evidence.get("tried", [])])
    saw: list[str] = []
    excerpts = [str(line) for line in evidence.get("excerpts", [])][:EXCERPT_LINES]
    saw.extend(f"- {_parts(item['text'])}" for item in evidence.get("items", [])
               if isinstance(item, dict) and item.get("text"))
    saw.extend(_parts(note) for note in evidence.get("notes", []))
    section("What she saw", saw, quote(excerpts) if excerpts else "")
    section("What she expected", [_parts(evidence["expected"])]
            if evidence.get("expected") else [])
    section("What the content says", [f"- {_parts(line)}"
                                      for line in evidence.get("content", [])])
    section("What the server source says", [f"- {_parts(line)}"
                                            for line in evidence.get("server", [])])
    state = _parts(evidence.get("state")) if evidence.get("state") else ""
    present = int(evidence.get("players_present", 0) or 0)
    if present:
        state = (state + " " if state else "") + (
            f"{present} other player{'s' if present != 1 else ''} in view.")
    section("Her state", [state] if state else [])
    section("Occurrences", [
        f"{occurrences} time{'s' if occurrences != 1 else ''} on {days} "
        f"day{'s' if days != 1 else ''}; first {_day(first_seen)}, last {_day(last_seen)}."])
    out.extend(("---", f"{FINGERPRINT_PREFIX}{short_fingerprint(digest)}", FOOTER))
    return "\n".join(out) + "\n"


def fingerprints_outside_code(body: str) -> list[str]:
    """The fingerprint lines of a body outside fenced code blocks: the only
    ones the filer (F6) may read, so one quoted from the game counts for
    nothing."""
    found: list[str] = []
    fence = ""
    for line in body.splitlines():
        match = _FENCE.match(line)
        if fence:
            if (match and match.group(1)[0] == fence[0] and
                    len(match.group(1)) >= len(fence) and not line.strip(" `~")):
                fence = ""
            continue
        # A backtick fence's info string has no backtick (CommonMark): a
        # line such as "```a```" is a code span, not a fence.
        if match and not (match.group(1)[0] == "`" and "`" in line[match.end():]):
            fence = match.group(1)
            continue
        if line.startswith(FINGERPRINT_PREFIX):
            found.append(line[len(FINGERPRINT_PREFIX):].strip())
    return found


def note(memory: Any, character: str, finding: Finding, *, at: float | None = None,
         private: Collection[str] = (), timeout: float = 0.25
         ) -> tuple[int, bool] | None:
    """Store ``finding`` as a new draft or another occurrence of its draft.

    Returns the row id and whether the draft is new, or None when the draft
    would contain a private term (nothing is stored). A draft's title and
    body are rendered again on each occurrence while it is a draft; a
    rejected or filed item only gains occurrences.
    """
    at = time.time() if at is None else at
    digest = finding.fingerprint
    terms = private_terms(private)

    def build(stored: dict[str, Any] | None, occurrences: int, days: int,
              first_seen: float, last_seen: float) -> dict[str, Any]:
        evidence = merge_evidence(stored, finding.evidence)
        body = render(evidence, digest=digest, occurrences=occurrences, days=days,
                      first_seen=first_seen, last_seen=last_seen)
        title = " ".join(inert(finding.title).split())
        hit = names_private(title + "\n" + body, terms)
        if hit is not None:
            raise PrivateText("a private term")
        return {"title": title, "body_md": body,
                "evidence_json": json.dumps(evidence, ensure_ascii=False,
                                            separators=(",", ":"), sort_keys=True),
                "labels_json": json.dumps(list(finding.labels)),
                "issue_type": finding.issue_type}

    try:
        return memory.record_feedback(
            character, fingerprint=digest, repo=finding.repo, kind=finding.kind,
            subject=finding.subject, at=at, occurrence=finding.occurrence,
            build=build, timeout=timeout)
    except PrivateText:
        # Never logged with the term or the text: only that it happened. A
        # stored draft that would now name one is withheld until a clean
        # render replaces it.
        log.warning("feedback draft %s not kept: it named a private term",
                    short_fingerprint(digest))
        try:
            memory.withhold_feedback_fingerprint(character, digest, timeout=timeout)
        except Exception as error:
            log.warning("feedback draft %s not withheld: %s", short_fingerprint(digest), error)
        return None


WITHHELD_TITLE = "(withheld: it names a private term)"


def screen(items: Iterable[dict[str, Any]], private: Collection[str]) -> list[int]:
    """Check stored items against the current private terms before anyone
    sees or files them (the dashboard, the CLI and the filer all call it):
    a term learned after an item was rendered, or one in an item past
    draft, is caught here. A failing or already withheld item is marked
    ``withheld`` and its title and body are blanked in place. Returns the
    ids that newly failed, for ``BotMemory.withhold_feedback``."""
    terms = private_terms(private)
    newly: list[int] = []
    for item in items:
        failed = names_private(f"{item.get('title', '')}\n{item.get('body_md', '')}", terms)
        if failed is not None and not item.get("withheld"):
            newly.append(int(item["id"]))
        if failed is not None or item.get("withheld"):
            item.update(withheld=True, title=WITHHELD_TITLE, body_md="")
        else:
            item["withheld"] = False
    return newly
