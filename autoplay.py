"""Persistent state-driven progression root for hands-free characters."""

from __future__ import annotations

import logging
import time

from .client import AtrinikClient
from .catalog_quest_tasks import CatalogQuestTask, POLICIES
from .navigation import (FarmCircuitTask, NavigateThenTask, WorldGraph,
                         analyzer_farm_selection, hungry, temple_food_trip)
from .quest_tasks import EscapingDesertedIslandTask
from . import constants as c
from .tasks import (BotTask, InventoryPolicy, JunkPolicy, RetrieveItemsTask,
                    SafetyPolicy, TaskStatus)

log = logging.getLogger(__name__)


class AutoplayTask(BotTask):
    """Bootstrap a character and keep autonomous progression running.

    This root deliberately owns child replacement.  A reconnect between ticks
    preserves the in-memory child; a connection lost during a child's tick
    drops that child, and a process restart reconstructs the phase, both from
    live quest and character state, so none needs a dashboard submission.
    """

    # Rebuilds its child from the server's state after a reconnect.
    survives_disconnect = True

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
        self._awaiting_ranking_recorded = False
        self._temple_food_retry_at = 0.0
        self._shed_at = 0.0
        self._shed_attempts: dict[int, int] = {}
        self.safety = SafetyPolicy()

    # Never shed these: money, food, containers (their contents would go too),
    # consumables and devices, jewellery and valuables, and non-objects.
    SHED_KEEP_TYPES = frozenset({
        c.TYPE_MONEY, c.TYPE_FOOD, c.TYPE_CONTAINER, c.TYPE_POTION,
        c.TYPE_SCROLL, c.TYPE_WAND, c.TYPE_ROD, c.TYPE_RING, c.TYPE_AMULET,
        c.TYPE_GEM, c.TYPE_JEWEL, c.TYPE_NUGGET, c.TYPE_SKILL, c.TYPE_SPELL,
        c.TYPE_FORCE, c.TYPE_SPELLBOOK})
    # Seconds between drops: the server must take one before the next.
    SHED_INTERVAL = 2.0

    def _sheddable(self, client: AtrinikClient) -> list:
        """Loose items she may drop when overloaded, heaviest first.

        Only unlocked ones: anything she or the operator locked stays, as do
        the keep classes above, keys and quest items, food by name, and
        ammunition for a missile weapon she wields.
        """
        policy, keep = InventoryPolicy(), JunkPolicy().keep_patterns
        food_words = SafetyPolicy().food_patterns
        shooting = any(item.item_type == c.TYPE_BOW and item.flags & c.ITEM_APPLIED
                       for item in client.state.inventory)
        candidates = []
        for item in client.state.inventory:
            name = item.name.casefold()
            if (item.weight * item.quantity <= 0 or
                    item.flags & (c.ITEM_APPLIED | c.ITEM_LOCKED | c.ITEM_UNPAID) or
                    item.item_type in self.SHED_KEEP_TYPES or
                    item.item_type == c.TYPE_ARROW and shooting or
                    policy.preserve(item) or
                    any(word in name for word in keep) or
                    any(word in name for word in food_words) or
                    self._shed_attempts.get(item.tag, 0) >= 2):
                continue
            candidates.append(item)
        return sorted(candidates, key=lambda item: -item.weight * item.quantity)

    async def _shed_overload(self, client: AtrinikClient) -> bool:
        """Over her carrying limit she crawls: drop loose junk where she stands.

        Live: 59 depletion points left Sera strength 1, a 25 kg limit and
        50 kg carried; at speed 0.1 every food trip ended in starvation, and
        each death's depletion made her weaker still. One drop per tick, the
        rest of the tick held, so navigation cannot cancel the queued move.
        """
        if (not getattr(client.state, "inventory_replay_complete", True) or
                InventoryPolicy().weight_ratio(client) <= 1.0):
            return False
        now = time.monotonic()
        if now < self._shed_at:
            return True
        candidates = self._sheddable(client)
        if not candidates:
            return False
        item = candidates[0]
        self._shed_attempts[item.tag] = self._shed_attempts.get(item.tag, 0) + 1
        await client.clear_actions()
        await client.move_item(0, item.tag, item.quantity)
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("overload-shed",
                     f"{item.name} x{item.quantity} "
                     f"({item.weight * item.quantity:.2f} kg; "
                     f"load {InventoryPolicy().weight_ratio(client):.2f} of the limit)")
        self._shed_at = now + self.SHED_INTERVAL
        return True

    def _needs_temple_food(self, client: AtrinikClient) -> bool:
        """Starving where nothing else will feed her.

        The farm circuit feeds her itself unless one of its maintenance trips
        is under way, and a hostile-route abort finishes first. Any other phase can outlast her food: a slow, depleted
        character once starved to death still walking to the apartment chest.
        """
        # Login and reconnect enter "playing" before the inventory replay;
        # the food she carries is unknown until it arrives.
        if (not getattr(client.state, "inventory_replay_complete", True) or
                time.monotonic() < self._temple_food_retry_at or
                not hungry(client)):
            return False
        child = self.child
        if child is None:
            return True
        if self.phase == "temple-food":
            return False
        if isinstance(child, FarmCircuitTask):
            return child.needs_outside_food()
        # Once Gandyld's quest has started, the starter is handing over the
        # crystal; replacing it then loses the reward until a checkpoint
        # reconnect, and apartment recovery would look for it in vain.
        return not (self.phase == "mana-crystal-starter" and
                    self.MANA_CRYSTAL_QUEST in client.state.quests)

    async def _start_temple_food(self, client: AtrinikClient) -> bool:
        """Pause the current phase for the free temple food service."""
        trip = temple_food_trip(self.graph, client)
        if trip is None:
            self._temple_food_retry_at = (
                time.monotonic() + FarmCircuitTask.TEMPLE_FOOD_RETRY_SECONDS)
            log.warning("starving, but no temple priest is reachable")
            return False
        paused = f"{self.phase}: {self.child.name}" if self.child else "idle"
        if self.phase == "mana-crystal-recovery":
            # Interrupted before an outcome; it runs again once she has eaten.
            self._mana_recovery_attempted = False
        await client.clear_actions()
        await client.set_combat(False)
        await client.clear_target()
        await self._set_child(client, "temple-food", trip)
        self._retry_at = 0.0
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("autoplay-hunger",
                     f"food {int(client.state.stats.get('food', 0) or 0)}; "
                     f"paused {paused}")
        return True

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
        try:
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
        except ConnectionError:
            # Setup was interrupted before the recovery ran to an outcome.
            self._mana_recovery_attempted = False
            if self.phase == "mana-crystal-recovery":
                self.child = None
            raise

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

        if await self._shed_overload(client):
            return
        if (self._needs_temple_food(client) and
                await self._start_temple_food(client)):
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
            try:
                await self.child.tick(client)
            except ConnectionError as exc:
                # The child's dialogue, transfer, or purchase may be half done;
                # rebuild it from the server's state after the reconnect.
                log.warning("autoplay %s child lost its connection (%s); "
                            "rebuilding it after the reconnect",
                            self.phase, exc or "closed")
                recorder = getattr(client, "record_action", None)
                if recorder is not None:
                    recorder("autoplay-disconnect",
                             f"{self.phase}: {self.child.name}")
                if self.phase == "mana-crystal-recovery":
                    # The interrupted recovery never ran to an outcome.
                    self._mana_recovery_attempted = False
                self.child = None
                raise
            if self.child.status not in (
                    TaskStatus.COMPLETE, TaskStatus.FAILED):
                return
            if self.child.status == TaskStatus.FAILED:
                if self.phase == "temple-food":
                    self._temple_food_retry_at = (
                        time.monotonic() +
                        FarmCircuitTask.TEMPLE_FOOD_RETRY_SECONDS)
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

        # The farming analyzer is the only source of farm spots. Until its
        # first ranking (or a usable, unquarantined spot) exists, stay safe
        # where the character is instead of guessing a spot.
        selection = analyzer_farm_selection(client, self.graph)
        if selection is None:
            if not self._awaiting_ranking_recorded:
                self._awaiting_ranking_recorded = True
                analyzer = getattr(client, "farm_analyzer", None)
                status = getattr(analyzer, "status", "unavailable")
                log.info("autoplay waiting for a farming analyzer spot (%s)", status)
                recorder = getattr(client, "record_action", None)
                if recorder is not None:
                    recorder("awaiting-farm-ranking", f"analyzer {status}")
            await self.safety.enforce(client)
            return
        self._awaiting_ranking_recorded = False
        legs, spot = selection
        await self._set_child(
            client, "adaptive-farming",
            FarmCircuitTask(
                self.graph, list(legs), level_until=self.target_level,
                analyzer_driven=True, spot=spot))
