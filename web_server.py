"""Dependency-free HTTP control plane for a persistent Atrinik client."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import constants as c
from .catalog_quest_tasks import (AllFormalQuestsTask, CatalogQuestTask,
                                  POLICIES, RECOMMENDED_QUEST_ORDER)
from .client import AtrinikClient, map_object_visual_name
from .farm_spots import farm_spot_catalog
from .navigation import (FarmCircuitTask, NavigateTask, NavigateThenTask,
                         ShopUpgradeSweepTask, WorldGraph)
from .navigation_spots import navigation_spot_catalog
from .quest_tasks import EscapingDesertedIslandTask
from .quests import QuestDefinition, QuestObjective, flatten_parts, load_catalog
from .tasks import (BankTask, BotTask, BuyShopUpgradeTask, DepositItemsTask,
                    FarmTask, InventoryPolicy, JunkPolicy, RetrieveItemsTask,
                    SellItemsTask, SellJunkTask, TaskEngine, TaskStatus)

log = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("web_static")

@lru_cache(maxsize=1)
def experience_thresholds() -> tuple[int, ...]:
    """Read an explicitly configured server level table, with a fallback."""
    server_source = os.getenv("ATRINIK_SERVER_SOURCE", "")
    if server_source:
        source = Path(server_source)
        if source.is_dir():
            source /= "src/server/exp.c"
        try:
            match = re.search(
                r"new_levels\[[^]]+\]\s*=\s*\{(.*?)\};",
                source.read_text(), re.S)
            values = tuple(
                int(value) for value in re.findall(r"\d+", match.group(1)))
            if len(values) >= 31:
                return values
        except (AttributeError, OSError, ValueError):
            pass
    return (
        0, 0, 2000, 4000, 8000, 16000, 32000, 64000, 125000,
        250000, 500000, 900000, 1400000, 2000000, 2600000,
        3300000, 4100000, 4900000, 5700000, 6600000, 7500000,
        8500000, 9500000, 10600000, 11800000, 13000000,
        14300000, 15600000, 17000000, 18500000, 20000000)


def experience_progress(level: int, experience: int) -> dict[str, Any]:
    thresholds = experience_thresholds()
    level = max(0, int(level or 0))
    experience = max(0, int(experience or 0))
    current = thresholds[level] if level < len(thresholds) else experience
    next_experience = (thresholds[level + 1]
                       if level + 1 < len(thresholds) else None)
    remaining = (max(0, next_experience - experience)
                 if next_experience is not None else 0)
    span = ((next_experience - current)
            if next_experience is not None else 0)
    percent = (100.0 if span <= 0 else
               max(0.0, min(100.0,
                            (experience - current) * 100.0 / span)))
    return {
        "experience": experience, "level_experience": current,
        "next_experience": next_experience,
        "remaining_experience": remaining,
        "progress_percent": round(percent, 1),
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


class DashboardState:
    def __init__(self, client: AtrinikClient, engine: TaskEngine,
                 graph: WorldGraph, catalog: dict[str, QuestDefinition]):
        self.client, self.engine = client, engine
        self.graph, self.catalog = graph, catalog
        self.started_at = time.time()

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
                    "objects": objects,
                })
        return {
            "name": game_map.name, "path": game_map.path,
            "region": game_map.region_longname or game_map.region,
            "weather": game_map.weather, "width": game_map.width,
            "height": game_map.height, "x": game_map.world_x,
            "y": game_map.world_y, "tiles": tiles,
        }

    def snapshot(self) -> dict[str, Any]:
        state, task = self.client.state, self.engine.task
        inventory = []
        skills = []
        equipped = set(state.equipment.values())
        for item in state.inventory:
            inventory.append({
                "tag": item.tag, "name": item.name, "quantity": item.quantity,
                "weight": item.weight, "type": item.item_type,
                "quality": item.quality, "condition": item.condition,
                "identified": InventoryPolicy().identified(item),
                "required_level": item.required_level,
                "applied": bool(item.flags & c.ITEM_APPLIED),
                "locked": bool(item.flags & c.ITEM_LOCKED),
                "magical": bool(item.flags & c.ITEM_MAGICAL),
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
            "face": self.client.faces.get(item.face, ""),
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
        return {
            "server_time": time.time(), "uptime": time.time() - self.started_at,
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
            },
            "task": task_data, "last_task": self.engine.last_task,
            "map": self.map(), "inventory": inventory, "ground": ground,
            "skills": sorted(skills, key=lambda skill: (-skill["level"], skill["name"])),
            "farm_spots": farm_spot_catalog(self.graph),
            "navigation_spots": navigation_spot_catalog(self.graph),
            "actions": self.client.action_history[-200:],
            "decisions": self.client.decision_history[-500:],
            "current_action": (self.client.action_history[-1]
                               if self.client.action_history else None),
            "messages": messages, "interface": interface,
            "quests_loaded": state.quests_loaded, "quests": self.quests(),
            "quest_journal": self.quest_journal(),
        }


class WebControlServer:
    def __init__(self, client: AtrinikClient, engine: TaskEngine, graph: WorldGraph,
                 host: str = "127.0.0.1", port: int = 8765):
        if not self._is_loopback_host(host):
            raise ValueError("the operator dashboard may only listen on loopback")
        self.client, self.engine, self.graph = client, engine, graph
        self.host, self.port = host, port
        self.catalog = load_catalog()
        self.dashboard = DashboardState(client, engine, graph, self.catalog)
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
                     content_type: str = "application/json; charset=utf-8") -> None:
        labels = {200: "OK", 201: "Created", 204: "No Content",
                  400: "Bad Request", 403: "Forbidden", 404: "Not Found",
                  405: "Method Not Allowed",
                  409: "Conflict", 413: "Content Too Large", 500: "Internal Server Error"}
        header = (f"HTTP/1.1 {status} {labels.get(status, 'Error')}\r\n"
                  f"Content-Type: {content_type}\r\nContent-Length: {len(body)}\r\n"
                  "Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n"
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
            path = urlsplit(raw_target).path
            await self._route(writer, method, path, body)
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
                     path: str, body: bytes) -> None:
        if method == "GET" and path == "/api/state":
            await self._reply(writer, 200, _json_bytes(self.dashboard.snapshot()))
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

    async def _replace_task(self, task: BotTask) -> dict[str, Any]:
        previous = self.engine.task
        if previous is not None:
            previous.fail("replaced by operator")
        if self.client.state.phase == "playing":
            await self.client.clear_actions()
            await self.client.set_combat(False)
            await self.client.clear_target()
        self.engine.set_task(task)
        return {"ok": True, "task": task.name}

    async def _set_task(self, data: dict[str, Any]) -> dict[str, Any]:
        kind = str(data.get("type", ""))
        if kind == "stop":
            previous = self.engine.task
            if previous:
                previous.fail("stopped by operator")
                self.engine.last_task = {
                    "name": previous.name, "status": "failed",
                    "error": previous.error, "started_at": previous.started_at,
                    "ended_at": time.time(),
                }
            self.engine.set_task(None)
            if self.client.state.phase == "playing":
                await self.client.clear_actions()
                await self.client.set_combat(False)
                await self.client.clear_target()
            return {"ok": True, "task": None}
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
                    "clear_hostile_route", False))))
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
                container, patterns))
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
                container, patterns))
        raise ValueError("unknown task type")

    def _inventory_item(self, tag: Any):
        value = int(tag)
        item = self.client.state.items.get(value)
        if item is None or item.location != self.client.state.player_tag:
            raise ValueError("inventory item no longer exists")
        return item

    async def _action(self, data: dict[str, Any]) -> dict[str, Any]:
        kind = str(data.get("type", ""))
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
