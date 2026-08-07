"""Typed Python adapters over the shared native pathfinding core."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from . import _pathfinding


@dataclass(frozen=True, slots=True)
class SearchMetrics:
    expanded: int
    generated: int
    examined_transitions: int
    peak_frontier: int
    total_cost: int


@dataclass(frozen=True, slots=True)
class SearchResult:
    status: str
    path: tuple[int, ...]
    transitions: tuple[int, ...]
    metrics: SearchMetrics


def _result(raw: dict) -> SearchResult:
    metrics = raw["metrics"]
    return SearchResult(
        status=raw["status"],
        path=tuple(raw["path"]),
        transitions=tuple(raw["transitions"]),
        metrics=SearchMetrics(**metrics),
    )


def state_mask(size: int, states: Iterable[int]) -> bytes:
    """Build the compact byte mask consumed by the native extension."""
    if size < 0:
        raise ValueError("mask size must be non-negative")
    mask = bytearray(size)
    for state in states:
        if state < 0 or state >= size:
            raise ValueError(f"state {state} is outside a {size}-state mask")
        mask[state] = 1
    return bytes(mask)


def grid_search(width: int, height: int, walkable: bytes, start: int,
                goals: Iterable[int], *, costs: bytes | None = None,
                excluded: Iterable[int] = (), diagonal: bool = True,
                max_generated: int = 0,
                return_partial: bool = False) -> SearchResult:
    size = width * height
    raw = _pathfinding.grid_search(
        width, height, walkable, start, state_mask(size, goals),
        costs=costs, excluded=state_mask(size, excluded),
        diagonal=diagonal, max_generated=max_generated,
        return_partial=return_partial,
    )
    return _result(raw)


def grid_reachable(width: int, height: int, walkable: bytes, start: int, *,
                   excluded: Iterable[int] = (), diagonal: bool = True,
                   max_generated: int = 0) -> tuple[str, tuple[int, ...], SearchMetrics]:
    size = width * height
    raw = _pathfinding.grid_reachable(
        width, height, walkable, start,
        excluded=state_mask(size, excluded), diagonal=diagonal,
        max_generated=max_generated,
    )
    return raw["status"], tuple(raw["states"]), SearchMetrics(**raw["metrics"])


def graph_search(offsets: Sequence[int], targets: Sequence[int], start: int,
                 goals: Iterable[int], *, costs: Sequence[int] | None = None,
                 metadata: Sequence[int] | None = None,
                 blocked_states: Iterable[int] = (),
                 excluded_edges: Iterable[int] = (),
                 max_generated: int = 0,
                 return_partial: bool = False) -> SearchResult:
    state_count = len(offsets) - 1
    raw = _pathfinding.graph_search(
        offsets, targets, start, state_mask(state_count, goals),
        costs=costs, metadata=metadata,
        blocked_states=state_mask(state_count, blocked_states),
        excluded_edges=state_mask(len(targets), excluded_edges),
        max_generated=max_generated, return_partial=return_partial,
    )
    return _result(raw)
