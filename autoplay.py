"""Persistent state-driven progression root for hands-free characters."""

from __future__ import annotations

import logging
import time

from .client import AtrinikClient
from .catalog_quest_tasks import CatalogQuestTask, POLICIES
from .navigation import FarmCircuitTask, NavigateThenTask, WorldGraph
from .quest_tasks import EscapingDesertedIslandTask
from .tasks import (BotTask, RetrieveItemsTask, SafetyPolicy, TaskStatus)

log = logging.getLogger(__name__)


class AutoplayTask(BotTask):
    """Bootstrap a character and keep autonomous progression running.

    This root deliberately owns child replacement.  A reconnect preserves the
    in-memory child; a process restart reconstructs the same phase from live
    quest and character state, so neither case needs a dashboard submission.
    """

    MANA_CRYSTAL_LEVEL = 18
    MANA_CRYSTAL_QUEST = "Gandyld's Mana Crystal"
    MANA_CRYSTAL_RECOVERY_RETRY = 24 * 60 * 60

    def __init__(self, graph: WorldGraph, *, target_level: int = 115):
        super().__init__(f"autoplay:level-{max(1, int(target_level))}")
        self.graph = graph
        self.target_level = max(1, int(target_level))
        self.child: BotTask | None = None
        self.phase = "bootstrap"
        self._intro_complete = False
        self._mana_recovery_attempted = False
        self._retry_at = 0.0
        self._consecutive_failures = 0
        self.safety = SafetyPolicy()

    def _needs_mana_crystal_start(self, client: AtrinikClient) -> bool:
        """Collect Gandyld's safe 50-SP opening reward exactly once."""
        return bool(
            client.state.quests_loaded and
            int(client.state.stats.get("level", 0) or 0) >=
            self.MANA_CRYSTAL_LEVEL and
            self.MANA_CRYSTAL_QUEST not in client.state.quests and
            not any("gandyld's mana crystal" in item.name.casefold()
                    for item in client.state.inventory)
        )

    def _has_mana_crystal(self, client: AtrinikClient) -> bool:
        return any("gandyld's mana crystal" in item.name.casefold()
                   for item in client.state.inventory)

    def _needs_mana_crystal_recovery(self, client: AtrinikClient) -> bool:
        quest = client.state.quests.get(self.MANA_CRYSTAL_QUEST)
        recent_failure = any(
            entry.get("action") == "autoplay-retry" and
            "mana-crystal-recovery" in str(entry.get("detail", "")) and
            time.time() - float(entry.get("time", 0) or 0) <
            self.MANA_CRYSTAL_RECOVERY_RETRY
            for entry in reversed(getattr(client, "decision_history", ()))
            if isinstance(entry, dict))
        return bool(
            client.state.quests_loaded and quest is not None and
            quest.status != "completed" and
            not self._has_mana_crystal(client) and
            not self._mana_recovery_attempted and not recent_failure)

    async def _start_mana_crystal_detour(
            self, client: AtrinikClient) -> None:
        await client.clear_actions()
        await client.set_combat(False)
        await client.clear_target()
        await self._set_child(
            client, "mana-crystal-starter",
            CatalogQuestTask(
                self.graph, POLICIES[self.MANA_CRYSTAL_QUEST],
                stop_after_start=True,
                start_reward_pattern=r"Gandyld's Mana Crystal"))

    async def _start_mana_crystal_recovery(
            self, client: AtrinikClient) -> None:
        self._mana_recovery_attempted = True
        await client.clear_actions()
        await client.set_combat(False)
        await client.clear_target()
        await self._set_child(
            client, "mana-crystal-recovery",
            NavigateThenTask(
                self.graph,
                "/shattered_islands/strakewood_island/apartments/"
                "apartment_cheap",
                RetrieveItemsTask(
                    "chest", (r"Gandyld's Mana Crystal",),
                    require_match=True),
                destination_xy=(1, 3), combat_approach=True))

    async def _set_child(self, client: AtrinikClient, phase: str,
                         child: BotTask) -> None:
        self.phase = phase
        self.child = child
        await child.start(client)
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("autoplay-phase", f"{phase}: {child.name}")

    async def _recover_child_failure(self, client: AtrinikClient) -> None:
        assert self.child is not None
        self._consecutive_failures += 1
        delay = min(300.0, 5.0 * (2 ** min(5,
                                            self._consecutive_failures - 1)))
        detail = (f"{self.phase}: {self.child.name}: {self.child.error}; "
                  f"retry in {delay:.0f}s")
        log.warning("autoplay child failed: %s", detail)
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("autoplay-retry", detail)
        await client.clear_actions()
        await client.set_combat(False)
        await client.clear_target()
        self.child = None
        self._retry_at = time.monotonic() + delay

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        if self.status != TaskStatus.RUNNING or client.state.phase != "playing":
            return

        if time.monotonic() < self._retry_at:
            await self.safety.enforce(client)
            return

        if self.child is not None:
            if (self.phase == "adaptive-farming" and
                    isinstance(self.child, FarmCircuitTask) and
                    self.child.safe_for_progression_detour(client)):
                if self._needs_mana_crystal_start(client):
                    await self._start_mana_crystal_detour(client)
                    return
                if self._needs_mana_crystal_recovery(client):
                    await self._start_mana_crystal_recovery(client)
                    return
            await self.child.tick(client)
            if self.child.status not in (
                    TaskStatus.COMPLETE, TaskStatus.FAILED):
                return
            if self.child.status == TaskStatus.FAILED:
                await self._recover_child_failure(client)
                return
            completed_phase = self.phase
            self.child = None
            self._consecutive_failures = 0
            self._retry_at = 0.0
            if completed_phase == "intro-quest":
                self._intro_complete = True
            elif completed_phase == "mana-crystal-starter":
                # Interface reward objects are already in the server-side
                # inventory, but a reconnect is the authoritative replay
                # barrier which moves them from the transient dialog model
                # into the bot's ordinary carried-item model.
                checkpoint = getattr(client, "checkpoint_reconnect", None)
                if checkpoint is not None and await checkpoint():
                    return
            elif completed_phase == "adaptive-farming":
                if int(client.state.stats.get("level", 0) or 0) >= \
                        self.target_level:
                    self.complete()
                    return

        # Always ask the live quest journal first.  On an established
        # character this child completes immediately; on a fresh character it
        # resumes every tutorial part and sails to the mainland.
        if not self._intro_complete:
            await self._set_child(
                client, "intro-quest",
                EscapingDesertedIslandTask(self.graph))
            return

        if int(client.state.stats.get("level", 0) or 0) >= self.target_level:
            self.complete()
            return

        if self._needs_mana_crystal_start(client):
            await self._start_mana_crystal_detour(client)
            return
        if self._needs_mana_crystal_recovery(client):
            await self._start_mana_crystal_recovery(client)
            return

        await self._set_child(
            client, "adaptive-farming",
            FarmCircuitTask(
                self.graph, list(FarmCircuitTask.EARLY_SAFE_LEGS),
                level_until=self.target_level))
