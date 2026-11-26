"""Dependency-free HTTP control plane for a persistent Atrinik client."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import builds
from . import constants as c
from .catalog_quest_tasks import (AllFormalQuestsTask, CatalogQuestTask,
                                  POLICIES, RECOMMENDED_QUEST_ORDER)
from .autoplay import AutoplayTask
from .build_info import build_info
from .farm_analyzer import (DEFAULT_OBJECTIVE, OBJECTIVES, SUMMARY_FIELDS,
                            encounter_warning, risky_approach, risky_spot)
from . import self_improvement
from .client import AtrinikClient, map_object_visual_name
from .equipment import equipment_report
from .feedback import (KINDS as FEEDBACK_KINDS, REPOS as FEEDBACK_REPOS,
                       screen as screen_feedback, short_fingerprint)
from .face_images import FaceImages
from .metrics import CONTENT_TYPE as METRICS_CONTENT_TYPE
from .alerts import active as active_alerts
from .navigation import (analyzer_farm_choice, analyzer_spot_legs, effective_farm_objective,
                         seam_ranking,
                         CARE_OWNER_BLOCK, CARE_WAIT_SECONDS,
                         FarmCircuitTask, NavigateTask, NavigateThenTask,
                         ShopUpgradeSweepTask, WorldGraph)
from . import route_blocks
from . import terrain
from . import route_events, route_shadow, storage
from .navigation_spots import navigation_spot_catalog
from .dialogue_edges import dialogue_summary
from .unlocks import gate_counts, unlock_state
from .operator_assist import (KEY_HEADER, MAX_GOLD_PER_RUN, OperatorAssist,
                              OperatorAssistRefused, key_matches)
from .quest_tasks import EscapingDesertedIslandTask
from .quests import QuestDefinition, QuestObjective, flatten_parts, load_catalog
from .tasks import (BankTask, BotTask, BuyShopUpgradeTask, DepositItemsTask,
                    FarmTask, InventoryPolicy, JunkPolicy, RetrieveItemsTask,
                    SellItemsTask, SellJunkTask, TaskEngine, TaskStatus,
                    carried_gear_score, working_recall)

log = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("web_static")
# Every response's Content-Security-Policy (docs/LEARNING_AND_FEEDBACK.md
# 4.10.5): scripts, styles, images and requests only from the dashboard
# itself; no inline script or style (the page sets styles through CSSOM),
# no plugins, no framing and no <base> rewriting.
# The longest the Knowledge tab waits for a locked database (seconds).
KNOWLEDGE_READ_TIMEOUT = 0.25
CONTENT_SECURITY_POLICY = ("default-src 'self'; script-src 'self'; object-src 'none'; "
                           "frame-ancestors 'none'; base-uri 'none'")

# Levels 0-30 of the classic server's new_levels table. Only used when the
# bundled server source cannot be read; levels past it report unknown progress.
FALLBACK_EXPERIENCE_THRESHOLDS = (
    0, 0, 2000, 4000, 8000, 16000, 32000, 64000, 125000,
    250000, 500000, 900000, 1400000, 2000000, 2600000,
    3300000, 4100000, 4900000, 5700000, 6600000, 7500000,
    8500000, 9500000, 10600000, 11800000, 13000000,
    14300000, 15600000, 17000000, 18500000, 20000000)


# Parsed server tables by source path. Only successful reads are kept, so a
# source that becomes readable later (a bundle installed after start) is used.
_SERVER_TABLES: dict[str, tuple[int, ...]] = {}
# Sources already reported as unusable, so the fallback is logged once each.
_UNUSABLE_SOURCES: set[str] = set()


def _server_experience_thresholds(source: str) -> tuple[int, ...] | None:
    """The server's new_levels table from ``source``, or None.

    Accepted only if it looks like that table: longer than the fallback, its
    first entries equal to the fallback, and never decreasing.
    """
    cached = _SERVER_TABLES.get(source)
    if cached is not None:
        return cached
    path = Path(source)
    if path.is_dir():
        path /= "src/server/exp.c"
    try:
        match = re.search(
            r"new_levels\[[^]]+\]\s*=\s*\{(.*?)\};", path.read_text(), re.S)
        values = tuple(int(value) for value in re.findall(r"\d+", match.group(1)))
    except (AttributeError, OSError, UnicodeDecodeError, ValueError):
        return None
    fallback = FALLBACK_EXPERIENCE_THRESHOLDS
    if (len(values) <= len(fallback) or
            values[:len(fallback)] != fallback or
            any(later < earlier for earlier, later in zip(values, values[1:]))):
        return None
    _SERVER_TABLES[source] = values
    return values


def _experience_table() -> tuple[tuple[int, ...], bool]:
    """The server level table and whether it is the complete one.

    ATRINIK_SERVER_SOURCE names a classic server source tree or exp.c; the
    compatibility bundle sets it to the classic_server input the farming
    analyzer reads, so an explicit setting still wins.
    """
    source = os.getenv("ATRINIK_SERVER_SOURCE", "")
    values = _server_experience_thresholds(source) if source else None
    if values is None:
        if source and source not in _UNUSABLE_SOURCES:
            _UNUSABLE_SOURCES.add(source)
            log.warning("ATRINIK_SERVER_SOURCE %s has no usable new_levels "
                        "table; level progress from level %d on is unknown",
                        source, len(FALLBACK_EXPERIENCE_THRESHOLDS) - 1)
        return FALLBACK_EXPERIENCE_THRESHOLDS, False
    return values, True


def experience_thresholds() -> tuple[int, ...]:
    """The bundled server level table, with a partial fallback."""
    return _experience_table()[0]


def experience_progress(level: int, experience: int) -> dict[str, Any]:
    thresholds, complete = _experience_table()
    level = max(0, int(level or 0))
    experience = max(0, int(experience or 0))
    current = thresholds[level] if level < len(thresholds) else experience
    # The server's table has MAXLEVEL + 2 entries: the last one is a sentinel
    # past the level cap, never a level she can reach.
    last_level = len(thresholds) - (2 if complete else 1)
    next_experience = (thresholds[level + 1]
                       if level < last_level else None)
    remaining = (max(0, next_experience - experience)
                 if next_experience is not None else 0)
    span = ((next_experience - current)
            if next_experience is not None else 0)
    if next_experience is None and not complete:
        # Past the end of the partial fallback: the next level is unknown,
        # not reached.
        percent = None
    else:
        percent = round(100.0 if span <= 0 else
                        max(0.0, min(100.0,
                                     (experience - current) * 100.0 / span)), 1)
    return {
        "experience": experience, "level_experience": current,
        "next_experience": next_experience,
        "remaining_experience": remaining,
        "progress_percent": percent,
    }


SUGGESTED_LEVELS = {
    "Escaping the Deserted Island": "1–4",
    "Lost Memories": "1–4",
    "Clearhaven Mine": "10–17",
    "Lairwenn's Notes": "5–9",
    "Melanye's Lost Walking Stick": "5–9",
    "Shipment of Charob Beer": "5–9",
    "Frevia's Tomboyish Fairy": "5–17",
    "Crazymix's Alchemical Reagents": "5–17",
    "Fort Sether Illness": "10–17",
    "Galann's Revenge": "18–29",
    "Gandyld's Mana Crystal": "18–39",
    "Construction of Telescope": "18–29",
    "The Mushroom Demon": "30–39",
    "Two Lovers Doomed": "35–54",
    "Rescuing Lynren": "about 50",
    "Portal of Llwyfen": "55–84 (final boss: 70+)",
}


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _clean_markup(text: str) -> str:
    return re.sub(r"\[[^]]+\]", "", text).strip()


def item_visual_name(client: AtrinikClient, item: Any) -> str:
    """Name the client would draw: the animation when one is set, else the face."""
    if getattr(item, "animation", 0):
        name = getattr(client, "animations", {}).get(item.animation, "")
        if name:
            return name
    return getattr(client, "faces", {}).get(item.face, "")


class DashboardState:
    def __init__(self, client: AtrinikClient, engine: TaskEngine,
                 graph: WorldGraph, catalog: dict[str, QuestDefinition]):
        self.client, self.engine = client, engine
        self.graph, self.catalog = graph, catalog
        self.started_at = time.time()
        # The owner's operator assist, when the dashboard server runs one.
        self.operator_assist: OperatorAssist | None = None

    def operator_assist_status(self) -> dict[str, Any] | None:
        assist = self.operator_assist
        return assist.status() if assist is not None else None

    @staticmethod
    def _objective(obj: QuestObjective) -> dict[str, Any]:
        return {
            "kind": obj.kind,
            "name": obj.name or obj.archetype.replace("_", " "),
            "archetype": obj.archetype,
            "quantity": obj.quantity,
        }

    def _objective_ready(self, objective: QuestObjective) -> bool:
        if objective.kind != "item":
            return False
        pattern = re.compile(
            objective.name or objective.archetype.replace("_", " "), re.I)
        return sum(item.quantity for item in self.client.state.inventory
                   if pattern.search(item.name)) >= objective.quantity

    def quests(self) -> list[dict[str, Any]]:
        state = self.client.state
        result: list[dict[str, Any]] = []
        for order, name in enumerate(RECOMMENDED_QUEST_ORDER):
            definition = self.catalog[name]
            live = state.quests.get(name)
            live_parts = {part.name: part for part in (live.parts if live else [])}
            parts = []
            for part in flatten_parts(definition.parts):
                progress = live_parts.get(part.name)
                objectives = [self._objective(obj) for obj in part.objectives]
                ready = bool(part.objectives) and all(
                    self._objective_ready(obj) if obj.kind == "item" else
                    bool(progress and progress.current is not None and
                         progress.required is not None and
                         progress.current >= progress.required)
                    for obj in part.objectives
                )
                parts.append({
                    "name": part.name,
                    "uid": part.uid,
                    "info": _clean_markup(part.info),
                    "status": progress.status if progress else "locked",
                    "current": progress.current if progress else None,
                    "required": progress.required if progress else None,
                    "objectives": objectives,
                    "ready_to_turn_in": ready,
                })
            if live and live.status == "completed":
                availability = "completed"
            elif live:
                availability = "active"
            else:
                availability = "available"
            if name == "Escaping the Deserted Island":
                giver, location = "Sam Goodberry", "/shattered_islands/world_-7_76"
            else:
                action = POLICIES[name].start
                giver = action.npc or "Automatic map trigger"
                location = action.place.map_path if action.place else ""
            requirements: list[str] = []
            if name == "Crazymix's Alchemical Reagents":
                requirements.append("Frevia's quest supplies the unique flower")
            if name == "Lost Memories":
                requirements.append("Reach Incuna after the Deserted Island voyage")
            if name == "Clearhaven Mine":
                requirements.append("Recover one bomb from each of ten storage chests")
            if name == "Portal of Llwyfen":
                requirements.append("Final encounter is the level-72 Nyhelobo")
            result.append({
                "name": name,
                "order": order + 1,
                "availability": availability,
                "suggested_level": SUGGESTED_LEVELS[name],
                "giver": giver,
                "start_location": location,
                "requirements": requirements,
                "parts": parts,
            })
        return result

    def unlocks(self) -> dict[str, Any]:
        """Her unlock state, read-only: the keys she carries and where."""
        gates = getattr(self, "_gate_counts", None)
        if gates is None:
            gates = self._gate_counts = gate_counts(self.graph)
        return unlock_state(self.client, self.graph).summary(gates)

    # Decisions the Blocks section lists: each care crossing and its outcome.
    CARE_ACTIONS = ("essential-route-override", "essential-care-starving",
                    "essential-care-food",
                    "essential-care-timeout", "essential-care-alert",
                    "essential-care-wait", "essential-care-staging",
                    "essential-care-yield")
    # A food trip's crossing (hungry or starving) stays alerted this long
    # (seconds).
    STARVING_CROSSING_ALERT_SECONDS = 3600.0

    def care_crossings(self) -> list[dict[str, Any]]:
        """Every care crossing under way: her task tree and idle survival."""
        roots = [self.engine.task, getattr(getattr(self.engine, "idle_survival", None),
                                           "child", None)]
        found, seen = [], set()
        pending = [(root, 0) for root in roots]
        while pending:
            current, depth = pending.pop()
            if not isinstance(current, BotTask) or id(current) in seen or depth > 10:
                continue
            seen.add(id(current))
            if isinstance(current, NavigateTask):
                status = current.care_status()
                if status is not None and current.status in (TaskStatus.READY,
                                                             TaskStatus.RUNNING):
                    found.append(status)
            for value in vars(current).values():
                if isinstance(value, BotTask):
                    pending.append((value, depth + 1))
        return found

    def route_blocks(self) -> dict[str, Any]:
        """The owner's blocks, the care crossings under way and the recent
        crossing decisions (docs/ROUTING.md 4.7, 4.8)."""
        now = time.time()
        blocks = [{**row, "remaining_seconds": round(row["until"] - now)}
                  for row in route_blocks.active_blocks(self.client, now=now)]
        decisions = [record for record in self.client.decision_history[-2000:]
                     if isinstance(record, dict) and
                     record.get("action") in self.CARE_ACTIONS]
        return {"blocks": blocks, "care": self.care_crossings(),
                "decisions": [{key: record.get(key) for key in
                               ("time", "action", "detail", "map", "task")}
                              for record in reversed(decisions[-25:])],
                "min_seconds": route_blocks.MIN_BLOCK_SECONDS,
                "max_seconds": route_blocks.MAX_BLOCK_SECONDS,
                "character_loaded": bool(getattr(self.client, "_memory_character", "")),
                "care_wait_seconds": CARE_WAIT_SECONDS}

    def route_learned(self) -> dict[str, Any]:
        """Her learned route subjects, read-only (docs/ROUTING.md 4.4, 4.8):
        the highest provisional costs, their signals and when each falls
        below half. The learned cost model (route_costs.py) reads the same
        rows with the kit rule and recovery."""
        recorder = route_events.recorder(self.client)
        if recorder is None:
            return {"character_loaded": False, "subjects": []}
        try:
            summary = recorder.learned(self.client)
        except Exception as error:   # never let the panel break /api/state
            log.warning("learned route subjects unavailable: %s", error)
            return {"character_loaded": bool(recorder.character), "subjects": [],
                    "error": "learned subjects unavailable"}
        return {"subjects": [], **summary,
                "weights": route_events.WEIGHTS, "half_lives": route_events.HALF_LIVES}

    def route_shadow(self) -> dict[str, Any]:
        """Shadow planning with learned costs, read-only (docs/ROUTING.md
        4.8, P6): the flag, comparisons per trip kind, the last
        differences and the last farm comparison."""
        value = route_shadow.shadow(self.client)
        if value is None:
            return {"enabled": False, "counts": {}, "recent": []}
        try:
            return value.summary(self.client)
        except Exception as error:   # never let the panel break /api/state
            log.warning("route shadow panel unavailable: %s", error)
            return {"enabled": False, "counts": {}, "recent": [],
                    "error": "shadow planning unavailable"}

    def care_alerts(self) -> list[dict[str, str]]:
        """A care wait across an owner block past CARE_WAIT_SECONDS, and a
        food trip's crossing, hungry or starving (owner-approved; ROUTING 4.5)."""
        alerts = []
        for care in self.care_crossings():
            subjects = ", ".join(care["subjects"])
            if (care["mode"] == CARE_OWNER_BLOCK and not care["cleared"] and
                    care["waiting_seconds"] >= CARE_WAIT_SECONDS):
                alerts.append({
                    "id": "essential-care-wait", "level": "critical",
                    "text": (f"Essential {care['trip']} trip waiting "
                             f"{int(care['waiting_seconds'] // 60)} min to cross the "
                             f"owner block {subjects}: needs "
                             f"{', '.join(care['missing']) or 'nothing more'}")})
            if care["cleared"] and care.get("food"):
                alerts.append({
                    "id": ("essential-care-starving" if care["starving"]
                           else "essential-care-food"), "level": "critical",
                    "text": (f"{'Starving' if care['starving'] else 'Hungry'}: food trip "
                             f"crossing the owner block {subjects} with only "
                             f"{'a ready recall and ' if care.get('recall_ready') else ''}"
                             "hold-fire")})
        food_ids = ("essential-care-starving", "essential-care-food")
        if not any(alert["id"] in food_ids for alert in alerts):
            cutoff = time.time() - self.STARVING_CROSSING_ALERT_SECONDS
            latest = next((record for record in reversed(self.client.decision_history)
                           if isinstance(record, dict) and
                           record.get("action") in food_ids and
                           float(record.get("time", 0.0) or 0.0) >= cutoff), None)
            if latest is not None:
                alerts.append({
                    "id": latest["action"], "level": "warn",
                    "text": ("A food trip crossed an owner block in the last hour "
                             "with only hold-fire (and a ready recall if she had one): "
                             f"{latest.get('detail', '')}")})
        return alerts

    def quest_journal(self) -> list[dict[str, Any]]:
        """Return only the live entries supplied by the server's Q journal."""
        return [{
            "name": quest.name,
            "status": quest.status,
            "parts": [{
                "name": part.name, "description": part.description,
                "status": part.status, "current": part.current,
                "required": part.required,
            } for part in quest.parts],
        } for quest in sorted(
            self.client.state.quests.values(),
            key=lambda quest: (quest.status == "completed",
                               quest.name.casefold()),
        )]

    @staticmethod
    def task_trace(task: BotTask | None) -> list[dict[str, Any]]:
        """Expose nested task state without coupling the UI to task classes."""
        result: list[dict[str, Any]] = []
        seen: set[int] = set()

        def visit(current: BotTask | None, depth: int) -> None:
            if current is None or id(current) in seen or depth > 8:
                return
            seen.add(id(current))
            entry: dict[str, Any] = {
                "depth": depth, "name": current.name,
                "class": type(current).__name__,
                "status": current.status.value, "error": current.error,
            }
            destination = getattr(current, "destination", None)
            destination_xy = getattr(current, "destination_xy", None)
            if destination:
                entry["destination"] = destination
            if destination_xy is not None:
                entry["coordinates"] = list(destination_xy)
            now = time.monotonic()
            if isinstance(current, FarmCircuitTask):
                minute = current.server_clock.game_minute()
                clock_text = (f"{int(minute) // 60:02d}:"
                              f"{int(minute) % 60:02d}"
                              if minute is not None else "unsynced")
                service = next((
                    getattr(current, attr, None)
                    for attr in ("_bank_sync", "_cure", "_restoration",
                                 "_resupply", "_storage", "_bed_binding",
                                 "_selling", "_banking", "_ship_key",
                                 "_spell_purchase", "_recall_shopping",
                                 "_utility_shopping", "_shopping",
                                 "_identification", "_capability",
                                 "_expedition_return")
                    if isinstance(getattr(current, attr, None), BotTask)
                ), None)
                at_farm = bool(
                    service is None and current.child is not None and
                    current._current_map_path ==
                    current.child.navigation.destination and
                    current.child.navigation.status == TaskStatus.COMPLETE)
                phase = ("maintenance" if service is not None else
                         "farming" if at_farm else "traveling")
                elapsed = (max(0.0, now - current._farm_started_at)
                           if at_farm and current._farm_started_at else 0.0)
                xp_gained = max(
                    0, current._current_exp - current._starting_exp)
                xp_elapsed = (max(
                    0.0, current.clock() - current._xp_started_at)
                    if current._xp_started_at else 0.0)
                xp_per_hour = (xp_gained * 3600.0 / xp_elapsed
                               if xp_elapsed > 0.0 else 0.0)
                combat_level = current._progression_level
                entry["detail"] = (
                    f"leg {current.leg_index + 1}/{len(current.legs)} · "
                    f"server {clock_text} · combat L{combat_level} · "
                    f"{phase} · "
                    f"+{xp_gained:,} XP · {xp_per_hour:,.0f}/h" +
                    (f" {elapsed:.0f}s" if at_farm else ""))
                entry["circuit"] = {
                    "leg": current.leg_index + 1,
                    "legs": len(current.legs),
                    "server_time": clock_text,
                    "combat_level": combat_level,
                    "phase": phase,
                    "xp_gained": xp_gained,
                    "xp_per_hour": round(xp_per_hour, 1),
                    "dwell_seconds": round(elapsed, 1),
                    "empty_confirmation_seconds": round(
                        max(0.0, now - current._empty_spawn_since), 1)
                        if current._empty_spawn_since else 0.0,
                }
            if isinstance(current, ShopUpgradeSweepTask):
                total = len(current.waypoints)
                position = min(current.index + 1, total) if total else 0
                waypoint = (current.waypoints[current.index]
                            if current.index < total else None)
                entry["detail"] = f"stock waypoint {position}/{total}"
                entry["shop_sweep"] = {
                    "waypoint": position,
                    "waypoints": total,
                    "current": ({
                        "map": waypoint[0],
                        "coordinates": list(waypoint[1]),
                    } if waypoint is not None else None),
                }
            if isinstance(current, NavigateTask):
                navigation_complete = current.status == TaskStatus.COMPLETE
                progress_age = (max(0.0, now - current._last_progress)
                                if (not navigation_complete and
                                    current._last_progress > 0.0) else None)
                progress_text = ("complete" if navigation_complete else
                                 f"progress {progress_age:.1f}s ago"
                                 if progress_age is not None else
                                 "progress pending")
                edge = (current.route[0]
                        if current.route and not navigation_complete else None)
                remaining_edges = (0 if navigation_complete else
                                   len(current.route))
                entry["detail"] = (
                    f"route edges {remaining_edges} · "
                    f"live blockers {len(current._runtime_blocked)} · "
                    f"occupants {len(current._temporary_blocked)} · "
                    f"route threats {len(current._route_threat_maps)}" +
                    (" (fallback) · " if current._threat_fallback else " · ") +
                    f"seam retries {len(current._failed_tile_crossings)} · "
                    f"{progress_text}")
                entry["navigation"] = {
                    "remaining_edges": remaining_edges,
                    "current_edge": ({
                        "source": edge.source,
                        "destination": edge.destination,
                        "departure": [edge.x, edge.y],
                        "kind": edge.kind,
                        "label": edge.label,
                    } if edge is not None else None),
                    "issued_goal": (list(current._issued_goal)
                                    if current._issued_goal else None),
                    "issued_click": (list(current._issued_click)
                                     if current._issued_click else None),
                    "runtime_blocked": [
                        list(point)
                        for point in sorted(current._runtime_blocked)[-20:]
                    ],
                    "threat_blocked": [
                        list(point)
                        for point in sorted(current._threat_blocked)[-20:]
                    ],
                    "temporary_blocked": [
                        list(point)
                        for point in sorted(current._temporary_blocked)[-20:]
                    ],
                    "threat_maps": sorted(current._route_threat_maps),
                    "threat_fallback": current._threat_fallback,
                    "failed_tile_crossings": [
                        {
                            "source": source,
                            "destination": destination,
                            "crossing": [x, y],
                        }
                        for source, destination, x, y in sorted(
                            current._failed_tile_crossings)[-20:]
                    ],
                    "progress_age_seconds": (round(progress_age, 1)
                                             if progress_age is not None else
                                             None),
                    "essential": current.essential,
                    "care": current.care_status(),
                }
            if isinstance(current, FarmTask):
                engaged = (current._engaged_target[0]
                           if current._engaged_target else 0)
                corpse_tags = sorted(current._corpse_take_all)
                last_age = (max(0.0, now - current._last_action)
                            if current._last_action else 0.0)
                entry["detail"] = (
                    f"visible {current._visible_target_count} · "
                    f"engaged {engaged or 'none'} · "
                    f"corpse phases {len(corpse_tags)} · "
                    f"last order {last_age:.1f}s")
                entry["farm"] = {
                    "visible_targets": current._visible_target_count,
                    "engaged_target": engaged,
                    "corpse_phase_tags": corpse_tags,
                    "suspected_corpse_tiles": len(
                        current._suspected_corpse_tiles),
                    "ignored_corpse_tiles": len(
                        current._ignored_corpse_tiles),
                    "last_action_age_seconds": round(last_age, 1),
                }
            result.append(entry)
            active_service = next((
                getattr(current, attr, None)
                for attr in ("_bank_sync", "_cure", "_restoration",
                             "_resupply", "_identification", "_storage",
                             "_bed_binding", "_selling", "_banking",
                             "_ship_key", "_spell_purchase",
                             "_recall_shopping", "_utility_shopping",
                             "_shopping", "_capability",
                             "_expedition_return")
                if isinstance(getattr(current, attr, None), BotTask)
            ), None)
            if active_service is not None:
                visit(active_service, depth + 1)
                return
            for attr in ("child", "navigation", "dialog", "task"):
                nested = getattr(current, attr, None)
                if isinstance(nested, BotTask):
                    visit(nested, depth + 1)

        visit(task, 0)
        return result

    def map(self) -> dict[str, Any]:
        state, game_map = self.client.state, self.client.state.map
        cx, cy = game_map.width // 2, game_map.height // 2
        tiles = []
        for y in range(game_map.height):
            for x in range(game_map.width):
                tile = game_map.tiles.get((x, y))
                objects = []
                if tile:
                    for obj in sorted(tile.objects.values(), key=lambda item: item.layer):
                        objects.append({
                            "name": obj.name,
                            "face": map_object_visual_name(self.client, obj),
                            "animation": self.client.animations.get(
                                obj.animation, "") if obj.animation else "",
                            "visual_id": obj.animation or obj.face,
                            "layer": obj.layer,
                            "target": bool(obj.target_id),
                            "target_id": obj.target_id,
                            "friendly": obj.is_friend,
                            "hp": obj.target_hp,
                        })
                tiles.append({
                    "x": x, "y": y,
                    "world_x": game_map.world_x + x - cx,
                    "world_y": game_map.world_y + y - cy,
                    "player": x == cx and y == cy,
                    "fogged": bool(tile and tile.fogged),
                    "objects": objects,
                })
        return {
            "name": game_map.name, "path": game_map.path,
            "region": game_map.region_longname or game_map.region,
            "weather": game_map.weather, "width": game_map.width,
            "height": game_map.height, "x": game_map.world_x,
            "y": game_map.world_y, "tiles": tiles,
        }

    def engine_error(self) -> dict[str, Any] | None:
        """The task engine's current run of failed steps, or None."""
        count = int(getattr(self.engine, "step_error_count", 0) or 0)
        if not count:
            return None
        return {"error": str(getattr(self.engine, "step_error", "")),
                "count": count,
                "since": float(getattr(self.engine, "step_error_since", 0.0))}

    def farm_encounter_warning(self, autoplay) -> str:
        """The encounter warning of the circuit autoplay farms now
        (FarmCircuitTask._note_encounter), or "": none once autoplay farms
        another circuit, does something else or stops."""
        warning = getattr(self.client, "farm_encounter_warning", None)
        if (not isinstance(warning, dict) or autoplay is None or
                getattr(autoplay, "child", None) is not warning.get("circuit")):
            return ""
        return str(warning.get("text", ""))

    def build_status(self, autoplay) -> dict[str, Any] | None:
        """The build of the circuit autoplay farms now
        (FarmCircuitTask._publish_build), or None, as farm_encounter_warning."""
        status = getattr(self.client, "build_status", None)
        if (not isinstance(status, dict) or autoplay is None or
                getattr(autoplay, "child", None) is not status.get("circuit")):
            return None
        return status

    def build_alerts(self, autoplay) -> list[dict[str, str]]:
        """A build she cannot field (with its fallback), and the money the
        spell her wanted build needs is short of."""
        status = self.build_status(autoplay)
        if status is None:
            return []
        alerts = []
        if status.get("fallback"):
            alerts.append({"id": "build-gap", "level": "warn",
                           "text": "Build gap: " + str(status.get("reason", ""))})
        if status.get("shortfall"):
            alerts.append({"id": "build-funds", "level": "warn",
                           "text": "Get this build: " + str(status["shortfall"])})
        failed = getattr(self.client, "learn_errand_failed", None) or {}
        for reason in failed.values():
            alerts.append({"id": "build-errand-failed", "level": "warn",
                           "text": f"Learn errand failed: {reason}"})
        return alerts

    def alerts(self) -> list[dict[str, str]]:
        """Alerts for the operator: deaths, starvation, stuck, disconnected,
        and while farming is unsafe its suspension and what blocks it."""
        watch = self.client.progress_watch
        task = self.engine.task
        autoplay = bool(task is not None and
                        str(getattr(task, "name", "")).startswith("autoplay"))
        # Moves happen between stats packets; the client reports disconnects.
        watch.observe(self.client.state)
        recovery = getattr(task, "recovery_alerts", None) if autoplay else None
        unsafe, waiting = recovery(self.client) if recovery else ([], False)
        alerts = active_alerts(
            self.client.state, self.client.decision_history, watch,
            autoplay_started_at=(float(getattr(task, "started_at", 0.0) or 0.0)
                                 if autoplay else None),
            task_running=task is not None, waiting_explained=waiting,
            engine_error=self.engine_error(),
            farm_encounter=self.farm_encounter_warning(task if autoplay else None))
        care = self.care_alerts()
        if any(alert["id"] == "essential-care-wait" for alert in care):
            # The care alert explains the wait; no stuck alert besides it.
            alerts = [alert for alert in alerts if alert["id"] != "stuck"]
        alerts[0:0] = [alert for alert in unsafe + care if alert["level"] == "critical"]
        alerts += [alert for alert in unsafe + care if alert["level"] != "critical"]
        alerts += self.build_alerts(task if autoplay else None)
        # Filed feedback that needs the owner, as last counted by the
        # running bot's queue screen (no database read on a live poll).
        attention = getattr(self.client, "feedback_attention", None) or {}
        for status, level, text in (
                ("filed-unlabelled", "warn", "filed without its labels or type: add them "
                                             "on GitHub"),
                ("needs-review", "info", "possibly a duplicate: decide with "
                                         "`feedback approve --file-anyway` or "
                                         "`feedback reject --duplicate`"),
                ("failed", "warn", "failed to file: see `feedback show`")):
            count = int(attention.get(status, 0) or 0)
            if count:
                alerts.append({"id": f"feedback-{status}", "level": level,
                               "text": f"{count} feedback item{'s' if count != 1 else ''} "
                                       f"{text}"})
        assist = self.operator_assist_status()
        if assist and assist["running"]:
            alerts.append({"id": "operator-assist-running", "level": "warn",
                           "text": "Operator assist running: she is held still"})
        if assist and assist["assisted"]:
            since = time.strftime("%Y-%m-%d", time.gmtime(assist["first_assisted_at"]))
            alerts.append({
                "id": "operator-assist", "level": "info",
                "text": (f"Operator-assisted since {since} "
                         f"({assist['copper_total'] // 10_000} gold): later "
                         "progress is not all earned")})
        return alerts

    def snapshot(self, include: set[str] | None = None) -> dict[str, Any]:
        state, task = self.client.state, self.engine.task
        inventory = []
        skills = []
        equipped = set(state.equipment.values())
        for item in state.inventory:
            inventory.append({
                "tag": item.tag, "name": item.name, "quantity": item.quantity,
                "face_id": item.face,
                "face": item_visual_name(self.client, item),
                "weight": item.weight, "type": item.item_type,
                "quality": item.quality, "condition": item.condition,
                "identified": InventoryPolicy().identified(item),
                "required_level": item.required_level,
                "applied": bool(item.flags & c.ITEM_APPLIED),
                "locked": bool(item.flags & c.ITEM_LOCKED),
                "magical": bool(item.flags & c.ITEM_MAGICAL),
                "cursed": bool(item.flags & (c.ITEM_CURSED | c.ITEM_DAMNED)),
                "damned": bool(item.flags & c.ITEM_DAMNED),
                "unpaid": bool(item.flags & c.ITEM_UNPAID),
                "trapped": bool(item.flags & c.ITEM_TRAPPED),
                "equipped": item.tag in equipped,
                "extra": item.extra,
            })
            if item.item_type == c.TYPE_SKILL:
                level = int(item.extra.get("level", 0) or 0)
                experience = int(item.extra.get("experience", 0) or 0)
                skills.append({
                    "name": item.name, "level": level,
                    **experience_progress(level, experience),
                })
        messages = [{"time": entry[0], "type": entry[1], "color": entry[2],
                     "text": _clean_markup(entry[3])}
                    for entry in state.messages[-150:]]
        interface = None
        if state.interface:
            interface = {
                "title": state.interface.title,
                "text": _clean_markup(state.interface.text),
                "links": [_clean_markup(link) for link in state.interface.links],
                "objects": [{
                    "tag": item.tag, "name": item.name,
                    "quantity": item.quantity, "type": item.item_type,
                    "quality": item.quality,
                } for item in state.interface.objects],
            }
        task_data = None if task is None else {
            "name": task.name, "status": task.status.value,
            "error": task.error, "started_at": task.started_at,
            "trace": self.task_trace(task),
        }
        ground = [{
            "tag": item.tag, "name": item.name, "quantity": item.quantity,
            "face_id": item.face,
            "face": item_visual_name(self.client, item),
            "type": item.item_type,
            "quality": item.quality, "condition": item.condition,
            "identified": (item.quality != 255
                           if item.item_type == 0 else
                           InventoryPolicy().identified(item)),
            "unpaid": bool(item.flags & c.ITEM_UNPAID),
            "cursed": bool(item.flags & (c.ITEM_CURSED | c.ITEM_DAMNED)),
            "magical": bool(item.flags & c.ITEM_MAGICAL),
            "required_level": item.required_level,
            "inferred_item_skill": item.extra.get("inferred_item_skill", 0),
            "inferred_skill_name": item.extra.get("inferred_skill_name", ""),
            "container_items": [
                state.items[tag].name
                for tag in state.inventories.get(item.tag, [])
                if tag in state.items
            ],
        } for item in state.ground]
        assist = self.operator_assist_status()
        return {
            "server_time": time.time(), "uptime": time.time() - self.started_at,
            "build": build_info(),
            "phase": state.phase, "connected": state.phase != "disconnected",
            "inventory_ready": state.inventory_replay_complete,
            "character": state.player_name, "account": state.account,
            "party": state.party_name,
            "chat_policy": self.client.chat_policy_status(),
            "stats": state.stats,
            "protections": {
                name: int(state.protections.get(index, 0) or 0)
                for index, name in enumerate(c.ATTACK_NAMES)
            },
            "level_progress": experience_progress(
                int(state.stats.get("level", 0) or 0),
                int(state.stats.get("exp", 0) or 0)),
            "target": {
                "id": state.target_id,
                "name": state.target_name, "combat": state.combat,
                # TARGET_SELF packets arrive before a later stats packet can
                # clear target_hp. Do not attribute the defeated monster's
                # stale percentage to the player in the dashboard.
                "hp": (state.stats.get("target_hp", 0)
                       if state.target_id else 0),
            },
            "conditions": {
                "depletion_points": state.depletion_points,
                "depletion_points_known": state.depletion_points_known,
                "depletion_threshold":
                    FarmCircuitTask.DEPLETION_SERVICE_THRESHOLD,
            },
            "economy": {
                "carried": BuyShopUpgradeTask.carried_wallet_value(self.client),
                "banked": state.bank_balance,
                "bank_known": state.bank_balance_known,
                "total": BuyShopUpgradeTask.wallet_value(self.client),
                # Copper an operator gave her: not earned in play.
                "operator_assisted_copper": assist["copper_total"] if assist else 0,
            },
            "operator_assist": assist,
            "task": task_data, "last_task": self.engine.last_task,
            "engine_error": self.engine_error(),
            "map": self.map(), "inventory": inventory, "ground": ground,
            "skills": sorted(skills, key=lambda skill: (-skill["level"], skill["name"])),
            "farm_spots": self.analyzer_farm_spots(),
            "navigation_spots": navigation_spot_catalog(self.graph),
            "actions": self.client.action_history[-200:],
            "decisions": self.client.decision_history[-500:],
            "current_action": (self.client.action_history[-1]
                               if self.client.action_history else None),
            "messages": messages, "interface": interface,
            "quests_loaded": state.quests_loaded, "quests": self.quests(),
            "quest_journal": self.quest_journal(),
            "unlocks": self.unlocks(),
            "route_blocks": self.route_blocks(),
            "farming_analysis": self.farming_analysis(),
            "post_mortems": list(reversed(
                getattr(self.client, "post_mortems", [])[-10:])),
            "alerts": self.alerts(),
            # Built only for the polls that ask for it (the slow one).
            **({"equipment": self.equipment()}
               if include is None or "equipment" in include else {}),
            **({"route_learned": self.route_learned()}
               if include is None or "route_learned" in include else {}),
            **({"dialogue_edges": self.dialogue_edges()}
               if include is None or "dialogue_edges" in include else {}),
            **({"route_shadow": self.route_shadow()}
               if include is None or "route_shadow" in include else {}),
        }

    def dialogue_edges(self) -> dict[str, Any]:
        """The dialogue_edges flag, each compiled voyage and whether it is
        offered and why, and the quest-gated exits (read-only;
        docs/ROUTING.md P4)."""
        try:
            return dialogue_summary(self.client, self.graph)
        except Exception as error:   # never let the panel break /api/state
            log.warning("voyage panel unavailable: %s", error)
            return {"enabled": False, "voyages": [], "gated_exits": [],
                    "error": f"voyages unavailable: {error}"}

    def equipment(self) -> dict[str, Any]:
        """The Equipment panel: effective stats per gear family and where
        they would choose differently from the current score (read-only)."""
        examiner = getattr(self.client, "gear_examiner", None)
        # The policy her farm last examined with (caster or melee scoring).
        policy = getattr(examiner, "policy", None) or InventoryPolicy()
        try:
            return equipment_report(self.client, policy,
                                    carried_gear_score(self.client, policy),
                                    recall=working_recall(self.client))
        except Exception as error:   # never let the panel break /api/state
            log.warning("equipment panel unavailable: %s", error)
            return {"families": [], "examined": 0, "shadow": [],
                    "error": f"equipment unavailable: {error}"}

    def prometheus(self) -> str:
        """The Prometheus exposition: activity counters and live gauges."""
        client = self.client
        state = client.state
        predicted = None
        choice = self._farm_choice()
        if choice is not None:
            # The spot autoplay farms, not the analyzer's #1 (approach
            # preference and live failures can pass over it).
            rate = choice[2].get("xp_hour")
            if isinstance(rate, (int, float)) and not isinstance(rate, bool):
                predicted = (choice[0], float(rate))
        return client.metrics.render(
            # An empty inventory before it loads is not an empty purse.
            carried=(BuyShopUpgradeTask.carried_wallet_value(client)
                     if state.phase == "playing" and
                     state.inventory_replay_complete else None),
            banked=state.bank_balance if state.bank_balance_known else None,
            predicted_xp=predicted,
            operator_assist=self.operator_assist_status())

    def analyzer_farm_spots(self) -> list[dict[str, Any]]:
        """The preferred objective's ranked spots, as manual farm circuits."""
        analyzer = getattr(self.client, "farm_analyzer", None)
        if analyzer is None:
            return []
        objective = self._farmed_objective()
        spots = []
        # The ranking autoplay chooses from: seam exposure folded in, and
        # spots it leaves out not offered.
        for rank, spot in enumerate(seam_ranking(self.client, self.graph, objective), 1):
            legs = analyzer_spot_legs(spot, self.graph)
            if legs:
                spots.append({
                    "id": str(spot.get("path") or rank),
                    "name": f"#{rank} {spot.get('name', '?')}",
                    "circuit": [{"zone": zone, "target": target}
                                for zone, target in legs],
                })
        return spots

    def _farm_choice(self):
        """analyzer_farm_choice's (objective, legs, spot) for now, or None.

        Read-only: the dashboard and Prometheus never run a whole-world
        route search on the event loop (cached results only).
        """
        if getattr(self.client, "farm_analyzer", None) is None:
            return None
        return analyzer_farm_choice(self.client, self.graph, search=False)

    def _farmed_objective(self) -> str:
        """The objective she farms now, after depletion and its fallback."""
        choice = self._farm_choice()
        return (choice[0] if choice is not None
                else effective_farm_objective(self.client.state))

    def build_snapshot(self) -> dict[str, Any]:
        """The dashboard's build panel: the operator's choices, the build
        for the farmed spot and why, gaps, and the suggested set."""
        task = getattr(self.engine, "task", None)
        autoplay = (task if task is not None and
                    str(getattr(task, "name", "")).startswith("autoplay") else None)
        status = self.build_status(autoplay)
        choices = dict(getattr(self.client.state, "build_choices", {}) or {})
        snapshot: dict[str, Any] = {
            "choices": list(builds.CHOICES),
            "labels": {name: build.label for name, build in builds.BUILDS.items()},
            "every_spot": choices.get(builds.EVERY_SPOT, builds.AUTO),
            "status": None, "suggestion": None}
        if status is not None:
            snapshot["status"] = {key: value for key, value in status.items()
                                  if key != "circuit"}
            snapshot["status"]["errand_failed"] = "; ".join(
                (getattr(self.client, "learn_errand_failed", None) or {}).values())
            snapshot["spot_choice"] = choices.get(status.get("spot", ""), builds.AUTO)
            try:
                snapshot["suggestion"] = status["circuit"].build_suggestion(self.client)
            except Exception as error:  # The panel must never break the state.
                snapshot["suggestion"] = {"error": str(error)}
        return snapshot

    def farming_analysis(self) -> dict[str, Any]:
        analyzer = getattr(self.client, "farm_analyzer", None)
        chosen = getattr(self.client.state, "farm_objective", DEFAULT_OBJECTIVE)
        choice = self._farm_choice()
        objective = (choice[0] if choice is not None
                     else effective_farm_objective(self.client.state))
        if analyzer is None:
            snapshot = {"status": "unavailable", "objective": objective,
                        "objectives": OBJECTIVES, "ranking": []}
        else:
            snapshot = dict(analyzer.snapshot(
                objective, seam_ranking(self.client, self.graph, objective)))
        # The ranking follows what she farms now; the toggle shows the choice.
        snapshot["selected_objective"] = chosen
        # The spot autoplay farms, with its rank in that objective's ranking.
        snapshot["farmed_spot"] = None
        if choice is not None:
            spot = choice[2]
            ranked = [item.get("path")
                      for item in seam_ranking(self.client, self.graph, choice[0])]
            snapshot["farmed_spot"] = {
                **{key: spot.get(key) for key in SUMMARY_FIELDS},
                "approach_risky": risky_approach(spot),
                "spot_risky": risky_spot(spot),
                # Warn only (farm_analyzer.encounter_warning).
                "encounter_warning": encounter_warning(spot),
                "rank": (ranked.index(spot.get("path")) + 1
                         if spot.get("path") in ranked else None)}
        snapshot["objective_override"] = (
            {"reason": "depletion",
             "points": int(self.client.state.depletion_points)}
            if objective != chosen else None)
        adaptations = getattr(self.client.state, "farm_adaptations", {}) or {}
        recent = sorted(adaptations.items(),
                        key=lambda item: float(item[1].get("updated_at", 0) or 0),
                        reverse=True)[:3]
        snapshot["self_improvement"] = {
            "spots": [{"spot": key, "level": record.get("level", 0),
                       "max_level": self_improvement.MAX_LEVEL,
                       "causes": record.get("causes", {}),
                       "reviews": list(record.get("reviews", ()))[-3:]}
                      for key, record in recent],
            "goals": self_improvement.improvement_goals(adaptations),
        }
        snapshot["build"] = self.build_snapshot()
        state = self.client.state
        bed_xy = getattr(state, "savebed_xy", None)
        snapshot["savebed"] = ({"map": state.savebed_map, "x": bed_xy[0], "y": bed_xy[1]}
                               if getattr(state, "savebed_map", "") and bed_xy else None)
        return snapshot


class WebControlServer:
    def __init__(self, client: AtrinikClient, engine: TaskEngine, graph: WorldGraph,
                 host: str = "127.0.0.1", port: int = 8765, *,
                 operator_character: str = "",
                 operator_assist: OperatorAssist | None = None,
                 operator_key: str = ""):
        if not self._is_loopback_host(host):
            raise ValueError("the operator dashboard may only listen on loopback")
        self.client, self.engine, self.graph = client, engine, graph
        self.host, self.port = host, port
        self.catalog = load_catalog()
        self.dashboard = DashboardState(client, engine, graph, self.catalog)
        # Only this server holds the operator assist: the client and the task
        # engine (so autoplay and every task) have no way to reach it.
        self.operator_assist = operator_assist or OperatorAssist(
            client, engine, operator_character)
        self.dashboard.operator_assist = self.operator_assist
        # The operator character never appears in a feedback draft.
        private = getattr(client, "feedback_private_names", None)
        operator = getattr(self.operator_assist, "operator_character", "") or operator_character
        if isinstance(private, set) and operator:
            private.add(operator)
        # The owner-only key /api/operator-assist requires; empty refuses all.
        self.operator_key = operator_key
        self._key_refusals_logged_at = 0.0
        # Whether the unpersistable-Stop warning was logged (once).
        self._unkept_stop_warned = False
        self._key_refusals_unlogged = 0
        client.metrics.attach(engine)
        self.face_images = FaceImages(
            getattr(client, "_runtime_content_root", Path(".")))
        self.server: asyncio.Server | None = None

    @staticmethod
    def _is_loopback_host(host: str | None) -> bool:
        if not host:
            return False
        if host.casefold() == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def _valid_host_header(self, authority: str) -> bool:
        try:
            host = urlsplit("//" + authority).hostname
        except ValueError:
            return False
        return self._is_loopback_host(host)

    def _valid_origin(self, origin: str) -> bool:
        try:
            parsed = urlsplit(origin)
            ports = {
                socket.getsockname()[1]
                for socket in (self.server.sockets if self.server else ())
            }
            return (parsed.scheme in ("http", "https") and
                    self._is_loopback_host(parsed.hostname) and
                    parsed.port in ports)
        except ValueError:
            return False

    async def serve(self) -> None:
        self.server = await asyncio.start_server(self._handle, self.host, self.port)
        addresses = ", ".join(str(sock.getsockname()) for sock in self.server.sockets or [])
        log.info("web dashboard listening on http://%s", addresses)
        async with self.server:
            await self.server.serve_forever()

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _reply(self, writer: asyncio.StreamWriter, status: int, body: bytes,
                     content_type: str = "application/json; charset=utf-8",
                     cache: str = "no-store") -> None:
        labels = {200: "OK", 201: "Created", 204: "No Content",
                  400: "Bad Request", 403: "Forbidden", 404: "Not Found",
                  405: "Method Not Allowed", 415: "Unsupported Media Type",
                  409: "Conflict", 413: "Content Too Large", 500: "Internal Server Error",
                  503: "Service Unavailable"}
        header = (f"HTTP/1.1 {status} {labels.get(status, 'Error')}\r\n"
                  f"Content-Type: {content_type}\r\nContent-Length: {len(body)}\r\n"
                  f"Cache-Control: {cache}\r\nX-Content-Type-Options: nosniff\r\n"
                  f"Content-Security-Policy: {CONTENT_SECURITY_POLICY}\r\n"
                  "Connection: close\r\n\r\n").encode()
        writer.write(header + body)
        await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5.0)
            if len(head) > 32768:
                await self._reply(writer, 413, _json_bytes({"error": "headers too large"}))
                return
            lines = head.decode("latin-1").split("\r\n")
            method, raw_target, _ = lines[0].split(" ", 2)
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.casefold()] = value.strip()
            if not self._valid_host_header(headers.get("host", "")):
                await self._reply(
                    writer, 400, _json_bytes({"error": "invalid Host header"}))
                return
            origin = headers.get("origin", "")
            if method == "POST" and origin and not self._valid_origin(origin):
                await self._reply(
                    writer, 403, _json_bytes({"error": "cross-origin control denied"}))
                return
            length = int(headers.get("content-length", "0"))
            if length > 1_000_000:
                await self._reply(writer, 413, _json_bytes({"error": "body too large"}))
                return
            body = await reader.readexactly(length) if length else b""
            target = urlsplit(raw_target)
            await self._route(writer, method, target.path, body, target.query,
                              headers)
        except (ValueError, json.JSONDecodeError, asyncio.IncompleteReadError) as exc:
            await self._reply(writer, 400, _json_bytes({"error": str(exc)}))
        except Exception as exc:
            log.exception("web request failed")
            await self._reply(writer, 500, _json_bytes({"error": str(exc)}))
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _route(self, writer: asyncio.StreamWriter, method: str,
                     path: str, body: bytes, query: str = "",
                     headers: dict[str, str] | None = None) -> None:
        if method == "GET" and path == "/api/state":
            include = {
                key for value in parse_qs(query).get("include", [])
                for key in value.split(",") if key}
            snapshot = self.dashboard.snapshot(include or None)
            if not include or "trends" in include:
                # A day of buckets: built only for polls that ask for it.
                snapshot["trends"] = self.client.trends.summary()
            if include:
                # The live view polls a small slice; heavy lists load on demand.
                snapshot = {key: value for key, value in snapshot.items()
                            if key in include}
            await self._reply(writer, 200, _json_bytes(snapshot))
            return
        if method == "GET" and path == "/metrics":
            # Read-only and secret-free (no account, password or invite
            # code), behind the same loopback listener and Host check as
            # /api/state.
            await self._reply(writer, 200, self.dashboard.prometheus().encode(),
                              METRICS_CONTENT_TYPE)
            return
        if method == "GET" and path == "/api/chat":
            # The durable chat log: ?sender=NAME&limit=N (newest N, oldest
            # first, up to 5000).
            params = parse_qs(query)
            character = (self.client.state.player_name or
                         self.client.config.character)
            history = self.client._memory_store().chat_history(
                character,
                limit=int((params.get("limit") or ["200"])[0]),
                sender=(params.get("sender") or [""])[0]) if character else []
            await self._reply(writer, 200, _json_bytes({"chat": history}))
            return
        if method == "GET" and path == "/api/knowledge":
            # Her observation ledger, read-only: ?kind=SOURCE_KIND&limit=N
            # (newest N, up to 1000). Rows passed the ingestion filter, so
            # they hold no chat and no other player's name.
            params = parse_qs(query)
            character = getattr(self.client, "_memory_character", "")
            # Facts too (facts.py): ?fact_kind=KIND&fact_limit=N.
            observations, counts, busy = [], {}, False
            facts, fact_counts = [], {}
            if character:
                memory = self.client._memory_store()
                try:
                    # A locked database must not hold up the event loop.
                    observations = memory.observations(
                        character, limit=int((params.get("limit") or ["100"])[0]),
                        source_kind=(params.get("kind") or [""])[0],
                        timeout=KNOWLEDGE_READ_TIMEOUT)
                    counts = memory.observation_counts(
                        character, timeout=KNOWLEDGE_READ_TIMEOUT)
                    facts = memory.facts(
                        character, limit=int((params.get("fact_limit") or ["100"])[0]),
                        kind=(params.get("fact_kind") or [""])[0],
                        timeout=KNOWLEDGE_READ_TIMEOUT)
                    fact_counts = memory.fact_counts(
                        character, timeout=KNOWLEDGE_READ_TIMEOUT)
                except sqlite3.OperationalError:
                    observations, counts, busy = [], {}, True
                    facts, fact_counts = [], {}
            await self._reply(writer, 200, _json_bytes({
                "observations": observations, "counts": counts, "busy": busy,
                "facts": facts, "fact_counts": fact_counts,
                "persist_tile_facts": bool(getattr(
                    self.client.config, "persist_tile_facts", False)),
                "authored_index": getattr(self.client, "authored_content", None) is not None}))
            return
        if method == "GET" and path == "/api/terrain":
            # Her fights by terrain (terrain.py), read-only: the newest
            # ?limit=N (up to 200) and the decayed rates over all kept.
            # Counts only: no attacker's or other player's name is stored.
            await self._reply(writer, 200, _json_bytes(self._terrain(parse_qs(query))))
            return
        if method == "GET" and path == "/api/owner-watch":
            # The operator character's fights she watched (owner_watch.py),
            # read-only: the newest ?limit=N (up to 200). Counts and
            # authored monster kinds only; no player's name, the operator
            # character's included, is stored or sent.
            await self._reply(writer, 200, _json_bytes(self._owner_watch(parse_qs(query))))
            return
        if method == "GET" and path == "/api/feedback":
            # Her feedback queue (feedback.py), read-only:
            # ?kind=KIND&repo=OWNER/NAME&status=STATUS&limit=N. Drafts hold
            # only authored text and her own state. Nothing is approved or
            # filed from here: the owner decides with the CLI on CT130
            # (owner decision, 2026-11-19) and only the separate filer
            # talks to GitHub. There is no feedback write route.
            params = parse_qs(query)
            character = getattr(self.client, "_memory_character", "")
            items, counts, busy = [], {}, False
            read_at = time.time()
            if character:
                memory = self.client._memory_store()
                try:
                    items = memory.feedback_items(
                        character, kind=(params.get("kind") or [""])[0],
                        repo=(params.get("repo") or [""])[0],
                        status=(params.get("status") or [""])[0],
                        limit=int((params.get("limit") or ["200"])[0]),
                        timeout=KNOWLEDGE_READ_TIMEOUT)
                    counts = memory.feedback_counts(character, timeout=KNOWLEDGE_READ_TIMEOUT)
                except sqlite3.OperationalError:
                    items, counts, busy = [], {}, True
            if items:
                # Checked against the current private terms on every read:
                # an item naming one is withheld (blanked here, flagged in
                # her memory) until a clean render replaces it.
                try:
                    newly = screen_feedback(items, self.client.feedback_private_terms())
                    memory = self.client._memory_store()
                    if newly:
                        memory.withhold_feedback(
                            character, newly, timeout=KNOWLEDGE_READ_TIMEOUT)
                    # Screened with her full terms: a reader without them
                    # (the CLI) may show these (feedback.readable).
                    memory.mark_feedback_screened(
                        character, [item["id"] for item in items if not item["withheld"]],
                        versions={item["id"]: item["updated_at"] for item in items},
                        at=read_at, timeout=KNOWLEDGE_READ_TIMEOUT)
                except Exception as error:
                    log.warning("feedback screening failed: %s", error)
                    items, busy = [], True
            for item in items:
                item["short_fingerprint"] = short_fingerprint(item["fingerprint"])
                try:
                    item["labels"] = json.loads(item.pop("labels_json") or "[]")
                except ValueError:
                    item["labels"] = []
                item.pop("evidence_json", None)
                try:
                    candidates = json.loads(item.pop("remote_candidates_json") or "[]")
                except ValueError:
                    candidates = []
                item["remote_candidates"] = (
                    [str(entry.get("url", "")) for entry in candidates
                     if isinstance(entry, dict)]
                    if item["status"] == "needs-review" and not item["withheld"] else [])
            await self._reply(writer, 200, _json_bytes({
                "items": items, "counts": counts, "busy": busy, "read_only": True,
                "kinds": list(FEEDBACK_KINDS), "repos": list(FEEDBACK_REPOS)}))
            return
        if method == "GET" and path.startswith("/api/face/"):
            image = self.face_images.png(path[len("/api/face/"):])
            if image is None:
                await self._reply(writer, 404, _json_bytes({"error": "unknown face"}))
            else:
                await self._reply(writer, 200, image, "image/png",
                                  cache="public, max-age=86400, immutable")
            return
        if method == "POST" and path == "/api/operator-assist":
            # The owner's one-shot `atrinik-playtester operator-assist`.
            # Reaching the dashboard is not enough (the lab proxy reaches
            # it with a loopback Host, and a POST without Origin passes):
            # the request must carry the owner-only key, as JSON, which an
            # HTML form cannot send cross-site.
            headers = headers or {}
            content_type = headers.get("content-type", "").split(";")[0].strip()
            if content_type.casefold() != "application/json":
                await self._reply(writer, 415, _json_bytes({
                    "ok": False, "error": "send application/json"}))
                return
            if not key_matches(self.operator_key, headers.get(KEY_HEADER.casefold(), "")):
                # Nothing is recorded: no SQLite row, and a bounded log.
                self._log_key_refusal()
                await self._reply(writer, 403, _json_bytes({
                    "ok": False, "outcome": "refused",
                    "error": ("operator assist is disabled"
                              if not self.operator_key else
                              "the operator key is missing or wrong")}))
                return
            data = json.loads(body or b"{}")
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            gold = data.get("gold")
            try:
                record = await self.operator_assist.run(gold, str(data.get("to", "")))
            except OperatorAssistRefused as refusal:
                await self._reply(writer, 409, _json_bytes({
                    "ok": False, "outcome": "refused", "error": str(refusal),
                    "max_gold_per_run": MAX_GOLD_PER_RUN}))
                return
            await self._reply(writer, 200, _json_bytes({
                "ok": record["outcome"] == "completed",
                "outcome": record["outcome"], "record": record}))
            return
        if method == "POST" and path == "/api/route-blocks":
            # The owner's blocks (docs/ROUTING.md 4.7): behind the loopback
            # listener, the Host check and the Origin check every POST gets,
            # and, as for the operator assist, only as JSON, which an HTML
            # form cannot send cross-site.
            content_type = (headers or {}).get("content-type", "").split(";")[0].strip()
            if content_type.casefold() != "application/json":
                await self._reply(writer, 415, _json_bytes({
                    "ok": False, "error": "send application/json"}))
                return
            data = json.loads(body or b"{}")
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            status, result = self._route_block_write(data)
            await self._reply(writer, status, _json_bytes(result))
            return
        if method == "POST" and path in ("/api/task", "/api/action"):
            data = json.loads(body or b"{}")
            result = (await self._set_task(data) if path == "/api/task"
                      else await self._action(data))
            await self._reply(writer, 200, _json_bytes(result))
            return
        static = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if method == "GET" and path in static:
            filename, content_type = static[path]
            await self._reply(writer, 200, (STATIC / filename).read_bytes(), content_type)
            return
        await self._reply(writer, 404, _json_bytes({"error": "not found"}))

    def _terrain(self, params: dict[str, list[str]]) -> dict[str, Any]:
        """The Terrain panel: recent fights and the learned rates."""
        character = getattr(self.client, "_memory_character", "")
        fights: list[dict[str, Any]] = []
        buckets: list[dict[str, Any]] = []
        total, busy = 0, False
        try:
            limit = max(1, min(200, int((params.get("limit") or ["30"])[0])))
        except ValueError:
            limit = 30
        if character:
            memory = self.client._memory_store()
            try:
                # A locked database must not hold up the event loop.
                fights = memory.fights(character, limit=limit, timeout=KNOWLEDGE_READ_TIMEOUT)
                buckets = memory.fight_buckets(character, now=time.time(),
                                               timeout=KNOWLEDGE_READ_TIMEOUT)
                total = memory.fight_count(character, timeout=KNOWLEDGE_READ_TIMEOUT)
            except sqlite3.OperationalError:
                fights, buckets, busy = [], [], True
        return {"fights": fights, "total": total, "rates": terrain.rates(buckets),
                "busy": busy,
                "terrain_known": getattr(self.client, "world_graph", None) is not None}

    def _owner_watch(self, params: dict[str, list[str]]) -> dict[str, Any]:
        """The Watching panel: whether the flag is on, whether the operator
        character is in view now, and the recent watched engagements."""
        config = self.client.config
        recorder = getattr(self.client, "owner_watch", None)
        character = getattr(self.client, "_memory_character", "")
        rows: list[dict[str, Any]] = []
        total, busy = 0, False
        try:
            limit = max(1, min(200, int((params.get("limit") or ["20"])[0])))
        except ValueError:
            limit = 20
        if character:
            memory = self.client._memory_store()
            try:
                rows = memory.owner_observations(character, limit=limit,
                                                 timeout=KNOWLEDGE_READ_TIMEOUT)
                total = memory.owner_observation_count(character,
                                                       timeout=KNOWLEDGE_READ_TIMEOUT)
            except sqlite3.OperationalError:
                rows, busy = [], True
        seen_at = getattr(recorder, "seen_at", None)
        return {
            "enabled": bool(getattr(config, "watch_owner", False)),
            "operator_configured": bool(str(getattr(config, "operator_character", "")).strip()),
            "in_view": seen_at is not None and time.time() - seen_at < 5.0,
            "engaged": bool(getattr(recorder, "active", False)),
            "shared_dropped": int(getattr(recorder, "shared_dropped", 0)),
            "observations": rows, "total": total, "busy": busy,
        }

    def _route_block_write(self, data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Add or remove one owner block; (HTTP status, reply)."""
        kind = data.get("action")
        try:
            if kind == "add":
                row = route_blocks.add_block(
                    self.client, self.graph, data.get("subject"),
                    data.get("seconds"), data.get("note", ""))
                self.client.record_action(
                    "route-block", f"{row['subject']} until {int(row['until'])}"
                    + (f": {row['note']}" if row["note"] else ""))
                return 200, {"ok": True, "block": row}
            if kind == "remove":
                subject = data.get("subject")
                if not route_blocks.remove_block(self.client, subject):
                    return 404, {"ok": False, "error": "no such block"}
                self.client.record_action("route-unblock", str(subject).strip())
                return 200, {"ok": True}
        except sqlite3.Error as exc:
            log.warning("route block write failed: %s", exc)
            return 503, {"ok": False, "error": "her memory database is busy; try again"}
        except ValueError as exc:
            return 400, {"ok": False, "error": str(exc)}
        return 400, {"ok": False, "error": "action must be add or remove"}

    KEY_REFUSAL_LOG_SECONDS = 60.0

    def _log_key_refusal(self) -> None:
        """At most one log line a minute, counting the refusals between."""
        now = time.monotonic()
        if now - self._key_refusals_logged_at < self.KEY_REFUSAL_LOG_SECONDS:
            self._key_refusals_unlogged += 1
            return
        log.warning("refused an operator-assist request without the operator key"
                    " (%d more since the last report)", self._key_refusals_unlogged)
        self._key_refusals_logged_at, self._key_refusals_unlogged = now, 0

    async def _replace_task(self, task: BotTask) -> dict[str, Any]:
        # The new task owns her: end an idle food trip now, as Stop does, so
        # one in flight cannot race the task (the engine drops it only on
        # its next step).
        survival = getattr(self.engine, "idle_survival", None)
        if survival is not None:
            survival.drop()
        previous = self.engine.task
        if previous is not None:
            previous.fail("replaced by operator")
        if self.client.state.phase == "playing":
            await self.client.clear_actions()
            await self.client.set_combat(False)
            await self.client.clear_target()
        self.engine.set_task(task)
        return {"ok": True, "task": task.name}

    def _ranked_spot(self, spot_id: str) -> dict[str, Any]:
        """A spot from the analyzer's current ranking, for any objective,
        with seam exposure folded in (spots it leaves out are not ranked)."""
        analyzer = getattr(self.client, "farm_analyzer", None)
        if analyzer is not None:
            for objective in OBJECTIVES:
                for spot in seam_ranking(self.client, self.graph, objective):
                    if str(spot.get("path")) == spot_id and analyzer_spot_legs(
                            spot, self.graph):
                        return spot
        raise ValueError("analyzer spot is no longer ranked")

    async def _set_task(self, data: dict[str, Any]) -> dict[str, Any]:
        kind = str(data.get("type", ""))
        if getattr(self.engine, "operator_hold", "") and kind != "stop":
            raise ValueError("an operator assist is running; start tasks after it ends")
        if kind == "stop":
            # Stop means "no tasks except survival": farming, questing and
            # navigation end, but the engine's idle guard still heals her and
            # feeds her (carried food, else a temple food trip), because a
            # connected Atrinik character keeps starving. Stop also ends a
            # running idle food trip and holds the next one off a while.
            self._hold_idle_survival("stopped by operator")
            # And it outlives a crash or restart: the ``autoplay`` command
            # stays idle until Start autoplay (systemd restarted a crashed
            # bot straight back into autoplay).
            unkept = self._persist_operator_stop(True)
            previous = self.engine.task
            if previous:
                previous.fail("stopped by operator")
                self.client.record_action(
                    "task-end", f"{previous.name} stopped by operator")
                self.engine.last_task = {
                    "name": previous.name, "status": "failed",
                    "error": previous.error, "started_at": previous.started_at,
                    "ended_at": time.time(),
                }
            if unkept:
                # The dashboard says so: a restart would autoplay again.
                now = time.time()
                last = self.engine.last_task if previous else {
                    "name": "stop", "status": "failed",
                    "started_at": now, "ended_at": now}
                last["error"] = f"stopped by operator; {unkept}"
                self.engine.last_task = last
            self.engine.set_task(None)
            if self.client.state.phase == "playing":
                await self.client.clear_actions()
                await self.client.set_combat(False)
                await self.client.clear_target()
            return {"ok": True, "task": None}
        if kind == "autoplay":
            until = max(1, min(115, int(data.get("until_level", 115) or 115)))
            self._persist_operator_stop(False)
            return await self._replace_task(AutoplayTask(self.graph, target_level=until))
        if kind == "quest":
            name = str(data.get("name", ""))
            if name == "all":
                return await self._replace_task(AllFormalQuestsTask(self.graph, self.catalog))
            if name == "Escaping the Deserted Island":
                return await self._replace_task(EscapingDesertedIslandTask(self.graph))
            if name not in POLICIES:
                raise ValueError("unknown formal quest")
            return await self._replace_task(
                CatalogQuestTask(self.graph, POLICIES[name], self.catalog))
        if kind == "navigate":
            destination = str(data.get("destination", ""))
            if destination not in self.graph.nodes:
                raise ValueError("destination is not an authored map path")
            xy = None
            if data.get("x") not in (None, "") and data.get("y") not in (None, ""):
                xy = (int(data["x"]), int(data["y"]))
            return await self._replace_task(NavigateTask(self.graph, destination, xy))
        if kind == "farm":
            zone, target, item = (str(data.get(key, ""))
                                  for key in ("zone", "target", "item"))
            task = FarmTask(zone=zone, target=target, item=item,
                            quantity=max(1, int(data.get("quantity", 1))),
                            level_until=max(0, int(data.get("until_level", 0))),
                            combat_skill=str(data.get("combat_skill", "")),
                            combat_spell=str(data.get("combat_spell", "")),
                            combat_skill_until_level=max(0, int(data.get(
                                "combat_skill_until_level", 0))),
                            neutral_targets=any(
                                name in target.casefold() for name in
                                ("wasp", "killer bee", "bee_killer")))
            node = self.graph.nodes.get(zone or self.client.state.map.path)
            if node is not None:
                task.map_bounds = (node.width, node.height)
                task.map_node = node
            priorities = self.graph.farm_priorities(
                zone or self.client.state.map.path, target)
            task.priority_spawns = [
                (spawn.x, spawn.y, spawn.named) for spawn in priorities]
            for spawn in priorities:
                for x, y in ((spawn.x + 1, spawn.y),
                             (spawn.x, spawn.y + 1),
                             (spawn.x - 1, spawn.y),
                             (spawn.x, spawn.y - 1),
                             (spawn.x + 4, spawn.y),
                             (spawn.x, spawn.y + 4),
                             (spawn.x - 4, spawn.y),
                             (spawn.x, spawn.y - 4)):
                    if (node is None or
                            (((node.walkable(x, y) if node.terrain else
                               0 <= x < node.width and 0 <= y < node.height)) and
                             (x, y) not in node.occupied)):
                        task.patrol.append((x, y))
            if zone.startswith("/"):
                if zone not in self.graph.nodes:
                    raise ValueError("farm zone is not an authored map path")
                task = NavigateThenTask(
                    self.graph, zone, task, combat_approach=True)
            return await self._replace_task(task)
        if kind == "farm_circuit":
            spot = None
            if data.get("analyzer_spot"):
                spot = self._ranked_spot(str(data["analyzer_spot"]))
                # The server's own ranking is authoritative for analyzer spots.
                data = dict(data, legs=[
                    {"zone": zone, "target": target}
                    for zone, target in analyzer_spot_legs(spot, self.graph)])
            raw_legs = data.get("legs", [])
            if not isinstance(raw_legs, list) or not raw_legs:
                raise ValueError("farm circuit requires at least one leg")
            legs = []
            for raw in raw_legs:
                if not isinstance(raw, dict):
                    raise ValueError("invalid farm circuit leg")
                zone = str(raw.get("zone", ""))
                target = str(raw.get("target", ""))
                if zone not in self.graph.nodes:
                    raise ValueError(
                        "farm circuit zone is not an authored map path")
                if target:
                    re.compile(target)
                legs.append((zone, target))
            return await self._replace_task(FarmCircuitTask(
                self.graph, legs,
                dwell_seconds=float(data.get("dwell_seconds", 90)),
                level_until=max(0, int(data.get("until_level", 0))),
                combat_skill=str(data.get("combat_skill", "")),
                combat_spell=str(data.get("combat_spell", "")),
                combat_skill_until_level=max(0, int(data.get(
                    "combat_skill_until_level", 0))),
                clear_hostile_route=bool(data.get(
                    "clear_hostile_route", False)),
                spot=spot))
        if kind == "sell_junk":
            merchant = str(data.get("merchant", "shop-floor")).strip() or "shop-floor"
            patterns = tuple(str(value) for value in data.get("patterns", []) if value)
            if not patterns:
                raise ValueError("supply at least one junk pattern")
            for pattern in patterns:
                re.compile(pattern)
            return await self._replace_task(SellJunkTask(
                merchant, JunkPolicy(patterns)))
        if kind == "sell_items":
            merchant = str(data.get("merchant", "shop-floor")).strip() or "shop-floor"
            tags = tuple(int(tag) for tag in data.get("tags", []))
            if not tags:
                raise ValueError("select at least one inventory item")
            return await self._replace_task(
                SellItemsTask(merchant, tags))
        if kind == "bank":
            banker = str(data.get("banker", "")).strip()
            if not banker:
                raise ValueError("banker NPC name is required")
            return await self._replace_task(BankTask(
                banker, str(data.get("amount", "all"))))
        if kind == "deposit":
            container = str(data.get("container", "")).strip()
            if not container:
                raise ValueError("storage container name is required")
            patterns = tuple(str(value) for value in data.get("patterns", []) if value)
            if not patterns:
                raise ValueError("supply at least one deposit pattern")
            return await self._replace_task(DepositItemsTask(
                container, patterns, indexer=storage.plain_indexer()))
        if kind == "retrieve":
            container = str(data.get("container", "")).strip()
            if not container:
                raise ValueError("storage container name is required")
            patterns = tuple(
                str(value) for value in data.get("patterns", []) if value)
            if not patterns:
                raise ValueError("supply at least one retrieve pattern")
            for pattern in patterns:
                re.compile(pattern)
            return await self._replace_task(RetrieveItemsTask(
                container, patterns, indexer=storage.plain_indexer()))
        raise ValueError("unknown task type")

    def _known_spots(self) -> set[str]:
        """Spots a build choice may name: every spot, the farmed one, and
        each spot of the analyzer's rankings."""
        known = {builds.EVERY_SPOT}
        status = getattr(self.client, "build_status", None)
        if isinstance(status, dict) and status.get("spot"):
            known.add(str(status["spot"]))
        report = getattr(getattr(self.client, "farm_analyzer", None), "report", None)
        for ranked in ((report or {}).get("rankings") or {}).values():
            for spot in ranked if isinstance(ranked, list) else ():
                if isinstance(spot, dict) and spot.get("path"):
                    known.add(str(spot["path"]))
        return known

    def _inventory_item(self, tag: Any):
        value = int(tag)
        item = self.client.state.items.get(value)
        if item is None or item.location != self.client.state.player_tag:
            raise ValueError("inventory item no longer exists")
        return item

    def _persist_operator_stop(self, stopped: bool) -> str:
        """Persist Stop (True) or Start autoplay; "" or why it is not kept."""
        setter = getattr(self.client, "set_operator_stopped", None)
        if setter is None or setter(stopped):
            return ""
        config = getattr(self.client, "config", None)
        if not getattr(config, "character", ""):
            reason = "not kept across a restart: no --character configured"
            if not self._unkept_stop_warned:
                self._unkept_stop_warned = True
                log.warning("operator Stop is %s", reason)
            return reason
        # The client logged why the save failed.
        return "not kept across a restart: saving it failed"

    def _hold_idle_survival(self, reason: str) -> None:
        """End the idle food trip and hold it off, as the operator took over."""
        survival = getattr(self.engine, "idle_survival", None)
        if survival is not None:
            survival.hold(self.client, reason)

    async def _action(self, data: dict[str, Any]) -> dict[str, Any]:
        kind = str(data.get("type", ""))
        if (getattr(self.engine, "operator_hold", "") and
                kind not in ("farm_objective", "build_choice", "retry_learn",
                             "refresh_quests")):
            # A manual order would move her or her gold mid-run.
            raise ValueError("an operator assist is running; act after it ends")
        if kind in ("move", "dialog", "apply", "apply_ground"):
            # A manual order would fight a running idle food trip.
            self._hold_idle_survival(f"manual {kind}")
        if kind == "farm_objective":
            objective = str(data.get("objective", ""))
            if objective not in OBJECTIVES:
                raise ValueError(f"unknown farming objective {objective!r}")
            self.client.set_farm_objective(objective)
            return {"ok": True, "objective": objective}
        if kind == "build_choice":
            choice = str(data.get("build", ""))
            if choice not in builds.CHOICES:
                raise ValueError(f"unknown build {choice!r}")
            spot = str(data.get("spot", "") or builds.EVERY_SPOT)
            if spot not in self._known_spots():
                raise ValueError("not a farm spot she knows")
            self.client.set_build_choice(spot, choice)
            return {"ok": True, "spot": spot, "build": choice}
        if kind == "retry_learn":
            # The operator retries a "get this build" errand that gave up.
            failed = dict(getattr(self.client, "learn_errand_failed", None) or {})
            if not failed:
                raise ValueError("no learn errand has given up")
            spell = str(data.get("spell", "") or "")
            if spell and spell not in failed:
                raise ValueError(f"the learn errand for {spell!r} has not given up")
            retried = [spell] if spell else list(failed)
            failures = dict(getattr(self.client, "learn_errand_failures", None) or {})
            for name in retried:
                failed.pop(name, None)
                failures.pop(name, None)
            self.client.learn_errand_failed = failed
            self.client.learn_errand_failures = failures
            # The circuit's route or dialogue backoff ends too
            # (_needs_learn_spell), never a seller's money refusal.
            self.client.learn_errand_retry = True
            return {"ok": True, "retried": retried}
        if kind == "chat":
            text = str(data.get("text", "")).strip()
            if not text:
                raise ValueError("chat text is empty")
            await self.client.command("say " + text)
        elif kind == "command":
            command = str(data.get("command", "")).strip()
            if not command:
                raise ValueError("command is empty")
            await self.client.execute_client_command(command)
        elif kind == "apply":
            await self.client.apply(self._inventory_item(data.get("tag")).tag)
        elif kind == "apply_ground":
            tag = int(data.get("tag"))
            item = next((candidate for candidate in self.client.state.ground
                         if candidate.tag == tag), None)
            if item is None:
                raise ValueError("ground item is no longer underfoot")
            await self.client.apply(item.tag)
        elif kind == "examine":
            await self.client.examine(self._inventory_item(data.get("tag")).tag)
        elif kind == "lock":
            await self.client.lock_item(self._inventory_item(data.get("tag")).tag)
        elif kind == "drop":
            item = self._inventory_item(data.get("tag"))
            if item.flags & (c.ITEM_APPLIED | c.ITEM_LOCKED):
                raise ValueError("refusing to drop an applied or locked item")
            await self.client.move_item(0, item.tag, item.quantity)
        elif kind == "pickup":
            tag = int(data.get("tag"))
            item = next((candidate for candidate in self.client.state.ground
                         if candidate.tag == tag), None)
            if item is None:
                raise ValueError("ground item is no longer underfoot")
            quantity = max(0, int(data.get("quantity", 0)))
            await self.client.move_item(
                self.client.state.player_tag, item.tag,
                min(quantity, item.quantity) if quantity else item.quantity)
        elif kind == "move":
            await self.client.move_to_view(int(data["x"]), int(data["y"]))
        elif kind == "dialog":
            await self.client.choose_interface_link(int(data["index"]))
        elif kind == "combat":
            await self.client.set_combat(bool(data.get("enabled")))
        elif kind == "refresh_quests":
            await self.client.request_quests()
        else:
            raise ValueError("unknown action type")
        return {"ok": True}
