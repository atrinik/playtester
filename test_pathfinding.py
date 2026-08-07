"""Shared-core binding fixtures for grid and indexed-graph policy adapters."""

from __future__ import annotations

import unittest

from . import _pathfinding
from .pathfinding import graph_search, grid_reachable, grid_search


class GridPathfindingTests(unittest.TestCase):
    def test_cardinal_diagonal_and_equal_cost_paths_are_deterministic(self):
        walkable = bytes([1] * 25)
        first = grid_search(5, 5, walkable, 0, (24,))
        second = grid_search(5, 5, walkable, 0, (24,))
        self.assertEqual(first.status, "found")
        self.assertEqual(first.path, second.path)
        self.assertEqual(first.transitions, second.transitions)
        self.assertEqual(first.path[0], 0)
        self.assertEqual(first.path[-1], 24)
        self.assertEqual(len(first.path), 5)

        cardinal = grid_search(5, 5, walkable, 0, (24,), diagonal=False)
        self.assertEqual(cardinal.status, "found")
        self.assertEqual(len(cardinal.path), 9)

    def test_weighted_terrain_and_goal_masks(self):
        walkable = bytes([1] * 6)
        costs = bytes((1, 100, 1, 1, 1, 1))
        result = grid_search(3, 2, walkable, 0, (2,), costs=costs)
        self.assertEqual(result.status, "found")
        self.assertNotIn(1, result.path)
        self.assertEqual(result.metrics.total_cost, 2)

        perimeter = grid_search(3, 2, walkable, 0, (1, 4, 5))
        self.assertEqual(perimeter.status, "found")
        self.assertEqual(perimeter.path[-1], 1)

    def test_statuses_limits_partial_and_dynamic_exclusions(self):
        walkable = bytes([1] * 16)
        limited = grid_search(4, 4, walkable, 0, (15,), max_generated=2)
        self.assertEqual(limited.status, "limit reached")
        self.assertFalse(limited.path)

        partial = grid_search(
            4, 4, walkable, 0, (15,), max_generated=2,
            return_partial=True,
        )
        self.assertEqual(partial.status, "partial")
        self.assertTrue(partial.path)

        blocked = grid_search(4, 4, walkable, 0, (15,), excluded=(5,))
        self.assertEqual(blocked.status, "found")
        self.assertNotIn(5, blocked.path)
        self.assertGreater(blocked.metrics.examined_transitions, 0)

    def test_reachability_handles_cycles_and_a_blocked_arrival_start(self):
        walkable = bytes((0, 1, 0, 1, 1, 0, 0, 1, 1))
        status, states, metrics = grid_reachable(3, 3, walkable, 0)
        self.assertEqual(status, "complete")
        self.assertEqual(set(states), {0, 1, 3, 4, 7, 8})
        self.assertEqual(metrics.generated, len(states))

    def test_invalid_grid_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            grid_search(3, 3, b"short", 0, (8,))
        with self.assertRaises(ValueError):
            grid_search(3, 3, bytes([1] * 9), 99, (8,))
        with self.assertRaisesRegex(TypeError, "immutable byte buffer"):
            _pathfinding.grid_search(
                1, 1, bytearray((1,)), 0, bytes((1,)))


class GraphPathfindingTests(unittest.TestCase):
    def test_metadata_preserves_exit_or_seam_choice(self):
        # 0 has two equal-cost routes to 3. Stable edge order chooses 0->1.
        result = graph_search(
            (0, 2, 3, 4, 4),
            (1, 2, 3, 3),
            0,
            (3,),
            metadata=(101, 102, 113, 123),
        )
        self.assertEqual(result.status, "found")
        self.assertEqual(result.path, (0, 1, 3))
        self.assertEqual(result.transitions, (101, 113))

    def test_weighted_avoidance_and_edge_exclusion_replan(self):
        offsets = (0, 2, 3, 4, 4)
        targets = (1, 2, 3, 3)
        costly = graph_search(
            offsets, targets, 0, (3,), costs=(20, 1, 1, 2),
            metadata=(101, 102, 113, 123),
        )
        self.assertEqual(costly.path, (0, 2, 3))
        self.assertEqual(costly.metrics.total_cost, 3)

        replanned = graph_search(
            offsets, targets, 0, (3,), metadata=(101, 102, 113, 123),
            excluded_edges=(0,),
        )
        self.assertEqual(replanned.path, (0, 2, 3))

    def test_large_cost_overflow_and_invalid_neighbor_are_distinct(self):
        overflow = graph_search(
            (0, 1, 2, 2), (1, 2), 0, (2,),
            costs=((1 << 64) - 1, 1),
        )
        self.assertEqual(overflow.status, "cost overflow")
        with self.assertRaises(ValueError):
            graph_search((0, 1), (1,), 0, (0,))
        with self.assertRaisesRegex(ValueError, "invalid integer"):
            graph_search((0, -1), (), 0, (0,))


if __name__ == "__main__":
    unittest.main()
