"""Persistent state-driven progression root for hands-free characters."""

from __future__ import annotations

import contextlib
import logging
import time

from .client import AtrinikClient
from .catalog_quest_tasks import (CatalogQuestTask, ManaCrystalUpgradeTask,
                                  POLICIES)
from .navigation import (RECALL_REQUIRED_LEVEL, FarmCircuitTask,
                         NavigateThenTask, RecoveryTask,
                         has_working_recall, heal_restock_alerts,
                         recall_alerts,
                         WorldGraph, analyzer_farm_selection,
                         farming_unsafe_reason, food_reserve, hungry,
                         temple_food_trip)
from .alerts import HUNGRY_FOOD
from .quest_tasks import EscapingDesertedIslandTask
from .tasks import (BotTask, RetrieveItemsTask, SafetyPolicy, TaskStatus,
                    unprepared_for_trips)

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
    # The alchemists' pool lies past Old Outpost maps whose residents reach
    # level 24 (troll elite soldiers): wait until she outlevels them well.
    MANA_CRYSTAL_UPGRADE_LEVEL = 30
    MANA_CRYSTAL_UPGRADE_RETRY = 24 * 60 * 60
    # A switch to the recovery phase waits at most this long for the
    # circuit's essential trip or farm fight: farm combat can pull the next
    # target, and a cure trip can stall.
    HANDOVER_SECONDS = 120.0
    # While she carries unpaid stock, the wait for its settling trip.
    HANDOVER_UNPAID_SECONDS = 300.0
    # A priest whose food trip failed is passed over this long.
    TEMPLE_PRIEST_RETRY = 60 * 60

    def __init__(self, graph: WorldGraph, *, target_level: int = 115):
        super().__init__(f"autoplay:level-{max(1, int(target_level))}")
        self.graph = graph
        self.target_level = max(1, int(target_level))
        self.child: BotTask | None = None
        self.phase = "bootstrap"
        self._intro_complete = False
        self._mana_recovery_attempted = False
        self._mana_upgrade_retry_at = 0.0
        self._retry_at = 0.0
        self._consecutive_failures = 0
        self._awaiting_ranking_recorded = False
        self._temple_food_retry_at = 0.0
        # Priests whose food trip failed, until when they are passed over.
        self._failed_priests: dict[str, float] = {}
        # Whether this hunger episode already tried a second priest.
        self._hunger_fallback_used = False
        # The farm circuit's pending maintenance backoffs. Each rebuilt
        # circuit inherits them, so it does not restart an errand that just
        # failed (live: the walk to a full apartment chest, every cycle).
        self._circuit_backoffs: dict[str, float] = {}
        # Since when a due switch to the recovery phase waits for the
        # circuit (AutoplayTask.HANDOVER_SECONDS).
        self._handover_since: float | None = None
        self.safety = SafetyPolicy()

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
        now = time.monotonic()
        if self.phase != "temple-food" or self.child is None:
            # A new hunger episode, not the fallback after a failed trip.
            self._hunger_fallback_used = False
        self._failed_priests = {
            name: until for name, until in self._failed_priests.items()
            if until > now}
        trip = temple_food_trip(self.graph, client,
                                exclude=self._avoided_priests())
        if trip is None:
            self._temple_food_retry_at = (
                now + FarmCircuitTask.TEMPLE_FOOD_RETRY_SECONDS)
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

    def _avoided_priests(self) -> tuple[str, ...]:
        """Priests whose food trip failed recently; farm circuits skip them too."""
        now = time.monotonic()
        return tuple(name for name, until in self._failed_priests.items()
                     if until > now)

    async def _temple_food_failed(self, client: AtrinikClient) -> bool:
        """Settle a failed temple food trip; True when another one started.

        That priest is passed over for a while. Once per hunger episode the
        nearest other priest reachable from where she now stands is tried at
        once: live, Sera retried an unreachable Talrain every cycle while
        Friar Marcus, who fed her each time, was a few maps away. Otherwise
        it takes the ordinary child-failure path: after that short backoff a
        fresh farm circuit is built, which feeds her itself (from a priest
        not passed over, or a shop), and autoplay's own temple trips wait
        the usual retry time.
        """
        assert self.child is not None
        failed = f"{self.phase}: {self.child.name}: {self.child.error}"
        priest = getattr(getattr(self.child, "task", None), "priest", "")
        if priest:
            self._failed_priests[priest] = (
                time.monotonic() + self.TEMPLE_PRIEST_RETRY)
        if not self._hunger_fallback_used and hungry(client):
            self._hunger_fallback_used = True
            if await self._start_temple_food(client):
                log.warning("autoplay temple food failed: %s; "
                            "trying another priest", failed)
                recorder = getattr(client, "record_action", None)
                if recorder is not None:
                    recorder("autoplay-retry",
                             f"{failed}; trying another priest")
                return True
        self._temple_food_retry_at = (
            time.monotonic() + FarmCircuitTask.TEMPLE_FOOD_RETRY_SECONDS)
        return False

    async def _start_mana_crystal_detours(self, client: AtrinikClient) -> bool:
        """Start the mana crystal's start, recovery or upgrade when due.

        Every path that starts one comes here. None starts while she has
        neither a usable heal nor a ready recall below UNPREPARED_HEALTH
        (unprepared_for_trips): it is postponed, and she farms (or rests on
        the farm) meanwhile. Live, on 2026-11-22, the mana-crystal-upgrade
        errand took her into the Old Outpost with no heals and no recall,
        and she died there.
        """
        # Its own safety policy reads the same heal as a farm's would:
        # SafetyPolicy._usable_heal depends only on her inventory, SP and
        # the healing patterns, which no farm changes (farms tune only the
        # heal and flee thresholds).
        if unprepared_for_trips(client, self.safety):
            return False
        if self._needs_mana_crystal_start(client):
            await self._start_mana_crystal_detour(client)
            return True
        if self._needs_mana_crystal_recovery(client):
            await self._start_mana_crystal_recovery(client)
            return True
        if self._needs_mana_crystal_upgrade(client):
            await self._start_mana_crystal_upgrade(client)
            return True
        return False

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

    def _needs_mana_crystal_upgrade(self, client: AtrinikClient) -> bool:
        """Take the crystal to the alchemists' pool and report to Gandyld.

        The pool also makes a new crystal when hers is lost, so this runs
        whether or not she carries one.
        """
        quest = client.state.quests.get(self.MANA_CRYSTAL_QUEST)
        recent_failure = any(
            entry.get("action") == "autoplay-retry" and
            "mana-crystal-upgrade" in str(entry.get("detail", "")) and
            time.time() - float(entry.get("time", 0) or 0) <
            self.MANA_CRYSTAL_UPGRADE_RETRY
            for entry in reversed(getattr(client, "decision_history", ()))
            if isinstance(entry, dict))
        return bool(
            client.state.quests_loaded and quest is not None and
            quest.status != "completed" and
            any(part.name == ManaCrystalUpgradeTask.PART and
                part.status not in ("done", "completed", "failed")
                for part in quest.parts) and
            int(client.state.stats.get("level", 0) or 0) >=
            self.MANA_CRYSTAL_UPGRADE_LEVEL and
            time.monotonic() >= self._mana_upgrade_retry_at and
            not recent_failure)

    async def _start_mana_crystal_upgrade(
            self, client: AtrinikClient) -> None:
        await client.clear_actions()
        await client.set_combat(False)
        await client.clear_target()
        await self._set_child(client, "mana-crystal-upgrade",
                              ManaCrystalUpgradeTask(self.graph))

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

    async def _start_recovery(self, client: AtrinikClient) -> None:
        """Run the recovery phase while farming is unsafe (RecoveryTask)."""
        await client.clear_actions()
        await client.set_combat(False)
        await client.clear_target()
        self._handover_since = None
        await self._set_child(client, "recovery", RecoveryTask(
            self.graph, backoffs=self._circuit_backoffs))

    @staticmethod
    def _forget_stale_recall_note(client: AtrinikClient) -> None:
        """Clear the persisted recall-unavailable note once recall works
        again (a working source, or below RECALL_REQUIRED_LEVEL), so a
        later recall loss never shows an old reason. Depletion taking
        priority does not clear it: the recall is still missing."""
        level = int(client.state.stats.get("level", 0) or 0)
        if (getattr(client.state, "recall_unavailable", "") and
                (level < RECALL_REQUIRED_LEVEL or has_working_recall(client))):
            setter = getattr(client, "set_recall_unavailable", None)
            if setter is not None:
                setter("")
            else:
                client.state.recall_unavailable = ""
                client.state.recall_unavailable_at = 0.0

    def recovery_alerts(self, client: AtrinikClient
                        ) -> tuple[list[dict[str, str]], bool]:
        """Dashboard alerts while farming is unsafe, from live state, and
        whether she is waiting at her savebed on an alerted blocker (then
        the stuck alert would say nothing new). Also, in any phase, the
        recall note (recall_alerts) and the heal-restock note
        (heal_restock_alerts), neither of which blocks farming.

        The farming-suspended warning follows the live reason in any phase;
        the specific blockers come from the recovery phase.
        """
        if self.status != TaskStatus.RUNNING:
            return [], False
        recall = recall_alerts(client) + heal_restock_alerts(client)
        reason = farming_unsafe_reason(client)
        if not reason:
            # A farm resting for want of a heal or a recall (at most
            # UNPREPARED_REST_LIMIT_SECONDS) is waiting, not stuck.
            resting = (isinstance(self.child, FarmCircuitTask) and
                       self.child.unprepared_resting())
            return recall, resting
        alerts = [{"id": "farming-suspended", "level": "warn",
                   "text": f"Farming suspended: {reason}"}] + recall
        waiting = False
        if isinstance(self.child, RecoveryTask):
            blockers = self.child.alerts(client)
            alerts = blockers + alerts
            waiting = bool(blockers) and self.child.waiting(client)
        return alerts, waiting

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

        self._forget_stale_recall_note(client)
        if (self._needs_temple_food(client) and
                await self._start_temple_food(client)):
            return

        if time.monotonic() < self._retry_at:
            await self.safety.enforce(client)
            return

        if self.child is not None:
            # Unsafe to farm: the recovery phase replaces the circuit. Its
            # discretionary trips end now; an essential trip, or a fight on
            # the farm map, finishes first, for at most HANDOVER_SECONDS.
            if (self.phase == "adaptive-farming" and
                    isinstance(self.child, FarmCircuitTask) and
                    farming_unsafe_reason(client)):
                dropped = self.child.drop_discretionary_trips(client)
                if dropped:
                    await client.clear_actions()
                    recorder = getattr(client, "record_action", None)
                    if recorder is not None:
                        recorder("recovery-handover-dropped", ", ".join(dropped))
                now = time.monotonic()
                if self._handover_since is None:
                    self._handover_since = now
                waited = now - self._handover_since
                # A payment under way is never cut off at the cap: unpaid
                # stock waits for its settling trip, up to the longer limit.
                capped = waited >= (
                    self.HANDOVER_UNPAID_SECONDS
                    if self.child.carries_unpaid(client)
                    else self.HANDOVER_SECONDS)
                if self.child.ready_for_handover(client) or capped:
                    self._circuit_backoffs = self.child.backoffs()
                    await self._start_recovery(client)
                    return
                self.child.hold_new_pulls()
            else:
                self._handover_since = None
                if isinstance(self.child, FarmCircuitTask):
                    # Safe again before the switch (a rod's retry ran out):
                    # farm normally.
                    self.child.hold_new_pulls(False)
            if (self.phase == "adaptive-farming" and
                    isinstance(self.child, FarmCircuitTask) and
                    self.child.safe_for_progression_detour(client) and
                    await self._start_mana_crystal_detours(client)):
                return
            try:
                await self.child.tick(client)
                if isinstance(self.child, (FarmCircuitTask, RecoveryTask)):
                    self._circuit_backoffs = self.child.backoffs()
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
                    if await self._temple_food_failed(client):
                        return
                elif self.phase == "mana-crystal-upgrade":
                    # A long, hostile trip: do not repeat it every few
                    # minutes when it cannot succeed.
                    self._mana_upgrade_retry_at = (
                        time.monotonic() + self.MANA_CRYSTAL_UPGRADE_RETRY)
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

        # Unsafe to farm: recovery, whether or not any spot is ranked, and
        # no progression detour.
        if farming_unsafe_reason(client):
            await self._start_recovery(client)
            return
        if await self._start_mana_crystal_detours(client):
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
                analyzer_driven=True, spot=spot,
                backoffs=self._circuit_backoffs,
                avoid_priests=self._avoided_priests))


class IdleSurvival:
    """Keep a connected character with no task fed, and nothing more.

    Stop means "no tasks except survival": a character left connected in
    Atrinik keeps getting hungry. Live, Stop left Sera idle on her no-magic
    savebed square with no food and no money, and she starved to death 22
    times, about every 50 minutes, while hunger handling ran only inside
    AutoplayTask. The engine's idle SafetyPolicy eats carried food below its
    threshold; this guard takes over once she is hungry (the autoplay test,
    ``hungry``) with nothing left to eat: the free temple food trip autoplay
    uses, to the nearest reachable priest. Afterwards she stays idle where
    she ate. A trip that cannot start or fails is recorded, raises the
    dashboard's idle-food alert, and is retried only after a growing
    backoff, so it never loops. Carried "food" that never raises her food
    stat (``SafetyPolicy.meals_failing``) does not count as food.

    Stop and manual dashboard actions call ``hold``: a running trip ends
    and none starts for ``RETRY_SECONDS``. After that, a still-hungry
    character goes again, since starving is worse than overriding the
    operator.
    """

    RETRY_SECONDS = FarmCircuitTask.TEMPLE_FOOD_RETRY_SECONDS
    MAX_RETRY_SECONDS = 60 * 60.0
    PRIEST_RETRY = AutoplayTask.TEMPLE_PRIEST_RETRY
    # Below this the engine's idle SafetyPolicy eats carried food.
    safety_min_food = SafetyPolicy().min_food

    def __init__(self, graph: WorldGraph):
        self.graph = graph
        self.child: BotTask | None = None
        self._retry_at = 0.0
        self._failures = 0
        # The operator's hold on new trips, and when this hunger episode's
        # first hold began (its budget is one RETRY_SECONDS).
        self._hold_until = 0.0
        self._hold_started: float | None = None
        # Priests whose food trip failed, until when they are passed over.
        self._failed_priests: dict[str, float] = {}
        # Bumped by drop and hold, which the dashboard and the engine call
        # while a tick awaits: the tick then leaves the trip as they left it.
        self._generation = 0

    def drop(self) -> None:
        """Forget a trip in flight; a still-hungry character starts afresh."""
        self.child = None
        self._generation += 1

    def hold(self, client: AtrinikClient, reason: str) -> None:
        """The operator took over: end a running trip and wait a while.

        No trip starts for up to ``RETRY_SECONDS``, so Stop (also during
        autoplay's own temple-food phase) or a manual move is not overridden
        at once. The hold never outlasts ``RETRY_SECONDS`` from the first
        one of a hunger episode, however often it is renewed, and ends once
        her food falls below the eating threshold: starving drains her
        health one-for-one (server types/player.c).
        """
        trip, self.child = self.child, None
        self._generation += 1
        now = time.monotonic()
        food = int(client.state.stats.get("food", 1000) or 0)
        if self._hold_started is None or food >= HUNGRY_FOOD:
            # Fed: a new hunger episode starts its own hold budget.
            self._hold_started = now
        if food < self.safety_min_food:
            self._hold_until = 0.0
            held = f"not held: food {food}"
        else:
            self._hold_until = max(self._hold_until, min(
                now + self.RETRY_SECONDS,
                self._hold_started + self.RETRY_SECONDS))
            held = (f"food trips held for "
                    f"{max(0.0, self._hold_until - now) / 60:.0f} min")
        ended = f"ended {trip.name}" if trip is not None else "no trip running"
        log.info("idle survival hold (%s): %s; %s", reason, ended, held)
        self._record(client, "idle-survival-hold",
                     f"{reason}; {ended}; {held}")

    def _avoided_priests(self) -> tuple[str, ...]:
        now = time.monotonic()
        self._failed_priests = {name: until for name, until
                                in self._failed_priests.items() if until > now}
        return tuple(self._failed_priests)

    @staticmethod
    def _record(client: AtrinikClient, action: str, detail: str) -> None:
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder(action, detail)

    def _give_up(self, client: AtrinikClient, reason: str) -> None:
        self._failures += 1
        delay = min(self.MAX_RETRY_SECONDS,
                    self.RETRY_SECONDS * 2 ** (self._failures - 1))
        self._retry_at = time.monotonic() + delay
        food = int(client.state.stats.get("food", 0) or 0)
        log.warning("idle and hungry (food %d) but cannot reach food: %s; "
                    "retry in %.0f min", food, reason, delay / 60)
        self._record(client, "idle-hunger-failed",
                     f"food {food}; {reason}; retry in {delay / 60:.0f} min")

    async def tick(self, client: AtrinikClient,
                   safety: SafetyPolicy | None = None) -> bool:
        """Run one survival step; True while a food trip has her busy.

        ``safety`` is the engine's idle policy, which eats carried food.
        """
        if self.child is not None:
            return await self._tick_trip(client)
        state = client.state
        food = int(state.stats.get("food", 1000) or 0)
        if food >= HUNGRY_FOOD:
            self._hold_started = None
        elif food < self.safety_min_food:
            # Starving soon: the operator's hold ends.
            self._hold_until = 0.0
        # Carried "food" that has not raised her food stat in three
        # applications is no food.
        fruitless = safety is not None and safety.meals_failing()
        # Login and reconnect enter "playing" before the inventory replay and
        # the first map; the food she carries is unknown until both arrive.
        if (not getattr(state, "inventory_replay_complete", True) or
                not getattr(getattr(state, "map", None), "path", "") or
                time.monotonic() < max(self._retry_at, self._hold_until) or
                not (hungry(client) or (fruitless and food < HUNGRY_FOOD))):
            return False
        if (food_reserve(client) and food < self.safety_min_food and
                not fruitless):
            # Her last meal first: the idle safety policy eats it now.
            return False
        trip = temple_food_trip(self.graph, client,
                                exclude=self._avoided_priests())
        if trip is None:
            self._give_up(client, "no temple priest is reachable")
            return False
        generation = self._generation
        try:
            await client.clear_actions()
            await client.set_combat(False)
            await client.clear_target()
            if self._generation != generation:
                # Stop (or a task) took over while those were sent.
                return False
            self.child = trip
            await trip.start(client)
        except ConnectionError as exc:
            # Never end the engine: the reconnect starts afresh.
            log.warning("idle food trip lost its connection while starting "
                        "(%s)", exc or "closed")
            if self.child is trip:
                self.child = None
            return True
        except Exception as exc:  # A trip bug must not end the engine.
            log.exception("idle food trip failed to start")
            if self.child is trip:
                self.child = None
            self._give_up(client, f"{trip.name} did not start: {exc}")
            return False
        if self.child is not trip:
            # Held or dropped while it started: undo what it sent.
            await self._clear_stale(client)
            return False
        self._record(client, "idle-hunger",
                     f"food {int(state.stats.get('food', 0) or 0)}; "
                     f"{trip.name}")
        return True

    @staticmethod
    async def _clear_stale(client: AtrinikClient) -> None:
        with contextlib.suppress(ConnectionError):
            await client.clear_actions()

    async def _tick_trip(self, client: AtrinikClient) -> bool:
        trip = self.child
        assert trip is not None
        try:
            await trip.tick(client)
        except ConnectionError as exc:
            # Rebuilt from the server's state once she is playing again.
            log.warning("idle food trip lost its connection (%s)",
                        exc or "closed")
            if self.child is trip:
                self.child = None
            return True
        except Exception as exc:  # A trip bug must not end the engine.
            log.exception("idle food trip failed")
            trip.fail(str(exc))
        if self.child is not trip:
            # Stop's hold (or a task) ended this trip while it ticked: not a
            # failure to back off from, and not hers to finish. Whatever the
            # rest of the tick sent after Stop's clear is cleared too, so
            # the stale trip does not keep walking.
            await self._clear_stale(client)
            return False
        if trip.status == TaskStatus.COMPLETE:
            self.child = None
            self._failures = 0
            self._retry_at = 0.0
            self._record(client, "idle-hunger-fed",
                         f"food {int(client.state.stats.get('food', 0) or 0)}"
                         f"; {trip.name}")
            return True
        if trip.status == TaskStatus.FAILED:
            self.child = None
            priest = getattr(getattr(trip, "task", None), "priest", "")
            if priest:
                self._failed_priests[priest] = (
                    time.monotonic() + self.PRIEST_RETRY)
            self._give_up(client, f"{trip.name}: {trip.error}")
            try:
                # Stop the rest of an abandoned route.
                await client.clear_actions()
            except ConnectionError:
                pass
            return False
        return True
