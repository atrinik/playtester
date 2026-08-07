"""Repeatable end-to-end benchmarks for the native bot adapters."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections.abc import Callable

from .pathfinding import SearchResult, graph_search, grid_search


def _measure(name: str, search: Callable[[], SearchResult],
             repeats: int) -> dict[str, int | float | str]:
    durations = []
    result = search()
    for _ in range(repeats):
        started = time.perf_counter_ns()
        result = search()
        durations.append(time.perf_counter_ns() - started)
    return {
        "name": name,
        "status": result.status,
        "repeats": repeats,
        "median_ms": statistics.median(durations) / 1_000_000,
        "minimum_ms": min(durations) / 1_000_000,
        "maximum_ms": max(durations) / 1_000_000,
        "expanded": result.metrics.expanded,
        "generated": result.metrics.generated,
        "examined_transitions": result.metrics.examined_transitions,
        "path_steps": len(result.path),
    }


def benchmarks(size: int, graph_states: int,
               repeats: int) -> list[dict[str, int | float | str]]:
    walkable = bytearray([1] * (size * size))
    for x in range(7, size - 1, 8):
        gap = (x * 11) % size
        for y in range(size):
            if y != gap:
                walkable[y * size + x] = 0
    grid = bytes(walkable)

    blocked = bytearray([1] * (size * size))
    blocked[(size // 2) * size:(size // 2 + 1) * size] = bytes(size)
    no_path_grid = bytes(blocked)

    offsets = [0]
    targets = []
    for state in range(graph_states):
        if state + 1 < graph_states:
            targets.append(state + 1)
        if state + 17 < graph_states:
            targets.append(state + 17)
        offsets.append(len(targets))
    graph_offsets = tuple(offsets)
    graph_targets = tuple(targets)

    return [
        _measure(
            "grid-maze",
            lambda: grid_search(
                size, size, grid, 0, (size * size - 1,)),
            repeats),
        _measure(
            "grid-no-path",
            lambda: grid_search(
                size, size, no_path_grid, 0, (size * size - 1,)),
            repeats),
        _measure(
            "indexed-world-graph",
            lambda: graph_search(
                graph_offsets, graph_targets, 0, (graph_states - 1,)),
            repeats),
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=128)
    parser.add_argument("--graph-states", type=int, default=10_000)
    parser.add_argument("--repeats", type=int, default=20)
    args = parser.parse_args()
    if args.size < 8 or args.graph_states < 2 or args.repeats < 1:
        parser.error("size >= 8, graph-states >= 2 and repeats >= 1 are required")
    print(json.dumps(
        benchmarks(args.size, args.graph_states, args.repeats), indent=2))


if __name__ == "__main__":
    main()
