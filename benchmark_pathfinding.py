"""Repeatable end-to-end benchmarks for the native bot adapters."""

from __future__ import annotations

import argparse
import json
import math
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


# Whole-world trips for the weighted-plan gate (docs/ROUTING.md 4.5):
# (name, source map, destination map, access lock ids). The incident's
# Brynknot bed to the Underground City circuit, and Morg'eean's ship.
WORLD_TRIPS = (
    ("incident", "/shattered_islands/world_1_67_1",
     "/shattered_islands/strakewood_island/underground_city/underground_city_0_4", ()),
    ("ship", "/shattered_islands/world_1_67_1", "/shattered_islands/world_6_51",
     ("morgeean_ship_key",)),
    ("ship-without-key", "/shattered_islands/world_1_67_1",
     "/shattered_islands/world_6_51", ()),
    ("burnt-forest", "/shattered_islands/world_1_67_1", "/shattered_islands/world_14_79", ()),
    ("incuna", "/shattered_islands/world_4_85", "/shattered_islands/world_2_87", ()),
    ("brynknot-strakewood", "/shattered_islands/world_0_70",
     "/shattered_islands/world_0_51", ()),
)
# The gate: the median weighted plan, warm, no slower than this many times
# today's breadth-first plan on the same trip.
WEIGHTED_GATE = 1.5


def world_benchmarks(repeats: int) -> dict:
    """Whole-world plans on the locked content: today's breadth-first
    route_points against the learned-cost one (route_costs.CostContext,
    with a learned death on the incident's entrance), each warm (component
    caches built), plus the weighted plan's first, cold-cache run."""
    from . import route_costs
    from .compatibility import configure_cached_bundle, default_cache_root, load_lock
    configure_cached_bundle(default_cache_root(), load_lock())
    from .navigation import WorldGraph, _clear_components

    graph = WorldGraph().build()
    now = time.time()
    model = route_costs.RouteCostModel(rows=[(
        now - 60, "map:/shattered_islands/strakewood_island/underground_city/"
        "underground_city_0_0", "death", 1.0, "", 1.0)])
    context = route_costs.CostContext(
        graph, model.view(now), route_costs.content_priors(graph, 34))
    results = []
    for name, source, destination, access in WORLD_TRIPS:
        if source not in graph.nodes or destination not in graph.nodes:
            continue
        beds = graph.savebeds.get(source) or []
        node = graph.nodes[source]
        start = beds[0] if beds else (node.enter_x, node.enter_y)
        allow = frozenset(access)

        def plain() -> list:
            return graph.route_points(source, start, destination, [], allow_locked=allow)

        def weighted() -> list:
            return graph.route_points(source, start, destination, [], allow_locked=allow,
                                      edge_cost=context.edge_cost)

        _clear_components(graph)
        started = time.perf_counter_ns()
        try:
            weighted()
        except ValueError:
            continue
        cold = (time.perf_counter_ns() - started) / 1_000_000
        timings = {}
        for label, plan in (("bfs", plain), ("weighted", weighted)):
            plan()
            durations = []
            for _ in range(repeats):
                started = time.perf_counter_ns()
                route = plan()
                durations.append((time.perf_counter_ns() - started) / 1_000_000)
            timings[label] = (statistics.median(durations), len(route))
        results.append({
            "name": name, "bfs_median_ms": round(timings["bfs"][0], 2),
            "weighted_median_ms": round(timings["weighted"][0], 2),
            "weighted_cold_ms": round(cold, 2),
            "ratio": round(timings["weighted"][0] / timings["bfs"][0], 3),
            "bfs_edges": timings["bfs"][1], "weighted_edges": timings["weighted"][1]})
    ratio = statistics.median(row["ratio"] for row in results) if results else math.inf
    return {"trips": results, "median_ratio": round(ratio, 3), "gate": WEIGHTED_GATE,
            "passed": ratio <= WEIGHTED_GATE}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=128)
    parser.add_argument("--graph-states", type=int, default=10_000)
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--world", action="store_true",
                        help="also run the whole-world weighted-plan gate "
                             "(needs the locked content bundle)")
    args = parser.parse_args()
    if args.size < 8 or args.graph_states < 2 or args.repeats < 1:
        parser.error("size >= 8, graph-states >= 2 and repeats >= 1 are required")
    report: object = benchmarks(args.size, args.graph_states, args.repeats)
    if args.world:
        report = {"native": report, "world": world_benchmarks(min(args.repeats, 5))}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
