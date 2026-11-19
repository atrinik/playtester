"""The owner's route and map blocks (docs/ROUTING.md 4.7, phase P3).

The owner blocks a map, an exit or a tile seam from the loopback dashboard
for 1 hour to 7 days. While a block lasts it is a wall for transit: the
planner never enters a blocked map (except as the trip's destination) and
never takes a blocked exit or seam. Only an essential trip may cross one,
and only when every route crosses it, with care (navigation.NavigateTask's
care crossing). Blocks are the owner's data: nothing in play sets one.

Subjects follow the edge identities of ROUTING 4.2:

- ``map:{path}`` - the whole map, for transit;
- ``exit:{source}:{x},{y}>{destination}`` - one exit, in that direction;
- ``seam:{source}>{destination}`` - a tile seam, in that direction.

Rows live in memory's ``route_blocks`` table, in her (server, account,
character) namespace. The planner reads them through a small per-client
cache, refreshed every ``REFRESH_SECONDS`` and at once after a dashboard
write; an expired block stops counting at its expiry even before the next
refresh, and its row is deleted on the next read.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Iterable

log = logging.getLogger(__name__)

# The owner's range for a block's duration (seconds).
MIN_BLOCK_SECONDS = 3600
MAX_BLOCK_SECONDS = 7 * 24 * 3600
NOTE_LIMIT = 200
SUBJECT_LIMIT = 512
# The planner's cache of her blocks is re-read this often (seconds); a
# dashboard write refreshes it at once. After a failed read it is retried
# this soon instead.
REFRESH_SECONDS = 60.0
RETRY_SECONDS = 5.0
# The longest a read or write waits for a locked database (seconds): the
# planner never stalls a tick on it, and the dashboard answers promptly.
READ_TIMEOUT = 0.25
WRITE_TIMEOUT = 0.25

MAP, EXIT, SEAM = "map", "exit", "seam"
_EXIT = re.compile(r"exit:(?P<source>/[^>]*):(?P<x>-?\d+),(?P<y>-?\d+)>(?P<destination>/.+)")
_SEAM = re.compile(r"seam:(?P<source>/[^>]*)>(?P<destination>/.+)")


def map_subject(path: str) -> str:
    return f"{MAP}:{path}"


def edge_subject(edge: Any) -> str:
    """The subject of a planned edge: its seam for a tile edge, else its exit."""
    if getattr(edge, "kind", "exit") == "tile":
        return f"{SEAM}:{edge.source}>{edge.destination}"
    return f"{EXIT}:{edge.source}:{edge.x},{edge.y}>{edge.destination}"


def normalize_subject(graph: Any, text: Any) -> str:
    """The canonical subject for ``text``, checked against the content
    graph, or ValueError. A bare map path names that map."""
    if not isinstance(text, str):
        raise ValueError("the subject must be text")
    subject = text.strip()
    if not subject or len(subject) > SUBJECT_LIMIT:
        raise ValueError("the subject is empty or too long")
    nodes = getattr(graph, "nodes", {})
    if subject.startswith("/"):
        subject = map_subject(subject)
    if subject.startswith(f"{MAP}:"):
        path = subject[len(MAP) + 1:]
        if path not in nodes:
            raise ValueError(f"{path} is not an authored map path")
        return map_subject(path)
    match = _EXIT.fullmatch(subject)
    if match is not None:
        source, destination = match["source"], match["destination"]
        x, y = int(match["x"]), int(match["y"])
        node = nodes.get(source)
        if node is None or not any(
                edge.kind != "tile" and edge.destination == destination and
                (edge.x, edge.y) == (x, y) for edge in node.edges):
            raise ValueError(f"no exit from {source} at {x},{y} to {destination}")
        return f"{EXIT}:{source}:{x},{y}>{destination}"
    match = _SEAM.fullmatch(subject)
    if match is not None:
        source, destination = match["source"], match["destination"]
        node = nodes.get(source)
        if node is None or not any(
                edge.kind == "tile" and edge.destination == destination
                for edge in node.edges):
            raise ValueError(f"no tile seam from {source} to {destination}")
        return f"{SEAM}:{source}>{destination}"
    raise ValueError("the subject must be a map path, map:PATH, "
                     "exit:SOURCE:X,Y>DESTINATION or seam:SOURCE>DESTINATION")


@dataclass(frozen=True, slots=True)
class OwnerWalls:
    """The transit walls her active blocks make, for one plan."""

    maps: frozenset[str] = frozenset()
    exits: frozenset[tuple[str, str, int, int]] = frozenset()
    seams: frozenset[tuple[str, str]] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.maps or self.exits or self.seams)

    def blocks(self, edge: Any, destination: str | None) -> bool:
        """Whether taking ``edge`` crosses a block. Entering a blocked map
        that is the trip's ``destination`` does not: a block is a transit
        wall, and the map stays usable as a destination."""
        if edge.kind == "tile":
            if (edge.source, edge.destination) in self.seams:
                return True
        elif (edge.source, edge.destination, edge.x, edge.y) in self.exits:
            return True
        return edge.destination in self.maps and edge.destination != destination

    def crossings(self, route: Iterable[Any], destination: str) -> tuple[str, ...]:
        """The block subjects ``route`` crosses, in route order, once each."""
        crossed: list[str] = []
        for edge in route:
            if not self.blocks(edge, destination):
                continue
            if edge.kind == "tile" and (edge.source, edge.destination) in self.seams:
                subject = edge_subject(edge)
            elif edge.kind != "tile" and (
                    edge.source, edge.destination, edge.x, edge.y) in self.exits:
                subject = edge_subject(edge)
            else:
                subject = map_subject(edge.destination)
            if subject not in crossed:
                crossed.append(subject)
        return tuple(crossed)


NO_WALLS = OwnerWalls()


def walls_for(subjects: Iterable[str]) -> OwnerWalls:
    """The walls of these (canonical) subjects."""
    maps, exits, seams = set(), set(), set()
    for subject in subjects:
        if subject.startswith(f"{MAP}:"):
            maps.add(subject[len(MAP) + 1:])
        elif (match := _EXIT.fullmatch(subject)) is not None:
            exits.add((match["source"], match["destination"],
                       int(match["x"]), int(match["y"])))
        elif (match := _SEAM.fullmatch(subject)) is not None:
            seams.add((match["source"], match["destination"]))
    return OwnerWalls(frozenset(maps), frozenset(exits), frozenset(seams))


class _Cache:
    __slots__ = ("character", "rows", "loaded_at", "walls_key", "walls", "warned",
                 "failed")

    def __init__(self, character: str):
        self.character = character
        self.rows: list[dict[str, Any]] = []
        self.loaded_at = float("-inf")
        self.walls_key: tuple[str, ...] | None = None
        self.walls = NO_WALLS
        self.warned = False
        # The last read failed: retry after RETRY_SECONDS.
        self.failed = False


def _character(client: Any) -> str:
    return str(getattr(client, "_memory_character", "") or "")


def _cache(client: Any) -> _Cache | None:
    """Her block cache, or None for a client without memory (tests, tools)."""
    character = _character(client)
    if not character or not callable(getattr(client, "_memory_store", None)):
        return None
    cache = getattr(client, "_owner_block_cache", None)
    if not isinstance(cache, _Cache) or cache.character != character:
        cache = _Cache(character)
        try:
            client._owner_block_cache = cache
        except AttributeError:
            return None
    return cache


def _refresh(client: Any, cache: _Cache, *, force: bool = False) -> None:
    now = time.monotonic()
    interval = RETRY_SECONDS if cache.failed else REFRESH_SECONDS
    if not force and now - cache.loaded_at < interval:
        return
    cache.loaded_at = now
    try:
        cache.rows = client._memory_store().route_blocks(
            cache.character, now=time.time(), timeout=READ_TIMEOUT)
        cache.warned = cache.failed = False
    except (OSError, sqlite3.Error) as exc:
        # Keep the last rows read: a busy database never lifts a block. A
        # failed read, the first one included, is retried within seconds.
        cache.failed = True
        if not cache.warned:
            log.warning("could not read her route blocks (retrying every %.0f s): %s",
                        RETRY_SECONDS, exc)
            cache.warned = True


def active_blocks(client: Any, *, now: float | None = None,
                  refresh: bool = False) -> list[dict[str, Any]]:
    """Her blocks that have not expired, soonest expiry first."""
    cache = _cache(client)
    if cache is None:
        return []
    _refresh(client, cache, force=refresh)
    now = time.time() if now is None else now
    return [dict(row) for row in cache.rows if float(row["until"]) > now]


def active_subjects(client: Any, *, now: float | None = None) -> set[str]:
    """The subjects of her active blocks."""
    return {row["subject"] for row in active_blocks(client, now=now)}


def owner_walls(client: Any, *, now: float | None = None) -> OwnerWalls:
    """The walls her active blocks make right now (NO_WALLS without any)."""
    cache = _cache(client)
    if cache is None:
        return NO_WALLS
    _refresh(client, cache)
    now = time.time() if now is None else now
    key = tuple(sorted(row["subject"] for row in cache.rows
                       if float(row["until"]) > now))
    if key != cache.walls_key:
        cache.walls_key, cache.walls = key, walls_for(key)
    return cache.walls


def add_block(client: Any, graph: Any, subject: Any, seconds: Any, note: Any = "",
              *, now: float | None = None) -> dict[str, Any]:
    """Store the owner's block of ``subject`` for ``seconds``, or ValueError.

    A block of a subject already blocked replaces it, expiry and note."""
    character = _character(client)
    if not character:
        raise ValueError("no character is loaded yet")
    canonical = normalize_subject(graph, subject)
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise ValueError("the duration must be whole seconds")
    if not MIN_BLOCK_SECONDS <= seconds <= MAX_BLOCK_SECONDS:
        raise ValueError("a block lasts from 1 hour to 7 days")
    if not isinstance(note, str):
        raise ValueError("the note must be text")
    now = time.time() if now is None else now
    row = {"subject": canonical, "created_at": now, "until": now + seconds,
           "note": " ".join(note.split())[:NOTE_LIMIT]}
    client._memory_store().set_route_block(
        character, canonical, created_at=row["created_at"], until=row["until"],
        note=row["note"], timeout=WRITE_TIMEOUT)
    invalidate(client)
    return row


def remove_block(client: Any, subject: Any) -> bool:
    """Remove the owner's block of ``subject`` early; whether there was one."""
    character = _character(client)
    if not character:
        raise ValueError("no character is loaded yet")
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError("the subject must be text")
    subject = subject.strip()
    if subject.startswith("/"):
        subject = map_subject(subject)
    removed = client._memory_store().remove_route_block(
        character, subject, timeout=WRITE_TIMEOUT)
    invalidate(client)
    return removed


def invalidate(client: Any) -> None:
    """Re-read her blocks on the next use."""
    cache = _cache(client)
    if cache is not None:
        _refresh(client, cache, force=True)
